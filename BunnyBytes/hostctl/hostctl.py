#!/usr/bin/env python3
"""bunny-hostctl: lets the BunnyBytes Settings page (Settings -> Services)
see and control what runs on this host next to the web apps: the user
systemd supervisors, the shared ComfyUI (through its supervisor) and the
Docker containers, including whether each starts at boot.

Runs as a systemd USER service (it needs `systemctl --user` and the docker
CLI), standard library only, on 127.0.0.1. Every request must carry
X-Hostctl-Secret (HOSTCTL_SECRET from BunnyBytes/.env); only the gateway
knows it. Nothing from a request is ever passed to a command: an item id
picks an entry from ITEMS below and the action must be one that entry allows.

Safety:
- stop/restart of an app with work in flight (renders, training, queued GPU
  jobs, ComfyUI queue) is refused; if its busy state can't be read, the
  caller must confirm with force=true;
- the gateway, the tunnel and this helper are locked (the page runs on them);
- the old standalone comfyui.service can't be started (a second ComfyUI on
  the shared GPU crashed the desktop);
- container boot choices are stored here and re-applied every minute, since
  `docker compose up` resets a container's restart policy."""
from __future__ import annotations

import calendar
from concurrent.futures import ThreadPoolExecutor
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import yaml
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from paths import HERMES_HOME
from typing import Any

import corebackend
import coreengines
import voiceengines
import llama_benchmark
import llama_hipblaslt
import coreextras
import reafolders
import reamods
import comfynodes
import modelfind
import storage
import providerchat
import coreruntime
import windowshost
import coreswap
import hermeslink
import llama_optimize
import loadpriority
import gputune
import codingclis
import uninstall

HOST = "127.0.0.1"
PORT = int(os.getenv("HOSTCTL_PORT", "8899"))
SECRET = os.getenv("HOSTCTL_SECRET", "")
STATE = Path(os.getenv("HOSTCTL_STATE", Path(__file__).resolve().parent / "state.json"))
SUPERVISOR = "http://127.0.0.1:8083"
COMFY = "http://127.0.0.1:8188"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hostctl")

ALL_ACTIONS = ("start", "stop", "restart", "boot-on", "boot-off")

# id -> spec. kind: unit (systemd --user), comfyui (via its supervisor), container.
ITEMS: dict[str, dict[str, Any]] = {
    "comfyui": {"kind": "comfyui", "group": "Host", "name": "ComfyUI",
                "note": "Shared image/video/music engine on port 8188, run by the ComfyUI supervisor", "busy": "comfy"},
    "comfyui-supervisor": {"kind": "unit", "unit": "comfyui-supervisor.service", "group": "Host", "name": "ComfyUI supervisor",
                           "note": "Starts ComfyUI on demand and switches its profile; ComfyUI keeps running if it stops"},
    "hostctl": {"kind": "unit", "unit": "bunny-hostctl.service", "group": "Host", "name": "Services helper",
                "note": "This page's link to the host (bunny-hostctl)", "locked": "all"},
    "video": {"kind": "container", "group": "Containers", "name": "Video", "note": "MiniMax H3, LTX 2.5 and Wan 2.2", "busy": "vid", "stop_timeout": 40},
    "krea": {"kind": "container", "group": "Containers", "name": "Image", "note": "Krea 2, MOSS-Image and Qwen Image", "busy": "krea"},
    "music": {"kind": "container", "group": "Containers", "name": "Music", "note": "Song generation (Mus)", "busy": "mus"},
    "bunny-trainer": {"kind": "container", "group": "Containers", "name": "Trainer", "note": "LoRA training", "busy": "trainer"},
    "bunny-agent": {"kind": "container", "group": "Containers", "name": "Agent", "note": "Master Agent (agentic mode)", "busy": "agent"},
    "bunny-lock": {"kind": "container", "group": "Containers", "name": "Queue", "note": "Shared GPU queue coordinator", "busy": "queue"},
    "bunny-motion": {"kind": "container", "group": "Containers", "name": "Motion", "note": "Code-based video renderer (Remotion) for the Master Agent", "busy": "motion"},
    "bunny-rea": {"kind": "container", "group": "Containers", "name": "REA", "note": "Reverse Engineer Anything: MCP tools (Ghidra, JADX, Rizin...) for Hermes", "busy": "rea"},
    "bunny-voice": {"kind": "container", "group": "Containers", "name": "Voice", "note": "Text to speech with saved voices", "busy": "voice"},
    "bunny-models": {"kind": "container", "group": "Containers", "name": "Models", "note": "Model / LoRA downloads and updates", "busy": "models"},
    "llama": {"kind": "container", "group": "Containers", "name": "Llama", "note": "llama-swap LLM server"},
    "hermes-agent": {"kind": "container", "group": "Containers", "name": "Hermes agent", "note": "Hermes gateway / agent runtime"},
    "hermes-dashboard": {"kind": "container", "group": "Containers", "name": "Hermes dashboard", "note": "Hermes web UI"},
    "bunny-connections": {"kind": "container", "group": "Containers", "name": "Connections",
                          "note": "Telegram output bot and the Cloudflare tunnel (public access)",
                          "locked": {"stop": "It runs the Cloudflare tunnel - the public site would go offline",
                                     "boot-off": "It runs the Cloudflare tunnel - the site would stay offline after a reboot"}},
    "bunny-framework": {"kind": "container", "group": "Containers", "name": "Framework", "note": "Shared Agentic framework for the Image, Music and Voice studios"},
    "bunny-gateway": {"kind": "container", "group": "Containers", "name": "Gateway", "note": "The Rocsy website itself",
                      "locked": "all"},
}
LOCK_ALL_REASON = "This page runs through it - manage it from a terminal"


