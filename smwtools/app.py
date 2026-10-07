"""The orchestrator: owns every source of truth and writes to OBS.

Splitting this out is the point of merging the old scripts. Previously the
kaizoff script owned the exits text with a hotkey-driven completed count, while
the tracker script overwrote that same count from console RAM — two writers
fighting over one text source. Here there is one writer, fed by both: the hack
name, exit total and author come from kaizoff; the completed exits and deaths
come from the console.
"""

import os
import threading
import time

from . import kaizoff as kz
from .config import DATA_DIR
from . import layout
from .obsws import ObsClient, ObsError, ObsRequestError
from .retro import RetroTracker
from .scan import ScanSession
from .snes import SnesTracker

# Which settings force which worker to restart. Anything not listed here — a
# source name, a format string, a layout number — is picked up by the next
# render pass without dropping a connection.
OBS_KEYS = ("obs_url", "obs_password", "enable_obs")
TWITCH_KEYS = ("twitch_channel", "twitch_client_id", "twitch_client_secret",
               "enable_twitch", "allow_insecure_hacks")
SNES_KEYS = ("snes_url", "snes_device", "exit_addr", "gate_addr", "gate_min",
             "gate_max", "arm_modes", "arm_after_s", "death_addr",
             "death_value", "death_mode", "poll_ms", "rom_addr", "enable_snes")
RA_KEYS = ("ra_user", "ra_api_key", "ra_game_id", "ra_hardcore", "enable_ra",
           "ra_progress_poll_s", "ra_alerts_on", "ra_alert_poll_s")
MANUAL_KEYS = ("manual_on", "manual_name", "manual_exits", "manual_author")


