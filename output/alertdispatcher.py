"""Despacho central de alertas a buzzer y cola MQTT."""

from __future__ import annotations

from typing import Dict, List

from output.buzzer import Buzzer
from output.mqttpublisher import MqttPublisher


class AlertDispatcher:
    def __init__(self, buzzer: Buzzer, mqtt: MqttPublisher) -> None:
        self.buzzer = buzzer
        self.mqtt = mqtt

    def dispatch(self, level: int, reasons: List[str], payload: Dict, emergency: bool = False, emergency_type: str | None = None) -> None:
        out_level = 4 if emergency else level
        self.buzzer.set_level(out_level)
        self.mqtt.set_level(out_level)
        enriched = dict(payload)
        enriched.setdefault("alerts", {})
        enriched["alerts"].update({"active": out_level > 0, "level": out_level, "reasons": reasons})
        enriched.setdefault("emergency", {})
        enriched["emergency"].update({"active": bool(emergency), "type": emergency_type})
        self.mqtt.enqueue({"kind": "immediate" if emergency else "telemetry", "payload": enriched})