# ---------------------------------------------------------------- helpers --

def run(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


def http_json(url: str, method: str = "GET", body: dict[str, Any] | None = None, timeout: float = 5) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(), strict=False)  # prompts can hold raw control chars


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(data: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=STATE.parent, prefix=".state-")
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, STATE)


def locked_reason(spec: dict[str, Any], action: str) -> str:
    locked = spec.get("locked")
    if locked == "all":
        return LOCK_ALL_REASON
    return (locked or {}).get(action, "")


# ------------------------------------------------------------ busy checks --
# Each returns (busy, detail). Raises when the state can't be read.

def _jobs(url: str) -> list[dict[str, Any]]:
    data = http_json(url)
    data = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(data, list):
        raise ValueError("unexpected job list")
    return data


def busy_check(kind: str) -> tuple[bool, str]:
    if kind == "vid":
        live = {"waiting_for_gpu", "preparing", "queued", "rendering", "finalizing", "cancelling"}
        jobs = [job for job in _jobs("http://127.0.0.1:8000/Video/api/jobs") if job.get("state") in live]
        agentic = [job for job in http_json("http://127.0.0.1:8000/Video/api/enhancement/jobs").get("jobs", [])
                   if job.get("agentic") and job.get("state") in {"waiting_for_gpu", "running", "queuing", "rendering",
                                                                   "reviewing", "awaiting_approval", "preparing"}]
        parts = [f"{len(jobs)} render(s)"] * bool(jobs) + [f"{len(agentic)} Agentic session(s)"] * bool(agentic)
        return bool(parts), " and ".join(parts)
    if kind in ("krea", "mus"):
        port, prefix = {"krea": (8300, "Image"), "mus": (8200, "Music")}[kind]
        jobs = [job for job in _jobs(f"http://127.0.0.1:{port}/{prefix}/api/jobs")
                if job.get("state") not in {"complete", "failed", "cancelled"}]
        return bool(jobs), f"{len(jobs)} job(s) in progress"
    if kind == "trainer":
        live = {"waiting_for_gpu", "preparing", "running", "queued"}
        jobs = [job for job in _jobs("http://127.0.0.1:8500/Trainer/api/jobs") if job.get("state") in live]
        tags = [job for job in _jobs("http://127.0.0.1:8500/Trainer/api/tag-jobs") if job.get("state") in live]
        return bool(jobs or tags), f"{len(jobs)} training run(s), {len(tags)} tagging job(s)"
    if kind == "agent":
        sessions = [s for s in http_json("http://127.0.0.1:8900/Agent/api/sessions").get("sessions", []) if s.get("busy")]
        return bool(sessions), f"{len(sessions)} Master Agent session(s) working"
    if kind == "queue":
        entries = http_json("http://127.0.0.1:8600/Queue/api/queue").get("entries", [])
        return bool(entries), f"{len(entries)} GPU queue entr{'y' if len(entries) == 1 else 'ies'}"
    if kind == "models":
        active = int(http_json("http://127.0.0.1:8960/Models/api/state").get("active") or 0)
        return bool(active), f"{active} download(s) in progress"
    if kind == "motion":
        jobs = [job for job in http_json("http://127.0.0.1:8970/api/jobs").get("jobs", [])
                if job.get("state") not in {"complete", "failed", "cancelled"}]
        return bool(jobs), f"{len(jobs)} Motion render(s) in progress"
    if kind == "voice":
        jobs = [job for job in http_json("http://127.0.0.1:8950/Voice/api/state").get("jobs", [])
                if job.get("state") in {"queued", "running"}]
        return bool(jobs), f"{len(jobs)} Voice job(s) in progress"
    if kind == "rea":
        busy = coreextras.rea_busy()
        return bool(busy), f"REA is analysing ({', '.join(busy)})"
    if kind == "comfy":
        queue = http_json(f"{COMFY}/queue")
        count = len(queue.get("queue_running") or []) + len(queue.get("queue_pending") or [])
        return bool(count), f"{count} ComfyUI job(s) running or queued"
    return False, ""


# ----------------------------------------------------------------- status --

def unit_status(spec: dict[str, Any]) -> dict[str, Any]:
    shown = run("systemctl", "--user", "show", spec["unit"], "--no-pager",
                "-p", "ActiveState,SubState,UnitFileState,LoadState,ActiveEnterTimestampMonotonic,MainPID", timeout=10)
    props = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
    if shown.returncode != 0:
        raise RuntimeError((shown.stderr or shown.stdout).strip()[:200] or "systemctl show failed")
    if "LoadState" not in props:
        raise RuntimeError("systemctl show returned no unit status")
    if props.get("LoadState") == "not-found":
        return {"exists": False, "running": False, "state": "not installed", "boot": False}
    since = None
    entered = int(props.get("ActiveEnterTimestampMonotonic") or 0)
    if props.get("ActiveState") == "active" and entered:
        since = time.time() - (time.monotonic() - entered / 1e6)
    return {"exists": True, "running": props.get("ActiveState") == "active",
            "state": f"{props.get('ActiveState', '?')} ({props.get('SubState', '?')})",
            "boot": props.get("UnitFileState") in {"enabled", "enabled-runtime", "linked"},
            "since": since}


