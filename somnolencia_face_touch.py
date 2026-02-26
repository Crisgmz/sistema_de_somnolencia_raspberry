"""Metricas de contacto mano-rostro.

Formulas:
- Face Touch Frequency = eventos_en_ventana / segundos_ventana
- Hand-to-Face Duration = tiempo continuo de contacto activo

Referencia:
- Conduct-based drowsiness studies: eye rubbing / face touching as fatigue indicators.
"""

from collections import deque
from typing import Deque, Dict, Optional

import numpy as np


class FaceTouchMetrics:
    def __init__(self, params) -> None:
        self.params = params
        self.touch_active = False
        self.touch_start_ts: Optional[float] = None
        self.touch_events: Deque[float] = deque(maxlen=600)
        self.eye_rub_counter = 0

    def update(
        self,
        ts: float,
        hand_results,
        face_detected: bool,
        face_width: float,
        eye_left_center,
        eye_right_center,
        ear: float,
    ) -> Dict[str, float]:
        touch_now = False
        eye_rub_detected = False

        if face_detected and hand_results and hand_results.multi_hand_landmarks:
            face_width = max(float(face_width), 1.0)
            face_thr = self.params.face_touch_distance_ratio * face_width
            rub_thr = max(24.0, self.params.eye_rub_distance_ratio * face_width)
            tip_idxs = [4, 8, 12, 16, 20]
            face_anchor = None
            if eye_left_center is not None and eye_right_center is not None:
                face_anchor = (np.asarray(eye_left_center, dtype=np.float32) + np.asarray(eye_right_center, dtype=np.float32)) * 0.5

            for hand_landmarks in hand_results.multi_hand_landmarks:
                for idx in tip_idxs:
                    tip = hand_landmarks.landmark[idx]
                    tip_pt = np.asarray([tip.x, tip.y], dtype=np.float32)
                    if face_anchor is not None:
                        d_face = float(np.linalg.norm((tip_pt * [1.0, 1.0]) - (face_anchor / max(face_width, 1.0))))
                        if d_face <= 0.35:
                            touch_now = True

                    if eye_left_center is not None and eye_right_center is not None:
                        tip_px = np.asarray([tip.x * face_width, tip.y * face_width], dtype=np.float32)
                        left_px = np.asarray(eye_left_center, dtype=np.float32)
                        right_px = np.asarray(eye_right_center, dtype=np.float32)
                        if min(float(np.linalg.norm(tip_px - left_px)), float(np.linalg.norm(tip_px - right_px))) <= rub_thr:
                            touch_now = True
                            if ear <= self.params.eye_rub_ear_max:
                                self.eye_rub_counter += 1
                            else:
                                self.eye_rub_counter = 0
                            if self.eye_rub_counter >= self.params.eye_rub_min_frames:
                                eye_rub_detected = True
                            break
                if touch_now:
                    break

        if touch_now and not self.touch_active:
            self.touch_active = True
            self.touch_start_ts = ts
            self.touch_events.append(ts)
        elif not touch_now:
            self.touch_active = False
            self.touch_start_ts = None
            self.eye_rub_counter = 0

        while self.touch_events and (ts - self.touch_events[0]) > self.params.face_touch_window_seconds:
            self.touch_events.popleft()

        touch_duration = float(ts - self.touch_start_ts) if self.touch_active and self.touch_start_ts is not None else 0.0
        freq_hz = float(len(self.touch_events) / max(1e-6, self.params.face_touch_window_seconds))

        return {
            "eye_rubbing_detection": bool(eye_rub_detected),
            "face_touch_frequency": float(freq_hz),
            "hand_to_face_duration": float(touch_duration),
            "hand_to_face_active": bool(self.touch_active),
        }
