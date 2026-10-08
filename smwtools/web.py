"""Local web UI and the alert overlay, served from one small HTTP server.

Binds to 127.0.0.1 only. The UI is a page you keep in a tab while streaming;
the overlay is a separate path you point an OBS browser source at.
"""

import json
import mimetypes
import os
import posixpath
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class AlertFeed(object):
    """Event ring for the achievement overlay.

    Handing out a cursor rather than replaying everything is what stops a
    browser-source reload from re-firing alerts you already saw.
    """

    def __init__(self, keep=20):
        self.lock = threading.Lock()
        self.events = []
        self.cursor = 0
        self.keep = keep

    def push(self, event):
        with self.lock:
            self.cursor += 1
            self.events.append(dict(event, id=self.cursor))
            del self.events[:-self.keep]
            return self.cursor

    def since(self, cursor):
        with self.lock:
            if cursor is None:
                return self.cursor, []
            return self.cursor, [e for e in self.events if e["id"] > cursor]


def make_server(app, config, alerts, actions, port):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # the UI polls constantly; this would drown the console

        # -- plumbing ----------------------------------------------------

        def _send(self, body, content_type="application/json", status=200):
            raw = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _json(self, payload, status=200):
            self._send(json.dumps(payload), "application/json", status)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return {}

        def _static(self, name):
            # Refuse anything that tries to climb out of the static folder.
            safe = posixpath.normpath("/" + name).lstrip("/")
            path = os.path.join(STATIC_DIR, safe)
            if not os.path.abspath(path).startswith(os.path.abspath(STATIC_DIR)):
                self.send_error(403)
                return
            if not os.path.isfile(path):
                self.send_error(404)
                return
            kind = mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as handle:
                self._send(handle.read(), kind)

        # -- routes ------------------------------------------------------

        def do_GET(self):
            parts = urllib.parse.urlparse(self.path)
            route = parts.path

            if route in ("/", "/index.html"):
                self._static("index.html")
            elif route == "/overlay" or route == "/overlay/":
                self._static("overlay.html")
            elif route == "/counters" or route == "/counters/":
                self._static("counters.html")
            elif route == "/api/state":
                payload = app.snapshot()
                payload["log"] = app.recent_log()[-60:]
                self._json(payload)
            elif route == "/api/overlay":
                # Deliberately not /api/state: a browser source has no use for
                # the log or the connection detail, and an overlay left running
                # all stream shouldn't be handed them once a second.
                state = app.snapshot()
                self._json({
                    "name_text": state.get("name_text", ""),
                    "exits_text": state.get("exits_text", ""),
                    "deaths_text": state.get("deaths_text", ""),
                    "author_text": state.get("author_text", ""),
                    "ra_text": (state.get("ra") or {}).get("text", ""),
                    "ra_available": state.get("ra_available", False),
                    "style": {k: v for k, v in config.public().items()
                              if k.startswith("ov_")},
                })
            elif route == "/api/config":
                self._json(config.public())
            elif route == "/events":
                query = urllib.parse.parse_qs(parts.query)
                raw = query.get("since", [None])[0]
                try:
                    cursor = int(raw) if raw is not None else None
                except ValueError:
                    cursor = None
                now, events = alerts.since(cursor)
                self._json({
                    "cursor": now, "events": events,
                    "hold_ms": int(config["ra_alert_seconds"] * 1000),
                    "position": config["ra_alert_position"],
                })
            elif route.startswith("/static/"):
                self._static(route[len("/static/"):])
            else:
                self.send_error(404)

        def do_POST(self):
            route = urllib.parse.urlparse(self.path).path
            body = self._body()

            if route == "/api/config":
                # A blank secret means "leave it alone", so the UI never has to
                # round-trip a password it was never shown.
                changes = {k: v for k, v in body.items()
                           if not (k in config.SECRETS and v == "")}
                # Compare after saving rather than before: update() coerces to
                # each setting's type, so "200" and 200 are the same save and
                # shouldn't count as a change worth restarting for.
                before = config.snapshot()
                config.update(changes)
                after = config.snapshot()
                moved = [key for key in after if before.get(key) != after.get(key)]
                app.restart_workers(moved)
                self._json({"ok": True, "config": config.public()})
            elif route == "/api/scan":
                # The finder reads through the tracker's own connection, so
                # these never open a second client on the device.
                what = body.get("do")
                try:
                    if what == "start":
                        result = app.scan.start(body.get("mode") or "deaths")
                    elif what == "round":
                        result = app.scan.step()
                    elif what == "apply":
                        keys = app.scan.apply(config, int(body.get("addr")))
                        app.restart_workers(keys)
                        result = app.scan.status()
                    elif what == "clear_override":
                        rom = (app.snes.state or {}).get("rom")
                        app.snes.clear_hack_deaths(rom)
                        result = app.scan.status()
                    elif what == "cancel":
                        app.scan.reset()
                        result = app.scan.status()
                    else:
                        self._json({"ok": False, "error": "unknown step"}, 400)
                        return
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 500)
                    return
                self._json({"ok": True, "scan": result,
                            "config": config.public()})
            elif route == "/api/action":
                name = body.get("action")
                handler = actions.get(name)
                if handler is None:
                    self._json({"ok": False, "error": "unknown action"}, 400)
                    return
                try:
                    result = handler(body)
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 500)
                    return
                self._json({"ok": True, "result": result})
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server


class WebServer(object):
    def __init__(self, app, config, alerts, actions):
        self.app = app
        self.config = config
        self.alerts = alerts
        self.actions = actions
        self.server = None
        self.thread = None
        self.port = None

    def start(self):
        import time
        port = self.config["web_port"]
        last = None
        # A socket does not always free instantly after a restart.
        for _ in range(10):
            try:
                self.server = make_server(self.app, self.config, self.alerts,
                                          self.actions, port)
            except OSError as exc:
                last = exc
                time.sleep(0.5)
                continue
            self.port = port
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           daemon=True)
            self.thread.start()
            return port
        raise RuntimeError("could not bind port %d: %s" % (port, last))

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        self.server = None
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self.thread = None
