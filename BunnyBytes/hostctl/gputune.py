"""GPU tuning (Settings -> GPU tuning): AI-first overclock profiles for the AMD card, applied through LACT,
plus a smart switch that picks the profile for what the GPU is doing right now.

- Talks to lactd over /run/lactd.sock (the user is in LACT's admin group, wheel). Every apply goes through
  set_gpu_config and is confirmed only after the card still answers, so a bad write is reverted by LACT itself.
- Profiles: stock (LACT defaults) plus one per workload class: llm (memory-bound: VRAM matters most),
  video (long sustained compute, also LoRA training) and media (image / voice / music, shorter bursts).
  Values are clamped to a conservative AI window: never above the card's stock boost clock.
- Smart switch: the active bunny-lock queue entry names the app (Vid, Krea, Mus, Agent ...), which maps to a
  workload before the job's GPU work starts; LLM servers used outside the queue are seen through LACT's
  per-process GPU utilisation. With nothing running, the idle profile comes back after a grace period.
- Guided tuning: one variable at a time, in the order VRAM -> voltage offset -> core clock -> power limit.
  A step is "testing" until the user marks it stable; an unstable mark (or a GPU reset seen in the kernel log
  while a tuned profile was applied) rolls that variable back to its last stable value.
- Telemetry: every workload run records time, average power/clocks, peak temperatures and throttle reasons
  under the profile that ran it, so before/after comparisons come from real jobs (no synthetic GPU tests)."""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("hostctl.gputune")

LACT_SOCKET = os.getenv("LACT_SOCKET", "/run/lactd.sock")
QUEUE_URL = os.getenv("GPUTUNE_QUEUE_URL", "http://127.0.0.1:8600/Queue/api/queue")
STORE = Path(os.getenv("GPUTUNE_STORE", Path(__file__).resolve().parent / "gputune.json"))
TICK = 2.0
COMPUTE_MODE = 5            # LACT power profile mode index "COMPUTE" on RDNA3
LLM_PROCESSES = re.compile(r"llama-server|strata|llama", re.I)
LLM_BUSY_PERCENT = 10
# H3 runs its Hermes (LLM) turns through the queue under app "Vid" with these job-id prefixes.
VID_LLM_PREFIXES = ("enhance-", "repair-", "ground-", "plan-review-", "character-", "scene-", "lora-")
APP_WORKLOADS = {"Vid": "video", "Trainer": "video", "Krea": "media", "Mus": "media",
                 "Voice": "media", "Agent": "llm", "Framework": "llm"}
WORKLOADS = {"llm": "LLM", "video": "Video generation", "media": "Image · Voice · Music"}
RESET_PATTERN = r"amdgpu.*(GPU reset|ring .* timeout|MODE1 reset|MES failed|GPU Hang|device wedged)"
TUNE_ORDER = ("vram", "voltage", "core", "power")
FIELDS = {
    # key: label, unit, step for the guided tuner (raw units), the direction a step goes
    "vram": {"label": "VRAM clock", "unit": "MHz", "step": 10, "dir": 1},
    "voltage": {"label": "Voltage offset", "unit": "mV", "step": -10, "dir": -1},
    "core": {"label": "Max core clock", "unit": "MHz", "step": 50, "dir": 1},
    "power": {"label": "Power limit", "unit": "%", "step": 3, "dir": 1},
}
BASELINE = {"vram": 1325, "voltage": -50, "core": 2800, "core_min": 500, "power": 0, "compute_mode": True}
DEFAULT_PROFILES = {
    "stock": {"name": "Stock", "note": "LACT defaults: the card as it ships.", "stock": True},
    "llm": {"name": "LLM", "note": "Token generation is memory-bound: VRAM up, core a little lower to keep "
                                   "heat down on long chats.", **BASELINE, "core": 2700},
    "video": {"name": "Video generation", "note": "Long sustained renders and LoRA training: stability "
                                                  "first, undervolt for lower heat.", **BASELINE, "core": 2750},
    "media": {"name": "Image · Voice · Music", "note": "Shorter compute bursts: the AI Daily baseline.",
              **BASELINE},
}
DEFAULT_AUTO = {"enabled": False, "idle_profile": "stock", "idle_after": 90,
                "map": {"llm": "llm", "video": "video", "media": "media"}}

