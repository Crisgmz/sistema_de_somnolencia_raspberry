"""Persistencia SQLite de la calibracion por conductor.

Evita recalibrar 5 min en cada arranque (ventana en la que el sistema va
desprotegido). Se guarda al terminar la calibracion y en el apagado; se carga
al iniciar si hay una calibracion reciente y valida para ese conductor.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Dict, Optional


class CalibrationStore:
    def __init__(self, db_path: str, vehicle_id: str, driver_id: str) -> None:
        self.db_path = db_path
        self.vehicle_id = vehicle_id
        self.driver_id = driver_id
        self.key = f"{vehicle_id}:{driver_id}"
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute(
            "create table if not exists calibration_state ("
            "key text primary key, "
            "vehicle_id text not null, "
            "driver_id text not null, "
            "payload text not null, "
            "updated_at real not null)"
        )
        self.conn.commit()

    def load(self, max_age_s: Optional[float] = None) -> Dict:
        """Devuelve el payload guardado (con `_updated_at`), o {} si no hay o
        si supera `max_age_s` (calibracion demasiado vieja para fiarse)."""
        with self._lock:
            row = self.conn.execute(
                "select payload, updated_at from calibration_state where key = ?",
                (self.key,),
            ).fetchone()
        if not row:
            return {}
        try:
            updated_at = float(row[1])
            if max_age_s is not None and (time.time() - updated_at) > float(max_age_s):
                return {}
            payload = json.loads(row[0])
            payload["_updated_at"] = updated_at
            return payload
        except (TypeError, ValueError):
            return {}

    def save(self, payload: Dict, ts: float | None = None) -> None:
        updated_at = float(time.time() if ts is None else ts)
        data = json.dumps(payload)
        with self._lock:
            self.conn.execute(
                "insert or replace into calibration_state(key, vehicle_id, driver_id, payload, updated_at) values (?, ?, ?, ?, ?)",
                (self.key, self.vehicle_id, self.driver_id, data, updated_at),
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
