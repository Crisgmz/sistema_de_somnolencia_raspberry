"""Dynamic Fatigue Score (0-100) con niveles 0..4."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List


@dataclass
class DynamicFatigueScore:
    score: int = 0
    max_score_seen: int = 0
    alert_count: int = 0
    last_event_ts: float = 0.0
    reasons: List[str] = field(default_factory=list)

    def _level(self) -> int:
        if self.score >= 80:
            return 4
        if self.score >= 60:
            return 3
        if self.score >= 40:
            return 2
        if self.score >= 20:
            return 1
        return 0

    @staticmethod
    def level_label(level: int) -> str:
        return ["NORMAL", "FATIGA", "SOMNOLENCIA", "CRITICO", "EMERGENCIA"][max(0, min(4, level))]

    def update(
        self,
        ts: float,
        param_outputs: Iterable[Dict],
        vehicle_moving: bool = True,
        driver_response: bool = False,
        forced_min_level: int = 0,
        forced_reasons: List[str] | None = None,
    ) -> Dict:
        deltas = 0
        local_reasons: List[str] = []
        had_event = False
        for out in param_outputs:
            if bool(out.get("eventflag", False)):
                had_event = True
                deltas += int(out.get("fatiguescoredelta", 0))
                local_reasons.append(str(out.get("paramid", "UNKNOWN")))

        if had_event:
            self.last_event_ts = float(ts)
            self.reasons = local_reasons
            self.alert_count += 1
        else:
            if ts - self.last_event_ts >= 60.0:
                deltas -= 2
            if not vehicle_moving:
                deltas -= 2
            if driver_response:
                deltas -= 3

        self.score = max(0, min(100, self.score + deltas))
        self.max_score_seen = max(self.max_score_seen, self.score)
        level = max(self._level(), int(forced_min_level))
        if forced_reasons:
            self.reasons = list(forced_reasons)

        return {
            "fatigue_score": self.score,
            "level": level,
            "label": self.level_label(level),
            "reasons": list(self.reasons),
            "max_fatigue": self.max_score_seen,
            "alert_count": self.alert_count,
        }


if __name__ == "__main__":
    dfs = DynamicFatigueScore()
    for i in range(10):
        sample = [{"paramid": "PERCLOS", "eventflag": i % 2 == 0, "fatiguescoredelta": 5}]
        print(dfs.update(i, sample))
