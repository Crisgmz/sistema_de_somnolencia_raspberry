"""Parametros oculares: cierre sostenido, PERCLOS (P80 FHWA) y metricas de parpadeo.

Diseno simplificado (ver docs/PLAN_TRABAJO_PRECISION.md):

- El disparador PRINCIPAL es el cierre sostenido (EYE_CLOSED_MS): EAR bajo un
  umbral ESTABLE derivado de la calibracion durante >= microsleep_ms. Es el
  mecanismo del detector de referencia (LearnOpenCV), robusto incluso con
  lentes: ningun parpadeo, reflejo de montura o frame ruidoso dura 500 ms.
- PERCLOS P80 como medida de fatiga acumulada (ventana de 60 s).
- El resto (Tc, Fb, IBI, amplitud, velocidad de reapertura, fijacion) se
  publica como TELEMETRIA pura (sin evento, delta 0): son metricas de
  investigacion demasiado ruidosas por conductor para puntuar sin falsos
  positivos.

El umbral de cierre NO se adapta frame a frame. La referencia adaptativa
anterior (open_ref con envolvente + escalas por noche/lentes) era la causa
principal de falsos cierres: los reflejos de los lentes arrastraban la
referencia y el umbral se movia bajo los pies del conductor. Ahora:
close_thr = SOMNO_EAR_CLOSE_FRAC * ear_baseline, congelado tras calibrar.
Con el baseline tipico (0.28) da ~0.196, en linea con el umbral fijo 0.18
del detector de referencia.
"""

from __future__ import annotations

import os
from collections import deque
from typing import Deque, Optional, Sequence, Tuple

import numpy as np

from core.calibration import Calibration
from core.common_types import build_param_output, normalize_linear


