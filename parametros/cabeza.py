"""Parametros de cabeza: pose (pitch/roll/yaw), velocidad caida, recovery, micro-oscilaciones."""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Sequence, Tuple

import cv2
import numpy as np

from core.calibration import Calibration
from core.common_types import angle_delta_deg, build_param_output, normalize_linear

POSE_IDX = [1, 152, 33, 263, 61, 291]
MODEL_POINTS = np.array(
    [
        (0.0, 0.0, 0.0),
        (0.0, -330.0, -65.0),
        (-225.0, 170.0, -135.0),
        (225.0, 170.0, -135.0),
        (-150.0, -150.0, -125.0),
        (150.0, -150.0, -125.0),
    ],
    dtype=np.float64,
)


def _rotation_to_euler_deg(rot_matrix: np.ndarray) -> Tuple[float, float, float]:
    sy = np.sqrt(rot_matrix[0, 0] ** 2 + rot_matrix[1, 0] ** 2)
    singular = sy < 1e-6
    if not singular:
        x = np.arctan2(rot_matrix[2, 1], rot_matrix[2, 2])
        y = np.arctan2(-rot_matrix[2, 0], sy)
        z = np.arctan2(rot_matrix[1, 0], rot_matrix[0, 0])
    else:
        x = np.arctan2(-rot_matrix[1, 2], rot_matrix[1, 1])
        y = np.arctan2(-rot_matrix[2, 0], sy)
        z = 0.0
    return float(np.degrees(x)), float(np.degrees(y)), float(np.degrees(z))


