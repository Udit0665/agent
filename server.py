"""Pocket Jarvis daemon.

One process = background voice listener + small local web UI.
The web page is only a remote control / transcript; closing it changes nothing.
"""

import http.server
import json
import mimetypes
import os
import signal
import threading
import time
import urllib.parse

import config
from brain import AgentBrain
from device import DeviceController, execute_extended_action
from hub import hub
from listener import Listener
from voice_engine import chariot_voice as voice

brain = AgentBrain()
device = DeviceController()

LOOPBACK = {"localhost", "127.0.0.1", "::1"}
ACTIONS = {"photo", "vibrate", "clipboard_set", "clipboard_get", "notification", "volume", "wifi_info",
           "wifi_enable", "location", "media", "torch_on", "torch_off", "battery", "brightness", "open_url"}

_cache: dict = {}


def cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value


def _hostname(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")]
    return value.split(":")[0]


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=config.STATIC_DIR, **kwargs)

    def log_message(self, fmt, *args):
        if "/api/events" in self.path or "/api/state" in self.path:
            return
        super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # --- helpers
    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guard(self, post=False) -> bool:
        """Block DNS-rebinding and cross-site requests: this server can place calls."""
        if config.HOST in LOOPBACK:
            if _hostname(self.headers.get("Host", "")) not in LOOPBACK:
                self.send_json({"error": "bad host"}, 403)
                return False
        if post:
            origin = self.headers.get("Origin")
            if origin and _hostname(urllib.parse.urlparse(origin).netloc) not in LOOPBACK:
                self.send_json({"error": "bad origin"}, 403)
                return False
            if "application/json" not in self.headers.get("Content-Type", ""):
                self.send_json({"error": "json required"}, 415)
                return False
        return True

    # --- GET
    def do_GET(self):
        if not self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)

        if path.startswith("/captures/"):
            name = os.path.basename(path)
            full = os.path.join(config.CAPTURES_DIR, name)
            if name.lower().endswith((".jpg", ".wav", ".mp3")) and os.path.isfile(full):
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(os.path.getsize(full)))
                self.end_headers()
                with open(full, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404)
            return

        if path == "/api/state":
            battery = cached("battery", 20, device.get_battery)
            wifi = cached("wifi", 30, device.get_wifi_info).get("data", {})
            return self.send_json({
                "state": hub.state, "detail": hub.detail, "muted": hub.muted,
                "session_active": hub.session_active, "last_id": hub.last_id(),
                "battery": battery.get("percentage"), "charging": battery.get("status") == "CHARGING",
                "wifi": wifi.get("ssid"), "llm": brain.is_llm_online(),
                "stt": config.STT_BACKEND, "tts": voice.engine_name, "wake": config.WAKE_WORDS[0],
            })

        if path == "/api/events":
            since = int((query.get("since") or ["0"])[0] or 0)
            return self.send_json({"events": hub.since(since), "last_id": hub.last_id()})

        if path == "/api/device/location":
            return self.send_json(device.get_location())
        if path == "/api/device/volume":
            return self.send_json({"streams": device.get_volume()})

        return super().do_GET()

    # --- POST
    def do_POST(self):
        if not self._guard(post=True):
            return
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode() or "{}") if length else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}

        if path == "/api/chat":
            message = str(payload.get("message", "")).strip()
            if not message:
                return self.send_json({"reply": "Say or type a command."})
            hub.add("user", message, source="ui")
            res = brain.process_instruction(message, source="ui")
            hub.add("bot", res["reply"], photo_url=res.get("photo_url"))
            if payload.get("speak", True):
                voice.speak(res["reply"], block=False)
            return self.send_json(res)

        if path == "/api/listen":
            hub.muted = not bool(payload.get("enabled", True))
            hub.add("system", "Microphone muted." if hub.muted else "Microphone on.")
            return self.send_json({"muted": hub.muted})

        if path == "/api/wake":
            hub.session_until = time.time() + config.SESSION_SECS
            return self.send_json({"session_active": True})

        if path == "/api/action":
            action = str(payload.get("action", ""))
            if action in ACTIONS:
                return self.send_json(execute_extended_action(action, str(payload.get("arg", ""))))
            return self.send_json({"success": False, "error": f"Unsupported action: {action}"}, 400)

        self.send_error(404)

    def do_OPTIONS(self):  # no CORS on purpose
        self.send_response(204)
        self.end_headers()


def main():
    listener = Listener(brain, voice)
    hub.muted = not config.AUTOSTART_LISTEN
    listener.start()

    httpd = http.server.ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    pid_file = os.path.join(config.BASE_DIR, "jarvis.pid")
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    def shutdown(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    print("=" * 50)
    print(f"  Jarvis running in background. UI: http://localhost:{config.PORT}")
    print(f"  Voice: STT={config.STT_BACKEND}  TTS={voice.engine_name}  wake word='{config.WAKE_WORDS[0]}'")
    print("=" * 50, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if os.path.exists(pid_file):
            os.remove(pid_file)


if __name__ == "__main__":
    main()
