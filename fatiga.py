import cv2
import mediapipe as mp
import numpy as np
from threading import Thread
import os
import time
import signal

from camera_setup import setup_camera
from somnolencia_core import BOCA, OJO_DER, OJO_IZQ, get_ear, get_head_pose, get_mar
from somnolencia_context import ContextMetrics
from somnolencia_face_touch import FaceTouchMetrics
from somnolencia_facial import FacialMetrics
from somnolencia_head_pose import HeadPoseMetrics
from somnolencia_medical_emergency import MedicalEmergencyPipeline
from somnolencia_ocular import OcularMetrics
from somnolencia_params import load_somnolencia_params

try:
    import RPi.GPIO as GPIO
except Exception:
    GPIO = None

alarm_active = False
stop_requested = False
exit_button_rect = (0, 0, 0, 0)
alert_started_at = None

# Perfil ligero para Raspberry Pi: menos carga visual, menos carga de Hands.
SHOW_DEBUG_PANEL = False
DRAW_FACE_OVERLAY = False
DRAW_HAND_OVERLAY = False
PROCESS_HANDS_EVERY_N_FRAMES = 3


class BuzzerController:
    def __init__(self, pin=17, active_high=True, enabled=True):
        self.pin = int(pin)
        self.active_high = bool(active_high)
        self.enabled = bool(enabled)
        self.available = False
        self._warned = False

        if not self.enabled:
            return
        if GPIO is None:
            self._warn("RPi.GPIO no disponible; buzzer desactivado.")
            return
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.OUT, initial=self._inactive_level())
            self.available = True
        except Exception as exc:
            self._warn(f"No se pudo inicializar buzzer GPIO{self.pin}: {exc}")

    def _active_level(self):
        return GPIO.HIGH if self.active_high else GPIO.LOW

    def _inactive_level(self):
        return GPIO.LOW if self.active_high else GPIO.HIGH

    def _warn(self, msg):
        if not self._warned:
            print(f"[WARN] {msg}")
            self._warned = True

    def on(self):
        if not self.available:
            return
        GPIO.output(self.pin, self._active_level())

    def off(self):
        if not self.available:
            return
        GPIO.output(self.pin, self._inactive_level())

    def cleanup(self):
        if not self.available:
            return
        try:
            self.off()
            GPIO.cleanup(self.pin)
        except Exception:
            pass


def _handle_stop_signal(signum, frame):
    del signum, frame
    global stop_requested
    stop_requested = True


def _handle_mouse(event, x, y, flags, param):
    del flags, param
    global stop_requested
    if event != cv2.EVENT_LBUTTONDOWN:
        return
    x1, y1, x2, y2 = exit_button_rect
    if x1 <= x <= x2 and y1 <= y <= y2:
        stop_requested = True


def create_face_mesh():
    return mp.solutions.face_mesh.FaceMesh(
        static_image_mode=False,
        max_num_faces=1,
        refine_landmarks=False,
        min_detection_confidence=0.6,
        min_tracking_confidence=0.6,
    )


def create_hands():
    return mp.solutions.hands.Hands(
        static_image_mode=False,
        max_num_hands=2,
        model_complexity=0,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )


def alarm_worker(params):
    global alarm_active
    while alarm_active:
        os.system(f'espeak "{params.alarm_message}"')
        time.sleep(params.alarm_repeat_seconds)


