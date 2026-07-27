#!/usr/bin/env python3
"""Arnes de validacion offline: corre un VIDEO por el pipeline ocular real.

Reproduce un clip grabado (con la camara de la Pi o cualquier webcam) contra
el mismo codigo de produccion (core.vision + parametros.ojos, con la
calibracion por mediana) y reporta las metricas que definen la precision:

- Separacion EAR abierto/cerrado (p5 vs mediana) y umbral de cierre efectivo
- Parpadeos detectados y su duracion
- Eventos de microsueno (EYE_CLOSED_MS) y episodios de cierre sostenido
- PERCLOS maximo y eventos PERCLOS
- Tasa de eventos con peso por minuto (= falsos positivos/min si el clip es
  de conduccion normal)

Uso:
    python tools/validate_video.py clip.mp4
    python tools/validate_video.py clip.mp4 --calib-seconds 20 --csv salida.csv

Criterio del plan (docs/PLAN_TRABAJO_PRECISION.md): en clips de conduccion
normal (con o sin lentes) la tasa de eventos debe ser 0/min; en microsuenos
simulados la deteccion debe llegar en < 2 s.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402
import mediapipe as mp  # noqa: E402
import numpy as np  # noqa: E402

from core.calibration import Calibration  # noqa: E402
from core.vision import OJO_DER, OJO_IZQ, eye_metrics  # noqa: E402
from parametros.ojos import OjosParametros  # noqa: E402


def _eye_centers(lm, w: int, h: int):
    def center(points):
        xs = [lm[i].x * w for i in points]
        ys = [lm[i].y * h for i in points]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    return center(OJO_IZQ), center(OJO_DER)


def main() -> int:
    ap = argparse.ArgumentParser(description="Valida el pipeline ocular contra un video grabado")
    ap.add_argument("video", help="ruta del clip (mp4/avi) grabado de frente al conductor")
    ap.add_argument("--calib-seconds", type=float, default=30.0, help="segundos iniciales usados para calibrar el baseline EAR (default 30)")
    ap.add_argument("--close-frac", type=float, default=float(os.getenv("SOMNO_EAR_CLOSE_FRAC", "0.70")), help="fraccion del baseline usada como umbral de cierre")
    ap.add_argument("--csv", default="", help="ruta opcional para volcar (ts, ear, cerrado, evento) por frame")
    args = ap.parse_args()

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"[ERROR] No se pudo abrir {args.video}")
        return 1
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if fps <= 1.0:
        fps = 30.0
    print(f"[INFO] {args.video} | {fps:.1f} FPS")

    face_mesh = mp.solutions.face_mesh.FaceMesh(
        static_image_mode=False,
        refine_landmarks=True,
        max_num_faces=1,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )

    # Pasada unica: se calibra con los primeros N segundos y despues se evalua.
    calibration = Calibration()
    ojos = OjosParametros()
    calib_samples: list[float] = []
    ears: list[float] = []
    rows: list[tuple] = []
    events: dict[str, int] = {}
    closure_episodes: list[float] = []
    blink_count = 0
    perclos_max = 0.0
    frames_with_face = 0
    frame_idx = 0
    eval_start_ts = None
    last_eye_closed_ms = 0.0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        ts = frame_idx / fps
        frame_idx += 1
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        out = face_mesh.process(rgb)
        if not out.multi_face_landmarks:
            continue
        frames_with_face += 1
        h, w = frame.shape[:2]
        lm = out.multi_face_landmarks[0].landmark
        ear_l, _ = eye_metrics(lm, OJO_IZQ, w, h)
        ear_r, _ = eye_metrics(lm, OJO_DER, w, h)
        ear = 0.5 * (ear_l + ear_r)
        lc, rc = _eye_centers(lm, w, h)

        if ts < args.calib_seconds:
            if 0.08 <= ear <= 0.50:
                calib_samples.append(ear)
            continue

        if not calibration.calibrated:
            if not calib_samples:
                print("[ERROR] Sin muestras de calibracion (no se detecto rostro al inicio)")
                return 1
            arr = np.asarray(calib_samples)
            p20 = float(np.percentile(arr, 20.0))
            open_samples = arr[arr >= p20]
            calibration.ear_baseline = float(np.median(open_samples if open_samples.size else arr))
            calibration.calibrated = True
            eval_start_ts = ts
            close_thr = max(0.10, args.close_frac * calibration.ear_baseline)
            print(f"[CALIB] baseline EAR={calibration.ear_baseline:.3f} | umbral cierre={close_thr:.3f} ({len(calib_samples)} muestras)")

        result = ojos.update(ts, ear, lc, rc, calibration)
        ears.append(ear)
        eye_closed_ms = float(result["EYE_CLOSED_MS"]["value"])
        perclos_max = max(perclos_max, float(result["PERCLOS"]["value"]))
        if result.get("blink_detected"):
            blink_count += 1
        # Fin de un episodio de cierre: el contador vuelve a 0.
        if last_eye_closed_ms > 0.0 and eye_closed_ms == 0.0:
            closure_episodes.append(last_eye_closed_ms)
        last_eye_closed_ms = eye_closed_ms

        frame_event = False
        for pid in ("EYE_CLOSED_MS", "PERCLOS"):
            if result[pid]["eventflag"]:
                events[pid] = events.get(pid, 0) + 1
                frame_event = True
        if args.csv:
            rows.append((round(ts, 3), round(ear, 4), 1 if eye_closed_ms > 0 else 0, 1 if frame_event else 0))

    cap.release()
    face_mesh.close()

    if not ears:
        print("[ERROR] El video no cubre la fase de evaluacion (¿clip mas corto que --calib-seconds?)")
        return 1

    arr = np.asarray(ears)
    eval_minutes = max(1e-6, (len(ears) / fps) / 60.0)
    total_events = sum(events.values())
    close_thr = max(0.10, args.close_frac * calibration.ear_baseline)

    print("\n===== RESULTADO =====")
    print(f"Frames con rostro: {frames_with_face}/{frame_idx} ({100.0 * frames_with_face / max(1, frame_idx):.0f}%)")
    print(f"EAR evaluacion: mediana={np.median(arr):.3f} p5={np.percentile(arr, 5):.3f} min={arr.min():.3f}")
    print(f"Umbral de cierre: {close_thr:.3f} (baseline {calibration.ear_baseline:.3f} x {args.close_frac})")
    margen = float(np.percentile(arr, 5)) - close_thr
    print(f"Margen p5-umbral: {margen:+.3f}  ({'OK: ojos abiertos lejos del umbral' if margen > 0.02 else 'RIESGO: EAR abierto roza el umbral'})")
    print(f"Parpadeos: {blink_count} ({blink_count / eval_minutes:.1f}/min)")
    if closure_episodes:
        print(f"Episodios de cierre: {len(closure_episodes)} | max={max(closure_episodes):.0f} ms")
    print(f"PERCLOS max: {perclos_max:.3f}")
    print(f"Eventos con peso: {dict(events) or 'ninguno'} -> {total_events / eval_minutes:.2f} eventos/min")
    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            wcsv = csv.writer(fh)
            wcsv.writerow(["ts_s", "ear", "cerrado", "evento"])
            wcsv.writerows(rows)
        print(f"[INFO] Detalle por frame en {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
