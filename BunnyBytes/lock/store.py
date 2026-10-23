from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class QueueEntry:
    entry_id: str
    app: str
    job_id: str
    token: str | None
    label: str
    state: str  # "waiting" | "active"
    is_continuous: bool
    shutdown_at: str | None
    seq: int
    sort_order: int
    is_locked: bool
    enqueued_at: str
    became_active_at: str | None
    expires_at: str | None

    def public(self, position: int | None = None) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id, "app": self.app, "job_id": self.job_id,
            "label": self.label, "state": self.state, "is_continuous": self.is_continuous,
            "shutdown_at": self.shutdown_at, "enqueued_at": self.enqueued_at,
            "is_locked": self.is_locked,
            "became_active_at": self.became_active_at, "expires_at": self.expires_at,
            "position": position,
        }


def _row_to_entry(row: sqlite3.Row) -> QueueEntry:
    return QueueEntry(
        entry_id=row["entry_id"], app=row["app"], job_id=row["job_id"], token=row["token"],
        label=row["label"], state=row["state"], is_continuous=bool(row["is_continuous"]),
        shutdown_at=row["shutdown_at"], seq=row["seq"], sort_order=row["sort_order"],
        is_locked=bool(row["is_locked"]), enqueued_at=row["enqueued_at"],
        became_active_at=row["became_active_at"], expires_at=row["expires_at"],
    )


