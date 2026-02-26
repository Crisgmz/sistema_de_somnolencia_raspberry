"""Parametros de manos/contacto: frotado de ojos, frecuencia y duracion mano-cara."""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Sequence

import numpy as np

from core.calibration import Calibration
from core.common_types import build_param_output, normalize_linear


class ManosParametros:
    def __init__(self, min_eye_rub_frames: int = 4, touch_window_s: float = 300.0) -> None:
        self.min_eye_rub_frames = int(min_eye_rub_frames)
        self.touch_window_s = float(touch_window_s)
        self.eye_rub_frames = 0
        self.touch_active = False
        self.touch_start_ts: Optional[float] = None
        self.touch_events: Deque[float] = deque(maxlen=2000)

    def update(
        self,
        ts: float,
        hand_results,
        left_eye_center: Sequence[float],
        right_eye_center: Sequence[float],
        frame_w: int,
        frame_h: int,
        calibration: Calibration,
    ):
        left = np.asarray(left_eye_center, dtype=np.float32)
        right = np.asarray(right_eye_center, dtype=np.float32)

        touch_now = False
        if hand_results and getattr(hand_results, "multi_hand_landmarks", None):
            for hand in hand_results.multi_hand_landmarks:
                for idx in (4, 8, 12, 16, 20):
                    tip = hand.landmark[idx]
                    tip_px = np.asarray([tip.x * frame_w, tip.y * frame_h], dtype=np.float32)
                    if min(float(np.linalg.norm(tip_px - left)), float(np.linalg.norm(tip_px - right))) <= 36.0:
                        touch_now = True
                        break
                if touch_now:
                    break

        self.eye_rub_frames = self.eye_rub_frames + 1 if touch_now else 0
        eye_rub_event = calibration.calibrated and self.eye_rub_frames >= self.min_eye_rub_frames

        if touch_now and not self.touch_active:
            self.touch_active = True
            self.touch_start_ts = ts
            self.touch_events.append(ts)
        elif not touch_now:
            self.touch_active = False
            self.touch_start_ts = None

        while self.touch_events and (ts - self.touch_events[0]) > self.touch_window_s:
            self.touch_events.popleft()

        touch_freq_per_min = float(len(self.touch_events)) * 60.0 / self.touch_window_s
        touch_dur_ms = (ts - self.touch_start_ts) * 1000.0 if self.touch_active and self.touch_start_ts is not None else 0.0

        return {
            "EYE_RUB": build_param_output("EYE_RUB", float(self.eye_rub_frames), 1.0 if eye_rub_event else 0.0, eye_rub_event, 4 if eye_rub_event else 0, ts=ts),
            "FACE_TOUCH_FREQ": build_param_output("FACE_TOUCH_FREQ", touch_freq_per_min, normalize_linear(touch_freq_per_min, 2.0, 10.0), calibration.calibrated and touch_freq_per_min >= 5.0, 2, ts=ts),
            "FACE_TOUCH_DUR": build_param_output("FACE_TOUCH_DUR", touch_dur_ms, normalize_linear(touch_dur_ms, 400.0, 2500.0), calibration.calibrated and touch_dur_ms >= 1200.0, 2, ts=ts),
        }


if __name__ == "__main__":
    c = Calibration(calibrated=True)
    p = ManosParametros()
    print(p.update(1.0, None, (100, 100), (120, 100), 640, 480, c)["EYE_RUB"])
