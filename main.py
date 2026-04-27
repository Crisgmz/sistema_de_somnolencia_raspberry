"""Main industrial para deteccion de somnolencia en Raspberry Pi 4.

Estructura simplificada por tipo de parametro dentro de carpeta `parametros/`.
"""

from __future__ import annotations

import os
import queue
import signal
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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
from core.eventstore import EventStore
from core.scorestate import ScoreStateStore
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
    MP_PROC_WIDTH = 320
    MP_PROC_HEIGHT = 180
    HANDS_EVERY_N_FRAMES = 4
    DISPLAY_INTERVAL_S = 1.0 / 15.0  # limita display a 15 fps maximo

    def __init__(self, config: AppConfig) -> None:
        self.cfg = config
        self.calibration = Calibration()
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
        self.hands_enabled = os.getenv("SOMNO_HANDS_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
        self.hands_worker = HandsWorker() if self.hands_enabled else None
        self.alert_memory = AlertMemory()
        self._active_param_events: dict[str, float] = {}
        self._last_emergency_type: str | None = None
        self._last_emergency_started_at: float | None = None
        self._minute_samples: list[dict] = []

        self.face_mesh = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=False,
            refine_landmarks=False,
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
        # Optimiza costo de inferencia sin recortar: solo escala manteniendo 16:9.
        if frame.shape[1] == SomnolenciaSystem.MP_PROC_WIDTH and frame.shape[0] == SomnolenciaSystem.MP_PROC_HEIGHT:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(
            frame,
            (SomnolenciaSystem.MP_PROC_WIDTH, SomnolenciaSystem.MP_PROC_HEIGHT),
            interpolation=cv2.INTER_LINEAR,
        )
        return cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

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
        self.score_state_store.close()
        self.event_store.close()

    def run(self) -> None:
        preferred = int(self.cfg.camera_index)
        print(f"[INFO] MP resolucion={self.MP_PROC_WIDTH}x{self.MP_PROC_HEIGHT} | manos={'ON' if self.hands_enabled else 'OFF'} | display={'ON' if self.display_enabled else 'OFF'}")
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
                fixation_value = 0.0
                face_detected = False

                if face_out.multi_face_landmarks:
                    face_detected = True
                    lm = face_out.multi_face_landmarks[0].landmark
                    ear_left = get_ear(lm, OJO_IZQ, mp_w, mp_h)
                    ear_right = get_ear(lm, OJO_DER, mp_w, mp_h)
                    ear = (ear_left + ear_right) * 0.5
                    mar = get_mar(lm, BOCA, mp_w, mp_h)

                    left_pts = np.asarray([[lm[i].x * mp_w, lm[i].y * mp_h] for i in OJO_IZQ], dtype=np.float32)
                    right_pts = np.asarray([[lm[i].x * mp_w, lm[i].y * mp_h] for i in OJO_DER], dtype=np.float32)
                    left_center = np.mean(left_pts, axis=0)
                    right_center = np.mean(right_pts, axis=0)

                    out_cabeza = self.cabeza.update(ts, lm, mp_w, mp_h, self.calibration)
                    pitch = out_cabeza["PITCH"]["value"]
                    yaw = out_cabeza["YAW"]["value"]
                    roll = out_cabeza["ROLL"]["value"]

                    out_ojos = self.ojos.update(ts, ear, left_center, right_center, self.calibration)
                    out_boca = self.boca.update(ts, mar, self.calibration)
                    out_facial = self.facial.update(ts, lm, mp_w, mp_h, self.calibration)
                    out_manos = self.manos.update(ts, hand_out, left_center, right_center, mp_w, mp_h, self.calibration)

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
                    if face_detected:
                        self.calibration.pitch_neutral = 0.99 * self.calibration.pitch_neutral + 0.01 * pitch
                        self.calibration.roll_neutral = 0.99 * self.calibration.roll_neutral + 0.01 * roll
                        self.calibration.yaw_neutral = 0.99 * self.calibration.yaw_neutral + 0.01 * yaw
                    calibration_seconds = float(os.getenv("CALIBRATION_SECONDS", "300"))
                    if elapsed >= calibration_seconds:
                        self.calibration.calibrated = True

                for p in param_outputs:
                    self.event_store.append(p)

                rules = self.rule_engine.latest()
                score_forced_min_level = rules.get("forced_min_level", 0) if face_detected else 0
                score_forced_reasons = rules.get("reasons", []) if face_detected else []
                score_out = self.score.update(
                    ts=ts,
                    param_outputs=param_outputs if face_detected else [],
                    vehicle_moving=True,
                    driver_response=False,
                    forced_min_level=score_forced_min_level,
                    forced_reasons=score_forced_reasons,
                    sensor_valid=face_detected,
                )

                pitch_delta = pitch - self.calibration.pitch_neutral
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
                        "facial_asymmetry": pv.get("FACIAL_ASYMMETRY", 0.0),
                        "fixation": fixation_value,
                        "blink_fb": pv.get("BLINK_FB", 0.0),
                        "face_out": not face_detected,
                        "yaw_justified": abs(yaw) >= 30.0,
                        "head_down_s": head_down_s,
                    }
                )
                if eye_closed_ms >= 1500.0 and int(score_out.get("level", 0)) < 2:
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
