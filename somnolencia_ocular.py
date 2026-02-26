"""Metricas oculares avanzadas para somnolencia.

Formulas:
- PERCLOS = (muestras con ojo cerrado) / (muestras totales en ventana)
- Fb (Hz) = parpadeos / segundos_ventana
- IBI (s) = promedio de intervalos entre timestamps de parpadeo
- Blink amplitude ~= EAR_open_ref - EAR_min_blink
- Re-opening speed ~= (EAR_post - EAR_min_blink) / dt_reapertura

Referencias clinicas:
- Johns, M. "A sleep physiologist's view of the drowsy driver", 2000.
- Wierwille et al. eye closure/PERCLOS as drowsiness correlate.
"""

from collections import deque
from typing import Deque, Dict, Optional, Tuple

import numpy as np


class OcularMetrics:
    def __init__(self, params) -> None:
        self.params = params
        self.eye_hist: Deque[Tuple[float, bool]] = deque(maxlen=6000)
        self.blink_times: Deque[float] = deque(maxlen=1000)
        self.ibi_hist: Deque[float] = deque(maxlen=400)
        self.reopen_speed_hist: Deque[float] = deque(maxlen=400)
        self.blink_active = False
        self.blink_start_ts: Optional[float] = None
        self.blink_min_ear = 1.0
        self.open_ear_ref = 0.30
        self.last_gaze: Optional[np.ndarray] = None
        self.last_gaze_ts: Optional[float] = None
        self.fixation_start_ts: Optional[float] = None

    def _update_fixation(self, ts: float, eye_left_center, eye_right_center) -> float:
        if eye_left_center is None or eye_right_center is None:
            self.last_gaze = None
            self.last_gaze_ts = None
            self.fixation_start_ts = None
            return 0.0

        gaze = (np.asarray(eye_left_center, dtype=np.float32) + np.asarray(eye_right_center, dtype=np.float32)) * 0.5
        if self.last_gaze is None or self.last_gaze_ts is None:
            self.last_gaze = gaze
            self.last_gaze_ts = ts
            self.fixation_start_ts = ts
            return 0.0

        dt = max(1e-6, ts - self.last_gaze_ts)
        speed = float(np.linalg.norm(gaze - self.last_gaze) / dt)
        if speed <= self.params.fixation_motion_threshold_px_s:
            if self.fixation_start_ts is None:
                self.fixation_start_ts = ts
        else:
            self.fixation_start_ts = ts

        self.last_gaze = gaze
        self.last_gaze_ts = ts
        return float(ts - self.fixation_start_ts) if self.fixation_start_ts is not None else 0.0

    def update(self, ts: float, ear: float, eye_left_center, eye_right_center) -> Dict[str, float]:
        closed = bool(ear < self.params.ear_threshold)
        self.eye_hist.append((ts, closed))
        while self.eye_hist and (ts - self.eye_hist[0][0]) > self.params.perclos_window_seconds:
            self.eye_hist.popleft()

        if ear > self.params.blink_open_threshold:
            self.open_ear_ref = 0.95 * self.open_ear_ref + 0.05 * ear

        blink_detected = False
        blink_amplitude = 0.0
        reopening_speed = 0.0
        tc = 0.0

        if closed and not self.blink_active:
            self.blink_active = True
            self.blink_start_ts = ts
            self.blink_min_ear = ear
        elif closed and self.blink_active:
            self.blink_min_ear = min(self.blink_min_ear, ear)
            tc = float(ts - self.blink_start_ts) if self.blink_start_ts is not None else 0.0
        elif (not closed) and self.blink_active:
            dt = float(ts - self.blink_start_ts) if self.blink_start_ts is not None else 0.0
            self.blink_active = False
            if self.blink_start_ts is not None:
                if self.params.blink_min_duration_s <= dt <= self.params.blink_max_duration_s:
                    blink_detected = True
                    self.blink_times.append(ts)
                    if len(self.blink_times) >= 2:
                        ibi = float(self.blink_times[-1] - self.blink_times[-2])
                        self.ibi_hist.append(ibi)
                    blink_amplitude = max(0.0, float(self.open_ear_ref - self.blink_min_ear))
                    reopening_speed = blink_amplitude / max(1e-3, dt * 0.5)
                    self.reopen_speed_hist.append(reopening_speed)
            self.blink_start_ts = None
            self.blink_min_ear = ear

        while self.blink_times and (ts - self.blink_times[0]) > self.params.perclos_window_seconds:
            self.blink_times.popleft()

        seconds_span = (
            max(1e-6, self.blink_times[-1] - self.blink_times[0]) if len(self.blink_times) >= 2 else self.params.perclos_window_seconds
        )
        fb_hz = float(len(self.blink_times) / seconds_span) if self.blink_times else 0.0
        ibi_s = float(np.mean(self.ibi_hist)) if self.ibi_hist else 0.0
        perclos = float(np.mean([1.0 if c else 0.0 for _, c in self.eye_hist])) if self.eye_hist else 0.0
        fixation_duration = self._update_fixation(ts, eye_left_center, eye_right_center)

        return {
            "perclos": perclos,
            "tc": float(tc),
            "fb_hz": fb_hz,
            "ibi_s": ibi_s,
            "blink_amplitude": float(blink_amplitude),
            "reopening_speed": float(reopening_speed),
            "fixation_duration": float(fixation_duration),
            "blink_detected": bool(blink_detected),
        }
