"""Persistent task and result history for generated videos."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional


class VideoJobStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS video_jobs (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    request_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    channel_name TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    video_path TEXT NOT NULL DEFAULT '',
                    video_url TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(video_jobs)").fetchall()
            }
            if "request_json" not in columns:
                connection.execute(
                    "ALTER TABLE video_jobs ADD COLUMN request_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_video_jobs_owner_created ON video_jobs(owner_id, created_at DESC)")

    def create(
        self,
        job_id: str,
        owner_id: str,
        prompt: str,
        request: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = time.time()
        request_json = json.dumps(request or {}, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO video_jobs(id, owner_id, prompt, request_json, status, created_at, updated_at) VALUES(?, ?, ?, ?, 'queued', ?, ?)",
                (job_id, str(owner_id), str(prompt), request_json, now, now),
            )

    def update(self, job_id: str, **values: Any) -> None:
        allowed = {"status", "channel_name", "model", "video_path", "video_url", "error", "request_json"}
        updates = {key: values[key] for key in allowed if key in values}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        params = list(updates.values()) + [time.time(), job_id]
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE video_jobs SET {assignments}, updated_at = ? WHERE id = ?",
                params,
            )

    def update_if_status(self, job_id: str, expected: List[str], **values: Any) -> bool:
        allowed = {"status", "channel_name", "model", "video_path", "video_url", "error"}
        updates = {key: values[key] for key in allowed if key in values}
        statuses = [str(status) for status in expected if str(status)]
        if not updates or not statuses:
            return False
        assignments = ", ".join(f"{key} = ?" for key in updates)
        placeholders = ", ".join("?" for _ in statuses)
        params = list(updates.values()) + [time.time(), job_id] + statuses
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE video_jobs SET {assignments}, updated_at = ? WHERE id = ? AND status IN ({placeholders})",
                params,
            )
        return cursor.rowcount == 1

    def get(self, job_id: str, *, owner_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM video_jobs WHERE id = ?"
        params: List[Any] = [job_id]
        if owner_id is not None:
            query += " AND owner_id = ?"
            params.append(str(owner_id))
        with self._lock, self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return dict(row) if row else None

    def list_recent(self, *, owner_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM video_jobs WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?",
                (str(owner_id), max(1, min(100, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_active(self, *, owner_id: str) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM video_jobs WHERE owner_id = ? AND status IN ('queued', 'running') ORDER BY created_at",
                (str(owner_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_count(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM video_jobs WHERE status IN ('queued', 'running')"
            ).fetchone()
        return int(row["total"] if row else 0)

    def latest_created_at(self, owner_id: str) -> float:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT created_at FROM video_jobs WHERE owner_id = ? ORDER BY created_at DESC LIMIT 1",
                (str(owner_id),),
            ).fetchone()
        return float(row["created_at"]) if row else 0.0

    def list_all_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM video_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_video_paths_to_prune(self, keep: int) -> List[Dict[str, str]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id, video_path FROM video_jobs "
                "WHERE status = 'succeeded' AND video_path <> '' "
                "ORDER BY updated_at DESC, created_at DESC, id DESC LIMIT -1 OFFSET ?",
                (max(1, min(10000, int(keep))),),
            ).fetchall()
        return [{"id": str(row["id"]), "video_path": str(row["video_path"])} for row in rows]

    def clear_video_path(self, job_id: str, expected_path: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE video_jobs SET video_path = '' "
                "WHERE id = ? AND status = 'succeeded' AND video_path = ?",
                (str(job_id), str(expected_path)),
            )
        return cursor.rowcount == 1

    def list_jobs(
        self,
        *,
        status: str = "",
        search: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        if search:
            clauses.append("(id LIKE ? OR prompt LIKE ? OR channel_name LIKE ? OR model LIKE ?)")
            pattern = f"%{str(search)[:160]}%"
            params.extend([pattern] * 4)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, min(200, int(limit))), max(0, int(offset))])
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM video_jobs{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def recover_interrupted(self) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE video_jobs SET status = 'failed', error = ?, updated_at = ? WHERE status IN ('queued', 'running')",
                ("插件重启时任务状态未知；请检查上游任务后再手动重试。", time.time()),
            )
        return cursor.rowcount
