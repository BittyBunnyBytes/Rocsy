"""Version history for Vibe Coding projects: snapshots of a project folder, so an agent turn can be undone.

Snapshots live in data/code-history/<project hash>/, outside every workspace the agent can write to:
objects/ holds each file's content once (named by its sha256), versions/<id>.json maps the project's
files to those objects, and index.json lists the versions plus whether history is on. Only the files
the library shows are kept (no dotfiles, .env, node_modules, build output, symlinks), and a restore only
touches those, so a project's own .git, .env or dependencies are never rewritten or deleted.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

import code_projects

HISTORY_DIR = Path(__file__).parent / "data" / "code-history"
MAX_VERSIONS = 60                    # oldest versions (and content only they used) are dropped past this
MAX_FILE_BYTES = 25 * 1024 ** 2      # bigger files are left out of versions (and left alone by restores)
MAX_SNAPSHOT_BYTES = 500 * 1024 ** 2
CHUNK = 1024 * 1024
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _key(info: dict[str, Any]) -> str:
    return f"{info['source']}:{info['path']}"


def _dir(info: dict[str, Any]) -> Path:
    return HISTORY_DIR / hashlib.sha256(_key(info).encode()).hexdigest()[:32]


def _lock(info: dict[str, Any]) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(_key(info), threading.Lock())


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(path)


def _index(info: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads((_dir(info) / "index.json").read_text())
        if isinstance(data, dict) and isinstance(data.get("versions"), list):
            return data
    except (OSError, ValueError):
        pass
    # New projects made on the Vibe Coding page keep versions by default; folders found in the other
    # workspaces can be big, so there it is opt-in.
    return {"key": _key(info), "enabled": info["source"] == "vibe", "versions": []}


def _manifest(info: dict[str, Any], version_id: str) -> dict[str, list]:
    try:
        return json.loads((_dir(info) / "versions" / f"{version_id}.json").read_text())["files"]
    except (OSError, ValueError, KeyError):
        raise HTTPException(404, "That version is gone (it was pruned or the history was reset)") from None


def _root(info: dict[str, Any]) -> Path:
    root = code_projects.ROOTS[info["source"]] / info["path"]
    if not root.is_dir() or root.is_symlink():
        raise HTTPException(404, "This project no longer exists (it was moved or deleted). Refresh the library.")
    return root


def _scan(root: Path) -> tuple[dict[str, tuple[Path, int, int]], list[str]]:
    """The project's visible files: relative path -> (path, size, mtime_ns), plus the ones too big to keep."""
    files: dict[str, tuple[Path, int, int]] = {}
    skipped: list[str] = []
    total = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        here = Path(base)
        dirs[:] = [d for d in dirs if code_projects._visible(d, True) and not (here / d).is_symlink()]
        for name in names:
            file = here / name
            if not code_projects._visible(name) or file.is_symlink() or not file.is_file():
                continue
            rel = file.relative_to(root).as_posix()
            st = file.stat()
            if st.st_size > MAX_FILE_BYTES:
                skipped.append(rel)
                continue
            total += st.st_size
            if len(files) >= code_projects.MAX_FILES or total > MAX_SNAPSHOT_BYTES:
                raise HTTPException(413, "This project is too big for version history (over 5,000 files or 500 MB). "
                                         "Turn history off for it.")
            files[rel] = (file, st.st_size, st.st_mtime_ns)
    return files, skipped


def _store(objects: Path, file: Path) -> str:
    """Copy one file into the object store, hashing it on the way; the same content is stored once."""
    objects.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    fd, tmp = tempfile.mkstemp(dir=objects, prefix=".in-")
    try:
        with os.fdopen(fd, "wb") as out, file.open("rb") as src:
            while chunk := src.read(CHUNK):
                digest.update(chunk)
                out.write(chunk)
        sha = digest.hexdigest()
        target = objects / sha[:2] / sha
        if target.exists():
            os.unlink(tmp)
        else:
            target.parent.mkdir(exist_ok=True)
            os.replace(tmp, target)
        return sha
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _summary(version: dict[str, Any]) -> dict[str, Any]:
    return {k: version.get(k) for k in ("id", "t", "label", "kind", "files", "bytes", "added", "changed", "removed",
                                        "changed_files", "skipped")}


