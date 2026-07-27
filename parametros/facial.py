"""Parametros faciales: estabilidad, tono muscular, asimetria."""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from core.calibration import Calibration
from core.common_types import build_param_output, normalize_linear


class FacialParametros:
    def __init__(self, stability_scale: float = 35.0, tone_alpha: float = 0.05) -> None:
        self.stability_scale = float(stability_scale)
        self.tone_alpha = float(tone_alpha)
        self.prev_xy: Optional[np.ndarray] = None
        self.tone_baseline: Optional[float] = None
        self.pairs = [(33, 263), (61, 291), (93, 323), (159, 386), (145, 374)]

    def update(self, ts: float, landmarks: Sequence, frame_w: int, frame_h: int, calibration: Calibration, rotation_index: int = 0):
        xy = []
        for p in landmarks:
            x = p.x * frame_w
            y = p.y * frame_h
            
            # Ajustar coordenadas según rotación para que sean equivalentes al sistema original
            if rotation_index == 1:  # 90 grados horario
                x, y = y, frame_w - x
            elif rotation_index == 2:  # 180 grados
                x, y = frame_w - x, frame_h - y
            elif rotation_index == 3:  # 90 grados antihorario (270 grados horario)
                x, y = frame_h - y, x
                
            xy.append([x, y])
        
        xy = np.asarray(xy, dtype=np.float32)
        face_w = max(1.0, float(np.linalg.norm(xy[33] - xy[263])))

        stability = 1.0
        if self.prev_xy is not None and self.prev_xy.shape == xy.shape:
            disp = np.linalg.norm(xy - self.prev_xy, axis=1)
            disp_n = float(np.mean(disp) / face_w)
            stability = 1.0 / (1.0 + self.stability_scale * disp_n)
        self.prev_xy = xy

        tone = float(np.linalg.norm(xy[10] - xy[152]) / face_w)
        if self.tone_baseline is None:
            self.tone_baseline = tone
        else:
            self.tone_baseline = (1.0 - self.tone_alpha) * self.tone_baseline + self.tone_alpha * tone
        base = max(1e-6, float(self.tone_baseline))
        tone_drop = max(0.0, (base - tone) / base)

        nose_x = float(xy[1, 0])
        terms = []
        for li, ri in self.pairs:
            lx, ly = xy[li]
            rx, ry = xy[ri]
            x_mirror = abs((lx + rx) - (2.0 * nose_x)) / face_w
            y_diff = abs(ly - ry) / face_w
            terms.append(x_mirror + y_diff)
        asym = float(np.mean(terms)) if terms else 0.0

        # Telemetria pura: LANDMARK_STABILITY alimenta el gate de calidad de
        # rostro (por valor) y FACIAL_ASYMMETRY las "sospechas" informativas del
        # detector de emergencia; no puntuan fatiga ni disparan emergencia aqui.
        return {
            "LANDMARK_STABILITY": build_param_output("LANDMARK_STABILITY", stability, normalize_linear(1.0 - stability, 0.1, 0.7), False, 0, ts=ts),
            "MUSCLE_TONE": build_param_output("MUSCLE_TONE", tone_drop, normalize_linear(tone_drop, 0.05, 0.35), False, 0, ts=ts),
            "FACIAL_ASYMMETRY": build_param_output("FACIAL_ASYMMETRY", asym, normalize_linear(asym, calibration.asymmetry_base * 1.3, calibration.asymmetry_base * 3.5), False, 0, ts=ts),
        }


if __name__ == "__main__":
    print("FacialParametros requiere landmarks reales")
