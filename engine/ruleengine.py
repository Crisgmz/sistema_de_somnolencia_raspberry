"""Rule Engine en hilo separado: reglas de ventana sobre las 4 senales con peso.

Tras la poda (docs/PLAN_TRABAJO_PRECISION.md) solo generan eventos:
EYE_CLOSED_MS (microsueno), PERCLOS, MAR (bostezo sostenido) y PITCH (cabeceo).
Las reglas de contexto (monotonia/tiempo en tarea) se eliminaron: forzaban
nivel 2 sin ninguna evidencia fisiologica del conductor.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, List

from core.eventstore import EventStore


class RuleEngine(threading.Thread):
    def __init__(self, event_store: EventStore, interval_s: float = 1.0) -> None:
        super().__init__(daemon=True)
        self.event_store = event_store
        self.interval_s = float(interval_s)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest: Dict = {"forced_min_level": 0, "reasons": []}

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
        # Las reglas de ventana FUERZAN un nivel minimo por el historial de 5-30
        # min (p.ej. 3 bostezos -> FATIGA por 30 min), desacoplado del score
        # actual. Util para fatiga acumulada, pero en un demo el nivel debe seguir
        # al estado ACTUAL (score 0 -> NORMAL). Se puede desactivar por entorno.
        if os.getenv("SOMNO_RULES_ENABLED", "1").strip().lower() not in ("1", "true", "yes", "on"):
            return {"forced_min_level": 0, "reasons": []}
        win_5 = self.event_store.window(now_ts, 5 * 60)
        win_30 = self.event_store.window(now_ts, 30 * 60)
        forced_level = 0
        reasons: List[str] = []

        # Cierre ocular >=2 s reciente: emergencia (ademas del pipeline medico).
        long_closure = any(
            e.get("paramid") == "EYE_CLOSED_MS" and float(e.get("value", 0.0)) >= 2000.0 and bool(e.get("eventflag"))
            for e in win_5
        )
        if long_closure:
            forced_level = max(forced_level, 4)
            reasons.append("EYE_CLOSED_2S_RULE")

        # Fatiga acumulada cruzada: PERCLOS recurrente + microsuenos en 30 min.
        if self._count(win_30, "PERCLOS") >= 4 and self._count(win_30, "EYE_CLOSED_MS") >= 3:
            forced_level = max(forced_level, 3)
            reasons.append("PERCLOS_MICROSLEEP_CROSS")

        # Racha de cabeceos en 5 min.
        if self._count(win_5, "PITCH") >= 3:
            forced_level = max(forced_level, 2)
            reasons.append("HEAD_NOD_CLUSTER")

        # Racha de bostezos en 30 min.
        if self._count(win_30, "MAR") >= 3:
            forced_level = max(forced_level, 1)
            reasons.append("YAWN_CLUSTER")

        return {"forced_min_level": forced_level, "reasons": reasons}

    def run(self) -> None:
        while not self._stop_event.is_set():
            now_ts = time.monotonic()
            data = self._evaluate(now_ts)
            with self._lock:
                self._latest = data
            self._stop_event.wait(self.interval_s)


if __name__ == "__main__":
    es = EventStore()
    t = time.monotonic()
    es.append({"timestamp": t, "paramid": "EYE_CLOSED_MS", "eventflag": True, "value": 2200})
    engine = RuleEngine(es)
    engine.start()
    time.sleep(1.2)
    print(engine.latest())
    engine.stop()