_lock = threading.RLock()
_live: dict[str, Any] = {"workload": None, "source": None, "since": None, "last_switch": 0.0, "idle_since": None,
                         "run": None, "error": None, "stats": None, "lact": False}


class LactError(RuntimeError):
    pass


def lact(_command: str, **args: Any) -> Any:
    message = {"command": _command, "args": args} if args else {"command": _command}   # lactd rejects args on unit commands
    request = json.dumps(message).encode() + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(15)
            sock.connect(LACT_SOCKET)
            sock.sendall(request)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError as exc:
        raise LactError(f"LACT daemon not reachable ({exc.strerror or exc})") from exc
    reply = json.loads(buf)
    if reply.get("status") != "ok":
        data = reply.get("data") or {}
        detail = data.get("description", "error") if isinstance(data, dict) else str(data)
        source = data.get("source") if isinstance(data, dict) else None
        while isinstance(source, dict):
            detail += f": {source.get('description')}"
            source = source.get("source")
        raise LactError(f"LACT {_command}: {detail}")
    return reply.get("data")


# ---------- store ----------

def _load() -> dict:
    try:
        data = json.loads(STORE.read_text())
    except (OSError, ValueError):
        data = {}
    profiles = copy.deepcopy(DEFAULT_PROFILES)
    for key, saved in (data.get("profiles") or {}).items():
        if key in profiles and isinstance(saved, dict):
            profiles[key].update({k: v for k, v in saved.items() if k in BASELINE or k == "tuning"})
    auto = {**DEFAULT_AUTO, **(data.get("auto") or {})}
    auto["map"] = {**DEFAULT_AUTO["map"], **(auto.get("map") or {})}
    return {**data, "profiles": profiles, "auto": auto, "events": data.get("events") or [],
            "runs": data.get("runs") or [], "reset_seen": data.get("reset_seen") or time.time()}


def _save(state: dict) -> None:
    out = {**state, "profiles": {k: {f: v for f, v in p.items() if f in BASELINE or f == "tuning"}
                                 for k, p in state["profiles"].items() if not p.get("stock")},
           "events": state["events"][-150:], "runs": state["runs"][-300:]}
    tmp = STORE.with_name(STORE.name + ".tmp")
    tmp.write_text(json.dumps(out, indent=1) + "\n")
    os.replace(tmp, STORE)


def _event(state: dict, kind: str, text: str, **extra: Any) -> None:
    state["events"].append({"time": time.time(), "kind": kind, "text": text, **extra})
    log.info("GPU tuning %s: %s", kind, text)


# ---------- device ----------

def _device(state: dict | None = None) -> dict:
    devices = lact("list_devices") or []
    wanted = (state or {}).get("gpu")
    for dev in devices:
        if dev["id"] == wanted:
            return dev
    dedicated = [d for d in devices if d.get("device_type") == "Dedicated"] or devices
    if not dedicated:
        raise LactError("LACT sees no GPU")
    return max(dedicated, key=lambda d: "7900" in d.get("name", ""))


