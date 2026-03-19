"""Main industrial para deteccion de somnolencia en Raspberry Pi 4.

Estructura simplificada por tipo de parametro dentro de carpeta `parametros/`.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from dotenv import load_dotenv

from output.alertdispatcher import AlertDispatcher
from output.buzzer import Buzzer
from core.calibration import Calibration
from core.config import AppConfig
from engine.emergencydetector import detect_emergency
from core.eventstore import EventStore
from engine.fatiguescore import DynamicFatigueScore
from output.mqttpublisher import MqttPublisher
from parametros.boca import BocaParametros
from parametros.cabeza import CabezaParametros
from parametros.contexto import ContextoParametros
from parametros.facial import FacialParametros
from parametros.manos import ManosParametros
from parametros.ojos import OjosParametros
from engine.ruleengine import RuleEngine
from somnolencia_core import BOCA, OJO_DER, OJO_IZQ, get_ear, get_mar
from storage.supabasesync import SupabaseSync
from camera_setup import setup_camera


@dataclass
class RuntimeState:
    session_id: str
    started_at: float
    last_minute_flush: float


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
            model_complexity=0,
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
    CAPTURE_WIDTH = 960
    CAPTURE_HEIGHT = 720
    MP_PROC_WIDTH = 480
    MP_PROC_HEIGHT = 360
    HANDS_EVERY_N_FRAMES = 4

    def __init__(self, config: AppConfig) -> None:
        self.cfg = config
        self.calibration = Calibration()
        self.event_store = EventStore()
        self.score = DynamicFatigueScore()

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
        self.hands_worker = HandsWorker()

        self.face_mesh = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=False,
            model_complexity=0,
            refine_landmarks=False,
            max_num_faces=1,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self.exit_requested = False
        self._exit_button_rect = (0, 0, 0, 0)
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

    def _draw_exit_button(self, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        margin = 12
        button_w = 110
        button_h = 38
        x1 = max(margin, w - button_w - margin)
        y1 = margin
        x2 = x1 + button_w
        y2 = y1 + button_h
        self._exit_button_rect = (x1, y1, x2, y2)

        cv2.rectangle(frame, (x1, y1), (x2, y2), (20, 20, 220), -1)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 255, 255), 1)
        cv2.putText(frame, "SALIR", (x1 + 24, y1 + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)

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

        params_sorted = sorted(params, key=lambda p: p.get("paramid", ""))
        visible = params_sorted[:max_lines]
        panel_h = (len(visible) + 1) * line_h + 10
        x2 = x1 + panel_w
        y2 = min(h - 8, y1 + panel_h)

        overlay = frame.copy()
        cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.5, frame, 0.5, 0.0, frame)

        cv2.putText(frame, "PARAMETROS", (x1 + 8, y1 + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        y = y1 + 34
        for p in visible:
            pid = str(p.get("paramid", "-"))
            value = float(p.get("value", 0.0))
            normalized = float(p.get("normalized", 0.0))
            event = "SI" if bool(p.get("eventflag", False)) else "NO"
            color = (0, 0, 255) if event == "SI" else (255, 255, 255)
            text = f"{pid:18s} v={value:7.3f} n={normalized:5.2f} ev={event}"
            cv2.putText(frame, text, (x1 + 8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.43, color, 1)
            y += line_h

        hidden = len(params_sorted) - len(visible)
        if hidden > 0 and y + 4 < y2:
            cv2.putText(frame, f"... y {hidden} mas", (x1 + 8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.43, (200, 200, 200), 1)

    @staticmethod
    def _try_open_camera(index: int) -> cv2.VideoCapture | None:
        cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, SomnolenciaSystem.CAPTURE_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, SomnolenciaSystem.CAPTURE_HEIGHT)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        for _ in range(10):
            ok, frame = cap.read()
            if ok and frame is not None:
                return cap
            time.sleep(0.05)
        cap.release()
        return None

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
    def _build_mediapipe_frame(frame: np.ndarray) -> np.ndarray:
        # Optimiza costo de inferencia sin recortar: solo escala manteniendo 16:9.
        if frame.shape[1] == SomnolenciaSystem.MP_PROC_WIDTH and frame.shape[0] == SomnolenciaSystem.MP_PROC_HEIGHT:
            return frame
        return cv2.resize(
            frame,
            (SomnolenciaSystem.MP_PROC_WIDTH, SomnolenciaSystem.MP_PROC_HEIGHT),
            interpolation=cv2.INTER_LINEAR,
        )

    def start_threads(self) -> None:
        self.mqtt.start()
        self.supabase.start()
        self.rule_engine.start()
        self.hands_worker.start()

    def stop(self) -> None:
        self.rule_engine.stop()
        self.hands_worker.stop()
        self.mqtt.stop()
        self.supabase.stop()
        self.buzzer.stop()
        self.face_mesh.close()

    def run(self) -> None:
        preferred = int(self.cfg.camera_index)
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
            candidates = [preferred] + [i for i in range(5) if i != preferred]
            for idx in candidates:
                probe = self._try_open_camera(idx)
                if probe is not None:
                    camera_kind = "opencv"
                    camera = probe
                    read_frame = self._read_opencv_frame
                    print(f"[INFO] Camara abierta con OpenCV en index={idx}.")
                    break
            if camera is None:
                raise RuntimeError(
                    "No se pudo abrir ninguna camara. "
                    "Verifica /dev/video*, permisos de grupo video y CAMERA_INDEX en .env."
                )
        print("[INFO] Esperando primer frame...")

        state = RuntimeState(session_id=f"ses_{uuid.uuid4().hex[:12]}", started_at=time.time(), last_minute_flush=time.time())
        self.start_threads()

        last_fps_ts = time.time()
        last_health_log_ts = time.time()
        fps_count = 0
        fps = 0.0
        first_frame_ok = False
        first_frame_deadline = time.time() + 10.0
        head_down_start_ts: float | None = None
        frame_idx = 0

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
                # Pantalla: frame original completo. MediaPipe: frame optimizado (sin recorte).
                display_frame = frame
                mp_frame = self._build_mediapipe_frame(display_frame)
                h, w = display_frame.shape[:2]
                face_out = self.face_mesh.process(mp_frame)
                if frame_idx % max(1, self.HANDS_EVERY_N_FRAMES) == 0:
                    self.hands_worker.submit(mp_frame)
                hand_out = self.hands_worker.latest()

                param_outputs = []
                pitch = yaw = roll = 0.0
                ear = mar = 0.0
                fixation_value = 0.0
                face_detected = False

                if face_out.multi_face_landmarks:
                    face_detected = True
                    lm = face_out.multi_face_landmarks[0].landmark
                    ear_left = get_ear(lm, OJO_IZQ, w, h)
                    ear_right = get_ear(lm, OJO_DER, w, h)
                    ear = (ear_left + ear_right) * 0.5
                    mar = get_mar(lm, BOCA, w, h)

                    left_pts = np.asarray([[lm[i].x * w, lm[i].y * h] for i in OJO_IZQ], dtype=np.float32)
                    right_pts = np.asarray([[lm[i].x * w, lm[i].y * h] for i in OJO_DER], dtype=np.float32)
                    left_center = np.mean(left_pts, axis=0)
                    right_center = np.mean(right_pts, axis=0)

                    out_cabeza = self.cabeza.update(ts, lm, w, h, self.calibration)
                    pitch = out_cabeza["PITCH"]["value"]
                    yaw = out_cabeza["YAW"]["value"]
                    roll = out_cabeza["ROLL"]["value"]

                    out_ojos = self.ojos.update(ts, ear, left_center, right_center, self.calibration)
                    out_boca = self.boca.update(ts, mar, self.calibration)
                    out_facial = self.facial.update(ts, lm, w, h, self.calibration)
                    out_manos = self.manos.update(ts, hand_out, left_center, right_center, w, h, self.calibration)

                    fixation_value = out_ojos["FIXATION"]["value"]

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
                    self.calibration.ear_baseline = 0.995 * self.calibration.ear_baseline + 0.005 * max(ear, 0.01)
                    self.calibration.mar_baseline = 0.995 * self.calibration.mar_baseline + 0.005 * max(mar, 0.01)
                    if elapsed >= 300.0:
                        self.calibration.calibrated = True

                for p in param_outputs:
                    self.event_store.append(p)

                rules = self.rule_engine.latest()
                score_out = self.score.update(
                    ts=ts,
                    param_outputs=param_outputs,
                    vehicle_moving=True,
                    driver_response=False,
                    forced_min_level=rules.get("forced_min_level", 0),
                    forced_reasons=rules.get("reasons", []),
                )

                pitch_delta = pitch - self.calibration.pitch_neutral
                head_down_now = face_detected and (pitch_delta <= -20.0)
                if head_down_now:
                    if head_down_start_ts is None:
                        head_down_start_ts = ts
                    head_down_s = max(0.0, ts - head_down_start_ts)
                else:
                    head_down_start_ts = None
                    head_down_s = 0.0
                emergency = detect_emergency(
                    {
                        "blink_tc_ms": next((p["value"] for p in param_outputs if p["paramid"] == "BLINK_TC"), 0.0),
                        "eye_closed_ms": next((p["value"] for p in param_outputs if p["paramid"] == "EYE_CLOSED_MS"), 0.0),
                        "pitch": pitch,
                        "pitch_delta": pitch_delta,
                        "roll": roll,
                        "yaw": yaw,
                        "head_micro_osc": next((p["value"] for p in param_outputs if p["paramid"] == "HEAD_MICRO_OSC"), 0.0),
                        "landmark_stability": next((p["value"] for p in param_outputs if p["paramid"] == "LANDMARK_STABILITY"), 1.0),
                        "facial_asymmetry": next((p["value"] for p in param_outputs if p["paramid"] == "FACIAL_ASYMMETRY"), 0.0),
                        "fixation": fixation_value,
                        "blink_fb": next((p["value"] for p in param_outputs if p["paramid"] == "BLINK_FB"), 0.0),
                        "face_out": not face_detected,
                        "yaw_justified": abs(yaw) >= 30.0,
                        "head_down_s": head_down_s,
                    }
                )

                fps_count += 1
                if time.time() - last_fps_ts >= 1.0:
                    fps = fps_count / max(1e-3, time.time() - last_fps_ts)
                    fps_count = 0
                    last_fps_ts = time.time()
                if time.time() - last_health_log_ts >= 10.0:
                    print(f"[INFO] Sistema activo | FPS={fps:.1f} | nivel={score_out['level']} | score={score_out['fatigue_score']}")
                    last_health_log_ts = time.time()

                telemetry = {
                    "v": self.cfg.vehicle_id,
                    "d": self.cfg.driver_id,
                    "ts": int(ts),
                    "session_id": state.session_id,
                    "score": score_out,
                    "alerts": {"active": score_out["level"] > 0, "level": score_out["level"], "reasons": score_out.get("reasons", [])},
                    "emergency": emergency,
                    "sys": {"fps": fps, "status": "online"},
                }

                self.dispatcher.dispatch(
                    level=int(score_out["level"]),
                    reasons=list(score_out.get("reasons", [])),
                    payload=telemetry,
                    emergency=bool(emergency["emergencyflag"]),
                    emergency_type=emergency.get("emergencytype"),
                    fixed_buzzer=bool(emergency.get("fixedbuzzer", False)),
                )

                if self.display_enabled:
                    cv2.putText(display_frame, f"FPS:{fps:.1f} SCORE:{score_out['fatigue_score']} LV:{score_out['level']}", (16, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)
                    self._draw_parameters_panel(display_frame, param_outputs)
                    self._draw_exit_button(display_frame)
                    cv2.imshow(self.WINDOW_NAME, display_frame)
                    key = cv2.waitKey(1) & 0xFF
                else:
                    key = -1
                if key == ord("q") or key == 27 or self.exit_requested:
                    break

        finally:
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