class QueueStore:
    """Persistent (sqlite-backed) cross-app GPU queue.

    Unlike the single-slot in-memory mutex this replaces, the waiting list here
    must survive a coordinator restart without silently dropping other apps'
    pending jobs -- hence sqlite instead of a plain dict. The *active* slot
    still self-heals via TTL exactly as before (a missed heartbeat expires it);
    only the waiting list gains persistence.
    """

    def __init__(self, path: Path):
        self.path = path
        self._guard = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL + NORMAL: a commit no longer fsyncs the whole database, so a burst
        # of enqueues/heartbeats from several apps doesn't stall the event loop.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS queue (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id TEXT NOT NULL UNIQUE,
                    app TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    token TEXT,
                    label TEXT NOT NULL,
                    state TEXT NOT NULL,
                    is_continuous INTEGER NOT NULL DEFAULT 0,
                    shutdown_at TEXT,
                    enqueued_at TEXT NOT NULL,
                    became_active_at TEXT,
                    expires_at TEXT
                );
                """
            )
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(queue)")}
            if "sort_order" not in columns:
                self._conn.execute("ALTER TABLE queue ADD COLUMN sort_order INTEGER")
            if "is_locked" not in columns:
                self._conn.execute("ALTER TABLE queue ADD COLUMN is_locked INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("UPDATE queue SET sort_order = seq WHERE sort_order IS NULL")
        # Restart recovery: a row left "active" from a prior process is only
        # reaped if its TTL has actually lapsed (expires_at is a plain
        # wall-clock timestamp, unaffected by how long the coordinator itself
        # was down) -- a brief coordinator restart while the owning app's job
        # is still genuinely rendering must NOT wipe its active entry out from
        # under it, or that job's eventual release() has nothing to promote
        # against and a job actually gets orphaned from the visible queue
        # while still safely serializing at the ComfyUI level. Only a holder
        # that's been silent for a full TTL (~90s - well past this app's own
        # ~20s heartbeat cadence) is genuinely abandoned. Waiting rows are
        # never touched here.
        with self._guard, self._conn:
            self._reap_expired_active_locked(utc_now())

    def _reap_expired_active_locked(self, now: datetime) -> None:
        row = self._conn.execute("SELECT * FROM queue WHERE state = 'active' LIMIT 1").fetchone()
        if row is None:
            return
        expires_at = row["expires_at"]
        if expires_at and parse_iso(expires_at) > now:
            return
        self._conn.execute("DELETE FROM queue WHERE entry_id = ?", (row["entry_id"],))

    def _promote_next_locked(self, now: datetime, ttl_seconds: float) -> QueueEntry | None:
        existing = self._conn.execute("SELECT 1 FROM queue WHERE state = 'active' LIMIT 1").fetchone()
        if existing is not None:
            return None
        row = self._conn.execute(
            "SELECT * FROM queue WHERE state = 'waiting' ORDER BY sort_order ASC, seq ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        token = uuid.uuid4().hex
        became_active_at = iso(now)
        expires_at = iso(now + timedelta(seconds=ttl_seconds))
        self._conn.execute(
            "UPDATE queue SET state = 'active', token = ?, became_active_at = ?, expires_at = ? WHERE entry_id = ?",
            (token, became_active_at, expires_at, row["entry_id"]),
        )
        row = self._conn.execute("SELECT * FROM queue WHERE entry_id = ?", (row["entry_id"],)).fetchone()
        return _row_to_entry(row)

    def enqueue(self, app: str, job_id: str, label: str, is_continuous: bool, ttl_seconds: float,
                priority: bool = False) -> tuple[QueueEntry, bool, int | None]:
        """Insert a new entry. Returns (entry, promoted, position) -- promoted is
        True if it became active immediately (queue was empty), position is its
        waiting slot otherwise. Insert, promotion and priority move all happen
        under one lock, so simultaneous enqueues into an empty queue can never
        both win (or both lose) the free slot."""
        now = utc_now()
        entry_id = uuid.uuid4().hex
        with self._guard, self._conn:
            self._reap_expired_active_locked(now)
            next_order = self._conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM queue").fetchone()[0]
            self._conn.execute(
                """INSERT INTO queue (entry_id, app, job_id, token, label, state, is_continuous, shutdown_at, enqueued_at, sort_order)
                   VALUES (?, ?, ?, NULL, ?, 'waiting', ?, NULL, ?, ?)""",
                (entry_id, app, job_id, label, 1 if is_continuous else 0, iso(now), next_order),
            )
            promoted = self._promote_next_locked(now, ttl_seconds)
            if priority and not promoted:
                self._prioritize_locked(entry_id)
            row = self._conn.execute("SELECT * FROM queue WHERE entry_id = ?", (entry_id,)).fetchone()
            position = None
            if row["state"] == "waiting":
                ids = [r["entry_id"] for r in self._conn.execute(
                    "SELECT entry_id FROM queue WHERE state = 'waiting' ORDER BY sort_order, seq")]
                position = ids.index(entry_id) + 1
        entry = _row_to_entry(row)
        return entry, bool(promoted and promoted.entry_id == entry_id), position

    def heartbeat(self, token: str, ttl_seconds: float) -> QueueEntry | None:
        now = utc_now()
        with self._guard, self._conn:
            self._reap_expired_active_locked(now)
            row = self._conn.execute("SELECT * FROM queue WHERE state = 'active' AND token = ?", (token,)).fetchone()
            if row is None:
                return None
            expires_at = iso(now + timedelta(seconds=ttl_seconds))
            self._conn.execute("UPDATE queue SET expires_at = ? WHERE entry_id = ?", (expires_at, row["entry_id"]))
            row = self._conn.execute("SELECT * FROM queue WHERE entry_id = ?", (row["entry_id"],)).fetchone()
        return _row_to_entry(row)

    def release(self, token: str, ttl_seconds: float) -> tuple[bool, QueueEntry | None]:
        """Release the active entry matching `token`. Idempotent: a token that
        no longer matches the (possibly already-expired-and-reaped) active
        entry is treated as already released, not an error. Returns
        (released, promoted_entry) -- promoted_entry is the next entry that
        became active as a result, if any, so the caller can fire its proceed
        callback."""
        now = utc_now()
        with self._guard, self._conn:
            self._reap_expired_active_locked(now)
            row = self._conn.execute("SELECT * FROM queue WHERE state = 'active'").fetchone()
            if row is None:
                promoted = self._promote_next_locked(now, ttl_seconds)
                return True, promoted
            if row["token"] != token:
                return False, None
            self._conn.execute("DELETE FROM queue WHERE entry_id = ?", (row["entry_id"],))
            promoted = self._promote_next_locked(now, ttl_seconds)
        return True, promoted

    def dequeue(self, entry_id: str) -> tuple[bool, str | None]:
        """Remove a still-waiting entry with no promotion side effect. Returns
        (dequeued, reason). reason is None on success, "not_found" or "active"
        otherwise -- an entry already active must not be touched here, its
        owning app's normal cancel/release path handles that."""
        with self._guard, self._conn:
            row = self._conn.execute("SELECT * FROM queue WHERE entry_id = ?", (entry_id,)).fetchone()
            if row is None:
                return True, "not_found"
            if row["state"] == "active":
                return False, "active"
            self._conn.execute("DELETE FROM queue WHERE entry_id = ?", (entry_id,))
        return True, None

    def set_shutdown_at(self, entry_id: str, shutdown_at: str | None) -> bool:
        with self._guard, self._conn:
            cursor = self._conn.execute("UPDATE queue SET shutdown_at = ? WHERE entry_id = ?", (shutdown_at, entry_id))
        return cursor.rowcount > 0

    def set_locked(self, entry_id: str, is_locked: bool) -> bool:
        with self._guard, self._conn:
            cursor = self._conn.execute(
                "UPDATE queue SET is_locked = ? WHERE entry_id = ?",
                (1 if is_locked else 0, entry_id),
            )
        return cursor.rowcount > 0

    def _reorder_waiting_locked(self, entry_ids: list[str]) -> str | None:
        rows = self._conn.execute(
            "SELECT entry_id, is_locked FROM queue WHERE state = 'waiting' ORDER BY sort_order, seq"
        ).fetchall()
        current = [row["entry_id"] for row in rows]
        if len(entry_ids) != len(current) or set(entry_ids) != set(current):
            return "queue_changed"
        for index, row in enumerate(rows):
            if row["is_locked"] and entry_ids[index] != row["entry_id"]:
                return "locked_position"
        # Use compact ranks so future enqueues always append after the
        # reordered waiting list, even after older jobs were removed.
        for index, entry_id in enumerate(entry_ids, start=1):
            self._conn.execute(
                "UPDATE queue SET sort_order = ? WHERE entry_id = ? AND state = 'waiting'",
                (index, entry_id),
            )
        return None

    def reorder_waiting(self, entry_ids: list[str]) -> str | None:
        """Change promotion order. Locked entries retain their exact waiting slots."""
        with self._guard, self._conn:
            return self._reorder_waiting_locked(entry_ids)

    def _prioritize_locked(self, entry_id: str) -> bool:
        rows = self._conn.execute(
            "SELECT entry_id, is_locked FROM queue WHERE state = 'waiting' ORDER BY sort_order, seq"
        ).fetchall()
        if entry_id not in {row["entry_id"] for row in rows}:
            return False
        movable = [row["entry_id"] for row in rows if not row["is_locked"]]
        if entry_id in movable:
            movable.remove(entry_id)
            movable.insert(0, entry_id)
        order = [row["entry_id"] if row["is_locked"] else movable.pop(0) for row in rows]
        return self._reorder_waiting_locked(order) is None

    def prioritize(self, entry_id: str) -> bool:
        """Move a waiting entry to the front of the line (the Master Agent's jobs).
        Entries the user locked keep their exact slots. False if it isn't waiting.
        Read and rewrite happen under one lock: a concurrent enqueue used to make
        the rewrite fail with "queue_changed" and the priority silently drop."""
        with self._guard, self._conn:
            return self._prioritize_locked(entry_id)

    def reap_and_promote(self, ttl_seconds: float) -> QueueEntry | None:
        """Periodic self-heal: if the active holder's TTL lapsed (crashed
        without releasing) and nothing has touched the store since, promote
        the next waiting entry so it isn't stuck behind a dead holder
        indefinitely. Safe to call on a timer; a no-op when nothing expired."""
        now = utc_now()
        with self._guard, self._conn:
            self._reap_expired_active_locked(now)
            promoted = self._promote_next_locked(now, ttl_seconds)
        return promoted

    def get(self, entry_id: str) -> QueueEntry | None:
        with self._guard:
            row = self._conn.execute("SELECT * FROM queue WHERE entry_id = ?", (entry_id,)).fetchone()
        return _row_to_entry(row) if row else None

    def list_all(self) -> list[QueueEntry]:
        now = utc_now()
        with self._guard, self._conn:
            self._reap_expired_active_locked(now)
            rows = self._conn.execute(
                "SELECT * FROM queue ORDER BY (state = 'active') DESC, sort_order ASC, seq ASC"
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def public_list(self) -> list[dict[str, Any]]:
        entries = self.list_all()
        waiting_position = 1
        result = []
        for entry in entries:
            if entry.state == "active":
                result.append(entry.public(position=None))
            else:
                result.append(entry.public(position=waiting_position))
                waiting_position += 1
        return result

    def close(self) -> None:
        with self._guard:
            self._conn.close()
