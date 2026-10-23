from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# How long a finished job's "Retry" button stays available. The request JSON
# itself already lives forever in the jobs table, but the reference files it
# points at don't - they're one shared pool the user deletes/crops/re-trims
# freely - so each job snapshots its own copies here, and those snapshots are
# what this window bounds.
RETRY_WINDOW = timedelta(hours=42)

# The reference fields that define what a generation actually received. A
# live reference that still matches the snapshot on all of these is reused
# as-is on retry; anything else (deleted, cropped, re-trimmed, un/muted) is
# restored from the snapshot instead.
_IDENTITY_FIELDS = ("stored_name", "trim_start", "trim_end", "muted")
_FILE_FIELDS = ("stored_name", "original_stored_name", "thumbnail_stored_name")

# Pool widgets send EVERY reference in their role (see the frontend's
# collectSettings()), so an exact retry has to make that role's pool match
# the snapshot, not just add the snapshot's references back into it.
POOL_ROLES = {"references", "sh-images", "sh-videos", "sh-audios"}


def _root(upload_dir: Path) -> Path:
    # Under upload_dir, not data_dir: same mount as the files being
    # snapshotted, so os.link() works and a snapshot costs no extra space.
    return upload_dir / ".retry-cache"


def _job_dir(upload_dir: Path, job_id: str) -> Path:
    return _root(upload_dir) / job_id


def _link_or_copy(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def snapshot(upload_dir: Path, job_id: str, references: list[dict[str, Any]]) -> str:
    """Hard-link every reference file a job used into its own cache folder
    and return the ISO timestamp the snapshot expires at."""
    target = _job_dir(upload_dir, job_id)
    target.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in references:
        if ref["id"] in seen:
            continue
        seen.add(ref["id"])
        for field in _FILE_FIELDS:
            name = ref.get(field)
            if name and (upload_dir / name).is_file() and not (target / name).exists():
                _link_or_copy(upload_dir / name, target / name)
        rows.append(dict(ref))
    expires_at = (datetime.now(timezone.utc) + RETRY_WINDOW).isoformat()
    (target / "manifest.json").write_text(json.dumps({"expires_at": expires_at, "references": rows}))
    return expires_at


def is_live(expires_at: str | None) -> bool:
    return bool(expires_at) and datetime.fromisoformat(expires_at) > datetime.now(timezone.utc)


def load(upload_dir: Path, job_id: str) -> dict[str, Any] | None:
    manifest = _job_dir(upload_dir, job_id) / "manifest.json"
    if not manifest.is_file():
        return None
    data = json.loads(manifest.read_text())
    return data if is_live(data.get("expires_at")) else None


def remove(upload_dir: Path, job_id: str) -> None:
    shutil.rmtree(_job_dir(upload_dir, job_id), ignore_errors=True)


def sweep(upload_dir: Path, keep_job_ids: set[str]) -> int:
    """Delete every snapshot that's expired or whose job no longer exists."""
    root = _root(upload_dir)
    if not root.is_dir():
        return 0
    removed = 0
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        live = False
        if folder.name in keep_job_ids:
            try:
                live = is_live(json.loads((folder / "manifest.json").read_text()).get("expires_at"))
            except (OSError, ValueError):
                live = False
        if not live:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed


def matches(live: dict[str, Any] | None, saved: dict[str, Any]) -> bool:
    return live is not None and live.get("role") == saved.get("role") and all(
        (live.get(field) or 0) == (saved.get(field) or 0) for field in _IDENTITY_FIELDS
    )


def restore_reference(upload_dir: Path, job_id: str, saved: dict[str, Any], alias: str) -> dict[str, Any]:
    """A brand-new reference row rebuilt from a snapshot, with fresh copies
    of its files in upload_dir (the snapshot keeps its own, so the cache
    stays usable for another retry)."""
    source = _job_dir(upload_dir, job_id)
    item = {key: saved.get(key) for key in (
        "filename", "mime_type", "role", "duration", "label", "muted", "has_audio", "trim_start", "trim_end",
        "crop_x", "crop_y", "crop_w", "crop_h",
    )}
    for field in _FILE_FIELDS:
        name = saved.get(field)
        if name and (source / name).is_file():
            fresh = f"{uuid.uuid4().hex}{Path(name).suffix}"
            _link_or_copy(source / name, upload_dir / fresh)
            item[field] = fresh
        else:
            item[field] = None
    if not item["stored_name"]:
        raise FileNotFoundError(saved.get("filename") or saved["id"])
    item.update({
        "id": uuid.uuid4().hex, "alias": alias, "trim_start": item.get("trim_start") or 0.0,
        "muted": bool(item.get("muted")), "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return item


def file_path(upload_dir: Path, job_id: str, name: str) -> Path:
    return _job_dir(upload_dir, job_id) / Path(name).name


def has_file(upload_dir: Path, job_id: str, saved: dict[str, Any]) -> bool:
    return bool(saved.get("stored_name")) and (_job_dir(upload_dir, job_id) / saved["stored_name"]).is_file()
