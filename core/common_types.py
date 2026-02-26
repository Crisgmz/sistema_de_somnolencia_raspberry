"""Tipos y helpers para salida estandar de modulos de somnolencia."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def normalize_linear(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return clamp01((float(value) - float(low)) / (float(high) - float(low)))


def build_param_output(
    param_id: str,
    value: float,
    normalized: float,
    event_flag: bool,
    fatigue_score_delta: int,
    emergency_flag: bool = False,
    emergency_type: Optional[str] = None,
    ts: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "paramid": str(param_id),
        "value": float(value),
        "normalized": clamp01(normalized),
        "eventflag": bool(event_flag),
        "fatiguescoredelta": int(fatigue_score_delta),
        "emergencyflag": bool(emergency_flag),
        "emergencytype": emergency_type,
        "timestamp": float(time.time() if ts is None else ts),
    }
