"""Brain: turns a sentence (English / Hindi / Hinglish) into a phone action.

1. Fast rule-based intents (instant, offline, no RAM).
2. Optional local LLM (llama-server) with [ACTION:...] tags for everything else.
The brain only decides and executes. Speaking the reply is done by the caller.
"""

import json
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
from device import DeviceController, execute_extended_action
from hub import hub

SYSTEM_PROMPT = """You are JARVIS, a private voice assistant running locally on the user's Android phone (Termux).
The user may speak English, Hindi or Hinglish. Reply in the same language, in at most 2 short sentences.
Your reply is read aloud: no markdown, no emojis, no lists.

For a phone action, put one or more [ACTION:...] tags in the reply:
- [ACTION:photo:0] (0 back camera, 1 selfie)
- [ACTION:vibrate:500]
- [ACTION:volume:music:10] (streams: alarm, music, notification, ring, system, call)
- [ACTION:brightness:60] (percent)
- [ACTION:clipboard_set:text] / [ACTION:clipboard_get]
- [ACTION:notification:Title|Message]
- [ACTION:wifi_info] / [ACTION:location] / [ACTION:battery]
- [ACTION:torch_on] / [ACTION:torch_off]
- [ACTION:open_url:https://example.com]

Example:
User: vibrate karo aur volume 10 kar do
Jarvis: [ACTION:vibrate:500] [ACTION:volume:music:10] Done, phone vibrate hua aur volume 10 hai.
"""

APPS: Dict[str, Tuple[Optional[str], Optional[str]]] = {
    "youtube": ("com.google.android.youtube", "https://m.youtube.com"),
    "whatsapp": ("com.whatsapp", "https://web.whatsapp.com"),
    "chrome": ("com.android.chrome", "https://www.google.com"),
    "google": (None, "https://www.google.com"),
    "maps": ("com.google.android.apps.maps", "https://maps.google.com"),
    "gmail": ("com.google.android.gm", "https://mail.google.com"),
    "instagram": ("com.instagram.android", "https://www.instagram.com"),
    "telegram": ("org.telegram.messenger", "https://web.telegram.org"),
    "spotify": ("com.spotify.music", "https://open.spotify.com"),
    "play store": ("com.android.vending", "https://play.google.com"),
    "settings": ("__settings__", None),
}

ON_WORDS = {"on", "chalu", "chalao", "jalao", "start", "enable", "shuru", "चालू"}
OFF_WORDS = {"off", "band", "bandh", "bujhao", "disable", "बंद"}
YES = {"yes", "yeah", "yep", "haan", "han", "ha", "haa", "confirm", "ok", "okay", "sure", "kardo", "kar", "theek", "correct", "हां"}
NO = {"no", "nope", "nahi", "nahin", "cancel", "mat", "don't", "dont", "ruko", "nahi."}


@dataclass
class Intent:
    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    confirm: Optional[str] = None  # if set and source == "voice", ask before running


