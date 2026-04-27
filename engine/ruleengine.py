"""Rule Engine en hilo separado para ventanas 5/30/60 min."""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

from core.eventstore import EventStore


class RuleEngine(threading.Thread):
    def __init__(self, event_store: EventStore, interval_s: float = 1.0) -> None:
        super().__init__(daemon=True)
        self.event_store = event_store
        self.interval_s = float(interval_s)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest: Dict = {"forced_min_level": 0, "reasons": []}
        self._uninterrupted_start_ts: Optional[float] = None

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=1.5)

    def latest(self) -> Dict:
        with self._lock:
            return dict(self._latest)

    @staticmethod
    def _count(events: List[Dict], paramid: str) -> int:
        return sum(1 for e in events if e.get("paramid") == paramid and e.get("eventflag"))

    def _evaluate(self, now_ts: float) -> Dict:
        win_5 = self.event_store.window(now_ts, 5 * 60)
        win_30 = self.event_store.window(now_ts, 30 * 60)
        win_60 = self.event_store.window(now_ts, 60 * 60)
        forced_level = 0
        reasons: List[str] = []

        ear_2s = any(
            e.get("paramid") == "BLINK_TC" and float(e.get("value", 0.0)) >= 2000.0 and bool(e.get("eventflag"))
            for e in win_5
        )
        if ear_2s:
            forced_level = max(forced_level, 4)
            reasons.append("EAR_2S_RULE")

        if self._count(win_30, "BLINK_TC") >= 6 and self._count(win_30, "PERCLOS") >= 4:
            forced_level = max(forced_level, 3)
            reasons.append("PERCLOS_TC_CROSS")

        if self._count(win_5, "PITCH") >= 3 or self._count(win_5, "HEAD_DROP_VELOCITY") >= 3:
            forced_level = max(forced_level, 2)
            reasons.append("HEAD_NOD_CLUSTER")

        if self._count(win_30, "BLINK_FB") >= 6:
            forced_level = max(forced_level, 1)
            reasons.append("YAWN_OR_BLINK_CLUSTER")

        if self._count(win_60, "MONOTONY") >= 5 and self._count(win_60, "TIME_ON_TASK") >= 5:
            if self._uninterrupted_start_ts is None:
                self._uninterrupted_start_ts = now_ts
            forced_level = max(forced_level, 2)
            reasons.append("LONG_TASK_MONOTONY")

        # Conduccion ininterrumpida: una vez activada, persiste mientras TIME_ON_TASK siga ocurriendo
        if self._uninterrupted_start_ts is not None:
            win_5_local = self.event_store.window(now_ts, 5 * 60)
            if self._count(win_5_local, "TIME_ON_TASK") >= 1:
                forced_level = max(forced_level, 2)
                if "LONG_TASK_MONOTONY" not in reasons:
                    reasons.append("UNINTERRUPTED_DRIVING")
            else:
                self._uninterrupted_start_ts = None

        return {"forced_min_level": forced_level, "reasons": reasons}

    def run(self) -> None:
        while not self._stop_event.is_set():
            now_ts = time.time()
            data = self._evaluate(now_ts)
            with self._lock:
                self._latest = data
            self._stop_event.wait(self.interval_s)


if __name__ == "__main__":
    es = EventStore()
    t = time.time()
    es.append({"timestamp": t, "paramid": "BLINK_TC", "eventflag": True, "value": 2200})
    engine = RuleEngine(es)
    engine.start()
    time.sleep(1.2)
    print(engine.latest())
    engine.stop()
