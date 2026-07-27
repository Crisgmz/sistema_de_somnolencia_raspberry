"""Pipeline independiente de emergencias (no altera el fatigue score).

Solo DOS condiciones disparan emergencia real (buzzer fijo, nivel 4,
notificacion inmediata), porque son las unicas medibles con fiabilidad por la
camara:

- LOSS_OF_CONSCIOUSNESS: ojos cerrados de forma continua >= 2 s.
- PROLONGED_HEAD_DOWN: cabeza caida sostenida >= 6 s.

Los patrones medicos especulativos (ictus por asimetria, colapso lateral,
disociacion, rostro fuera de cuadro) se infieren de las senales MAS ruidosas
del sistema; una emergencia falsa es el peor fallo posible. Quedan degradados
a "suspicions": viajan en la telemetria para que el panel los muestre, pero
NO suenan ni fuerzan nivel.
"""

from __future__ import annotations

import os
from typing import Dict, List

HEAD_DOWN_THRESHOLD_DEG = 24.0


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Ojos cerrados de forma continua >= este umbral => emergencia (buzzer fijo).
EYE_CLOSED_EMERGENCY_MS = _env_f("SOMNO_EYE_CLOSED_EMERGENCY_MS", 2000.0)
HEAD_DOWN_FIXED_BUZZER_S = 6.0


def detect_emergency(metrics: Dict) -> Dict:
    pitch_delta = float(metrics.get("pitch_delta", metrics.get("pitch", 0.0)))
    # En este pipeline, pitch negativo representa inclinacion hacia abajo.
    head_down = pitch_delta <= -HEAD_DOWN_THRESHOLD_DEG
    eye_closed_ms = float(metrics.get("eye_closed_ms", metrics.get("blink_tc_ms", 0.0)))
    head_down_s = float(metrics.get("head_down_s", 0.0))

    reasons: List[str] = []
    fixed_buzzer = False
    if eye_closed_ms >= EYE_CLOSED_EMERGENCY_MS:
        reasons.append("LOSS_OF_CONSCIOUSNESS")
        fixed_buzzer = True
    if head_down and head_down_s >= HEAD_DOWN_FIXED_BUZZER_S:
        reasons.append("PROLONGED_HEAD_DOWN")
        fixed_buzzer = True

    # Sospechas informativas (telemetria/panel): nunca suenan ni fuerzan nivel.
    suspicions: List[str] = []
    stroke_thr = float(metrics.get("asymmetry_thr", 0.09))
    if float(metrics.get("facial_asymmetry", 0.0)) >= stroke_thr:
        suspicions.append("STROKE_PATTERN")
    if abs(float(metrics.get("roll", 0.0))) >= 45.0 and abs(float(metrics.get("yaw", 0.0))) >= 30.0:
        suspicions.append("LATERAL_COLLAPSE")
    if float(metrics.get("fixation", 0.0)) >= 15.0 and float(metrics.get("blink_fb", 20.0)) <= 4.0:
        suspicions.append("DISSOCIATION")
    if bool(metrics.get("face_out", False)) and not bool(metrics.get("yaw_justified", False)):
        suspicions.append("FACE_OUT_OF_FRAME")

    emergency = len(reasons) > 0
    return {
        "emergencyflag": emergency,
        "emergencytype": reasons[0] if emergency else None,
        "reasons": reasons,
        "fixedbuzzer": fixed_buzzer,
        "suspicions": suspicions,
    }


if __name__ == "__main__":
    print(detect_emergency({"eye_closed_ms": 2800, "pitch_delta": 0}))
    print(detect_emergency({"eye_closed_ms": 0, "pitch_delta": -30, "head_down_s": 7}))
    print(detect_emergency({"facial_asymmetry": 0.3, "pitch_delta": 0}))
