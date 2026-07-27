"""Despacho central de alertas a buzzer y cola MQTT.

Incluye histeresis para evitar oscilacion rapida entre niveles
y cooldown para reducir publicaciones MQTT redundantes.
"""

from __future__ import annotations

import os
import time
from typing import Dict, List

from output.buzzer import Buzzer
from output.mqttpublisher import MqttPublisher


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class AlertDispatcher:
    # Tiempo minimo (segundos) que un nivel debe mantenerse antes de poder bajar.
    LEVEL_HOLD_S = 3.0
    SOUND_DELAY_S = 2.0
    # Intervalo minimo entre publicaciones MQTT para el mismo nivel+reasons.
    MQTT_DEDUP_S = 2.0

    SUPERVISOR_COOLDOWN_S = 30.0  # minimo entre notificaciones al supervisor

    # Auto-silencio del buzzer: si el conductor lleva este tiempo SIN senales
    # reales de fatiga (despierto, mirando bien), se calla el buzzer aunque la
    # histeresis del score siga elevada. Evita el pitido molesto persistente tras
    # recuperarse. La emergencia medica NUNCA se silencia. Configurable por entorno.
    BUZZER_QUIET_AFTER_CLEAR_S = _env_f("SOMNO_BUZZER_QUIET_S", 30.0)

    def __init__(self, buzzer: Buzzer, mqtt: MqttPublisher) -> None:
        self.buzzer = buzzer
        self.mqtt = mqtt
        self._effective_level = 0
        self._level_since_ts = 0.0
        self._last_mqtt_signature: tuple = ()
        self._last_mqtt_ts = 0.0
        self._suppressed_count = 0
        self._last_supervisor_ts = 0.0
        self._last_supervisor_sig: tuple = ()
        self._emergency_active = False
        self._sound_candidate_level = 0
        self._sound_candidate_since_ts = 0.0
        # Instante desde el que el conductor esta "despejado" (sin fatiga real).
        self._clear_since_ts = 0.0

    def _apply_hysteresis(self, raw_level: int, emergency: bool) -> int:
        now = time.monotonic()
        if emergency:
            self._emergency_active = True
            self._effective_level = 4
            self._level_since_ts = now
            return 4

        if self._emergency_active:
            self._emergency_active = False
            self._effective_level = raw_level
            self._level_since_ts = now
            return self._effective_level

        if raw_level >= self._effective_level:
            self._effective_level = raw_level
            self._level_since_ts = now
        elif (now - self._level_since_ts) >= self.LEVEL_HOLD_S:
            self._effective_level = raw_level
            self._level_since_ts = now

        return self._effective_level

    def _delayed_buzzer_level(self, level: int, emergency: bool) -> int:
        level = max(0, min(4, int(level)))
        if level <= 0:
            self._sound_candidate_level = 0
            self._sound_candidate_since_ts = 0.0
            return 0
        if emergency:
            return level

        now = time.monotonic()
        if level != self._sound_candidate_level:
            self._sound_candidate_level = level
            self._sound_candidate_since_ts = now
            return 0
        if (now - self._sound_candidate_since_ts) < self.SOUND_DELAY_S:
            return 0
        return level

    def _quiet_when_recovered(self, buzzer_level: int, driver_clear: bool, emergency: bool) -> int:
        """Calla el buzzer si el conductor lleva >= BUZZER_QUIET_AFTER_CLEAR_S sin
        senales reales de fatiga. La emergencia medica nunca se silencia."""
        if emergency:
            self._clear_since_ts = 0.0
            return buzzer_level
        now = time.monotonic()
        if driver_clear:
            if self._clear_since_ts == 0.0:
                self._clear_since_ts = now
            if (now - self._clear_since_ts) >= self.BUZZER_QUIET_AFTER_CLEAR_S:
                return 0
        else:
            self._clear_since_ts = 0.0
        return buzzer_level

    def _should_publish_mqtt(self, level: int, reasons: List[str], emergency: bool) -> bool:
        if emergency:
            return True
        now = time.monotonic()
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
        driver_clear: bool = False,
        face_present: bool = True,
    ) -> Dict:
        out_level = self._apply_hysteresis(level, emergency)
        buzzer_level = self._delayed_buzzer_level(out_level, emergency)
        buzzer_level = self._quiet_when_recovered(buzzer_level, driver_clear, emergency)
        # SIN ROSTRO NO SUENA: si no hay cara detectada, el buzzer queda en
        # silencio pase lo que pase (arranque sin nadie, conductor fuera de cuadro).
        # La telemetria/MQTT sí puede seguir reportando; solo se silencia el sonido.
        if not face_present:
            buzzer_level = 0
        self.buzzer.set_level(buzzer_level)
        self.buzzer.set_continuous(bool(fixed_buzzer) and buzzer_level > 0 and face_present)
        self.mqtt.set_level(out_level)

        enriched = dict(payload)
        enriched.setdefault("alerts", {})
        enriched["alerts"].update({"active": out_level > 0, "level": out_level, "reasons": reasons})
        enriched.setdefault("emergency", {})
        enriched["emergency"].update({"active": bool(emergency), "type": emergency_type})

        if self._should_publish_mqtt(out_level, reasons, emergency):
            immediate = bool(emergency) or out_level >= 2
            self.mqtt.enqueue({"kind": "immediate" if immediate else "telemetry", "payload": enriched})

        notify_level = 4 if emergency else int(level)
        if self._should_notify_supervisor(notify_level, emergency, reasons):
            self.mqtt.enqueue_supervisor({
                "vehicle_id": enriched.get("v"),
                "driver_id": enriched.get("d"),
                "ts": enriched.get("ts"),
                "session_id": enriched.get("session_id"),
                "level": notify_level,
                "emergency": bool(emergency),
                "emergency_type": emergency_type,
                "reasons": reasons,
            })

        return enriched

    def _should_notify_supervisor(self, level: int, emergency: bool, reasons: list) -> bool:
        if not (bool(emergency) or level >= 3):
            return False
        now = time.monotonic()
        sig = (level, bool(emergency))
        if sig == self._last_supervisor_sig and (now - self._last_supervisor_ts) < self.SUPERVISOR_COOLDOWN_S:
            return False
        self._last_supervisor_sig = sig
        self._last_supervisor_ts = now
        return True

    def stats(self) -> Dict:
        return {
            "effective_level": self._effective_level,
            "suppressed_mqtt": self._suppressed_count,
        }