def comfy_status() -> dict[str, Any]:
    try:
        status = http_json(f"{SUPERVISOR}/status", timeout=4)
    except Exception:  # noqa: BLE001
        return {"exists": True, "running": False, "state": "supervisor unreachable", "boot": None}
    running = bool(status.get("pid")) and bool(status.get("reachable"))
    detail = f"profile {status.get('profile') or '?'} · GPU {status.get('gpus') or '?'}" if status.get("pid") else "not running"
    if status.get("pid") and not status.get("reachable"):
        detail = "starting or busy"
    return {"exists": True, "running": running, "state": detail, "boot": None}


def _container_status(state_json: str, policy: str) -> dict[str, Any]:
    state = json.loads(state_json)
    since = None
    if state.get("Running") and state.get("StartedAt"):
        stamp = re.sub(r"\.\d+", "", state["StartedAt"]).replace("Z", "+00:00")
        try:
            since = calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))  # Docker reports UTC
        except ValueError:
            since = None
    health = (state.get("Health") or {}).get("Status")
    return {"exists": True, "running": bool(state.get("Running")),
            "state": state.get("Status", "?") + (f" · {health}" if health else ""),
            "boot": policy in {"always", "unless-stopped"}, "since": since}


def container_status(name: str) -> dict[str, Any]:
    inspected = run("docker", "inspect", "--type=container", "--format",
                    "{{json .State}}|{{.HostConfig.RestartPolicy.Name}}", name, timeout=15)
    if inspected.returncode != 0:
        if "no such object" in inspected.stderr.lower() or "no such container" in inspected.stderr.lower():
            return {"exists": False, "running": False, "state": "not created", "boot": False}
        raise RuntimeError((inspected.stderr or inspected.stdout).strip()[:200] or "docker inspect failed")
    state_json, policy = inspected.stdout.strip().rsplit("|", 1)
    return _container_status(state_json, policy)


def container_statuses() -> dict[str, dict[str, Any]]:
    """Read the container list once and inspect only the managed containers that exist."""
    names = [name for name, spec in ITEMS.items() if spec["kind"] == "container"]
    listed = run("docker", "ps", "--all", "--format", "{{.Names}}", timeout=15)
    if listed.returncode != 0:
        raise RuntimeError((listed.stderr or listed.stdout).strip()[:200] or "docker ps failed")
    present = set(listed.stdout.splitlines())
    found = [name for name in names if name in present]
    result = {name: {"exists": False, "running": False, "state": "not created", "boot": False}
              for name in names if name not in present}
    if found:
        inspected = run("docker", "inspect", "--type=container", "--format",
                        "{{.Name}}|{{json .State}}|{{.HostConfig.RestartPolicy.Name}}", *found, timeout=15)
        if inspected.returncode != 0:
            raise RuntimeError((inspected.stderr or inspected.stdout).strip()[:200] or "docker inspect failed")
        for line in inspected.stdout.splitlines():
            name, state_json, policy = line.split("|", 2)
            result[name.removeprefix("/")] = _container_status(state_json, policy)
        if any(name not in result for name in found):
            raise RuntimeError("docker inspect returned incomplete status")
    return result


def describe(item_id: str, status: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = ITEMS[item_id]
    if status is None:
        status = (unit_status(spec) if spec["kind"] == "unit" else comfy_status() if spec["kind"] == "comfyui"
                  else container_status(item_id))
    actions = {}
    for action in ALL_ACTIONS:
        reason = locked_reason(spec, action)
        if spec["kind"] == "comfyui" and action.startswith("boot"):
            reason = "Started on demand by its supervisor"
        if not status["exists"]:
            reason = reason or "Not installed"
        actions[action] = {"allowed": not reason, "reason": reason}
    return {"id": item_id, "kind": spec["kind"], "group": spec["group"], "name": spec["name"], "note": spec["note"],
            **status, "actions": actions}


_items_cache_lock = threading.Lock()
_items_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])


def _status_error(item_id: str, exc: Exception) -> dict[str, Any]:
    spec = ITEMS[item_id]
    return {"id": item_id, "kind": spec["kind"], "group": spec["group"], "name": spec["name"],
            "note": spec["note"], "exists": None, "running": False, "state": f"status unavailable: {exc}",
            "boot": None, "actions": {}}


def all_items() -> list[dict[str, Any]]:
    global _items_cache
    with _items_cache_lock:
        if time.monotonic() - _items_cache[0] < 3:
            return _items_cache[1]
        with ThreadPoolExecutor(max_workers=4) as pool:
            containers = pool.submit(container_statuses)
            units = {name: pool.submit(describe, name) for name, spec in ITEMS.items() if spec["kind"] != "container"}
            try:
                container_states = containers.result()
                container_error = None
            except Exception as exc:  # noqa: BLE001
                container_states, container_error = {}, exc
            rows = []
            for name, spec in ITEMS.items():
                try:
                    if spec["kind"] == "container":
                        if container_error:
                            raise container_error
                        rows.append(describe(name, container_states[name]))
                    else:
                        rows.append(units[name].result())
                except Exception as exc:  # noqa: BLE001
                    rows.append(_status_error(name, exc))
        _items_cache = (time.monotonic(), rows)
        return rows