class App(object):
    def __init__(self, config, alerts=None):
        self.config = config
        self.data_dir = DATA_DIR
        os.makedirs(self.data_dir, exist_ok=True)

        self.log_lines = []
        self._log_lock = threading.Lock()

        self.kaizoff = kz.Kaizoff(self.data_dir, log=self.log)
        self.snes = SnesTracker(config, self.data_dir, log=self.log,
                                on_change=self.request_render,
                                on_rom_change=self._rom_changed)
        self.obs = ObsClient(config["obs_url"], config["obs_password"])
        self.scan = ScanSession(self.snes, log=self.log)
        self.retro = RetroTracker(config, self.data_dir, alerts, log=self.log,
                                  on_change=self.request_render) if alerts else None

        self.state = {
            "obs": {"connected": False, "status": "not connected", "version": ""},
            "twitch": {"status": "idle", "title": "", "match": ""},
            "hack": None,          # {name, display, exits, author}
            "text_sources": [],
        }
        self._state_lock = threading.Lock()
        self._render = threading.Event()
        self._run = False
        self._threads = []
        self._last_written = {}
        self._missing = {}
        self._fitted_for = None

    # -- logging ----------------------------------------------------------

    def log(self, message):
        stamp = time.strftime("%H:%M:%S")
        line = "%s  %s" % (stamp, message)
        with self._log_lock:
            self.log_lines.append(line)
            del self.log_lines[:-200]
        print(line, flush=True)

    def recent_log(self):
        with self._log_lock:
            return list(self.log_lines)

    # -- state ------------------------------------------------------------

    def set_state(self, section, **kwargs):
        with self._state_lock:
            if section:
                self.state.setdefault(section, {}).update(kwargs)
            else:
                self.state.update(kwargs)

    def snapshot(self):
        """Everything the UI shows, in one object."""
        with self._state_lock:
            base = {k: (dict(v) if isinstance(v, dict) else v)
                    for k, v in self.state.items()}
        snes = dict(self.snes.state)
        hack = base.get("hack")
        base["snes"] = snes
        base["exits_text"] = self.compose_exits()
        base["deaths_text"] = self.compose_deaths()
        base["name_text"] = hack["display"] if hack else ""
        base["author_text"] = self.compose_author()
        ra = dict(self.retro.state) if self.retro else {"status": "off"}
        base["ra"] = ra
        # One place decides whether this hack has achievements at all, so the
        # overlay and anything else asking get the same answer. A total means a
        # set exists; earned may legitimately be 0. Kept true through a
        # transient API error, because the last good numbers are still on
        # screen and blinking the row off mid-stream would be worse.
        base["ra_available"] = bool(self.config["enable_ra"] and ra.get("total"))
        base["scan"] = self.scan.status()
        return base

    def request_render(self):
        self._render.set()

    def _rom_changed(self, rom):
        """A different hack is loaded.

        RetroAchievements can't see this: a hack with no achievement set never
        reaches its servers, so it keeps reporting the previous hack. The
        console is the only thing that knows, so it is what tells the readout
        to stop trusting what it has.
        """
        if self.retro:
            self.retro.note_rom_change(rom)

    # -- text composition --------------------------------------------------

    def compose_exits(self):
        """`done` comes from the console when it's connected, otherwise from
        the per-hack count on disk, so the text is still right while offline."""
        hack = self.state.get("hack")
        if not hack:
            return ""
        total = int(hack.get("exits") or 0)
        done = self.snes.state.get("exits")
        if done is None:
            done = self.kaizoff.get_progress(hack["name"])
        done = int(done or 0)
        if total > 0:
            done = max(0, min(done, total))
        width = max(2, len(str(total)))
        template = (self.config["exits_format"] or "Exits {done}/{total}")
        template = template.replace("\\n", "\n")
        try:
            return template.format(
                done=str(done).zfill(width), total=str(total).zfill(width),
                done_raw=done, total_raw=total,
                name=hack["display"], author=hack.get("author", ""),
                difficulty=hack.get("difficulty", ""), type=hack.get("type", ""),
                deaths=self.snes.state.get("deaths") or 0)
        except (KeyError, IndexError, ValueError):
            self.log("bad exits format %r — using the default" % template)
            return "Exits %s/%s" % (str(done).zfill(width), str(total).zfill(width))

    def compose_deaths(self):
        deaths = self.snes.state.get("deaths")
        if deaths is None:
            return ""
        template = (self.config["deaths_format"] or "Deaths: {deaths}")
        template = template.replace("\\n", "\n")
        try:
            return template.format(deaths=deaths)
        except (KeyError, IndexError, ValueError):
            return "Deaths: %d" % deaths

    def _wants_author(self):
        """Is the author going to be shown anywhere?

        It costs an extra request to kaizoff, so it is only fetched when
        something will display it. Every way of displaying it has to be listed
        here — the browser overlay was missing, so ticking its author row
        showed nothing at all for anyone not also writing to an OBS source.
        """
        cfg = self.config
        return bool(cfg["author_source"]
                    or cfg["ov_show_author"]
                    or "{author" in (cfg["exits_format"] or ""))

    def compose_author(self):
        hack = self.state.get("hack")
        author = (hack or {}).get("author", "")
        if not author:
            return ""
        template = (self.config["author_format"] or "BY: {author}").replace("\\n", "\n")
        try:
            return template.format(author=author)
        except (KeyError, IndexError, ValueError):
            return "BY: %s" % author

    # -- OBS writing -------------------------------------------------------

    def _write(self, source_key, text):
        """Write to a source, skipping anything that hasn't changed."""
        name = self.config[source_key]
        if not name or text is None:
            return
        if self._last_written.get(source_key) == (name, text):
            return
        try:
            self.obs.set_text(name, text)
            self._last_written[source_key] = (name, text)
        except ObsRequestError as exc:
            # Usually a source name that doesn't exist in OBS. Say so once
            # rather than every pass, and leave the connection alone.
            if self._missing.get(source_key) != name:
                self._missing[source_key] = name
                self.log("no OBS source named %r — check the setting for %s"
                         % (name, source_key.replace("_source", "")))
        except ObsError:
            self._last_written.pop(source_key, None)
            raise
        else:
            self._missing.pop(source_key, None)

    def render(self):
        if not (self.config["enable_obs"] and self.obs.connected):
            return
        try:
            hack = self.state.get("hack")
            name = hack["display"] if hack else None
            if name and self._fitted_for != (self.config["name_source"], name):
                # Fitting costs a handful of round trips, so only redo it when
                # the name itself changes — not on every exit or death update.
                written = layout.fit_name(self.obs, self.config, name, self.log)
                self._fitted_for = (self.config["name_source"], name)
                self._last_written["name_source"] = (self.config["name_source"], written)
                layout.reflow_exits(self.obs, self.config, self.log)
            elif name is None:
                self._fitted_for = None

            self._write("exits_source", self.compose_exits() or None)
            self._write("deaths_source", self.compose_deaths() or None)
            self._write("author_source", self.compose_author() or None)
            if self.retro:
                self._write("ra_source", self.retro.state.get("text") or None)
        except ObsError:
            self.set_state("obs", connected=False, status="lost connection")
            self.obs.close()

    # -- workers -----------------------------------------------------------

    def _obs_loop(self):
        backoff = 1.0
        while self._run:
            if not self.config["enable_obs"]:
                if self.obs.connected:
                    self.obs.close()
                self.set_state("obs", connected=False, status="off")
                time.sleep(2)
                continue
            if self.obs.connected:
                time.sleep(1.0)
                continue
            self.obs.url = self.config["obs_url"]
            self.obs.password = self.config["obs_password"]
            try:
                version = self.obs.connect()
                self.set_state("obs", connected=True, version=version,
                               status="connected")
                self.log("OBS connected (obs-websocket %s)" % version)
                backoff = 1.0
                try:
                    self.set_state(None, text_sources=self.obs.text_sources())
                except ObsError:
                    pass
                self._last_written.clear()
                self._fitted_for = None
                self.request_render()
            except ObsError as exc:
                self.set_state("obs", connected=False, status=str(exc))
                time.sleep(backoff)
                backoff = min(backoff * 2, 20.0)

    def _render_loop(self):
        while self._run:
            # Coalesce bursts of changes into one write pass.
            self._render.wait(1.0)
            self._render.clear()
            self.render()

    def _twitch_loop(self):
        last_title = None
        while self._run:
            cfg = self.config
            if self._manual_active():
                # Nothing to look up: the name and total were typed in. Don't
                # spend API calls on a title whose answer would be discarded.
                self.set_state("twitch", status="off (manual hack)")
                time.sleep(2)
                continue
            if not (cfg["enable_twitch"] and cfg["twitch_channel"]
                    and cfg["twitch_client_id"] and cfg["twitch_client_secret"]):
                self.set_state("twitch", status="off")
                time.sleep(2)
                continue
            try:
                title = self.kaizoff.stream_title(
                    cfg["twitch_channel"], cfg["twitch_client_id"],
                    cfg["twitch_client_secret"])
                self.set_state("twitch", status="ok", title=title)
                if title and title != last_title:
                    last_title = title
                    self._apply_title(title)
            except Exception as exc:
                self.set_state("twitch", status="error: %s" % exc)
            self._sleep(max(cfg["title_poll_seconds"], 15))

    def _manual_active(self):
        """Is the typed-in hack actually overriding anything?

        Ticked but with the name left blank is not an override — it would
        otherwise freeze whatever was last shown, which is the least
        predictable thing it could do.
        """
        return bool(self.config["manual_on"]
                    and (self.config["manual_name"] or "").strip())

    def _apply_manual(self):
        """Use the hack name and exit total typed into the settings page.

        Exits and deaths are read from the console and are not affected by any
        of this — the console is the only thing that knows them. What the index
        supplies is the name and the total, and it cannot know about every
        hack. This is the way out for an unlisted hack, a title the matcher
        refuses, or anyone who would rather not register a Twitch app at all.
        """
        cfg = self.config
        name = (cfg["manual_name"] or "").strip()
        if not name:
            return False
        total = max(0, int(cfg["manual_exits"] or 0))
        current = self.state.get("hack") or {}
        author = (cfg["manual_author"] or "").strip()
        if (current.get("name") == name and current.get("exits") == total
                and current.get("author", "") == author):
            return True            # already showing this; don't churn the overlay
        self.set_state(None, hack={
            "name": name, "display": name, "exits": total, "author": author,
            "difficulty": "", "type": "", "id": None, "manual": True,
        })
        self.log("manual hack: %s (%s exits)%s"
                 % (name, total, " by %s" % author if author else ""))
        self.request_render()
        return True

    def _apply_title(self, title):
        cfg = self.config
        if self._manual_active():
            return                 # the typed-in hack wins
        hacks = self.kaizoff.hacks or self.kaizoff.fetch_hacks(
            cfg["hack_cache_hours"], cfg["allow_insecure_hacks"])
        hack, reason = kz.match_hack(title, hacks, self.kaizoff.overrides, self.log)
        self.set_state("twitch", match=reason)
        if not hack:
            self.log("no hack matched: %s (%s)" % (title, reason))
            return
        current = self.state.get("hack")
        if current and current["name"] == hack["name"]:
            return
        author = ""
        if self._wants_author():
            author = self.kaizoff.fetch_authors(hack.get("id"),
                                                cfg["allow_insecure_hacks"])
        display = self.kaizoff.display_name(hack)
        if cfg["auto_shorten"]:
            display = kz.trim_subtitle(display)
        self.set_state(None, hack={
            "name": hack["name"], "display": display,
            "exits": hack.get("exits"), "author": author,
            "difficulty": hack.get("difficulty", ""), "type": hack.get("type", ""),
            "id": hack.get("id"),
        })
        self.log("now showing: %s (%s exits)%s"
                 % (hack["name"], hack.get("exits"),
                    " by %s" % author if author else ""))
        self.request_render()

    def _sleep(self, seconds):
        """Sleep in slices so a stop or a settings change is picked up fast."""
        end = time.time() + seconds
        while self._run and time.time() < end:
            time.sleep(0.25)

    # -- control -----------------------------------------------------------

    def refresh_now(self):
        """Force a hack-index refresh and re-match the current title."""
        cfg = self.config
        self.kaizoff.fetch_hacks(cfg["hack_cache_hours"],
                                 cfg["allow_insecure_hacks"], force=True)
        title = self.state["twitch"].get("title")
        if title:
            self.set_state(None, hack=None)
            self._apply_title(title)
        return True

    def set_manual_exits(self, value):
        """Used when no console is connected — keeps the old hotkey behaviour."""
        hack = self.state.get("hack")
        if hack:
            self.kaizoff.set_progress(hack["name"], max(0, int(value)))
        self.request_render()

    def start(self):
        self._run = True
        if self._manual_active():
            self._apply_manual()
        for target in (self._obs_loop, self._render_loop, self._twitch_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)
        if self.config["enable_snes"]:
            self.snes.start()
        if self.retro:
            self.retro.start()
        self.log("started")

    def stop(self):
        self._run = False
        if self.retro:
            self.retro.stop()
        self.snes.stop()
        self.obs.close()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=2.0)
        self._threads = []

    def restart_workers(self, changed=None):
        """Called after a settings change.

        Only the parts whose settings actually moved are restarted. Bouncing
        the console connection on every save is expensive in a way that isn't
        obvious: reconnecting costs the arm dwell, so saving a setting while
        playing used to stall the exit count for ten seconds — and saving a few
        in a row could stall it indefinitely.
        """
        touched = None if changed is None else set(changed)

        def moved(keys):
            return touched is None or bool(touched & set(keys))

        if moved(OBS_KEYS):
            self.obs.close()
            self.set_state("obs", connected=False, status="reconnecting")
        if moved(TWITCH_KEYS):
            self.kaizoff.forget_channel()
        if moved(SNES_KEYS):
            self.snes.stop()
            if self.config["enable_snes"]:
                self.snes.start()
        if self.retro and moved(RA_KEYS):
            self.retro.stop()
            self.retro.start()
        if moved(MANUAL_KEYS):
            if self._manual_active():
                self._apply_manual()
            else:
                # Back to the index: drop the typed-in hack and re-match the
                # last title, rather than leaving it on screen until the title
                # happens to change.
                if (self.state.get("hack") or {}).get("manual"):
                    self.set_state(None, hack=None)
                title = self.state["twitch"].get("title")
                if title:
                    self._apply_title(title)

        # Turning on anything that shows the author has to fetch it now. It is
        # otherwise only looked up when the hack changes, so ticking the box
        # mid-stream would leave the line blank until the next hack.
        # A manual hack has no index entry to look an author up in — whatever
        # was typed is all there is.
        hack = self.state.get("hack")
        if (hack and not hack.get("manual") and hack.get("id")
                and self._wants_author() and not hack.get("author")):
            author = self.kaizoff.fetch_authors(
                hack.get("id"), self.config["allow_insecure_hacks"])
            if author:
                self.set_state(None, hack=dict(hack, author=author))
                self.log("author: %s" % author)

        # Source names and formats may have changed even when nothing restarted.
        self._last_written.clear()
        self._fitted_for = None
        self.request_render()
