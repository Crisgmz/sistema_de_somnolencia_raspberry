"""Persistencia SQLite del estado del fatigue score."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Dict


class ScoreStateStore:
    def __init__(self, db_path: str, vehicle_id: str, driver_id: str) -> None:
        self.db_path = db_path
        self.vehicle_id = vehicle_id
        self.driver_id = driver_id
        self.key = f"{vehicle_id}:{driver_id}"
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute(
            "create table if not exists score_state ("
            "key text primary key, "
            "vehicle_id text not null, "
            "driver_id text not null, "
            "payload text not null, "
            "updated_at real not null)"
        )
        self.conn.commit()

    def load(self) -> Dict:
        with self._lock:
            row = self.conn.execute("select payload, updated_at from score_state where key = ?", (self.key,)).fetchone()
        if not row:
            return {}
        try:
            payload = json.loads(row[0])
            payload["_updated_at"] = float(row[1])
            return payload
        except (TypeError, ValueError):
            return {}

    def save(self, payload: Dict, ts: float | None = None) -> None:
        updated_at = float(time.time() if ts is None else ts)
        data = json.dumps(payload)
        with self._lock:
            self.conn.execute(
                "insert or replace into score_state(key, vehicle_id, driver_id, payload, updated_at) values (?, ?, ?, ?, ?)",
                (self.key, self.vehicle_id, self.driver_id, data, updated_at),
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
