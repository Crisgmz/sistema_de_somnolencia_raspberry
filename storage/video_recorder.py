"""Grabador opcional de video DESDE el pipeline en vivo, para el arnes offline.

El sistema corre en vivo: no hay una fase aparte de "grabar clips". Cuando se
activa (SOMNO_RECORD_VIDEO=1), el bucle principal escribe el frame CRUDO (ya
rotado, sin HUD ni overlays) a `recordings/<session_id>.avi` mientras el
sistema opera con normalidad. El clip resultante es exactamente lo que vio el
detector y alimenta `tools/validate_video.py`.

Decisiones:
- Codec MJPG en .avi: barato de CPU en la Pi 4 (sin encoder h264 por software).
- FPS fijo objetivo (SOMNO_RECORD_FPS, default 10): se escribe a cadencia
  regular con reloj monotonico, de modo que el timestamp reconstruido por el
  validador (frame/fps) sea correcto aunque el FPS real del pipeline varie.
- Tope de duracion (SOMNO_RECORD_MAX_S, default 600 s) para no llenar la SD:
  a 640x480 MJPG son ~50-100 MB por 10 min.
- Nunca tumba el bucle principal: cualquier error de E/S desactiva el grabador.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import cv2
import numpy as np


class VideoRecorder:
    def __init__(self, session_id: str, enabled: Optional[bool] = None, directory: Optional[str] = None) -> None:
        if enabled is None:
            enabled = os.getenv("SOMNO_RECORD_VIDEO", "0").strip().lower() in ("1", "true", "yes", "on")
        self.enabled = bool(enabled)
        self.directory = directory or os.getenv("SOMNO_RECORD_DIR", "recordings")
        self.fps = max(1.0, float(os.getenv("SOMNO_RECORD_FPS", "10")))
        self.max_seconds = float(os.getenv("SOMNO_RECORD_MAX_S", "600"))
        self.path = os.path.join(self.directory, f"{session_id}.avi")
        self._writer: Optional[cv2.VideoWriter] = None
        self._last_write_mono = 0.0
        self._started_mono = 0.0
        self._frames_written = 0

    def write(self, frame: np.ndarray) -> None:
        """Escribe el frame si toca por cadencia. Llamar cada iteracion."""
        if not self.enabled or frame is None:
            return
        now = time.monotonic()
        if self._writer is None:
            try:
                os.makedirs(self.directory, exist_ok=True)
                h, w = frame.shape[:2]
                self._writer = cv2.VideoWriter(self.path, cv2.VideoWriter_fourcc(*"MJPG"), self.fps, (w, h))
                if not self._writer.isOpened():
                    raise RuntimeError("VideoWriter no abrio")
                self._started_mono = now
                self._last_write_mono = 0.0
                print(f"[REC] Grabando video en {self.path} ({w}x{h} @ {self.fps:.0f} FPS, max {self.max_seconds:.0f}s)")
            except Exception as exc:
                print(f"[REC] Grabacion de video deshabilitada: {exc}")
                self.enabled = False
                self._writer = None
                return
        if (now - self._started_mono) >= self.max_seconds:
            print(f"[REC] Tope de {self.max_seconds:.0f}s alcanzado; video cerrado ({self._frames_written} frames).")
            self.close()
            self.enabled = False
            return
        if (now - self._last_write_mono) < (1.0 / self.fps):
            return
        try:
            self._writer.write(frame)
            self._frames_written += 1
            self._last_write_mono = now
        except Exception as exc:
            print(f"[REC] Error escribiendo video, se desactiva: {exc}")
            self.enabled = False
            self.close()

    def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.release()
                if self._frames_written > 0:
                    print(f"[REC] Video guardado: {self.path} ({self._frames_written} frames)")
            except Exception:
                pass
            self._writer = None
