"""Pipeline independiente de emergencias medicas (no altera fatigue score)."""

from __future__ import annotations

from typing import Dict, List


def detect_emergency(metrics: Dict) -> Dict:
    reasons: List[str] = []
    if float(metrics.get("blink_tc_ms", 0.0)) >= 2500.0 and float(metrics.get("pitch", 0.0)) >= 20.0:
        reasons.append("LOSS_OF_CONSCIOUSNESS")
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
    }


if __name__ == "__main__":
    print(detect_emergency({"blink_tc_ms": 2800, "pitch": 24}))
