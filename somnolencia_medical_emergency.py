"""Pipeline independiente para emergencias medicas.

No mezcla puntuacion de somnolencia. Solo produce banderas criticas.

Incluye:
- Sudden Loss of Consciousness
- Convulsive Episode (FFT de pitch)
- Stroke por asimetria facial abrupta
- Lateral Collapse (roll sostenido)
- Dissociation/Absence (ojos abiertos, sin blink, mirada fija)
- Face Out of Frame sin justificacion por yaw
"""

from collections import deque
from threading import Event, Lock, Thread
from typing import Deque, Dict, Optional, Tuple

import numpy as np


class MedicalEmergencyPipeline:
    def __init__(self, params) -> None:
        self.params = params
        self._in_lock = Lock()
        self._out_lock = Lock()
        self._latest_input: Optional[Dict] = None
        self._latest_output: Dict = self._empty_output()
        self._stop = Event()
        self._thread = Thread(target=self._worker, daemon=True)

        self.pitch_hist: Deque[Tuple[float, float]] = deque(maxlen=8000)
        self.asym_start_ts: Optional[float] = None
        self.lateral_start_ts: Optional[float] = None
        self.face_out_start_ts: Optional[float] = None
        self.last_face_ts: Optional[float] = None
        self.last_yaw: float = 0.0
        self.last_blink_ts: Optional[float] = None
        self._thread.start()

    @staticmethod
    def _empty_output() -> Dict:
        return {
            "medical_alert": False,
            "medical_reasons": [],
            "sudden_loss_of_consciousness": False,
            "convulsive_episode": False,
            "stroke_asymmetry": False,
            "lateral_collapse": False,
            "dissociation_absence": False,
            "face_out_of_frame_no_yaw": False,
        }

    def submit(self, sample: Dict) -> None:
        with self._in_lock:
            self._latest_input = sample

    def get_latest(self) -> Dict:
        with self._out_lock:
            return dict(self._latest_output)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _worker(self) -> None:
        while not self._stop.is_set():
            sample = None
            with self._in_lock:
                sample = self._latest_input
                self._latest_input = None
            if sample is None:
                self._stop.wait(0.01)
                continue
            out = self._process(sample)
            with self._out_lock:
                self._latest_output = out

    def _convulsive_from_pitch(self, ts: float) -> bool:
        while self.pitch_hist and (ts - self.pitch_hist[0][0]) > self.params.emg_convulsive_window_seconds:
            self.pitch_hist.popleft()
        if len(self.pitch_hist) < 24:
            return False

        t = np.asarray([k for k, _ in self.pitch_hist], dtype=np.float64)
        x = np.asarray([v for _, v in self.pitch_hist], dtype=np.float64)
        x = x - np.mean(x)
        dt = np.median(np.diff(t)) if t.size > 1 else 0.0
        if dt <= 0:
            return False
        fs = 1.0 / dt
        spec = np.abs(np.fft.rfft(x))
        freqs = np.fft.rfftfreq(x.size, d=dt)
        if spec.size <= 2:
            return False

        band = (freqs >= self.params.emg_convulsive_min_hz) & (freqs <= self.params.emg_convulsive_max_hz)
        band_power = float(np.sum(spec[band] ** 2))
        total_power = float(np.sum(spec[1:] ** 2))
        if total_power <= 1e-9:
            return False
        ratio = band_power / total_power
        return bool(ratio >= self.params.emg_convulsive_power_ratio and fs >= 10.0)

    def _process(self, sample: Dict) -> Dict:
        ts = float(sample.get("ts", 0.0))
        face_detected = bool(sample.get("face_detected", False))
        pitch = float(sample.get("pitch", 0.0))
        yaw = float(sample.get("yaw", 0.0))
        roll = float(sample.get("roll", 0.0))
        eye_open = bool(sample.get("eye_open", True))
        eye_closed_time = float(sample.get("eye_closed_time", 0.0))
        fixation_duration = float(sample.get("fixation_duration", 0.0))
        asymmetry = float(sample.get("facial_asymmetry_index", 0.0))
        blink_detected = bool(sample.get("blink_detected", False))

        reasons = []
        out = self._empty_output()

        self.pitch_hist.append((ts, pitch))
        self.last_yaw = yaw

        if face_detected:
            self.last_face_ts = ts
            self.face_out_start_ts = None
        else:
            if self.face_out_start_ts is None:
                self.face_out_start_ts = ts

        if blink_detected:
            self.last_blink_ts = ts

        sudden_loc = eye_closed_time >= self.params.emg_loc_eye_closed_seconds and pitch >= self.params.head_recovery_start_pitch
        if sudden_loc:
            out["sudden_loss_of_consciousness"] = True
            reasons.append("Sudden loss of consciousness pattern")

        convulsive = self._convulsive_from_pitch(ts)
        if convulsive:
            out["convulsive_episode"] = True
            reasons.append("Convulsive pitch rhythm detected")

        if asymmetry >= self.params.emg_stroke_asymmetry_threshold:
            if self.asym_start_ts is None:
                self.asym_start_ts = ts
        else:
            self.asym_start_ts = None
        stroke = self.asym_start_ts is not None and (ts - self.asym_start_ts) >= self.params.emg_stroke_sustain_seconds
        if stroke:
            out["stroke_asymmetry"] = True
            reasons.append("Sustained facial asymmetry")

        if abs(roll) >= self.params.emg_lateral_roll_threshold:
            if self.lateral_start_ts is None:
                self.lateral_start_ts = ts
        else:
            self.lateral_start_ts = None
        lateral = self.lateral_start_ts is not None and (ts - self.lateral_start_ts) >= self.params.emg_lateral_sustain_seconds
        if lateral:
            out["lateral_collapse"] = True
            reasons.append("Lateral collapse pattern")

        no_blink_elapsed = float(ts - self.last_blink_ts) if self.last_blink_ts is not None else 0.0
        dissociation = eye_open and no_blink_elapsed >= self.params.emg_absence_no_blink_seconds and fixation_duration >= self.params.emg_absence_fixation_seconds
        if dissociation:
            out["dissociation_absence"] = True
            reasons.append("Absence-like fixed gaze with no blink")

        face_out = (
            (not face_detected)
            and self.face_out_start_ts is not None
            and (ts - self.face_out_start_ts) >= self.params.emg_face_out_seconds
            and abs(self.last_yaw) < self.params.emg_face_out_yaw_justification
        )
        if face_out:
            out["face_out_of_frame_no_yaw"] = True
            reasons.append("Face out of frame without yaw justification")

        out["medical_reasons"] = reasons
        out["medical_alert"] = bool(reasons)
        return out
