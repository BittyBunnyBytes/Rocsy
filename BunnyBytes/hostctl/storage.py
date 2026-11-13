"""Storage locations: where Rocsy keeps model files (Settings -> Storage, Models -> Manager -> Move).

ComfyUI's models/ folder stays the one place every service looks. A model moved to another
folder or drive is replaced there by a symlink to its new home, so ComfyUI (on the host),
llama-swap, the studios and the Models hub keep finding it under the same name and path.

Containers can only follow such a link if they can see its target, so every location is
bind-mounted at the same path into CONSUMERS through a marked block in
docker-compose.override.yml (git-ignored, local to this machine). Docker can't add a mount to a
running container, so the apply worker recreates them one by one once each is idle (hostctl's
busy checks; llama waits until no model is loaded). Moves INTO a location wait until every
consumer sees it. Removable drives (/run/media/<user>/X, /media/<user>/X) are mounted by their
parent folder with rslave propagation: a drive plugged in later shows up inside, and an
unplugged one doesn't stop a container from starting.

Moves run one at a time: same filesystem = hard link (instant), otherwise a copy that is read
back and SHA-256-compared with the source. The old name is then swapped for the link (or, going
back to the models folder, for the file) in one rename, so a model is never missing for a
moment; sidecars (.sha256, LoRA Manager metadata, previews) stay next to the link.

State: hostctl/storage.json (this machine only, never shipped).
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from paths import COMFY_ROOT, COMPOSE_DIR, ROOT, HERMES_HOME, env_setting

HOME = Path.home()
MODELS = Path(os.getenv("CORE_COMFY_DIR", COMFY_ROOT)) / "models"
STATE = Path(os.getenv("STORAGE_STATE", Path(__file__).resolve().parent / "storage.json"))
COMPOSE = COMPOSE_DIR / "docker-compose.override.yml"
BEGIN = "      # >>> Rocsy storage (managed by hostctl/storage.py: Settings -> Storage)\n"
END = "      # <<< Rocsy storage\n"
OWN_VOLUMES = "    volumes:  # rocsy-storage\n"   # keys this module added (removed again when empty)
OWN_MARK = "  # rocsy-storage"
LLAMA = env_setting("LLAMA_UPSTREAM") or "http://127.0.0.1:8400"
BUILTIN = "comfy"
CATEGORIES = ("models", "loras", "llm")
# What a location can be used for (Settings -> Storage -> Edit). Model categories = what it may hold and be a
# download default for; "outputs" = it may hold the service outputs.
HANDLES = CATEGORIES + ("outputs",)
HANDLE_NAMES = {"models": "models", "loras": "LoRAs", "llm": "LLMs", "outputs": "service outputs"}
MAX_LOCATIONS = 12
CHUNK = 16 * 1024 * 1024
# Compose service -> (container, hostctl busy kind, writable). Everything that reads model files.
CONSUMERS: dict[str, tuple[str, str, bool]] = {
    "modelhub": ("bunny-models", "models", True),   # downloads into locations, uninstall removes there
    "video": ("video", "vid", False),
    "krea": ("krea", "krea", False),
    "music": ("music", "mus", False),
    "trainer": ("bunny-trainer", "trainer", False),
    "llama": ("llama", "llama", False),
    "voice": ("bunny-voice", "voice", False),       # only gets mounts for service outputs (OUTPUT_WRITERS)
}
MODEL_READERS = ("modelhub", "video", "krea", "music", "trainer", "llama")
NAMES = {"modelhub": "Models", "video": "Video", "krea": "Image", "music": "Music", "trainer": "Trainer", "llama": "Llama",
         "voice": "Voice", "gateway": "Gateway", "connections": "Connections"}

# Service outputs (Settings -> Storage -> Service outputs). Each folder can live in one storage location
# (<location's drive folder>/Rocsy outputs/<key>); its old path becomes a symlink, so ComfyUI, compose files and
# the apps keep using the same path. "binds": compose services that bind-mount the folder itself (docker resolves
# the link when the container is created, so they're force-recreated at the switch). OUTPUT_WRITERS see the folder
# through a link inside a folder they mount, so the location is bind-mounted into them read-write at the same path.
OUTPUT_DIR = "Rocsy outputs"
OUTPUT_WRITERS = ("video", "trainer", "voice")
OUTPUT_BUSY = ("comfy", "queue", "vid", "krea", "mus", "trainer", "voice")
OUTPUT_BUSY_CONTAINER = {"vid": "video", "krea": "krea", "mus": "music", "trainer": "bunny-trainer", "voice": "bunny-voice"}
SERVICE_CONTAINERS = {**{s: c[0] for s, c in CONSUMERS.items()}, "gateway": "bunny-gateway", "connections": "bunny-connections"}


def output_folders() -> list[dict]:
    comfy = Path(os.getenv("CORE_COMFY_DIR", COMFY_ROOT)) / "output"
    return [
        {"key": "comfyui", "label": "Renders (Video, Image, Music)", "path": comfy,
         "note": "ComfyUI's output folder", "binds": ["video", "music", "krea", "connections", "modelhub", "gateway", "voice"]},
        {"key": "video-stitched", "label": "Stitched films", "path": ROOT / "Video" / "data" / "stitched", "note": "Video", "binds": []},
        {"key": "video-postedit", "label": "Post-Edits", "path": ROOT / "Video" / "data" / "postedit", "note": "Video", "binds": []},
        {"key": "trainer", "label": "Trained LoRAs", "path": ROOT / "Trainer" / "data" / "output", "note": "Trainer", "binds": ["video"]},
        {"key": "voice", "label": "Voice clips", "path": COMPOSE_DIR / "voice" / "data" / "outputs", "note": "Voice", "binds": []},
    ]
SHARD = re.compile(r"-00001-of-(\d{5})\.(gguf|safetensors)$")
WEIGHT = re.compile(r"\.(safetensors|sft|gguf|ckpt|pt|pth|bin|pkl|onnx)$", re.I)

LOCK = threading.RLock()
WAKE = threading.Event()
busy_check: Callable[[str], tuple[bool, str]] | None = None   # set by hostctl.main
_apply = {"pending": [], "running": "", "error": "", "checked": 0.0, "force_llama": False}
_jobs: dict[str, dict] = {}
_queue: list[str] = []
_cache: dict[str, tuple[float, Any]] = {}


class Refused(Exception):
    pass


# ------------------------------------------------------------------ state --

def load() -> dict:
    try:
        data = json.loads(STATE.read_text())
    except (OSError, ValueError):
        data = {}
    locations = [l for l in data.get("locations") or [] if isinstance(l, dict) and l.get("id") and l.get("path")]
    for loc in locations:
        handles = loc.get("handles")
        loc["handles"] = [h for h in HANDLES if h in handles] if isinstance(handles, list) else list(HANDLES)
    defaults = {c: str((data.get("defaults") or {}).get(c) or BUILTIN) for c in CATEGORIES}
    ids = {l["id"] for l in locations}
    holds = {c: {l["id"] for l in locations if c in l["handles"]} for c in CATEGORIES}
    defaults = {c: v if v == BUILTIN or v in holds[c] else BUILTIN for c, v in defaults.items()}
    # Same-path mounts this module wrote at some point: the ones that may still need removing.
    known = [str(k) for k in data.get("known") or [] if isinstance(k, str)]
    # Service outputs: "at" = where they live, "to" = where a running move takes them ("" = none).
    outputs = data.get("outputs") if isinstance(data.get("outputs"), dict) else {}
    outputs = {k: str(outputs.get(k) or "") for k in ("at", "to")}
    outputs = {k: v if v in ids or (k == "at" and v == BUILTIN) else "" for k, v in outputs.items()}
    outputs["at"] = outputs["at"] or BUILTIN
    return {"locations": locations, "defaults": defaults, "known": known, "outputs": outputs}


def save(state: dict) -> None:
    tmp = STATE.with_name(STATE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, STATE)


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def category_of(rel: str) -> str:
    """Which download/holding category a path under models/ belongs to."""
    top = str(rel).split("/", 1)[0]
    return "loras" if top == "loras" else "llm" if top == "llm" else "models"


def location_of(real: Path, state: dict) -> str | None:
    """Which location holds this real file: "comfy", a location id, or None (somewhere else)."""
    if _inside(real, _real(MODELS)):
        return BUILTIN
    for loc in state["locations"]:
        if _inside(real, Path(loc["path"])):
            return loc["id"]
    return None


# ------------------------------------------------------------------ disks --

def _cached(key: str, ttl: float, make: Callable[[], Any]) -> Any:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = make()
    _cache[key] = (time.monotonic(), value)
    return value


def _findmnt(path: Path) -> dict:
    def make() -> dict:
        result = subprocess.run(["findmnt", "-n", "-J", "-o", "TARGET,SOURCE,FSTYPE", "-T", str(path)],
                                capture_output=True, text=True, timeout=10)
        try:
            fs = json.loads(result.stdout)["filesystems"][0]
        except (ValueError, KeyError, IndexError):
            return {"target": "/", "source": "", "fstype": ""}
        return {"target": fs.get("target") or "/", "source": fs.get("source") or "", "fstype": fs.get("fstype") or ""}
    return _cached(f"mnt:{path}", 30, make)


def _device_info(source: str) -> dict:
    """Model / removable / bus of the disk behind a mount source like /dev/sda1 or /dev/nvme0n1p2[/@home]."""
    dev = source.split("[", 1)[0]
    if not dev.startswith("/dev/"):
        return {}

    def make() -> dict:
        parent = subprocess.run(["lsblk", "-ndo", "PKNAME", dev], capture_output=True, text=True, timeout=10).stdout.strip()
        disk = f"/dev/{parent}" if parent else dev
        result = subprocess.run(["lsblk", "-J", "-ndo", "MODEL,RM,TRAN,LABEL", disk], capture_output=True, text=True, timeout=10)
        label = subprocess.run(["lsblk", "-ndo", "LABEL", dev], capture_output=True, text=True, timeout=10).stdout.strip()
        try:
            info = json.loads(result.stdout)["blockdevices"][0]
        except (ValueError, KeyError, IndexError):
            info = {}
        return {"model": (info.get("model") or "").strip(), "removable": bool(info.get("rm")) or info.get("tran") == "usb",
                "bus": info.get("tran") or "", "label": label}
    return _cached(f"dev:{dev}", 300, make)


def disk(path: Path) -> dict:
    try:
        st = os.statvfs(path)
    except OSError:
        return {"online": False}
    mnt = _findmnt(path)
    return {"online": True, "total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize,
            "fstype": mnt["fstype"], "mount": mnt["target"], **_device_info(mnt["source"])}


def _automount_parents() -> list[Path]:
    return [Path("/run/media") / HOME.name, Path("/media") / HOME.name, Path("/media")]


def mount_source(path: Path) -> Path:
    """What to bind-mount so containers see `path`: the drive's parent folder for removable drives
    (survives unplug/replug), the drive's mount point for other drives, else the folder itself."""
    target = Path(_findmnt(path)["target"])
    if target.parent in _automount_parents():
        return target.parent
    if target in (Path("/"), Path("/home"), HOME) or _inside(_real(MODELS), target):
        return path
    return target


