"""Background voice loop: mic -> speech detection -> STT -> wake word -> brain -> voice.

Runs in a thread inside server.py, so it keeps working when the browser page is closed.
Mic capture uses termux-microphone-record in short chunks (needs ffmpeg).
"""

import array
import collections
import difflib
import math
import os
import re
import shutil
import sys
import threading
import time
import wave
from typing import List, Optional, Tuple

import config
from asr_engine import make_stt
from device import run_termux_cmd
from hub import hub


def frame_levels(wav_path: str, frame_ms: int = 100) -> List[float]:
    """Loudness (dBFS) of each frame of a 16-bit PCM wav."""
    with wave.open(wav_path, "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        return []
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    step = max(1, int(rate * frame_ms / 1000)) * ch
    levels = []
    for i in range(0, len(samples) - step + 1, step):
        chunk = samples[i:i + step]
        rms = math.sqrt(sum(x * x for x in chunk) / len(chunk))
        levels.append(20 * math.log10(max(rms, 1.0) / 32768))
    return levels


def concat_wavs(paths: List[str], out_path: str) -> None:
    with wave.open(out_path, "wb") as out:
        for i, p in enumerate(paths):
            with wave.open(p, "rb") as w:
                if i == 0:
                    out.setparams(w.getparams())
                out.writeframes(w.readframes(w.getnframes()))


class Listener(threading.Thread):
    def __init__(self, brain, voice) -> None:
        super().__init__(daemon=True, name="listener")
        self.brain = brain
        self.voice = voice
        self.stt = make_stt()
        self.threshold = config.SPEECH_DB
        self._silent_peaks: collections.deque = collections.deque(maxlen=12)
        self._seq = 0

    # ------------------------------------------------------------ recording
    def _ready(self) -> Optional[str]:
        if not shutil.which("termux-microphone-record") and self.stt.name == "chariot":
            return "termux-microphone-record not found. Run: pkg install termux-api"
        if self.stt.name == "chariot" and not shutil.which("ffmpeg"):
            return "ffmpeg not found. Run: pkg install ffmpeg"
        return None

    def record_chunk(self, secs: int) -> Optional[str]:
        self._seq += 1
        raw = os.path.join(config.TMP_DIR, "chunk.m4a")
        wav = os.path.join(config.TMP_DIR, f"chunk_{self._seq % 8}.wav")
        for p in (raw, wav):
            if os.path.exists(p):
                os.remove(p)
        run_termux_cmd(["termux-microphone-record", "-q"], timeout=5)
        r = run_termux_cmd(["termux-microphone-record", "-f", raw, "-l", str(secs),
                            "-r", "16000", "-c", "1", "-e", "aac"], timeout=10)
        if r.returncode != 0:
            hub.add("system", f"Microphone error: {r.stderr.strip() or 'record failed'}")
            return None
        time.sleep(secs + 0.3)
        for _ in range(12):
            info = run_termux_cmd(["termux-microphone-record", "-i"], timeout=5).stdout
            if '"isrecording":true' not in re.sub(r"\s+", "", info.lower()):
                break
            time.sleep(0.2)
        c = run_termux_cmd(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-ar", "16000", "-ac", "1",
                            "-c:a", "pcm_s16le", wav], timeout=20)
        if c.returncode != 0 or not os.path.exists(wav):
            return None
        return wav

    def calibrate(self) -> None:
        hub.set_state("thinking", "Calibrating mic")
        wav = self.record_chunk(config.CHUNK_SECS)
        if wav:
            levels = sorted(frame_levels(wav))
            if levels:
                ambient = levels[int(len(levels) * 0.9) - 1 if len(levels) > 1 else 0]
                self.threshold = max(-48.0, min(-28.0, ambient + 10))
                hub.add("system", f"Mic calibrated (speech threshold {self.threshold:.0f} dB)")
        hub.set_state("idle")

    def _adapt(self, peak: float) -> None:
        self._silent_peaks.append(peak)
        if len(self._silent_peaks) >= 4:
            avg = sum(self._silent_peaks) / len(self._silent_peaks)
            self.threshold = max(-48.0, min(-28.0, avg + 8))

    def capture_utterance(self) -> Optional[str]:
        parts: List[str] = []
        for _ in range(config.MAX_CHUNKS):
            if hub.muted or self.voice.speaking:
                return None
            wav = self.record_chunk(config.CHUNK_SECS)
            if self.voice.speaking:       # reply started while recording: discard
                return None
            if not wav:
                time.sleep(1)
                break
            levels = frame_levels(wav)
            loud = [lv > self.threshold for lv in levels]
            if sum(loud) >= 2:
                hub.set_state("hearing")
                kept = os.path.join(config.TMP_DIR, f"utt_{len(parts)}.wav")
                os.replace(wav, kept)
                parts.append(kept)
                if not any(loud[-6:]):    # quiet for the last 0.6 s -> sentence finished
                    break
            else:
                if levels:
                    self._adapt(max(levels))
                if parts:
                    break
                return None
        if not parts:
            return None
        out = os.path.join(config.TMP_DIR, "utterance.wav")
        concat_wavs(parts, out)
        return out

    # ------------------------------------------------------------ wake word
    def split_wake(self, text: str) -> Tuple[bool, str]:
        words = list(re.finditer(r"[\wऀ-ॿ]+", text))
        for m in words[:3]:
            w = m.group(0).lower()
            if w in config.WAKE_WORDS or difflib.get_close_matches(w, config.WAKE_WORDS, n=1, cutoff=0.8):
                return True, text[m.end():].strip(" ,.!?:;-")
        return False, text.strip()

    def handle_text(self, text: str) -> None:
        now = time.time()
        woke, rest = self.split_wake(text)
        if woke:
            hub.session_until = now + config.SESSION_SECS
            if not rest:
                hub.add("bot", "Yes boss?")
                self.voice.speak("Yes boss?")
                hub.session_until = time.time() + config.SESSION_SECS
                return
            command = rest
        elif hub.session_active or self.brain.has_pending() or not config.WAKE_WORD_REQUIRED:
            command = text
        else:
            hub.add("heard", text)  # shown dimmed in the UI: heard, but no wake word
            return

        hub.add("user", command, source="voice")
        hub.set_state("thinking")
        res = self.brain.process_instruction(command, source="voice")
        hub.set_state("idle")
        hub.add("bot", res["reply"], photo_url=res.get("photo_url"))
        self.voice.speak(res["reply"])
        if not res.get("end_session"):
            hub.session_until = time.time() + config.SESSION_SECS

    # ------------------------------------------------------------- main loop
    def listen_once(self) -> Optional[str]:
        if self.stt.name == "android":
            hub.set_state("idle")
            return self.stt.listen()
        wav = self.capture_utterance()
        if not wav:
            hub.set_state("idle")
            return None
        hub.set_state("thinking")
        text = self.stt.transcribe(wav)
        hub.set_state("idle")
        return text

    def run(self) -> None:
        problem = self._ready()
        if problem:
            hub.set_state("error", problem)
            hub.add("system", problem)
            return
        hub.add("system", f"Voice engine ready (STT: {self.stt.name}). Say '{config.WAKE_WORDS[0]}' then your command.")
        if self.stt.name == "chariot" and config.CALIBRATE and not hub.muted:
            self.calibrate()
        if config.GREET and not hub.muted:
            self.voice.speak("Jarvis online.")
        while True:
            try:
                if hub.muted or self.voice.speaking:
                    time.sleep(0.3)
                    continue
                text = self.listen_once()
                if text:
                    self.handle_text(text)
            except Exception as e:  # noqa: BLE001
                hub.add("system", f"Listener error: {e}")
                hub.set_state("idle")
                time.sleep(2)