# ---------------------------------------------------------------- actions --


_PLATFORM: dict | None = None


def platform_info() -> dict:
    """Linux, or Windows via WSL2 (untested) - Settings → Services labels it."""
    global _PLATFORM
    if _PLATFORM is None:
        _PLATFORM = windowshost.platform()
    return _PLATFORM

class Refused(Exception):
    def __init__(self, reason: str, can_force: bool = False):
        super().__init__(reason)
        self.reason, self.can_force = reason, can_force


def perform(item_id: str, action: str, force: bool) -> str:
    spec = ITEMS[item_id]
    reason = locked_reason(spec, action)
    if reason:
        raise Refused(reason)
    if action in ("stop", "restart") and spec.get("busy"):
        try:
            busy, detail = busy_check(spec["busy"])
        except Exception as exc:  # noqa: BLE001
            if not force:
                raise Refused(f"Couldn't confirm {spec['name']} is idle ({type(exc).__name__}). "
                              "It may be hung - confirm to do it anyway.", can_force=True) from None
            busy, detail = False, ""
        if busy:
            raise Refused(f"{spec['name']} is busy: {detail}. Wait for it to finish (or cancel it) first.")
    kind = spec["kind"]
    if kind == "unit":
        verb = {"start": "start", "stop": "stop", "restart": "restart", "boot-on": "enable", "boot-off": "disable"}[action]
        result = run("systemctl", "--user", verb, spec["unit"], timeout=90)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip()[:300] or f"systemctl {verb} failed")
        return f"{spec['name']}: {verb}d"
    if kind == "comfyui":
        if action.startswith("boot"):
            raise Refused("Started on demand by its supervisor")
        reply = http_json(f"{SUPERVISOR}/{action}", "POST", {}, timeout=60)
        status = reply.get("status") if isinstance(reply, dict) else None
        if status == "busy":
            raise Refused("ComfyUI is busy - its supervisor refused")
        return f"ComfyUI: {status or action}"
    # container
    if action in ("boot-on", "boot-off"):
        state = load_state()
        state.setdefault("container_boot", {})[item_id] = action == "boot-on"
        save_state(state)
        apply_boot(item_id, action == "boot-on")
        return f"{spec['name']}: {'starts' if action == 'boot-on' else 'does not start'} at boot"
    args = ["docker", action]
    if action in ("stop", "restart"):
        args += ["-t", str(spec.get("stop_timeout", 20))]
    result = run(*args, item_id, timeout=180)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:300] or f"docker {action} failed")
    return f"{spec['name']}: {action} done"


def apply_boot(name: str, on: bool) -> None:
    result = run("docker", "update", "--restart", "unless-stopped" if on else "no", name, timeout=30)
    if result.returncode != 0:
        log.warning("docker update %s failed: %s", name, result.stderr.strip()[:200])


def enforce_boot_loop() -> None:
    """Re-applies saved container boot choices (compose recreates reset them)."""
    while True:
        try:
            for name, on in load_state().get("container_boot", {}).items():
                if ITEMS.get(name, {}).get("kind") != "container":
                    continue
                status = container_status(name)
                if status["exists"] and status["boot"] != on:
                    log.info("Re-applying boot=%s for %s", on, name)
                    apply_boot(name, on)
        except Exception:  # noqa: BLE001
            log.exception("boot enforcement failed")
        time.sleep(60)


# ----------------------------------------------------------------- browser --
# The Master Agent's /browser command (Agent page): a Chromium with remote debugging on
# loopback, in Hermes' own debug profile (what `hermes /browser connect` launches), and
# Hermes' browser tools pointed at it through `browser.cdp_url` in its config.

CDP_PORT = 9222
CDP_DEFAULT = f"http://127.0.0.1:{CDP_PORT}"
DEBUG_PROFILE = HERMES_HOME / "chrome-debug"
HERMES_CONTAINER = "hermes-agent"


