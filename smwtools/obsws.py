"""obs-websocket v5 client.

Talks to OBS from outside, over the WebSocket server built into OBS 28 and
later (Tools -> WebSocket Server Settings). That is the whole reason this
project stopped being an OBS script: getting Python working *inside* OBS is
the hardest part of the setup for most people, and it is worst on macOS.

Only websocket-client is needed; the v5 auth handshake is a few lines of
hashing, so there is no reason to pull in a heavier dependency.
"""

import base64
import hashlib
import json
import threading
import time

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover - surfaced in the UI instead
    websocket = None

DEFAULT_URL = "ws://127.0.0.1:4455"

# obs-websocket v5 opcodes
OP_HELLO = 0
OP_IDENTIFY = 1
OP_IDENTIFIED = 2
OP_REQUEST = 6
OP_REQUEST_RESPONSE = 7


class ObsError(RuntimeError):
    """The connection to OBS is unusable."""


class ObsRequestError(ObsError):
    """One request failed, but the connection is fine.

    A missing source is not a reason to tear down and rebuild the whole
    connection, which is what happens if the caller cannot tell the two apart.
    """


def _auth_response(password, salt, challenge):
    """The v5 challenge: base64(sha256(base64(sha256(password + salt)) + challenge))."""
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()).decode()
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode()


