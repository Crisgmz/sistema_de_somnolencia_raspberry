"""Parametros oculares: PERCLOS, Tc, Fb, IBI, amplitud, velocidad reapertura, fijacion."""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Sequence, Tuple

import numpy as np

from core.calibration import Calibration
from core.common_types import build_param_output, normalize_linear


class OjosParametros:
    def __init__(self, perclos_window_s: float = 60.0, fixation_motion_px_s: float = 28.0) -> None:
        self.perclos_window_s = float(perclos_window_s)
        self.fixation_motion_px_s = float(fixation_motion_px_s)

        self.eye_hist: Deque[Tuple[float, int]] = deque(maxlen=8000)
        self.blink_times: Deque[float] = deque(maxlen=3000)
        self.ibi_hist: Deque[float] = deque(maxlen=600)

        self.blink_active = False
        self.blink_start_ts: Optional[float] = None
        self.blink_min_ear = 1.0
        self.open_ref = 0.30
        self.last_tc_ms = 0.0
        self.last_amp = 0.0
        self.last_reopen = 0.0

        self.last_center: Optional[np.ndarray] = None
        self.last_center_ts: Optional[float] = None
        self.fix_start_ts: Optional[float] = None

    def _update_fixation(self, ts: float, left_eye_center: Sequence[float], right_eye_center: Sequence[float]) -> float:
        center = (np.asarray(left_eye_center, dtype=np.float32) + np.asarray(right_eye_center, dtype=np.float32)) * 0.5
        if self.last_center is None or self.last_center_ts is None:
            self.last_center = center
            self.last_center_ts = ts
            self.fix_start_ts = ts
            return 0.0
        dt = max(1e-3, ts - self.last_center_ts)
        speed = float(np.linalg.norm(center - self.last_center) / dt)
        if speed > self.fixation_motion_px_s:
            self.fix_start_ts = ts
        self.last_center = center
        self.last_center_ts = ts
        return float(ts - (self.fix_start_ts if self.fix_start_ts is not None else ts))

    def update(self, ts: float, ear: float, left_eye_center: Sequence[float], right_eye_center: Sequence[float], calibration: Calibration):
        close_thr = max(0.12, calibration.ear_baseline * 0.8)
        open_thr = max(close_thr + 0.02, calibration.ear_baseline * 0.9)

        if ear > open_thr:
            self.open_ref = 0.95 * self.open_ref + 0.05 * ear

        closed = 1 if ear < close_thr else 0
        self.eye_hist.append((float(ts), closed))
        while self.eye_hist and (ts - self.eye_hist[0][0]) > self.perclos_window_s:
            self.eye_hist.popleft()

        blink_detected = False
        if ear < close_thr and not self.blink_active:
            self.blink_active = True
            self.blink_start_ts = ts
            self.blink_min_ear = ear
        elif ear < close_thr and self.blink_active:
            self.blink_min_ear = min(self.blink_min_ear, ear)
        elif ear >= open_thr and self.blink_active:
            self.blink_active = False
            if self.blink_start_ts is not None:
                dt = max(1e-3, ts - self.blink_start_ts)
                self.last_tc_ms = dt * 1000.0
                self.last_amp = max(0.0, self.open_ref - self.blink_min_ear)
                self.last_reopen = max(0.0, (ear - self.blink_min_ear) / dt)
                if 0.06 <= dt <= 0.8:
                    blink_detected = True
                    self.blink_times.append(float(ts))
                    if len(self.blink_times) >= 2:
                        self.ibi_hist.append(float(self.blink_times[-1] - self.blink_times[-2]))
            self.blink_start_ts = None

        while self.blink_times and (ts - self.blink_times[0]) > self.perclos_window_s:
            self.blink_times.popleft()

        perclos = sum(v for _, v in self.eye_hist) / float(len(self.eye_hist)) if self.eye_hist else 0.0
        fb_per_min = float(len(self.blink_times))
        ibi_s = float(np.mean(self.ibi_hist)) if self.ibi_hist else 0.0
        fixation_s = self._update_fixation(ts, left_eye_center, right_eye_center)
        eye_closed_ms = 0.0
        if self.blink_active and self.blink_start_ts is not None:
            eye_closed_ms = max(0.0, (ts - self.blink_start_ts) * 1000.0)

        immediate_eye_closed_event = eye_closed_ms >= 450.0
        return {
            "EAR": build_param_output("EAR", ear, normalize_linear(max(0.0, calibration.ear_baseline - ear), 0.0, 0.20), calibration.calibrated and ear < close_thr, 2, ts=ts),
            "EYE_CLOSED_MS": build_param_output("EYE_CLOSED_MS", eye_closed_ms, normalize_linear(eye_closed_ms, 300.0, 2000.0), immediate_eye_closed_event, 8, ts=ts),
            "PERCLOS": build_param_output("PERCLOS", perclos, normalize_linear(perclos, 0.15, 0.5), calibration.calibrated and perclos >= 0.25, 10, ts=ts),
            "BLINK_TC": build_param_output("BLINK_TC", self.last_tc_ms, normalize_linear(self.last_tc_ms, calibration.tc_baseline_ms, 700.0), calibration.calibrated and self.last_tc_ms >= 500.0, 6, ts=ts),
            "BLINK_FB": build_param_output("BLINK_FB", fb_per_min, max(normalize_linear(fb_per_min, 0.0, 6.0), normalize_linear(fb_per_min, 20.0, 35.0)), calibration.calibrated and (fb_per_min < 6.0 or fb_per_min > 28.0), 5, ts=ts),
            "IBI": build_param_output("IBI", ibi_s, normalize_linear(ibi_s, 3.0, 10.0), calibration.calibrated and ibi_s >= 6.0, 4, ts=ts),
            "BLINK_AMPLITUDE": build_param_output("BLINK_AMPLITUDE", self.last_amp, normalize_linear(self.last_amp, 0.02, 0.18), calibration.calibrated and 0.0 < self.last_amp < 0.035, 3, ts=ts),
            "REOPEN_SPEED": build_param_output("REOPEN_SPEED", self.last_reopen, normalize_linear(1.2 - self.last_reopen, 0.0, 1.2), calibration.calibrated and 0.0 < self.last_reopen < 0.12, 5, ts=ts),
            "FIXATION": build_param_output("FIXATION", fixation_s, normalize_linear(fixation_s, 2.0, 8.0), calibration.calibrated and fixation_s >= 6.0, 4, ts=ts),
            "blink_detected": blink_detected,
        }


if __name__ == "__main__":
    c = Calibration(calibrated=True)
    p = OjosParametros()
    out = None
    for i in range(100):
        out = p.update(i * 0.1, 0.16 if i % 4 == 0 else 0.30, (100, 100), (120, 100), c)
    print(out["PERCLOS"])