def limits(gpu_id: str, state: dict) -> dict:
    """The tuning window for this card: stock boost is the core ceiling (sustained AI loads gain little above
    it and it is where resets start), VRAM up to +10 % over stock, undervolt only, power at most +10 %."""
    clocks = (lact("device_clocks_info", id=gpu_id) or {}).get("table", {}).get("value", {}).get("data", {})
    stats = lact("device_stats", id=gpu_id) or {}
    power = stats.get("power") or {}
    sclk = clocks.get("current_sclk_range") or {"min": 500, "max": 3000}
    mclk = clocks.get("current_mclk_range") or {"max": 1250}
    od = clocks.get("od_range") or {}
    # While a tuned profile is applied LACT reports the tuned range, so stock comes from what was seen before.
    stock = state.get("stock_clocks") or {}
    tuned = _tuned(state)
    stock_core = stock.get("core", sclk["max"]) if tuned else sclk["max"]
    stock_vram = stock.get("vram", mclk["max"]) if tuned else mclk["max"]
    volt_min = max((od.get("voltage_offset") or {}).get("min", -150), -150)
    return {"core": [1800, stock_core], "core_min": [500, 1500],
            "vram": [stock_vram, min(round(stock_vram * 1.10), (od.get("mclk") or {}).get("max", 9999))],
            "voltage": [volt_min, 0], "power": [-10, 10],
            "stock": {"core": stock_core, "vram": stock_vram, "power_w": power.get("cap_default")},
            "power_w": {"default": power.get("cap_default"), "min": power.get("cap_min"), "max": power.get("cap_max")}}


def _tuned(state: dict) -> bool:
    active = state.get("active")
    return bool(active) and not DEFAULT_PROFILES.get(active, {}).get("stock")


def _remember_stock(state: dict, lim: dict) -> None:
    if not state.get("stock_clocks") and not _tuned(state):
        state["stock_clocks"] = {"core": lim["stock"]["core"], "vram": lim["stock"]["vram"]}


def _clamp(profile: dict, lim: dict) -> dict:
    out = dict(profile)
    for key in ("core", "core_min", "vram", "voltage", "power"):
        if key in out:
            lo, hi = lim[key]
            out[key] = int(min(max(int(out[key]), lo), hi))
    out["core_min"] = min(out.get("core_min", 500), out.get("core", 2800) - 300)
    return out


def gpu_config(profile: dict, lim: dict, current: dict | None) -> dict:
    """LACT GpuConfig: keep whatever else the user set in LACT (fans, pmfw), replace clocks/power/mode."""
    ours = {"performance_level", "power_cap", "power_profile_mode_index", "min_core_clock", "max_core_clock",
            "min_memory_clock", "max_memory_clock", "voltage_offset", "gpu_clock_offsets", "mem_clock_offsets",
            "min_voltage", "max_voltage"}
    config = {k: v for k, v in (current or {}).items() if k not in ours}
    config.setdefault("fan_control_enabled", False)
    if profile.get("stock"):
        config["performance_level"] = "auto"
        return config
    p = _clamp(profile, lim)
    config.update({"performance_level": "manual", "min_core_clock": p["core_min"], "max_core_clock": p["core"],
                   "max_memory_clock": p["vram"], "voltage_offset": p["voltage"]})
    if p.get("compute_mode", True):
        config["power_profile_mode_index"] = COMPUTE_MODE
    default_w = lim["power_w"].get("default")
    if p["power"] and default_w:
        watts = round(default_w * (1 + p["power"] / 100))
        config["power_cap"] = float(min(max(watts, lim["power_w"]["min"]), lim["power_w"]["max"]))
    return config


def apply(profile_id: str, reason: str, state: dict | None = None) -> dict:
    """Apply a profile; LACT reverts it unless we confirm, and we confirm only once the card answers again."""
    with _lock:
        own = state is None
        state = state or _load()
        profile = state["profiles"].get(profile_id)
        if not profile:
            raise LactError(f"Unknown profile {profile_id}")
        dev = _device(state)
        lim = limits(dev["id"], state)
        _remember_stock(state, lim)
        config = gpu_config(profile, lim, lact("get_gpu_config", id=dev["id"]))
        lact("set_gpu_config", id=dev["id"], config=config)
        time.sleep(0.6)
        try:
            lact("device_stats", id=dev["id"])
        except LactError:
            lact("confirm_pending_config", command="revert")
            raise
        try:
            lact("confirm_pending_config", command="confirm")
        except LactError as exc:            # nothing pending = LACT applied it as final
            log.info("LACT confirm: %s", exc)
        previous = state.get("active")
        state["active"] = profile_id
        state["applied_at"] = time.time()
        _event(state, "apply", f"{profile['name']} applied ({reason})", profile=profile_id, previous=previous)
        if own:
            _save(state)
        return state


