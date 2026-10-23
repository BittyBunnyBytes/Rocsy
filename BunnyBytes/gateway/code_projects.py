"""Project index, previews, downloads, and confirmed deletion."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from fastapi import HTTPException

# New projects made on the Vibe Coding page live in the agent's workspace, in their own folder.
VIBE_DIR = "vibe-projects"
ROOTS = {
    "vibe": Path(os.getenv("ROCSY_AGENT_WORKSPACE_DIR", "/storage/agent-workspace")) / VIBE_DIR,
    "hermes": Path(os.getenv("ROCSY_HERMES_PROJECTS_DIR", "/storage/hermes-workspace")),
    "rocsy": Path(os.getenv("ROCSY_AGENT_WORKSPACE_DIR", "/storage/agent-workspace")),
}
SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", ".venv", "venv", "__pycache__", ".cache", "coverage"}
MARKERS = {"index.html", "package.json", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod", "AGENT.md", "README.md"}
CODE_EXT = {".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".py", ".rs", ".go", ".vue", ".svelte"}
TEXT_EXT = CODE_EXT | {".json", ".md", ".txt", ".yaml", ".yml", ".toml", ".sh", ".svg", ".xml", ".csv"}
MEDIA_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".mp4", ".webm", ".mp3", ".wav", ".ogg", ".pdf"}
PREVIEW_EXT = {".html", ".css", ".js", ".mjs", ".json", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".webm", ".mp3", ".wav", ".ogg"}
MAX_FILES = 5000
MAX_ZIP_BYTES = 2 * 1024 ** 3
MAX_TEXT_BYTES = 1024 * 1024


def _visible(name: str, directory: bool = False) -> bool:
    return bool(name and name not in {".", ".."} and not name.startswith(".") and not name.lower().startswith(".env")
                and name not in (SKIP_DIRS if directory else set()))


def _code_inside(folder: Path, max_depth: int = 3) -> bool:
    for base, dirs, files in os.walk(folder, followlinks=False):
        here = Path(base)
        depth = len(here.relative_to(folder).parts)
        dirs[:] = [d for d in dirs if depth < max_depth and _visible(d, True) and not (here / d).is_symlink()]
        if any(f in MARKERS or Path(f).suffix.lower() in CODE_EXT for f in files if _visible(f)):
            return True
    return False


def project_id_for(source: str, relative: str) -> str:
    return _project_id(source, relative)


def _project_id(source: str, relative: str) -> str:
    return base64.urlsafe_b64encode(f"{source}:{relative}".encode()).decode().rstrip("=")


def _project(source: str, folder: Path) -> dict:
    root = ROOTS[source]
    relative = folder.relative_to(root).as_posix()
    return {
        "id": _project_id(source, relative), "name": folder.name.replace("_", " ").replace("-", " "),
        "source": source, "path": relative, "preview": (folder / "index.html").is_file() and not (folder / "index.html").is_symlink(),
        "updated": int(folder.stat().st_mtime),
    }


LIST_TTL = 2.0           # a preview page loads many files, each resolving its project: scan once per burst
_list_cache: tuple[float, list[dict]] | None = None
_list_lock = threading.Lock()
NOT_FOUND = "This project no longer exists (it was moved or deleted). Refresh the library."


def forget_list() -> None:
    global _list_cache
    _list_cache = None


def list_projects() -> list[dict]:
    global _list_cache
    with _list_lock:
        if _list_cache is None or time.monotonic() - _list_cache[0] > LIST_TTL:
            _list_cache = (time.monotonic(), _scan_projects())
        return [dict(item) for item in _list_cache[1]]


def _scan_projects() -> list[dict]:
    projects = []
    for source, root in ROOTS.items():
        if not root.is_dir():
            continue
        for first in root.iterdir():
            if not first.is_dir() or first.is_symlink() or not _visible(first.name, True):
                continue
            # The top-level src folder is part of the shared Remotion install,
            # rather than a standalone coding project.
            if source == "rocsy" and first.name in ("src", VIBE_DIR):
                continue
            if source == "vibe":
                projects.append(_project(source, first))     # made here: listed even before it has code
                continue
            if source == "rocsy" and re.fullmatch(r"[0-9a-f]{32}", first.name):
                # Rocsy stores each chat under a session id. Look for named projects inside it.
                for base, dirs, files in os.walk(first, followlinks=False):
                    here = Path(base)
                    depth = len(here.relative_to(first).parts)
                    dirs[:] = [d for d in dirs if depth < 4 and d != "motion" and _visible(d, True)
                               and not (here / d).is_symlink()]
                    if here == first:
                        continue
                    if any(f in MARKERS for f in files) and _code_inside(here):
                        projects.append(_project(source, here))
                        dirs[:] = []
                continue
            if _code_inside(first):
                projects.append(_project(source, first))
    return sorted(projects, key=lambda item: (item["source"], item["name"].lower()))


def project_root(project_id: str) -> Path:
    # Resolve only ids in the current index. An encoded path alone grants nothing.
    for item in list_projects():
        if item["id"] == project_id:
            return ROOTS[item["source"]] / item["path"]
    raise HTTPException(404, NOT_FOUND)


def project_info(project_id: str) -> dict:
    for item in list_projects():
        if item["id"] == project_id:
            return item
    raise HTTPException(404, NOT_FOUND)


def delete_project(project_id: str, confirmation: str) -> dict:
    info = project_info(project_id)
    if confirmation != info["name"]:
        raise HTTPException(400, "Type the project name exactly to delete it")
    root = ROOTS[info["source"]].resolve()
    target = root / info["path"]
    # Recheck the target and every ancestor immediately before removal. The
    # index alone is not a safe authorization boundary for a destructive action.
    if (not target.is_dir() or target.is_symlink() or target.resolve() == root
            or not target.resolve().is_relative_to(root)):
        raise HTTPException(404, NOT_FOUND)
    current = target.parent
    while current != root:
        if current.is_symlink() or not current.resolve().is_relative_to(root):
            raise HTTPException(404, NOT_FOUND)
        current = current.parent
    if not shutil.rmtree.avoids_symlink_attacks:
        raise HTTPException(503, "Safe project deletion is unavailable")
    try:
        shutil.rmtree(target)
        forget_list()
    except OSError as exc:
        raise HTTPException(409, f"Could not delete project: {exc.strerror or 'filesystem error'}") from exc
    return {"deleted": project_id, "name": info["name"]}


def make_preview_token(project_id: str, secret: str) -> str:
    info = project_info(project_id)
    if not info["preview"]:
        raise HTTPException(404, "Preview unavailable")
    expires = int(time.time()) + 3600
    body = f"{project_id}:{expires}"
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}:{signature}".encode()).decode().rstrip("=")


def verify_preview_token(token: str, secret: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        project_id, expires_text, signature = raw.split(":")
        expires = int(expires_text)
        expected = hmac.new(secret.encode(), f"{project_id}:{expires}".encode(), hashlib.sha256).hexdigest()
        if expires < time.time() or expires > time.time() + 3600 or not hmac.compare_digest(signature, expected):
            raise ValueError("invalid token")
        if not project_info(project_id)["preview"]:
            raise ValueError("preview unavailable")
        return project_id
    except (ValueError, UnicodeError, IndexError, binascii.Error):
        raise HTTPException(404, "Preview unavailable") from None


def project_path(project_id: str, relative: str = "") -> Path:
    root = project_root(project_id)
    parts = Path(relative).parts
    if Path(relative).is_absolute() or any(not _visible(part, True) for part in parts):
        raise HTTPException(404, "File not found")
    path = root.joinpath(*parts)
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise HTTPException(404, "File not found")
    # Reject symlinks in intermediate path segments too.
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise HTTPException(404, "File not found")
    if not path.exists():
        raise HTTPException(404, "File not found")
    return path


def list_files(project_id: str, relative: str = "") -> dict:
    folder = project_path(project_id, relative)
    if not folder.is_dir():
        raise HTTPException(400, "Path is not a folder")
    entries = []
    for child in folder.iterdir():
        if child.is_symlink() or not _visible(child.name, child.is_dir()):
            continue
        stat = child.stat()
        entries.append({"name": child.name, "path": (Path(relative) / child.name).as_posix(),
                        "directory": child.is_dir(), "size": stat.st_size if child.is_file() else None})
    entries.sort(key=lambda x: (not x["directory"], x["name"].lower()))
    return {"path": relative, "entries": entries}


def make_zip(project_id: str) -> tuple[str, str]:
    root = project_root(project_id)
    info = project_info(project_id)
    temp = tempfile.NamedTemporaryFile(prefix="rocsy-code-", suffix=".zip", delete=False)
    path = temp.name
    temp.close()
    count = total = 0
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4) as archive:
            for base, dirs, files in os.walk(root, followlinks=False):
                here = Path(base)
                dirs[:] = [d for d in dirs if _visible(d, True) and not (here / d).is_symlink()]
                for name in files:
                    file = here / name
                    if not _visible(name) or file.is_symlink() or not file.is_file():
                        continue
                    count += 1
                    total += file.stat().st_size
                    if count > MAX_FILES or total > MAX_ZIP_BYTES:
                        raise HTTPException(413, "Project exceeds the download limit")
                    archive.write(file, Path(root.name) / file.relative_to(root))
        return path, re.sub(r"[^A-Za-z0-9._-]+", "-", root.name) + ".zip"
    except Exception:
        os.unlink(path)
        raise
