#!/usr/bin/env python3
"""Main industrial para deteccion de somnolencia en Raspberry Pi 4.

Estructura simplificada por tipo de parametro dentro de carpeta `parametros/`.

Se puede ejecutar de cualquiera de estas formas; todas se re-lanzan solas con
el interprete del venv (.venv/bin/python):
    python3 main.py
    ./main.py
    (boton "Run" del editor)
"""

from __future__ import annotations

import os
import queue
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# --- Auto-relanzado con el interprete del venv ---------------------------
# El sistema depende de paquetes que solo estan en el venv de Python 3.12
# (.venv/): cv2, mediapipe, picamera2, etc. Si este archivo se ejecuta con
# otro Python (por ejemplo `python3 main.py` con el 3.13 del sistema), se
# re-lanza a si mismo usando .venv/bin/python para que "ejecutar main.py"
# siempre funcione, sin importar como se invoque.
import sys as _sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENV_DIR = os.path.join(_HERE, ".venv")
_VENV_PY = os.path.join(_VENV_DIR, "bin", "python")
if os.path.abspath(_sys.prefix) != _VENV_DIR and os.path.exists(_VENV_PY):
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__)] + _sys.argv[1:])
# -------------------------------------------------------------------------

import cv2
import mediapipe as mp
import numpy as np
from dotenv import load_dotenv

from output.alertdispatcher import AlertDispatcher
from output.buzzer import Buzzer
from core.alertmemory import AlertMemory
from core.calibration import Calibration
from core.config import AppConfig
from engine.emergencydetector import detect_emergency
from engine.corroboration import LevelStabilizer, StabilizerConfig, family_of
from core.eventstore import EventStore
from core.scorestate import ScoreStateStore
from core.calibrationstore import CalibrationStore
from core.common_types import CircularMeanDeg, angle_delta_deg
from engine.fatiguescore import DynamicFatigueScore
from output.mqttpublisher import MqttPublisher
from parametros.boca import BocaParametros
from parametros.cabeza import CabezaParametros
from parametros.contexto import ContextoParametros
from parametros.facial import FacialParametros
from parametros.manos import ManosParametros
from parametros.ojos import OjosParametros
from engine.ruleengine import RuleEngine
from somnolencia_core import BOCA, OJO_DER, OJO_IZQ, eye_metrics, fuse_ear, get_ear, get_mar
from storage.supabasesync import SupabaseSync
from storage.session_recorder import SessionRecorder, build_record
from camera_setup import describe_camera_environment, list_opencv_candidates, setup_camera


@dataclass
class RuntimeState:
    session_id: str
    started_at: float
    last_minute_flush: float
    last_telemetry_persist: float = 0.0
    last_session_sync: float = 0.0
    last_score_state_persist: float = 0.0
    last_score_state_value: int = -1


class HandsWorker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._stop_event = threading.Event()
        self._in_queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=1)
        self._latest = None
        self._lock = threading.Lock()
        self.hands = mp.solutions.hands.Hands(
            static_image_mode=False,
            max_num_hands=2,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )

    def submit(self, rgb_frame: np.ndarray) -> None:
        try:
            self._in_queue.put_nowait(rgb_frame)
        except queue.Full:
            pass

    def latest(self):
        with self._lock:
            return self._latest

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                rgb = self._in_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            out = self.hands.process(rgb)
            with self._lock:
                self._latest = out

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=1.0)
        self.hands.close()


