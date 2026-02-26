"""Parametros de cabeza: pose (pitch/roll/yaw), velocidad caida, recovery, micro-oscilaciones."""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Sequence, Tuple

import cv2
import numpy as np

from core.calibration import Calibration
from core.common_types import build_param_output, normalize_linear

POSE_IDX = [1, 152, 33, 263, 61, 291]
MODEL_POINTS = np.array(
    [
        (0.0, 0.0, 0.0),
        (0.0, -330.0, -65.0),
        (-225.0, 170.0, -135.0),
        (225.0, 170.0, -135.0),
        (-150.0, -150.0, -125.0),
        (150.0, -150.0, -125.0),
    ],
    dtype=np.float64,
)


def _rotation_to_euler_deg(rot_matrix: np.ndarray) -> Tuple[float, float, float]:
    sy = np.sqrt(rot_matrix[0, 0] ** 2 + rot_matrix[1, 0] ** 2)
    singular = sy < 1e-6
    if not singular:
        x = np.arctan2(rot_matrix[2, 1], rot_matrix[2, 2])
        y = np.arctan2(-rot_matrix[2, 0], sy)
        z = np.arctan2(rot_matrix[1, 0], rot_matrix[0, 0])
    else:
        x = np.arctan2(-rot_matrix[1, 2], rot_matrix[1, 1])
        y = np.arctan2(-rot_matrix[2, 0], sy)
        z = 0.0
    return float(np.degrees(x)), float(np.degrees(y)), float(np.degrees(z))


class CabezaParametros:
    def __init__(self, micro_window_s: float = 6.0) -> None:
        self.prev_pitch: Optional[float] = None
        self.prev_ts: Optional[float] = None
        self.recovery_active = False
        self.recovery_start_ts: Optional[float] = None
        self.last_recovery = 0.0
        self.pitch_hist: Deque[Tuple[float, float]] = deque(maxlen=6000)

    def _pose(self, landmarks: Sequence, frame_w: int, frame_h: int) -> Tuple[float, float, float]:
        image_points = np.asarray([[landmarks[i].x * frame_w, landmarks[i].y * frame_h] for i in POSE_IDX], dtype=np.float64)
        camera_matrix = np.asarray([[frame_w, 0, frame_w / 2.0], [0, frame_w, frame_h / 2.0], [0, 0, 1]], dtype=np.float64)
        ok, rvec, _ = cv2.solvePnP(MODEL_POINTS, image_points, camera_matrix, np.zeros((4, 1), dtype=np.float64), flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok:
            return 0.0, 0.0, 0.0
        rot_matrix, _ = cv2.Rodrigues(rvec)
        return _rotation_to_euler_deg(rot_matrix)

    def update(self, ts: float, landmarks: Sequence, frame_w: int, frame_h: int, calibration: Calibration):
        pitch, yaw, roll = self._pose(landmarks, frame_w, frame_h)
        velocity = 0.0
        if self.prev_pitch is not None and self.prev_ts is not None:
            velocity = (pitch - self.prev_pitch) / max(1e-3, ts - self.prev_ts)
        self.prev_pitch = pitch
        self.prev_ts = ts

        pitch_delta = pitch - calibration.pitch_neutral
        if pitch_delta >= 20.0 and not self.recovery_active:
            self.recovery_active = True
            self.recovery_start_ts = ts
        elif self.recovery_active and pitch_delta <= 12.0:
            self.last_recovery = max(0.0, ts - (self.recovery_start_ts if self.recovery_start_ts is not None else ts))
            self.recovery_active = False
            self.recovery_start_ts = None

        self.pitch_hist.append((ts, pitch))
        while self.pitch_hist and (ts - self.pitch_hist[0][0]) > 6.0:
            self.pitch_hist.popleft()

        micro_value = 0.0
        if len(self.pitch_hist) >= 24:
            t = np.asarray([k for k, _ in self.pitch_hist], dtype=np.float64)
            x = np.asarray([v for _, v in self.pitch_hist], dtype=np.float64)
            x = x - np.mean(x)
            dt = np.median(np.diff(t)) if t.size > 1 else 0.0
            if dt > 0:
                spec = np.abs(np.fft.rfft(x))
                freqs = np.fft.rfftfreq(x.size, d=dt)
                band = (freqs >= 2.5) & (freqs <= 3.5)
                total = float(np.sum(spec[1:] ** 2)) if spec.size > 2 else 0.0
                micro_value = float(np.sum(spec[band] ** 2) / total) if total > 1e-9 else 0.0

        return {
            "PITCH": build_param_output("PITCH", pitch, normalize_linear(abs(pitch_delta), 8.0, 25.0), calibration.calibrated and abs(pitch_delta) >= 20.0, 6, ts=ts),
            "ROLL": build_param_output("ROLL", roll, normalize_linear(abs(roll - calibration.roll_neutral), 8.0, 35.0), calibration.calibrated and abs(roll - calibration.roll_neutral) >= 28.0, 5, ts=ts),
            "YAW": build_param_output("YAW", yaw, normalize_linear(abs(yaw - calibration.yaw_neutral), 10.0, 40.0), calibration.calibrated and abs(yaw - calibration.yaw_neutral) >= 35.0, 3, ts=ts),
            "HEAD_DROP_VELOCITY": build_param_output("HEAD_DROP_VELOCITY", velocity, normalize_linear(abs(velocity), 8.0, 35.0), calibration.calibrated and velocity >= 18.0, 7, ts=ts),
            "HEAD_RECOVERY": build_param_output("HEAD_RECOVERY", self.last_recovery, normalize_linear(self.last_recovery, 0.5, 3.0), calibration.calibrated and self.last_recovery >= 1.8, 6, ts=ts),
            "HEAD_MICRO_OSC": build_param_output("HEAD_MICRO_OSC", micro_value, normalize_linear(micro_value, 0.2, 0.7), calibration.calibrated and micro_value >= 0.45, 0, ts=ts),
        }


if __name__ == "__main__":
    print("CabezaParametros requiere landmarks reales")
