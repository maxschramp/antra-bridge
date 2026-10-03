"""
SQLite persistence: jobs + resolve cache. Survives restarts.

One connection guarded by a re-entrant lock; writes are small and rare enough
that this is simpler and safer than a connection pool. WAL mode so readers
(the API) never block on the worker's writes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  status TEXT NOT NULL,
  request TEXT NOT NULL,
  progress TEXT,
  result TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS resolve_cache (
  key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, db_path: Path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript("PRAGMA journal_mode=WAL;\n" + _SCHEMA)
            self._conn.commit()

    # -- jobs ---------------------------------------------------------------

    def create_job(self, job_id: str, request: dict, status: str = "queued") -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, created_at, updated_at, status, request) VALUES (?, ?, ?, ?, ?)",
                (job_id, now, now, status, json.dumps(request)),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def list_jobs(self, status: Optional[str] = None, limit: int = 50) -> list[dict]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_job(r) for r in rows]

    def update_job(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        progress: Optional[dict] = None,
        result: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> None:
        sets = ["updated_at = ?"]
        params: list[Any] = [time.time()]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if progress is not None:
            sets.append("progress = ?")
            params.append(json.dumps(progress))
        if result is not None:
            sets.append("result = ?")
            params.append(json.dumps(result))
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        params.append(job_id)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
            self._conn.commit()

    def count_jobs(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> dict:
        job = {
            "id": row["id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "status": row["status"],
            "request": json.loads(row["request"]) if row["request"] else None,
            "progress": json.loads(row["progress"]) if row["progress"] else None,
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"],
        }
        return job

    # -- resolve cache ------------------------------------------------------

    def cache_get(self, key: str) -> Optional[dict]:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, expires_at FROM resolve_cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                self._conn.execute("DELETE FROM resolve_cache WHERE key = ?", (key,))
                self._conn.commit()
                return None
        return json.loads(row["payload"])

    def cache_put(self, key: str, payload: dict, ttl_s: int) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO resolve_cache (key, payload, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (key, json.dumps(payload), now, now + max(0, ttl_s)),
            )
            self._conn.commit()

    def cache_delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM resolve_cache WHERE key = ?", (key,))
            self._conn.commit()
