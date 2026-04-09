"""Despacho central de alertas a buzzer y cola MQTT.

Incluye histeresis para evitar oscilacion rapida entre niveles
y cooldown para reducir publicaciones MQTT redundantes.
"""

from __future__ import annotations

import time
from typing import Dict, List

from output.buzzer import Buzzer
from output.mqttpublisher import MqttPublisher


class AlertDispatcher:
    # Tiempo minimo (segundos) que un nivel debe mantenerse antes de poder bajar.
    LEVEL_HOLD_S = 3.0
    # Intervalo minimo entre publicaciones MQTT para el mismo nivel+reasons.
    MQTT_DEDUP_S = 2.0

    def __init__(self, buzzer: Buzzer, mqtt: MqttPublisher) -> None:
        self.buzzer = buzzer
        self.mqtt = mqtt
        self._effective_level = 0
        self._level_since_ts = 0.0
        self._last_mqtt_signature: tuple = ()
        self._last_mqtt_ts = 0.0
        self._suppressed_count = 0

    def _apply_hysteresis(self, raw_level: int, emergency: bool) -> int:
        now = time.time()
        if emergency:
            self._effective_level = 4
            self._level_since_ts = now
            return 4

        if raw_level >= self._effective_level:
            self._effective_level = raw_level
            self._level_since_ts = now
        elif (now - self._level_since_ts) >= self.LEVEL_HOLD_S:
            self._effective_level = raw_level
            self._level_since_ts = now

        return self._effective_level

    def _should_publish_mqtt(self, level: int, reasons: List[str], emergency: bool) -> bool:
        if emergency:
            return True
        now = time.time()
        signature = (level, tuple(sorted(reasons)))
        if signature != self._last_mqtt_signature:
            self._last_mqtt_signature = signature
            self._last_mqtt_ts = now
            return True
        if (now - self._last_mqtt_ts) >= self.MQTT_DEDUP_S:
            self._last_mqtt_ts = now
            return True
        self._suppressed_count += 1
        return False

    def dispatch(
        self,
        level: int,
        reasons: List[str],
        payload: Dict,
        emergency: bool = False,
        emergency_type: str | None = None,
        fixed_buzzer: bool = False,
    ) -> Dict:
        out_level = self._apply_hysteresis(level, emergency)
        self.buzzer.set_level(out_level)
        self.buzzer.set_continuous(bool(fixed_buzzer) and out_level > 0)
        self.mqtt.set_level(out_level)

        enriched = dict(payload)
        enriched.setdefault("alerts", {})
        enriched["alerts"].update({"active": out_level > 0, "level": out_level, "reasons": reasons})
        enriched.setdefault("emergency", {})
        enriched["emergency"].update({"active": bool(emergency), "type": emergency_type})

        if self._should_publish_mqtt(out_level, reasons, emergency):
            immediate = bool(emergency) or out_level >= 2
            self.mqtt.enqueue({"kind": "immediate" if immediate else "telemetry", "payload": enriched})

        return enriched

    def stats(self) -> Dict:
        return {
            "effective_level": self._effective_level,
            "suppressed_mqtt": self._suppressed_count,
        }
