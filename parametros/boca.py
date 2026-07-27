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

    def update(self, ts: float, mar: float, calibration: Calibration, pose_reliable: bool = True):
        reliable = bool(pose_reliable)
        thr = max(0.34, calibration.mar_baseline * 1.4)
        if not reliable:
            # Pose no fiable: abortar bostezo en curso sin contarlo. Un giro/
            # inclinacion fuerte deforma la boca y falsea el MAR.
            self.yawn_active = False
            self.yawn_start_ts = None
        elif mar >= thr and not self.yawn_active:
            self.yawn_active = True
            self.yawn_start_ts = ts
        elif mar < thr and self.yawn_active:
            dur = max(0.0, ts - (self.yawn_start_ts if self.yawn_start_ts is not None else ts))
            self.yawn_active = False
            self.yawn_start_ts = None
            # Solo cuenta como bostezo si la apertura fue SOSTENIDA (>=0.8s).
            # Hablar abre/cierra la boca en <0.5s y no debe inflar el conteo.
            if dur >= 0.8:
                self.last_yawn_dur = dur
                self.yawn_count += 1

        # Duracion de la apertura en curso.
        mouth_open_ms = 0.0
        if self.yawn_active and self.yawn_start_ts is not None:
            mouth_open_ms = max(0.0, (ts - self.yawn_start_ts) * 1000.0)
        # MAR solo puntua con apertura SOSTENIDA (>=500ms) = bostezo real, no una
        # apertura breve al hablar ni ruido de landmarks. Sin esto, cualquier
        # movimiento de boca subia el score sin somnolencia (igual que los
        # parpadeos con EAR).
        event = reliable and calibration.calibrated and mar >= thr and mouth_open_ms >= 500.0
        # Solo MAR (bostezo sostenido) puntua. Conteo y duracion de bostezos son
        # derivados del mismo gesto: puntuarlos ademas duplicaba la evidencia.
        return {
            "MAR": build_param_output("MAR", mar, normalize_linear(mar, calibration.mar_baseline * 1.1, calibration.mar_baseline * 2.0), event, 4 if event else 0, ts=ts),
            "YAWN_FREQ": build_param_output("YAWN_FREQ", float(self.yawn_count), normalize_linear(float(self.yawn_count), 2.0, 7.0), False, 0, ts=ts),
            "YAWN_DUR": build_param_output("YAWN_DUR", float(self.last_yawn_dur), normalize_linear(float(self.last_yawn_dur), 1.0, 3.0), False, 0, ts=ts),
        }


if __name__ == "__main__":
    c = Calibration(calibrated=True)
    p = BocaParametros()
    print(p.update(0.0, 0.4, c)["MAR"])
