"""RetroAchievements progress and unlock alerts.

Reads the RetroAchievements website, not your console, so it works whatever is
actually awarding the achievements: an RA-enabled emulator, RALibretro, or
RA2Snes on real hardware through the FXPak's USB port.

Timing is the thing to understand here. Your RA client plays its unlock sound
the instant it happens, but this only finds out by asking the site — so the
alert poll interval is the delay a viewer sees. The progress text gets around
its own slower interval by being woken when an unlock is spotted, rather than
by polling harder.
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request

API_BASE = "https://retroachievements.org/API/"
USER_AGENT = "smw-stream-tools/1.0"

MIN_PROGRESS_POLL_S = 30
MIN_ALERT_POLL_S = 5
REFRESH_SETTLE_S = 2.0      # RA's progress endpoint can trail its unlock feed


def _api_get(endpoint, api_key, params, timeout=10):
    query = dict(params)
    query["y"] = api_key
    url = API_BASE + endpoint + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _pick(record, *names):
    """Field casing differs between the raw PHP API and the client libraries."""
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return None


def fetch_progress(user, api_key, game_id=None, hardcore=False):
    """Progress for a game, or for whatever is being played right now."""
    if game_id:
        data = _api_get("API_GetGameInfoAndUserProgress.php", api_key,
                        {"u": user, "g": game_id})
        if not data:
            return None
        earned = (_pick(data, "NumAwardedToUserHardcore", "numAwardedToUserHardcore")
                  if hardcore else
                  _pick(data, "NumAwardedToUser", "numAwardedToUser"))
        total = _pick(data, "NumAchievements", "numAchievements")
        title = _pick(data, "Title", "title")
        stamp = None          # a pinned game is trusted; nothing to confirm
    else:
        data = _api_get("API_GetUserRecentlyPlayedGames.php", api_key,
                        {"u": user, "c": 1})
        if not data:
            return None
        record = data[0]
        earned = (_pick(record, "NumAchievedHardcore") if hardcore
                  else _pick(record, "NumAchieved"))
        total = _pick(record, "NumPossibleAchievements")
        title = _pick(record, "Title")
        game_id = _pick(record, "GameID")
        # RA's own timestamp for this game. Compared only against the previous
        # one we saw, never against the clock, so its timezone doesn't matter.
        stamp = _pick(record, "LastPlayed")
    if total is None:
        return None
    earned, total = int(earned or 0), int(total)
    return {"earned": earned, "total": total, "title": title or "",
            "game_id": game_id, "stamp": stamp,
            "pct": (100.0 * earned / total) if total else 0.0}


def fetch_recent(user, api_key, minutes=60):
    """Achievements unlocked in the last `minutes`, newest first."""
    return _api_get("API_GetUserRecentAchievements.php", api_key,
                    {"u": user, "m": minutes}) or []


def badge_url(record):
    direct = _pick(record, "BadgeURL", "badgeUrl")
    if direct:
        return (direct if direct.startswith("http")
                else "https://media.retroachievements.org" + direct)
    name = _pick(record, "BadgeName", "badgeName")
    return ("https://media.retroachievements.org/Badge/%s.png" % name) if name else ""


def format_progress(template, progress):
    return (template
            .replace("{earned}", str(progress["earned"]))
            .replace("{total}", str(progress["total"]))
            .replace("{title}", progress["title"])
            .replace("{pct}", "%.0f" % progress["pct"]))


class RetroTracker(object):
    """Two loops: one for the progress text, one for unlock alerts."""

    def __init__(self, config, data_dir, alerts, log=None, on_change=None):
        self.config = config
        self.alerts = alerts
        self.log = log or (lambda *_: None)
        self.on_change = on_change or (lambda: None)
        self.seen_path = os.path.join(data_dir, "ra_seen.json")
        self.state = {"status": "off", "text": "", "earned": None,
                      "total": None, "game": ""}
        self._seen = None
        # Is RA actually tracking the hack that is loaded right now?
        #
        # With no Game ID pinned, we ask RA what this user played most
        # recently — and RA only knows about games it recognises. Load a hack
        # with no achievement set and RA never hears about it, so it keeps
        # answering with the *previous* hack forever. That is why the readout
        # used to sit there showing counts for a game you had stopped playing.
        #
        # The console knows better: it tells us the instant the ROM changes.
        # So a ROM change drops the readout, and it comes back only once RA
        # shows evidence of tracking the new one — a different game, or its
        # own LastPlayed moving on.
        self._confirmed = False
        self._pending_since = None
        self._last_stamp = None
        self._last_game = None
        self._run = False
        self._threads = []
        self._generation = 0
        self._refresh = threading.Event()
        self._lock = threading.Lock()

    # -- seen unlocks -----------------------------------------------------

    def _load_seen(self):
        try:
            with open(self.seen_path, "r", encoding="utf-8") as handle:
                return set(json.load(handle))
        except Exception:
            return set()

    def _save_seen(self):
        try:
            os.makedirs(os.path.dirname(self.seen_path), exist_ok=True)
            with open(self.seen_path, "w", encoding="utf-8") as handle:
                json.dump(sorted(self._seen)[-500:], handle)
        except Exception:
            pass

    # -- lifecycle --------------------------------------------------------

    def start(self):
        self.stop()
        with self._lock:
            self._generation += 1
            generation = self._generation
        self._run = True
        for target in (self._progress_loop, self._alert_loop):
            thread = threading.Thread(target=target, args=(generation,),
                                      daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self):
        self._run = False
        self._refresh.set()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=3.0)
        self._threads = []
        self._refresh.clear()

    def note_rom_change(self, rom):
        """The console loaded a different hack.

        Treat the readout as unproven from here: whatever RA last said was
        about the hack before this one.
        """
        with self._lock:
            self._confirmed = False
            self._pending_since = time.time()
        self._set(text="", earned=None, total=None, game="")
        self._refresh.set()        # ask RA again now rather than at the next poll

    def _active(self, generation):
        """False once this thread has been superseded.

        An API call can take ten seconds, longer than the join above waits, so
        a thread can outlive its stop(). `_run` is back to True by then, so
        without this check the old loop carries on — two alert loops polling
        RA, firing the same unlock twice and doubling the request rate.
        """
        return self._run and generation == self._generation

    def _set(self, **kwargs):
        with self._lock:
            self.state.update(kwargs)
        self.on_change()

    def _ready(self):
        cfg = self.config
        return bool(cfg["enable_ra"] and cfg["ra_user"] and cfg["ra_api_key"])

    # -- progress ---------------------------------------------------------

    def _confirm(self, progress):
        """Decide whether RA's answer is about the hack actually loaded.

        Returns the progress unchanged once there is evidence it is current,
        or None while there isn't — which reads downstream as "this hack has
        no achievements" and hides the line.

        Only used when following whatever is being played. A pinned Game ID is
        a deliberate choice and is always trusted.
        """
        game = progress.get("game_id")
        stamp = progress.get("stamp")
        with self._lock:
            first_look = self._last_game is None
            moved = (game != self._last_game) or (
                stamp is not None and stamp != self._last_stamp)
            self._last_game, self._last_stamp = game, stamp

            if first_look:
                # Nothing to compare against at startup. Trust it rather than
                # blanking a readout that is probably right.
                self._confirmed = True
            elif moved:
                # RA moved on: either a different game, or more play recorded
                # against this one. Either way it is tracking what is loaded.
                if not self._confirmed:
                    self.log("RA: now tracking %s" % (progress.get("title") or game))
                self._confirmed = True
            confirmed = self._confirmed
        return progress if confirmed else None

    def _progress_loop(self, generation):
        backoff = 1.0
        while self._active(generation):
            if not self._ready():
                self._set(status="off", text="")
                time.sleep(2)
                continue
            cfg = self.config
            try:
                progress = fetch_progress(cfg["ra_user"], cfg["ra_api_key"],
                                          cfg["ra_game_id"].strip() or None,
                                          cfg["ra_hardcore"])
                backoff = 1.0
                if progress is not None and not cfg["ra_game_id"].strip():
                    progress = self._confirm(progress)
                if progress is None:
                    # Clear the numbers too, not just the text. They are what
                    # the overlay uses to decide whether this hack has a set at
                    # all, and leaving the previous hack's totals behind would
                    # keep the row on screen showing counts for a game you are
                    # no longer playing.
                    self._set(status="no game with achievements", text="",
                              earned=None, total=None, game="")
                else:
                    self._set(status="ok",
                              text=format_progress(cfg["ra_format"], progress),
                              earned=progress["earned"], total=progress["total"],
                              game=progress["title"])
            except Exception as exc:
                # RA has taken endpoints offline for load before. Treat any
                # failure as transient and keep the last good text on screen.
                self._set(status="API error: %s" % exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
                continue

            interval = max(cfg["ra_progress_poll_s"], MIN_PROGRESS_POLL_S)
            deadline = time.time() + interval
            while self._active(generation):
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                if self._refresh.wait(min(0.5, remaining)):
                    self._refresh.clear()
                    if not self._active(generation):
                        break
                    time.sleep(REFRESH_SETTLE_S)
                    break

    # -- alerts -----------------------------------------------------------

    def _alert_loop(self, generation):
        first_pass = True
        backoff = 1.0
        while self._active(generation):
            cfg = self.config
            if not (self._ready() and cfg["ra_alerts_on"]):
                time.sleep(2)
                continue
            try:
                interval = max(cfg["ra_alert_poll_s"], MIN_ALERT_POLL_S)
                # Look back further than the interval so nothing falls between
                # two requests.
                minutes = max(2, int(interval / 60.0) + 2)
                recent = fetch_recent(cfg["ra_user"], cfg["ra_api_key"], minutes)
                backoff = 1.0

                if self._seen is None:
                    self._seen = self._load_seen()

                fresh = []
                for record in reversed(recent):   # oldest first, so alerts queue in order
                    ach_id = _pick(record, "AchievementID", "achievementId")
                    if ach_id is None or str(ach_id) in self._seen:
                        continue
                    self._seen.add(str(ach_id))
                    fresh.append(record)

                if fresh:
                    self._save_seen()
                    if first_pass:
                        # Learn what is already unlocked without replaying it.
                        self.log("RA alerts ready (%d recent unlocks noted)" % len(fresh))
                    else:
                        self._refresh.set()     # update the progress text too
                        for record in fresh:
                            self.alerts.push({
                                "title": _pick(record, "Title", "title") or "Achievement",
                                "description": _pick(record, "Description", "description") or "",
                                "points": int(_pick(record, "Points", "points") or 0),
                                "game": _pick(record, "GameTitle", "gameTitle") or "",
                                "badge": badge_url(record),
                            })
                        self.log("RA: %d unlock(s)" % len(fresh))
                elif first_pass:
                    self.log("RA alerts ready")
                first_pass = False
            except Exception as exc:
                self.log("RA alert poll failed: %s" % exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
                continue

            slept = 0.0
            interval = max(cfg["ra_alert_poll_s"], MIN_ALERT_POLL_S)
            while self._active(generation) and slept < interval:
                time.sleep(0.5)
                slept += 0.5
