"""Device layer: thin wrappers over Termux:API commands (all local, no cloud)."""

import difflib
import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

import config

CAPTURES_DIR = config.CAPTURES_DIR


def run_termux_cmd(args: List[str], timeout: int = 10, input_data: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a command safely; never raises."""
    try:
        return subprocess.run(args, input=input_data, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args, 127, "", f"Command '{args[0]}' not found. Run: pkg install termux-api")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "Command timed out.")


def _json(res: subprocess.CompletedProcess) -> Any:
    if res.returncode == 0 and res.stdout.strip():
        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError:
            return None
    return None


def _digits(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit() or ch in "+*#")


class DeviceController:
    # ---------------- contacts, calls, SMS ----------------
    @staticmethod
    def get_contacts() -> List[Dict[str, Any]]:
        data = _json(run_termux_cmd(["termux-contact-list"], timeout=15))
        return data if isinstance(data, list) else []

    @classmethod
    def search_contact(cls, name_query: str) -> List[Dict[str, str]]:
        """Substring match first, then fuzzy match (helps with speech-to-text typos)."""
        query = name_query.strip().lower()
        contacts = [c for c in cls.get_contacts() if c.get("name") and c.get("number")]
        exact = [{"name": c["name"], "number": c["number"]} for c in contacts if query in c["name"].lower()]
        if exact:
            return exact
        names = {c["name"].lower(): c for c in contacts}
        close = difflib.get_close_matches(query, list(names), n=3, cutoff=0.72)
        return [{"name": names[n]["name"], "number": names[n]["number"]} for n in close]

    @staticmethod
    def make_call(phone_number: str) -> Dict[str, Any]:
        number = _digits(phone_number)
        if not number:
            return {"success": False, "error": "Invalid phone number"}
        res = run_termux_cmd(["termux-telephony-call", number])
        if res.returncode == 0:
            return {"success": True, "message": f"Calling {number}"}
        return {"success": False, "error": res.stderr.strip() or "Failed to start call"}

    @staticmethod
    def send_sms(phone_number: str, message: str) -> Dict[str, Any]:
        number = _digits(phone_number)
        if not number:
            return {"success": False, "error": "Invalid phone number"}
        if not message.strip():
            return {"success": False, "error": "Message is empty"}
        res = run_termux_cmd(["termux-sms-send", "-n", number, message])
        if res.returncode == 0:
            return {"success": True, "message": f"SMS sent to {number}"}
        return {"success": False, "error": res.stderr.strip() or "Failed to send SMS"}

    # ---------------- audio ----------------
    @staticmethod
    def speak(text: str) -> Dict[str, Any]:
        """Android TTS, non-blocking."""
        text = text.strip()
        if not text:
            return {"success": False, "error": "Nothing to say"}
        try:
            subprocess.Popen(["termux-tts-speak", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"success": True}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": str(e)}

    @staticmethod
    def speak_blocking(text: str) -> Dict[str, Any]:
        """Android TTS, waits until speech finishes (so the mic doesn't hear it)."""
        text = text.strip()
        if not text:
            return {"success": False, "error": "Nothing to say"}
        res = run_termux_cmd(["termux-tts-speak", text], timeout=60)
        return {"success": res.returncode == 0}

    @staticmethod
    def speech_to_text() -> str:
        """Android on-device recognizer (one phrase)."""
        res = run_termux_cmd(["termux-speech-to-text"], timeout=25)
        if res.returncode != 0:
            return ""
        out = res.stdout.strip()
        if not out:
            return ""
        try:
            data = json.loads(out)
            if isinstance(data, list) and data:
                first = data[0]
                return (first.get("text", "") if isinstance(first, dict) else str(first)).strip()
            if isinstance(data, dict):
                return str(data.get("text", "")).strip()
        except json.JSONDecodeError:
            pass
        return out.splitlines()[-1].strip()

    @staticmethod
    def get_volume() -> List[Dict[str, Any]]:
        data = _json(run_termux_cmd(["termux-volume"]))
        return data if isinstance(data, list) else []

    @classmethod
    def _stream_info(cls, stream: str) -> Dict[str, Any]:
        for s in cls.get_volume():
            if s.get("stream") == stream:
                return s
        return {"stream": stream, "volume": 7, "max_volume": 15}

    @classmethod
    def set_volume(cls, stream: str = "music", volume: int = 10) -> Dict[str, Any]:
        streams = {"media": "music", "music": "music", "alarm": "alarm", "notification": "notification",
                   "ring": "ring", "system": "system", "call": "call"}
        target = streams.get(stream.lower(), "music")
        max_v = int(cls._stream_info(target).get("max_volume", 15))
        vol = max(0, min(max_v, int(volume)))
        res = run_termux_cmd(["termux-volume", target, str(vol)])
        return {"success": res.returncode == 0, "stream": target, "volume": vol, "max": max_v}

    @classmethod
    def change_volume(cls, stream: str, delta: int) -> Dict[str, Any]:
        info = cls._stream_info(stream)
        return cls.set_volume(stream, int(info.get("volume", 7)) + delta)

    @staticmethod
    def play_media(file_path: str) -> Dict[str, Any]:
        if not os.path.exists(file_path):
            return {"success": False, "error": f"File not found: {file_path}"}
        res = run_termux_cmd(["termux-media-player", "play", file_path])
        return {"success": res.returncode == 0, "message": f"Playing {os.path.basename(file_path)}"}

    @staticmethod
    def resume_media() -> Dict[str, Any]:
        res = run_termux_cmd(["termux-media-player", "play"])
        return {"success": res.returncode == 0}

    @staticmethod
    def pause_media() -> Dict[str, Any]:
        return {"success": run_termux_cmd(["termux-media-player", "pause"]).returncode == 0}

    @staticmethod
    def stop_media() -> Dict[str, Any]:
        return {"success": run_termux_cmd(["termux-media-player", "stop"]).returncode == 0}

    @staticmethod
    def get_media_info() -> Dict[str, Any]:
        res = run_termux_cmd(["termux-media-player", "info"])
        return {"info": res.stdout.strip()} if res.returncode == 0 else {"status": "stopped"}

    # ---------------- camera, clipboard ----------------
    @staticmethod
    def take_photo(camera_id: int = 0) -> Dict[str, Any]:
        fname = f"photo_{int(time.time())}.jpg"
        out_path = os.path.join(CAPTURES_DIR, fname)
        res = run_termux_cmd(["termux-camera-photo", "-c", str(camera_id), out_path], timeout=20)
        if res.returncode == 0 and os.path.exists(out_path):
            return {"success": True, "filename": fname, "file_path": out_path, "url": f"/captures/{fname}",
                    "facing": "back" if str(camera_id) == "0" else "front"}
        return {"success": False, "error": res.stderr.strip() or "Camera capture failed. Check Camera permission."}

    @staticmethod
    def get_clipboard() -> Dict[str, Any]:
        res = run_termux_cmd(["termux-clipboard-get"])
        return {"success": res.returncode == 0, "text": res.stdout if res.returncode == 0 else ""}

    @staticmethod
    def set_clipboard(text: str) -> Dict[str, Any]:
        return {"success": run_termux_cmd(["termux-clipboard-set", text]).returncode == 0}

    # ---------------- system ----------------
    @staticmethod
    def vibrate(duration_ms: int = 500) -> Dict[str, Any]:
        res = run_termux_cmd(["termux-vibrate", "-d", str(duration_ms), "-f"])
        return {"success": res.returncode == 0, "duration_ms": duration_ms}

    @staticmethod
    def get_battery() -> Dict[str, Any]:
        data = _json(run_termux_cmd(["termux-battery-status"]))
        return data if isinstance(data, dict) else {"percentage": None, "status": "UNKNOWN"}

    @staticmethod
    def toggle_torch(enabled: bool) -> Dict[str, Any]:
        state = "on" if enabled else "off"
        return {"success": run_termux_cmd(["termux-torch", state]).returncode == 0, "state": state}

    @staticmethod
    def set_brightness(percent: int) -> Dict[str, Any]:
        value = max(0, min(255, round(percent * 255 / 100)))
        res = run_termux_cmd(["termux-brightness", str(value)])
        return {"success": res.returncode == 0, "percent": percent}

    @staticmethod
    def send_notification(title: str, content: str) -> Dict[str, Any]:
        res = run_termux_cmd(["termux-notification", "--title", title, "--content", content, "--priority", "high"])
        return {"success": res.returncode == 0}

    @staticmethod
    def get_wifi_info() -> Dict[str, Any]:
        data = _json(run_termux_cmd(["termux-wifi-connectioninfo"]))
        if isinstance(data, dict):
            return {"success": True, "data": data}
        return {"success": False, "error": "Unable to read Wi-Fi info"}

    @staticmethod
    def toggle_wifi(enable: bool) -> Dict[str, Any]:
        res = run_termux_cmd(["termux-wifi-enable", "true" if enable else "false"])
        return {"success": res.returncode == 0, "enabled": enable}

    @staticmethod
    def get_location(provider: str = "network", request: str = "last") -> Dict[str, Any]:
        for prov, req, tmo in ((provider, request, 10), ("gps", "last", 6)):
            loc = _json(run_termux_cmd(["termux-location", "-p", prov, "-r", req], timeout=tmo))
            if isinstance(loc, dict) and "latitude" in loc and "longitude" in loc:
                return {"success": True, "location": loc}
        return {"success": False, "error": "Location unavailable. Turn on GPS and allow Location for Termux:API."}

    @staticmethod
    def open_url(url: str) -> Dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            return {"success": False, "error": "Only http/https links can be opened"}
        return {"success": run_termux_cmd(["termux-open-url", url]).returncode == 0}

    @classmethod
    def open_app(cls, package: Optional[str], fallback_url: Optional[str] = None) -> Dict[str, Any]:
        if package == "__settings__":
            res = run_termux_cmd(["am", "start", "-a", "android.settings.SETTINGS"])
            return {"success": res.returncode == 0}
        if package:
            res = run_termux_cmd(["am", "start", "-a", "android.intent.action.MAIN",
                                  "-c", "android.intent.category.LAUNCHER", "-p", package])
            if res.returncode == 0 and "Error" not in res.stdout + res.stderr:
                return {"success": True}
        if fallback_url:
            return cls.open_url(fallback_url)
        return {"success": False, "error": "Could not open the app"}


def execute_extended_action(action: str, arg: Optional[str] = "") -> Dict[str, Any]:
    """Dispatch [ACTION:name:arg] tags produced by the LLM or the REST API."""
    act = (action or "").strip().lower()
    val = (arg or "").strip()
    D = DeviceController

    if act == "photo":
        return D.take_photo(1 if ("front" in val or "selfie" in val or val == "1") else 0)
    if act == "vibrate":
        return D.vibrate(min(int(val), 5000) if val.isdigit() else 500)
    if act == "clipboard_set":
        return D.set_clipboard(val)
    if act == "clipboard_get":
        return D.get_clipboard()
    if act == "notification":
        sep = "|" if "|" in val else (":" if ":" in val else None)
        title, content = (val.split(sep, 1) if sep else ("Jarvis", val))
        return D.send_notification(title.strip() or "Jarvis", content.strip())
    if act == "volume":
        stream, lvl = (val.split(":", 1) if ":" in val else ("music", val))
        return D.set_volume(stream.strip(), int(lvl) if lvl.strip().isdigit() else 10)
    if act == "wifi_info":
        return D.get_wifi_info()
    if act == "wifi_enable":
        return D.toggle_wifi(val.lower() in ("true", "1", "on"))
    if act == "location":
        return D.get_location()
    if act == "media":
        if val.startswith("play"):
            path = val.split(":", 1)[1].strip() if ":" in val else ""
            return D.play_media(path) if path else D.resume_media()
        if val == "pause":
            return D.pause_media()
        if val == "stop":
            return D.stop_media()
        return D.get_media_info()
    if act == "torch_on":
        return D.toggle_torch(True)
    if act == "torch_off":
        return D.toggle_torch(False)
    if act == "battery":
        return D.get_battery()
    if act == "brightness":
        return D.set_brightness(int(val) if val.isdigit() else 50)
    if act == "open_url":
        return D.open_url(val)
    return {"success": False, "error": f"Unknown action: {act}"}
