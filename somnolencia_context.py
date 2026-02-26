"""Metricas contextuales de fatiga (software-only).

Formulas:
- Time on Task = t_actual - t_inicio_sesion
- Circadian multiplier = 1.4 si hora local en [2..6] o [13..15], en otro caso 1.0
- Monotony Index: inverso de la variabilidad (movimiento cabeza + iluminacion)
- Illumination Level: mean(gray)/255

Referencia:
- Time-on-task and circadian modulation in driver fatigue literature.
"""

from collections import deque
from datetime import datetime
from typing import Deque, Dict, Tuple

import numpy as np


class ContextMetrics:
    def __init__(self, params) -> None:
        self.params = params
        self.start_ts = None
        self.motion_hist: Deque[Tuple[float, float]] = deque(maxlen=6000)
        self.illum_hist: Deque[Tuple[float, float]] = deque(maxlen=6000)

    @staticmethod
    def _circadian_multiplier(hour_local: int) -> float:
        if 2 <= hour_local <= 6 or 13 <= hour_local <= 15:
            return 1.4
        return 1.0

    def update(self, ts: float, pitch: float, yaw: float, roll: float, illumination_level: float) -> Dict[str, float]:
        if self.start_ts is None:
            self.start_ts = ts
        time_on_task = float(ts - self.start_ts)

        motion = float(abs(pitch) + abs(yaw) + abs(roll))
        illum = float(illumination_level)
        self.motion_hist.append((ts, motion))
        self.illum_hist.append((ts, illum))
        while self.motion_hist and (ts - self.motion_hist[0][0]) > self.params.context_monotony_window_seconds:
            self.motion_hist.popleft()
        while self.illum_hist and (ts - self.illum_hist[0][0]) > self.params.context_monotony_window_seconds:
            self.illum_hist.popleft()

        motion_std = float(np.std([v for _, v in self.motion_hist])) if self.motion_hist else 0.0
        illum_std = float(np.std([v for _, v in self.illum_hist])) if self.illum_hist else 0.0
        monotony_index = float(np.clip(1.0 / (1.0 + 0.15 * motion_std + 4.0 * illum_std), 0.0, 1.0))

        hour_local = datetime.now().hour
        circadian = self._circadian_multiplier(hour_local)

        if illum < self.params.illumination_dark_threshold:
            illum_state = "dark"
        elif illum > self.params.illumination_bright_threshold:
            illum_state = "bright"
        else:
            illum_state = "normal"

        return {
            "time_on_task": time_on_task,
            "time_of_day_multiplier": float(circadian),
            "monotony_index": monotony_index,
            "illumination_level": float(illum),
            "illumination_state": illum_state,
        }
