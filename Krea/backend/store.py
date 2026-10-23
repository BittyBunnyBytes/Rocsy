from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UNFINISHED_JOB_STATES = ("waiting_for_gpu", "preparing", "queued", "enhancing", "rendering", "cancelling")


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
                    id TEXT PRIMARY KEY, prompt_id TEXT, prompt TEXT NOT NULL,
                    seed TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    enhance INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                    output_path TEXT, error TEXT, request_json TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                    lock_token TEXT, queue_entry_id TEXT,
                    model TEXT NOT NULL DEFAULT 'krea2', stage TEXT NOT NULL DEFAULT 'render', enhanced_prompt TEXT
                );
                -- Named settings presets per model (like H3 Machine's), shared by every browser.
                CREATE TABLE IF NOT EXISTS presets (
                    model TEXT NOT NULL, name TEXT NOT NULL, settings_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (model, name)
                );
                """
            )
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
            for column, definition in (
                ("queue_entry_id", "TEXT"),
                ("model", "TEXT NOT NULL DEFAULT 'krea2'"),
                # "enhance" while the Qwen prompt-enhancer pre-pass owns prompt_id, then "render".
                ("stage", "TEXT NOT NULL DEFAULT 'render'"),
                ("enhanced_prompt", "TEXT"),
                # Multi-image generations: every ComfyUI prompt id, and each image's output
                # path by index (null until that image is done). prompt_id/output_path
                # keep pointing at the first one.
                ("batch_count", "INTEGER NOT NULL DEFAULT 1"),
                ("prompt_ids", "TEXT"),
                ("outputs_json", "TEXT"),
                # Jobs other services render through Krea (e.g. GPU tuning benchmarks): never listed in the gallery or home.
                ("hidden", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in columns:
                    self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")

    def create_job(self, item: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO jobs
                   (id, prompt_id, prompt, seed, width, height, enhance, state, created_at, started_at,
                    completed_at, output_path, error, request_json, progress, lock_token, queue_entry_id,
                    model, stage, batch_count, hidden)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item["id"], None, item["prompt"], str(item["seed"]), item["width"], item["height"],
                 int(item["enhance"]), item["state"], item["created_at"], None, None, None, None,
                 json.dumps(item["request"]), 0, item.get("lock_token"), item.get("queue_entry_id"),
                 item.get("model", "krea2"), item.get("stage", "render"), int(item.get("batch_count", 1)),
                 int(bool(item.get("hidden")))),
            )

    def update_job(self, job_id: str, **changes: Any) -> None:
        allowed = {"prompt_id", "state", "started_at", "completed_at", "output_path", "error", "progress", "lock_token", "queue_entry_id", "stage", "enhanced_prompt", "width", "height", "prompt_ids", "outputs_json"}
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

    def visible_jobs(self) -> list[dict[str, Any]]:
        """The gallery's jobs: everything except hidden renders made for other apps."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs WHERE hidden = 0 ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def job_by_prompt(self, prompt_id: str) -> dict[str, Any] | None:
        """The job owning a ComfyUI prompt id (its first one, or any image of a multi-image job)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE prompt_id = ? OR prompt_ids LIKE ?", (prompt_id, f'%"{prompt_id}"%'),
            ).fetchone()
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

    def presets(self) -> dict[str, dict[str, Any]]:
        """{model: {name: settings}}"""
        with self._lock:
            rows = self._conn.execute("SELECT model, name, settings_json FROM presets").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            result.setdefault(row[0], {})[row[1]] = json.loads(row[2])
        return result

    def save_preset(self, model: str, name: str, settings: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO presets (model, name, settings_json, updated_at) VALUES (?, ?, ?, ?)",
                (model, name, json.dumps(settings), utc_now()),
            )

    def rename_preset(self, model: str, name: str, new_name: str) -> bool:
        with self._lock, self._conn:
            return self._conn.execute(
                "UPDATE presets SET name = ?, updated_at = ? WHERE model = ? AND name = ?", (new_name, utc_now(), model, name),
            ).rowcount > 0

    def delete_preset(self, model: str, name: str) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM presets WHERE model = ? AND name = ?", (model, name)).rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
