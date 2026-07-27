"""Event store en RAM para las ventanas temporales del RuleEngine.

Guarda solo las TRANSICIONES (flanco de subida de eventflag) de cada parametro:
es lo que las reglas cuentan ("N cabeceos en 5 min"), no frames. Antes se
apoyaba en SQLite con commit por evento en el hilo principal (latencia) y
persistia entre sesiones, pero las ventanas siempre se acotan al arranque de la
sesion actual, asi que esa persistencia nunca se usaba. Los eventos que si
deben sobrevivir van a Supabase (tabla `events`) por su propio camino.

Los timestamps son del reloj MONOTONICO del proceso (ver plan LTE): nunca
saltan con NTP.
"""

from __future__ import annotations

from collections import deque
import threading
import time
from typing import Deque, Dict, Iterable, List


class EventStore:
    def __init__(self, maxlen: int = 20000, retention_seconds: float = 2 * 60 * 60) -> None:
        self._events: Deque[Dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._active_params: set[str] = set()
        self.retention_seconds = float(retention_seconds)
        self.session_start_ts = time.monotonic()

    def append(self, event: Dict) -> None:
        param_id = str(event.get("paramid", ""))
        if not param_id:
            return
        is_active = bool(event.get("eventflag", False))
        with self._lock:
            if is_active and param_id not in self._active_params:
                self._active_params.add(param_id)
                self._events.append(event)
                self._prune(float(event.get("timestamp", 0.0)))
            elif not is_active:
                self._active_params.discard(param_id)

    def extend(self, events: Iterable[Dict]) -> None:
        for event in events:
            self.append(event)

    def window(self, now_ts: float, window_seconds: float) -> List[Dict]:
        start_ts = max(float(now_ts) - float(window_seconds), self.session_start_ts)
        with self._lock:
            return [e for e in self._events if float(e.get("timestamp", 0.0)) >= start_ts]

    def all(self) -> List[Dict]:
        with self._lock:
            return list(self._events)

    def _prune(self, now_ts: float) -> None:
        cutoff = float(now_ts) - self.retention_seconds
        while self._events and float(self._events[0].get("timestamp", 0.0)) < cutoff:
            self._events.popleft()

    def close(self) -> None:
        pass