class AgentBrain:
    def __init__(self) -> None:
        self.device = DeviceController()
        self._lock = threading.RLock()
        self._pending: Optional[Dict[str, Any]] = None
        self._history: List[Dict[str, str]] = []
        self._llm_ok = False
        self._llm_checked = 0.0

    # ------------------------------------------------------------------ LLM
    def is_llm_online(self) -> bool:
        now = time.time()
        if now - self._llm_checked < 10:
            return self._llm_ok
        try:
            with urllib.request.urlopen(f"{config.LLAMA_SERVER_URL}/health", timeout=1) as resp:
                self._llm_ok = resp.status == 200
        except Exception:  # noqa: BLE001
            self._llm_ok = False
        self._llm_checked = now
        return self._llm_ok

    def query_llama_server(self, user_text: str) -> Optional[str]:
        if not self.is_llm_online():
            return None
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + self._history[-6:] + \
                   [{"role": "user", "content": user_text}]
        payload = {"messages": messages, "max_tokens": 120, "temperature": 0.5}
        req = urllib.request.Request(f"{config.LLAMA_SERVER_URL}/v1/chat/completions",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = json.loads(resp.read().decode())
                return body["choices"][0]["message"]["content"].strip()
        except Exception as e:  # noqa: BLE001
            print(f"[brain] llama-server error: {e}")
            self._llm_checked = 0
            return None

    def parse_and_execute_actions(self, text: str) -> Tuple[str, List[Dict[str, Any]]]:
        pattern = re.compile(r"\[ACTION:([a-zA-Z0-9_]+)(?::([^\]]*))?\]")
        done: List[Dict[str, Any]] = []
        for name, arg in pattern.findall(text):
            if name.lower() in ("speak", "call", "sms"):  # reply is spoken anyway; no calls from LLM
                continue
            done.append({"action": name, "arg": arg, "result": execute_extended_action(name, arg)})
        clean = re.sub(r"\s+", " ", pattern.sub("", text)).strip()
        return clean, done

    # --------------------------------------------------------------- parsing
    def _resolve_target(self, target: str) -> Tuple[Optional[str], str, Optional[str]]:
        target = target.strip()
        if any(ch.isdigit() for ch in target):
            return target, target, None
        contacts = self.device.search_contact(target)
        if not contacts:
            return None, target, f"I couldn't find {target} in your contacts."
        return contacts[0]["number"], contacts[0]["name"], None

    def parse_intent(self, text: str) -> Optional[Intent]:  # noqa: C901 - one flat rule list on purpose
        clean = re.sub(r"\s+", " ", text.strip()).strip(" .!?,;")
        if not clean:
            return None
        lower = clean.lower().replace("wi-fi", "wifi").replace("wi fi", "wifi")
        toks = {t.strip(".,!?;:\"'") for t in lower.split()}

        if re.search(r"\b(go to sleep|sleep mode|stop listening|chup ho ja|chup ho jao|so ja)\b", lower):
            return Intent("sleep")
        if lower in ("stop", "cancel", "ruko", "rehne do", "never mind", "nevermind"):
            return Intent("noop")
        if toks <= {"hello", "hi", "hey", "namaste", "hii", "jarvis", "there", "boss", "bhai"}:
            return Intent("chat", {"reply": "Yes boss, I'm here. What can I do for you?"})
        if toks <= {"thanks", "thank", "you", "shukriya", "dhanyavad", "thanku", "so", "much", "jarvis"}:
            return Intent("chat", {"reply": "Anytime."})

        if re.search(r"\b(what(?:'s| is)? the time|what time|current time|time kya|kitne baje|time batao|tell me the time)\b", lower):
            return Intent("time")
        if re.search(r"\b(what(?:'s| is)? (?:the )?(?:date|day)|today'?s date|aaj (?:ki )?(?:date|tarikh)|aaj kya (?:din|tarikh)|tarikh kya)\b", lower):
            return Intent("date")

        # --- call
        m = re.match(r"^(?:please\s+)?(?:call|phone|dial)\s+(?:to\s+)?(.+)$", lower) or \
            re.match(r"^(.+?)\s+ko\s+(?:call|phone|fone)(?:\s+(?:karo|kar|lagao|laga|kardo|kar do|laga do|milao))?$", lower)
        if m:
            number, label, err = self._resolve_target(m.group(1))
            if err:
                return Intent("error", {"message": err})
            return Intent("call", {"number": number, "label": label}, confirm=f"Call {label}? Say yes to confirm.")

        # --- sms
        m = re.match(r"^(?:send\s+)?(?:sms|text|msg|message)\s+(?:to\s+)?(.+?)\s*(?::|\bsaying\b|\bthat\b|\bki\b)\s*(.+)$", clean, re.I) or \
            re.match(r"^(.+?)\s+ko\s+(?:sms|text|msg|message)\s+(?:bhejo|bhej\s*do|karo|kar\s*do|send)\s*(?:ki\s+|:)?\s*(.+)$", clean, re.I) or \
            re.match(r"^(?:sms|msg|message|text)\s+(\S+)\s+(.+)$", clean, re.I)
        if m:
            number, label, err = self._resolve_target(m.group(1))
            if err:
                return Intent("error", {"message": err})
            body = m.group(2).strip()
            return Intent("sms", {"number": number, "label": label, "body": body},
                          confirm=f"Send \"{body}\" to {label}? Say yes to confirm.")

        # --- torch
        if "torch" in lower or "flashlight" in lower or "टॉर्च" in lower:
            return Intent("torch", {"on": not (toks & OFF_WORDS)})

        if "battery" in lower or "बैटरी" in lower:
            return Intent("battery")

        if toks & {"vibrate", "buzz", "vibration"}:
            m = re.search(r"(\d+)\s*(ms|millis|milliseconds|seconds|second|sec|s)?\b", lower)
            ms = 500
            if m:
                n = int(m.group(1))
                ms = n * 1000 if (m.group(2) or "").startswith("s") else n
            return Intent("vibrate", {"ms": max(100, min(ms, 5000))})

        if "brightness" in lower:
            m = re.search(r"(\d+)", lower)
            if m:
                return Intent("brightness", {"percent": max(0, min(100, int(m.group(1))))})
            if toks & {"max", "full", "zyada", "up", "increase", "badhao"}:
                return Intent("brightness", {"percent": 100})
            if toks & {"min", "low", "kam", "down", "decrease", "ghatao"}:
                return Intent("brightness", {"percent": 10})

        if "volume" in lower or "awaaz" in lower or "awaz" in lower or "आवाज" in lower or toks & {"mute", "unmute"}:
            stream = next((s for s in ("alarm", "ring", "notification", "system", "call") if s in toks), "music")
            num = re.search(r"(\d+)", lower)
            if "unmute" in toks:
                return Intent("volume", {"stream": stream, "level": 7})
            if "mute" in toks:
                return Intent("volume", {"stream": stream, "level": 0})
            if num:
                return Intent("volume", {"stream": stream, "level": int(num.group(1))})
            if toks & {"max", "full"}:
                return Intent("volume", {"stream": stream, "level": 99})
            if toks & {"up", "increase", "badhao", "badha", "zyada", "jyada", "high", "louder", "more"}:
                return Intent("volume", {"stream": stream, "delta": 2})
            if toks & {"down", "decrease", "kam", "ghatao", "low", "lower", "softer", "less"}:
                return Intent("volume", {"stream": stream, "delta": -2})

        verbs = {"take", "click", "capture", "snap", "khicho", "kheecho", "khincho", "lo", "le", "lelo", "lena"}
        if "selfie" in lower or "front camera" in lower:
            return Intent("photo", {"camera": 1})
        if (toks & {"photo", "picture", "pic", "tasveer", "फोटो"}) and toks & verbs:
            return Intent("photo", {"camera": 0})

        if "clipboard" in lower:
            m = re.match(r"^(?:copy|set clipboard(?: to)?)\s+(.+)$", clean, re.I)
            if m:
                return Intent("clipboard_set", {"text": m.group(1).strip()})
            return Intent("clipboard_get")
        m = re.match(r"^copy\s+(.+)$", clean, re.I)
        if m:
            return Intent("clipboard_set", {"text": m.group(1).strip()})

        if "wifi" in lower:
            if toks & OFF_WORDS:
                return Intent("wifi_toggle", {"on": False})
            if toks & ON_WORDS:
                return Intent("wifi_toggle", {"on": True})
            return Intent("wifi_info")

        if any(p in lower for p in ("where am i", "my location", "current location", "gps location",
                                    "check location", "main kahan hu", "mai kahan hu", "meri location")):
            return Intent("location")

        m = re.match(r"^(?:notify|send notification|push alert|remind me)\s+(?:me\s+)?(.+)$", clean, re.I)
        if m:
            return Intent("notification", {"content": m.group(1).strip()})

        if re.search(r"\b(pause)\b.*\b(music|audio|song|gana)\b", lower):
            return Intent("media", {"op": "pause"})
        if re.search(r"\b(stop)\b.*\b(music|audio|song|gana)\b", lower):
            return Intent("media", {"op": "stop"})
        if re.search(r"\b(resume|continue)\b.*\b(music|audio|song|gana)\b", lower):
            return Intent("media", {"op": "resume"})
        m = re.match(r"^play\s+(?:music|song|audio)\s*(.*)$", lower)
        if m:
            return Intent("media", {"op": "play", "path": m.group(1).strip()})

        m = re.match(r"^(?:open|launch|kholo|start)\s+(.+)$", lower) or \
            re.match(r"^(.+?)\s+(?:kholo|open karo|open kar|khol do|chalao)$", lower)
        if m:
            return Intent("open", {"target": m.group(1).strip()})

        m = re.match(r"^(?:say|speak|announce|bolo)\s+(.+)$", clean, re.I)
        if m:
            return Intent("chat", {"reply": m.group(1).strip()})

        return None

    # ------------------------------------------------------------- execution
    def _run_intent(self, it: Intent) -> Dict[str, Any]:  # noqa: C901
        d, a, n = self.device, it.args, it.name
        res: Dict[str, Any] = {}
        photo = None
        end_session = False
        ok = lambda: bool(res.get("success"))  # noqa: E731

        if n == "chat":
            reply = a["reply"]
        elif n == "noop":
            reply = "Okay."
        elif n == "error":
            reply = a["message"]
        elif n == "sleep":
            reply, end_session = "Okay. Say my name when you need me.", True
        elif n == "time":
            reply = "It's " + datetime.now().strftime("%I:%M %p").lstrip("0") + "."
        elif n == "date":
            reply = "Today is " + datetime.now().strftime("%A, %d %B %Y") + "."
        elif n == "call":
            res = d.make_call(a["number"])
            reply = f"Calling {a['label']}." if ok() else f"I couldn't place the call. {res.get('error', '')}"
        elif n == "sms":
            res = d.send_sms(a["number"], a["body"])
            reply = f"Message sent to {a['label']}." if ok() else f"I couldn't send the message. {res.get('error', '')}"
        elif n == "torch":
            res = d.toggle_torch(a["on"])
            reply = f"Flashlight {'on' if a['on'] else 'off'}." if ok() else "I couldn't reach the flashlight."
        elif n == "battery":
            res = d.get_battery()
            pct = res.get("percentage")
            status = str(res.get("status", "")).lower().replace("_", " ")
            reply = f"Battery is at {pct} percent, {status}." if pct is not None else "I couldn't read the battery."
        elif n == "vibrate":
            res = d.vibrate(a["ms"])
            reply = "Vibrating." if ok() else "Vibration failed."
        elif n == "brightness":
            res = d.set_brightness(a["percent"])
            reply = f"Brightness set to {a['percent']} percent." if ok() else "I couldn't change the brightness."
        elif n == "volume":
            res = d.change_volume(a["stream"], a["delta"]) if "delta" in a else d.set_volume(a["stream"], a["level"])
            if ok():
                reply = "Muted." if res["volume"] == 0 else f"{res['stream'].capitalize()} volume is {res['volume']} of {res['max']}."
            else:
                reply = "I couldn't change the volume."
        elif n == "photo":
            res = d.take_photo(a["camera"])
            if ok():
                photo = res["url"]
                reply = "Here's your selfie." if a["camera"] == 1 else "Photo captured."
            else:
                reply = f"Photo failed. {res.get('error', '')}"
        elif n == "clipboard_get":
            res = d.get_clipboard()
            txt = res.get("text", "").strip()
            reply = f"Your clipboard says: {txt}" if txt else "Your clipboard is empty."
        elif n == "clipboard_set":
            res = d.set_clipboard(a["text"])
            reply = "Copied." if ok() else "I couldn't copy that."
        elif n == "wifi_info":
            res = d.get_wifi_info()
            data = res.get("data", {})
            reply = f"Connected to {data.get('ssid', 'unknown')}, IP {data.get('ip', 'unknown')}." if ok() else "I couldn't read Wi-Fi status."
        elif n == "wifi_toggle":
            res = d.toggle_wifi(a["on"])
            reply = f"Wi-Fi {'enabled' if a['on'] else 'disabled'}." if ok() else "Android blocked the Wi-Fi change."
        elif n == "location":
            res = d.get_location()
            loc = res.get("location", {})
            reply = (f"You are at latitude {round(loc['latitude'], 4)}, longitude {round(loc['longitude'], 4)}."
                     if ok() else res.get("error", "Location unavailable."))
        elif n == "notification":
            res = d.send_notification("Jarvis", a["content"])
            reply = "Notification posted." if ok() else "Notification failed."
        elif n == "media":
            op = a["op"]
            if op == "pause":
                res, reply = d.pause_media(), "Paused."
            elif op == "stop":
                res, reply = d.stop_media(), "Stopped."
            elif op == "resume":
                res, reply = d.resume_media(), "Resuming."
            else:
                path = a.get("path", "")
                res = d.play_media(path) if path else d.resume_media()
                reply = "Playing." if ok() else res.get("error", "Tell me a valid file path.")
        elif n == "open":
            target = a["target"]
            pkg, url = APPS.get(target, (None, None))
            if target in APPS:
                res = d.open_app(pkg, url)
            elif "." in target and " " not in target:
                res = d.open_url(target if target.startswith("http") else f"https://{target}")
            else:
                res = d.open_url("https://www.google.com/search?q=" + urllib.request.quote(target))
            reply = f"Opening {target}." if ok() else f"I couldn't open {target}."
        else:
            reply = "Done."

        payload: Dict[str, Any] = {"reply": reply, "details": {"action": n, **a, "result": res}}
        if photo:
            payload["photo_url"] = photo
        if end_session:
            payload["end_session"] = True
            hub.session_until = 0.0
        return payload

    # ----------------------------------------------------------------- entry
    def has_pending(self) -> bool:
        p = self._pending
        return bool(p and time.time() < p["expires"])

    @staticmethod
    def _yes_no(text: str) -> Optional[bool]:
        toks = [t.strip(".,!?") for t in text.lower().split()]
        if not toks or len(toks) > 4:
            return None
        if any(t in NO for t in toks):
            return False
        if any(t in YES for t in toks):
            return True
        return None

    def process_instruction(self, user_text: str, source: str = "ui") -> Dict[str, Any]:
        """source: 'voice' (asks confirmation for call/SMS) or 'ui' (typed/tapped)."""
        text = user_text.strip()
        if not text:
            return {"reply": "Say or type a command."}
        with self._lock:
            if self._pending:
                pending, self._pending = self._pending, None
                if time.time() < pending["expires"]:
                    answer = self._yes_no(text)
                    if answer is True:
                        return self._run_intent(pending["intent"])
                    if answer is False:
                        return {"reply": "Okay, cancelled.", "details": {"action": "cancelled"}}

            intent = self.parse_intent(text)
            if intent:
                if intent.confirm and source == "voice" and config.CONFIRM_VOICE_CALLS:
                    self._pending = {"intent": intent, "expires": time.time() + 20}
                    return {"reply": intent.confirm, "details": {"action": "confirm", "for": intent.name}}
                return self._run_intent(intent)

            raw = self.query_llama_server(text)
            if raw:
                clean, executed = self.parse_and_execute_actions(raw)
                self._history += [{"role": "user", "content": text}, {"role": "assistant", "content": clean}]
                self._history = self._history[-12:]
                payload: Dict[str, Any] = {"reply": clean or "Done.",
                                           "details": {"action": "llm", "executed_actions": executed}}
                for ex in executed:
                    if ex["action"] == "photo" and ex["result"].get("url"):
                        payload["photo_url"] = ex["result"]["url"]
                return payload

            return {"reply": "Sorry, I didn't understand. Try battery, torch on, take photo, or call someone.",
                    "details": {"action": "fallback"}}
