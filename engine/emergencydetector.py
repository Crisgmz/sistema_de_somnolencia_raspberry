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
# El umbral se lee dentro de detect_emergency (no a nivel de modulo) para que
# respete el .env, que se carga DESPUES de importar este modulo.
HEAD_DOWN_FIXED_BUZZER_S = 6.0


def detect_emergency(metrics: Dict) -> Dict:
    pitch_delta = float(metrics.get("pitch_delta", metrics.get("pitch", 0.0)))
    # En este pipeline, pitch negativo representa inclinacion hacia abajo.
    # Umbrales/activacion leidos por llamada para respetar el .env (cargado tras
    # importar este modulo). El trigger de cabeza-abajo depende de la pose de
    # solvePnP, INESTABLE cerca de rostro frontal (pitch ~180): saltaba a valores
    # negativos y disparaba PROLONGED_HEAD_DOWN en falso con la cabeza recta. Se
    # puede desactivar (SOMNO_HEAD_DOWN_EMERGENCY=0) hasta tener correccion de pose.
    head_down_deg = _env_f("SOMNO_HEAD_DOWN_DEG", HEAD_DOWN_THRESHOLD_DEG)
    head_down_s_thr = _env_f("SOMNO_HEAD_DOWN_S", HEAD_DOWN_FIXED_BUZZER_S)
    head_down_enabled = os.getenv("SOMNO_HEAD_DOWN_EMERGENCY", "1").strip().lower() in ("1", "true", "yes", "on")
    head_down = pitch_delta <= -head_down_deg
    eye_closed_ms = float(metrics.get("eye_closed_ms", metrics.get("blink_tc_ms", 0.0)))
    head_down_s = float(metrics.get("head_down_s", 0.0))

    eye_closed_emergency_ms = _env_f("SOMNO_EYE_CLOSED_EMERGENCY_MS", 2000.0)
    reasons: List[str] = []
    fixed_buzzer = False
    if eye_closed_ms >= eye_closed_emergency_ms:
        reasons.append("LOSS_OF_CONSCIOUSNESS")
        fixed_buzzer = True
    if head_down_enabled and head_down and head_down_s >= head_down_s_thr:
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
