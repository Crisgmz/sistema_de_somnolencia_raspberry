"""Test de integracion sintetico: score, reglas y cola MQTT."""

from __future__ import annotations

import time

from core.eventstore import EventStore
from engine.fatiguescore import DynamicFatigueScore
from engine.ruleengine import RuleEngine


def run_synthetic_test() -> None:
    store = EventStore()
    score = DynamicFatigueScore()
    engine = RuleEngine(store)
    engine.start()

    ts = time.time()
    for i in range(30):
        t = ts + i
        perclos_event = {"timestamp": t, "paramid": "PERCLOS", "eventflag": i % 2 == 0, "fatiguescoredelta": 6, "value": 0.3}
        tc_event = {"timestamp": t, "paramid": "BLINK_TC", "eventflag": i % 5 == 0, "fatiguescoredelta": 5, "value": 600}
        store.append(perclos_event)
        store.append(tc_event)
        out = score.update(t, [perclos_event, tc_event], forced_min_level=engine.latest().get("forced_min_level", 0))

    time.sleep(1.2)
    rules = engine.latest()
    engine.stop()

    print("Score final:", out)
    print("Reglas:", rules)
    assert out["fatigue_score"] > 0
    assert out["level"] >= 1


if __name__ == "__main__":
    run_synthetic_test()