# ------------------------------------------------------- compose + mounts --

def wanted_mounts(state: dict) -> list[Path]:
    sources: list[Path] = []
    for loc in state["locations"]:
        if not set(loc["handles"]) & set(CATEGORIES):
            continue        # outputs only: the model readers don't need it
        src = mount_source(Path(loc["path"]))
        if src not in sources:
            sources.append(src)
    # A folder already inside another mounted folder needs no mount of its own.
    return [s for s in sources if not any(o != s and _inside(s, o) for o in sources)]


def output_base(loc: dict) -> Path:
    """<the location's folder>/Rocsy outputs; for a "Rocsy models" folder, next to it on the same drive."""
    path = Path(loc["path"])
    return (path.parent if path.name == "Rocsy models" else path) / OUTPUT_DIR


def output_mounts(state: dict) -> list[Path]:
    """What OUTPUT_WRITERS need mounted (read-write) to follow links into the outputs location(s)."""
    out: list[Path] = []
    ids = {state["outputs"]["at"], state["outputs"]["to"]} | {_output_where(f, state)[0] for f in output_folders()}
    for loc_id in sorted(ids - {BUILTIN, "", "missing"}):
        loc = next((l for l in state["locations"] if l["id"] == loc_id), None)
        if not loc:
            continue
        base = output_base(loc)
        src = mount_source(Path(loc["path"]))
        if not _inside(base, src):
            src = base      # a plain folder location: mount just its outputs folder
        if src not in out:
            out.append(src)
    return out


def wanted_by_service(state: dict) -> dict[str, dict[str, bool]]:
    """service -> {same-path mount: writable} it should have."""
    out: dict[str, dict[str, bool]] = {s: {} for s in CONSUMERS}
    for m in wanted_mounts(state):
        for s in MODEL_READERS:
            out[s][str(m)] = out[s].get(str(m), False) or CONSUMERS[s][2]
    for m in output_mounts(state):
        for s in OUTPUT_WRITERS:
            out[s][str(m)] = True
    for s, mounts in out.items():
        # Inside another mount of this service that is at least as writable: not needed.
        out[s] = {m: w for m, w in mounts.items()
                  if not any(o != m and _inside(Path(m), Path(o)) and (mounts[o] or not w) for o in mounts)}
    return out


def _all_wanted(state: dict) -> list[str]:
    return sorted({m for mounts in wanted_by_service(state).values() for m in mounts})


