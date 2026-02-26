"""Metricas faciales de estabilidad y tono muscular.

Formulas:
- Landmark Stability = 1 / (1 + scale * mean_displacement_normalized)
- Facial Muscle Tone Drop = max(0, baseline_tone - tone_now) / baseline_tone
- Facial asymmetry index: diferencia izquierda/derecha normalizada por ancho facial

Referencia:
- Estudios de hipotonia facial y asimetria en vigilancia clinica neurologica.
"""

from typing import Dict, Optional

import numpy as np


class FacialMetrics:
    def __init__(self, params) -> None:
        self.params = params
        self.prev_xy: Optional[np.ndarray] = None
        self.tone_baseline: Optional[float] = None

    @staticmethod
    def _as_xy(landmarks, w: int, h: int) -> np.ndarray:
        return np.asarray([[p.x * w, p.y * h] for p in landmarks], dtype=np.float32)

    def update(self, landmarks, w: int, h: int) -> Dict[str, float]:
        if landmarks is None:
            self.prev_xy = None
            return {
                "facial_landmark_stability": 0.0,
                "facial_muscle_tone_drop": 0.0,
                "facial_asymmetry_index": 0.0,
            }

        xy = self._as_xy(landmarks, w, h)
        face_width = float(np.linalg.norm(xy[33] - xy[263]))
        face_width = max(face_width, 1.0)

        stability = 1.0
        if self.prev_xy is not None and self.prev_xy.shape == xy.shape:
            disp = np.linalg.norm(xy - self.prev_xy, axis=1)
            disp_n = float(np.mean(disp) / face_width)
            stability = 1.0 / (1.0 + self.params.facial_stability_scale * disp_n)
        self.prev_xy = xy

        mouth_open = float(np.linalg.norm(xy[13] - xy[14]) / face_width)
        jaw_drop = float(np.linalg.norm(xy[152] - xy[1]) / face_width)
        tone_now = 0.6 * mouth_open + 0.4 * jaw_drop
        if self.tone_baseline is None:
            self.tone_baseline = tone_now
        else:
            self.tone_baseline = (1.0 - self.params.facial_tone_alpha) * self.tone_baseline + self.params.facial_tone_alpha * tone_now
        baseline = max(1e-6, float(self.tone_baseline))
        tone_drop = max(0.0, (baseline - tone_now) / baseline)

        nose_x = float(xy[1, 0])
        pairs = [(33, 263), (61, 291), (93, 323), (159, 386), (145, 374)]
        asym_terms = []
        for li, ri in pairs:
            lx, ly = xy[li]
            rx, ry = xy[ri]
            x_mirror = abs((lx + rx) - (2.0 * nose_x)) / face_width
            y_diff = abs(ly - ry) / face_width
            asym_terms.append(x_mirror + y_diff)
        asym_idx = float(np.mean(asym_terms)) if asym_terms else 0.0

        return {
            "facial_landmark_stability": float(stability),
            "facial_muscle_tone_drop": float(tone_drop),
            "facial_asymmetry_index": float(asym_idx),
        }
