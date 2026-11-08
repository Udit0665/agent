"""Text-to-speech: Chariot HD voice if a key is set, else Android's offline TTS.

Everything plays on the phone speaker, so it keeps working with the browser closed.
"""

import glob
import hashlib
import json
import os
import re
import threading
import time
import urllib.request
from typing import Any, Dict, Optional

import config
from device import DeviceController, run_termux_cmd
from hub import hub


class VoiceEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._down_until = 0.0  # circuit breaker after a Chariot failure

    @property
    def speaking(self) -> bool:
        return hub.speaking

    @property
    def engine_name(self) -> str:
        return "chariot" if config.CHARIOT_API_KEY else "android"

    @staticmethod
    def clean_text_for_speech(text: str) -> str:
        text = re.sub(r"\[ACTION:[^\]]*\]", "", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[*_~`#]", "", text)
        return re.sub(r"\s+", " ", text).strip()

    def synthesize(self, text: str) -> Optional[Dict[str, Any]]:
        """Return {'filepath': ...} for the spoken text, or None if Chariot is unavailable."""
        if not config.CHARIOT_API_KEY or time.time() < self._down_until:
            return None
        text = self.clean_text_for_speech(text)
        if not text:
            return None
        if len(text) > 800:
            text = text[:800] + "..."

        key = hashlib.md5(f"{config.CHARIOT_VOICE_ID}_{text}".encode()).hexdigest()[:12]
        for cached in glob.glob(os.path.join(config.CAPTURES_DIR, f"chariot_{key}.*")):
            if os.path.getsize(cached) > 1024:
                return {"filepath": cached, "cached": True}

        payload = {"voice_id": config.CHARIOT_VOICE_ID, "text": text, "model_type": "chariot-multilingual-v1"}
        req = urllib.request.Request(
            f"{config.CHARIOT_BASE_URL}/v1/tts", data=json.dumps(payload).encode(),
            headers={"chariotai-api-key": config.CHARIOT_API_KEY, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                audio = resp.read()
        except Exception as e:  # noqa: BLE001
            print(f"[voice] Chariot TTS error: {e}")
            self._down_until = time.time() + 60
            return None
        if len(audio) < 1024:
            return None

        ext = "wav" if audio[:4] == b"RIFF" else "mp3" if audio[:3] == b"ID3" or audio[:1] == b"\xff" else "wav"
        path = os.path.join(config.CAPTURES_DIR, f"chariot_{key}.{ext}")
        with open(path, "wb") as f:
            f.write(audio)
        self._prune_cache()
        return {"filepath": path, "cached": False}

    @staticmethod
    def _prune_cache(keep: int = 80) -> None:
        files = sorted(glob.glob(os.path.join(config.CAPTURES_DIR, "chariot_*")), key=os.path.getmtime)
        for old in files[:-keep]:
            try:
                os.remove(old)
            except OSError:
                pass

    @staticmethod
    def play_on_device(filepath: str, max_wait: float = 60.0) -> bool:
        """Play on the phone speaker and wait until it finishes."""
        run_termux_cmd(["termux-media-player", "stop"])
        res = run_termux_cmd(["termux-media-player", "play", filepath])
        if res.returncode != 0:
            return False
        time.sleep(0.4)
        start = time.time()
        while time.time() - start < max_wait:
            info = run_termux_cmd(["termux-media-player", "info"], timeout=5).stdout.lower()
            if "playing" not in info:
                break
            time.sleep(0.25)
        return True

    def speak(self, text: str, block: bool = True) -> None:
        if not block:
            threading.Thread(target=self.speak, args=(text, True), daemon=True).start()
            return
        text = self.clean_text_for_speech(text)
        if not text:
            return
        with self._lock:
            hub.speaking = True
            try:
                info = self.synthesize(text)
                if info and self.play_on_device(info["filepath"]):
                    return
                DeviceController.speak_blocking(text)
            finally:
                time.sleep(0.3)  # let the room go quiet before the mic restarts
                hub.speaking = False


chariot_voice = VoiceEngine()