def _draw_metric_line(img, text, line_idx, color=(0, 255, 0)):
    y = 40 + (line_idx * 32)
    cv2.putText(img, text, (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.75, color, 2)


def _draw_exit_button(img):
    global exit_button_rect
    _, w = img.shape[:2]
    btn_w, btn_h = 170, 44
    x1, y1 = w - btn_w - 20, 16
    x2, y2 = x1 + btn_w, y1 + btn_h
    exit_button_rect = (x1, y1, x2, y2)
    cv2.rectangle(img, (x1, y1), (x2, y2), (20, 20, 220), -1)
    cv2.rectangle(img, (x1, y1), (x2, y2), (255, 255, 255), 2)
    cv2.putText(img, "SALIR", (x1 + 44, y1 + 30), cv2.FONT_HERSHEY_DUPLEX, 0.9, (255, 255, 255), 2)


def main():
    global alarm_active, stop_requested, alert_started_at
    signal.signal(signal.SIGINT, _handle_stop_signal)
    signal.signal(signal.SIGTERM, _handle_stop_signal)

    params = load_somnolencia_params()
    buzzer = BuzzerController(
        pin=params.buzzer_gpio_pin,
        active_high=params.buzzer_active_high,
        enabled=params.buzzer_enabled,
    )
    ocular_monitor = OcularMetrics(params)
    head_monitor = HeadPoseMetrics(params)
    facial_monitor = FacialMetrics(params)
    touch_monitor = FaceTouchMetrics(params)
    context_monitor = ContextMetrics(params)
    medical_pipeline = MedicalEmergencyPipeline(params)
    picam2 = setup_camera()
    face_mesh = create_face_mesh()
    hands = create_hands()
    mp_draw = mp.solutions.drawing_utils
    mp_styles = mp.solutions.drawing_styles
    cv2.namedWindow("Monitor de Fatiga")
    cv2.setMouseCallback("Monitor de Fatiga", _handle_mouse)

    eye_counter = 0
    yawn_count = 0
    nod_count = 0
    eye_rub_count = 0
    yawn_active = False
    nod_active = False
    eye_rub_active = False
    eye_rub_frame_counter = 0
    frame_idx = 0
    cached_hand_results = None

    try:
        while not stop_requested:
            frame_idx += 1
            frame_raw = picam2.capture_array()
            if frame_raw.ndim == 3 and frame_raw.shape[2] == 4:
                frame = cv2.cvtColor(frame_raw, cv2.COLOR_BGRA2BGR)
            elif frame_raw.ndim == 3 and frame_raw.shape[2] == 3:
                frame = frame_raw
            else:
                continue

            h, w, _ = frame.shape
            mesh_frame = np.zeros_like(frame) if SHOW_DEBUG_PANEL else None
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = face_mesh.process(rgb_frame)
            if frame_idx % max(1, PROCESS_HANDS_EVERY_N_FRAMES) == 0 or cached_hand_results is None:
                cached_hand_results = hands.process(rgb_frame)
            hand_results = cached_hand_results

            ear = 0.0
            mar = 0.0
            pitch, yaw, roll = 0.0, 0.0, 0.0
            yawn_detected = False
            nod_detected = False
            eye_rub_detected = False
            hand_near_eye = False
            face_detected = False
            eye_left_center = None
            eye_right_center = None
            face_width = 0.0
            face_landmarks_data = None

            if results.multi_face_landmarks:
                for face_landmarks in results.multi_face_landmarks:
                    face_detected = True
                    face_landmarks_data = face_landmarks.landmark
                    if SHOW_DEBUG_PANEL and DRAW_FACE_OVERLAY:
                        mp_draw.draw_landmarks(
                            image=mesh_frame,
                            landmark_list=face_landmarks,
                            connections=mp.solutions.face_mesh.FACEMESH_CONTOURS,
                            landmark_drawing_spec=None,
                            connection_drawing_spec=mp_styles.get_default_face_mesh_contours_style(),
                        )

                    ear_izq = get_ear(face_landmarks.landmark, OJO_IZQ, w, h)
                    ear_der = get_ear(face_landmarks.landmark, OJO_DER, w, h)
                    ear = (ear_izq + ear_der) / 2.0

                    mar = get_mar(face_landmarks.landmark, BOCA, w, h)
                    pitch, yaw, roll = get_head_pose(face_landmarks.landmark, w, h)
                    left_eye_pts = np.array(
                        [[face_landmarks.landmark[i].x * w, face_landmarks.landmark[i].y * h] for i in OJO_IZQ],
                        dtype=np.float32,
                    )
                    right_eye_pts = np.array(
                        [[face_landmarks.landmark[i].x * w, face_landmarks.landmark[i].y * h] for i in OJO_DER],
                        dtype=np.float32,
                    )
                    eye_left_center = np.mean(left_eye_pts, axis=0)
                    eye_right_center = np.mean(right_eye_pts, axis=0)
                    lm_left_corner = face_landmarks.landmark[33]
                    lm_right_corner = face_landmarks.landmark[263]
                    face_width = float(
                        np.linalg.norm(
                            np.array([lm_left_corner.x * w, lm_left_corner.y * h], dtype=np.float32)
                            - np.array([lm_right_corner.x * w, lm_right_corner.y * h], dtype=np.float32)
                        )
                    )

                    if mar >= params.mar_yawn_threshold and not yawn_active:
                        yawn_active = True
                    elif mar < params.mar_yawn_threshold and yawn_active:
                        yawn_active = False
                        yawn_count += 1
                        yawn_detected = True

                    if pitch >= params.head_pitch_threshold and not nod_active:
                        nod_active = True
                    elif pitch < params.head_pitch_reset_threshold and nod_active:
                        nod_active = False
                        nod_count += 1
                        nod_detected = True

                    if ear < params.ear_threshold:
                        eye_counter += 1
                    else:
                        eye_counter = 0

                    break
            else:
                eye_counter = 0
                yawn_active = False
                nod_active = False

            if hand_results and hand_results.multi_hand_landmarks and face_detected and eye_left_center is not None and eye_right_center is not None:
                rub_dist_thr = max(24.0, params.eye_rub_distance_ratio * max(face_width, 1.0))
                tip_idxs = [4, 8, 12, 16, 20]
                for hand_landmarks in hand_results.multi_hand_landmarks:
                    if SHOW_DEBUG_PANEL and DRAW_HAND_OVERLAY:
                        mp_draw.draw_landmarks(
                            mesh_frame,
                            hand_landmarks,
                            mp.solutions.hands.HAND_CONNECTIONS,
                        )
                    for idx in tip_idxs:
                        tip = hand_landmarks.landmark[idx]
                        tip_pt = np.array([tip.x * w, tip.y * h], dtype=np.float32)
                        d_left = float(np.linalg.norm(tip_pt - eye_left_center))
                        d_right = float(np.linalg.norm(tip_pt - eye_right_center))
                        if min(d_left, d_right) <= rub_dist_thr and ear <= params.eye_rub_ear_max:
                            hand_near_eye = True
                            break
                    if hand_near_eye:
                        break

            if hand_near_eye:
                eye_rub_frame_counter += 1
            else:
                eye_rub_frame_counter = 0
                eye_rub_active = False

            if eye_rub_frame_counter >= params.eye_rub_min_frames and not eye_rub_active:
                eye_rub_active = True
                eye_rub_count += 1
                eye_rub_detected = True

            ts = time.monotonic()
            ocular_metrics = ocular_monitor.update(ts, ear, eye_left_center, eye_right_center)
            head_metrics = head_monitor.update(ts, pitch, yaw, roll)
            facial_metrics = facial_monitor.update(face_landmarks_data, w, h)
            touch_metrics = touch_monitor.update(
                ts=ts,
                hand_results=hand_results,
                face_detected=face_detected,
                face_width=face_width,
                eye_left_center=eye_left_center,
                eye_right_center=eye_right_center,
                ear=ear,
            )
            illumination = float(np.mean(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)) / 255.0)
            context_metrics = context_monitor.update(ts, pitch, yaw, roll, illumination)
            medical_pipeline.submit(
                {
                    "ts": ts,
                    "face_detected": face_detected,
                    "pitch": pitch,
                    "yaw": yaw,
                    "roll": roll,
                    "eye_open": ear >= params.blink_open_threshold,
                    "eye_closed_time": ocular_metrics["tc"],
                    "fixation_duration": ocular_metrics["fixation_duration"],
                    "facial_asymmetry_index": facial_metrics["facial_asymmetry_index"],
                    "blink_detected": ocular_metrics["blink_detected"],
                }
            )
            medical_metrics = medical_pipeline.get_latest()

            sleep_alert = eye_counter >= params.consec_frames
            if sleep_alert:
                if not alarm_active:
                    alarm_active = True
                    alert_started_at = time.monotonic()
                    Thread(target=alarm_worker, args=(params,), daemon=True).start()
                elapsed = time.monotonic() - (alert_started_at if alert_started_at is not None else time.monotonic())
                cycle = max(0.05, params.buzzer_beep_on_seconds) + max(0.05, params.buzzer_beep_off_seconds)
                if (elapsed % cycle) < max(0.05, params.buzzer_beep_on_seconds):
                    buzzer.on()
                else:
                    buzzer.off()
                cv2.rectangle(frame, (0, 0), (w, h), (0, 0, 255), 10)
                cv2.putText(frame, "ALERTA SUEÑO", (40, 80), cv2.FONT_HERSHEY_DUPLEX, 1.3, (0, 0, 255), 3)
            else:
                buzzer.off()
                alarm_active = False
                alert_started_at = None

            # Overlay rapido en panel de camara
            cv2.putText(frame, f"EAR: {ear:.2f}", (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.putText(frame, f"MAR: {mar:.2f}", (20, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.putText(frame, f"Cabeceo (Pitch): {pitch:.1f}", (20, 95), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 200, 0), 2)
            cv2.putText(
                frame,
                f"PERCLOS: {ocular_metrics['perclos']*100:.1f}%  Fb: {ocular_metrics['fb_hz']:.2f}Hz  IBI: {ocular_metrics['ibi_s']:.2f}s",
                (20, 125),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 255, 0),
                2,
            )
            cv2.putText(
                frame,
                f"HeadVel: {head_metrics['head_drop_velocity']:.1f}  MicroOsc: {head_metrics['head_micro_oscillations']:.2f}",
                (20, 150),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 200, 255),
                2,
            )
            cv2.putText(
                frame,
                f"TouchFreq: {touch_metrics['face_touch_frequency']*60:.1f}/min  TouchDur: {touch_metrics['hand_to_face_duration']:.1f}s",
                (20, 175),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 120, 255),
                2,
            )
            cv2.putText(
                frame,
                f"Tarea: {context_metrics['time_on_task']:.0f}s  Circadian: x{context_metrics['time_of_day_multiplier']:.1f}  Luz: {context_metrics['illumination_state']}",
                (20, 200),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                (200, 255, 200),
                2,
            )
            if medical_metrics.get("medical_alert"):
                cv2.putText(frame, "ALERTA MEDICA CRITICA", (20, 230), cv2.FONT_HERSHEY_DUPLEX, 0.8, (0, 0, 255), 2)

            if SHOW_DEBUG_PANEL:
                _draw_metric_line(mesh_frame, "DATOS FACEMESH", 0, (255, 255, 255))
                _draw_metric_line(
                    mesh_frame,
                    f"EAR: {ear:.3f} ({'BAJO' if ear < params.ear_threshold else 'OK'})",
                    1,
                    (0, 255, 0) if ear >= params.ear_threshold else (0, 0, 255),
                )
                _draw_metric_line(
                    mesh_frame,
                    f"MAR: {mar:.3f} ({'BOSTEZO' if mar >= params.mar_yawn_threshold else 'OK'})",
                    2,
                    (0, 255, 255) if mar < params.mar_yawn_threshold else (0, 0, 255),
                )
                _draw_metric_line(
                    mesh_frame,
                    f"Cabeza - Pitch: {pitch:.1f}  Yaw: {yaw:.1f}  Roll: {roll:.1f}",
                    3,
                    (255, 200, 0),
                )
                _draw_metric_line(
                    mesh_frame,
                    (
                        f"Cabeza: {'ABAJO' if pitch >= params.head_pitch_threshold else 'OK'} "
                        f"(umbral={params.head_pitch_threshold:.1f}/{params.head_pitch_reset_threshold:.1f})"
                    ),
                    4,
                    (0, 0, 255) if pitch >= params.head_pitch_threshold else (0, 255, 0),
                )
                _draw_metric_line(mesh_frame, f"Frames ojos cerrados: {eye_counter}/{params.consec_frames}", 5)
                _draw_metric_line(
                    mesh_frame,
                    f"Bostezos: {yawn_count}  Activo: {yawn_active}  Detectado: {yawn_detected}",
                    6,
                    (0, 255, 255),
                )
                _draw_metric_line(
                    mesh_frame,
                    f"Cabeceos: {nod_count}  Activo: {nod_active}  Detectado: {nod_detected}",
                    7,
                    (255, 200, 0),
                )
                _draw_metric_line(
                    mesh_frame,
                    f"Frotado de ojos: {eye_rub_count}  Activo: {eye_rub_active}  Detectado: {eye_rub_detected}",
                    8,
                    (255, 0, 255),
                )
                _draw_metric_line(
                    mesh_frame,
                    f"Mano cerca del ojo: {'SI' if hand_near_eye else 'NO'}",
                    9,
                    (255, 0, 255) if hand_near_eye else (160, 160, 160),
                )
                if not results.multi_face_landmarks:
                    _draw_metric_line(mesh_frame, "No se detecta rostro", 10, (0, 140, 255))
                combined = np.hstack((frame, mesh_frame))
            else:
                combined = frame
            _draw_exit_button(combined)
            cv2.imshow("Monitor de Fatiga", combined)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if cv2.getWindowProperty("Monitor de Fatiga", cv2.WND_PROP_VISIBLE) < 1:
                break
    finally:
        medical_pipeline.close()
        buzzer.cleanup()
        face_mesh.close()
        hands.close()
        picam2.stop()
        cv2.destroyAllWindows()
        alarm_active = False
        alert_started_at = None


if __name__ == "__main__":
    main()