def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class OjosParametros:
    def __init__(self, perclos_window_s: float = 60.0, fixation_motion_px_s: float = 28.0) -> None:
        self.perclos_window_s = float(perclos_window_s)
        self.fixation_motion_px_s = float(fixation_motion_px_s)

        # Umbral de cierre como fraccion del EAR de ojo abierto calibrado.
        self.close_frac = _envf("SOMNO_EAR_CLOSE_FRAC", 0.70)
        # Histeresis de reapertura: evita oscilar en el borde del umbral.
        self.hysteresis = _envf("SOMNO_EAR_HYSTERESIS", 0.03)

        # PERCLOS segun el criterio P80 (FHWA): fraccion de tiempo con el
        # parpado cubriendo >=80% del ojo.
        self.perclos_close_frac = _envf("SOMNO_PERCLOS_CLOSE_FRAC", 0.80)  # P80
        self.perclos_closed_floor = _envf("SOMNO_PERCLOS_CLOSED_FLOOR", 0.08)  # EAR ojo cerrado
        self.perclos_onset = _envf("SOMNO_PERCLOS_ONSET", 0.15)  # umbral somnolencia (15%)
        self.perclos_severe = _envf("SOMNO_PERCLOS_SEVERE", 0.30)  # somnolencia marcada
        # Microsueno: cierre ocular sostenido >=500 ms segun literatura.
        self.microsleep_ms = _envf("SOMNO_MICROSLEEP_MS", 500.0)

        self.eye_hist: Deque[Tuple[float, int]] = deque(maxlen=8000)
        self.blink_times: Deque[float] = deque(maxlen=3000)
        self.ibi_hist: Deque[float] = deque(maxlen=600)

        self.blink_active = False
        self.blink_start_ts: Optional[float] = None
        self.eye_closed_event_reported = False
        self.blink_min_ear = 1.0
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

    def update(self, ts: float, ear: float, left_eye_center: Sequence[float], right_eye_center: Sequence[float], calibration: Calibration, pose_reliable: bool = True):
        reliable = bool(pose_reliable)

        # Umbrales ESTABLES derivados de la calibracion (congelados tras
        # calibrar). Antes de calibrar, ear_baseline arranca en 0.28 -> umbral
        # ~0.196, ya razonable para no perder deteccion en el arranque.
        open_level = max(0.15, float(calibration.ear_baseline))
        close_thr = max(0.10, self.close_frac * open_level)
        open_thr = close_thr + self.hysteresis

        # PERCLOS: solo se contabilizan frames con pose FIABLE. Girar la cara
        # escorza el ojo y colapsa el EAR; contar esos frames como "cerrado"
        # inflaba el PERCLOS con falsos positivos.
        if reliable:
            floor = self.perclos_closed_floor
            open_ref_ear = max(open_level, floor + 0.02)
            p80_thr = floor + (1.0 - self.perclos_close_frac) * (open_ref_ear - floor)
            self.eye_hist.append((float(ts), 1 if ear <= p80_thr else 0))
        while self.eye_hist and (ts - self.eye_hist[0][0]) > self.perclos_window_s:
            self.eye_hist.popleft()

        blink_detected = False
        if not reliable:
            # Pose no fiable (cara girada/inclinada): abortar cualquier parpadeo
            # en curso SIN registrar metricas, para no generar un falso
            # "ojo cerrado prolongado" mientras se vira la cabeza.
            self.blink_active = False
            self.blink_start_ts = None
            self.eye_closed_event_reported = False
        elif ear < close_thr and not self.blink_active:
            self.blink_active = True
            self.blink_start_ts = ts
            self.eye_closed_event_reported = False
            self.blink_min_ear = ear
        elif ear < close_thr and self.blink_active:
            self.blink_min_ear = min(self.blink_min_ear, ear)
        elif ear >= open_thr and self.blink_active:
            self.blink_active = False
            self.eye_closed_event_reported = False
            if self.blink_start_ts is not None:
                dt = max(1e-3, ts - self.blink_start_ts)
                self.last_tc_ms = dt * 1000.0
                self.last_amp = max(0.0, open_level - self.blink_min_ear)
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

        # Microsueno: solo cuenta con calibracion terminada, para no puntuar
        # mientras el baseline aun se esta asentando.
        immediate_eye_closed_event = (
            calibration.calibrated
            and eye_closed_ms >= self.microsleep_ms
            and not self.eye_closed_event_reported
        )
        if immediate_eye_closed_event:
            self.eye_closed_event_reported = True

        # Solo EYE_CLOSED_MS y PERCLOS generan eventos con peso. El resto es
        # telemetria: se publica el valor para auditoria/panel, sin puntuar.
        return {
            "EAR": build_param_output("EAR", ear, normalize_linear(max(0.0, calibration.ear_baseline - ear), 0.0, 0.20), False, 0, ts=ts),
            "EYE_CLOSED_MS": build_param_output("EYE_CLOSED_MS", eye_closed_ms, normalize_linear(eye_closed_ms, self.microsleep_ms, 3000.0), immediate_eye_closed_event, 8 if eye_closed_ms < 1500.0 else 12, ts=ts),
            "PERCLOS": build_param_output("PERCLOS", perclos, normalize_linear(perclos, self.perclos_onset, self.perclos_severe * 2.0), calibration.calibrated and perclos >= self.perclos_onset, 10 if perclos < self.perclos_severe else 14, ts=ts),
            "BLINK_TC": build_param_output("BLINK_TC", self.last_tc_ms, normalize_linear(self.last_tc_ms, calibration.tc_baseline_ms, 700.0), False, 0, ts=ts),
            "BLINK_FB": build_param_output("BLINK_FB", fb_per_min, max(normalize_linear(fb_per_min, 0.0, 4.0), normalize_linear(fb_per_min, 24.0, 40.0)), False, 0, ts=ts),
            "IBI": build_param_output("IBI", ibi_s, normalize_linear(ibi_s, 4.0, 12.0), False, 0, ts=ts),
            "BLINK_AMPLITUDE": build_param_output("BLINK_AMPLITUDE", self.last_amp, normalize_linear(self.last_amp, 0.02, 0.18), False, 0, ts=ts),
            "REOPEN_SPEED": build_param_output("REOPEN_SPEED", self.last_reopen, normalize_linear(1.2 - self.last_reopen, 0.0, 1.2), False, 0, ts=ts),
            "FIXATION": build_param_output("FIXATION", fixation_s, normalize_linear(fixation_s, 3.0, 10.0), False, 0, ts=ts),
            "blink_detected": blink_detected,
        }


if __name__ == "__main__":
    c = Calibration(calibrated=True)
    p = OjosParametros()
    out = None
    for i in range(100):
        out = p.update(i * 0.1, 0.16 if i % 4 == 0 else 0.30, (100, 100), (120, 100), c)
    print(out["PERCLOS"])