def _service_span(lines: list[str], name: str) -> tuple[int, int] | None:
    start = next((i for i, l in enumerate(lines) if l.split("#")[0].rstrip() == f"  {name}:"), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line.lstrip().startswith("#") and len(line) - len(line.lstrip()) <= 2:
            break
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def _covered(mount: str, targets: set[str]) -> bool:
    return any(_inside(Path(mount), Path(t)) for t in targets)


def render_override(text: str, mounts: list[Path] | dict[str, dict[str, bool]], existing: dict[str, set[str]]) -> str:
    """docker-compose.override.yml with our blocks for these mounts (and none when there are none).
    mounts: service -> {path: writable}, or a plain list = model locations for every model reader."""
    if not isinstance(mounts, dict):
        mounts = {s: {str(m): CONSUMERS[s][2] for m in mounts} for s in MODEL_READERS}
    # Drop the old blocks (and volumes: keys we added that are now empty).
    while BEGIN in text:
        start, end = text.find(BEGIN), text.find(END)
        if end < start:
            raise RuntimeError(f"The Rocsy storage block in {COMPOSE.name} is damaged; fix it by hand")
        text = text[:start] + text[end + len(END):]
    if END in text:
        raise RuntimeError(f"The Rocsy storage block in {COMPOSE.name} is damaged; fix it by hand")
    lines = text.splitlines(keepends=True)
    for key, child in ((OWN_VOLUMES, "      "), (None, "    ")):
        out = []
        for i, line in enumerate(lines):
            own = line == key if key else (line.endswith(OWN_MARK + "\n") and line.startswith("  ") and not line.startswith("   "))
            rest = next((l for l in lines[i + 1:] if l.strip()), "")
            if own and not rest.startswith(child):
                if out and not out[-1].strip():
                    out.pop()
                continue
            out.append(line)
        lines = out
    if not lines or not any(l.rstrip() == "services:" for l in lines):
        lines += ["services:\n"]
    for service in CONSUMERS:
        have = existing.get(service, {"same": set(), "all": set()})
        wanted = mounts.get(service) or {}
        entries = [m for m in wanted if not _covered(str(m), have["same"]) and str(m) not in have["all"]]
        if not entries:
            continue
        block = [BEGIN]
        for m in entries:
            block += ["      - type: bind\n", f'        source: "{m}"\n', f'        target: "{m}"\n']
            if not wanted[m]:
                block.append("        read_only: true\n")
            block += ["        bind:\n", "          create_host_path: true\n", "          propagation: rslave\n"]
        block.append(END)
        span = _service_span(lines, service)
        if span is None:
            services = next(i for i, l in enumerate(lines) if l.rstrip() == "services:")
            end = services + 1
            while end < len(lines) and (not lines[end].strip() or lines[end].startswith(" ") or lines[end].lstrip().startswith("#")):
                end += 1
            while end > services + 1 and not lines[end - 1].strip():
                end -= 1
            if lines and not lines[end - 1].endswith("\n"):
                lines[end - 1] += "\n"
            lines[end:end] = ["\n", f"  {service}:{OWN_MARK}\n", OWN_VOLUMES] + block
            continue
        start, end = span
        vol = next((j for j in range(start + 1, end) if lines[j].rstrip().split("#")[0].rstrip() == "    volumes:"), None)
        if vol is None:
            if not lines[end - 1].endswith("\n"):
                lines[end - 1] += "\n"
            lines[end:end] = [OWN_VOLUMES] + block
        else:
            lines[vol + 1:vol + 1] = block
    return "".join(lines)


def _compose_config(files: list[str]) -> dict:
    args = ["docker", "compose"] + [a for f in files for a in ("-f", f)] + ["config", "--format", "json"]
    result = subprocess.run(args, cwd=COMPOSE_DIR, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("docker compose config failed: " + (result.stderr or result.stdout).strip()[-300:])
    return json.loads(result.stdout)


def _targets(config: dict) -> dict[str, dict[str, set[str]]]:
    """Per consumer: every mount target, and the same-path binds (host folder = container folder)."""
    out = {}
    for service in CONSUMERS:
        vols = [v for v in ((config.get("services") or {}).get(service) or {}).get("volumes") or [] if isinstance(v, dict)]
        out[service] = {"all": {str(v.get("target")) for v in vols},
                        "same": {str(v.get("target")) for v in vols if v.get("type") == "bind" and v.get("source") == v.get("target")}}
    return out


def write_compose(state: dict) -> None:
    text = COMPOSE.read_text() if COMPOSE.is_file() else "services:\n"
    base = render_override(text, [], {})
    check = COMPOSE_DIR / ".docker-compose.storage-check.yml"
    try:
        check.write_text(base)
        existing = _targets(_compose_config(["docker-compose.yml", str(check)]))
        new = render_override(text, wanted_by_service(state), existing)
        check.write_text(new)
        _compose_config(["docker-compose.yml", str(check)])     # must stay valid
    finally:
        check.unlink(missing_ok=True)
    if new == text:
        return
    if COMPOSE.is_file():
        shutil.copy2(COMPOSE, COMPOSE.with_name(COMPOSE.name + ".bak-storage"))
    tmp = COMPOSE.with_name(".docker-compose.override.yml.storage-tmp")
    tmp.write_text(new)
    os.replace(tmp, COMPOSE)


def live_mounts(container: str) -> dict[str, str] | None:
    """target -> source of a container's bind mounts, or None when it doesn't exist."""
    result = subprocess.run(["docker", "inspect", "--format", "{{json .Mounts}}", container],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        return None
    try:
        mounts = json.loads(result.stdout) or []
    except ValueError:
        return None
    _live_rw[container] = {m.get("Destination", ""): bool(m.get("RW")) for m in mounts}
    return {m.get("Destination", ""): m.get("Source", "") for m in mounts}


_live_rw: dict[str, dict[str, bool]] = {}   # container -> target -> writable (filled by live_mounts)


def _running(container: str) -> bool:
    result = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", container],
                            capture_output=True, text=True, timeout=15)
    return result.stdout.strip() == "true"


def pending_services(state: dict) -> list[dict]:
    """Consumers whose container doesn't (yet) have exactly the mounts it should."""
    by_service = wanted_by_service(state)
    out = []
    for service, (container, _busy, _w) in CONSUMERS.items():
        wanted = by_service.get(service) or {}
        live = live_mounts(container)
        if live is None:
            continue    # not installed here (e.g. no Trainer): nothing to refresh
        same = {t for t, s in live.items() if t == s}
        rw = _live_rw.get(container, {})
        writable = {t for t in same if rw.get(t)}
        missing = sorted(m for m, w in wanted.items() if not _covered(m, writable if w else same))
        stale = sorted(t for t in same if t in state["known"] and t not in wanted)
        if missing or stale:
            out.append({"service": service, "name": NAMES[service], "missing": missing, "stale": stale})
    return out


def _llama_loaded() -> list[str] | None:
    key = env_setting("LLAMA_API_KEY")
    try:
        req = urllib.request.Request(f"{LLAMA}/running", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=5) as response:
            return [str(m.get("model")) for m in json.loads(response.read()).get("running", [])]
    except (OSError, ValueError):
        return None


def _busy(service: str) -> str:
    """Why this container can't be recreated right now ("" = it can)."""
    kind = CONSUMERS[service][1]
    if kind == "llama":
        loaded = _llama_loaded()
        if loaded is None:
            return "llama-swap isn't answering"
        if loaded and not _apply["force_llama"]:
            return f"model loaded: {', '.join(loaded)}"
        return ""
    if kind == "models" and any(j["state"] in ("queued", "copying", "verifying") for j in _jobs.values()):
        return "a move is running"
    if busy_check is None:
        return "busy checks aren't ready"
    try:
        busy, detail = busy_check(kind)
        if not busy and kind in ("vid", "krea", "mus", "trainer"):
            busy, detail = busy_check("queue")      # a queued GPU job may belong to it
    except Exception as exc:  # noqa: BLE001 - unreadable = busy (fail closed)
        return f"couldn't check ({type(exc).__name__})"
    return detail if busy else ""


def apply_once() -> None:
    state = load()
    pending = pending_services(state)
    _apply["checked"] = time.time()
    for item in pending:
        reason = _busy(item["service"])
        item["waiting"] = reason
        if reason:
            continue
        container = CONSUMERS[item["service"]][0]
        args = ["docker", "compose", "up", "--no-deps"] + (["-d"] if _running(container) else ["--no-start"]) + [item["service"]]
        _apply["running"] = item["name"]
        try:
            result = subprocess.run(args, cwd=COMPOSE_DIR, capture_output=True, text=True, timeout=300)
            if result.returncode:
                _apply["error"] = f"{item['name']}: " + (result.stderr or result.stdout).strip()[-300:]
                item["waiting"] = "refresh failed"
            else:
                item["done"] = True
                _apply["error"] = ""
        finally:
            _apply["running"] = ""
    _apply["pending"] = [p for p in pending if not p.get("done")]
    if not _apply["pending"]:
        _apply["force_llama"] = False
        wanted = _all_wanted(state)
        if sorted(state["known"]) != sorted(wanted):
            save({**state, "known": wanted})       # removed mounts are gone from every container


def apply_loop() -> None:
    while True:
        WAKE.wait(30)
        WAKE.clear()
        state = load()
        if not state["locations"] and not state["known"]:
            _apply["pending"] = []
            continue
        try:
            with LOCK:
                apply_once()
        except Exception as exc:  # noqa: BLE001 - shown in Settings, retried
            _apply["error"] = str(exc)[:300]


def ready(loc: dict, pending: list[dict]) -> bool:
    """Every container that reads models can follow links into this location."""
    path = Path(loc["path"])
    return not any(_inside(path, Path(m)) for p in pending for m in p["missing"])


# ---------------------------------------------------------------- browse --

def _check_path(path: str) -> Path:
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 1024:
        raise Refused("Give an absolute folder path")
    if any(ch in path for ch in '"\\$\n\r\0') or path.strip() != path:
        raise Refused('Folder paths with " \\ $ or line breaks can\'t be used')
    real = _real(Path(path))
    if not real.is_dir():
        raise Refused("That folder doesn't exist")
    if _inside(real, _real(MODELS)):
        raise Refused("That's inside ComfyUI's models folder already")
    for bad in (Path("/proc"), Path("/sys"), Path("/dev"), Path("/etc"), Path("/usr"), Path("/boot"), Path("/var/lib/docker")):
        if _inside(real, bad):
            raise Refused(f"{bad} can't hold models")
    return real


def browse(path: str | None, hidden: bool = False) -> dict:
    target = _real(Path(path)) if path else HOME
    if not target.is_dir():
        raise Refused("That folder doesn't exist")
    dirs = []
    try:
        entries = sorted(os.scandir(target), key=lambda e: e.name.lower())
    except OSError as exc:
        raise Refused(f"Can't read {target}: {exc.strerror}") from exc
    for entry in entries:
        if entry.name.startswith(".") and not hidden:
            continue
        try:
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": entry.path})
        except OSError:
            continue
    try:
        _check_path(str(target))
        refused = ""
    except Refused as exc:
        refused = str(exc)
    writable = os.access(target, os.W_OK)
    return {"path": str(target), "parent": str(target.parent) if target != target.parent else None,
            "dirs": dirs[:1000], "refused": refused or ("" if writable else "You can't write to this folder"),
            "disk": disk(target), "shortcuts": shortcuts()}


def shortcuts() -> list[dict]:
    """Home plus every real drive that's mounted (removable ones included)."""
    out = [{"label": "Home", "path": str(HOME), "disk": disk(HOME)}]
    result = subprocess.run(["findmnt", "-J", "-l", "-o", "TARGET,SOURCE,FSTYPE", "-t",
                             "btrfs,ext4,ext3,xfs,f2fs,ntfs,ntfs3,exfat,vfat,bcachefs,zfs,fuseblk"],
                            capture_output=True, text=True, timeout=10)
    try:
        mounts = json.loads(result.stdout)["filesystems"]
    except (ValueError, KeyError):
        mounts = []
    seen = {HOME}
    root_src = _findmnt(HOME)["source"].split("[", 1)[0]
    for fs in mounts:
        target = Path(fs["target"])
        src = (fs.get("source") or "").split("[", 1)[0]
        if target in seen or src == root_src or str(target).startswith(("/boot", "/efi", "/var/lib/snapd", "/snap")):
            continue
        seen.add(target)
        info = _device_info(fs.get("source") or "")
        name = info.get("label") or info.get("model") or target.name or str(target)
        out.append({"label": name, "path": str(target), "disk": disk(target), "removable": info.get("removable", False)})
    return out


# ------------------------------------------------------------- locations --

def _slug(text: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "location"
    if base == BUILTIN:
        base = "comfy-2"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _clean_handles(handles: Any) -> list[str]:
    if not isinstance(handles, list) or not all(isinstance(h, str) for h in handles):
        raise Refused("Pick what this location is for")
    picked = [h for h in HANDLES if h in handles]
    if not picked:
        raise Refused("Pick at least one thing this location is for")
    return picked


def add_location(path: str, label: str = "", subfolder: bool = True, handles: Any = None, downloads: Any = None) -> dict:
    with LOCK:
        state = load()
        handles = list(HANDLES) if handles is None else _clean_handles(handles)
        real = _check_path(path)
        if not os.access(real, os.W_OK):
            raise Refused("You can't write to that folder")
        if subfolder and real.name != "Rocsy models":
            real = real / "Rocsy models"
            real.mkdir(exist_ok=True)
        probe = real / f".rocsy-write-test-{uuid.uuid4().hex[:6]}"
        try:
            probe.write_bytes(b"ok")
        except OSError as exc:
            raise Refused(f"Can't write there: {exc.strerror}") from exc
        finally:
            probe.unlink(missing_ok=True)
        for loc in state["locations"]:
            if _inside(real, Path(loc["path"])) or _inside(Path(loc["path"]), real):
                raise Refused(f"That overlaps the location “{loc['label']}”")
        if len(state["locations"]) >= MAX_LOCATIONS:
            raise Refused(f"Up to {MAX_LOCATIONS} locations; remove one first")
        info = disk(real)
        label = (label or "").strip()[:60] or info.get("label") or info.get("model") or real.parent.name or real.name
        loc = {"id": _slug(label, {l["id"] for l in state["locations"]}), "label": label, "path": str(real), "added": time.time(),
               "handles": handles}
        previous = json.loads(json.dumps(state))
        state["locations"].append(loc)
        for category in downloads if isinstance(downloads, list) else []:
            if category in CATEGORIES and category in handles:
                state["defaults"][category] = loc["id"]
        save(state)
        try:
            write_compose(state)
        except Exception:
            save(previous)
            raise
        _remember_known(load())
        WAKE.set()
        return view()


def remove_location(loc_id: str) -> dict:
    with LOCK:
        state = load()
        loc = next((l for l in state["locations"] if l["id"] == loc_id), None)
        if not loc:
            raise Refused("Unknown location")
        stored = [rel for rel, where in links(state).items() if where["location"] == loc_id]
        if stored:
            raise Refused(f"{len(stored)} model file(s) still live there: move them first (Models -> Manager -> Installed)")
        if any(j["state"] in ("queued", "copying", "verifying") and loc_id in (j["to"], j.get("from")) for j in _jobs.values()):
            raise Refused("A move into or out of it is running")
        if loc_id in (state["outputs"]["at"], state["outputs"]["to"]) or any(
                f["where"] == loc_id for f in _output_state(state)):
            raise Refused("Service outputs are stored there: move them back first (Service outputs below)")
        previous = json.loads(json.dumps(state))
        state["locations"] = [l for l in state["locations"] if l["id"] != loc_id]
        state["defaults"] = {c: BUILTIN if v == loc_id else v for c, v in state["defaults"].items()}
        save(state)
        try:
            write_compose(state)
        except Exception:
            save(previous)
            raise
        WAKE.set()
        return view()


def set_defaults(body: dict) -> dict:
    with LOCK:
        state = load()
        by_id = {l["id"]: l for l in state["locations"]}
        for category in CATEGORIES:
            if category in body:
                value = str(body[category] or BUILTIN)
                if value != BUILTIN and value not in by_id:
                    raise Refused("Unknown location")
                if value != BUILTIN and category not in by_id[value]["handles"]:
                    raise Refused(f"“{by_id[value]['label']}” isn't set to hold {HANDLE_NAMES[category]}: turn that on with Edit first")
                state["defaults"][category] = value
        save(state)
        return view()


def rename_location(loc_id: str, label: str) -> dict:
    with LOCK:
        state = load()
        loc = next((l for l in state["locations"] if l["id"] == loc_id), None)
        if not loc or not (label or "").strip():
            raise Refused("Give a name")
        loc["label"] = label.strip()[:60]
        save(state)
        return view()


def edit_location(loc_id: str, body: dict) -> dict:
    """Name, what it handles, and which new downloads go to it."""
    with LOCK:
        state = load()
        loc = next((l for l in state["locations"] if l["id"] == loc_id), None)
        if not loc:
            raise Refused("Unknown location")
        previous = json.loads(json.dumps(state))
        if "label" in body:
            label = str(body.get("label") or "").strip()
            if not label:
                raise Refused("Give a name")
            loc["label"] = label[:60]
        if "handles" in body:
            handles = _clean_handles(body["handles"])
            dropped = [h for h in loc["handles"] if h not in handles]
            if dropped:
                stored = [rel for rel, where in links(state).items() if where["location"] == loc_id and category_of(rel) in dropped]
                if stored:
                    names = sorted({HANDLE_NAMES[category_of(r)] for r in stored})
                    raise Refused(f"{len(stored)} file(s) ({', '.join(names)}) still live there: move them first "
                                  "(Models -> Manager -> Installed)")
                if "outputs" in dropped and (loc_id in (state["outputs"]["at"], state["outputs"]["to"])
                                             or any(_output_where(f, state)[0] == loc_id for f in output_folders())):
                    raise Refused("Service outputs are stored there: move them elsewhere first")
            loc["handles"] = handles
        downloads = body.get("downloads")
        if downloads is not None:
            if not isinstance(downloads, list):
                raise Refused("Bad downloads list")
            for category in CATEGORIES:
                if category in downloads:
                    if category not in loc["handles"]:
                        raise Refused(f"Turn on “{HANDLE_NAMES[category]}” to send those downloads here")
                    state["defaults"][category] = loc_id
                elif state["defaults"][category] == loc_id:
                    state["defaults"][category] = BUILTIN
        for category in CATEGORIES:
            if state["defaults"][category] == loc_id and category not in loc["handles"]:
                state["defaults"][category] = BUILTIN
        save(state)
        try:
            write_compose(state)
        except Exception:
            save(previous)
            raise
        _remember_known(load())
        WAKE.set()
        return view()


def _remember_known(state: dict) -> None:
    known = sorted(set(state["known"]) | set(_all_wanted(state)))
    if known != sorted(state["known"]):
        save({**state, "known": known})


def links(state: dict | None = None) -> dict[str, dict]:
    """rel path under models/ -> {location, target, online} for every symlinked model file."""
    state = state or load()

    def make() -> dict:
        out = {}
        for dirpath, dirnames, filenames in os.walk(MODELS):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                full = Path(dirpath) / name
                if not full.is_symlink() or not WEIGHT.search(name):
                    continue
                target = _real(full)
                out[str(full.relative_to(MODELS))] = {"location": location_of(target, state) or "", "target": str(target),
                                                      "online": target.exists()}
        return out
    return _cached("links", 8, make)


def view() -> dict:
    state = load()
    pending = _apply["pending"] if _apply["checked"] else pending_services(state)
    stored: dict[str, dict] = {}
    for rel, where in links(state).items():
        bucket = stored.setdefault(where["location"], {"count": 0, "bytes": 0, "offline": 0})
        bucket["count"] += 1
        if where["online"]:
            try:
                bucket["bytes"] += os.stat(where["target"]).st_size
            except OSError:
                pass
        else:
            bucket["offline"] += 1
    locations = []
    for loc in state["locations"]:
        info = disk(Path(loc["path"]))
        online = info.get("online", False) and Path(loc["path"]).is_dir()
        locations.append({**loc, "disk": info, "online": online, "writable": online and os.access(loc["path"], os.W_OK),
                          "ready": online and ready(loc, pending), "stored": stored.get(loc["id"], {"count": 0, "bytes": 0, "offline": 0})})
    other = [("Rocsy", ROOT), ("ComfyUI", COMFY_ROOT), ("ComfyUI outputs", COMFY_ROOT / "output"), ("Hermes agent", HERMES_HOME)]
    return {"builtin": {"id": BUILTIN, "label": "ComfyUI models folder", "path": str(MODELS), "disk": disk(MODELS),
                        "online": True, "writable": True, "ready": True, "handles": list(HANDLES)},
            "locations": locations, "defaults": state["defaults"],
            "apply": {"pending": pending, "running": _apply["running"], "error": _apply["error"]},
            "jobs": sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)[:30],
            "outputs": outputs_view(state),
            "other":[{"label": label, "path": str(p), "disk": disk(p)} for label, p in other if p.exists()]}


# ------------------------------------------------------------------ moves --

def _group(link: Path) -> list[Path]:
    """The file plus its other shards (model-00002-of-00004.gguf ...)."""
    files = [link]
    match = SHARD.search(link.name)
    if match:
        files += [link.with_name(link.name.replace("-00001-of-", f"-{n:05d}-of-")) for n in range(2, int(match.group(1)) + 1)]
    return [f for f in files if f.exists() or f.is_symlink()]


def _clean_rel(rel: str) -> Path:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\0" in rel or ".." in Path(rel).parts:
        raise Refused("Bad model path")
    link = MODELS / rel
    if not (link.is_file() or link.is_symlink()) or not WEIGHT.search(link.name):
        raise Refused("Not an installed model file")
    return link


def plan_move(rel: str, to: str) -> dict:
    state = load()
    link = _clean_rel(rel)
    if to != BUILTIN:
        loc = next((l for l in state["locations"] if l["id"] == to), None)
        if not loc:
            raise Refused("Unknown location")
        if category_of(rel) not in loc["handles"]:
            raise Refused(f"“{loc['label']}” isn't set to hold {HANDLE_NAMES[category_of(rel)]}: "
                          "change that with Edit in Settings -> Storage")
        if not Path(loc["path"]).is_dir():
            raise Refused(f"“{loc['label']}” isn't reachable (drive unplugged?)")
        pending = _apply["pending"] if _apply["checked"] else pending_services(state)
        if not ready(loc, pending):
            waiting = [p["name"] for p in pending]
            raise Refused(f"“{loc['label']}” is still being connected to {', '.join(waiting)} (they refresh once idle); try again then")
        base = Path(loc["path"])
    else:
        base = MODELS
    files, total = [], 0
    for path in _group(link):
        real = _real(path)
        where = location_of(real, state) if path.is_symlink() else BUILTIN
        if where is None:
            raise Refused(f"{path.name} is a link to {real}, which Rocsy doesn't manage; move it by hand")
        if not real.is_file():
            raise Refused(f"{path.name}: its file at {real} isn't reachable (drive unplugged?)")
        if where == to:
            continue
        st = real.stat()
        files.append({"link": str(path), "real": str(real), "from": where, "dest": str(base / path.relative_to(MODELS)),
                      "size": st.st_size, "was_link": path.is_symlink(), "nlink": st.st_nlink})
        total += files[-1]["size"]
    if not files:
        raise Refused("It's already there")
    dest_dir = Path(files[0]["dest"]).parent
    probe = next(p for p in [dest_dir, *dest_dir.parents] if p.exists())
    info = disk(probe)
    same = all(os.stat(f["real"]).st_dev == os.stat(probe).st_dev for f in files)
    if not same and info.get("free", 0) < total + 2 * 1024 ** 3:
        raise Refused(f"Not enough space there ({info.get('free', 0) / 1024 ** 3:.1f} GB free, needs {total / 1024 ** 3:.1f} GB + 2 GB)")
    if info.get("fstype") == "vfat" and any(f["size"] >= 4 * 1024 ** 3 for f in files):
        raise Refused("That drive is FAT32, which can't hold files over 4 GB; reformat it as exFAT, NTFS or ext4")
    return {"files": files, "total": total}


def start_move(rel: str, to: str) -> dict:
    with LOCK:
        if any(j["path"] == rel and j["state"] in ("queued", "copying", "verifying") for j in _jobs.values()):
            raise Refused("It's already being moved")
        plan = plan_move(rel, to)
        job = {"id": uuid.uuid4().hex[:12], "path": rel, "to": to, "from": plan["files"][0]["from"], "state": "queued",
               "total": plan["total"], "bytes": 0, "files": len(plan["files"]), "error": "", "note": "",
               "created": time.time(), "finished": 0, "cancel": False}
        _jobs[job["id"]] = job
        _queue.append(job["id"])
        if len(_jobs) > 200:
            for old in sorted(_jobs.values(), key=lambda j: j["created"])[:50]:
                if old["state"] not in ("queued", "copying", "verifying"):
                    _jobs.pop(old["id"], None)
        threading.Thread(target=_drain, daemon=True).start()
        return _public(job)


def cancel_move(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        raise Refused("Unknown move")
    if job["state"] == "queued":
        job["state"] = "cancelled"
    elif job["state"] in ("copying", "verifying"):
        job["cancel"] = True
    return _public(job)


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "cancel"}


_drain_lock = threading.Lock()


def _drain() -> None:
    if not _drain_lock.acquire(blocking=False):
        return
    try:
        while _queue:
            job = _jobs.get(_queue.pop(0))
            if not job or job["state"] != "queued":
                continue
            try:
                _run(job)
                job.update(state="done", finished=time.time())
            except _Cancelled:
                job.update(state="cancelled", finished=time.time())
            except Exception as exc:  # noqa: BLE001 - shown on the job
                job.update(state="failed", error=str(exc)[:400] or type(exc).__name__, finished=time.time())
            _cache.pop("links", None)
    finally:
        _drain_lock.release()


class _Cancelled(Exception):
    pass


def _identity(path: str) -> tuple:
    st = os.stat(path)
    return st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns


def _copy(job: dict, src: str, stage: Path) -> None:
    """Copy src to stage, then read the copy back from disk and compare SHA-256."""
    before = _identity(src)
    want, got = hashlib.sha256(), hashlib.sha256()
    job["state"] = "copying"
    with open(src, "rb") as fin, open(stage, "wb") as fout:
        while chunk := fin.read(CHUNK):
            if job["cancel"]:
                raise _Cancelled
            fout.write(chunk)
            want.update(chunk)
            job["bytes"] += len(chunk)
        fout.flush()
        os.fsync(fout.fileno())
        os.posix_fadvise(fout.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)    # verify what's on disk, not page cache
    if _identity(src) != before:
        raise RuntimeError(f"{Path(src).name} changed while it was copied; nothing was moved")
    job["state"] = "verifying"
    with open(stage, "rb") as fin:
        while chunk := fin.read(CHUNK):
            if job["cancel"]:
                raise _Cancelled
            got.update(chunk)
    if got.hexdigest() != want.hexdigest():
        raise RuntimeError(f"The copy of {Path(src).name} doesn't match the original (SHA-256); nothing was moved")
    shutil.copystat(src, stage)      # same mtime: the Models hub's hash cache stays valid


def _run(job: dict) -> None:
    plan = plan_move(job["path"], job["to"])     # re-checked: things may have changed while it waited
    job["total"] = plan["total"]
    for f in plan["files"]:
        link, real, dest = Path(f["link"]), Path(f["real"]), Path(f["dest"])
        going_home = dest == link
        if not going_home and (dest.exists() or dest.is_symlink()):
            dest = dest.with_name(f"{dest.stem}.{job['id'][:6]}{dest.suffix}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        stage = dest.with_name(f".{dest.name}.rocsy-move")
        stage.unlink(missing_ok=True)
        try:
            try:
                os.link(real, stage)               # same filesystem: instant, no second copy on disk
                job["bytes"] += f["size"]
                job["note"] = "Same drive: moved without copying"
            except OSError as exc:
                if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP, errno.EOPNOTSUPP):
                    raise
                _copy(job, str(real), stage)
            if job["cancel"]:
                raise _Cancelled
            if going_home:
                os.replace(stage, link)            # the link becomes the file again
            else:
                os.rename(stage, dest)
                tmp_link = link.with_name(f".{link.name}.rocsy-link")
                tmp_link.unlink(missing_ok=True)
                os.symlink(dest, tmp_link)
                os.replace(tmp_link, link)         # the file (or old link) becomes a link, atomically
        except BaseException:
            stage.unlink(missing_ok=True)
            raise
        if f["was_link"]:                          # the file lived in another location: free that copy
            real.unlink()
            _prune_empty(real.parent, f["from"])
        # (a plain file in the models folder was replaced by the link above, which freed it)
        if f["nlink"] > 1 and not job["note"]:
            job["note"] = "The old file had another hard link elsewhere, so its space wasn't freed"
    job["bytes"] = job["total"]


def _prune_empty(folder: Path, loc_id: str) -> None:
    """Remove folders a move left empty inside a location (never the location itself)."""
    if loc_id == BUILTIN:
        return
    loc = next((l for l in load()["locations"] if l["id"] == loc_id), None)
    if not loc:
        return
    top = Path(loc["path"])
    while folder != top and _inside(folder, top):
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def default_for(dest: str) -> dict:
    """Where a new download into models/<dest> should go: {"id", "path"} ("" path = models folder)."""
    state = load()
    category = category_of(dest)
    loc_id = state["defaults"][category]
    loc = next((l for l in state["locations"] if l["id"] == loc_id), None)
    return {"id": loc_id, "path": loc["path"] if loc else ""}


# -------------------------------------------------------- service outputs --
# One move at a time takes every output folder to one place: copy (read back + SHA-256, like model moves) while
# Rocsy keeps working, then wait until nothing renders, copy what changed meanwhile, swap each folder for a link
# (or the link back for the folder), recreate the containers that bind them, and only then free the old copies.

OUT_RUNNING = ("queued", "connecting", "copying", "verifying", "waiting", "switching")
PART = ".rocsy-part"
_out_job: dict = {}


def _output_where(folder: dict, state: dict) -> tuple[str, Path | None]:
    """(location id | BUILTIN | "" = a link Rocsy doesn't manage | "missing", the real folder)."""
    path = folder["path"]
    if path.is_symlink():
        real = _real(path)
        for loc in state["locations"]:
            if _inside(real, output_base(loc)):
                return loc["id"], real
        return "", real
    if path.is_dir():
        return BUILTIN, path
    return "missing", None


def _du(path: Path) -> int | None:
    def make() -> int | None:
        result = subprocess.run(["du", "-sb", "--", str(path)], capture_output=True, text=True, timeout=300)
        try:
            return int(result.stdout.split()[0])
        except (ValueError, IndexError):
            return None
    return _cached(f"du:{path}", 120, make)


def _output_state(state: dict, sizes: bool = False) -> list[dict]:
    out = []
    for folder in output_folders():
        where, real = _output_where(folder, state)
        online = bool(real and real.is_dir())
        item = {**folder, "where": where, "real": real, "online": online}
        if sizes:
            item["bytes"] = _du(real) if online else None
        out.append(item)
    return out


def outputs_view(state: dict) -> dict:
    folders = [{"key": f["key"], "label": f["label"], "note": f["note"], "path": str(f["path"]), "where": f["where"],
                "real": str(f["real"] or ""), "online": f["online"], "bytes": f.get("bytes")}
               for f in _output_state(state, sizes=True)]
    job = {k: v for k, v in _out_job.items() if k not in ("cancel", "items")} if _out_job else None
    return {"at": state["outputs"]["at"], "to": state["outputs"]["to"], "folders": folders, "job": job,
            "writers": [NAMES[s] for s in OUTPUT_WRITERS]}


def _sleep(job: dict, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if job["cancel"]:
            raise _Cancelled
        time.sleep(0.5)


def _sync_tree(job: dict, src: Path, dst: Path, tolerant: bool) -> None:
    """Make dst a copy of src (files verified by SHA-256; unchanged ones skipped by size + mtime).
    tolerant: a file being written or deleted while Rocsy works is skipped (the final pass gets it)."""
    dst.mkdir(parents=True, exist_ok=True)
    seen: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(src):
        here = Path(dirpath)
        rel = here.relative_to(src)
        (dst / rel).mkdir(exist_ok=True)
        links = [d for d in dirnames if (here / d).is_symlink()]
        dirnames[:] = [d for d in dirnames if d not in links]
        seen.update(rel / d for d in dirnames)
        for name in filenames + links:
            s, d = here / name, dst / rel / name
            seen.add(rel / name)
            if job["cancel"]:
                raise _Cancelled
            if name.endswith(PART):
                continue
            if s.is_symlink():
                target = os.readlink(s)
                if d.is_symlink() and os.readlink(d) == target:
                    continue
                if d.is_dir() and not d.is_symlink():
                    shutil.rmtree(d)
                else:
                    d.unlink(missing_ok=True)
                os.symlink(target, d)
                continue
            try:
                st = s.stat()
            except FileNotFoundError:
                continue
            if not s.is_file():
                continue
            try:
                dt = d.lstat()
                if not d.is_symlink() and d.is_file() and dt.st_size == st.st_size and dt.st_mtime_ns == st.st_mtime_ns:
                    continue
            except FileNotFoundError:
                pass
            part = d.with_name(d.name + PART)
            try:
                _copy(job, str(s), part)
                if d.is_dir() and not d.is_symlink():
                    shutil.rmtree(d)
                os.replace(part, d)
            except _Cancelled:
                part.unlink(missing_ok=True)
                raise
            except (RuntimeError, FileNotFoundError):
                part.unlink(missing_ok=True)
                if not tolerant:
                    raise
    for dirpath, dirnames, filenames in os.walk(dst, topdown=False):
        here = Path(dirpath)
        rel = here.relative_to(dst)
        for name in filenames + dirnames:
            if rel / name in seen:
                continue
            gone = here / name
            if gone.is_dir() and not gone.is_symlink():
                shutil.rmtree(gone, ignore_errors=True)
            else:
                gone.unlink(missing_ok=True)


def _port_open(port: int) -> bool:
    import socket
    try:
        socket.create_connection(("127.0.0.1", port), 2).close()
        return True
    except OSError:
        return False


def _outputs_busy() -> str:
    """Why outputs can't be switched right now ("" = everything that writes them is idle)."""
    if busy_check is None:
        return "busy checks aren't ready"
    for kind in OUTPUT_BUSY:
        container = {**OUTPUT_BUSY_CONTAINER, "queue": "bunny-lock"}.get(kind)
        if container and not _running(container):
            continue
        try:
            busy, detail = busy_check(kind)
        except Exception as exc:  # noqa: BLE001 - unreadable = busy (fail closed), unless it isn't running at all
            if kind == "comfy" and not _port_open(8188):
                continue
            return f"couldn't check {kind} ({type(exc).__name__})"
        if busy:
            return detail
    return ""


def _recreate(services: list[str]) -> str:
    """Force-recreate these compose services so their binds follow the new links. Returns errors ("" = ok)."""
    errors = []
    present = [s for s in services if live_mounts(SERVICE_CONTAINERS[s]) is not None]
    running = [s for s in present if _running(SERVICE_CONTAINERS[s])]
    stopped = [s for s in present if s not in running]
    for group, mode in ((running, "-d"), (stopped, "--no-start")):
        if not group:
            continue
        result = subprocess.run(["docker", "compose", "up", "--no-deps", "--force-recreate", mode, *group],
                                cwd=COMPOSE_DIR, capture_output=True, text=True, timeout=600)
        if result.returncode:
            errors.append((result.stderr or result.stdout).strip()[-300:])
    return "; ".join(errors)


def start_outputs_move(to: str) -> dict:
    with LOCK:
        if _out_job.get("state") in OUT_RUNNING:
            raise Refused("Service outputs are already being moved")
        state = load()
        label = "Rocsy's own folders"
        if to != BUILTIN:
            loc = next((l for l in state["locations"] if l["id"] == to), None)
            if not loc:
                raise Refused("Unknown location")
            if "outputs" not in loc["handles"]:
                raise Refused(f"“{loc['label']}” isn't set to hold service outputs: turn that on with Edit first")
            if not Path(loc["path"]).is_dir():
                raise Refused(f"“{loc['label']}” isn't reachable (drive unplugged?)")
            label = loc["label"]
        folders = _output_state(state)
        if any(f["where"] == "" for f in folders):
            bad = next(f for f in folders if f["where"] == "")
            raise Refused(f"{bad['path']} is a link to {bad['real']}, which Rocsy doesn't manage; move it back by hand first")
        offline = [f for f in folders if f["where"] not in (BUILTIN, "missing", to) and not f["online"]]
        if offline:
            raise Refused(f"{offline[0]['label']} is on a drive that isn't reachable (unplugged?)")
        todo = [f for f in folders if f["where"] not in (to, "missing")]
        if not todo:
            if state["outputs"]["at"] != to:
                save({**state, "outputs": {"at": to, "to": ""}})
            raise Refused("They're already there")
        if to != BUILTIN:
            state["outputs"]["to"] = to
            save(state)
            write_compose(state)
            WAKE.set()
        _out_job.clear()
        _out_job.update(id=uuid.uuid4().hex[:12], to=to, label=label, state="queued", total=0, bytes=0, waiting="",
                        error="", note="", created=time.time(), finished=0, cancel=False,
                        folders=[f["label"] for f in todo])
        threading.Thread(target=_outputs_thread, args=(_out_job,), daemon=True).start()
        return view()


def cancel_outputs_move() -> dict:
    if _out_job.get("state") not in OUT_RUNNING:
        raise Refused("No move is running")
    if _out_job["state"] == "switching":
        raise Refused("It's switching over right now; that takes a few seconds and can't be stopped")
    _out_job["cancel"] = True
    return view()


def _outputs_thread(job: dict) -> None:
    items: list[dict] = []
    try:
        _outputs_run(job, items)
        job.update(state="done", finished=time.time())
    except _Cancelled:
        job.update(state="cancelled", finished=time.time())
    except Exception as exc:  # noqa: BLE001 - shown on the job
        job.update(state="failed", error=str(exc)[:400] or type(exc).__name__, finished=time.time())
    finally:
        if job["state"] != "done":
            for item in items:
                if item.get("stage") and not item.get("switched"):
                    shutil.rmtree(item["stage"], ignore_errors=True)
        with LOCK:
            state = load()
            folders = _output_state(state)
            wheres = {f["where"] for f in folders if f["where"] != "missing"}
            at = job["to"] if job["state"] == "done" else (wheres.pop() if len(wheres) == 1 else state["outputs"]["at"])
            state["outputs"] = {"at": at if at in {l["id"] for l in state["locations"]} | {BUILTIN} else BUILTIN, "to": ""}
            save(state)
            try:
                write_compose(state)
            except Exception as exc:  # noqa: BLE001
                job["note"] = (job["note"] + " · " if job["note"] else "") + f"compose: {str(exc)[:200]}"
            WAKE.set()


def _outputs_run(job: dict, items: list[dict]) -> None:
    to = job["to"]
    state = load()
    base = None
    if to != BUILTIN:
        loc = next(l for l in state["locations"] if l["id"] == to)
        base = output_base(loc)
        base.mkdir(parents=True, exist_ok=True)
        # 1. The apps that write outputs must see the drive before anything points there.
        job["state"] = "connecting"
        while True:
            need = [p["name"] for p in pending_services(load())
                    if p["service"] in OUTPUT_WRITERS and any(_inside(base, Path(m)) for m in p["missing"])]
            if not need:
                break
            job["waiting"] = f"Connecting the drive to {', '.join(need)} (each refreshes once idle)"
            WAKE.set()
            _sleep(job, 5)
        job["waiting"] = ""
    # 2. Plan + copy while Rocsy keeps working.
    for folder in _output_state(load()):
        if folder["where"] in (to, "missing"):
            continue
        src = folder["real"]
        if base is not None:
            dest = base / folder["key"]
            if dest.is_symlink() or (dest.exists() and any(dest.iterdir())):
                dest = base / f"{folder['key']}-{time.strftime('%Y%m%d-%H%M%S')}"
            elif dest.exists():
                dest.rmdir()
        else:
            dest = folder["path"]
        stage = dest.with_name(f".{dest.name}.rocsy-move")
        same = os.stat(src).st_dev == os.stat(dest.parent).st_dev
        items.append({**folder, "src": src, "dest": dest, "stage": None if same else stage, "same": same})
    copy = [i for i in items if not i["same"]]
    job["total"] = sum(_du(i["src"]) or 0 for i in copy)
    if copy:
        info = disk(copy[0]["dest"].parent)
        if info.get("free", 0) < job["total"] + 2 * 1024 ** 3:
            raise RuntimeError(f"Not enough space there ({info.get('free', 0) / 1024 ** 3:.1f} GB free, "
                               f"needs {job['total'] / 1024 ** 3:.1f} GB + 2 GB)")
        if info.get("fstype") == "vfat":
            raise RuntimeError("That drive is FAT32 (no links, 4 GB file limit); reformat it as ext4, btrfs, exFAT or NTFS")
    for item in copy:
        job["waiting"] = f"Copying {item['label']}"
        _sync_tree(job, item["src"], item["stage"], tolerant=True)
    # 3. Wait until nothing renders, trains or speaks.
    job["state"] = "waiting"
    while reason := _outputs_busy():
        job["waiting"] = f"Waiting until Rocsy is idle: {reason}"
        _sleep(job, 10)
    # 4. Switch: last changes, swap every folder, recreate the containers that bind them, free the old copies.
    job.update(state="switching", waiting="Switching over")
    with LOCK:
        for item in copy:
            _sync_tree(job, item["src"], item["stage"], tolerant=False)
        job["state"] = "switching"         # _copy marks copying/verifying; from here on nothing can cancel
        for item in items:
            path, src, dest = item["path"], item["src"], item["dest"]
            moved = src if item["same"] else item["stage"]
            if base is not None:
                os.rename(moved, dest)
                tmp = path.with_name(f".{path.name}.rocsy-link")
                tmp.unlink(missing_ok=True)
                os.symlink(dest, tmp)
                if path.is_symlink() or not path.exists():
                    os.replace(tmp, path)                 # atomic: old link (or nothing) -> new link
                else:
                    old = path.with_name(f".{path.name}.rocsy-old")
                    os.rename(path, old)
                    os.replace(tmp, path)
                    item["old"] = old
            else:
                link = path.with_name(f".{path.name}.rocsy-oldlink")
                link.unlink(missing_ok=True)
                os.rename(path, link)
                os.rename(moved, path)
                link.unlink()
            if not item["same"] and item["where"] != BUILTIN:
                item["old"] = src                          # the copy on the location it left
            item["switched"] = True
        _cache.clear()
    services = sorted({s for item in items for s in item["binds"]})
    job["waiting"] = f"Refreshing {', '.join(NAMES.get(s, s) for s in services)}" if services else ""
    errors = _recreate(services) if services else ""
    if errors:
        job["note"] = f"Couldn't refresh every app ({errors}); the old copies were kept. Restart Rocsy, then delete them."
        return
    for item in items:
        old = item.get("old")
        if old:
            shutil.rmtree(old, ignore_errors=True)
            if item["where"] not in (BUILTIN, ""):
                prev = next((l for l in load()["locations"] if l["id"] == item["where"]), None)
                if prev:
                    try:
                        output_base(prev).rmdir()
                    except OSError:
                        pass
    job["waiting"] = ""


# ---------------------------------------------------------------- routes --

def handle(method: str, path: str, body: dict) -> tuple[int, dict]:
    from urllib.parse import parse_qs, urlsplit
    url = urlsplit(path)
    query = parse_qs(url.query)
    route = url.path
    try:
        if method == "GET" and route == "/storage":
            return 200, view()
        if method == "GET" and route == "/storage/links":
            state = load()
            return 200, {"links": links(state), "defaults": state["defaults"],
                         "locations": [{"id": l["id"], "label": l["label"], "path": l["path"], "handles": l["handles"]}
                                       for l in state["locations"]],
                         "jobs": [_public(j) for j in sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)[:30]]}
        if method == "GET" and route == "/storage/browse":
            return 200, browse((query.get("path") or [None])[0], (query.get("hidden") or ["0"])[0] == "1")
        if method == "POST" and route == "/storage/locations/add":
            return 200, add_location(str(body.get("path") or ""), str(body.get("label") or ""), body.get("subfolder", True) is not False,
                                     body.get("handles"), body.get("downloads"))
        if method == "POST" and route == "/storage/locations/edit":
            return 200, edit_location(str(body.get("id") or ""), body)
        if method == "POST" and route == "/storage/locations/remove":
            return 200, remove_location(str(body.get("id") or ""))
        if method == "POST" and route == "/storage/locations/rename":
            return 200, rename_location(str(body.get("id") or ""), str(body.get("label") or ""))
        if method == "POST" and route == "/storage/defaults":
            return 200, set_defaults(body)
        if method == "POST" and route == "/storage/apply":
            _apply["force_llama"] = bool(body.get("force_llama"))
            WAKE.set()
            return 200, {"ok": True}
        if method == "POST" and route == "/storage/move":
            return 200, start_move(str(body.get("path") or ""), str(body.get("to") or ""))
        if method == "POST" and route == "/storage/outputs/move":
            return 200, start_outputs_move(str(body.get("to") or ""))
        if method == "POST" and route == "/storage/outputs/cancel":
            return 200, cancel_outputs_move()
        if method == "POST" and route == "/storage/move/cancel":
            return 200, cancel_move(str(body.get("id") or ""))
        return 404, {"error": "not found"}
    except Refused as exc:
        return 409, {"error": str(exc)}
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        return 500, {"error": str(exc)[:300]}


def start_background() -> None:
    threading.Thread(target=apply_loop, daemon=True).start()
    WAKE.set()