# ---------- guided tuning ----------

def _tuning(profile: dict) -> dict:
    t = profile.setdefault("tuning", {})
    t.setdefault("settled", [])
    t.setdefault("stable", {k: profile.get(k) for k in TUNE_ORDER})
    t.setdefault("steps", [])
    return t


def _power_limited(state: dict, profile_id: str) -> bool:
    runs = [r for r in state["runs"] if r.get("profile") == profile_id][-5:]
    return any(any("ppt" in str(t).lower() or "power" in str(t).lower() for t in r.get("throttles", []))
               for r in runs)


def suggestion(state: dict, profile_id: str, lim: dict | None) -> dict | None:
    profile = state["profiles"][profile_id]
    if profile.get("stock"):
        return None
    t = _tuning(profile)
    testing = next((s for s in t["steps"] if s["result"] == "testing"), None)
    if testing:
        runs = [r for r in state["runs"] if r.get("profile") == profile_id and r["start"] >= testing["time"]]
        minutes = sum(r["secs"] for r in runs) / 60
        return {"kind": "testing", "step": testing, "jobs": len(runs), "minutes": round(minutes, 1),
                "ready": len(runs) >= 3 and minutes >= 20,
                "text": f"Testing {FIELDS[testing['field']]['label']} {testing['to']} {FIELDS[testing['field']]['unit']}: "
                        f"{len(runs)} job(s), {minutes:.0f} min under load so far. Run your usual work, then mark it."}
    for field in TUNE_ORDER:
        if field in t["settled"]:
            continue
        spec = FIELDS[field]
        current = profile.get(field, 0)
        target = current + spec["step"]
        if lim:
            lo, hi = lim[field]
            if not lo <= target <= hi:
                continue
        if field == "power" and not _power_limited(state, profile_id):
            return {"kind": "done", "text": "Done: VRAM, voltage and core are settled. Power limit stays stock: "
                                            "recent jobs on this profile weren't power-limited."}
        return {"kind": "next", "field": field, "from": current, "to": target,
                "text": f"Next: {spec['label']} {current} → {target} {spec['unit']}"
                        + (" (raw; effective VRAM clock is double)" if field == "vram" else "")}
    return {"kind": "done", "text": "Every variable is settled for this profile."}


def step(state: dict, profile_id: str) -> None:
    sug = suggestion(state, profile_id, limits(_device(state)["id"], state))
    if not sug or sug["kind"] != "next":
        raise LactError("No tuning step to try right now")
    profile = state["profiles"][profile_id]
    t = _tuning(profile)
    t["stable"][sug["field"]] = profile.get(sug["field"])
    profile[sug["field"]] = sug["to"]
    t["steps"].append({"time": time.time(), "field": sug["field"], "from": sug["from"], "to": sug["to"],
                       "result": "testing"})
    _event(state, "step", f"{profile['name']}: trying {FIELDS[sug['field']]['label']} {sug['from']} → {sug['to']}",
           profile=profile_id)
    apply(profile_id, "tuning step", state)


def mark(state: dict, profile_id: str, result: str, why: str = "") -> bool:
    profile = state["profiles"][profile_id]
    t = _tuning(profile)
    testing = next((s for s in t["steps"] if s["result"] == "testing"), None)
    if not testing:
        return False
    testing["result"] = result
    testing["marked"] = time.time()
    field = testing["field"]
    if result == "stable":
        t["stable"][field] = testing["to"]
        text = f"{profile['name']}: {FIELDS[field]['label']} {testing['to']} marked stable"
    else:
        profile[field] = testing["from"]                    # back off to the last stable value
        t["stable"][field] = testing["from"]
        if field not in t["settled"]:
            t["settled"].append(field)
        text = f"{profile['name']}: {FIELDS[field]['label']} {testing['to']} unstable{why} → back to {testing['from']}"
    _event(state, "mark", text, profile=profile_id)
    return True


