"""System tray icon, so the app can sit in the background while you stream.

The icon carries the status: green when everything it has been asked to do is
working, amber when something is still connecting, red when OBS is unreachable.
Hovering gives the detail. That way a glance at the tray answers "is my overlay
going to update?" without opening anything.

pystray is optional. It raises on import where there is no display — a headless
box, a remote session — so every use of it here is guarded and the app falls
back to running in the console.
"""

import threading
import webbrowser

# Colours match the dots in the web UI.
OK = (74, 222, 128)
WARN = (251, 191, 36)
BAD = (248, 113, 113)
IDLE = (107, 116, 136)
PLATE = (22, 25, 32)
EDGE = (40, 45, 56)


_LOAD_ERROR = []


def _load():
    """Import pystray and Pillow, or return None if unavailable."""
    try:
        import pystray
        from PIL import Image, ImageDraw
        return pystray, Image, ImageDraw
    except Exception as exc:
        _LOAD_ERROR[:] = [exc]
        return None


def unavailable_reason():
    """Why there is no tray icon, in words a user can act on.

    This matters more than it looks: a windowed build has no console, so
    without this the app would just run invisibly with no icon and no
    explanation.
    """
    if not _LOAD_ERROR:
        return "tray not available on this system"
    exc = _LOAD_ERROR[0]
    text = str(exc)
    if isinstance(exc, ImportError):
        return ("no tray icon: pystray and Pillow are not installed — "
                "run: pip install pystray Pillow")
    if "display" in text.lower() or "DISPLAY" in text:
        return "no tray icon: no desktop session available (%s)" % text
    return "no tray icon: %s" % text


def make_icon(colour, size=64):
    """A rounded dark plate with a status dot, drawn rather than shipped.

    Generating it avoids an image asset that could go missing from a build, and
    lets the colour change with status.
    """
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad, radius = size // 10, size // 5
    draw.rounded_rectangle([pad, pad, size - pad, size - pad],
                           radius=radius, fill=PLATE, outline=EDGE,
                           width=max(1, size // 32))
    # An offset dot, so it reads as a status light rather than a bullseye.
    r = size // 5
    cx, cy = size // 2, size // 2
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    return image


def status_of(snapshot, config):
    """Reduce the whole app to one colour and one line of text.

    Only things that are switched on count: nobody should see a warning
    because they left RetroAchievements off.
    """
    parts = []
    worst = OK

    if config["enable_obs"]:
        obs = snapshot.get("obs") or {}
        if obs.get("connected"):
            parts.append("OBS connected")
        else:
            parts.append("OBS: %s" % (obs.get("status") or "not connected"))
            worst = BAD

    if config["enable_snes"]:
        snes = snapshot.get("snes") or {}
        if snes.get("connected"):
            parts.append("console: %s" % (snes.get("status") or "connected"))
            if not snes.get("armed") and worst is OK:
                worst = WARN
        else:
            parts.append("console: not connected")
            if worst is OK:
                worst = WARN

    if config["enable_twitch"]:
        twitch = (snapshot.get("twitch") or {}).get("status") or "idle"
        parts.append("Twitch: %s" % twitch)
        if twitch not in ("ok", "off") and worst is OK:
            worst = WARN

    if config["enable_ra"]:
        ra = (snapshot.get("ra") or {}).get("status") or "off"
        parts.append("RA: %s" % ra)
        if ra not in ("ok", "off") and worst is OK:
            worst = WARN

    hack = snapshot.get("hack")
    if hack:
        parts.insert(0, hack.get("display") or hack.get("name"))

    return worst, "SMW Stream Tools\n" + "\n".join(parts)


class Tray(object):
    """Runs pystray on the main thread; everything else is already threaded."""

    def __init__(self, app, config, url, on_quit):
        self.app = app
        self.config = config
        self.url = url
        self.on_quit = on_quit
        self.icon = None
        self._run = False
        self._colour = None

    def available(self):
        return _load() is not None

    def _open(self, *_):
        try:
            webbrowser.open(self.url)
        except Exception:
            pass

    def _quit(self, *_):
        self._run = False
        if self.icon:
            self.icon.stop()
        self.on_quit()

    def _watch(self):
        import time
        while self._run:
            try:
                colour, tooltip = status_of(self.app.snapshot(), self.config)
                if self.icon is not None:
                    if colour != self._colour:
                        self._colour = colour
                        self.icon.icon = make_icon(colour)
                    self.icon.title = tooltip
            except Exception:
                pass
            time.sleep(2.0)

    def run(self):
        """Blocks until quit. Returns False if a tray isn't possible here."""
        loaded = _load()
        if loaded is None:
            return False
        pystray, _Image, _Draw = loaded

        menu = pystray.Menu(
            pystray.MenuItem("Open settings page", self._open, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )
        self._colour = IDLE
        self.icon = pystray.Icon("smw-stream-tools", make_icon(IDLE),
                                 "SMW Stream Tools", menu)
        self._run = True
        threading.Thread(target=self._watch, daemon=True).start()
        try:
            self.icon.run()          # must be the main thread on macOS
        except Exception:
            return False
        return True
