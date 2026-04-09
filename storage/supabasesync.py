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
    MAX_CONSECUTIVE_FAILURES = 10
    BACKOFF_BASE_S = 2.0

    def __init__(self, config: AppConfig, flush_interval_s: float = 15.0) -> None:
        super().__init__(daemon=True)
        self.config = config
        self.flush_interval_s = float(flush_interval_s)
        self._stop_event = threading.Event()
        self._db_lock = threading.Lock()
        self.conn = sqlite3.connect(self.config.sqlite_queue_path, check_same_thread=False)
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute(
            "create table if not exists queue ("
            "id integer primary key autoincrement, "
            "table_name text not null, "
            "payload text not null, "
            "immediate int default 0, "
            "op text not null default 'insert', "
            "conflict_target text, "
            "created_at real not null)"
        )
        self._ensure_column("op", "text not null default 'insert'")
        self._ensure_column("conflict_target", "text")
        self.conn.commit()
        self.sb = None
        self._stats = {"queued": 0, "flushed": 0, "failed": 0, "last_error": "", "last_flush_ts": 0.0}
        self._consecutive_failures = 0
        if create_client and self.config.supabase_url and self.config.supabase_key:
            try:
                self.sb = create_client(self.config.supabase_url, self.config.supabase_key)
            except Exception as exc:
                self._stats["last_error"] = str(exc)
                self.sb = None

    def _ensure_column(self, name: str, definition: str) -> None:
        cols = {row[1] for row in self.conn.execute("pragma table_info(queue)").fetchall()}
        if name not in cols:
            self.conn.execute(f"alter table queue add column {name} {definition}")

    def stats(self) -> Dict:
        with self._db_lock:
            pending = self.conn.execute("select count(*) from queue").fetchone()[0]
        return {
            **self._stats,
            "enabled": bool(self.sb),
            "pending": pending,
        }

    def enqueue(self, table_name: str, payload: Dict, immediate: bool = False, op: str = "insert", conflict_target: str | None = None) -> None:
        with self._db_lock:
            self.conn.execute(
                "insert into queue(table_name, payload, immediate, op, conflict_target, created_at) values (?, ?, ?, ?, ?, ?)",
                (table_name, json.dumps(payload), 1 if immediate else 0, op, conflict_target, time.time()),
            )
            self.conn.commit()
        self._stats["queued"] += 1
        if immediate:
            self._flush_once(force_immediate=True)

    def enqueue_upsert(self, table_name: str, payload: Dict, conflict_target: str, immediate: bool = False) -> None:
        self.enqueue(table_name, payload, immediate=immediate, op="upsert", conflict_target=conflict_target)

    def _flush_once(self, force_immediate: bool = False) -> None:
        if not self.sb:
            return
        where = "where immediate = 1" if force_immediate else ""
        with self._db_lock:
            rows = self.conn.execute(
                f"select id, table_name, payload, op, conflict_target from queue {where} order by id limit 200"
            ).fetchall()
        flushed_ids: list[int] = []
        batch_failures = 0
        for row_id, table_name, payload_json, op, conflict_target in rows:
            payload = json.loads(payload_json)
            try:
                if op == "upsert":
                    self.sb.table(table_name).upsert(payload, on_conflict=conflict_target).execute()
                else:
                    self.sb.table(table_name).insert(payload).execute()
                flushed_ids.append(row_id)
                self._stats["flushed"] += 1
                self._consecutive_failures = 0
            except Exception as exc:
                batch_failures += 1
                self._consecutive_failures += 1
                self._stats["failed"] += 1
                self._stats["last_error"] = str(exc)
                if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                    print(f"[SUPABASE] {self._consecutive_failures} fallos consecutivos, pausando flush.")
                    break
        if flushed_ids:
            with self._db_lock:
                self.conn.executemany("delete from queue where id = ?", [(rid,) for rid in flushed_ids])
                self.conn.commit()
        self._stats["last_flush_ts"] = time.time()

    def drain_pending(self) -> int:
        """Flush all pending items from a prior session (e.g. after crash).
        Call BEFORE starting new session writes. Returns count flushed."""
        if not self.sb:
            return 0
        with self._db_lock:
            count = self.conn.execute("select count(*) from queue").fetchone()[0]
        if count > 0:
            print(f"[SUPABASE] Drenando {count} registros pendientes de sesion anterior...")
            self._flush_once(force_immediate=False)
        return count

    def run(self) -> None:
        self.drain_pending()
        while not self._stop_event.is_set():
            self._flush_once(force_immediate=False)
            if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                backoff = min(60.0, self.BACKOFF_BASE_S * (2 ** min(self._consecutive_failures // self.MAX_CONSECUTIVE_FAILURES, 5)))
                self._stop_event.wait(backoff)
            else:
                self._stop_event.wait(self.flush_interval_s)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=2.0)
        # Flush final: primero inmediatos (session end_time), luego el resto.
        self._flush_once(force_immediate=True)
        self._flush_once(force_immediate=False)
        with self._db_lock:
            pending = self.conn.execute("select count(*) from queue").fetchone()[0]
            if pending > 0:
                print(f"[SUPABASE] {pending} registros aun en cola al cerrar (se enviaran en proximo inicio).")
            self.conn.close()
