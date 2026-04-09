"""Memoria temporal de alertas para UI, MQTT y persistencia.

Mantiene contexto de corto/mediano plazo para evitar alertas "sin memoria":
- duracion del nivel actual
- historial reciente de niveles/reasons
- conteos por ventanas temporales
- transiciones relevantes (sube/baja/cambia motivo)
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple


@dataclass
class AlertSnapshot:
    ts: float
    level: int
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    emergency_type: Optional[str] = None


class AlertMemory:
    # Cooldown: una vez que el nivel baja a 0, no reportar reasons duplicadas
    # dentro de esta ventana (evita alertas "sin memoria" que repiten lo mismo).
    REASON_COOLDOWN_S = 30.0

    def __init__(self, history_seconds: float = 3600.0) -> None:
        self.history_seconds = float(history_seconds)
        self._history: Deque[AlertSnapshot] = deque()
        self._current_signature: Tuple[int, Tuple[str, ...], Optional[str]] = (0, tuple(), None)
        self._current_since: float = 0.0
        self._last_transition_ts: float = 0.0
        self._transition_count: int = 0
        self._reason_last_seen: Dict[str, float] = {}
        self._escalation_count: int = 0
        self._prev_level: int = 0

    def _prune(self, now_ts: float) -> None:
        cutoff = float(now_ts) - self.history_seconds
        while self._history and self._history[0].ts < cutoff:
            self._history.popleft()

    def update(self, ts: float, level: int, reasons: List[str] | None, emergency_type: Optional[str] = None) -> Dict:
        reasons_tuple = tuple(sorted({str(r) for r in (reasons or []) if r}))
        signature = (int(level), reasons_tuple, emergency_type)
        if self._current_since == 0.0:
            self._current_since = float(ts)
            self._last_transition_ts = float(ts)
            self._current_signature = signature
        elif signature != self._current_signature:
            old_level = self._current_signature[0]
            if int(level) > old_level:
                self._escalation_count += 1
            self._current_signature = signature
            self._current_since = float(ts)
            self._last_transition_ts = float(ts)
            self._transition_count += 1

        for r in reasons_tuple:
            self._reason_last_seen[r] = float(ts)
        self._prev_level = int(level)

        if int(level) > 0 or emergency_type:
            self._history.append(
                AlertSnapshot(ts=float(ts), level=int(level), reasons=reasons_tuple, emergency_type=emergency_type)
            )
        self._prune(ts)
        return self.snapshot(ts)

    def is_reason_in_cooldown(self, reason: str, now_ts: float) -> bool:
        last = self._reason_last_seen.get(reason)
        if last is None:
            return False
        return (now_ts - last) < self.REASON_COOLDOWN_S

    def snapshot(self, now_ts: float) -> Dict:
        self._prune(now_ts)
        counters = {"5m": Counter(), "15m": Counter(), "60m": Counter()}
        windows = {"5m": 5 * 60.0, "15m": 15 * 60.0, "60m": 60 * 60.0}
        level_peaks = {key: 0 for key in windows}
        emergency_counts = {key: 0 for key in windows}

        for item in self._history:
            age = float(now_ts) - item.ts
            for key, seconds in windows.items():
                if age <= seconds:
                    level_peaks[key] = max(level_peaks[key], item.level)
                    if item.emergency_type:
                        emergency_counts[key] += 1
                    for reason in item.reasons:
                        counters[key][reason] += 1

        top_reasons = {
            key: [{"reason": reason, "count": count} for reason, count in counter.most_common(3)]
            for key, counter in counters.items()
        }
        level, reasons, emergency_type = self._current_signature
        return {
            "active_level": int(level),
            "active_reasons": list(reasons),
            "active_emergency_type": emergency_type,
            "active_duration_s": max(0.0, float(now_ts) - self._current_since),
            "last_transition_s_ago": max(0.0, float(now_ts) - self._last_transition_ts),
            "transition_count": self._transition_count,
            "escalation_count": self._escalation_count,
            "peaks": level_peaks,
            "emergency_counts": emergency_counts,
            "top_reasons": top_reasons,
            "history_size": len(self._history),
        }
