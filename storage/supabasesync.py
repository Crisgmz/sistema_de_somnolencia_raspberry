"""Sincronizacion SQLite -> Supabase en hilo separado."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Dict, Optional

from core.config import AppConfig

try:
    from supabase import create_client
except Exception:
    create_client = None


class SupabaseSync(threading.Thread):
    def __init__(self, config: AppConfig, flush_interval_s: float = 60.0) -> None:
        super().__init__(daemon=True)
        self.config = config
        self.flush_interval_s = float(flush_interval_s)
        self._stop = threading.Event()
        self.conn = sqlite3.connect(self.config.sqlite_queue_path, check_same_thread=False)
        self.conn.execute(
            "create table if not exists queue (id integer primary key autoincrement, table_name text not null, payload text not null, immediate int default 0, created_at real not null)"
        )
        self.conn.commit()
        self.sb = None
        if create_client and self.config.supabase_url and self.config.supabase_key:
            try:
                self.sb = create_client(self.config.supabase_url, self.config.supabase_key)
            except Exception:
                self.sb = None

    def enqueue(self, table_name: str, payload: Dict, immediate: bool = False) -> None:
        self.conn.execute(
            "insert into queue(table_name, payload, immediate, created_at) values (?, ?, ?, ?)",
            (table_name, json.dumps(payload), 1 if immediate else 0, time.time()),
        )
        self.conn.commit()
        if immediate:
            self._flush_once(force_immediate=True)

    def _flush_once(self, force_immediate: bool = False) -> None:
        if not self.sb:
            return
        where = "where immediate = 1" if force_immediate else ""
        rows = self.conn.execute(f"select id, table_name, payload from queue {where} order by id limit 200").fetchall()
        for row_id, table_name, payload_json in rows:
            payload = json.loads(payload_json)
            try:
                self.sb.table(table_name).insert(payload).execute()
                self.conn.execute("delete from queue where id = ?", (row_id,))
            except Exception:
                continue
        self.conn.commit()

    def run(self) -> None:
        while not self._stop.is_set():
            self._flush_once(force_immediate=False)
            self._stop.wait(self.flush_interval_s)

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=1.5)
        self.conn.close()
