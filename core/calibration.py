"""Estado de calibracion compartido para todos los modulos."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Calibration:
    ear_baseline: float = 0.28
    mar_baseline: float = 0.25
    tc_baseline_ms: float = 180.0
    fb_baseline_per_min: float = 15.0
    pitch_neutral: float = 0.0
    roll_neutral: float = 0.0
    yaw_neutral: float = 0.0
    asymmetry_base: float = 0.03
    calibrated: bool = False
    glassesmode: bool = False
    nightmode: bool = False

    def mark_calibrated(self) -> None:
        self.calibrated = True
