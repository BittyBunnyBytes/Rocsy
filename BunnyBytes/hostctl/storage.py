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
MAX_LOCATIONS = 12
CHUNK = 16 * 1024 * 1024
# Compose service -> (container, hostctl busy kind, writable). Everything that reads model files.
CONSUMERS: dict[str, tuple[str, str, bool]] = {
    "modelhub": ("bunny-models", "models", True),   # downloads into locations, uninstall removes there
    "h3-machine": ("h3-machine", "vid", False),
    "krea": ("krea", "krea", False),
    "music": ("music", "mus", False),
    "trainer": ("bunny-trainer", "trainer", False),
    "llama": ("llama", "llama", False),
}
NAMES = {"modelhub": "Models", "h3-machine": "Video", "krea": "Image", "music": "Music", "trainer": "Trainer", "llama": "Llama"}
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
    defaults = {c: str((data.get("defaults") or {}).get(c) or BUILTIN) for c in CATEGORIES}
    ids = {l["id"] for l in locations}
    defaults = {c: v if v == BUILTIN or v in ids else BUILTIN for c, v in defaults.items()}
    # Same-path mounts this module wrote at some point: the ones that may still need removing.
    known = [str(k) for k in data.get("known") or [] if isinstance(k, str)]
    return {"locations": locations, "defaults": defaults, "known": known}


def save(state: dict) -> None:
    tmp = STATE.with_name(STATE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, STATE)


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


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
        src = mount_source(Path(loc["path"]))
        if src not in sources:
            sources.append(src)
    # A folder already inside another mounted folder needs no mount of its own.
    return [s for s in sources if not any(o != s and _inside(s, o) for o in sources)]


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


def render_override(text: str, mounts: list[Path], existing: dict[str, set[str]]) -> str:
    """docker-compose.override.yml with our blocks for these mounts (and none when the list is empty)."""
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
    for service, (_container, _busy, writable) in CONSUMERS.items():
        have = existing.get(service, {"same": set(), "all": set()})
        entries = [m for m in mounts if not _covered(str(m), have["same"]) and str(m) not in have["all"]]
        if not entries:
            continue
        block = [BEGIN]
        for m in entries:
            block += ["      - type: bind\n", f'        source: "{m}"\n', f'        target: "{m}"\n']
            if not writable:
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
        new = render_override(text, wanted_mounts(state), existing)
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
        return {m.get("Destination", ""): m.get("Source", "") for m in json.loads(result.stdout) or []}
    except ValueError:
        return None


def _running(container: str) -> bool:
    result = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", container],
                            capture_output=True, text=True, timeout=15)
    return result.stdout.strip() == "true"


def pending_services(state: dict) -> list[dict]:
    """Consumers whose container doesn't (yet) have exactly the mounts it should."""
    wanted = {str(m) for m in wanted_mounts(state)}
    out = []
    for service, (container, _busy, _w) in CONSUMERS.items():
        live = live_mounts(container)
        if live is None:
            continue    # not installed here (e.g. no Trainer): nothing to refresh
        same = {t for t, s in live.items() if t == s}
        missing = sorted(m for m in wanted if not _covered(m, same))
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
        wanted = [str(m) for m in wanted_mounts(state)]
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


def add_location(path: str, label: str = "", subfolder: bool = True) -> dict:
    with LOCK:
        state = load()
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
        loc = {"id": _slug(label, {l["id"] for l in state["locations"]}), "label": label, "path": str(real), "added": time.time()}
        previous = json.loads(json.dumps(state))
        state["locations"].append(loc)
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
        ids = {l["id"] for l in state["locations"]} | {BUILTIN}
        for category in CATEGORIES:
            if category in body:
                value = str(body[category] or BUILTIN)
                if value not in ids:
                    raise Refused("Unknown location")
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


def _remember_known(state: dict) -> None:
    known = sorted(set(state["known"]) | {str(m) for m in wanted_mounts(state)})
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
                        "online": True, "writable": True, "ready": True},
            "locations": locations, "defaults": state["defaults"],
            "apply": {"pending": pending, "running": _apply["running"], "error": _apply["error"]},
            "jobs": sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)[:30],
            "other": [{"label": label, "path": str(p), "disk": disk(p)} for label, p in other if p.exists()]}


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
    top = dest.split("/", 1)[0]
    category = "loras" if top == "loras" else "llm" if top == "llm" else "models"
    loc_id = state["defaults"][category]
    loc = next((l for l in state["locations"] if l["id"] == loc_id), None)
    return {"id": loc_id, "path": loc["path"] if loc else ""}


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
                         "locations": [{"id": l["id"], "label": l["label"], "path": l["path"]} for l in state["locations"]],
                         "jobs": [_public(j) for j in sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)[:30]]}
        if method == "GET" and route == "/storage/browse":
            return 200, browse((query.get("path") or [None])[0], (query.get("hidden") or ["0"])[0] == "1")
        if method == "POST" and route == "/storage/locations/add":
            return 200, add_location(str(body.get("path") or ""), str(body.get("label") or ""), body.get("subfolder", True) is not False)
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
