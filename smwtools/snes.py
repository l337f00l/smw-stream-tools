"""Reads exits and deaths out of SNES memory over usb2snes (QUsb2Snes).

Works the same against an FXPak Pro on real hardware and against an emulator,
because the server presents both through the same protocol and address map.

The detection here is the configuration that survived testing on hardware:
exits come from the game's own counter, so the hack decides what counts as an
exit and things like non-counting switch palaces are filtered for free; deaths
come from the sprite-lock byte, which holds a distinct value for about a second
during a death and different values again for pipes and doors, so room
transitions never look like deaths.
"""

import json
import os
import threading
import time

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover - surfaced in the UI instead
    websocket = None

DEFAULT_URL = "ws://localhost:23074"   # 8080 is QUsb2Snes' deprecated port
APP_NAME = "SMW Stream Tools"
WRAM_BASE = 0xF50000
FILLER = 0x55          # what WRAM reads as while a ROM is loading

# How often the death byte is sampled while you are in a level. Measured on
# hardware, a prompt-retry hack can clear it in 23ms — under two frames — so
# the 200ms used for everything else catches only a fraction of deaths. One
# byte this often is cheap; everything else this often is not, which is why
# only this read is singled out. The loop is time-bounded, so a slow link
# simply gets fewer samples rather than falling behind.
DEATH_WATCH_MS = 20

# Game modes from here up mean "in a level" rather than on the overworld. The
# level number is only re-read when the mode crosses this line, because that is
# the only moment it can change — a death-and-retry reload stays above it the
# whole way (0F -> 11 -> 12 -> 13 -> 14 on hardware), so retries cost no reads.
IN_LEVEL_MODE = 0x0F

# The level number is read once the game has settled into play, not the moment
# it starts fading in. The translevel byte is "set during transfer from world
# map to level", so reading it at the fade can catch it before it has been
# written and record level 00 for a level that is nothing of the sort.
SETTLED_MODE = 0x14

# How long the death byte may sit at the death value before it is worth
# mentioning. A death animation is over in well under a second, but a retry
# prompt holds sprites locked until the player answers it, and a player who
# steps away or talks to chat can leave one up for minutes. Three seconds —
# the first guess here — called that a fault on a hack that was counting
# perfectly, which is worse than saying nothing.
HELD_WARN_S = 90

# $9D locks sprites for several reasons — dying, grabbing a powerup, a Yoshi
# hatching out of a block — so a lock appearing is not proof of a death. The
# player's own animation trigger is, and the two are consulted together: when
# the lock appears, this byte is watched briefly for the dying animation.
#
# It is not simply used on its own, because it is far too brief to catch by
# polling — that was tried first and lost most deaths. Watched only in the
# moment a lock appears, which is rare, it costs nothing and settles the
# question the lock byte cannot.
CONFIRM_ADDR = 0xF50071
CONFIRM_VALUE = 0x09
CONFIRM_MS = 250

# How many sprite locks may be rejected in a row, with no death in between,
# before the animation check is abandoned for that hack. A player hitting that
# many Yoshi blocks without dying is less likely than the trigger being missed,
# and silently losing every death is far worse than counting a few extra.
GIVE_UP_AFTER = 8

# Kevin's Retry System — the retry patch most modern kaizo hacks use — keeps a
# death total at a documented offset from its free-RAM base, five bytes holding
# one decimal digit each. Five bytes all in 0-9 is a fingerprint: arbitrary
# memory manages it about once in ten million. Probed and logged only, because
# a hack can move the base and this is not yet something to count deaths from.
RETRY_DEATHS = 0xF6B43B
RETRY_DEATH_DIGITS = 5

# Emulator devices are named after the transport rather than the emulator:
# BizHawk and snes9x-rr both appear as luabridge://host:port. Map what people
# actually type.
#
# Servers name the same pak differently — "SD2SNES COM6" under QUsb2Snes,
# "fxpakpro://..." elsewhere — so an alias is tried *as well as* what was
# typed, never instead of it. Aliasing "sd2snes" to "fxpakpro" alone would
# fail to match a device literally called SD2SNES.
DEVICE_ALIASES = {
    "bizhawk": "luabridge", "snes9x": "luabridge", "emu": "luabridge",
    "emulator": "luabridge", "lua": "luabridge",
    "fxpak": "fxpakpro", "sd2snes": "fxpakpro", "pak": "fxpakpro",
}


def device_matches(filter_text, name):
    """True if a device name matches what the user typed, or its alias."""
    wanted = (filter_text or "").strip().lower()
    if not wanted:
        return True
    name = name.lower()
    alias = DEVICE_ALIASES.get(wanted)
    return wanted in name or (alias is not None and alias in name)


