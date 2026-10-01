#!/usr/bin/env python3
"""SMW Stream Tools — start the background app and its web UI.

Everything runs from here: it talks to OBS over obs-websocket, reads your
console through QUsb2Snes, matches your Twitch title against the kaizoff index,
and serves both the settings page and the achievement alert overlay.

By default it sits in the system tray. Pass --no-tray to keep it in a console
window instead, or --no-browser to stop it opening the settings page at start.

Naming a tool instead runs that tool in the console and exits — see
`python run.py --help`. Those are for finding memory addresses on a hack whose
layout doesn't match the defaults, and are not needed for normal use.
"""

import os
import sys
import time
import webbrowser

# Run correctly however it was launched — double-clicked, from another
# directory, or from a PyInstaller bundle.
if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(sys.executable))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from smwtools.app import App
    from smwtools.config import Config
    from smwtools.tray import Tray, unavailable_reason as tray_reason
    from smwtools.web import AlertFeed, WebServer
except ImportError as exc:
    print("Could not load the smwtools package: %s\n" % exc)
    print("run.py needs to sit next to the smwtools folder, like this:")
    print("    smw-stream-tools/")
    print("      run.py")
    print("      smwtools/")
    print("        app.py, config.py, ... , static/")
    print("\nIf the files ended up flat in one folder, move everything except")
    print("run.py and requirements.txt into a folder named smwtools, and the")
    print("two .html files into smwtools/static.")
    if "websocket" in str(exc):
        print("\nIf it is websocket-client that is missing:")
        print("    pip install websocket-client")
    sys.exit(1)


def _wants_tool():
    """True when the first argument names a scan tool rather than a flag.

    Kept deliberately simple so the normal startup path — no arguments, or the
    two flags — behaves exactly as it always has.
    """
    from smwtools.tools import COMMANDS
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    return bool(argv) and argv[0] in COMMANDS


def main():
    if _wants_tool() or "--help" in sys.argv or "-h" in sys.argv:
        if getattr(sys, "frozen", False):
            # The build is windowed, so there is no console for output to
            # reach. Say so rather than appearing to do nothing.
            _no_console_notice()
            return 1
        from smwtools.tools import main as tools_main
        return tools_main(sys.argv[1:])

    config = Config()
    alerts = AlertFeed()
    app = App(config, alerts)

    actions = {
        "refresh": lambda body: app.refresh_now(),
        "deaths_plus": lambda body: app.snes.adjust_deaths(1),
        "deaths_minus": lambda body: app.snes.adjust_deaths(-1),
        "deaths_reset": lambda body: app.snes.reset_deaths(),
        "exits_set": lambda body: app.set_manual_exits(body.get("value", 0)),
        "test_alert": lambda body: alerts.push({
            "title": "Test Alert",
            "description": "If you can see this, the browser source is wired up.",
            "points": 10, "game": "Super Mario World", "badge": "",
        }),
    }

    web = WebServer(app, config, alerts, actions)
    try:
        port = web.start()
    except RuntimeError as exc:
        print("Could not start the web UI: %s" % exc)
        print("Another copy of this app is probably already running.")
        return 1

    app.start()
    url = "http://127.0.0.1:%d/" % port
    print("\n  SMW Stream Tools")
    print("  Settings and status : %s" % url)
    print("  Alert overlay       : %soverlay" % url)

    if "--no-browser" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    stopping = []

    def shutdown():
        if stopping:
            return
        stopping.append(True)
        app.stop()
        web.stop()

    tray = Tray(app, config, url, shutdown)
    want_tray = "--no-tray" not in sys.argv
    if want_tray:
        if tray.available():
            print("  Running in the system tray. Right-click the icon to quit.\n")
            if tray.run():              # blocks until Quit
                return 0
            reason = "the tray icon could not start"
        else:
            reason = tray_reason()
        # A windowed build has no console, so this has to reach the web UI or
        # the user sees an app with no window and no icon and no explanation.
        app.log(reason)
        app.log("Running without a tray icon. Close this window, or quit from "
                "the taskbar, to stop the app.")
        print("  %s\n" % reason)

    print("  Ctrl-C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        shutdown()
    return 0


def _no_console_notice():
    """The executable has no console, so a scan tool would print into nothing."""
    where = os.path.dirname(os.path.abspath(sys.executable))
    message = (
        "The scan tools need a console, and this executable is built without "
        "one so it can sit in the tray quietly.\n\n"
        "Run them from the source instead, in the folder holding run.py:\n\n"
        "    python run.py scan\n"
        "    python run.py --help\n")
    try:
        with open(os.path.join(where, "scan-tools.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write(message)
    except Exception:
        pass
    print(message)
    return 1


def _crash_report(exc):
    """A windowed build has no console, so a startup failure would be silent.

    Write it next to the executable instead, which is the only place a user
    will think to look.
    """
    import traceback
    where = (os.path.dirname(os.path.abspath(sys.executable))
             if getattr(sys, "frozen", False)
             else os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(where, "startup-error.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write("SMW Stream Tools failed to start.\n\n")
            handle.write(traceback.format_exc())
    except Exception:
        pass
    raise exc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        _crash_report(exc)
