from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UNFINISHED_JOB_STATES = ("waiting_for_gpu", "preparing", "queued", "rendering", "cancelling", "finalizing")


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
                CREATE TABLE IF NOT EXISTS h3_references (
                    id TEXT PRIMARY KEY, stored_name TEXT NOT NULL, filename TEXT NOT NULL,
                    alias TEXT NOT NULL, mime_type TEXT NOT NULL, created_at TEXT NOT NULL,
                    role TEXT
                );
                -- One row per finished render: how long it took, keyed by the
                -- settings that drive render time (see backend/estimates.py).
                CREATE TABLE IF NOT EXISTS render_times (
                    job_id TEXT PRIMARY KEY, settings_key TEXT NOT NULL, settings_json TEXT NOT NULL,
                    seconds REAL NOT NULL, completed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS render_times_by_key ON render_times (settings_key, completed_at);
                -- Extreme thumbs up (2, best quality possible) / up (1) / down (-1) per finished video, keyed by the settings
                -- that shape its look (see estimates.quality_settings), for the score
                -- shown next to the time estimate. Every rating counts - no cap.
                CREATE TABLE IF NOT EXISTS ratings (
                    job_id TEXT PRIMARY KEY, quality_key TEXT NOT NULL, settings_json TEXT NOT NULL,
                    rating INTEGER NOT NULL, rated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ratings_by_key ON ratings (quality_key);
                -- Named settings presets (MiniMax H3 / LTX 2.5), shared by every browser.
                -- Small app-wide values, e.g. which settings torch.compile is warm for.
                CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS presets (
                    model TEXT NOT NULL, name TEXT NOT NULL, settings_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY (model, name)
                );
                -- Prompt aliases for LoRA trigger words, per LoRA family (minimax_h3 / ltx_2_5).
                CREATE TABLE IF NOT EXISTS trigger_aliases (
                    model TEXT NOT NULL, alias TEXT NOT NULL COLLATE NOCASE, lora TEXT NOT NULL,
                    target TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (model, alias)
                );
                -- Web Push subscriptions, one per browser/device that turned alerts on.
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    endpoint TEXT PRIMARY KEY, subscription_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, prompt_id TEXT, model TEXT NOT NULL DEFAULT 'minimax_h3', mode TEXT NOT NULL, prompt TEXT NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                    output_path TEXT, error TEXT, width INTEGER, height INTEGER, duration REAL,
                    request_json TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                    candidate_outputs TEXT, stage TEXT, queue_entry_id TEXT
                );
                """
            )
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "progress" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN progress INTEGER NOT NULL DEFAULT 0")
            if "model" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN model TEXT NOT NULL DEFAULT 'minimax_h3'")
            if "candidate_outputs" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN candidate_outputs TEXT")
            if "stage" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN stage TEXT")
            if "queue_entry_id" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN queue_entry_id TEXT")
            if "fps" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN fps REAL")
            if "retry_expires_at" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN retry_expires_at TEXT")
            if "rating" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN rating INTEGER")
            if "phase2" not in columns:  # JSON {status, progress, chunk, chunks} of the H3 latent upscale pass
                self._conn.execute("ALTER TABLE jobs ADD COLUMN phase2 TEXT")
            if "rating_reasons" not in columns:  # JSON list of estimates.RATING_REASONS keys
                self._conn.execute("ALTER TABLE jobs ADD COLUMN rating_reasons TEXT")
            if "timings" not in columns:  # JSON {runs: [...]} - per-node wall time, for the info panel's Stats tab
                self._conn.execute("ALTER TABLE jobs ADD COLUMN timings TEXT")
            # job_by_prompt runs on every ComfyUI websocket event; without this it scans every job's request_json.
            self._conn.execute("CREATE INDEX IF NOT EXISTS jobs_by_prompt ON jobs (prompt_id)")
            if "reasons" not in {row[1] for row in self._conn.execute("PRAGMA table_info(ratings)").fetchall()}:
                self._conn.execute("ALTER TABLE ratings ADD COLUMN reasons TEXT")
            reference_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(h3_references)").fetchall()}
            if "role" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN role TEXT")
            if "duration" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN duration REAL")
            if "label" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN label TEXT")
            if "muted" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN muted INTEGER NOT NULL DEFAULT 0")
            if "audio_only" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN audio_only INTEGER NOT NULL DEFAULT 0")
            if "has_audio" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN has_audio INTEGER")
            if "trim_start" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN trim_start REAL NOT NULL DEFAULT 0")
            if "trim_end" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN trim_end REAL")
            if "original_stored_name" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN original_stored_name TEXT")
            if "crop_x" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN crop_x REAL")
            if "crop_y" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN crop_y REAL")
            if "crop_w" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN crop_w REAL")
            if "crop_h" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN crop_h REAL")
            if "thumbnail_stored_name" not in reference_columns:
                self._conn.execute("ALTER TABLE h3_references ADD COLUMN thumbnail_stored_name TEXT")

    def add_reference(self, item: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO h3_references (id, stored_name, filename, alias, mime_type, created_at, role, duration, "
                "label, muted, has_audio, audio_only, trim_start, trim_end, original_stored_name, crop_x, crop_y, crop_w, crop_h, "
                "thumbnail_stored_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item["id"], item["stored_name"], item["filename"], item["alias"], item["mime_type"], item["created_at"],
                 item.get("role"), item.get("duration"), item.get("label"), int(item.get("muted", False)),
                 item.get("has_audio"), int(item.get("audio_only", False)), item.get("trim_start", 0.0), item.get("trim_end"),
                 item.get("original_stored_name"), item.get("crop_x"), item.get("crop_y"), item.get("crop_w"),
                 item.get("crop_h"), item.get("thumbnail_stored_name")),
            )

    def update_reference(self, ref_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {
            "label", "alias", "muted", "has_audio", "audio_only", "trim_start", "trim_end", "stored_name", "mime_type", "duration",
            "original_stored_name", "crop_x", "crop_y", "crop_w", "crop_h", "thumbnail_stored_name",
        }
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return self.reference(ref_id)
        if "muted" in values:
            values["muted"] = int(bool(values["muted"]))
        if "audio_only" in values:
            values["audio_only"] = int(bool(values["audio_only"]))
        if "has_audio" in values and values["has_audio"] is not None:
            values["has_audio"] = int(bool(values["has_audio"]))
        statement = ", ".join(f"{key} = ?" for key in values)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE h3_references SET {statement} WHERE id = ?", (*values.values(), ref_id))
        return self.reference(ref_id)

    def references(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM h3_references ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def reference(self, ref_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM h3_references WHERE id = ?", (ref_id,)).fetchone()
        return dict(row) if row else None

    def delete_reference(self, ref_id: str) -> dict[str, Any] | None:
        item = self.reference(ref_id)
        if item:
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM h3_references WHERE id = ?", (ref_id,))
        return item

    def create_job(self, item: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO jobs
                   (id, prompt_id, model, mode, prompt, state, created_at, started_at,
                    completed_at, output_path, error, width, height, duration, request_json, progress, stage, queue_entry_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item["id"], None, item["model"], item["mode"], item["prompt"], item["state"], item["created_at"],
                 None, None, None, None, item["width"], item["height"], item["duration"], json.dumps(item["request"]), 0,
                 item.get("stage"), item.get("queue_entry_id")),
            )

    def update_job(self, job_id: str, **changes: Any) -> None:
        allowed = {
            "prompt_id", "state", "started_at", "completed_at", "output_path", "error", "progress",
            "candidate_outputs", "stage", "queue_entry_id", "width", "height", "duration", "fps",
            "retry_expires_at", "phase2", "timings",
        }
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

    def job_fingerprints(self) -> list[tuple[str, str, Any, tuple[Any, ...]]]:
        """(id, state, retry_expires_at, every column but the immutable prompt /
        request_json / timings), newest first: publish_jobs rebuilds only changed jobs."""
        with self._lock:
            columns = [row[1] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
                       if row[1] not in ("prompt", "request_json", "timings")]
            rows = self._conn.execute(f"SELECT {', '.join(columns)} FROM jobs ORDER BY created_at DESC").fetchall()
        id_at, state_at, expiry_at = columns.index("id"), columns.index("state"), columns.index("retry_expires_at")
        return [(row[id_at], row[state_at], row[expiry_at], tuple(row)) for row in rows]

    def jobs_for_listing(self) -> list[dict[str, Any]]:
        """jobs() without request_json for finished jobs (public_job only reads it
        for an unfinished job's estimate). It is most of the table's bytes, and
        this runs on every live update: about 2/3 of the read time."""
        placeholders = ", ".join("?" for _ in UNFINISHED_JOB_STATES)
        with self._lock:
            columns = [row[1] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()]
            select = ", ".join(
                f"CASE WHEN state IN ({placeholders}) THEN request_json END AS request_json" if name == "request_json" else name
                for name in columns if name != "timings"  # only the info panel reads it
            )
            rows = self._conn.execute(f"SELECT {select} FROM jobs ORDER BY created_at DESC", UNFINISHED_JOB_STATES).fetchall()
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
        """Drops jobs a restart interrupts (GPU-queue waits, submissions, ffmpeg
        Post-Edit/Stitch passes). Prompts already handed to ComfyUI are kept: it
        keeps rendering through an app restart, and the monitor picks the result
        up from /history (or fails the job if ComfyUI has no record of it) -
        these used to vanish while ComfyUI still finished and saved them."""
        placeholders = ", ".join("?" for _ in UNFINISHED_JOB_STATES)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"""DELETE FROM jobs WHERE state IN ({placeholders})
                    AND NOT (state IN ('queued', 'rendering', 'finalizing') AND prompt_id IS NOT NULL
                             AND model NOT IN ('stitch', 'postedit'))""",
                UNFINISHED_JOB_STATES,
            )
        return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record_render_time(self, job_id: str, settings_key: str, settings_json: str, seconds: float, completed_at: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO render_times (job_id, settings_key, settings_json, seconds, completed_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, settings_key, settings_json, seconds, completed_at),
            )

    def recent_render_times(self, settings_key: str, limit: int = 5) -> list[float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seconds FROM render_times WHERE settings_key = ? ORDER BY completed_at DESC LIMIT ?",
                (settings_key, limit),
            ).fetchall()
        return [row[0] for row in rows]

    def render_times_all(self) -> list[tuple[str, float]]:
        """(settings_json, seconds) of every timed job, newest first."""
        with self._lock:
            return self._conn.execute(
                "SELECT settings_json, seconds FROM render_times ORDER BY completed_at DESC"
            ).fetchall()

    def timed_job_ids(self) -> set[str]:
        with self._lock:
            return {row[0] for row in self._conn.execute("SELECT job_id FROM render_times").fetchall()}

    def save_push_subscription(self, endpoint: str, subscription_json: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO push_subscriptions (endpoint, subscription_json, created_at) VALUES (?, ?, ?)",
                (endpoint, subscription_json, utc_now()),
            )

    def delete_push_subscription(self, endpoint: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

    def push_subscriptions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT subscription_json FROM push_subscriptions").fetchall()
        return [json.loads(row[0]) for row in rows]

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

    def trigger_aliases(self) -> dict[str, list[dict[str, str]]]:
        """{model: [{alias, lora, target}]}, sorted by alias."""
        with self._lock:
            rows = self._conn.execute("SELECT model, alias, lora, target FROM trigger_aliases ORDER BY alias COLLATE NOCASE").fetchall()
        result: dict[str, list[dict[str, str]]] = {}
        for model, alias, lora, target in rows:
            result.setdefault(model, []).append({"alias": alias, "lora": lora, "target": target})
        return result

    def save_trigger_alias_group(self, model: str, lora: str, target: str, aliases: list[str],
                                 previous_lora: str | None = None, previous_target: str | None = None) -> None:
        """Every alias of one (LoRA, target keyword) entry, replacing the entry's
        old aliases (and, when editing, the entry it was before)."""
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM trigger_aliases WHERE model = ? AND lora = ? AND target = ?", (model, lora, target))
            if previous_lora is not None and previous_target is not None:
                self._conn.execute("DELETE FROM trigger_aliases WHERE model = ? AND lora = ? AND target = ?",
                                   (model, previous_lora, previous_target))
            self._conn.executemany(
                "INSERT OR REPLACE INTO trigger_aliases (model, alias, lora, target, updated_at) VALUES (?, ?, ?, ?, ?)",
                [(model, alias, lora, target, now) for alias in aliases],
            )

    def delete_trigger_alias_group(self, model: str, lora: str, target: str) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM trigger_aliases WHERE model = ? AND lora = ? AND target = ?",
                                      (model, lora, target)).rowcount > 0

    def delete_preset(self, model: str, name: str) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM presets WHERE model = ? AND name = ?", (model, name)).rowcount > 0

    def get_state(self, key: str) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value_json FROM app_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_state(self, key: str, value: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO app_state (key, value_json) VALUES (?, ?)", (key, json.dumps(value)))

    def set_rating(self, job_id: str, rating: int, quality_key: str | None, settings_json: str | None,
                   reasons: list[str] | None = None) -> None:
        """rating 2 / 1 / -1 records it (under quality_key); 0 clears it.
        reasons: why an extreme thumbs up was best quality (JSON list)."""
        reasons_json = json.dumps(reasons) if reasons else None
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET rating = ?, rating_reasons = ? WHERE id = ?",
                               (rating or None, reasons_json, job_id))
            if not rating or quality_key is None:
                self._conn.execute("DELETE FROM ratings WHERE job_id = ?", (job_id,))
            else:
                self._conn.execute(
                    "INSERT OR REPLACE INTO ratings (job_id, quality_key, settings_json, rating, rated_at, reasons) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (job_id, quality_key, settings_json, rating, utc_now(), reasons_json),
                )

    def rating_counts(self, quality_key: str) -> tuple[int, int, int]:
        """(good, bad, best) over every rated video with these settings. An
        extreme thumbs up (2) counts as good and also as best."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(rating >= 1), 0), COALESCE(SUM(rating = -1), 0), COALESCE(SUM(rating = 2), 0) "
                "FROM ratings WHERE quality_key = ?",
                (quality_key,),
            ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def rated_settings(self) -> list[dict[str, Any]]:
        """Every rated settings combo with its counts, rating sum and the
        average render time of its rated jobs (None when none were timed)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT r.quality_key, MAX(r.settings_json) AS settings_json,
                       SUM(r.rating >= 1) AS good, SUM(r.rating = -1) AS bad, SUM(r.rating = 2) AS best,
                       SUM(r.rating) AS points, AVG(t.seconds) AS seconds, COUNT(t.seconds) AS timed
                FROM ratings r LEFT JOIN render_times t ON t.job_id = r.job_id
                GROUP BY r.quality_key
                """
            ).fetchall()
            reasons = self._conn.execute(
                "SELECT quality_key, reasons FROM ratings WHERE reasons IS NOT NULL"
            ).fetchall()
        by_key: dict[str, dict[str, int]] = {}
        for key, reasons_json in reasons:
            counts = by_key.setdefault(key, {})
            for reason in json.loads(reasons_json):
                counts[reason] = counts.get(reason, 0) + 1
        return [{**dict(row), "reasons": by_key.get(row["quality_key"], {})} for row in rows]
