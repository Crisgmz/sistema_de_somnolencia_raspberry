"""Tipos y helpers para salida estandar de modulos de somnolencia."""

from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def wrap_deg(angle: float) -> float:
    """Normaliza un angulo a [-180, 180)."""
    return ((float(angle) + 180.0) % 360.0) - 180.0


def angle_delta_deg(a: float, b: float) -> float:
    """Diferencia angular MAS CORTA a-b en grados, en [-180, 180).

    Imprescindible para pose: pitch/roll de solvePnP rondan ±180 y saltan el
    wraparound; restarlos linealmente da valores enormes espurios.
    """
    return wrap_deg(float(a) - float(b))


class CircularMeanDeg:
    """Media circular incremental (EMA) de un angulo en grados via sin/cos.

    Una EMA lineal de angulos cerca de ±180 se rompe (promediar +170 y -170 da
    0, no 180). Acumular seno/coseno da la media circular correcta.
    """

    def __init__(self, alpha: float = 0.01) -> None:
        self.alpha = float(alpha)
        self._s = 0.0
        self._c = 0.0
        self._init = False

    def update(self, angle_deg: float) -> float:
        r = math.radians(float(angle_deg))
        s, c = math.sin(r), math.cos(r)
        if not self._init:
            self._s, self._c = s, c
            self._init = True
        else:
            self._s = (1.0 - self.alpha) * self._s + self.alpha * s
            self._c = (1.0 - self.alpha) * self._c + self.alpha * c
        return self.value()

    def value(self) -> float:
        if not self._init:
            return 0.0
        return math.degrees(math.atan2(self._s, self._c))


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
