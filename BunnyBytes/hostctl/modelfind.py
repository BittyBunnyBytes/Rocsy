"""Studios -> "missing model files" dialog: find files on this machine that ComfyUI can't see.

POST /models/find  {"names": ["flux2-vae.safetensors", ...]}
     -> {"found": {name: [{path, size}, ...]}, "seconds", "truncated"}
     Looks for the exact file name (any case) under the home folder and the usual mount
     points (/mnt, /media, /run/media), skipping caches, git/venv/node folders and the Trash
     but keeping ~/.cache/huggingface (hub snapshots are real model files).
POST /models/link  {"path": <a path the last find returned>, "folder": "vae", "name": "flux2-vae.safetensors"}
     -> {"dest", "method"}: puts the file into ComfyUI's models/<folder>/<name> without
     copying data: a hard link, else a btrfs/XFS reflink copy, else a symlink. The source
     stays where it is. Only paths the last find returned (within 15 minutes) are accepted,
     and an existing destination is never replaced.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from paths import COMFY_ROOT

MODELS = Path(os.getenv("CORE_COMFY_DIR", COMFY_ROOT)) / "models"
ROOTS = [Path.home(), Path("/mnt"), Path("/media"), Path("/run/media")]
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "site-packages", "dist-packages", ".venv", "venv",
             ".snapshots", "Trash", ".Trash", "lost+found", "proc"}
# Hidden folders are skipped except these (relative to their parent): HF hub downloads live in .cache.
KEEP_HIDDEN = {".cache", "huggingface", "hub"}
NAME = re.compile(r"^[^/\\\0]{1,255}\.(safetensors|sft|gguf|ckpt|pt|pth|bin|pkl|onnx)$", re.I)
FOLDER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
BUDGET = 45.0
TTL = 15 * 60

_lock = threading.Lock()
_found: dict[str, float] = {}  # real path -> when a find returned it


def _clean_names(names) -> list[str]:
    out = []
    for raw in names if isinstance(names, list) else []:
        base = str(raw).replace("\\", "/").rsplit("/", 1)[-1]
        if NAME.match(base) and base.lower() not in {n.lower() for n in out}:
            out.append(base)
    return out[:64]


def find(names) -> dict:
    wanted = {name.lower(): name for name in _clean_names(names)}
    found: dict[str, list[dict]] = {name: [] for name in wanted.values()}
    if not wanted:
        return {"found": found, "seconds": 0, "truncated": False}
    start, truncated, seen = time.monotonic(), False, set()
    for root in ROOTS:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _: None):
            if time.monotonic() - start > BUDGET:
                truncated = True
                break
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and (not d.startswith(".") or d in KEEP_HIDDEN)]
            for filename in filenames:
                name = wanted.get(filename.lower())
                if not name:
                    continue
                path = os.path.join(dirpath, filename)
                try:
                    real = os.path.realpath(path)
                    size = os.stat(real).st_size
                except OSError:
                    continue  # broken symlink
                if size <= 0 or real in seen or not os.path.isfile(real):
                    continue
                seen.add(real)
                found[name].append({"path": path, "size": size})
        if truncated:
            break
    now = time.time()
    with _lock:
        for items in found.values():
            for item in items:
                _found[os.path.realpath(item["path"])] = now
        for key in [k for k, at in _found.items() if now - at > TTL]:
            del _found[key]
    return {"found": found, "seconds": round(time.monotonic() - start, 1), "truncated": truncated}


def link(body: dict) -> tuple[int, dict]:
    path = str(body.get("path") or "")
    folder = str(body.get("folder") or "")
    name = str(body.get("name") or "").replace("\\", "/")
    parts = name.split("/")
    if not FOLDER.match(folder) or not NAME.match(parts[-1]) or any(p in ("", ".", "..") for p in parts) or len(parts) > 3:
        return 400, {"error": "Invalid folder or file name."}
    real = os.path.realpath(path)
    with _lock:
        known = time.time() - _found.get(real, 0) < TTL
    if not known or not os.path.isfile(real):
        return 409, {"error": "Search again: that file wasn't found by the last search."}
    if Path(real).name.lower() != parts[-1].lower() and Path(path).name.lower() != parts[-1].lower():
        return 400, {"error": "That file has a different name."}
    dest = MODELS / folder / Path(*parts)
    if not str(dest.resolve().parent).startswith(str(MODELS.resolve())):
        return 400, {"error": "Invalid destination."}
    if dest.exists() or dest.is_symlink():
        return 409, {"error": f"models/{folder}/{name} already exists."}
    dest.parent.mkdir(parents=True, exist_ok=True)
    method = ""
    try:
        os.link(real, dest)
        method = "hardlink"
    except OSError:
        tmp = dest.with_name(f".{dest.name}.reflink")
        result = subprocess.run(["cp", "--reflink=always", "--preserve=timestamps", real, str(tmp)],
                                capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            os.replace(tmp, dest)
            method = "reflink"
        else:
            tmp.unlink(missing_ok=True)
            os.symlink(real, dest)
            method = "symlink"
    return 200, {"dest": f"{folder}/{name}", "method": method, "source": path}


def handle(method: str, path: str, body: dict) -> tuple[int, dict]:
    if method == "POST" and path == "/models/find":
        return 200, find(body.get("names"))
    if method == "POST" and path == "/models/link":
        return link(body)
    return 404, {"error": "not found"}