def cdp_ready(url: str, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/json/version", timeout=timeout) as response:
            return json.loads(response.read())
    except Exception:  # noqa: BLE001
        return None


def find_cdp(url: str) -> str | None:
    candidates = [url] + (["http://[::1]:9222"] if url == CDP_DEFAULT else [])
    return next((c for c in candidates if cdp_ready(c)), None)


def hermes_config(*args: str) -> subprocess.CompletedProcess:
    return run("docker", "exec", HERMES_CONTAINER, "hermes", "config", *args, timeout=40)


def configured_cdp() -> str:
    """browser.cdp_url from Hermes' config, read on the host (a `docker exec hermes config get` takes seconds)."""
    try:
        config = yaml.safe_load((HERMES_HOME / "config.yaml").read_text()) or {}
        return str(((config.get("browser") or {}).get("cdp_url")) or "").strip()
    except (OSError, yaml.YAMLError, AttributeError):
        got = hermes_config("get", "browser.cdp_url")
        return got.stdout.strip() if got.returncode == 0 else ""


def browser_status() -> dict[str, Any]:
    configured = configured_cdp()
    configured = "" if configured.lower() in ("none", "null", "''", '""') else configured
    live = find_cdp(configured or CDP_DEFAULT)
    info = cdp_ready(live) if live else None
    return {"configured": configured, "live": live, "browser": (info or {}).get("Browser", ""),
            "connected": bool(configured and live)}


DDG_SEARCH = {"short_name": "DuckDuckGo", "keyword": "duckduckgo.com", "url": "https://duckduckgo.com/?q={searchTerms}",
              "suggestions_url": "https://duckduckgo.com/ac/?q={searchTerms}&type=list",
              "favicon_url": "https://duckduckgo.com/favicon.ico", "prepopulate_id": 92, "safe_for_autoreplace": True}


def ddg_default_search(profile: Path) -> None:
    """The debug profile's default search engine = DuckDuckGo (the user's choice for the agent's browser).
    Written before launch, while no Chromium holds the profile; Linux builds don't enforce the pref hash."""
    prefs_path = profile / "Default" / "Preferences"
    try:
        prefs = json.loads(prefs_path.read_text()) if prefs_path.exists() else {}
    except (OSError, ValueError):
        return                                   # unreadable: leave the profile alone
    data = prefs.setdefault("default_search_provider_data", {})
    if (data.get("template_url_data") or {}).get("keyword") == DDG_SEARCH["keyword"]:
        return
    data["template_url_data"] = dict(DDG_SEARCH)
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = prefs_path.with_suffix(".rocsy-tmp")
    tmp.write_text(json.dumps(prefs, separators=(",", ":")))
    tmp.replace(prefs_path)


def browser_connect(url: str) -> dict[str, Any]:
    url = (url or CDP_DEFAULT).strip().rstrip("/")
    if not re.fullmatch(r"https?://(127\.0\.0\.1|localhost|\[::1\]):\d{2,5}", url):
        raise Refused("Only a browser on this machine (127.0.0.1 / localhost) can be connected")
    live = find_cdp(url)
    launched = False
    if not live and url == CDP_DEFAULT:
        binary = next((b for b in ("chromium", "google-chrome-stable", "google-chrome", "brave-browser") if shutil.which(b)), None)
        if not binary:
            raise RuntimeError("No Chromium-family browser is installed on the host")
        DEBUG_PROFILE.mkdir(parents=True, exist_ok=True)
        ddg_default_search(DEBUG_PROFILE)
        env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0"),
               "XAUTHORITY": os.environ.get("XAUTHORITY", str(Path.home() / ".Xauthority"))}
        subprocess.Popen([binary, f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1",
                          f"--user-data-dir={DEBUG_PROFILE}", "--no-first-run", "--no-default-browser-check", "about:blank"],
                         env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        launched = True
        for _ in range(40):
            time.sleep(0.5)
            if live := find_cdp(url):
                break
    if not live:
        raise RuntimeError(f"No browser answered at {url}" + (" after launching one" if launched else
                           ". Start a Chromium-family browser with --remote-debugging-port, then retry"))
    result = hermes_config("set", "browser.cdp_url", live)
    if result.returncode != 0:
        raise RuntimeError("hermes config set failed: " + (result.stderr or result.stdout).strip()[:200])
    return {**browser_status(), "launched": launched}


def browser_disconnect() -> dict[str, Any]:
    result = hermes_config("unset", "browser.cdp_url")
    if result.returncode != 0 and "not" not in (result.stderr + result.stdout).lower():
        raise RuntimeError("hermes config unset failed: " + (result.stderr or result.stdout).strip()[:200])
    return browser_status()


# ------------------------------------------------------------------ server --

_action_lock = threading.Lock()
_cli_slots = threading.BoundedSemaphore(8)


class Handler(BaseHTTPRequestHandler):
    server_version = "bunny-hostctl"

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self.send_error(400, "Chunked request bodies are unsupported")
            return False
        raw_length = self.headers.get("Content-Length", "0")
        limit = (48 * 1024 * 1024 if self.path in ("/clis/run", "/clis/chat", "/providers/chat")
                 else 16384 if self.path.startswith("/core/comfynodes/") or self.path == "/comfyui/profiles"
                 else 8192)
        try:
            length = int(raw_length)
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            self.close_connection = True
            self.send_error(413 if length > limit else 400, "Invalid request body length")
            return False
        return True

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter access log
        log.debug(fmt, *args)

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Hostctl-Secret", "")
        return bool(SECRET) and hmac.compare_digest(supplied, SECRET) and not self.headers.get("CF-Connecting-IP")

    def _small_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            body = {}
        return body if isinstance(body, dict) else {}

    def do_GET(self) -> None:
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        if not self._authorized():
            return self._send(403, {"error": "forbidden"})
        if self.path == "/items":
            return self._send(200, {"items": all_items(), "platform": platform_info()})
        if self.path == "/uninstall/status":
            status, reply = uninstall.status()
            return self._send(status, reply)
        if self.path == "/comfyui/profiles":
            try:
                return self._send(200, http_json(f"{SUPERVISOR}/profiles"))
            except Exception as exc:
                log.exception("Could not read ComfyUI profiles")
                return self._send(503, {"error": f"ComfyUI supervisor unavailable: {str(exc)[:160]}"})
        if self.path == "/load-priority":
            return self._send(200, loadpriority.settings())
        if self.path == "/gpu-tune":
            return self._send(*gputune.handle("GET", self.path, {}))
        if self.path == "/browser":
            return self._send(200, browser_status())
        if self.path in ("/clis", "/clis?fresh=1"):
            return self._send(200, codingclis.detect(fresh=self.path.endswith("fresh=1")))
        if self.path.startswith("/providers/info?"):
            slug = (urllib.parse.parse_qs(self.path.partition("?")[2]).get("provider") or [""])[0]
            try:
                return self._send(200, providerchat.info(slug))
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                return self._send(400, {"error": str(exc)[:300]})
        if self.path.startswith("/storage"):
            return self._send(*storage.handle("GET", self.path, {}))
        if self.path.startswith("/core/voice"):
            return self._send(*voiceengines.handle("GET", self.path))
        if self.path.startswith("/core/engines"):
            return self._engines("GET", {})
        if self.path.startswith("/llama-benchmark/"):
            return self._benchmark("GET", {})
        if self.path.startswith("/llama-hipblaslt/"):
            return self._send(*llama_hipblaslt.handle("GET", self.path, {}))
        if self.path.startswith("/core/llamacpp"):
            return self._core("GET", {})
        if self.path.startswith("/core/extras"):
            return self._extras("GET")
        if self.path.startswith("/rea/mods/"):
            return self._send(*reamods.handle("GET", self.path))
        if self.path.startswith("/rea/"):
            return self._send(*reafolders.handle("GET", self.path, {}))
        if self.path.startswith("/core/comfynodes"):
            return self._comfynodes("GET", {})
        if self.path.startswith("/core/swap"):
            return self._swap("GET", {})
        if self.path == "/connections/hermes":
            return self._hermes_link("GET", {})
        if self.path.startswith("/llama-optimize/"):
            return self._llama_optimize("GET", {})
        if self.path.startswith("/core/runtime"):
            return self._runtime("GET", {})
        self._send(404, {"error": "not found"})

    def _engines(self, method: str, body: dict[str, Any]) -> None:
        try:
            status, reply = coreengines.handle(method, self.path, body)
        except Exception as exc:
            log.exception("Core engine action failed")
            status, reply = 500, {"error": str(exc)[:300]}
        self._send(status, reply)

    def _benchmark(self, method: str, body: dict[str, Any]) -> None:
        try:
            status, reply = llama_benchmark.handle(method, self.path, body)
        except Exception as exc:
            log.exception("Benchmark action failed")
            status, reply = 500, {"error": str(exc)[:300]}
        self._send(status, reply)

    def _runtime(self, method: str, body: dict[str, Any]) -> None:
        """Core Backend → GPU runtimes (Rocsy's own ROCm versions + Vulkan tools, coreruntime.py)."""
        try:
            status, reply = coreruntime.handle(method, self.path, body)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s %s failed", method, self.path)
            status, reply = 500, {"error": str(exc)[:300]}
        if method == "POST":
            log.info("%s -> %s", self.path, reply.get("message") or reply.get("error"))
        self._send(status, reply)

    def _llama_optimize(self, method: str, body: dict[str, Any]) -> None:
        try:
            status, reply = llama_optimize.handle(method, self.path, body)
        except (OSError, ValueError) as exc:
            status, reply = 422, {"error": str(exc)[:300]}
        except Exception as exc:
            log.exception("Llama optimization failed")
            status, reply = 500, {"error": str(exc)[:300]}
        self._send(status, reply)

    def _core(self, method: str, body: dict[str, Any]) -> None:
        """Models -> Manager -> Core Backend (llama.cpp + RDNA Boost builds, corebackend.py)."""
        try:
            status, reply = corebackend.handle(method, self.path, body if isinstance(body, dict) else {})
        except Exception as exc:  # noqa: BLE001
            log.exception("%s %s failed", method, self.path)
            status, reply = 500, {"error": str(exc)[:300]}
        if method == "POST":
            log.info("%s -> %s", self.path, reply.get("message") or reply.get("error"))
        self._send(status, reply)

    def _extras(self, method: str) -> None:
        try:
            status, reply = coreextras.handle(method, self.path)
        except Exception as exc:
            log.exception("%s %s failed", method, self.path)
            status, reply = 500, {"error": str(exc)[:300]}
        self._send(status, reply)

    def _comfynodes(self, method: str, body: dict[str, Any]) -> None:
        """Core Backend -> ComfyUI: install missing custom node packs (comfynodes.py), restart when idle."""
        try:
            if method == "POST" and self.path == "/core/comfynodes/restart":
                try:
                    message = perform("comfyui", "restart", False)
                    comfynodes.mark_restarted()
                    status, reply = 200, {"message": message}
                except Refused as exc:
                    status, reply = 409, {"error": exc.reason}
            else:
                status, reply = comfynodes.handle(method, self.path, body)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s %s failed", method, self.path)
            status, reply = 500, {"error": str(exc)[:300]}
        if method == "POST":
            log.info("%s -> %s", self.path, reply.get("message") or reply.get("error"))
        self._send(status, reply)

    def _swap(self, method: str, body: dict[str, Any]) -> None:
        try:
            status, reply = coreswap.handle(method, self.path, body)
        except Exception as exc:
            log.exception("%s %s failed", method, self.path)
            status, reply = 500, {"error": str(exc)[:300]}
        self._send(status, reply)

    def _hermes_link(self, method: str, body: dict[str, Any]) -> None:
        try:
            reply = (hermeslink.status() if method == "GET" else hermeslink.disconnect() if method == "DISCONNECT"
                     else hermeslink.connect(str(body.get("model") or "")))
            status = 200
        except hermeslink.LinkError as exc:
            status, reply = 409, {"error": str(exc)}
        except Exception as exc:
            log.exception("Hermes connection failed")
            status, reply = 500, {"error": "Could not configure the Hermes connection"}
        self._send(status, reply)

    def _stream_cli(self) -> None:
        if not _cli_slots.acquire(blocking=False):
            return self._send(503, {"error": "Too many CLI streams are running"})
        try:
            self._stream_cli_active()
        finally:
            _cli_slots.release()

    def _stream_cli_active(self) -> None:
        """POST /clis/run (NDJSON events), /clis/chat and /providers/chat (chat-completion SSE): see
        codingclis and providerchat."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Expected an object")
            if self.path in ("/clis/chat", "/providers/chat"):
                lines = source = (codingclis if self.path == "/clis/chat" else providerchat).chat_stream(body)
                kind = "text/event-stream"
                first = next(lines)        # starts the CLI: a bad request still gets a plain error
            else:
                run_id, source = codingclis.run(body)
                lines, kind = (json.dumps(e) + "\n" for e in source), "application/x-ndjson"
                first = json.dumps({"type": "started", "run": run_id}) + "\n"
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return self._send(400, {"error": str(exc)[:300]})
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(first.encode())
            for line in lines:
                self.wfile.write(line.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            source.close()             # the caller went away: end the CLI with it
        self.close_connection = True

    def do_POST(self) -> None:
        if not self._authorized():
            return self._send(403, {"error": "forbidden"})
        if self.path in ("/clis/run", "/clis/chat", "/providers/chat"):
            return self._stream_cli()
        if self.path == "/clis/stop":
            return self._send(200, {"stopped": codingclis.stop(str(self._small_json().get("run") or ""))})
        if self.path.startswith("/rea/mods/"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                body = {}
            return self._send(*reamods.handle("POST", self.path, body if isinstance(body, dict) else {}))
        if self.path.startswith("/rea/"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            return self._send(*reafolders.handle("POST", self.path, body if isinstance(body, dict) else {}))
        if self.path.startswith("/gpu-tune/"):
            return self._send(*gputune.handle("POST", self.path, self._small_json()))
        if self.path == "/uninstall/start":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            if not isinstance(body, dict) or type(body.get("delete_files")) is not bool:
                return self._send(400, {"error": "Invalid uninstall choice."})
            try:
                status, reply = uninstall.start(body["delete_files"])
            except Exception:
                log.exception("Could not start Rocsy uninstall")
                return self._send(503, {"error": "Could not start uninstall."})
            return self._send(status, reply)
        if self.path.startswith("/core/voice/"):
            return self._send(*voiceengines.handle("POST", self.path))
        if self.path.startswith("/core/engines/") or self.path.startswith("/llama-benchmark/"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            return (self._engines("POST", body) if self.path.startswith("/core/engines/")
                    else self._benchmark("POST", body))
        if self.path.startswith("/core/llamacpp/"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            return self._core("POST", body)
        if self.path.startswith("/core/extras/"):
            return self._extras("POST")
        if self.path.startswith("/core/comfynodes/"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            return self._comfynodes("POST", body if isinstance(body, dict) else {})
        if self.path.startswith("/storage/"):
            # Settings -> Storage + Models -> Manager -> Move (storage.py).
            status, reply = storage.handle("POST", self.path, self._small_json())
            if self.path in ("/storage/move", "/storage/locations/add", "/storage/locations/remove"):
                log.info("%s -> %s", self.path, reply.get("error") or "ok")
            return self._send(status, reply)
        if self.path.startswith("/models/"):
            # Studios' missing-files dialog (via Models): find model files on disk, link them in.
            try:
                status, reply = modelfind.handle("POST", self.path, self._small_json())
            except Exception as exc:  # noqa: BLE001
                log.exception("%s failed", self.path)
                status, reply = 500, {"error": str(exc)[:300]}
            if self.path == "/models/link":
                log.info("%s -> %s", self.path, reply.get("dest") or reply.get("error"))
            return self._send(status, reply)
        if self.path.startswith("/core/runtime/"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            return self._runtime("POST", body if isinstance(body, dict) else {})
        if self.path.startswith("/core/swap/"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            return self._swap("POST", body if isinstance(body, dict) else {})
        if self.path == "/connections/hermes":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            return self._hermes_link("POST", body if isinstance(body, dict) else {})
        if self.path in ("/llama-hipblaslt/start", "/llama-hipblaslt/delete"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            return self._send(*llama_hipblaslt.handle("POST", self.path, body if isinstance(body, dict) else {}))
        if self.path == "/llama-optimize/start":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            return self._llama_optimize("POST", body if isinstance(body, dict) else {})
        if self.path == "/connections/hermes/disconnect":
            return self._hermes_link("DISCONNECT", {})
        if self.path in ("/browser/connect", "/browser/disconnect"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            try:
                reply = browser_connect(str((body or {}).get("url") or "")) if self.path.endswith("connect") and "dis" not in self.path \
                    else browser_disconnect()
                log.info("%s -> %s", self.path, reply)
                return self._send(200, reply)
            except Refused as exc:
                return self._send(409, {"error": exc.reason})
            except Exception as exc:  # noqa: BLE001
                log.exception("%s failed", self.path)
                return self._send(500, {"error": str(exc)[:300]})
        match = re.fullmatch(r"/items/([a-z0-9-]+)/(start|stop|restart|boot-on|boot-off)", self.path)
        if not match or match.group(1) not in ITEMS:
            return self._send(404, {"error": "unknown item or action"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        item_id, action = match.groups()
        if not _action_lock.acquire(timeout=1):
            return self._send(409, {"error": "Another action is still running", "can_force": False})
        try:
            message = perform(item_id, action, bool(isinstance(body, dict) and body.get("force")))
            global _items_cache
            with _items_cache_lock:
                _items_cache = (0.0, [])
            log.info("%s %s -> %s", action, item_id, message)
            self._send(200, {"message": message, "item": describe(item_id)})
        except Refused as exc:
            self._send(409, {"error": exc.reason, "can_force": exc.can_force})
        except Exception as exc:  # noqa: BLE001
            log.exception("%s %s failed", action, item_id)
            self._send(500, {"error": str(exc)[:300]})
        finally:
            _action_lock.release()

    def do_PUT(self) -> None:
        if not self._authorized():
            return self._send(403, {"error": "forbidden"})
        if self.path == "/gpu-tune":
            return self._send(*gputune.handle("PUT", self.path, self._small_json()))
        if self.path == "/load-priority":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            return self._send(*loadpriority.save(body))
        if self.path != "/comfyui/profiles":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            reply = http_json(f"{SUPERVISOR}/profiles", "PUT", body, timeout=10)
            self._send(200, reply)
        except urllib.error.HTTPError as exc:
            try:
                reply = json.loads(exc.read())
            except (ValueError, OSError):
                reply = {"error": "ComfyUI profile update was rejected"}
            self._send(exc.code, reply)
        except Exception as exc:
            log.exception("Could not update ComfyUI profiles")
            self._send(503, {"error": f"ComfyUI supervisor unavailable: {str(exc)[:160]}"})


def hermes_context_loop() -> None:
    """Keep Hermes' context_length for llama-swap models equal to what llama.cpp really serves
    (Parameters / Models hub edits land within 30 s)."""
    while True:
        try:
            result = hermeslink.sync_context()
            if result["changed"]:
                log.info("Hermes context lengths synced from llama-swap: %s", result["contexts"])
        except Exception:
            log.exception("Hermes context sync failed")
        try:
            result = hermeslink.sync_local_route()
            if result["changed"]:
                log.info("Hermes: %d model route(s) moved off the Cloudflare tunnel to the local llama-swap", result["routes"])
        except Exception:
            log.exception("Hermes local route sync failed")
        try:
            result = hermeslink.sync_guardrails()
            if result["changed"]:
                log.info("Hermes loop guard set to %s from Agent settings", result["mode"])
        except Exception:
            log.exception("Hermes loop guard sync failed")
        try:
            result = hermeslink.sync_compression()
            if result["changed"]:
                log.info("Hermes compression synced: %s, cap %s for %s, backup %s", result["preset"], result["cap"],
                         result["model"] or "no Agent model", result["backup"] or "none")
        except Exception:
            log.exception("Hermes compression sync failed")
        time.sleep(30)


def warm_caches() -> None:
    for name, warm in (("Core extras", coreextras.view), ("GPU runtimes", coreruntime.overview)):
        try:
            warm()
        except Exception:  # noqa: BLE001
            log.exception("%s cache warm-up failed", name)


class BoundedHTTPServer(ThreadingHTTPServer):
    request_queue_size = 64

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._slots = threading.BoundedSemaphore(48)
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def main() -> None:
    if not SECRET:
        raise SystemExit("HOSTCTL_SECRET is not set - refusing to start without authentication")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    llama_optimize.recover()
    threading.Thread(target=enforce_boot_loop, daemon=True).start()
    threading.Thread(target=corebackend.background_loop, daemon=True).start()
    threading.Thread(target=hermes_context_loop, daemon=True).start()
    threading.Thread(target=gputune.background_loop, daemon=True).start()
    threading.Thread(target=warm_caches, daemon=True).start()
    storage.busy_check = busy_check
    storage.start_background()
    server = BoundedHTTPServer((HOST, PORT), Handler)
    log.info("bunny-hostctl on %s:%s (%d items)", HOST, PORT, len(ITEMS))
    server.serve_forever()


if __name__ == "__main__":
    import dockeraccess  # the docker group in the session that installed Rocsy (see dockeraccess.py)
    dockeraccess.reexec_with_docker_group()
    main()