# ---------- smart switch ----------

def detect(state: dict, dev_id: str) -> tuple[str | None, str | None]:
    try:
        with urllib.request.urlopen(QUEUE_URL, timeout=3) as response:
            entries = json.loads(response.read()).get("entries", [])
    except Exception:  # noqa: BLE001 - queue down: fall through to process detection
        entries = []
    for entry in entries:
        if entry.get("state") != "active":
            continue
        app = entry.get("app", "")
        workload = APP_WORKLOADS.get(app)
        if app == "Vid" and str(entry.get("job_id", "")).startswith(VID_LLM_PREFIXES):
            workload = "llm"
        if workload:
            return workload, f"{app}: {entry.get('label') or entry.get('job_id')}"[:120]
    try:
        processes = (lact("process_list", id=dev_id) or {}).get("processes", {})
    except LactError:
        processes = {}
    for proc in processes.values():
        util = proc.get("util") or {}
        busy = max([v for v in util.values() if isinstance(v, (int, float))] or [0])
        if LLM_PROCESSES.search(proc.get("name", "")) and busy >= LLM_BUSY_PERCENT:
            return "llm", f"{proc['name']} generating"
    return None, None


def _sample(run: dict, stats: dict) -> None:
    run["n"] += 1
    run["power"] += (stats.get("power") or {}).get("average") or 0
    clock = stats.get("clockspeed") or {}
    run["sclk"] += clock.get("gpu_clockspeed") or 0
    run["mclk"] += clock.get("vram_clockspeed") or 0
    run["busy"] += stats.get("busy_percent") or 0
    temps = stats.get("temps") or {}
    for key in ("junction", "mem"):
        value = (temps.get(key) or {}).get("current")
        if value is not None:
            run[f"max_{key}"] = max(run.get(f"max_{key}", 0), value)
    for reason in (stats.get("throttle_info") or {}):
        if reason not in run["throttles"]:
            run["throttles"].append(reason)


def _finish_run(state: dict) -> None:
    run = _live.get("run")
    _live["run"] = None
    if not run or not run["n"]:
        return
    n = run["n"]
    secs = time.time() - run["start"]
    if secs < 15:
        return
    state["runs"].append({"start": run["start"], "secs": round(secs), "profile": run["profile"],
                          "workload": run["workload"], "source": run["source"],
                          "avg_power": round(run["power"] / n, 1), "avg_sclk": round(run["sclk"] / n),
                          "avg_mclk": round(run["mclk"] / n), "avg_busy": round(run["busy"] / n),
                          "max_junction": run.get("max_junction"), "max_mem": run.get("max_mem"),
                          "throttles": run["throttles"]})


def _resets_since(since: float) -> list[str]:
    try:
        out = subprocess.run(["journalctl", "-k", "--no-pager", "-o", "short-unix", f"--since=@{int(since)}",
                              "-g", RESET_PATTERN], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line for line in out.splitlines() if line and not line.startswith("--")]


def _on_reset(state: dict, lines: list[str]) -> None:
    active = state.get("active")
    profile = state["profiles"].get(active or "", {})
    _event(state, "reset", f"GPU reset in the kernel log while {profile.get('name', 'no profile')} was applied",
           lines=lines[-4:])
    if active and not profile.get("stock"):
        mark(state, active, "unstable", " (GPU reset)")
        state["auto"]["enabled"] = False
        state["hold"] = None
        try:
            apply("stock", "safety: GPU reset seen", state)
        except LactError as exc:
            _event(state, "error", f"Could not return to stock after the reset: {exc}")
        _event(state, "safety", "Smart switch turned off after a GPU reset. Check the profile, then turn it back on.")


