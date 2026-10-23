from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UNFINISHED_JOB_STATES = ("waiting_for_gpu", "preparing", "queued", "rendering", "cancelling")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, prompt_id TEXT, style TEXT NOT NULL, lyrics TEXT NOT NULL,
                    seed TEXT NOT NULL, duration REAL NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                    output_path TEXT, error TEXT, request_json TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                    lock_token TEXT
                );
                """
            )
            # Manual additive migration, matching Video's store.py idiom.
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "lock_token" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN lock_token TEXT")
            if "queue_entry_id" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN queue_entry_id TEXT")

    def create_job(self, item: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO jobs
                   (id, prompt_id, style, lyrics, seed, duration, state, created_at, started_at,
                    completed_at, output_path, error, request_json, progress, lock_token, queue_entry_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item["id"], None, item["style"], item["lyrics"], str(item["seed"]), item["duration"], item["state"],
                 item["created_at"], None, None, None, None, json.dumps(item["request"]), 0, item.get("lock_token"),
                 item.get("queue_entry_id")),
            )

    def update_job(self, job_id: str, **changes: Any) -> None:
        allowed = {"prompt_id", "state", "started_at", "completed_at", "output_path", "error", "progress", "lock_token", "queue_entry_id"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return
        statement = ", ".join(f"{key} = ?" for key in values)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE jobs SET {statement} WHERE id = ?", (*values.values(), job_id))

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def job_by_prompt(self, prompt_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE prompt_id = ?", (prompt_id,)).fetchone()
        return dict(row) if row else None

    def delete_job(self, job_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cursor.rowcount > 0

    def unfinished_jobs(self) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in UNFINISHED_JOB_STATES)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY created_at",
                UNFINISHED_JOB_STATES,
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_unfinished_jobs(self) -> int:
        placeholders = ", ".join("?" for _ in UNFINISHED_JOB_STATES)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"DELETE FROM jobs WHERE state IN ({placeholders})",
                UNFINISHED_JOB_STATES,
            )
        return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
