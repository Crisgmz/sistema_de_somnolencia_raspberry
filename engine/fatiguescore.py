"""Dynamic Fatigue Score (0-100) con niveles 0..4.

El score SIEMPRE arranca en 0 en cada sesion: no se restaura estado de
sesiones anteriores. Restaurarlo obligaba a una maquinaria defensiva entera
(cap de restauracion, exigir evento fresco, descuento por minutos offline)
para evitar alertas fantasma al encender; la evidencia real sube el score en
segundos si el conductor de verdad esta fatigado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Dict, Iterable, List


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class DynamicFatigueScore:
    score: int = 0
    max_score_seen: int = 0
    alert_count: int = 0
    last_event_ts: float = 0.0
    reasons: List[str] = field(default_factory=list)
    active_event_params: set[str] = field(default_factory=set)
    # Perdida de rostro: periodo de gracia (congelar la evidencia) + decaimiento
    # gradual proporcional al tiempo. Quitar la cara un instante (girar la
    # cabeza, mirar el espejo) NO debe reiniciar el score ni la histeresis.
    sensor_grace_s: float = field(default_factory=lambda: _env_f("SOMNO_FACE_GRACE_S", 4.0))
    sensor_decay_per_s: float = field(default_factory=lambda: _env_f("SOMNO_FACE_DECAY_PER_S", 3.0))
    _sensor_invalid_since: float = 0.0
    _sensor_decay_base: float = -1.0
    # Recuperacion cuando el conductor deja de mostrar signos: tras un margen sin
    # eventos, el score baja de forma CONTINUA (pts/seg), independiente del FPS.
    # Histeresis de fatiga: el estado elevado se MANTIENE al menos este tiempo
    # sin eventos antes de empezar a recuperar. 5 min = una vez detectada
    # somnolencia, el conductor sigue marcado como fatigado un buen rato.
    recovery_grace_s: float = field(default_factory=lambda: _env_f("SOMNO_RECOVERY_GRACE_S", 300.0))
    recovery_per_s: float = field(default_factory=lambda: _env_f("SOMNO_RECOVERY_PER_S", 3.0))
    _recovery_base: float = -1.0
    _recovery_anchor_ts: float = 0.0
    # Ganancia global de SUBIDA: escala cuanto suma cada evento de fatiga. 1.0 =
    # normal; 0.5 = sube a la mitad de velocidad (menos "trigger-happy"). Permite
    # equilibrar la sensibilidad de subida con la de bajada (recovery_per_s).
    rise_gain: float = field(default_factory=lambda: _env_f("SOMNO_RISE_GAIN", 1.0))

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
        sensor_valid: bool = True,
    ) -> Dict:
        if not sensor_valid:
            now = float(ts)
            # Marcar el inicio de la ausencia de rostro la primera vez.
            if self._sensor_invalid_since <= 0.0:
                self._sensor_invalid_since = now
                self._sensor_decay_base = -1.0
            invalid_for = max(0.0, now - self._sensor_invalid_since)
            self.active_event_params = set()
            self.reasons = []
            # Periodo de gracia: congelar el score (no borrar la evidencia). Solo
            # tras una ausencia sostenida se decae, de forma gradual/proporcional
            # al tiempo transcurrido (independiente del FPS), no 10 pts por frame.
            # Se decae desde una base fija para no perder el decaimiento por el
            # redondeo a entero en cada frame.
            if invalid_for > self.sensor_grace_s:
                if self._sensor_decay_base < 0.0:
                    self._sensor_decay_base = float(self.score)
                decay_for = invalid_for - self.sensor_grace_s
                decayed = self._sensor_decay_base - self.sensor_decay_per_s * decay_for
                self.score = max(0, int(round(decayed)))
            return {
                "fatigue_score": self.score,
                "level": 0,
                "label": self.level_label(0),
                "reasons": [],
                "max_fatigue": self.max_score_seen,
                "alert_count": self.alert_count,
            }

        # Rostro valido: cerrar cualquier ventana de ausencia previa.
        self._sensor_invalid_since = 0.0

        deltas = 0.0
        current_event_params: set[str] = set()
        current_reasons: List[str] = []
        had_new_event = False
        for out in param_outputs:
            if not bool(out.get("eventflag", False)):
                continue
            delta = int(out.get("fatiguescoredelta", 0))
            # Solo la evidencia con PESO de fatiga (delta > 0) mantiene el score
            # elevado y reinicia el temporizador de recuperacion.
            if delta <= 0:
                continue
            param_id = str(out.get("paramid", "UNKNOWN"))
            current_event_params.add(param_id)
            current_reasons.append(param_id)
            if param_id not in self.active_event_params:
                had_new_event = True
                deltas += delta * self.rise_gain

        if current_event_params:
            self.last_event_ts = float(ts)
            self.reasons = current_reasons
            self._recovery_base = -1.0  # cancelar recuperacion: hay evidencia fresca
            if had_new_event:
                self.alert_count += 1
        else:
            if driver_response:
                deltas -= 3
            # Recuperacion continua: tras `recovery_grace_s` sin eventos, el score
            # baja `recovery_per_s` pts/seg (proporcional al tiempo, no al FPS).
            # Se decae desde una base fija para no perder fracciones por redondeo.
            if (float(ts) - self.last_event_ts) >= self.recovery_grace_s:
                if self._recovery_base < 0.0:
                    self._recovery_base = float(self.score)
                    self._recovery_anchor_ts = float(ts)
                target = self._recovery_base - self.recovery_per_s * (float(ts) - self._recovery_anchor_ts)
                deltas += min(0, int(round(target)) - self.score)
            else:
                self._recovery_base = -1.0

        self.active_event_params = current_event_params
        self.score = max(0, min(100, self.score + int(round(deltas))))
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
