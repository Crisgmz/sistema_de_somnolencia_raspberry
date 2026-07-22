"""Grabador local de sesiones en JSONL para validacion de precision offline.

Cuando se activa (SOMNO_RECORD_SESSION=1), escribe una linea JSON por segundo
con la decision de somnolencia y las metricas clave de cada frame. Esos archivos
(`recordings/<session_id>.jsonl`) alimentan tools/validate_precision.py sin
depender de Supabase.

Diseño: nunca debe tumbar el bucle principal; cualquier error de E/S se traga y
se desactiva el grabador.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Optional, TextIO


class SessionRecorder:
    def __init__(
        self,
        session_id: str,
        enabled: Optional[bool] = None,
        directory: Optional[str] = None,
        min_interval_s: float = 1.0,
    ) -> None:
        if enabled is None:
            enabled = os.getenv("SOMNO_RECORD_SESSION", "0").strip().lower() in ("1", "true", "yes", "on")
        self.enabled = bool(enabled)
        self.session_id = str(session_id)
        self.directory = directory or os.getenv("SOMNO_RECORD_DIR", "recordings")
        self.min_interval_s = float(min_interval_s)
        self._last_write_ts = 0.0
        self._fh: Optional[TextIO] = None
        if self.enabled:
            try:
                os.makedirs(self.directory, exist_ok=True)
                path = os.path.join(self.directory, f"{self.session_id}.jsonl")
                self._fh = open(path, "a", buffering=1, encoding="utf-8")
                print(f"[REC] Grabando sesion en {path}")
            except Exception as exc:  # noqa: BLE001
                print(f"[REC] No se pudo abrir el archivo de grabacion: {exc}")
                self.enabled = False
                self._fh = None

    def append(self, ts: float, record: Dict) -> None:
        if not self.enabled or self._fh is None:
            return
        if (ts - self._last_write_ts) < self.min_interval_s:
            return
        self._last_write_ts = ts
        try:
            self._fh.write(json.dumps(record, separators=(",", ":"), default=float) + "\n")
        except Exception:  # noqa: BLE001
            # No romper el pipeline por un fallo de escritura.
            self.enabled = False

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._fh = None


def build_record(ts: float, telemetry: Dict, param_values: Dict[str, float]) -> Dict:
    """Arma el registro compacto que se persiste por segundo."""
    d = telemetry.get("drowsiness", {})
    emg = telemetry.get("emergency", {})
    return {
        "ts": float(ts),
        "committed_level": int(d.get("committed_level", telemetry.get("score", {}).get("level", 0))),
        "raw_level": int(d.get("raw_level", 0)),
        "fatigue_score": int(d.get("fatigue_score", 0)),
        "corroborated": bool(d.get("corroborated", True)),
        "active_families": d.get("active_families", []),
        "active_events": d.get("active_events", []),
        "face_quality_ok": bool(d.get("face_quality_ok", False)),
        "emergency": bool(emg.get("emergencyflag", False)),
        "emergency_type": emg.get("emergencytype"),
        # Metricas clave para analisis por-metrica en el arnes.
        "perclos": float(param_values.get("PERCLOS", 0.0)),
        "ear": float(param_values.get("EAR", 0.0)),
        "eye_closed_ms": float(param_values.get("EYE_CLOSED_MS", 0.0)),
        "mar": float(param_values.get("MAR", 0.0)),
        "blink_fb": float(param_values.get("BLINK_FB", 0.0)),
        "pitch": float(param_values.get("PITCH", 0.0)),
        "landmark_stability": float(d.get("landmark_stability", 0.0)),
    }
