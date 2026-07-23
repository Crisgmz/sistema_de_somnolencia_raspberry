"""Estado de calibracion compartido para todos los modulos."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Calibration:
    ear_baseline: float = 0.28
    # MAR de boca cerrada con la formula actual (aperturas verticales / ancho
    # comisura-a-comisura) ronda ~0.45. El default anterior (0.25) era de una
    # formula previa y hacia que la calibracion rechazara los valores reales.
    mar_baseline: float = 0.45
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

    # Campos que definen el "punto de referencia" del conductor (persistibles).
    _PERSIST_FIELDS = (
        "ear_baseline", "mar_baseline", "tc_baseline_ms", "fb_baseline_per_min",
        "pitch_neutral", "roll_neutral", "yaw_neutral", "asymmetry_base",
    )

    # Rangos plausibles para validar/clampear tras la calibracion. Un baseline
    # fuera de rango casi siempre indica calibracion contaminada.
    # Nota: los neutros de pose (pitch/roll/yaw) NO se acotan a ~0. El pitch/roll
    # crudos de solvePnP no estan centrados en 0 (de frente el pitch ~170 deg por
    # la convencion del modelo 3D); el neutro absorbe ese offset. Solo se limita
    # al rango fisico de Euler [-180,180] como salvaguarda ante valores basura.
    _RANGES = {
        "ear_baseline": (0.15, 0.42),
        "mar_baseline": (0.20, 0.70),
        "tc_baseline_ms": (80.0, 400.0),
        "fb_baseline_per_min": (5.0, 40.0),
        "pitch_neutral": (-180.0, 180.0),
        "roll_neutral": (-180.0, 180.0),
        "yaw_neutral": (-180.0, 180.0),
        "asymmetry_base": (0.005, 0.15),
    }

    def sanitize(self) -> list[str]:
        """Clampa los baselines a rangos plausibles. Devuelve la lista de campos
        corregidos (para loggear una calibracion sospechosa)."""
        corrected = []
        for name, (lo, hi) in self._RANGES.items():
            val = float(getattr(self, name))
            clamped = max(lo, min(hi, val))
            if abs(clamped - val) > 1e-9:
                corrected.append(name)
                setattr(self, name, clamped)
        return corrected

    def snapshot(self) -> dict:
        snap = {f: float(getattr(self, f)) for f in self._PERSIST_FIELDS}
        snap["glassesmode"] = bool(self.glassesmode)
        return snap

    def restore(self, payload: dict) -> None:
        for f in self._PERSIST_FIELDS:
            if f in payload:
                try:
                    setattr(self, f, float(payload[f]))
                except (TypeError, ValueError):
                    pass
        if "glassesmode" in payload:
            self.glassesmode = bool(payload["glassesmode"])
        self.sanitize()
