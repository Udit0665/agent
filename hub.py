"""Shared state + event log between the voice thread, brain and web UI."""

import collections
import threading
import time
from typing import Any, Dict, List


class Hub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: collections.deque = collections.deque(maxlen=300)
        self._next_id = 1
        self.base_state = "idle"      # idle | hearing | thinking | error  (set by listener)
        self.detail = ""
        self.speaking = False          # set by voice engine
        self.muted = False             # set by UI
        self.session_until = 0.0       # wake-word session deadline (epoch seconds)

    @property
    def state(self) -> str:
        if self.muted:
            return "muted"
        if self.speaking:
            return "speaking"
        return self.base_state

    @property
    def session_active(self) -> bool:
        return time.time() < self.session_until

    def set_state(self, state: str, detail: str = "") -> None:
        self.base_state = state
        self.detail = detail

    def add(self, kind: str, text: str, **extra: Any) -> int:
        with self._lock:
            ev = {"id": self._next_id, "ts": time.time(), "kind": kind, "text": text}
            ev.update(extra)
            self._events.append(ev)
            self._next_id += 1
            return ev["id"]

    def since(self, last_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["id"] > last_id]

    def last_id(self) -> int:
        with self._lock:
            return self._next_id - 1


hub = Hub()
