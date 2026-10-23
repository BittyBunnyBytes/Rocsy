"""REA folders: host directories (games, programs) mounted read-only into bunny-rea.

The REA page (through the gateway) browses host directories here and picks folders.
Each one becomes a `<host path>:/folders/<slug>:ro` volume inside a marked block of the
rea service in docker-compose.override.yml (local to this machine, git-ignored, merged over
docker-compose.yml by compose), then bunny-rea is recreated (no rebuild). Docker cannot
add a mount to a running container, so this is the only way to reach a new folder.

Which folders may be mounted is up to BLOCKED below (empty by default). Mounts are always
read-only; the REA container also serves the Master Agent's MCP tools, which can read any mounted file."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import coreextras

from paths import COMPOSE_DIR, HERMES_HOME

HOME = Path.home()
COMPOSE = COMPOSE_DIR / "docker-compose.override.yml"
STATE = COMPOSE_DIR / "rea/folders.json"
BEGIN = "      # >>> REA folders (managed by hostctl/reafolders.py: add/remove them on the REA page)\n"
END = "      # <<< REA folders\n"
ANCHOR = "  rea:\n    volumes:\n"
MAX_FOLDERS = 20
LOCK = threading.Lock()

# Your own blocked folders: a folder that is inside one of these, or contains one, is refused.
# Empty on purpose (the built-in list was removed on request); add Path(...) entries here.
BLOCKED: list[Path] = []


class Refused(Exception):
    pass


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _blocked_hit(real: Path) -> Path | None:
    for blocked in BLOCKED:
        if _inside(real, blocked):
            return blocked
    return None


def check(path: str) -> Path:
    """The real directory to mount, or Refused with the reason."""
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 1024:
        raise Refused("Give an absolute folder path")
    if any(ch in path for ch in ':"\n\r#') or path.strip() != path:
        raise Refused("Folder paths with : \" # or line breaks can't be mounted")
    real = Path(os.path.realpath(path))
    if not real.is_dir():
        raise Refused("That folder doesn't exist")
    if _blocked_hit(real):
        raise Refused(f"{real} is in a blocked folder")
    for blocked in BLOCKED:
        if _inside(blocked, real):
            raise Refused(f"{real} contains the blocked folder {blocked}")
    return real


def load() -> list[dict]:
    try:
        data = json.loads(STATE.read_text())
        return [f for f in data if isinstance(f, dict) and f.get("path") and f.get("slug")]
    except (OSError, ValueError):
        return []


def mounted() -> dict[str, str]:
    """slug -> host path, as the running container sees them."""
    result = subprocess.run(["docker", "inspect", "--format", "{{json .Mounts}}", "bunny-rea"],
                            capture_output=True, text=True, timeout=10)
    try:
        mounts = json.loads(result.stdout) if result.returncode == 0 else []
    except ValueError:
        mounts = []
    return {m["Destination"].split("/folders/", 1)[1]: m.get("Source", "") for m in mounts or []
            if str(m.get("Destination", "")).startswith("/folders/")}


def view() -> dict:
    live = mounted()
    return {"folders": [{**f, "mounted": live.get(f["slug"]) == f["path"]} for f in load()],
            "max": MAX_FOLDERS}


def _slug(path: Path, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name).strip("._-")[:60] or "folder"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _write_compose(folders: list[dict]) -> None:
    text = COMPOSE.read_text() if COMPOSE.is_file() else "services:\n"
    start, end = text.find(BEGIN), text.find(END)
    if start >= 0 and end > start:
        text = text[:start] + text[end + len(END):]
    elif start >= 0 or end >= 0:
        raise RuntimeError(f"The REA folders block in {COMPOSE.name} is damaged; fix it by hand")
    if text.count(ANCHOR) > 1:
        raise RuntimeError(f"{COMPOSE.name} has the rea service more than once; fix it by hand")
    if not text.count(ANCHOR):
        if "\n  rea:\n" in "\n" + text:
            raise RuntimeError(f"Couldn't find the rea service volumes in {COMPOSE.name}")
        if not text.startswith("services:") and "\nservices:\n" not in text:
            raise RuntimeError(f"{COMPOSE.name} has no services: section")
        text = text.rstrip("\n") + "\n\n" + ANCHOR
    if folders:
        lines = "".join(f'      - "{f["path"]}:/folders/{f["slug"]}:ro"\n' for f in folders)
        text = text.replace(ANCHOR, ANCHOR + BEGIN + lines + END)
    elif text.endswith(ANCHOR):
        text = text[:-len(ANCHOR)].rstrip("\n") + "\n"      # an empty volumes: list is invalid
    check_file = COMPOSE_DIR / ".docker-compose.rea-folders-check.yml"
    check_file.write_text(text)
    try:
        result = subprocess.run(["docker", "compose", "-f", "docker-compose.yml", "-f", str(check_file), "config", "--quiet"],
                                cwd=COMPOSE_DIR, capture_output=True, text=True, timeout=30)
    finally:
        check_file.unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError(f"{COMPOSE.name} would not be valid: " + (result.stderr or result.stdout).strip()[-300:])
    if COMPOSE.is_file():
        shutil.copy2(COMPOSE, COMPOSE.with_name(COMPOSE.name + ".bak-rea-folders"))
    tmp = COMPOSE.with_name(".docker-compose.yml.rea-tmp")
    tmp.write_text(text)
    os.replace(tmp, COMPOSE)


def _recreate(force: bool) -> None:
    busy = coreextras.rea_busy()
    if busy and not force:
        raise Refused(f"REA is analysing ({', '.join(busy)}); remounting restarts it and closes open files")
    result = subprocess.run(["docker", "compose", "up", "-d", "--no-deps", "rea"], cwd=COMPOSE_DIR,
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-300:])


def _save(folders: list[dict], force: bool) -> dict:
    previous = load()
    STATE.write_text(json.dumps(folders, indent=1))
    try:
        _write_compose(folders)
        _recreate(force)
    except Exception:
        STATE.write_text(json.dumps(previous, indent=1))
        if isinstance(previous, list):
            try:
                _write_compose(previous)
            except Exception:
                pass
        raise
    return view()


def add(path: str, force: bool = False) -> dict:
    with LOCK:
        real = check(path)
        folders = load()
        if any(f["path"] == str(real) for f in folders):
            return view()
        if len(folders) >= MAX_FOLDERS:
            raise Refused(f"Up to {MAX_FOLDERS} folders; remove one first")
        folders.append({"slug": _slug(real, {f["slug"] for f in folders}), "path": str(real), "added": time.time()})
        return _save(folders, force)


def remove(slug: str, force: bool = False) -> dict:
    with LOCK:
        folders = load()
        kept = [f for f in folders if f["slug"] != slug]
        if len(kept) == len(folders):
            raise Refused("Unknown folder")
        return _save(kept, force)


def _shortcuts() -> list[dict]:
    candidates = [("Home", HOME), ("Games", HOME / "Games"),
                  ("Steam games", HOME / ".local/share/Steam/steamapps/common"),
                  ("Steam games (Flatpak)", HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common"),
                  ("Heroic games", HOME / "Games/Heroic"), ("Desktop", HOME / "Desktop"), ("Downloads", HOME / "Downloads"),
                  ("/opt", Path("/opt")), ("/usr", Path("/usr")), ("/mnt", Path("/mnt")),
                  ("Removable drives", Path("/run/media") / HOME.name), ("/media", Path("/media"))]
    return [{"label": label, "path": str(p)} for label, p in candidates if p.is_dir()]


def browse(path: str | None, hidden: bool) -> dict:
    target = Path(os.path.realpath(path)) if path else HOME
    if not target.is_dir():
        raise Refused("That folder doesn't exist")
    if _blocked_hit(target):
        raise Refused("That folder is blocked")
    added_paths = {f["path"] for f in load()}
    dirs = []
    try:
        entries = sorted(os.scandir(target), key=lambda e: e.name.lower())
    except OSError as exc:
        raise Refused(f"Can't read {target}: {exc.strerror}") from exc
    for entry in entries:
        if entry.name.startswith(".") and not hidden:
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        full = Path(entry.path)
        try:
            check(str(full))
            reason = ""
        except Refused as exc:
            reason = str(exc)
        dirs.append({"name": entry.name, "path": str(full), "refused": reason,
                     "added": os.path.realpath(full) in added_paths})
    try:
        check(str(target))
        here = ""
    except Refused as exc:
        here = str(exc)
    return {"path": str(target), "parent": str(target.parent) if target != target.parent else None,
            "dirs": dirs[:1000], "refused": here, "added": str(target) in added_paths,
            "shortcuts": _shortcuts()}


# ------------------------------------------------------------ open in the file manager --
# Paths come as REA sees them; only REA's own roots map back to host folders.
WORKSPACE_HOST = HERMES_HOME / "rocsy-workspace"
ATTACHMENTS_HOST = HERMES_HOME / "rocsy-attachments"


def host_path(container_path: str) -> Path:
    if not isinstance(container_path, str) or "\0" in container_path:
        raise Refused("Bad path")
    roots = [("/opt/data/rocsy-workspace", WORKSPACE_HOST), ("/opt/data/rocsy-attachments", ATTACHMENTS_HOST)]
    roots += [(f"/folders/{f['slug']}", Path(f["path"])) for f in load()]
    for prefix, base in roots:
        if container_path == prefix or container_path.startswith(prefix + "/"):
            base = Path(os.path.realpath(base))
            real = Path(os.path.realpath(base / container_path[len(prefix):].lstrip("/")))
            if real != base and base not in real.parents:
                raise Refused("That path leaves its folder")
            if not real.exists():
                raise Refused("That file or folder doesn't exist any more")
            return real
    raise Refused("Not a path REA knows")


def open_in_file_manager(container_path: str) -> dict:
    real = host_path(container_path)
    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0"),
           "XAUTHORITY": os.environ.get("XAUTHORITY", str(HOME / ".Xauthority"))}
    uri = real.as_uri()
    # A file opens its folder with the file selected; a folder opens itself.
    method = "ShowItems" if real.is_file() else "ShowFolders"
    result = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
                             "--object-path", "/org/freedesktop/FileManager1",
                             "--method", f"org.freedesktop.FileManager1.{method}", f"['{uri}']", ""],
                            capture_output=True, text=True, timeout=15, env=env)
    if result.returncode:
        subprocess.Popen(["xdg-open", str(real if real.is_dir() else real.parent)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"opened": str(real)}


def handle(method: str, path: str, body: dict) -> tuple[int, dict]:
    try:
        if method == "GET" and path == "/rea/folders":
            return 200, view()
        if method == "GET" and path.startswith("/rea/browse"):
            from urllib.parse import parse_qs, urlsplit
            query = parse_qs(urlsplit(path).query)
            return 200, browse((query.get("path") or [None])[0], (query.get("hidden") or ["0"])[0] == "1")
        if method == "POST" and path == "/rea/folders/add":
            return 200, add(body.get("path"), bool(body.get("force")))
        if method == "POST" and path == "/rea/open":
            return 200, open_in_file_manager(body.get("path"))
        if method == "POST" and path == "/rea/folders/remove":
            return 200, remove(str(body.get("slug") or ""), bool(body.get("force")))
        return 404, {"error": "not found"}
    except Refused as exc:
        return 409, {"error": str(exc), "can_force": "analysing" in str(exc)}
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return 500, {"error": str(exc)[:300]}