class SomnolenciaSystem:
    WINDOW_NAME = "Somnolencia Main"
    # Reducido a 480p para mejorar latencia/deteccion en Raspberry.
    CAPTURE_WIDTH = 640
    CAPTURE_HEIGHT = 480
    # Lado mayor del frame para MediaPipe. CRITICO: el redimensionado PRESERVA la
    # relacion de aspecto. Antes se forzaba 320x180 (16:9) sobre una captura 4:3
    # (640x480), lo que ESTIRABA la cara ~33% y distorsionaba EAR/MAR/asimetria y
    # los puntos 2D de solvePnP (pose inestable). Nunca volver a un tamano fijo.
    MP_PROC_LONG = 320
    HANDS_EVERY_N_FRAMES = 4
    DISPLAY_INTERVAL_S = 1.0 / 15.0  # limita display a 15 fps maximo

    def __init__(self, config: AppConfig) -> None:
        self.cfg = config
        self.calibration = Calibration()
        # Modo lentes: manual por entorno (la autodeteccion fiable de lentes no
        # es viable). Ajusta umbrales oculares. El modo noche se autodetecta por
        # iluminacion en parametros/contexto.py.
        self.calibration.glassesmode = os.getenv("SOMNO_GLASSES", "0").strip().lower() in ("1", "true", "yes", "on")
        # Persistencia de calibracion por conductor: si hay una reciente y valida
        # se restaura y se evita la ventana de 5 min desprotegida al arrancar.
        self.calibration_store = CalibrationStore(config.sqlite_queue_path, config.vehicle_id, config.driver_id)
        self.calibration_max_age_s = float(os.getenv("SOMNO_CALIB_MAX_AGE_S", str(7 * 24 * 3600)))
        force_calib = os.getenv("SOMNO_FORCE_CALIB", "0").strip().lower() in ("1", "true", "yes", "on")
        if not force_calib:
            saved_calib = self.calibration_store.load(max_age_s=self.calibration_max_age_s)
            # Guarda: una calibracion real NUNCA deja los tres neutros de pose en
            # exactamente 0 (el pitch/roll crudos de solvePnP no estan centrados
            # en 0). Si estan a 0 es una "no-calibracion" (defaults) y se descarta
            # para forzar recalibracion, evitando arrastrar un pitch_delta espurio.
            pose_untouched = saved_calib and (
                abs(float(saved_calib.get("pitch_neutral", 0.0))) < 1.0
                and abs(float(saved_calib.get("yaw_neutral", 0.0))) < 1.0
                and abs(float(saved_calib.get("roll_neutral", 0.0))) < 1.0
            )
            if saved_calib and not pose_untouched:
                self.calibration.restore(saved_calib)
                self.calibration.calibrated = True
                print(
                    "[CALIB] Calibracion restaurada "
                    f"ear={self.calibration.ear_baseline:.3f} mar={self.calibration.mar_baseline:.3f} "
                    f"pitch0={self.calibration.pitch_neutral:.1f} yaw0={self.calibration.yaw_neutral:.1f}"
                )
            elif pose_untouched:
                print("[CALIB] Calibracion guardada invalida (neutros de pose en 0), se recalibrara")
        self.event_store = EventStore(config.sqlite_queue_path)
        self.score = DynamicFatigueScore()
        self.score_state_store = ScoreStateStore(config.sqlite_queue_path, config.vehicle_id, config.driver_id)
        saved_score_state = self.score_state_store.load()
        if saved_score_state:
            self.score.restore(saved_score_state)
            print(
                "[SCORE] Estado restaurado "
                f"score={self.score.score} max={self.score.max_score_seen} alertas={self.score.alert_count}"
            )

        self.ojos = OjosParametros()
        self.boca = BocaParametros()
        self.cabeza = CabezaParametros()
        self.facial = FacialParametros()
        self.manos = ManosParametros()
        self.contexto = ContextoParametros()

        self.mqtt = MqttPublisher(config)
        self.supabase = SupabaseSync(config)
        self.buzzer = Buzzer(pin=17, active_high=True, enabled=True)
        self.dispatcher = AlertDispatcher(self.buzzer, self.mqtt)
        self.rule_engine = RuleEngine(self.event_store)
        self.level_stabilizer = LevelStabilizer(StabilizerConfig.from_env(os.getenv))
        # Estabilidad minima de landmarks para aceptar el rostro como valido
        # (evita falsos positivos por caras espurias en texturas de fondo).
        self.face_quality_min = float(os.getenv("SOMNO_FACE_QUALITY_MIN", "0.30"))
        # Periodo de gracia ante perdida breve de rostro: durante estos segundos
        # NO se decae el score ni se relaja la histeresis del nivel, de modo que
        # quitar la cara y volver a ponerla no borra los datos acumulados.
        self.face_grace_s = float(os.getenv("SOMNO_FACE_GRACE_S", "4.0"))
        self._face_lost_since: float | None = None
        # Medias CIRCULARES para el neutro de pose. El pitch/roll de solvePnP
        # rondan ±180 y saltan el wraparound; una EMA lineal da un neutro basura
        # (promediar +170 y -170 da 0). La media circular via sin/cos lo resuelve.
        self._pitch_mean = CircularMeanDeg(0.01)
        self._roll_mean = CircularMeanDeg(0.01)
        self._yaw_mean = CircularMeanDeg(0.01)
        # Umbrales de fiabilidad por pose de cabeza (grados, respecto al neutro
        # calibrado). Fuera de estos rangos la geometria 2D de ojos/boca se
        # escorza y NO es confiable, asi que se suprimen sus eventos para evitar
        # falsos positivos al virar/inclinar la cara. Configurable por entorno.
        self.yaw_ocular_max = float(os.getenv("SOMNO_YAW_OCULAR_MAX", "38.0"))
        self.pitch_ocular_max = float(os.getenv("SOMNO_PITCH_OCULAR_MAX", "28.0"))
        self.roll_ocular_max = float(os.getenv("SOMNO_ROLL_OCULAR_MAX", "35.0"))
        self.yaw_mouth_max = float(os.getenv("SOMNO_YAW_MOUTH_MAX", "45.0"))
        self.pitch_mouth_max = float(os.getenv("SOMNO_PITCH_MOUTH_MAX", "32.0"))
        # Iluminacion minima DE LA CARA (recuadro facial, no del fondo) para
        # fiarse de las metricas oculares/faciales. Por debajo (cara realmente a
        # oscuras) los landmarks son ruido y generan falsos positivos. Con un
        # fondo oscuro pero la cara iluminada, esto ya NO suprime nada.
        self.min_illumination = float(os.getenv("SOMNO_MIN_ILLUMINATION", "0.12"))
        # Transicion de la fusion de EAR entre ojos segun yaw (grados).
        self.ear_yaw_soft = float(os.getenv("SOMNO_EAR_YAW_SOFT", "12.0"))
        self.ear_yaw_hard = float(os.getenv("SOMNO_EAR_YAW_HARD", "30.0"))
        # Distraccion / mirada fuera de via: un giro (yaw) sostenido no es fatiga,
        # pero SI es distraccion. Se marca como senal independiente (no altera el
        # score de somnolencia). Umbrales configurables por entorno.
        self.distraction_enabled = os.getenv("SOMNO_DISTRACTION_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
        self.distraction_yaw = float(os.getenv("SOMNO_DISTRACTION_YAW", "40.0"))
        self.distraction_min_s = float(os.getenv("SOMNO_DISTRACTION_MIN_S", "2.5"))
        # Aviso audible de distraccion: chirp breve distinto del de fatiga, con
        # re-aviso periodico mientras la mirada siga fuera de via.
        self.distraction_buzzer = os.getenv("SOMNO_DISTRACTION_BUZZER", "1").strip().lower() in ("1", "true", "yes", "on")
        self.distraction_rechirp_s = float(os.getenv("SOMNO_DISTRACTION_RECHIRP_S", "3.0"))
        self._distraction_since: float | None = None
        self._distraction_reported = False
        self._distraction_last_chirp = 0.0
        self.hands_enabled = os.getenv("SOMNO_HANDS_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
        self.hands_worker = HandsWorker() if self.hands_enabled else None
        self.alert_memory = AlertMemory()
        self._active_param_events: dict[str, float] = {}
        self._last_emergency_type: str | None = None
        self._last_emergency_started_at: float | None = None
        self._minute_samples: list[dict] = []

        # Iris/refine_landmarks: refina parpados+iris -> EAR mas estable y menos
        # falsos cierres. Cuesta algo de CPU; si el FPS cae mucho en la Pi se
        # puede desactivar con SOMNO_REFINE_LANDMARKS=0.
        self.refine_landmarks = os.getenv("SOMNO_REFINE_LANDMARKS", "1").strip().lower() in ("1", "true", "yes", "on")
        self.face_mesh = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=False,
            refine_landmarks=self.refine_landmarks,
            max_num_faces=1,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self.exit_requested = False
        self._exit_button_rect = (0, 0, 0, 0)
        self._rotate_button_rect = (0, 0, 0, 0)
        self.rotation_index = self._parse_rotation(os.getenv("CAMERA_ROTATION", "0"))
        self.display_enabled = os.getenv("SOMNO_DISPLAY_ENABLED", "1").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    def _handle_mouse(self, event, x, y, _flags, _userdata) -> None:
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        x1, y1, x2, y2 = self._exit_button_rect
        if x1 <= x <= x2 and y1 <= y <= y2:
            self.exit_requested = True
            return
        x1, y1, x2, y2 = self._rotate_button_rect
        if x1 <= x <= x2 and y1 <= y <= y2:
            self._set_rotation((self.rotation_index + 1) % 4, "manual")

    def _draw_exit_button(self, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        margin = 12
        button_w = 110
        button_h = 38

        rx1 = margin
        ry1 = margin
        rx2 = rx1 + button_w
        ry2 = ry1 + button_h
        self._rotate_button_rect = (rx1, ry1, rx2, ry2)
        cv2.rectangle(frame, (rx1, ry1), (rx2, ry2), (180, 120, 20), -1)
        cv2.rectangle(frame, (rx1, ry1), (rx2, ry2), (255, 255, 255), 1)
        cv2.putText(frame, f"GIRO {self.rotation_index * 90}", (rx1 + 8, ry1 + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255, 255, 255), 2)

        x1 = max(margin, w - button_w - margin)
        y1 = margin
        x2 = x1 + button_w
        y2 = y1 + button_h
        self._exit_button_rect = (x1, y1, x2, y2)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (20, 20, 220), -1)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 255, 255), 1)
        cv2.putText(frame, "SALIR", (x1 + 24, y1 + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)

    @staticmethod
    def _param_color(normalized: float, event: bool) -> tuple:
        """Color del parametro: rojo si evento, amarillo si normalizado alto, verde si bajo."""
        if event:
            return (0, 0, 255)  # rojo BGR
        if normalized >= 0.7:
            return (0, 180, 255)  # naranja BGR
        if normalized >= 0.4:
            return (0, 255, 255)  # amarillo BGR
        return (200, 200, 200)  # gris claro

    @staticmethod
    def _parse_rotation(raw_value: str) -> int:
        try:
            value = int(str(raw_value).strip())
        except (TypeError, ValueError):
            return 0
        if value in (0, 90, 180, 270):
            return (value // 90) % 4
        return value % 4

    def _set_rotation(self, rotation_index: int, source: str) -> None:
        rotation_index = int(rotation_index) % 4
        if rotation_index == self.rotation_index:
            return
        self.rotation_index = rotation_index
        print(f"[CAM] Rotacion cambiada a {self.rotation_index * 90} grados ({source})")

    @staticmethod
    def _draw_parameters_panel(frame: np.ndarray, params: list[dict]) -> None:
        if not params:
            return

        h, w = frame.shape[:2]
        x1 = 12
        y1 = 44
        panel_w = min(520, max(320, w - 24))
        line_h = 18
        max_lines = max(6, int((h - y1 - 12) / line_h) - 1)

        # Mostrar eventos activos primero, luego por paramid
        params_sorted = sorted(params, key=lambda p: (not p.get("eventflag", False), p.get("paramid", "")))
        visible = params_sorted[:max_lines]
        panel_h = (len(visible) + 1) * line_h + 10
        x2 = x1 + panel_w
        y2 = min(h - 8, y1 + panel_h)

        overlay = frame.copy()
        cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.5, frame, 0.5, 0.0, frame)

        active_count = sum(1 for p in params if p.get("eventflag", False))
        header_color = (0, 0, 255) if active_count > 0 else (0, 255, 255)
        cv2.putText(frame, f"PARAMETROS ({active_count} activos)", (x1 + 8, y1 + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, header_color, 1)
        y = y1 + 34
        for p in visible:
            pid = str(p.get("paramid", "-"))
            value = float(p.get("value", 0.0))
            normalized = float(p.get("normalized", 0.0))
            event = bool(p.get("eventflag", False))
            marker = "!" if event else " "
            color = SomnolenciaSystem._param_color(normalized, event)
            bar_len = int(normalized * 12)
            bar = "|" * bar_len + "." * (12 - bar_len)
            text = f"{marker}{pid:17s} {value:7.3f} [{bar}] {normalized:.2f}"
            cv2.putText(frame, text, (x1 + 8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.40, color, 1)
            y += line_h

        hidden = len(params_sorted) - len(visible)
        if hidden > 0 and y + 4 < y2:
            cv2.putText(frame, f"... y {hidden} mas", (x1 + 8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (200, 200, 200), 1)

    @staticmethod
    def _try_open_camera(index: int | str) -> cv2.VideoCapture | None:
        # Bajo el shim V4L2 de libcamera (libcamerify) OpenCV entrega un buffer
        # plano (1, W*H*3) si no se fija el fourcc; YUYV fuerza la negociacion a
        # BGR de 3 canales. Se deja el intento sin fourcc como respaldo para
        # camaras USB que solo exponen MJPG.
        for fourcc in ("YUYV", None):
            cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(index)
            if not cap.isOpened():
                cap.release()
                continue
            if fourcc is not None:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, SomnolenciaSystem.CAPTURE_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, SomnolenciaSystem.CAPTURE_HEIGHT)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            for _ in range(30):
                ok, frame = cap.read()
                if ok and frame is not None and frame.ndim == 3:
                    return cap
                time.sleep(0.1)
            cap.release()
        return None

    @staticmethod
    def _camera_candidates(preferred: int) -> list[int | str]:
        candidates: list[int | str] = [preferred]

        for idx in range(5):
            if idx != preferred:
                candidates.append(idx)

        for device_path in list_opencv_candidates():
            candidates.append(device_path)
            suffix = device_path.removeprefix("/dev/video")
            if suffix.isdigit():
                candidates.append(int(suffix))

        unique: list[int | str] = []
        seen: set[int | str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            unique.append(candidate)
        return unique

    @staticmethod
    def _read_opencv_frame(cap: cv2.VideoCapture) -> tuple[bool, np.ndarray | None]:
        ok, frame = cap.read()
        if not ok or frame is None:
            return False, None
        return True, frame

    @staticmethod
    def _read_picamera_frame(picam2) -> tuple[bool, np.ndarray | None]:
        try:
            frame_raw = picam2.capture_array()
        except Exception:
            return False, None
        if frame_raw is None:
            return False, None
        # Sin conversion/manipulacion de color:
        # Si llega en 4 canales (por ejemplo XBGR/RGBA), no se transforma aqui.
        # if frame_raw.ndim == 3 and frame_raw.shape[2] == 4:
        #     return True, np.ascontiguousarray(frame_raw[:, :, :3])
        if frame_raw.ndim == 3 and frame_raw.shape[2] == 3:
            return True, np.ascontiguousarray(frame_raw)
        return False, None

    @staticmethod
    def _apply_rotation(frame: np.ndarray, rotation_index: int) -> np.ndarray:
        rotation_index = int(rotation_index) % 4
        if rotation_index == 1:
            return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        if rotation_index == 2:
            return cv2.rotate(frame, cv2.ROTATE_180)
        if rotation_index == 3:
            return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return frame

    @staticmethod
    def _build_mediapipe_frame(frame: np.ndarray) -> np.ndarray:
        # Escala PRESERVANDO la relacion de aspecto (lado mayor -> MP_PROC_LONG).
        # Estirar el frame distorsiona toda la geometria facial y la pose.
        h, w = frame.shape[:2]
        long_side = max(w, h)
        target = SomnolenciaSystem.MP_PROC_LONG
        if long_side <= target:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        scale = float(target) / float(long_side)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        return cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

    # Landmarks del contorno del rostro para acotar el recuadro de la cara.
    _FACE_BOX_IDX = (10, 152, 234, 454, 33, 263, 61, 291)

    @staticmethod
    def _face_illumination(mp_frame: np.ndarray, lm, mp_w: int, mp_h: int) -> float:
        """Brillo medio (0-1) del RECUADRO de la cara, no de todo el frame.

        La media global se hunde con un fondo oscuro aunque la cara este bien
        iluminada; medir solo la region facial refleja la luz real sobre el rostro.
        """
        xs = [lm[i].x for i in SomnolenciaSystem._FACE_BOX_IDX]
        ys = [lm[i].y for i in SomnolenciaSystem._FACE_BOX_IDX]
        x0 = max(0, int(min(xs) * mp_w)); x1 = min(mp_w, int(max(xs) * mp_w) + 1)
        y0 = max(0, int(min(ys) * mp_h)); y1 = min(mp_h, int(max(ys) * mp_h) + 1)
        if x1 <= x0 or y1 <= y0:
            return 0.0
        crop = mp_frame[y0:y1, x0:x1]
        return float(crop.mean()) / 255.0 if crop.size else 0.0

    @staticmethod
    def _iso_ts(ts: float) -> str:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()

    @staticmethod
    def _param_value(param_outputs: list[dict], param_id: str, default: float = 0.0) -> float:
        for item in param_outputs:
            if item.get("paramid") == param_id:
                return float(item.get("value", default))
        return float(default)

    @staticmethod
    def _avg(samples: list[dict], key: str) -> float:
        values = [float(s.get(key, 0.0)) for s in samples if key in s]
        return float(sum(values) / len(values)) if values else 0.0

    def _sync_session(self, state: RuntimeState, ts: float, score_out: dict, is_final: bool = False) -> None:
        payload = {
            "session_id": state.session_id,
            "vehicle_id": self.cfg.vehicle_id,
            "driver_id": self.cfg.driver_id,
            "start_time": self._iso_ts(state.started_at),
            "end_time": self._iso_ts(ts) if is_final else None,
            "max_fatigue": int(score_out.get("max_fatigue", score_out.get("fatigue_score", 0))),
            "alert_count": int(score_out.get("alert_count", 0)),
        }
        self.supabase.enqueue_upsert("sessions", payload, conflict_target="session_id", immediate=is_final)

    def _persist_telemetry(self, telemetry: dict, ts: float, immediate: bool = False) -> None:
        payload = {
            "session_id": telemetry.get("session_id"),
            "ts": self._iso_ts(ts),
            "vehicle_id": telemetry.get("v"),
            "driver_id": telemetry.get("d"),
            "payload": telemetry,
        }
        self.supabase.enqueue("telemetry_raw", payload, immediate=immediate)

    def _persist_param_events(self, telemetry: dict, param_outputs: list[dict], score_out: dict, ts: float) -> None:
        active_now = {str(p.get("paramid")) for p in param_outputs if bool(p.get("eventflag", False)) and p.get("paramid")}
        for p in param_outputs:
            param_id = str(p.get("paramid", ""))
            if not param_id or not bool(p.get("eventflag", False)):
                continue
            if param_id in self._active_param_events:
                continue
            self._active_param_events[param_id] = ts
            event_payload = {
                "session_id": telemetry.get("session_id"),
                "ts": self._iso_ts(ts),
                "event_type": param_id,
                "severity_level": int(telemetry.get("alerts", {}).get("level", 0)),
                "param_id": param_id,
                "param_value": float(p.get("value", 0.0)),
                "duration_ms": 0,
                "fatigue_score": int(score_out.get("fatigue_score", 0)),
                "payload": {
                    "telemetry_ts": telemetry.get("ts"),
                    "normalized": float(p.get("normalized", 0.0)),
                    "fatiguescoredelta": int(p.get("fatiguescoredelta", 0)),
                    "alert_memory": telemetry.get("alert_memory", {}),
                },
            }
            self.supabase.enqueue("events", event_payload, immediate=False)
        for param_id in list(self._active_param_events.keys()):
            if param_id not in active_now:
                self._active_param_events.pop(param_id, None)

    def _persist_emergency(self, telemetry: dict, emergency: dict, ts: float) -> None:
        emergency_type = emergency.get("emergencytype")
        emergency_flag = bool(emergency.get("emergencyflag", False))
        if emergency_flag:
            if emergency_type != self._last_emergency_type:
                self._last_emergency_type = emergency_type
                self._last_emergency_started_at = ts
                payload = {
                    "session_id": telemetry.get("session_id"),
                    "ts": self._iso_ts(ts),
                    "emergency_type": emergency_type or "UNKNOWN",
                    "trigger_params": {
                        "reasons": emergency.get("reasons", []),
                        "fatigue_score": telemetry.get("score", {}).get("fatigue_score", 0),
                        "level": telemetry.get("score", {}).get("level", 0),
                        "alert_memory": telemetry.get("alert_memory", {}),
                    },
                    "duration_seconds": 0.0,
                    "resolved_at": None,
                    "resolution_type": None,
                    "payload": telemetry,
                }
                self.supabase.enqueue("emergency_alerts", payload, immediate=True)
        else:
            self._last_emergency_type = None
            self._last_emergency_started_at = None

    def _persist_score_state(self, state: RuntimeState, ts: float) -> None:
        score_value = int(self.score.score)
        should_save = (
            state.last_score_state_persist == 0.0
            or score_value != state.last_score_state_value
            or (ts - state.last_score_state_persist) >= 5.0
        )
        if not should_save:
            return
        self.score_state_store.save(self.score.snapshot(), ts=ts)
        state.last_score_state_persist = ts
        state.last_score_state_value = score_value

    def _finalize_calibration(self) -> None:
        """Cierra la calibracion: valida/clampea baselines y los persiste."""
        corrected = self.calibration.sanitize()
        self.calibration.calibrated = True
        if corrected:
            print(f"[CALIB] Baseline sospechoso, campos clampeados: {', '.join(corrected)}")
        try:
            self.calibration_store.save(self.calibration.snapshot())
        except Exception as exc:  # no debe tumbar el sistema por un fallo de IO
            print(f"[WARN] No se pudo guardar la calibracion: {exc}")
        print(
            "[CALIB] Calibracion completada y guardada "
            f"ear={self.calibration.ear_baseline:.3f} mar={self.calibration.mar_baseline:.3f} "
            f"pitch0={self.calibration.pitch_neutral:.1f} yaw0={self.calibration.yaw_neutral:.1f}"
        )

    def _append_minute_sample(self, ts: float, param_outputs: list[dict], score_out: dict) -> None:
        self._minute_samples.append(
            {
                "ts": ts,
                "ear": self._param_value(param_outputs, "EAR"),
                "mar": self._param_value(param_outputs, "MAR"),
                "pitch": self._param_value(param_outputs, "PITCH"),
                "perclos": self._param_value(param_outputs, "PERCLOS"),
                "blink_freq": self._param_value(param_outputs, "BLINK_FB"),
                "illumination": self._param_value(param_outputs, "ILLUMINATION"),
                "time_on_task": self._param_value(param_outputs, "TIME_ON_TASK"),
                "monotony_index": self._param_value(param_outputs, "MONOTONY"),
                "fatigue_score": float(score_out.get("fatigue_score", 0)),
                "fatigue_level": float(score_out.get("level", 0)),
            }
        )

    def _flush_minute_summary(self, state: RuntimeState, ts: float, telemetry: dict, force: bool = False) -> None:
        if not self._minute_samples:
            return
        current_minute = int(ts // 60)
        last_minute = int(state.last_minute_flush // 60)
        if not force and current_minute == last_minute:
            return
        payload = {
            "session_id": state.session_id,
            "ts": self._iso_ts(ts),
            "avg_ear": self._avg(self._minute_samples, "ear"),
            "avg_mar": self._avg(self._minute_samples, "mar"),
            "avg_pitch": self._avg(self._minute_samples, "pitch"),
            "perclos": self._avg(self._minute_samples, "perclos"),
            "blink_freq": self._avg(self._minute_samples, "blink_freq"),
            "fatigue_score": int(round(self._avg(self._minute_samples, "fatigue_score"))),
            "fatigue_level": int(round(self._avg(self._minute_samples, "fatigue_level"))),
            "fatigue_label": telemetry.get("score", {}).get("label"),
            "illumination": str(round(self._avg(self._minute_samples, "illumination"), 3)),
            "time_on_task": int(round(self._avg(self._minute_samples, "time_on_task"))),
            "monotony_index": int(round(self._avg(self._minute_samples, "monotony_index"))),
            "payload": {
                "samples": len(self._minute_samples),
                "alert_memory": telemetry.get("alert_memory", {}),
            },
        }
        self.supabase.enqueue("metrics_summary", payload, immediate=False)
        self._minute_samples.clear()
        state.last_minute_flush = ts

    def _draw_system_status(
        self,
        frame: np.ndarray,
        state: RuntimeState,
        score_out: dict,
        emergency: dict,
        alert_memory: dict,
    ) -> None:
        mqtt_stats = self.mqtt.stats()
        db_stats = self.supabase.stats()
        disp_stats = self.dispatcher.stats()
        uptime_s = int(max(0.0, time.time() - state.started_at))
        uptime_str = f"{uptime_s // 60}m{uptime_s % 60:02d}s" if uptime_s >= 60 else f"{uptime_s}s"

        mqtt_color = (220, 255, 220) if mqtt_stats["connected"] else (0, 0, 255)
        db_color = (220, 255, 220) if db_stats["enabled"] else (180, 180, 180)

        level = score_out.get("level", 0)
        level_colors = {0: (220, 255, 220), 1: (0, 255, 255), 2: (0, 180, 255), 3: (0, 80, 255), 4: (0, 0, 255)}
        level_color = level_colors.get(level, (220, 255, 220))

        lines_with_colors = [
            (f"Sesion: {state.session_id[:20]} | {uptime_str} | Cal:{'SI' if self.calibration.calibrated else 'NO'}", (220, 255, 220)),
            (f"MQTT: {'OK' if mqtt_stats['connected'] else 'OFF'} pub={mqtt_stats['published_count']} dlv={mqtt_stats.get('delivered_count', '?')} drop={mqtt_stats['dropped_count']} q={mqtt_stats['queue_size']}", mqtt_color),
            (f"DB: {'OK' if db_stats['enabled'] else 'OFF'} pend={db_stats['pending']} ok={db_stats['flushed']} err={db_stats['failed']}", db_color),
            (f"Nivel: {level} ({score_out.get('label', '?')}) score={score_out.get('fatigue_score', 0)} esc={alert_memory.get('escalation_count', 0)} trans={alert_memory.get('transition_count', 0)}", level_color),
            (f"Mem5m: pico={alert_memory.get('peaks', {}).get('5m', 0)} emerg={alert_memory.get('emergency_counts', {}).get('5m', 0)} | suprimidos={disp_stats.get('suppressed_mqtt', 0)}", (220, 255, 220)),
        ]
        if emergency.get("emergencyflag"):
            lines_with_colors.append((f"EMERGENCIA: {emergency.get('emergencytype')} - {', '.join(emergency.get('reasons', []))}", (0, 0, 255)))
        if mqtt_stats.get("last_error"):
            lines_with_colors.append((f"MQTT err: {mqtt_stats['last_error'][:50]}", (0, 100, 255)))

        x1, y1 = 12, frame.shape[0] - (len(lines_with_colors) * 18 + 16)
        x2, y2 = min(frame.shape[1] - 12, x1 + 560), frame.shape[0] - 12
        overlay = frame.copy()
        cv2.rectangle(overlay, (x1, max(8, y1)), (x2, y2), (15, 15, 15), -1)
        cv2.addWeighted(overlay, 0.45, frame, 0.55, 0.0, frame)
        y = max(24, y1 + 18)
        for line_text, color in lines_with_colors:
            cv2.putText(frame, line_text, (x1 + 8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.40, color, 1)
            y += 18

    def start_threads(self) -> None:
        self.mqtt.start()
        self.supabase.start()
        self.rule_engine.start()
        if self.hands_worker:
            self.hands_worker.start()

    def stop(self) -> None:
        self.rule_engine.stop()
        if self.hands_worker:
            self.hands_worker.stop()
        self.mqtt.stop()
        self.supabase.stop()
        self.buzzer.stop()
        self.face_mesh.close()
        # Persistir la calibracion vigente al apagar (si ya estaba calibrado)
        # para no perder el ajuste del conductor.
        if self.calibration.calibrated:
            try:
                self.calibration_store.save(self.calibration.snapshot())
            except Exception:
                pass
        self.calibration_store.close()
        self.score_state_store.close()
        self.event_store.close()

    def run(self) -> None:
        preferred = int(self.cfg.camera_index)
        print(f"[INFO] MP lado_mayor={self.MP_PROC_LONG} (aspecto preservado) | manos={'ON' if self.hands_enabled else 'OFF'} | display={'ON' if self.display_enabled else 'OFF'}")
        print(f"[INFO] Abriendo camara (index preferido={preferred})...")
        camera_kind = ""
        camera = None
        read_frame = None

        try:
            picam2 = setup_camera()
            camera_kind = "picamera2"
            camera = picam2
            read_frame = self._read_picamera_frame
            print("[INFO] Camara abierta con Picamera2.")
        except Exception as exc:
            print(f"[WARN] Picamera2 no disponible: {exc}")
            candidates = self._camera_candidates(preferred)
            print("[INFO] Candidatos OpenCV: " + ", ".join(str(candidate) for candidate in candidates))
            for candidate in candidates:
                probe = self._try_open_camera(candidate)
                if probe is not None:
                    camera_kind = "opencv"
                    camera = probe
                    read_frame = self._read_opencv_frame
                    print(f"[INFO] Camara abierta con OpenCV en fuente={candidate}.")
                    break
            if camera is None:
                raise RuntimeError(
                    "No se pudo abrir ninguna camara. "
                    "Verifica /dev/video*, permisos de grupo video, libcamera y CAMERA_INDEX en .env. "
                    f"Estado detectado: {describe_camera_environment()}"
                )
        print("[INFO] Esperando primer frame...")

        state = RuntimeState(session_id=f"ses_{uuid.uuid4().hex[:12]}", started_at=time.time(), last_minute_flush=time.time())
        self.recorder = SessionRecorder(state.session_id)

        # SIGTERM (systemd stop, kill) debe disparar shutdown limpio.
        def _sigterm_handler(_signum, _frame):
            print("[INFO] SIGTERM recibido, cerrando sesion...")
            self.exit_requested = True

        signal.signal(signal.SIGTERM, _sigterm_handler)

        self.start_threads()

        last_fps_ts = time.time()
        last_health_log_ts = time.time()
        last_display_ts = 0.0
        fps_count = 0
        fps = 0.0
        first_frame_ok = False
        first_frame_deadline = time.time() + 10.0
        head_down_start_ts: float | None = None
        frame_idx = 0
        last_score_out = {"fatigue_score": 0, "level": 0, "label": "NORMAL", "max_fatigue": 0, "alert_count": 0}
        last_telemetry = {
            "v": self.cfg.vehicle_id,
            "d": self.cfg.driver_id,
            "ts": int(time.time()),
            "session_id": state.session_id,
            "score": last_score_out,
            "alerts": {"active": False, "level": 0, "reasons": []},
            "emergency": {"emergencyflag": False, "emergencytype": None, "reasons": [], "fixedbuzzer": False},
            "alert_memory": self.alert_memory.snapshot(time.time()),
            "sys": {"fps": 0.0, "status": "starting"},
        }

        try:
            if self.display_enabled:
                cv2.namedWindow(self.WINDOW_NAME)
                cv2.setMouseCallback(self.WINDOW_NAME, self._handle_mouse)
            while True:
                ok, frame = read_frame(camera)
                if not ok or frame is None:
                    if time.time() >= first_frame_deadline and not first_frame_ok:
                        raise RuntimeError(
                            "La camara se abrio, pero no entrega frames en 10s. "
                            "Revisa CAMERA_INDEX, permisos de video y que ningun otro proceso use la camara."
                        )
                    time.sleep(0.05)
                    continue
                if not first_frame_ok:
                    first_frame_ok = True
                    print("[INFO] Primer frame recibido. Pipeline en ejecucion.")

                ts = time.time()
                frame_idx += 1
                # Pantalla: frame original rotado segun preferencia. MediaPipe: frame optimizado (sin recorte).
                display_frame = self._apply_rotation(frame, self.rotation_index)
                mp_frame = self._build_mediapipe_frame(display_frame)
                h, w = display_frame.shape[:2]
                mp_h, mp_w = mp_frame.shape[:2]
                face_out = self.face_mesh.process(mp_frame)
                if self.hands_worker and frame_idx % max(1, self.HANDS_EVERY_N_FRAMES) == 0:
                    self.hands_worker.submit(mp_frame)
                hand_out = self.hands_worker.latest() if self.hands_worker else None

                param_outputs = []
                pitch = yaw = roll = 0.0
                ear = mar = 0.0
                yaw_delta = pitch_delta_head = roll_delta = 0.0
                ocular_reliable = mouth_reliable = False
                off_road = False
                distraction_s = 0.0
                distraction_flag = False
                fixation_value = 0.0
                asym_value = 0.0
                face_detected = False
                # Iluminacion: por defecto la global (respaldo si no hay cara).
                # Si hay rostro se recalcula sobre el recuadro de la cara.
                illumination_now = float(mp_frame.mean()) / 255.0
                face_illumination = illumination_now
                light_ok = illumination_now >= self.min_illumination

                if face_out.multi_face_landmarks:
                    face_detected = True
                    lm = face_out.multi_face_landmarks[0].landmark
                    # Luz REAL sobre la cara (recuadro facial), no del fondo.
                    face_illumination = self._face_illumination(mp_frame, lm, mp_w, mp_h)
                    light_ok = face_illumination >= self.min_illumination
                    ear_left, w_eye_left = eye_metrics(lm, OJO_IZQ, mp_w, mp_h, self.rotation_index)
                    ear_right, w_eye_right = eye_metrics(lm, OJO_DER, mp_w, mp_h, self.rotation_index)
                    mar = get_mar(lm, BOCA, mp_w, mp_h, self.rotation_index)

                    left_pts = []
                    right_pts = []
                    for i in OJO_IZQ:
                        x = lm[i].x * mp_w
                        y = lm[i].y * mp_h
                        if self.rotation_index == 1:  # 90 grados horario
                            x, y = y, mp_w - x
                        elif self.rotation_index == 2:  # 180 grados
                            x, y = mp_w - x, mp_h - y
                        elif self.rotation_index == 3:  # 90 grados antihorario
                            x, y = mp_h - y, x
                        left_pts.append([x, y])
                    
                    for i in OJO_DER:
                        x = lm[i].x * mp_w
                        y = lm[i].y * mp_h
                        if self.rotation_index == 1:  # 90 grados horario
                            x, y = y, mp_w - x
                        elif self.rotation_index == 2:  # 180 grados
                            x, y = mp_w - x, mp_h - y
                        elif self.rotation_index == 3:  # 90 grados antihorario
                            x, y = mp_h - y, x
                        right_pts.append([x, y])
                    
                    left_pts = np.asarray(left_pts, dtype=np.float32)
                    right_pts = np.asarray(right_pts, dtype=np.float32)
                    left_center = np.mean(left_pts, axis=0)
                    right_center = np.mean(right_pts, axis=0)

                    out_cabeza = self.cabeza.update(ts, lm, mp_w, mp_h, self.calibration, self.rotation_index)
                    pitch = out_cabeza["PITCH"]["value"]
                    yaw = out_cabeza["YAW"]["value"]
                    roll = out_cabeza["ROLL"]["value"]

                    # Desviaciones de pose respecto al neutro (diferencia angular
                    # MAS CORTA: robusta al wraparound de ±180 de solvePnP).
                    yaw_delta = angle_delta_deg(yaw, self.calibration.yaw_neutral)
                    pitch_delta_head = angle_delta_deg(pitch, self.calibration.pitch_neutral)
                    roll_delta = angle_delta_deg(roll, self.calibration.roll_neutral)

                    # EAR fusionado con conciencia de pose: de frente promedia
                    # ambos ojos; al girar pondera hacia el ojo mas frontal.
                    ear = fuse_ear(
                        ear_left, w_eye_left, ear_right, w_eye_right, yaw_delta,
                        yaw_soft=self.ear_yaw_soft, yaw_hard=self.ear_yaw_hard,
                    )

                    # Fiabilidad geometrica de cada grupo segun cuanto se escorza
                    # la cara Y si hay luz suficiente. Fuera de rango o en casi
                    # oscuridad, se suprimen los eventos de ese grupo.
                    ocular_reliable = (
                        light_ok
                        and abs(yaw_delta) <= self.yaw_ocular_max
                        and abs(pitch_delta_head) <= self.pitch_ocular_max
                        and abs(roll_delta) <= self.roll_ocular_max
                    )
                    mouth_reliable = (
                        light_ok
                        and abs(yaw_delta) <= self.yaw_mouth_max
                        and abs(pitch_delta_head) <= self.pitch_mouth_max
                    )

                    # Distraccion: giro (yaw) sostenido = mirada fuera de via. No
                    # es fatiga (no toca el score), pero se marca como senal.
                    if self.distraction_enabled and abs(yaw_delta) >= self.distraction_yaw:
                        if self._distraction_since is None:
                            self._distraction_since = ts
                        distraction_s = max(0.0, ts - self._distraction_since)
                        off_road = True
                    else:
                        self._distraction_since = None
                        self._distraction_reported = False
                    distraction_flag = self.distraction_enabled and distraction_s >= self.distraction_min_s
                    if distraction_flag and not self._distraction_reported:
                        self._distraction_reported = True
                        print(f"[DISTRACCION] mirada fuera de via {distraction_s:.1f}s (yaw={yaw_delta:.0f} deg)")
                    # Aviso audible con re-chirp mientras persista la distraccion.
                    if distraction_flag and self.distraction_buzzer and (ts - self._distraction_last_chirp) >= self.distraction_rechirp_s:
                        self.buzzer.chirp(beeps=2, on_s=0.05, off_s=0.05)
                        self._distraction_last_chirp = ts

                    out_ojos = self.ojos.update(ts, ear, left_center, right_center, self.calibration, pose_reliable=ocular_reliable)
                    out_boca = self.boca.update(ts, mar, self.calibration, pose_reliable=mouth_reliable)
                    out_facial = self.facial.update(ts, lm, mp_w, mp_h, self.calibration, self.rotation_index)
                    out_manos = self.manos.update(ts, hand_out, left_center, right_center, mp_w, mp_h, self.calibration, self.rotation_index)

                    fixation_value = out_ojos["FIXATION"]["value"]
                    asym_value = out_facial["FACIAL_ASYMMETRY"]["value"]

                    param_outputs.extend([v for k, v in out_ojos.items() if k != "blink_detected"])
                    param_outputs.extend(list(out_boca.values()))
                    param_outputs.extend(list(out_cabeza.values()))
                    param_outputs.extend(list(out_facial.values()))
                    param_outputs.extend(list(out_manos.values()))

                has_event = any(p.get("eventflag", False) for p in param_outputs)
                out_contexto = self.contexto.update(ts, display_frame, has_event, self.calibration)
                param_outputs.extend(list(out_contexto.values()))

                if not self.calibration.calibrated:
                    elapsed = ts - state.started_at
                    if face_detected:
                        # Pose neutra: media CIRCULAR (solo requiere rostro; la pose
                        # por solvePnP es robusta a la luz). El pitch/roll crudos
                        # rondan ±180 y saltan el wraparound; la media circular da
                        # el neutro correcto donde una EMA lineal daba basura.
                        self.calibration.pitch_neutral = self._pitch_mean.update(pitch)
                        self.calibration.roll_neutral = self._roll_mean.update(roll)
                        self.calibration.yaw_neutral = self._yaw_mean.update(yaw)
                        # EAR/MAR/asimetria son sensibles al ruido: solo con luz
                        # suficiente y rechazando outliers por PLAUSIBILIDAD (un
                        # parpadeo/bostezo no debe sesgar el baseline).
                        if light_ok:
                            # EAR: solo ojo abierto (descarta parpadeos, ~0.75*baseline).
                            if 0.12 <= ear <= 0.45 and ear >= 0.75 * self.calibration.ear_baseline:
                                self.calibration.ear_baseline = 0.99 * self.calibration.ear_baseline + 0.01 * ear
                            # MAR: solo boca cerrada (descarta bostezos/habla).
                            if 0.05 <= mar <= self.calibration.mar_baseline * 1.3:
                                self.calibration.mar_baseline = 0.99 * self.calibration.mar_baseline + 0.01 * mar
                            # Asimetria facial de reposo del conductor (descarta ruido).
                            if 0.0 < asym_value < 0.30:
                                self.calibration.asymmetry_base = 0.99 * self.calibration.asymmetry_base + 0.01 * asym_value
                    calibration_seconds = float(os.getenv("CALIBRATION_SECONDS", "300"))
                    if elapsed >= calibration_seconds:
                        self._finalize_calibration()

                for p in param_outputs:
                    self.event_store.append(p)

                rules = self.rule_engine.latest()
                score_forced_min_level = rules.get("forced_min_level", 0) if face_detected else 0
                score_forced_reasons = rules.get("reasons", []) if face_detected else []
                # Gate de calidad de rostro: MediaPipe a veces detecta un "rostro"
                # espurio en texturas (p.ej. una pared), lo que disparaba falsos
                # positivos. Solo se considera rostro valido si la deteccion es
                # estable (LANDMARK_STABILITY alto). Umbral configurable.
                landmark_stability = float(self._param_value(param_outputs, "LANDMARK_STABILITY", 0.0)) if face_detected else 0.0
                face_quality_ok = face_detected and landmark_stability >= self.face_quality_min
                score_forced_min_level = score_forced_min_level if face_quality_ok else 0
                score_forced_reasons = score_forced_reasons if face_quality_ok else []

                score_out = self.score.update(
                    ts=ts,
                    param_outputs=param_outputs if face_quality_ok else [],
                    vehicle_moving=True,
                    driver_response=False,
                    forced_min_level=score_forced_min_level,
                    forced_reasons=score_forced_reasons,
                    sensor_valid=face_quality_ok,
                )

                # Ventana de gracia ante perdida breve de rostro: si la cara acaba
                # de desaparecer, congelamos el nivel comprometido (histeresis) en
                # lugar de dejarlo caer. Asi, quitar la cara y volver a ponerla en
                # unos segundos no reinicia la deteccion.
                if face_quality_ok:
                    self._face_lost_since = None
                    face_in_grace = False
                else:
                    if self._face_lost_since is None:
                        self._face_lost_since = ts
                    face_in_grace = (ts - self._face_lost_since) <= self.face_grace_s

                # Endurecimiento de precision: corroboracion multi-cue + persistencia
                # temporal + histeresis sobre el nivel crudo del score.
                active_event_params = [
                    str(p.get("paramid")) for p in param_outputs
                    if bool(p.get("eventflag", False)) and p.get("paramid")
                ] if face_quality_ok else []
                if face_in_grace:
                    # Congelar la histeresis: mantener el ultimo nivel comprometido
                    # sin actualizar el estabilizador (no alimentar raw_level=0).
                    frozen = int(self.level_stabilizer.committed_level)
                    stab = {
                        "committed_level": frozen,
                        "raw_level": frozen,
                        "corroborated": True,
                        "active_families": [],
                    }
                else:
                    stab = self.level_stabilizer.update(
                        ts=ts,
                        raw_level=int(score_out["level"]),
                        active_event_params=active_event_params,
                        forced_min_level=int(score_forced_min_level),
                    )
                committed_level = int(stab["committed_level"])
                score_out = {
                    **score_out,
                    "level": committed_level,
                    "label": self.score.level_label(committed_level),
                    "raw_level": int(stab["raw_level"]),
                    "corroborated": bool(stab["corroborated"]),
                    "active_families": stab["active_families"],
                    "landmark_stability": round(landmark_stability, 3),
                }

                pitch_delta = angle_delta_deg(pitch, self.calibration.pitch_neutral)
                head_down_now = face_detected and (pitch_delta <= -24.0)
                if head_down_now:
                    if head_down_start_ts is None:
                        head_down_start_ts = ts
                    head_down_s = max(0.0, ts - head_down_start_ts)
                else:
                    head_down_start_ts = None
                    head_down_s = 0.0
                pv = {p["paramid"]: p["value"] for p in param_outputs if "paramid" in p}
                eye_closed_ms = float(pv.get("EYE_CLOSED_MS", 0.0))
                # La emergencia medica solo se evalua con un rostro CONFIABLE. Un
                # rostro espurio/inestable (textura de fondo) no debe disparar
                # "perdida de consciencia" ni "ictus". La ausencia total de rostro
                # se sigue reportando como FACE_OUT_OF_FRAME.
                if face_quality_ok:
                    emergency = detect_emergency(
                        {
                            "blink_tc_ms": pv.get("BLINK_TC", 0.0),
                            "eye_closed_ms": eye_closed_ms,
                            "pitch": pitch,
                            "pitch_delta": pitch_delta,
                            "roll": roll,
                            "yaw": yaw,
                            "head_micro_osc": pv.get("HEAD_MICRO_OSC", 0.0),
                            "landmark_stability": pv.get("LANDMARK_STABILITY", 1.0),
                            # Solo se considera asimetria (ictus) con cara frontal
                            # fiable; y con umbral CALIBRADO al conductor, no fijo.
                            "facial_asymmetry": pv.get("FACIAL_ASYMMETRY", 0.0) if ocular_reliable else 0.0,
                            "asymmetry_thr": max(0.20, self.calibration.asymmetry_base * 3.0),
                            "fixation": fixation_value,
                            "blink_fb": pv.get("BLINK_FB", 0.0),
                            "face_out": False,
                            "yaw_justified": abs(yaw) >= 30.0,
                            "head_down_s": head_down_s,
                        }
                    )
                elif not face_detected:
                    emergency = detect_emergency(
                        {"pitch_delta": pitch_delta, "head_down_s": head_down_s,
                         "face_out": True, "yaw_justified": False}
                    )
                else:
                    emergency = {"emergencyflag": False, "emergencytype": None, "reasons": [], "fixedbuzzer": False}
                if face_quality_ok and eye_closed_ms >= 1500.0 and int(score_out.get("level", 0)) < 2:
                    reasons = list(score_out.get("reasons", []))
                    if "EYE_CLOSED_MS_FAST" not in reasons:
                        reasons.append("EYE_CLOSED_MS_FAST")
                    score_out = {**score_out, "level": 2, "label": self.score.level_label(2), "reasons": reasons}
                self._persist_score_state(state, ts)

                fps_count += 1
                if time.time() - last_fps_ts >= 1.0:
                    fps = fps_count / max(1e-3, time.time() - last_fps_ts)
                    fps_count = 0
                    last_fps_ts = time.time()
                if time.time() - last_health_log_ts >= 10.0:
                    print(f"[INFO] Sistema activo | FPS={fps:.1f} | nivel={score_out['level']} | score={score_out['fatigue_score']}")
                    last_health_log_ts = time.time()

                alert_memory = self.alert_memory.update(
                    ts=ts,
                    level=int(score_out["level"]),
                    reasons=list(score_out.get("reasons", [])),
                    emergency_type=emergency.get("emergencytype") if emergency.get("emergencyflag") else None,
                )
                telemetry = {
                    "v": self.cfg.vehicle_id,
                    "d": self.cfg.driver_id,
                    "ts": int(ts),
                    "session_id": state.session_id,
                    "score": score_out,
                    "alerts": {"active": score_out["level"] > 0, "level": score_out["level"], "reasons": score_out.get("reasons", [])},
                    # Bloque de trazabilidad de la decision de somnolencia. Sirve
                    # para auditar precision en el arnes de validacion: por que se
                    # comprometio (o no) un nivel. Compatible hacia atras: 'score'
                    # y 'alerts' se mantienen intactos.
                    "drowsiness": {
                        "committed_level": int(score_out["level"]),
                        "raw_level": int(score_out.get("raw_level", score_out["level"])),
                        "corroborated": bool(score_out.get("corroborated", True)),
                        "active_families": score_out.get("active_families", []),
                        "fatigue_score": int(score_out.get("fatigue_score", 0)),
                        "face_quality_ok": bool(face_quality_ok),
                        "landmark_stability": float(score_out.get("landmark_stability", 0.0)),
                        "active_events": active_event_params,
                        # Trazabilidad de pose: permite auditar supresiones por giro.
                        "pose": {
                            "yaw_delta": round(float(yaw_delta), 1),
                            "pitch_delta": round(float(pitch_delta_head), 1),
                            "roll_delta": round(float(roll_delta), 1),
                            "ocular_reliable": bool(ocular_reliable),
                            "mouth_reliable": bool(mouth_reliable),
                            "face_illumination": round(float(face_illumination), 3),
                            "light_ok": bool(light_ok),
                        },
                        # Valores crudos oculares/boca para auditar falsos cierres.
                        "raw": {
                            "ear": round(float(pv.get("EAR", 0.0)), 3),
                            "mar": round(float(pv.get("MAR", 0.0)), 3),
                            "blink_tc_ms": round(float(pv.get("BLINK_TC", 0.0)), 0),
                            "eye_closed_ms": round(float(pv.get("EYE_CLOSED_MS", 0.0)), 0),
                            "perclos": round(float(pv.get("PERCLOS", 0.0)), 3),
                        },
                        # Distraccion (mirada fuera de via): senal independiente de
                        # la fatiga; util para auditar atencion, no altera el nivel.
                        "distraction": {
                            "off_road": bool(off_road),
                            "sustained": bool(distraction_flag),
                            "duration_s": round(float(distraction_s), 1),
                        },
                    },
                    "emergency": emergency,
                    "alert_memory": alert_memory,
                    "sys": {
                        "fps": fps,
                        "status": "online",
                        "mqtt": self.mqtt.stats(),
                        "supabase": self.supabase.stats(),
                        "calibrated": self.calibration.calibrated,
                    },
                }

                telemetry = self.dispatcher.dispatch(
                    level=int(score_out["level"]),
                    reasons=list(score_out.get("reasons", [])),
                    payload=telemetry,
                    emergency=bool(emergency["emergencyflag"]),
                    emergency_type=emergency.get("emergencytype"),
                    fixed_buzzer=bool(emergency.get("fixedbuzzer", False)),
                )

                self.recorder.append(ts, build_record(ts, telemetry, pv))
                self._append_minute_sample(ts, param_outputs, score_out)
                if state.last_session_sync == 0.0 or (ts - state.last_session_sync) >= 15.0:
                    self._sync_session(state, ts, score_out, is_final=False)
                    state.last_session_sync = ts
                persist_immediate = bool(emergency.get("emergencyflag")) or int(score_out.get("level", 0)) >= 3
                if state.last_telemetry_persist == 0.0 or persist_immediate or (ts - state.last_telemetry_persist) >= 2.0:
                    self._persist_telemetry(telemetry, ts, immediate=persist_immediate)
                    state.last_telemetry_persist = ts
                self._persist_param_events(telemetry, param_outputs, score_out, ts)
                self._persist_emergency(telemetry, emergency, ts)
                self._flush_minute_summary(state, ts, telemetry, force=False)
                last_score_out = score_out
                last_telemetry = telemetry

                if self.display_enabled and (ts - last_display_ts) >= self.DISPLAY_INTERVAL_S:
                    last_display_ts = ts
                    hud_level = score_out["level"]
                    hud_colors = {0: (0, 255, 0), 1: (0, 255, 255), 2: (0, 180, 255), 3: (0, 80, 255), 4: (0, 0, 255)}
                    hud_color = hud_colors.get(hud_level, (0, 255, 255))
                    cv2.putText(display_frame, f"FPS:{fps:.1f} SCORE:{score_out['fatigue_score']} {score_out.get('label', '?')}", (16, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, hud_color, 2)
                    self._draw_parameters_panel(display_frame, param_outputs)
                    self._draw_system_status(display_frame, state, score_out, emergency, alert_memory)
                    self._draw_exit_button(display_frame)
                    cv2.imshow(self.WINDOW_NAME, display_frame)
                    key = cv2.waitKey(1) & 0xFF
                else:
                    key = cv2.waitKey(1) & 0xFF if self.display_enabled else -1
                if key == ord("r"):
                    self._set_rotation((self.rotation_index + 1) % 4, "manual")
                if key == ord("q") or key == 27 or self.exit_requested:
                    break

        finally:
            shutdown_ts = time.time()
            if getattr(self, "recorder", None) is not None:
                self.recorder.close()
            self.score_state_store.save(self.score.snapshot(), ts=shutdown_ts)
            self._flush_minute_summary(state, shutdown_ts, last_telemetry, force=True)
            self._sync_session(state, shutdown_ts, last_score_out, is_final=True)
            if camera_kind == "opencv" and camera is not None:
                camera.release()
            elif camera_kind == "picamera2" and camera is not None:
                try:
                    camera.stop()
                except Exception:
                    pass
            if self.display_enabled:
                cv2.destroyAllWindows()
            self.stop()


def main() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=env_path)
    cfg = AppConfig.from_env()
    system = SomnolenciaSystem(cfg)
    system.run()


if __name__ == "__main__":
    main()
