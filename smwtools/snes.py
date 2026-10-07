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
        self.devices = json.loads(self.ws.recv()).get("Results", [])
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
        death_addr = int(cfg["death_addr"], 16) if cfg["death_addr"].strip() else None
        death_value = int(cfg["death_value"], 16) if cfg["death_value"].strip() else None
        death_mode = (cfg["death_mode"] or "state").strip().lower()
        rom_addr = int(cfg["rom_addr"], 16) if cfg["rom_addr"].strip() else None
        interval = max(cfg["poll_ms"], 50) / 1000.0
        dwell = cfg["arm_after_s"]
        disarm_below = min([gate_min] + sorted(arm_modes)) if arm_modes else gate_min

        if not arm_modes:
            self._armed = True
        armed = self._armed
        death_high = False
        # None until the first read, so a reconnect resyncs rather than
        # crediting everything that happened while we were away.
        last_count = None
        pending, pending_hits = None, 0
        next_rom_check = 0.0

        # One request per group of nearby addresses instead of one per address.
        wanted = [gate_addr, exit_addr] + ([death_addr] if death_addr is not None else [])

        while self._active(generation):
            now = time.time()
            self._serve_sample(snes)

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
                    last_count = None
                    self._armed_rom = rom_id
                    entry = self._entry(rom_id)
                    self._set(rom=rom_id,
                              exits=entry.get("exits"),
                              deaths=entry.get("deaths", 0))
                    self.log("hack: %s (deaths %s)" % (rom_id, entry.get("deaths", 0)))
                    try:
                        self.on_rom_change(rom_id)
                    except Exception as exc:
                        self.log("rom-change handler failed: %s" % exc)

            sample = snes.read_map(wanted)
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

            # Deaths. From the same sample as the mode above, so a death can
            # never be credited against a stale mode.
            if death_addr is not None:
                raw = sample[death_addr]
                if death_mode == "counter":
                    # A total the hack keeps itself cannot be missed by
                    # polling the way a brief state can: whatever happened
                    # between two reads is still in the number. That matters
                    # on a retry patch whose death state is gone in a frame or
                    # two, where watching for the state catches only some of
                    # them.
                    if last_count is not None:
                        # Forward distance, so a counter rolling over 255 is
                        # still a small step. A reset lands far away instead
                        # and is rejected by the cap below rather than
                        # subtracting or adding hundreds.
                        step = (raw - last_count) & 0xFF
                        if 0 < step <= 32:
                            self._add_deaths(step)
                    last_count = raw
                elif death_value is not None:
                    dying = raw == death_value
                    if dying and not death_high:
                        self._add_deaths(1)
                    death_high = dying

            value = sample[exit_addr]

            # The counter comes from a different request than this check, so
            # confirm we are still in the game before trusting the pair.
            recheck = snes.read_u8(gate_addr)
            if not (gate_min <= recheck <= gate_max):
                time.sleep(interval)
                continue

            with self._lock:
                last = self.state["exits"]

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

            time.sleep(interval)

    def _add_deaths(self, count):
        with self._lock:
            self.state["deaths"] = (self.state["deaths"] or 0) + count
            total, rom = self.state["deaths"], self.state["rom"]
        if rom:
            self._entry(rom)["deaths"] = total
            self._save_cache()
        self.log("deaths = %d" % total)
        self.on_change()

    def _store_exits(self, value):
        with self._lock:
            self.state["exits"] = value
            rom = self.state["rom"]
        if rom:
            self._entry(rom)["exits"] = value
            self._save_cache()
        self.on_change()