class CabezaParametros:
    def __init__(self, micro_window_s: float = 6.0) -> None:
        self.prev_pitch: Optional[float] = None
        self.prev_ts: Optional[float] = None
        self.recovery_active = False
        self.recovery_start_ts: Optional[float] = None
        self.last_recovery = 0.0
        # HEAD_RECOVERY es un evento MOMENTANEO: se dispara solo en el frame en
        # que termina la recuperacion de un cabeceo, no de forma permanente. Antes
        # `last_recovery` se quedaba fijo (p.ej. 22s) y el evento se re-disparaba
        # en cada frame para siempre, fijando el score. Este flag lo consume una vez.
        self._recovery_event_pending = False
        # Ventana corta de pitch para medir la velocidad de cabeceo sobre ~0.35s
        # en vez de frame-a-frame. El solvePnP tiembla (picos de un frame) y una
        # velocidad instantanea disparaba HEAD_DROP falsos; sobre la ventana el
        # ruido promedia ~0 y solo una caida SOSTENIDA (cabeceo real) la supera.
        self._pitch_win: Deque[Tuple[float, float]] = deque(maxlen=60)
        self.pitch_hist: Deque[Tuple[float, float]] = deque(maxlen=6000)
        # Semilla temporal para solvePnP: reutilizar la pose previa estabiliza la
        # solucion (menos saltos/ambiguedad frame a frame) y por tanto reduce
        # falsos picos de velocidad de caida y micro-oscilacion.
        self._rvec: Optional[np.ndarray] = None
        self._tvec: Optional[np.ndarray] = None

    def _pose(self, landmarks: Sequence, frame_w: int, frame_h: int, rotation_index: int = 0) -> Tuple[float, float, float]:
        image_points = []
        for i in POSE_IDX:
            x = landmarks[i].x * frame_w
            y = landmarks[i].y * frame_h
            
            # Ajustar coordenadas según rotación para que sean equivalentes al sistema original
            if rotation_index == 1:  # 90 grados horario
                x, y = y, frame_w - x
            elif rotation_index == 2:  # 180 grados
                x, y = frame_w - x, frame_h - y
            elif rotation_index == 3:  # 90 grados antihorario (270 grados horario)
                x, y = frame_h - y, x
                
            image_points.append([x, y])
        
        image_points = np.asarray(image_points, dtype=np.float64)
        camera_matrix = np.asarray([[frame_w, 0, frame_w / 2.0], [0, frame_w, frame_h / 2.0], [0, 0, 1]], dtype=np.float64)
        use_guess = self._rvec is not None and self._tvec is not None
        ok, rvec, tvec = cv2.solvePnP(
            MODEL_POINTS,
            image_points,
            camera_matrix,
            np.zeros((4, 1), dtype=np.float64),
            rvec=self._rvec.copy() if use_guess else None,
            tvec=self._tvec.copy() if use_guess else None,
            useExtrinsicGuess=use_guess,
            flags=cv2.SOLVEPNP_ITERATIVE,
        )
        if not ok:
            return 0.0, 0.0, 0.0
        self._rvec, self._tvec = rvec, tvec
        rot_matrix, _ = cv2.Rodrigues(rvec)
        pitch, yaw, roll = _rotation_to_euler_deg(rot_matrix)
        # Ambiguedad de pose frontal en solvePnP: la descomposicion de Euler a
        # veces devuelve el roll "volteado" ~±180 (R[0,0]<0) en vez del giro
        # real ~0. Un conductor sentado NUNCA rueda la cabeza >90 grados, asi que
        # un |roll| cercano a 180 es el reflejo del solver, no un giro fisico. Se
        # pliega al rango fisico [-90, 90] restando/sumando 180 para eliminar el
        # falso giro de ~175 grados que disparaba ROLL de forma espuria.
        if roll > 90.0:
            roll -= 180.0
        elif roll < -90.0:
            roll += 180.0
        return pitch, yaw, roll

    def update(self, ts: float, landmarks: Sequence, frame_w: int, frame_h: int, calibration: Calibration, rotation_index: int = 0):
        pitch, yaw, roll = self._pose(landmarks, frame_w, frame_h, rotation_index)
        # Velocidad de cabeceo sobre una VENTANA corta (~0.35s), no frame a frame.
        # Diferencia circular (evita el wraparound ±180). El temblor de solvePnP
        # (picos de un frame) se promedia a ~0; un cabeceo real (caida sostenida)
        # sí supera el umbral. Esto elimina los HEAD_DROP falsos por jitter.
        self._pitch_win.append((float(ts), float(pitch)))
        while self._pitch_win and (ts - self._pitch_win[0][0]) > 0.35:
            self._pitch_win.popleft()
        velocity = 0.0
        if len(self._pitch_win) >= 3:
            t0, p0 = self._pitch_win[0]
            velocity = angle_delta_deg(pitch, p0) / max(1e-3, ts - t0)
        self.prev_pitch = pitch
        self.prev_ts = ts

        pitch_delta = angle_delta_deg(pitch, calibration.pitch_neutral)
        if pitch_delta >= 24.0 and not self.recovery_active:
            self.recovery_active = True
            self.recovery_start_ts = ts
        elif self.recovery_active and pitch_delta <= 12.0:
            self.last_recovery = max(0.0, ts - (self.recovery_start_ts if self.recovery_start_ts is not None else ts))
            self.recovery_active = False
            self.recovery_start_ts = None
            self._recovery_event_pending = True

        self.pitch_hist.append((ts, pitch))
        while self.pitch_hist and (ts - self.pitch_hist[0][0]) > 6.0:
            self.pitch_hist.popleft()

        micro_value = 0.0
        if len(self.pitch_hist) >= 24:
            t = np.asarray([k for k, _ in self.pitch_hist], dtype=np.float64)
            x = np.asarray([v for _, v in self.pitch_hist], dtype=np.float64)
            x = x - np.mean(x)
            dt = np.median(np.diff(t)) if t.size > 1 else 0.0
            if dt > 0:
                spec = np.abs(np.fft.rfft(x))
                freqs = np.fft.rfftfreq(x.size, d=dt)
                band = (freqs >= 2.5) & (freqs <= 3.5)
                total = float(np.sum(spec[1:] ** 2)) if spec.size > 2 else 0.0
                micro_value = float(np.sum(spec[band] ** 2) / total) if total > 1e-9 else 0.0

        recovery_event = calibration.calibrated and self._recovery_event_pending and self.last_recovery >= 2.3
        self._recovery_event_pending = False

        return {
            "PITCH": build_param_output("PITCH", pitch, normalize_linear(abs(pitch_delta), 12.0, 30.0), calibration.calibrated and abs(pitch_delta) >= 24.0, 5, ts=ts),
            "ROLL": build_param_output("ROLL", roll, normalize_linear(abs(angle_delta_deg(roll, calibration.roll_neutral)), 12.0, 40.0), calibration.calibrated and abs(angle_delta_deg(roll, calibration.roll_neutral)) >= 32.0, 4, ts=ts),
            "YAW": build_param_output("YAW", yaw, normalize_linear(abs(angle_delta_deg(yaw, calibration.yaw_neutral)), 15.0, 45.0), calibration.calibrated and abs(angle_delta_deg(yaw, calibration.yaw_neutral)) >= 40.0, 2, ts=ts),
            "HEAD_DROP_VELOCITY": build_param_output("HEAD_DROP_VELOCITY", velocity, normalize_linear(abs(velocity), 12.0, 45.0), calibration.calibrated and 25.0 <= velocity <= 130.0, 6, ts=ts),
            "HEAD_RECOVERY": build_param_output("HEAD_RECOVERY", self.last_recovery, normalize_linear(self.last_recovery, 0.8, 3.5), recovery_event, 5, ts=ts),
            "HEAD_MICRO_OSC": build_param_output("HEAD_MICRO_OSC", micro_value, normalize_linear(micro_value, 0.3, 0.8), calibration.calibrated and micro_value >= 0.55, 0, ts=ts),
        }


if __name__ == "__main__":
    print("CabezaParametros requiere landmarks reales")