def _snapshot(info: dict[str, Any], label: str, kind: str, prune: bool = True) -> tuple[dict[str, Any] | None, dict[str, list]]:
    """Save the folder as a new version, unless it matches the newest one. Caller holds the lock.
    prune=False keeps every version (and its objects) for now; the caller prunes later."""
    root = _root(info)
    home = _dir(info)
    index = _index(info)
    last = index["versions"][-1] if index["versions"] else None
    before = _manifest(info, last["id"]) if last else {}
    files, skipped = _scan(root)
    manifest: dict[str, list] = {}
    for rel, (file, size, mtime) in files.items():
        old = before.get(rel)
        # Unchanged size and mtime: same content as the last version, no need to read it again.
        if old and old[1] == size and old[2] == mtime and (home / "objects" / old[0][:2] / old[0]).exists():
            manifest[rel] = old
            continue
        try:
            manifest[rel] = [_store(home / "objects", file), size, mtime]
        except FileNotFoundError:
            continue                     # deleted while we were reading the folder
    same = lambda a, b: a is not None and b is not None and a[0] == b[0]
    if last and len(manifest) == len(before) and all(same(manifest[r], before.get(r)) for r in manifest):
        return None, manifest
    added = sorted(r for r in manifest if r not in before)
    removed = sorted(r for r in before if r not in manifest)
    changed = sorted(r for r in manifest if r in before and not same(manifest[r], before[r]))
    version = {"id": f"{int(time.time() * 1000):x}", "t": time.time(), "label": label[:160], "kind": kind,
               "files": len(manifest), "bytes": sum(x[1] for x in manifest.values()),
               "added": len(added), "changed": len(changed), "removed": len(removed),
               "changed_files": ([f"+ {r}" for r in added] + [f"~ {r}" for r in changed] + [f"− {r}" for r in removed])[:40],
               "skipped": skipped[:20]}
    if last and version["id"] <= last["id"]:
        version["id"] = f"{int(last['id'], 16) + 1:x}"
    _write_json(home / "versions" / f"{version['id']}.json", {"files": manifest})
    index["versions"].append(version)
    if prune:
        _prune(home, index)
    _write_json(home / "index.json", index)
    return version, manifest


def _prune(home: Path, index: dict[str, Any]) -> None:
    drop, index["versions"] = index["versions"][:-MAX_VERSIONS], index["versions"][-MAX_VERSIONS:]
    if not drop:
        return
    for v in drop:
        (home / "versions" / f"{v['id']}.json").unlink(missing_ok=True)
    keep: set[str] = set()
    for v in index["versions"]:
        try:
            keep.update(x[0] for x in json.loads((home / "versions" / f"{v['id']}.json").read_text())["files"].values())
        except (OSError, ValueError, KeyError):
            continue
    for obj in (home / "objects").glob("*/*"):
        if obj.name not in keep:
            obj.unlink(missing_ok=True)


# ---------------- public ----------------
def state(info: dict[str, Any]) -> dict[str, Any]:
    index = _index(info)
    return {"enabled": bool(index.get("enabled")), "versions": [_summary(v) for v in reversed(index["versions"])],
            "max_versions": MAX_VERSIONS}


def set_enabled(info: dict[str, Any], enabled: bool) -> dict[str, Any]:
    with _lock(info):
        index = _index(info)
        index["enabled"] = enabled
        _write_json(_dir(info) / "index.json", index)
        if enabled:
            _snapshot(info, "History turned on", "manual")
    return state(info)


def save(info: dict[str, Any], label: str, kind: str = "manual") -> dict[str, Any]:
    with _lock(info):
        version, _ = _snapshot(info, label or "Saved by you", kind)
    return {**state(info), "saved": version is not None}


def auto_snapshot(info: dict[str, Any], label: str, kind: str) -> None:
    """Snapshot around agent turns when history is on. Never blocks the agent: failures are only logged."""
    if not _index(info).get("enabled"):
        return
    try:
        with _lock(info):
            _snapshot(info, label, kind)
    except Exception as exc:              # a full disk or an odd file must not stop the user's message
        print(f"code_history: snapshot failed for {_key(info)}: {exc}", flush=True)


