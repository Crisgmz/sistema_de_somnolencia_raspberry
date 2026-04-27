"""Event store con cache RAM y respaldo SQLite para ventanas temporales."""

from __future__ import annotations

from collections import deque
import json
import sqlite3
import threading
from typing import Deque, Dict, Iterable, List


class EventStore:
    def __init__(self, db_path: str | None = None, maxlen: int = 20000, retention_seconds: float = 2 * 60 * 60) -> None:
        self._events: Deque[Dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._active_params: set[str] = set()
        self.retention_seconds = float(retention_seconds)
        self.conn: sqlite3.Connection | None = None
        if db_path:
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self.conn.execute("pragma journal_mode=wal")
            self.conn.execute(
                "create table if not exists event_history ("
                "id integer primary key autoincrement, "
                "timestamp real not null, "
                "paramid text not null, "
                "value real not null, "
                "eventflag int not null, "
                "payload text not null)"
            )
            self.conn.execute("create index if not exists idx_event_history_ts on event_history(timestamp)")
            self.conn.commit()
            self._load_recent()

    def append(self, event: Dict) -> None:
        with self._lock:
            self._events.append(event)
            param_id = str(event.get("paramid", ""))
            is_active = bool(event.get("eventflag", False))
            should_persist = is_active and param_id and param_id not in self._active_params
            if is_active and param_id:
                self._active_params.add(param_id)
            elif param_id:
                self._active_params.discard(param_id)
            if self.conn and should_persist:
                self.conn.execute(
                    "insert into event_history(timestamp, paramid, value, eventflag, payload) values (?, ?, ?, ?, ?)",
                    (
                        float(event.get("timestamp", 0.0)),
                        str(event.get("paramid", "")),
                        float(event.get("value", 0.0)),
                        1,
                        json.dumps(event),
                    ),
                )
                self.conn.commit()
                self._prune_db(float(event.get("timestamp", 0.0)))

    def extend(self, events: Iterable[Dict]) -> None:
        for event in events:
            self.append(event)

    def window(self, now_ts: float, window_seconds: float) -> List[Dict]:
        start_ts = float(now_ts) - float(window_seconds)
        if self.conn:
            with self._lock:
                rows = self.conn.execute(
                    "select payload from event_history where timestamp >= ? order by timestamp",
                    (start_ts,),
                ).fetchall()
            return [json.loads(row[0]) for row in rows]
        with self._lock:
            return [e for e in self._events if float(e.get("timestamp", 0.0)) >= start_ts]

    def all(self) -> List[Dict]:
        with self._lock:
            return list(self._events)

    def close(self) -> None:
        if self.conn:
            with self._lock:
                self.conn.close()
                self.conn = None

    def _load_recent(self) -> None:
        if not self.conn:
            return
        rows = self.conn.execute(
            "select payload from event_history where timestamp >= strftime('%s','now') - ? order by timestamp",
            (self.retention_seconds,),
        ).fetchall()
        self._events.extend(json.loads(row[0]) for row in rows)

    def _prune_db(self, now_ts: float) -> None:
        if not self.conn:
            return
        cutoff = float(now_ts) - self.retention_seconds
        self.conn.execute("delete from event_history where timestamp < ?", (cutoff,))
        self.conn.commit()
