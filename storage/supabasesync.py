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
    # Cap de la cola local: con red caida (o LTE lentisimo) la cola SQLite no
    # debe crecer sin limite. Al superar el cap se descarta el backlog MAS VIEJO
    # de las tablas de alta frecuencia; eventos/emergencias/sesiones jamas.
    MAX_QUEUE_ROWS = 20000
    PRUNABLE_TABLES = ("telemetry_raw", "metrics_summary")

    def __init__(self, config: AppConfig, flush_interval_s: float = 15.0) -> None:
        super().__init__(daemon=True)
        self.config = config
        self.flush_interval_s = float(flush_interval_s)
        self._stop_event = threading.Event()
        self._flush_requested = threading.Event()
        self._flush_lock = threading.Lock()
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
        self._stats = {"queued": 0, "flushed": 0, "failed": 0, "dropped": 0, "last_error": "", "last_flush_ts": 0.0}
        self._consecutive_failures = 0
        self._enqueued_since_prune = 0
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
            self._enqueued_since_prune += 1
            if self._enqueued_since_prune >= 500:
                self._enqueued_since_prune = 0
                self._prune_queue_locked()
        self._stats["queued"] += 1
        if immediate:
            self._flush_requested.set()

    def _prune_queue_locked(self) -> None:
        """Descarta telemetria vieja si la cola supera el cap. Llamar con _db_lock."""
        total = self.conn.execute("select count(*) from queue").fetchone()[0]
        excess = int(total) - self.MAX_QUEUE_ROWS
        if excess <= 0:
            return
        placeholders = ",".join("?" for _ in self.PRUNABLE_TABLES)
        cur = self.conn.execute(
            f"delete from queue where id in ("
            f"select id from queue where table_name in ({placeholders}) and immediate = 0 order by id limit ?)",
            (*self.PRUNABLE_TABLES, excess),
        )
        self.conn.commit()
        dropped = cur.rowcount if cur.rowcount is not None else 0
        if dropped > 0:
            self._stats["dropped"] += dropped
            print(f"[SUPABASE] Cola > {self.MAX_QUEUE_ROWS}: descartadas {dropped} filas de telemetria antigua.")

    def enqueue_upsert(self, table_name: str, payload: Dict, conflict_target: str, immediate: bool = False) -> None:
        self.enqueue(table_name, payload, immediate=immediate, op="upsert", conflict_target=conflict_target)

    def _flush_once(self, force_immediate: bool = False) -> None:
        if not self.sb:
            return
        if not self._flush_lock.acquire(blocking=False):
            return
        try:
            where = "where immediate = 1" if force_immediate else ""
            with self._db_lock:
                rows = self.conn.execute(
                    f"select id, table_name, payload, op, conflict_target from queue {where} order by id limit 200"
                ).fetchall()
            # Agrupar filas CONSECUTIVAS con el mismo destino y enviarlas en UN
            # solo request (insert/upsert aceptan listas). Antes se enviaba fila
            # a fila: hasta 200 round-trips por flush, que con latencia LTE
            # (300-800 ms cada uno) tardaban minutos y la cola nunca vaciaba.
            groups: list[tuple[tuple, list[int], list[Dict]]] = []
            for row_id, table_name, payload_json, op, conflict_target in rows:
                key = (table_name, op, conflict_target)
                if not groups or groups[-1][0] != key:
                    groups.append((key, [], []))
                groups[-1][1].append(row_id)
                groups[-1][2].append(json.loads(payload_json))
            flushed_ids: list[int] = []
            for (table_name, op, conflict_target), ids, payloads in groups:
                batch_payloads = payloads
                if op == "upsert" and conflict_target:
                    # Postgres rechaza un upsert que toca la misma fila dos veces
                    # en el mismo lote: conservar solo la version MAS RECIENTE
                    # por clave de conflicto (las anteriores quedan superadas).
                    latest: dict = {}
                    for p in payloads:
                        latest[p.get(conflict_target)] = p
                    batch_payloads = list(latest.values())
                try:
                    if op == "upsert":
                        self.sb.table(table_name).upsert(batch_payloads, on_conflict=conflict_target).execute()
                    else:
                        self.sb.table(table_name).insert(batch_payloads).execute()
                    flushed_ids.extend(ids)
                    self._stats["flushed"] += len(ids)
                    self._consecutive_failures = 0
                except Exception:
                    # El lote fallo: reintento fila a fila para aislar una fila
                    # invalida sin bloquear al resto del lote.
                    for rid, payload in zip(ids, payloads):
                        try:
                            if op == "upsert":
                                self.sb.table(table_name).upsert(payload, on_conflict=conflict_target).execute()
                            else:
                                self.sb.table(table_name).insert(payload).execute()
                            flushed_ids.append(rid)
                            self._stats["flushed"] += 1
                            self._consecutive_failures = 0
                        except Exception as exc:
                            self._consecutive_failures += 1
                            self._stats["failed"] += 1
                            self._stats["last_error"] = str(exc)
                            if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                                break
                    if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                        print(f"[SUPABASE] {self._consecutive_failures} fallos consecutivos, pausando flush.")
                        break
            if flushed_ids:
                with self._db_lock:
                    self.conn.executemany("delete from queue where id = ?", [(rid,) for rid in flushed_ids])
                    self.conn.commit()
            self._stats["last_flush_ts"] = time.time()
        finally:
            self._flush_lock.release()

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
            self._flush_requested.clear()
            self._flush_once(force_immediate=False)
            if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                backoff = min(60.0, self.BACKOFF_BASE_S * (2 ** min(self._consecutive_failures // self.MAX_CONSECUTIVE_FAILURES, 5)))
                self._stop_event.wait(backoff)
            else:
                self._flush_requested.wait(self.flush_interval_s)

    def stop(self) -> None:
        if self.sb and self.is_alive():
            requested_at = time.time()
            self._flush_requested.set()
            while time.time() - requested_at < 2.0:
                if self._stats.get("last_flush_ts", 0.0) >= requested_at and not self._flush_lock.locked():
                    break
                time.sleep(0.05)

        self._stop_event.set()
        self._flush_requested.set()
        self.join(timeout=1.0)
        if self.is_alive():
            print("[SUPABASE] Sync no respondio al cierre; se conserva la cola local para el proximo inicio.")
            return
        with self._db_lock:
            pending = self.conn.execute("select count(*) from queue").fetchone()[0]
            if pending > 0:
                print(f"[SUPABASE] {pending} registros aun en cola al cerrar (se enviaran en proximo inicio).")
            self.conn.close()
