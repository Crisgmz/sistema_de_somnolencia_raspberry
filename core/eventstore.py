"""Event store en RAM con deque y ventanas temporales."""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Iterable, List


class EventStore:
    def __init__(self, maxlen: int = 20000) -> None:
        self._events: Deque[Dict] = deque(maxlen=maxlen)

    def append(self, event: Dict) -> None:
        self._events.append(event)

    def extend(self, events: Iterable[Dict]) -> None:
        self._events.extend(events)

    def window(self, now_ts: float, window_seconds: float) -> List[Dict]:
        start_ts = float(now_ts) - float(window_seconds)
        return [e for e in self._events if float(e.get("timestamp", 0.0)) >= start_ts]

    def all(self) -> List[Dict]:
        return list(self._events)