class SnesError(RuntimeError):
    pass


class Usb2Snes(object):
    """Minimal usb2snes client: attach to a device and read memory."""

    # Generous, because a stall shorter than this costs nothing while a timeout
    # costs a reconnect. The server serialises requests, so a busy moment — an
    # emulator save state, another client attached — can outlast a tight
    # timeout. Nothing waits on it: stop() closes the socket to interrupt a
    # read rather than waiting for it to expire.
    def __init__(self, url=DEFAULT_URL, device_filter="", timeout=10.0):
        self.url = url or DEFAULT_URL
        self.device_filter = (device_filter or "").strip().lower()
        self.timeout = timeout
        self.ws = None
        self.device = None
        self.devices = []

    def connect(self):
        if websocket is None:
            raise SnesError("websocket-client is not installed")
        try:
            self.ws = websocket.create_connection(self.url, timeout=self.timeout)
        except Exception as exc:
            raise SnesError("could not reach QUsb2Snes at %s — is it running? (%s)"
                            % (self.url, exc))
        self._send("DeviceList")
        try:
            reply = self.ws.recv()
        except Exception as exc:
            # The socket opened but the very first request went unanswered.
            # That is a different fault from "nothing is listening" and from
            # "attached but not reading", and it used to surface as a bare
            # timeout that said none of this.
            raise SnesError(
                "connected to %s but it never sent a device list (%s). "
                "Something is listening on that port without answering as a "
                "usb2snes server — usually QUsb2Snes wedged after a client "
                "was interrupted. Close everything using the pak, restart "
                "QUsb2Snes, then start this app again." % (self.url, exc))
        try:
            self.devices = json.loads(reply).get("Results", [])
        except Exception:
            raise SnesError(
                "%s answered, but not with a device list. Check nothing else "
                "has taken that port — it should be QUsb2Snes." % self.url)
        if not self.devices:
            raise SnesError("No device found. Check the FXPak is plugged in "
                            "with a ROM running, or that your emulator's "
                            "connector script is active.")
        if self.device_filter:
            matches = [d for d in self.devices
                       if device_matches(self.device_filter, d)]
            if not matches:
                raise SnesError("no device matching %r. Found: %s (emulators "
                                "appear as luabridge://... rather than by name)"
                                % (self.device_filter, ", ".join(self.devices)))
            self.device = matches[0]
        else:
            self.device = self.devices[0]
        self._send("Attach", [self.device])
        self._send("Name", [APP_NAME])
        self._handshake()
        return self.device

    def _handshake(self, probe_timeout=4.0):
        """Prove the attach actually works by reading a byte.

        Attach and Name are not acknowledged, so without this the first failure
        appears much later as a bare socket timeout in the middle of polling,
        which says nothing about the cause. A device that answers the device
        list but never answers a read is the signature of another usb2snes
        client already holding it, so name that.
        """
        try:
            self.ws.settimeout(probe_timeout)
        except Exception:
            pass
        try:
            self.read(WRAM_BASE, 1)
        except SnesError:
            raise
        except Exception:
            raise SnesError(
                "attached to %s but it never answered a read. Check the URL is "
                "ws://localhost:23074 — the old 8080 port is deprecated and can "
                "answer without working. Otherwise close any other copy of "
                "this app." % self.device)
        finally:
            try:
                self.ws.settimeout(self.timeout)
            except Exception:
                pass

    def _send(self, opcode, operands=None):
        self.ws.send(json.dumps({
            "Opcode": opcode, "Space": "SNES", "Operands": operands or []}))

    def read(self, addr, size):
        """Read `size` bytes. Replies arrive in chunks of at most 1024 bytes."""
        self._send("GetAddress", [format(addr, "X"), format(size, "X")])
        buf = bytearray()
        while len(buf) < size:
            try:
                chunk = self.ws.recv()
            except Exception as exc:
                if "timed out" in str(exc).lower() or not str(exc):
                    raise SnesError(
                        "no reply reading $%06X — attached but not answering. "
                        "Usually the wrong port: use ws://localhost:23074, not "
                        "8080." % addr)
                raise
            if isinstance(chunk, str):
                raise SnesError("unexpected text reply: %s" % chunk[:120])
            buf.extend(chunk)
        return bytes(buf)

    def read_u8(self, addr):
        return self.read(addr, 1)[0]

    def read_map(self, addresses, span=0x100):
        """Read several addresses, grouping any that sit close together.

        Two reasons. Every round trip costs real time, and more over USB to a
        pak than to an emulator — fewer of them means less chance of a stall
        long enough to drop the connection. And addresses read together are a
        coherent sample, so the game mode and the death byte can't disagree
        about which instant they describe.
        """
        wanted = sorted(set(addresses))
        out = {}
        start = 0
        while start < len(wanted):
            end = start
            while end + 1 < len(wanted) and wanted[end + 1] - wanted[start] < span:
                end += 1
            low = wanted[start]
            blob = self.read(low, wanted[end] - low + 1)
            for addr in wanted[start:end + 1]:
                out[addr] = blob[addr - low]
            start = end + 1
        return out

    def read_rom_id(self, addr=0x007FC0):
        """Identity of the loaded ROM: internal title plus header checksum.

        The title alone is not enough — most hacks are patches over Super Mario
        World and never touch the header, so two different hacks usually both
        say "SUPER MARIO WORLD". The checksum at $7FDE differs whenever the ROM
        content does, and its complement at $7FDC validates that we are looking
        at a real header rather than mid-load garbage.
        """
        # Read errors deliberately propagate. A failed read can leave part of a
        # reply sitting in the socket, and the next read would then return the
        # previous read's bytes — silently wrong exit counts. Reconnecting is
        # the only reliable resync, so the caller gets to hear about it.
        raw = self.read(addr, 0x20)
        title = "".join(chr(b) for b in raw[:21] if 32 <= b < 127).strip()
        checksum = raw[0x1E] | (raw[0x1F] << 8)
        complement = raw[0x1C] | (raw[0x1D] << 8)
        if (checksum ^ complement) != 0xFFFF:
            return None
        return "%s [%04X]" % (title or "UNTITLED", checksum)

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None


