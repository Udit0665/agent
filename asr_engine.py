"""Speech-to-text backends: Chariot cloud ASR, or Android's recognizer."""

import json
import os
import urllib.request
import uuid
from typing import Optional

import config
from device import DeviceController


class ChariotASR:
    name = "chariot"

    def transcribe(self, audio_filepath: str) -> Optional[str]:
        if not os.path.exists(audio_filepath):
            return None
        try:
            with open(audio_filepath, "rb") as f:
                data = f.read()
        except OSError as e:
            print(f"[ASR] read error: {e}")
            return None
        if len(data) < 1000:
            return None

        boundary = "----pocketjarvis" + uuid.uuid4().hex
        filename = os.path.basename(audio_filepath)
        ctype = "audio/wav" if filename.endswith(".wav") else "audio/mp4"
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="input_audio_file"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {ctype}\r\n\r\n".encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        req = urllib.request.Request(
            f"{config.CHARIOT_BASE_URL}/v1/asr/transcribe", data=body,
            headers={"chariotai-api-key": config.CHARIOT_API_KEY,
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200:
                    text = json.loads(resp.read().decode()).get("text", "").strip()
                    return text or None
        except Exception as e:  # noqa: BLE001
            print(f"[ASR] transcription error: {e}")
        return None


class AndroidASR:
    """termux-speech-to-text: records and recognizes by itself (no ffmpeg needed)."""
    name = "android"

    def listen(self) -> Optional[str]:
        return DeviceController.speech_to_text() or None


def make_stt():
    return ChariotASR() if config.STT_BACKEND == "chariot" else AndroidASR()
