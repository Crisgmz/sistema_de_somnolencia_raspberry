"""Parametros de boca: MAR y bostezo."""

from __future__ import annotations

from typing import Optional

from core.calibration import Calibration
from core.common_types import build_param_output, normalize_linear


class BocaParametros:
    def __init__(self) -> None:
        self.yawn_active = False
        self.yawn_start_ts: Optional[float] = None
        self.last_yawn_dur = 0.0
        self.yawn_count = 0

    def update(self, ts: float, mar: float, calibration: Calibration):
        thr = max(0.34, calibration.mar_baseline * 1.4)
        if mar >= thr and not self.yawn_active:
            self.yawn_active = True
            self.yawn_start_ts = ts
        elif mar < thr and self.yawn_active:
            self.yawn_active = False
            self.last_yawn_dur = max(0.0, ts - (self.yawn_start_ts if self.yawn_start_ts is not None else ts))
            self.yawn_count += 1
            self.yawn_start_ts = None

        event = calibration.calibrated and mar >= thr
        return {
            "MAR": build_param_output("MAR", mar, normalize_linear(mar, calibration.mar_baseline * 1.1, calibration.mar_baseline * 2.0), event, 4 if event else 0, ts=ts),
            "YAWN_FREQ": build_param_output("YAWN_FREQ", float(self.yawn_count), normalize_linear(float(self.yawn_count), 2.0, 7.0), calibration.calibrated and self.yawn_count >= 4, 5, ts=ts),
            "YAWN_DUR": build_param_output("YAWN_DUR", float(self.last_yawn_dur), normalize_linear(float(self.last_yawn_dur), 1.0, 3.0), calibration.calibrated and self.last_yawn_dur >= 2.0, 4, ts=ts),
        }


if __name__ == "__main__":
    c = Calibration(calibrated=True)
    p = BocaParametros()
    print(p.update(0.0, 0.4, c)["MAR"])