def _hexset(text):
    return {int(part, 16) for part in (text or "").replace(",", " ").split()}


class SnesTracker(object):
    """Polls the console and keeps exits, deaths and ROM identity up to date.

    Runs its own thread. Everything it learns lands in `self.state`, which the
    orchestrator reads; it never writes to OBS itself.
    """

    def __init__(self, config, data_dir, log=None, on_change=None,
                 on_rom_change=None):
        self.config = config
        self.log = log or (lambda *_: None)
        self.on_change = on_change or (lambda: None)
        # Fired when a different hack is loaded. The RA readout needs this:
        # the console notices a hack change that RetroAchievements cannot.
        self.on_rom_change = on_rom_change or (lambda rom: None)
        self.cache_path = os.path.join(data_dir, "snes_counts.json")
        self.state = {
            "connected": False,
            "device": "",
            "status": "idle",
            "rom": None,
            "exits": None,
            "deaths": None,
            "mode": None,
            "armed": False,
            "level": None,
            "level_deaths": None,
        }
        self._run = False
        self._thread = None
        self._generation = 0
        self._snes = None          # the live connection, so stop() can close it
        self._lock = threading.Lock()
        # Arming survives a reconnect. Otherwise a connection that drops every
        # few seconds never reaches the arm dwell, and the exit count you
        # already have is never picked up at all.
        self._armed = False
        self._armed_rom = None
        # When play was first seen. Also survives a reconnect, so the arm dwell
        # accumulates across brief drops instead of restarting from zero —
        # otherwise a connection that blips every few seconds never arms at all.
        self._valid_since = None
        self._drops = 0
        self._last_drop = None
        # Set when a per-hack death setting changes, so the poll loop picks
        # it up without waiting for the hack to change.
        self._resettle = False
        self._death_high = False
        self._last_count = None
        # Counted per death and logged with it, so a miss can be told
        # apart from a stuck edge without another round of guessing.
        self._samples = 0
        self._highs = 0
        self._high_since = None
        # The level a death gets credited to. Latched on entry rather than read
        # when the death happens: a death that sends you back to the overworld
        # changes the level number before the watcher reports it, and crediting
        # it to wherever the game went next is how per-level counts drift.
        self._level = None
        self._retry_probe_due = False
        # Whether this hack's deaths have been seen to move the animation
        # trigger. Until one has, a sprite lock is taken at face value, so a
        # hack whose trigger never fires keeps working exactly as before.
        self._confirm_learned = False
        self._ignored = 0
        self._in_a_row = 0
        # Region reads for the address finder, serviced by the poll loop.
        # Deliberately not a connection of its own: a second client attached
        # to the same device is how this app once broke itself, and the pak
        # answers one at a time anyway.
        self._sample_req = None
        self._sample_out = None
        self._sample_evt = threading.Event()
        self.cache = self._load_cache()

    # -- per-hack counts --------------------------------------------------

    def _load_cache(self):
        try:
            with open(self.cache_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return {}

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as handle:
                json.dump(self.cache, handle, indent=1)
        except Exception:
            pass

    def _entry(self, rom):
        entry = self.cache.get(rom)
        if not isinstance(entry, dict):
            entry = {}
        self.cache[rom] = entry
        return entry

    # -- per-hack death detection ----------------------------------------

    def death_settings(self, rom=None):
        """How to detect deaths for this hack.

        Hacks disagree about this more than about anything else: vanilla-based
        ones use the sprite-lock byte, while a hack with its own retry patch
        may keep a death total somewhere in $7F entirely. A setting that is
        right for one is wrong for the next, so whatever was found for a hack
        is remembered against that hack and the global values are only the
        fallback.
        """
        cfg = self.config
        addr_text, mode_text, value_text = (cfg["death_addr"], cfg["death_mode"],
                                            cfg["death_value"])
        entry = self.cache.get(rom) if rom else None
        if isinstance(entry, dict) and entry.get("death_addr"):
            addr_text = entry.get("death_addr") or addr_text
            mode_text = entry.get("death_mode") or mode_text
            value_text = entry.get("death_value") or value_text
        addr = int(addr_text, 16) if (addr_text or "").strip() else None
        value = int(value_text, 16) if (value_text or "").strip() else None
        return addr, (mode_text or "state").strip().lower(), value

    def set_hack_deaths(self, rom, addr, mode, value=None):
        """Bind a death address to one hack, leaving every other hack alone."""
        if not rom:
            return False
        entry = self._entry(rom)
        entry["death_addr"] = addr
        entry["death_mode"] = mode
        if value is not None:
            entry["death_value"] = value
        self._save_cache()
        self._resettle = True
        self.log("deaths for this hack: %s (%s)" % (addr, mode))
        self.on_change()
        return True

    def clear_hack_deaths(self, rom):
        """Back to the default settings for this hack."""
        entry = self.cache.get(rom)
        if not isinstance(entry, dict):
            return False
        for key in ("death_addr", "death_mode", "death_value"):
            entry.pop(key, None)
        self._save_cache()
        self._resettle = True
        self.log("deaths for this hack: back to the default settings")
        self.on_change()
        return True

    def adjust_deaths(self, delta):
        with self._lock:
            self.state["deaths"] = max(0, (self.state["deaths"] or 0) + delta)
            value, rom = self.state["deaths"], self.state["rom"]
        if rom:
            self._entry(rom)["deaths"] = value
            self._save_cache()
        self.on_change()
        return value

    def reset_deaths(self):
        with self._lock:
            self.state["deaths"] = 0
            rom = self.state["rom"]
        if rom:
            self._entry(rom)["deaths"] = 0
            self._save_cache()
        self.on_change()

    # -- per-level counts -------------------------------------------------

    def _levels(self, rom):
        """The per-level death counts for one hack.

        Kept beside the hack total rather than derived from it. The total is
        the number people have been watching all along and can nudge by hand,
        and per-level counts only start existing once this feature does — so
        summing them would show nothing for a hack you have already played.
        """
        entry = self._entry(rom)
        table = entry.get("levels")
        if not isinstance(table, dict):
            table = {}
            entry["levels"] = table
        return table

    def level_deaths(self, rom=None, level=None):
        """Deaths on one level, or on the level being played."""
        with self._lock:
            rom = rom or self.state["rom"]
            level = level if level is not None else self.state["level"]
        if not rom or level is None:
            return None
        return self._levels(rom).get(level, 0)

    def level_table(self, rom=None):
        """Every level this hack has deaths against, worst first."""
        with self._lock:
            rom = rom or self.state["rom"]
        if not rom:
            return []
        table = self._levels(rom)
        rows = [{"level": key, "deaths": value} for key, value in table.items()]
        rows.sort(key=lambda row: (-row["deaths"], row["level"]))
        return rows

    def adjust_level_deaths(self, delta):
        with self._lock:
            rom, level = self.state["rom"], self.state["level"]
        if not rom or level is None:
            return None
        table = self._levels(rom)
        table[level] = max(0, table.get(level, 0) + delta)
        self._save_cache()
        self._set(level_deaths=table[level])
        self.on_change()
        return table[level]

    def reset_level_deaths(self):
        """Zero the current level only, leaving the hack total alone."""
        with self._lock:
            rom, level = self.state["rom"], self.state["level"]
        if not rom or level is None:
            return False
        self._levels(rom)[level] = 0
        self._save_cache()
        self._set(level_deaths=0)
        self.on_change()
        return True

    def _enter_level(self, raw):
        """Latch the level now being played and show its count."""
        level = "%02X" % raw
        self._level = level
        with self._lock:
            rom = self.state["rom"]
        count = self._levels(rom).get(level, 0) if rom else None
        self._set(level=level, level_deaths=count)

    # -- lifecycle --------------------------------------------------------

    def start(self):
        self.stop()
        with self._lock:
            self._generation += 1
            generation = self._generation
        self._run = True
        self._thread = threading.Thread(target=self._loop, args=(generation,),
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._run = False
        # Closing the socket unblocks a thread parked in recv(). Without this
        # the join below times out while the old thread is still reading, and
        # two clients end up attached to one device, corrupting each other's
        # replies.
        snes = self._snes
        if snes is not None:
            snes.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        with self._lock:
            self.state["connected"] = False

    def _active(self, generation):
        """False once this thread has been superseded.

        `_run` alone is not enough: start() sets it back to True, so a thread
        that outlived its join would otherwise see the flag flip back and
        carry on reading forever alongside its replacement.
        """
        return self._run and generation == self._generation

    def sample_region(self, base, size, timeout=15.0):
        """Read a block of memory through the live connection.

        Blocks until the poll loop gets to it, which is at most one poll
        interval away. Raises rather than returning junk, so a caller never
        mistakes a failed read for a region of zeroes.
        """
        with self._lock:
            connected = self.state["connected"]
        if not connected:
            raise RuntimeError("the console is not connected")
        with self._lock:
            self._sample_out = None
            self._sample_req = (base, size)
        self._sample_evt.clear()
        if not self._sample_evt.wait(timeout):
            raise RuntimeError("timed out waiting for the console")
        with self._lock:
            data, error = self._sample_out or (None, "no reply")
        if error:
            raise RuntimeError(error)
        return data

    def _serve_sample(self, snes):
        """Hand the finder its block, if one was asked for."""
        with self._lock:
            request = self._sample_req
            self._sample_req = None
        if not request:
            return
        try:
            data = snes.read(request[0], request[1])
        except Exception as exc:
            with self._lock:
                self._sample_out = (None, str(exc))
            # Set the event before re-raising: a failed read drops the
            # connection, and the caller must not sit waiting for a reply
            # that is never coming.
            self._sample_evt.set()
            raise
        with self._lock:
            self._sample_out = (data, None)
        self._sample_evt.set()

    def override_summary(self):
        """What this hack uses, if it isn't the defaults — for the UI."""
        with self._lock:
            rom = self.state["rom"]
        entry = self.cache.get(rom) if rom else None
        if not (isinstance(entry, dict) and entry.get("death_addr")):
            return None
        return {"addr": entry["death_addr"],
                "mode": entry.get("death_mode") or "state"}

    def _set(self, **kwargs):
        with self._lock:
            changed = any(self.state.get(k) != v for k, v in kwargs.items())
            self.state.update(kwargs)
        if changed:
            self.on_change()
        return changed

    # -- the loop ---------------------------------------------------------

    def _loop(self, generation):
        backoff = 1.0
        while self._active(generation):
            cfg = self.config
            snes = Usb2Snes(cfg["snes_url"], cfg["snes_device"])
            self._snes = snes
            try:
                device = snes.connect()
                backoff = 1.0
                if self._drops:
                    self.log("console reconnected after %d drop%s: %s"
                             % (self._drops, "" if self._drops == 1 else "s",
                                device))
                    self._drops, self._last_drop = 0, None
                self._set(connected=True, device=device, status="connected")
                self._read_forever(snes, generation)
            except Exception as exc:
                if not self._active(generation):
                    break
                self._report_drop(exc)
                self._set(connected=False, status="reconnecting: %s" % exc)
                self._sleep(backoff, generation)
                backoff = min(backoff * 2, 15.0)
            finally:
                snes.close()
                if self._snes is snes:
                    self._snes = None

    def _report_drop(self, exc):
        """Say why the connection went, without flooding the log if it repeats.

        The reason is the whole diagnosis when this goes wrong, and it used to
        only ever appear in a status pill that changes too fast to read.
        """
        text = str(exc)
        self._drops += 1
        if text != self._last_drop:
            self._last_drop = text
            self.log("console disconnected: %s" % text)
        elif self._drops % 10 == 0:
            self.log("console still dropping (%d times): %s" % (self._drops, text))

    def _sleep(self, seconds, generation):
        end = time.time() + seconds
        while self._active(generation) and time.time() < end:
            time.sleep(0.1)

    def _read_forever(self, snes, generation):
        cfg = self.config
        exit_addr = int(cfg["exit_addr"], 16)
        gate_addr = int(cfg["gate_addr"], 16)
        gate_min, gate_max = int(cfg["gate_min"], 16), int(cfg["gate_max"], 16)
        arm_modes = _hexset(cfg["arm_modes"])
        with self._lock:
            rom_now = self.state["rom"]
        death_addr, death_mode, death_value = self.death_settings(rom_now)
        rom_addr = int(cfg["rom_addr"], 16) if cfg["rom_addr"].strip() else None
        level_addr = (int(cfg["level_addr"], 16)
                      if (cfg.get("level_addr") or "").strip() else None)
        interval = max(cfg["poll_ms"], 50) / 1000.0
        dwell = cfg["arm_after_s"]
        disarm_below = min([gate_min] + sorted(arm_modes)) if arm_modes else gate_min

        if not arm_modes:
            self._armed = True
        def wait(snes, addr, mode, value, generation):
            """Wait out the poll interval, watching the death byte while we do."""
            if addr is None:
                time.sleep(interval)
            else:
                self._watch_deaths(snes, addr, mode, value, interval, generation)

        armed = self._armed
        # None until the first read, so a reconnect resyncs rather than
        # crediting everything that happened while we were away.
        self._last_count = None
        self._death_high = False
        pending, pending_hits = None, 0
        next_rom_check = 0.0

        # One request per group of nearby addresses instead of one per address.
        def address_list(with_exits):
            # The death byte is deliberately absent: it is watched between
            # these cycles, far more often than everything else. The exit
            # counter is read only now and then, because every extra read
            # here is time the death byte is NOT being watched — and on a
            # hack whose death state lasts 25ms, that gap loses deaths.
            return [gate_addr, exit_addr] if with_exits else [gate_addr]
        cycle = 0
        prev_mode = None
        level_pending = False

        while self._active(generation):
            now = time.time()
            self._serve_sample(snes)

            if self._resettle:
                self._resettle = False
                with self._lock:
                    rom_now = self.state["rom"]
                death_addr, death_mode, death_value = self.death_settings(rom_now)
                cycle = 0
                self._last_count = None
                self._death_high = False

            # Which hack is loaded? Identity is title + checksum, because the
            # title alone collides across almost every SMW hack.
            if rom_addr is not None and now >= next_rom_check:
                next_rom_check = now + 2.0
                rom_id = snes.read_rom_id(rom_addr)
                with self._lock:
                    known = self.state["rom"]
                if rom_id and rom_id != known:
                    armed = self._armed = False
                    self._valid_since = None
                    self._last_count = None
                    self._death_high = False
                    self._armed_rom = rom_id
                    # This hack may detect deaths differently from the last.
                    death_addr, death_mode, death_value = self.death_settings(rom_id)
                    cycle = 0
                    # A level number means nothing across hacks, so the latch
                    # is dropped and re-read rather than carried over.
                    self._level = None
                    prev_mode = None
                    self._retry_probe_due = True
                    entry = self._entry(rom_id)
                    # What was learned about this hack last time, so the Yoshi
                    # filter is armed from the first death rather than after it.
                    self._confirm_learned = bool(entry.get("death_confirm"))
                    self._ignored = self._in_a_row = 0
                    self._set(rom=rom_id,
                              exits=entry.get("exits"),
                              deaths=entry.get("deaths", 0),
                              level=None, level_deaths=None)
                    self.log("hack: %s (deaths %s)" % (rom_id, entry.get("deaths", 0)))
                    try:
                        self.on_rom_change(rom_id)
                    except Exception as exc:
                        self.log("rom-change handler failed: %s" % exc)

            # Exits only every fifth pass, so four cycles out of five cost a
            # single read and the death byte is watched almost continuously.
            cycle += 1
            check_exits = (cycle % 5 == 1)
            sample = snes.read_map(address_list(check_exits))
            mode = sample[gate_addr]

            if mode == FILLER:
                self._set(status="memory not readable yet (ROM loading?)", mode=None)
                time.sleep(interval)
                continue

            # Reset or menu. The floor sits below the lowest arm mode so that
            # arming on a mode too early to read from still works.
            if mode < disarm_below or mode > gate_max:
                armed = self._armed = False
                self._valid_since = None
                # Whatever level was being played is over, and the mode will
                # cross back in from below, so the next entry re-reads it.
                prev_mode = None
                self._set(status="no file loaded", mode=mode, armed=False)
                time.sleep(interval)
                continue

            # Arm before the read gate: the title screen demo plays a real
            # level, so arming is what keeps its values out.
            if mode in arm_modes:
                armed = self._armed = True
                self._valid_since = None
            elif not armed:
                # Fallback for hub-world hacks that never show an overworld,
                # and for starting the app mid-session.
                if self._valid_since is None:
                    self._valid_since = now
                elif dwell and now - self._valid_since >= dwell:
                    armed = self._armed = True
                    self._valid_since = None
                    self.log("armed after %ds of play" % dwell)

            if mode < gate_min:
                self._set(status="waiting for game to start", mode=mode, armed=armed)
                time.sleep(interval)
                continue
            if arm_modes and not armed:
                self._set(status="waiting to arm", mode=mode, armed=False)
                time.sleep(interval)
                continue

            self._set(status="reading", mode=mode, armed=True)

            # The level number can only change on a level load, and a level
            # load is exactly the game mode crossing into the in-level range —
            # so that crossing is the only time it costs a read. The slow cycle
            # picks it up too, which covers starting the app mid-level.
            # Both tests require actually being in a level. On the overworld the
            # translevel byte still holds the last level entered, so reading it
            # there would have the dashboard name a level you are not playing —
            # and credit the next death to it if one somehow arrived.
            if level_addr is not None and mode >= IN_LEVEL_MODE:
                if prev_mode is None or prev_mode < IN_LEVEL_MODE \
                        or (check_exits and self._level is None):
                    level_pending = True
                # Wait for settled play before reading, so the level number is
                # the one being loaded rather than whatever was there mid-load.
                if level_pending and mode >= SETTLED_MODE:
                    level_pending = False
                    self._enter_level(snes.read_u8(level_addr))
            prev_mode = mode

            if self._retry_probe_due:
                self._retry_probe_due = False
                self._probe_retry(snes)

            if not check_exits:
                wait(snes, death_addr, death_mode, death_value, generation)
                continue

            value = sample[exit_addr]
            with self._lock:
                last = self.state["exits"]

            # Only when the number actually moved is it worth a second read to
            # confirm we are still in the game — which is rarely.
            if value != last:
                recheck = snes.read_u8(gate_addr)
                if not (gate_min <= recheck <= gate_max):
                    wait(snes, death_addr, death_mode, death_value, generation)
                    continue

            if last is not None and value < last:
                # A console reset zeroes WRAM before the save file loads, so a
                # drop has to persist before it is believed.
                if value == pending:
                    pending_hits += 1
                else:
                    pending, pending_hits = value, 1
                if pending_hits >= 3:
                    self._store_exits(value)
                    pending, pending_hits = None, 0
            else:
                pending, pending_hits = None, 0
                if last != value:
                    self._store_exits(value)

            wait(snes, death_addr, death_mode, death_value, generation)

    def _note_counter(self, raw):
        """One sample of a total the hack keeps itself."""
        self._samples += 1
        # Such a total cannot be missed by polling at all: whatever happened
        # between two reads is still in the number.
        if self._last_count is not None:
            # Forward distance, so a counter rolling past 255 is still a small
            # step. A reset lands far away and is rejected by the cap rather
            # than adding hundreds.
            step = (raw - self._last_count) & 0xFF
            if 0 < step <= 32:
                self._add_deaths(step)
        self._last_count = raw

    def _note_held(self, raw):
        """The lock byte has been set a long time. Usually that is a prompt."""
        if self._high_since and time.time() - self._high_since > HELD_WARN_S:
            self._high_since = None
            self.log("death byte has read 0x%02X for %d seconds. That is "
                     "normal while a retry prompt or a pause is waiting on "
                     "you — counting carries on as soon as it clears. It only "
                     "means the wrong value if it never clears while you are "
                     "playing." % (raw, HELD_WARN_S))

    def _confirm_death(self, snes, addr, value, generation):
        """A sprite lock just appeared. Is the player actually dying?

        Watches the player's animation trigger for a moment, while carrying on
        watching the lock byte so its fall is not missed. Returns whether the
        dying animation was seen, and whether the lock has already cleared.

        Two reads per sample instead of one, but only while resolving a lock
        that just appeared — a few times an hour, not fifty times a second.
        """
        seen, cleared = False, False
        end = time.time() + CONFIRM_MS / 1000.0
        while self._active(generation) and time.time() < end:
            if snes.read_u8(CONFIRM_ADDR) == CONFIRM_VALUE:
                seen = True
                break
            # The lock clearing does not end the window. On a hack that clears
            # it in two frames the animation can still be a moment behind, and
            # giving up here would throw away a real death on exactly the
            # quick-retry hacks this whole watcher exists for.
            if not cleared:
                self._samples += 1
                if snes.read_u8(addr) == value:
                    self._highs += 1
                else:
                    cleared = True
            time.sleep(DEATH_WATCH_MS / 1000.0)
        return seen, cleared

    def _note_state(self, snes, raw, addr, value, generation):
        """One sample of a byte that means the player is dying."""
        self._samples += 1
        dying = raw == value
        if not dying:
            self._death_high = False
            return
        self._highs += 1
        if self._death_high:
            self._note_held(raw)
            return

        # A lock has just appeared. $9D is set by anything that freezes the
        # scene, so this may be a death or may be a Yoshi hatching out of a
        # block, which looks identical in that byte alone.
        self._death_high = True
        self._high_since = time.time()
        seen, cleared = self._confirm_death(snes, addr, value, generation)
        if cleared:
            self._death_high = False
        if seen:
            if not self._confirm_learned:
                # This hack's deaths do move the animation trigger, so from
                # here on a lock that doesn't is something else. Remembered
                # against the hack, so it is known from the start next time.
                self._learn_confirm()
            self._in_a_row = 0
            self._add_deaths(1)
        elif self._confirm_learned:
            self._ignored += 1
            self._in_a_row += 1
            if self._in_a_row >= GIVE_UP_AFTER:
                # Every lock in a row rejected and no death in between. More
                # likely the trigger is being missed on this hack than that
                # the player hit that many Yoshi blocks without dying, and
                # losing deaths silently is far worse than counting a few
                # extra. Back to taking locks at face value.
                self._forget_confirm()
                self._add_deaths(1)
                return
            self.log("ignored a sprite lock that wasn't a death (%d so far on "
                     "this hack). Yoshi hatching out of a block does this."
                     % self._ignored)
        else:
            # Nothing has yet shown that this hack's deaths move the animation
            # trigger, so there is nothing to disbelieve this lock with.
            # Counting it is what this did before, and a hack where the
            # trigger never fires has to keep working.
            self._add_deaths(1)

    def _forget_confirm(self):
        """Stop trusting the animation trigger on this hack."""
        self._confirm_learned = False
        self._in_a_row = 0
        with self._lock:
            rom = self.state["rom"]
        if rom:
            self._entry(rom).pop("death_confirm", None)
            self._save_cache()
        self.log("%d sprite locks in a row with no dying animation, so the "
                 "animation check is being dropped for this hack — counting "
                 "every lock again, the way it worked before." % GIVE_UP_AFTER)

    def _learn_confirm(self):
        self._confirm_learned = True
        with self._lock:
            rom = self.state["rom"]
        if rom:
            self._entry(rom)["death_confirm"] = True
            self._save_cache()
        self.log("deaths on this hack move the animation trigger, so a sprite "
                 "lock on its own will no longer be counted as one")

    def _watch_deaths(self, snes, addr, mode, value, seconds, generation):
        """Sample the death byte repeatedly instead of idling between cycles.

        This is where the waiting between polls happens now, so watching
        closely costs no extra time — only the one extra byte per sample.
        """
        end = time.time() + seconds
        while self._active(generation):
            raw = snes.read_u8(addr)
            if mode == "counter":
                self._note_counter(raw)
            elif value is not None:
                self._note_state(snes, raw, addr, value, generation)
            remaining = end - time.time()
            if remaining <= 0:
                return
            time.sleep(min(DEATH_WATCH_MS / 1000.0, remaining))

    def _add_deaths(self, count):
        level = self._level
        with self._lock:
            self.state["deaths"] = (self.state["deaths"] or 0) + count
            total, rom = self.state["deaths"], self.state["rom"]
        here = None
        if rom:
            entry = self._entry(rom)
            entry["deaths"] = total
            if level is not None:
                table = self._levels(rom)
                here = table[level] = table.get(level, 0) + count
            self._save_cache()
        if here is None:
            self.log("deaths = %d  (%d of %d samples saw it)"
                     % (total, self._highs, self._samples))
        else:
            self._set(level_deaths=here)
            self.log("deaths = %d, level %s = %d  (%d of %d samples saw it)"
                     % (total, level, here, self._highs, self._samples))
        self._highs = self._samples = 0
        self.on_change()

    def _probe_retry(self, snes):
        """Log the Retry System's own death total if this hack keeps one.

        Kevin's Retry System is what most modern kaizo hacks use for the
        prompt, and it keeps a death total of its own at a documented address —
        five bytes, one decimal digit each. A total cannot be missed the way a
        death animation lasting 23ms can, so if it is there it is strictly
        better than anything the finder turns up by watching for an edge.

        This only reports it. A hack can relocate the patch's free RAM, and
        reading digits when the base has moved would count nonsense, so
        switching to it is a decision for after it has been seen to be right.
        """
        try:
            addrs = [RETRY_DEATHS + i for i in range(RETRY_DEATH_DIGITS)]
            digits = snes.read_map(addrs)
        except Exception:
            return
        values = [digits[addr] for addr in addrs]
        if any(value > 9 for value in values):
            return
        self.log("Retry System death counter found at %06X: %d deaths on this "
                 "save. Exact, and cannot be missed by polling — tell the "
                 "developer if your deaths disagree with it."
                 % (RETRY_DEATHS, int("".join(str(v) for v in values))))

    def _store_exits(self, value):
        with self._lock:
            self.state["exits"] = value
            rom = self.state["rom"]
        if rom:
            self._entry(rom)["exits"] = value
            self._save_cache()
        self.on_change()