def restore(info: dict[str, Any], version_id: str) -> dict[str, Any]:
    """Put the project's files back as they were in a version. The current files are saved as a version
    first, so the restore itself can be undone."""
    with _lock(info):
        index = _index(info)
        target = next((v for v in index["versions"] if v["id"] == version_id), None)
        if not target:
            raise HTTPException(404, "That version is gone (it was pruned or the history was reset)")
        wanted = _manifest(info, version_id)
        when = time.strftime("%b %d, %H:%M", time.localtime(target["t"]))
        # Don't prune yet: at the version limit that would delete the very version being restored.
        _, current = _snapshot(info, f"Before restoring “{target['label']}” ({when})", "safety", prune=False)
        try:
            _restore_files(info, wanted, current)
            _snapshot(info, f"Restored “{target['label']}” ({when})", "restored")
        finally:
            index = _index(info)
            if len(index["versions"]) > MAX_VERSIONS:
                _prune(_dir(info), index)
                _write_json(_dir(info) / "index.json", index)
    return {**state(info), "restored": version_id}


def _restore_files(info: dict[str, Any], wanted: dict[str, list], current: dict[str, list]) -> None:
    """Make the project folder match `wanted` (a version manifest). Caller holds the lock."""
    root = _root(info).resolve()
    objects = _dir(info) / "objects"
    missing = [rel for rel, x in wanted.items() if not (objects / x[0][:2] / x[0]).exists()]
    if missing:
        raise HTTPException(409, f"This version can't be restored: {len(missing)} of its files are missing from the history store")
    for rel in [*current, *wanted]:         # check every path first, so a refusal changes nothing
        _safe_path(root, rel)
    for rel in current:
        if rel not in wanted:
            _safe_path(root, rel).unlink(missing_ok=True)
    for rel, (sha, _size, mtime) in wanted.items():
        if current.get(rel, [None])[0] == sha:
            continue
        path = _safe_path(root, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        _safe_path(root, rel)               # recheck: mkdir must not have followed a planted symlink
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".restore-")
        os.close(fd)
        try:
            shutil.copyfile(objects / sha[:2] / sha, tmp)
            os.replace(tmp, path)
            os.utime(path, ns=(mtime, mtime))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    _drop_empty_dirs(root, wanted)


def _safe_path(root: Path, rel: str) -> Path:
    path = root.joinpath(*Path(rel).parts)
    current = root
    for part in Path(rel).parts[:-1]:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise HTTPException(409, f"Can't restore {rel}: a folder on its path was replaced by a link or a file")
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise HTTPException(409, f"Can't restore {rel}: it is a link now")
    return path


def _drop_empty_dirs(root: Path, wanted: dict[str, list]) -> None:
    keep = {p.as_posix() for rel in wanted for p in Path(rel).parents}
    found: list[Path] = []
    for base, dirs, _ in os.walk(root, followlinks=False):
        here = Path(base)
        dirs[:] = [d for d in dirs if code_projects._visible(d, True) and not (here / d).is_symlink()]
        found.extend(here / d for d in dirs if (here / d).relative_to(root).as_posix() not in keep)
    for here in sorted(found, key=lambda p: -len(p.parts)):     # deepest first, so nested empty folders go too
        try:
            here.rmdir()                       # only succeeds when it is empty
        except OSError:
            pass


def forget(info: dict[str, Any]) -> None:
    with _lock(info):
        shutil.rmtree(_dir(info), ignore_errors=True)


def activity(days: int = 182) -> dict[int, int]:
    """Coding activity for the home page's Vibe Coding heatmap: hour (epoch seconds) -> count, across every
    project. Each visible file counts once at its last change, and each saved version counts once."""
    since = time.time() - days * 86400
    hours: dict[int, int] = {}

    def add(t: float) -> None:
        if t >= since:
            hour = int(t // 3600 * 3600)
            hours[hour] = hours.get(hour, 0) + 1

    for info in code_projects.list_projects():
        try:
            files, _ = _scan(_root(info))
        except HTTPException:
            files = {}                   # gone, or too big to scan: its versions still count
        for _, _, mtime_ns in files.values():
            add(mtime_ns / 1e9)
        for version in _index(info)["versions"]:
            add(float(version.get("t") or 0))
    return hours
