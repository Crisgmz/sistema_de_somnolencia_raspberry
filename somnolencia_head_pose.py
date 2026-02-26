"""Metricas avanzadas de dinamica de cabeza.

Formulas:
- Head Drop Velocity = d(pitch)/dt (grados/seg)
- Head Recovery Time = tiempo desde pitch alto hasta retorno a umbral reset
- Micro-oscillations = std(detrended_pitch) en ventana corta

Referencia:
- Evaluaciones de cabeceo en vigilancia de fatiga de conductor (SAE/IEEE ADAS literature).
"""

from collections import deque
from typing import Deque, Dict, Optional, Tuple

import numpy as np


class HeadPoseMetrics:
    def __init__(self, params) -> None:
        self.params = params
        self.prev_pitch: Optional[float] = None
        self.prev_ts: Optional[float] = None
        self.pitch_hist: Deque[Tuple[float, float]] = deque(maxlen=1200)
        self.recovery_active = False
        self.recovery_start_ts: Optional[float] = None

    def update(self, ts: float, pitch: float, yaw: float, roll: float) -> Dict[str, float]:
        self.pitch_hist.append((ts, float(pitch)))
        while self.pitch_hist and (ts - self.pitch_hist[0][0]) > self.params.head_micro_window_seconds:
            self.pitch_hist.popleft()

        velocity = 0.0
        if self.prev_pitch is not None and self.prev_ts is not None:
            dt = max(1e-6, ts - self.prev_ts)
            velocity = float((pitch - self.prev_pitch) / dt)
        self.prev_pitch = float(pitch)
        self.prev_ts = ts

        recovery_time = 0.0
        if pitch >= self.params.head_recovery_start_pitch and not self.recovery_active:
            self.recovery_active = True
            self.recovery_start_ts = ts
        elif self.recovery_active and pitch <= self.params.head_recovery_reset_pitch:
            recovery_time = float(ts - self.recovery_start_ts) if self.recovery_start_ts is not None else 0.0
            self.recovery_active = False
            self.recovery_start_ts = None

        micro_osc = 0.0
        if len(self.pitch_hist) >= 8:
            arr = np.asarray([v for _, v in self.pitch_hist], dtype=np.float32)
            x = np.arange(arr.size, dtype=np.float32)
            slope, intercept = np.polyfit(x, arr, deg=1)
            detrended = arr - (slope * x + intercept)
            micro_osc = float(np.std(detrended))

        return {
            "head_drop_velocity": float(velocity),
            "head_recovery_time": float(recovery_time),
            "head_micro_oscillations": float(micro_osc),
            "head_drop_velocity_alert": bool(velocity >= self.params.head_drop_velocity_threshold_dps),
            "pitch": float(pitch),
            "yaw": float(yaw),
            "roll": float(roll),
        }
