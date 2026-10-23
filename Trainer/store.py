from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TrainerStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._guard = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, model TEXT NOT NULL, name TEXT NOT NULL,
                dataset TEXT NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL,
                created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                error TEXT, queue_entry_id TEXT, lock_token TEXT,
                request_json TEXT NOT NULL, log_text TEXT NOT NULL DEFAULT ''
            )""")

    def create(self, item: dict[str, Any]) -> None:
        with self._guard, self._db:
            self._db.execute(
                """INSERT INTO jobs
                (id,model,name,dataset,state,stage,created_at,request_json)
                VALUES (:id,:model,:name,:dataset,:state,:stage,:created_at,:request_json)""",
                {**item, "request_json": json.dumps(item["request"], separators=(",", ":"))},
            )

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._guard:
            row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._guard:
            rows = self._db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"state", "stage", "started_at", "completed_at", "error", "queue_entry_id", "lock_token"}
        if fields.keys() - allowed:
            raise ValueError("Unsupported trainer job field")
        clause = ",".join(f"{name}=?" for name in fields)
        with self._guard, self._db:
            self._db.execute(f"UPDATE jobs SET {clause} WHERE id=?", (*fields.values(), job_id))

    def append_log(self, job_id: str, line: str) -> None:
        with self._guard, self._db:
            self._db.execute(
                "UPDATE jobs SET log_text=substr(log_text || ?, -1000000) WHERE id=?",
                (line, job_id),
            )

    def delete(self, job_id: str) -> None:
        with self._guard, self._db:
            self._db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def stale(self) -> list[dict[str, Any]]:
        with self._guard:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE state IN ('waiting_for_gpu','preparing','running','cancelling')"
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._guard:
            self._db.close()
