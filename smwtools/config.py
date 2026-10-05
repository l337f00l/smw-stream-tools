"""Settings, stored as JSON next to the app.

One file for everything, so a user backs up or shares a setup by copying it.
Secrets live here in plain text — same as OBS scene collections do — so the
web UI never echoes them back once saved.
"""

import json
import os
import sys
import threading

def _app_dir():
    """Where settings and saved counts live.

    In a PyInstaller one-file build the module path points inside a temporary
    extraction directory that is deleted on exit, so anything written there is
    lost between runs. Next to the executable is the durable place.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


APP_DIR = _app_dir()
DATA_DIR = os.path.join(APP_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

SECRET_KEYS = {"twitch_client_secret", "obs_password", "ra_api_key"}

DEFAULTS = {
    # OBS
    "obs_url": "ws://127.0.0.1:4455",
    "obs_password": "",

    # Manual hack override. Exits and deaths always come from the console;
    # only the name and the total have to be told to us, and the index cannot
    # know about every hack — nor does everyone want to register a Twitch app.
    "manual_on": False,
    "manual_name": "",
    "manual_exits": 0,
    "manual_author": "",

    # Twitch / kaizoff
    "twitch_channel": "",
    "twitch_client_id": "",
    "twitch_client_secret": "",
    "title_poll_seconds": 60,
    "hack_cache_hours": 12,
    "allow_insecure_hacks": False,
    "auto_shorten": True,

    # SNES
    "snes_url": "ws://localhost:23074",   # QUsb2Snes; 8080 is the dead legacy port
    "snes_device": "",
    "exit_addr": "F51F2E",
    "gate_addr": "F50100",
    "gate_min": "0B",
    "gate_max": "1F",
    "arm_modes": "0A,0E",
    "arm_after_s": 10,
    "death_addr": "F5009D",
    "death_value": "30",
    "poll_ms": 200,
    "rom_addr": "007FC0",

    # OBS sources
    "name_source": "",
    "exits_source": "",
    "deaths_source": "",
    "author_source": "",
    "exits_format": "Exits {done}/{total}",
    "deaths_format": "Deaths: {deaths}",
    "author_format": "BY: {author}",

    # Name fitting / layout
    "max_width": 420,
    "base_font": 42,
    "min_font": 22,
    "layout_mode": "below",
    "gap": 18,

    # RetroAchievements
    "ra_user": "",
    "ra_api_key": "",
    "ra_game_id": "",
    "ra_hardcore": False,
    "ra_source": "",
    "ra_format": "Achievements {earned}/{total}",
    "ra_progress_poll_s": 60,
    "ra_alerts_on": False,
    "ra_alert_poll_s": 10,
    "ra_alert_seconds": 6,
    "ra_alert_position": "br",

    # Web UI
    "web_port": 4599,

    # Browser overlay (/counters) — an alternative to OBS text sources, and
    # the only route for software with no text API of its own, such as Meld.
    "ov_font": "Inter, Segoe UI, sans-serif",
    "ov_size": 40,
    "ov_min_size": 22,
    # Per-line size overrides. 0 means "use Font size", so nobody has to
    # fill in five boxes to change one line.
    "ov_size_name": 0,
    "ov_size_exits": 0,
    "ov_size_deaths": 0,
    "ov_size_author": 0,
    "ov_size_ra": 0,
    "ov_color": "#ffffff",
    "ov_outline": "#000000",
    "ov_outline_px": 3,
    # Transparent suits OBS, which composites it. Anything that does not
    # can put a solid plate behind the text instead.
    "ov_bg": "transparent",
    "ov_pad": 0,
    "ov_align": "left",           # left | center | right
    "ov_layout": "stack",         # stack | inline
    "ov_gap": 6,
    "ov_width": 520,
    "ov_show_name": True,
    "ov_show_exits": True,
    "ov_show_deaths": True,
    "ov_show_author": False,
    "ov_show_ra": True,

    # Feature switches
    # OBS is off for anyone driving the browser overlay instead — Meld
    # users have no obs-websocket to connect to, and a connection that
    # can never succeed should not show as a fault.
    "enable_obs": True,
    "enable_twitch": True,
    "enable_snes": True,
    "enable_ra": False,
}


class Config(object):
    SECRETS = SECRET_KEYS

    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.values = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
        except Exception:
            return
        with self._lock:
            for key, value in stored.items():
                # Ignore keys we no longer use rather than carrying them along.
                if key in DEFAULTS:
                    self.values[key] = value

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._lock:
            snapshot = dict(self.values)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.path)     # never leave a half-written config

    def __getitem__(self, key):
        with self._lock:
            return self.values.get(key, DEFAULTS.get(key))

    def get(self, key, default=None):
        with self._lock:
            return self.values.get(key, default)

    def update(self, changes):
        """Apply a partial update, coercing to each default's type."""
        with self._lock:
            for key, value in changes.items():
                if key not in DEFAULTS:
                    continue
                default = DEFAULTS[key]
                try:
                    if isinstance(default, bool):
                        value = bool(value)
                    elif isinstance(default, int):
                        value = int(value)
                    else:
                        value = "" if value is None else str(value)
                except (TypeError, ValueError):
                    continue
                self.values[key] = value
        self.save()

    def snapshot(self):
        """Every value as stored, for working out what a save actually changed."""
        with self._lock:
            return dict(self.values)

    def public(self):
        """Config for the UI: secrets replaced by whether one is set.

        The UI needs to show that a password exists without handing it back to
        anything that can read the page.
        """
        with self._lock:
            out = dict(self.values)
        for key in SECRET_KEYS:
            out[key] = ""
            out[key + "_set"] = bool(self.values.get(key))
        return out
