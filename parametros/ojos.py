"""Parametros oculares: PERCLOS (P80 FHWA), Tc, Fb, IBI, amplitud, velocidad reapertura, fijacion."""

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

        # --- PERCLOS segun el criterio P80 (FHWA): fraccion de tiempo con el
        # parpado cubriendo >=80% del ojo. Se mide con un umbral de EAR cercano
        # al cierre, independiente del umbral de parpadeo, para no alterar la
        # deteccion de blinks. Umbrales alineados con la literatura y
        # configurables por entorno para poder afinarlos con el arnes de validacion.
        self.perclos_close_frac = _envf("SOMNO_PERCLOS_CLOSE_FRAC", 0.80)  # P80
        self.perclos_closed_floor = _envf("SOMNO_PERCLOS_CLOSED_FLOOR", 0.08)  # EAR ojo cerrado
        self.perclos_onset = _envf("SOMNO_PERCLOS_ONSET", 0.15)  # umbral somnolencia (15%)
        self.perclos_severe = _envf("SOMNO_PERCLOS_SEVERE", 0.30)  # somnolencia marcada
        # Microsueno: cierre ocular sostenido. La literatura situa el inicio del
        # microsueno / lapso de atencion en >=500 ms. Configurable.
        self.microsleep_ms = _envf("SOMNO_MICROSLEEP_MS", 500.0)

        # Ajuste de umbral de cierre segun condiciones adversas. Con poca luz o
        # lentes, los landmarks del parpado son mas ruidosos y el EAR puede
        # caer sin que el ojo este realmente cerrado -> se exige un cierre algo
        # mas marcado (factor <1 sobre close_thr) para no generar falsos cierres.
        # El PERCLOS (temporal) sigue capturando el cierre sostenido real.
        self.night_close_scale = _envf("SOMNO_NIGHT_CLOSE_SCALE", 0.90)
        self.glasses_close_scale = _envf("SOMNO_GLASSES_CLOSE_SCALE", 0.90)
        # Con lentes, el ojo "cerrado" suele medir un EAR mas alto (reflejos,
        # montura): se sube el piso de cierre del PERCLOS.
        self.glasses_perclos_floor_bonus = _envf("SOMNO_GLASSES_PERCLOS_FLOOR", 0.02)

        self.eye_hist: Deque[Tuple[float, int]] = deque(maxlen=8000)
        self.blink_times: Deque[float] = deque(maxlen=3000)
        self.ibi_hist: Deque[float] = deque(maxlen=600)

        self.blink_active = False
        self.blink_start_ts: Optional[float] = None
        self.eye_closed_event_reported = False
        self.blink_min_ear = 1.0
        self.open_ref = 0.30
        self.last_tc_ms = 0.0
        self.last_amp = 0.0
        self.last_reopen = 0.0
        self.blink_metrics_event_pending = False

        self.last_center: Optional[np.ndarray] = None
        self.last_center_ts: Optional[float] = None
        self.fix_start_ts: Optional[float] = None
        # Instante de la primera observacion ocular. La tasa de parpadeo
        # (BLINK_FB) se mide en una ventana deslizante de 60s; recien arrancado
        # (o tras restaurar calibracion) la ventana esta VACIA -> fb=0 -> se
        # interpretaba como "muy pocos parpadeos" = somnolencia. Es un falso
        # positivo de arranque en frio. Se exige que la ventana lleve al menos
        # `perclos_window_s` observando antes de fiarse del lado bajo (fb<4).
        self._obs_start_ts: Optional[float] = None

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
        if self._obs_start_ts is None:
            self._obs_start_ts = float(ts)
        # Ventana de parpadeo "caliente": lleva suficiente tiempo observando como
        # para que fb_per_min sea significativo (no un cero de arranque en frio).
        blink_window_warm = (float(ts) - self._obs_start_ts) >= self.perclos_window_s
        # Endurecimiento del umbral de cierre en condiciones adversas (noche/lentes).
        close_scale = 1.0
        if getattr(calibration, "nightmode", False):
            close_scale *= self.night_close_scale
        if getattr(calibration, "glassesmode", False):
            close_scale *= self.glasses_close_scale

        # Referencia ADAPTATIVA de "ojo abierto": envolvente superior del EAR
        # (sube rapido, baja despacio). Se adapta al nivel real de operacion
        # (gaze, distancia, montura) en vez de fiarse solo del baseline de
        # calibracion, que puede no coincidir con la pose real y provocar cierres
        # FALSOS (EAR de reposo por debajo del umbral fijo). Solo se actualiza con
        # el ojo claramente abierto (ear > mitad de la referencia) para que un
        # cierre real NO arrastre la referencia hacia abajo.
        if reliable and ear > 0.5 * self.open_ref:
            if ear > self.open_ref:
                self.open_ref = 0.5 * self.open_ref + 0.5 * ear
            else:
                self.open_ref = 0.98 * self.open_ref + 0.02 * ear
        # Nivel de apertura de referencia: el mayor entre el adaptativo y un piso
        # del baseline (evita que caiga demasiado por ruido).
        open_level = max(self.open_ref, 0.6 * calibration.ear_baseline)
        close_thr = max(0.10, open_level * 0.70 * close_scale)
        open_thr = max(close_thr + 0.025, open_level * 0.82)
        perclos_floor = self.perclos_closed_floor + (
            self.glasses_perclos_floor_bonus if getattr(calibration, "glassesmode", False) else 0.0
        )

        # PERCLOS: solo se contabilizan frames con pose FIABLE. Girar la cara
        # escorza el ojo y colapsa el EAR; contar esos frames como "cerrado"
        # inflaba el PERCLOS con falsos positivos. Al excluirlos, la ventana
        # mantiene su ultimo estado fiable en vez de contaminarse.
        if reliable:
            open_ref_ear = max(open_level, perclos_floor + 0.02)
            p80_thr = perclos_floor + (1.0 - self.perclos_close_frac) * (open_ref_ear - perclos_floor)
            closed = 1 if ear <= p80_thr else 0
            self.eye_hist.append((float(ts), closed))
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
            self.blink_metrics_event_pending = False
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
                self.last_amp = max(0.0, self.open_ref - self.blink_min_ear)
                self.last_reopen = max(0.0, (ear - self.blink_min_ear) / dt)
                self.blink_metrics_event_pending = True
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

        immediate_eye_closed_event = eye_closed_ms >= self.microsleep_ms and not self.eye_closed_event_reported
        if immediate_eye_closed_event:
            self.eye_closed_event_reported = True
        blink_tc_event = self.blink_metrics_event_pending and calibration.calibrated and self.last_tc_ms >= 800.0
        blink_amplitude_event = self.blink_metrics_event_pending and calibration.calibrated and 0.0 < self.last_amp < 0.035
        reopen_speed_event = self.blink_metrics_event_pending and calibration.calibrated and 0.0 < self.last_reopen < 0.12
        self.blink_metrics_event_pending = False
        return {
            "EAR": build_param_output("EAR", ear, normalize_linear(max(0.0, calibration.ear_baseline - ear), 0.0, 0.20), reliable and calibration.calibrated and ear < close_thr, 2, ts=ts),
            "EYE_CLOSED_MS": build_param_output("EYE_CLOSED_MS", eye_closed_ms, normalize_linear(eye_closed_ms, self.microsleep_ms, 3000.0), immediate_eye_closed_event, 8 if eye_closed_ms < 1500.0 else 12, ts=ts),
            "PERCLOS": build_param_output("PERCLOS", perclos, normalize_linear(perclos, self.perclos_onset, self.perclos_severe * 2.0), calibration.calibrated and perclos >= self.perclos_onset, 10 if perclos < self.perclos_severe else 14, ts=ts),
            "BLINK_TC": build_param_output("BLINK_TC", self.last_tc_ms, normalize_linear(self.last_tc_ms, calibration.tc_baseline_ms, 700.0), blink_tc_event, 6, ts=ts),
            "BLINK_FB": build_param_output("BLINK_FB", fb_per_min, max(normalize_linear(fb_per_min, 0.0, 4.0), normalize_linear(fb_per_min, 24.0, 40.0)), calibration.calibrated and ((blink_window_warm and fb_per_min < 4.0) or fb_per_min > 32.0), 5, ts=ts),
            "IBI": build_param_output("IBI", ibi_s, normalize_linear(ibi_s, 4.0, 12.0), calibration.calibrated and ibi_s >= 8.0, 4, ts=ts),
            "BLINK_AMPLITUDE": build_param_output("BLINK_AMPLITUDE", self.last_amp, normalize_linear(self.last_amp, 0.02, 0.18), blink_amplitude_event, 3, ts=ts),
            "REOPEN_SPEED": build_param_output("REOPEN_SPEED", self.last_reopen, normalize_linear(1.2 - self.last_reopen, 0.0, 1.2), reopen_speed_event, 5, ts=ts),
            "FIXATION": build_param_output("FIXATION", fixation_s, normalize_linear(fixation_s, 3.0, 10.0), calibration.calibrated and fixation_s >= 8.0, 4, ts=ts),
            "blink_detected": blink_detected,
        }


if __name__ == "__main__":
    c = Calibration(calibrated=True)
    p = OjosParametros()
    out = None
    for i in range(100):
        out = p.update(i * 0.1, 0.16 if i % 4 == 0 else 0.30, (100, 100), (120, 100), c)
    print(out["PERCLOS"])
