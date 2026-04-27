"""Pipeline independiente de emergencias medicas (no altera fatigue score)."""

from __future__ import annotations

from typing import Dict, List

HEAD_DOWN_THRESHOLD_DEG = 24.0
EYE_CLOSED_EMERGENCY_MS = 2000.0
HEAD_DOWN_FIXED_BUZZER_S = 6.0


def detect_emergency(metrics: Dict) -> Dict:
    pitch_delta = float(metrics.get("pitch_delta", metrics.get("pitch", 0.0)))
    # En este pipeline, pitch negativo representa inclinacion hacia abajo.
    head_down = pitch_delta <= -HEAD_DOWN_THRESHOLD_DEG
    eye_closed_ms = float(metrics.get("eye_closed_ms", metrics.get("blink_tc_ms", 0.0)))
    head_down_s = float(metrics.get("head_down_s", 0.0))
    fixed_buzzer = False

    reasons: List[str] = []
    if eye_closed_ms >= EYE_CLOSED_EMERGENCY_MS:
        reasons.append("LOSS_OF_CONSCIOUSNESS")
    if head_down and head_down_s >= HEAD_DOWN_FIXED_BUZZER_S:
        reasons.append("PROLONGED_HEAD_DOWN")
        fixed_buzzer = True

    if not head_down:
        emergency = len(reasons) > 0
        return {
            "emergencyflag": emergency,
            "emergencytype": reasons[0] if emergency else None,
            "reasons": reasons,
            "fixedbuzzer": fixed_buzzer,
        }

    if float(metrics.get("head_micro_osc", 0.0)) >= 0.45 and float(metrics.get("landmark_stability", 1.0)) <= 0.35:
        reasons.append("CONVULSIVE_PATTERN")
    if float(metrics.get("facial_asymmetry", 0.0)) >= 0.09:
        reasons.append("STROKE_PATTERN")
    if abs(float(metrics.get("roll", 0.0))) >= 45.0 and abs(float(metrics.get("yaw", 0.0))) >= 30.0:
        reasons.append("LATERAL_COLLAPSE")
    if float(metrics.get("fixation", 0.0)) >= 15.0 and float(metrics.get("blink_fb", 20.0)) <= 4.0:
        reasons.append("DISSOCIATION")
    if bool(metrics.get("face_out", False)) and not bool(metrics.get("yaw_justified", False)):
        reasons.append("FACE_OUT_OF_FRAME")

    emergency = len(reasons) > 0
    return {
        "emergencyflag": emergency,
        "emergencytype": reasons[0] if emergency else None,
        "reasons": reasons,
        "fixedbuzzer": fixed_buzzer,
    }


if __name__ == "__main__":
    print(detect_emergency({"blink_tc_ms": 2800, "pitch_delta": 24}))
