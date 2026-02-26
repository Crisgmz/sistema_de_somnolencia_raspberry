"""Main industrial para deteccion de somnolencia en Raspberry Pi 4.

Estructura simplificada por tipo de parametro dentro de carpeta `parametros/`.
"""

from __future__ import annotations

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


@dataclass
class RuntimeState:
    session_id: str
    started_at: float
    last_minute_flush: float


class HandsWorker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._stop = threading.Event()
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
        while not self._stop.is_set():
            try:
                rgb = self._in_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            out = self.hands.process(rgb)
            with self._lock:
                self._latest = out

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=1.0)
        self.hands.close()


class SomnolenciaSystem:
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
            max_num_faces=1,
            refine_landmarks=False,
            min_detection_confidence=0.6,
            min_tracking_confidence=0.6,
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
        cap = cv2.VideoCapture(self.cfg.camera_index)
        if not cap.isOpened():
            raise RuntimeError("No se pudo abrir la camara.")

        state = RuntimeState(session_id=f"ses_{uuid.uuid4().hex[:12]}", started_at=time.time(), last_minute_flush=time.time())
        self.start_threads()

        last_fps_ts = time.time()
        fps_count = 0
        fps = 0.0

        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    time.sleep(0.05)
                    continue

                ts = time.time()
                h, w = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                face_out = self.face_mesh.process(rgb)
                self.hands_worker.submit(rgb)
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
                out_contexto = self.contexto.update(ts, frame, has_event, self.calibration)
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

                emergency = detect_emergency(
                    {
                        "blink_tc_ms": next((p["value"] for p in param_outputs if p["paramid"] == "BLINK_TC"), 0.0),
                        "pitch": pitch,
                        "roll": roll,
                        "yaw": yaw,
                        "head_micro_osc": next((p["value"] for p in param_outputs if p["paramid"] == "HEAD_MICRO_OSC"), 0.0),
                        "landmark_stability": next((p["value"] for p in param_outputs if p["paramid"] == "LANDMARK_STABILITY"), 1.0),
                        "facial_asymmetry": next((p["value"] for p in param_outputs if p["paramid"] == "FACIAL_ASYMMETRY"), 0.0),
                        "fixation": fixation_value,
                        "blink_fb": next((p["value"] for p in param_outputs if p["paramid"] == "BLINK_FB"), 0.0),
                        "face_out": not face_detected,
                        "yaw_justified": abs(yaw) >= 30.0,
                    }
                )

                fps_count += 1
                if time.time() - last_fps_ts >= 1.0:
                    fps = fps_count / max(1e-3, time.time() - last_fps_ts)
                    fps_count = 0
                    last_fps_ts = time.time()

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
                )

                cv2.putText(frame, f"FPS:{fps:.1f} SCORE:{score_out['fatigue_score']} LV:{score_out['level']}", (16, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)
                cv2.imshow("Somnolencia Main", frame)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break

        finally:
            cap.release()
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
