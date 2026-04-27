"""Parametros de contexto: tiempo de tarea, circadiano, monotonia, iluminacion."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import numpy as np

from core.calibration import Calibration
from core.common_types import build_param_output, normalize_linear


class ContextoParametros:
    def __init__(self) -> None:
        self.start_ts: Optional[float] = None
        self.last_event_ts: Optional[float] = None

    def update(self, ts: float, frame_bgr: np.ndarray, has_relevant_event: bool, calibration: Calibration):
        if self.start_ts is None:
            self.start_ts = ts
        time_on_task_min = max(0.0, (ts - self.start_ts) / 60.0)

        hour = datetime.now().hour
        circadian_mult = 1.4 if (2 <= hour <= 6 or 13 <= hour <= 15) else 1.0

        if has_relevant_event:
            self.last_event_ts = ts
            monotony = 0.0
        else:
            if self.last_event_ts is None:
                self.last_event_ts = ts
            monotony = float(max(0.0, (ts - self.last_event_ts) // 60.0))

        gray = frame_bgr.mean(axis=2) if frame_bgr.ndim == 3 else frame_bgr
        illumination = float(np.mean(gray) / 255.0)
        calibration.nightmode = illumination < 0.28

        return {
            "TIME_ON_TASK": build_param_output("TIME_ON_TASK", time_on_task_min, normalize_linear(time_on_task_min, 60.0, 180.0), calibration.calibrated and time_on_task_min >= 120.0, 2, ts=ts),
            "CIRCADIAN": build_param_output("CIRCADIAN", circadian_mult, normalize_linear(circadian_mult, 1.0, 1.4), calibration.calibrated and circadian_mult > 1.0, 0, ts=ts),
            "MONOTONY": build_param_output("MONOTONY", monotony, normalize_linear(monotony, 2.0, 12.0), calibration.calibrated and monotony >= 5.0, 2, ts=ts),
            "ILLUMINATION": build_param_output("ILLUMINATION", illumination, normalize_linear(1.0 - illumination, 0.3, 0.85), calibration.calibrated and illumination < 0.15, 1, ts=ts),
        }


if __name__ == "__main__":
    c = Calibration(calibrated=True)
    f = np.full((20, 20, 3), 30, dtype=np.uint8)
    print(ContextoParametros().update(0.0, f, False, c)["ILLUMINATION"])