class ObsClient(object):
    """Blocking request/response client, safe to call from one worker thread."""

    def __init__(self, url=DEFAULT_URL, password="", timeout=5.0):
        self.url = url or DEFAULT_URL
        self.password = password or ""
        self.timeout = timeout
        self.ws = None
        self.version = None
        self._lock = threading.Lock()
        self._request_id = 0

    # -- connection ------------------------------------------------------

    def _recv_json(self, context):
        """Receive one frame as JSON, turning a dropped socket into a clear error.

        OBS answers a wrong password by simply closing the connection, which
        otherwise surfaces as a JSON parse error and tells the user nothing.
        """
        try:
            raw = self.ws.recv()
        except Exception as exc:
            raise ObsError("OBS closed the connection while %s — if you have a "
                           "WebSocket password set, check it matches (%s)"
                           % (context, exc))
        if not raw:
            raise ObsError("OBS closed the connection while %s — the WebSocket "
                           "password is probably wrong" % context)
        try:
            return json.loads(raw)
        except ValueError:
            raise ObsError("unreadable reply from OBS while %s" % context)

    def connect(self):
        if websocket is None:
            raise ObsError("websocket-client is not installed")
        try:
            self.ws = websocket.create_connection(self.url, timeout=self.timeout)
        except Exception as exc:
            raise ObsError("could not reach OBS at %s — is OBS running with its "
                           "WebSocket server enabled? (%s)" % (self.url, exc))

        hello = self._recv_json("connecting")
        if hello.get("op") != OP_HELLO:
            raise ObsError("unexpected greeting from OBS: %r" % hello)
        data = hello.get("d", {})
        self.version = data.get("obsWebSocketVersion")

        identify = {"op": OP_IDENTIFY, "d": {"rpcVersion": data.get("rpcVersion", 1)}}
        auth = data.get("authentication")
        if auth:
            if not self.password:
                raise ObsError(
                    "OBS requires a WebSocket password — copy it from "
                    "Tools > WebSocket Server Settings > Show Connect Info")
            identify["d"]["authentication"] = _auth_response(
                self.password, auth["salt"], auth["challenge"])
        self.ws.send(json.dumps(identify))

        reply = self._recv_json("authenticating")
        if reply.get("op") != OP_IDENTIFIED:
            raise ObsError("OBS rejected the connection — check the password")
        return self.version

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

    @property
    def connected(self):
        return self.ws is not None

    # -- requests --------------------------------------------------------

    def request(self, request_type, data=None):
        """Send one request and wait for its response.

        Events arrive on the same socket, so anything that is not the reply we
        are waiting for gets skipped rather than confusing the caller.
        """
        if self.ws is None:
            raise ObsError("not connected")
        with self._lock:
            self._request_id += 1
            request_id = str(self._request_id)
            self.ws.send(json.dumps({
                "op": OP_REQUEST,
                "d": {
                    "requestType": request_type,
                    "requestId": request_id,
                    "requestData": data or {},
                },
            }))
            deadline = time.time() + self.timeout
            while True:
                if time.time() > deadline:
                    raise ObsError("timed out waiting for %s" % request_type)
                message = self._recv_json("waiting for %s" % request_type)
                if message.get("op") != OP_REQUEST_RESPONSE:
                    continue
                body = message.get("d", {})
                if body.get("requestId") != request_id:
                    continue
                status = body.get("requestStatus", {})
                if not status.get("result"):
                    raise ObsRequestError("%s failed: %s" % (
                        request_type, status.get("comment") or status.get("code")))
                return body.get("responseData") or {}

    # -- the handful of things this app actually needs --------------------

    def text_sources(self):
        """Names of every text source, for the dropdowns in the UI."""
        inputs = self.request("GetInputList").get("inputs", [])
        return sorted(
            item["inputName"] for item in inputs
            if "text" in (item.get("inputKind") or "").lower())

    def get_text(self, name):
        settings = self.request("GetInputSettings",
                                {"inputName": name}).get("inputSettings", {})
        return settings.get("text", "")

    def set_text(self, name, text):
        self.request("SetInputSettings", {
            "inputName": name,
            "inputSettings": {"text": text},
            "overlay": True,          # merge, so font and colour are untouched
        })

    # -- scenes, transforms and fonts --------------------------------------

    def current_scene(self):
        data = self.request("GetCurrentProgramScene")
        # obs-websocket renamed this field in 5.5; accept either.
        return data.get("sceneName") or data.get("currentProgramSceneName")

    def find_item(self, source_name, scene=None):
        """Locate a source in the current scene, looking inside groups too.

        Returns (scene_name, item_id) or (None, None). Groups matter because a
        tidy overlay usually has its text sources bundled into one.
        """
        scene = scene or self.current_scene()
        if not scene or not source_name:
            return None, None
        try:
            item_id = self.request("GetSceneItemId", {
                "sceneName": scene, "sourceName": source_name})["sceneItemId"]
            return scene, item_id
        except ObsRequestError:
            pass
        try:
            items = self.request("GetSceneItemList",
                                 {"sceneName": scene}).get("sceneItems", [])
        except ObsRequestError:
            return None, None
        for item in items:
            if not item.get("isGroup"):
                continue
            group = item.get("sourceName")
            try:
                sub = self.request("GetGroupSceneItemList",
                                   {"sceneName": group}).get("sceneItems", [])
            except ObsRequestError:
                continue
            for child in sub:
                if child.get("sourceName") == source_name:
                    return group, child["sceneItemId"]
        return None, None

    def get_transform(self, scene, item_id):
        return self.request("GetSceneItemTransform", {
            "sceneName": scene, "sceneItemId": item_id})["sceneItemTransform"]

    def set_position(self, scene, item_id, x, y):
        self.request("SetSceneItemTransform", {
            "sceneName": scene, "sceneItemId": item_id,
            "sceneItemTransform": {"positionX": float(x), "positionY": float(y)}})

    def get_font(self, name):
        settings = self.request("GetInputSettings",
                                {"inputName": name}).get("inputSettings", {})
        return dict(settings.get("font") or {})

    def set_font_size(self, name, size, font=None):
        """Font is a nested object, so it has to be written back whole —
        sending only the size would drop the face, style and everything else."""
        font = dict(font or self.get_font(name))
        if not font:
            return False
        font["size"] = int(size)
        self.request("SetInputSettings", {
            "inputName": name, "inputSettings": {"font": font}, "overlay": True})
        return True

    def source_size(self, scene, item_id):
        """Rendered size in scene space, which is what a layout cares about."""
        t = self.get_transform(scene, item_id)
        width = (t.get("sourceWidth") or 0) * (t.get("scaleX") or 1.0)
        height = (t.get("sourceHeight") or 0) * (t.get("scaleY") or 1.0)
        return width, height, t

    def source_exists(self, name):
        try:
            self.request("GetInputSettings", {"inputName": name})
            return True
        except ObsRequestError:
            return False