def tick() -> None:
    with _lock:
        state = _load()
        try:
            dev = _device(state)
            stats = lact("device_stats", id=dev["id"])
            _live.update(stats=stats, lact=True, error=None, device=dev)
        except (LactError, ValueError) as exc:
            _live.update(lact=False, error=str(exc))
            return
        changed = False
        now = time.time()
        if now - state.get("reset_checked", 0) >= 15:
            lines = _resets_since(state["reset_seen"])
            state["reset_checked"] = now
            state["reset_seen"] = now
            if lines:
                _finish_run(state)
                _on_reset(state, lines)
            changed = True
        workload, source = detect(state, dev["id"])
        if workload != _live["workload"]:
            _finish_run(state)
            _live.update(workload=workload, source=source, since=now)
            changed = True
        if workload:
            _live["idle_since"] = None
            if not _live.get("run"):
                _live["run"] = {"start": now, "profile": state.get("active") or "stock", "workload": workload,
                                "source": source, "n": 0, "power": 0.0, "sclk": 0, "mclk": 0, "busy": 0,
                                "throttles": []}
            _sample(_live["run"], stats)
        elif _live["idle_since"] is None:
            _live["idle_since"] = now
        auto = state["auto"]
        if auto["enabled"] and not state.get("hold"):
            target = auto["map"].get(workload) if workload else None
            if not workload and now - (_live["idle_since"] or now) >= auto["idle_after"]:
                target = auto["idle_profile"]
            if target and target in state["profiles"] and target != state.get("active") \
                    and now - _live["last_switch"] >= 5:
                try:
                    _finish_run(state)
                    label = WORKLOADS.get(workload, "idle")
                    apply(target, f"smart switch: {label}" + (f" · {source}" if source else ""), state)
                    _live["last_switch"] = now
                    if workload:
                        _live["run"] = None     # the next tick starts a run under the new profile
                except LactError as exc:
                    _event(state, "error", f"Smart switch could not apply {target}: {exc}")
                changed = True
        if changed:
            _save(state)


def background_loop() -> None:
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001
            log.exception("GPU tuning tick failed")
        time.sleep(TICK)


# ---------- HTTP ----------

def snapshot() -> dict:
    with _lock:
        state = _load()
        lim = None
        try:
            dev = _device(state)
            lim = limits(dev["id"], state)
        except LactError as exc:
            dev = None
            _live["error"] = str(exc)
        profiles = []
        for key, p in state["profiles"].items():
            item = {"id": key, **{k: v for k, v in p.items() if k != "tuning"}}
            if not p.get("stock"):
                t = _tuning(p)
                item["tuning"] = {"settled": t["settled"], "steps": t["steps"][-12:], "stable": t["stable"]}
                item["suggestion"] = suggestion(state, key, lim)
                runs = [r for r in state["runs"] if r.get("profile") == key]
                item["runs"] = runs[-8:]
            profiles.append(item)
    stats = _live.get("stats") or {}
    return {"lact": _live.get("lact"), "error": _live.get("error"), "device": dev, "limits": lim,
            "fields": FIELDS, "workloads": WORKLOADS, "profiles": profiles, "auto": state["auto"],
            "active": state.get("active"), "hold": state.get("hold"), "applied_at": state.get("applied_at"),
            "workload": _live.get("workload"), "source": _live.get("source"),
            "idle_since": _live.get("idle_since"), "events": state["events"][-25:][::-1],
            "stock_runs": [r for r in state["runs"] if r.get("profile") == "stock"][-8:],
            "live": {"power": (stats.get("power") or {}).get("average"),
                     "cap": (stats.get("power") or {}).get("cap_current"),
                     "sclk": (stats.get("clockspeed") or {}).get("gpu_clockspeed"),
                     "mclk": (stats.get("clockspeed") or {}).get("vram_clockspeed"),
                     "junction": ((stats.get("temps") or {}).get("junction") or {}).get("current"),
                     "mem_temp": ((stats.get("temps") or {}).get("mem") or {}).get("current"),
                     "busy": stats.get("busy_percent"), "level": stats.get("performance_level"),
                     "throttles": list((stats.get("throttle_info") or {}).keys()),
                     "vram_used": (stats.get("vram") or {}).get("used"),
                     "vram_total": (stats.get("vram") or {}).get("total")}}


def handle(method: str, path: str, body: dict) -> tuple[int, dict]:
    try:
        if method == "GET" and path == "/gpu-tune":
            return 200, snapshot()
        if method == "PUT" and path == "/gpu-tune":
            return _update(body)
        if method == "POST" and path.startswith("/gpu-tune/"):
            return _action(path.rsplit("/", 1)[1], body)
    except LactError as exc:
        return 409, {"error": str(exc)}
    return 404, {"error": "not found"}


def _update(body: dict) -> tuple[int, dict]:
    with _lock:
        state = _load()
        lim = limits(_device(state)["id"], state)
        for key, values in (body.get("profiles") or {}).items():
            profile = state["profiles"].get(key)
            if not profile or profile.get("stock") or not isinstance(values, dict):
                return 400, {"error": f"Unknown or fixed profile {key}"}
            clean = {}
            for field in ("core", "core_min", "vram", "voltage", "power"):
                if field in values:
                    try:
                        clean[field] = int(values[field])
                    except (TypeError, ValueError):
                        return 400, {"error": f"{field} must be a number"}
            if "compute_mode" in values:
                clean["compute_mode"] = bool(values["compute_mode"])
            profile.update(_clamp({**profile, **clean}, lim))
            for field in TUNE_ORDER:
                if field in clean:
                    _tuning(profile)["stable"][field] = profile[field]
            if state.get("active") == key:
                apply(key, "edited", state)
        auto = body.get("auto")
        if isinstance(auto, dict):
            ids = set(state["profiles"])
            if "enabled" in auto:
                state["auto"]["enabled"] = bool(auto["enabled"])
                _event(state, "auto", "Smart switch " + ("on" if auto["enabled"] else "off"))
            if auto.get("idle_profile") in ids:
                state["auto"]["idle_profile"] = auto["idle_profile"]
            if "idle_after" in auto:
                state["auto"]["idle_after"] = int(min(max(int(auto["idle_after"]), 10), 1800))
            for workload, target in (auto.get("map") or {}).items():
                if workload in WORKLOADS and target in ids:
                    state["auto"]["map"][workload] = target
        _save(state)
    return 200, snapshot()


def _action(action: str, body: dict) -> tuple[int, dict]:
    profile_id = str(body.get("profile") or "")
    with _lock:
        state = _load()
        if action in ("apply", "step", "mark", "reset-tuning") and profile_id not in state["profiles"]:
            return 400, {"error": "Unknown profile"}
        if action == "apply":
            # A manual pick holds while the smart switch is on, until Resume.
            state["hold"] = profile_id if state["auto"]["enabled"] else None
            apply(profile_id, "manual", state)
        elif action == "resume":
            state["hold"] = None
            _event(state, "auto", "Smart switch resumed")
        elif action == "step":
            if state["auto"]["enabled"]:
                state["hold"] = profile_id      # stay on the profile under test until it is marked
            step(state, profile_id)
        elif action == "mark":
            result = body.get("result")
            if result not in ("stable", "unstable") or not mark(state, profile_id, result):
                return 400, {"error": "Nothing is being tested on this profile"}
            if state.get("active") == profile_id:
                apply(profile_id, "tuning mark", state)
            if state.get("hold") == profile_id:
                state["hold"] = None
        elif action == "reset-tuning":
            profile = state["profiles"][profile_id]
            if profile.get("stock"):
                return 400, {"error": "Stock has nothing to reset"}
            defaults = DEFAULT_PROFILES[profile_id]
            profile.clear()
            profile.update(copy.deepcopy(defaults))
            _event(state, "reset-tuning", f"{defaults['name']} back to its starting values", profile=profile_id)
            if state.get("active") == profile_id:
                apply(profile_id, "reset to defaults", state)
        else:
            return 404, {"error": "unknown action"}
        _save(state)
    return 200, snapshot()
