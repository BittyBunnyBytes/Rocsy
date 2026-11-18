"""Maintenance (Settings -> Maintenance): automatic restarts for Rocsy machines that run as home servers.

Off by default. Nothing here runs until the master switch is on, and then only the parts switched on:

- Network watchdog: when the network has been out for `minutes` (default 10) the computer restarts, at most
  `max_restarts` times in a row (default 3). The count is kept on disk, so it survives the restarts, and goes back
  to 0 as soon as the network is seen working again. After the last try it waits for the network instead of
  restarting forever. "Out" is either the Wi-Fi/LAN link to the router (default: an internet provider outage,
  which a restart can't fix, doesn't count) or any internet outage.
- Daily restart: once a day at a set time. With "wait for jobs" on, it waits while the GPU queue, a training
  run or a model download is busy, for up to DAILY_WINDOW after the set time, then skips that day.

Restarts go through `systemctl reboot` (logind allows it for the user's own session) or `shutdown /r` on Windows.
MAINTENANCE_DRY_RUN=1 logs a restart instead of doing it (for testing)."""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("hostctl.maintenance")

STORE = Path(os.getenv("MAINTENANCE_STORE", Path(__file__).resolve().parent / "maintenance.json"))
DRY_RUN = os.getenv("MAINTENANCE_DRY_RUN", "") == "1"
TICK = 20                      # seconds between checks
DAILY_WINDOW = 3 * 3600        # how long after the set time a daily restart may still happen (waiting for jobs)
BUSY_GRACE = 60 * 60           # how long a network restart waits for running jobs before going ahead anyway
MIN_UPTIME = 10 * 60           # never a daily restart in the first minutes after boot
# Public resolvers, reached by TCP: a reply or a refusal both prove the internet answers. No data is sent.
INTERNET_PROBES = (("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443))
HISTORY = 40

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "watchdog": {"enabled": True, "minutes": 10, "max_restarts": 3, "mode": "router"},
    "daily": {"enabled": False, "time": "04:00", "wait_idle": True},
}

busy_check: Callable[[str], tuple[bool, str]] | None = None   # hostctl.busy_check, set at startup

_lock = threading.Lock()
_live: dict[str, Any] = {"online": None, "router": None, "internet": None, "outage_since": None,
                         "busy_since": None, "checked": 0, "note": ""}
_started = time.time()


# ------------------------------------------------------------------ store --

def _load() -> dict[str, Any]:
    try:
        data = json.loads(STORE.read_text())
    except (OSError, ValueError):
        data = {}
    config = copy.deepcopy(DEFAULTS)
    saved = data.get("config") if isinstance(data.get("config"), dict) else {}
    config["enabled"] = bool(saved.get("enabled", False))
    for part in ("watchdog", "daily"):
        if isinstance(saved.get(part), dict):
            config[part].update({k: v for k, v in saved[part].items() if k in DEFAULTS[part]})
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    state.setdefault("net_restarts", 0)
    state.setdefault("last_daily", "")
    state.setdefault("history", [])
    return {"config": config, "state": state}


def _save(data: dict[str, Any]) -> None:
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STORE)


def _event(data: dict[str, Any], kind: str, text: str) -> None:
    log.info("maintenance %s: %s", kind, text)
    data["state"]["history"] = ([{"t": int(time.time()), "kind": kind, "text": text}] + data["state"]["history"])[:HISTORY]


def _clean(body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(current)
    if "enabled" in body:
        config["enabled"] = bool(body["enabled"])
    w = body.get("watchdog") if isinstance(body.get("watchdog"), dict) else {}
    if "enabled" in w:
        config["watchdog"]["enabled"] = bool(w["enabled"])
    if "minutes" in w:
        config["watchdog"]["minutes"] = max(5, min(240, int(w["minutes"])))
    if "max_restarts" in w:
        config["watchdog"]["max_restarts"] = max(1, min(10, int(w["max_restarts"])))
    if w.get("mode") in ("router", "internet"):
        config["watchdog"]["mode"] = w["mode"]
    d = body.get("daily") if isinstance(body.get("daily"), dict) else {}
    if "enabled" in d:
        config["daily"]["enabled"] = bool(d["enabled"])
    if "time" in d:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(d["time"])):
            raise ValueError("The daily restart time must look like 04:00")
        config["daily"]["time"] = str(d["time"])
    if "wait_idle" in d:
        config["daily"]["wait_idle"] = bool(d["wait_idle"])
    return config


# --------------------------------------------------------------- checks --

def _default_gateway() -> str:
    """The router's address from the kernel's routing table (Linux); '' when there's no default route."""
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                return socket.inet_ntoa(int(fields[2], 16).to_bytes(4, "little"))
    except (OSError, ValueError):
        pass
    return ""


def _answers(host: str, port: int, timeout: float = 3.0) -> bool:
    """True when the host replies at all: a connection or a refusal (closed port) both count."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except ConnectionRefusedError:
        return True
    except OSError:
        return False


def _check(mode: str) -> dict[str, Any]:
    internet = any(_answers(host, port) for host, port in INTERNET_PROBES)
    router: bool | None = None
    if sys.platform.startswith("linux"):
        gateway = _default_gateway()
        router = bool(gateway) and (internet or any(_answers(gateway, port, 2.0) for port in (53, 80, 443)))
    if mode == "internet" or router is None:
        online = internet
    else:
        online = router            # the router answers: a provider outage isn't something a restart fixes
    return {"online": online, "router": router, "internet": internet}


def _busy() -> str:
    if busy_check is None:
        return ""
    reasons = []
    for kind in ("queue", "trainer", "models"):
        try:
            busy, why = busy_check(kind)
        except Exception:          # that service is down or not installed: it can't be busy
            continue
        if busy:
            reasons.append(why)
    return ", ".join(reasons)


def _uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return time.time() - _started


def can_restart() -> str:
    """'yes', 'challenge' (needs a password), 'no' or '' (unknown) - logind's answer for this user."""
    if sys.platform == "win32":
        return "yes"
    try:
        out = subprocess.run(["busctl", "call", "org.freedesktop.login1", "/org/freedesktop/login1",
                              "org.freedesktop.login1.Manager", "CanReboot"], capture_output=True, text=True, timeout=5)
        match = re.search(r'"(\w+)"', out.stdout)
        return match.group(1) if match else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _restart(data: dict[str, Any], kind: str, text: str) -> None:
    _event(data, kind, text + (" (dry run: not restarted)" if DRY_RUN else ""))
    _save(data)
    if DRY_RUN:
        return
    command = ["shutdown", "/r", "/t", "30", "/c", "Rocsy maintenance restart"] if sys.platform == "win32" else ["systemctl", "reboot"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or f"exit {result.returncode}").strip()[:200])
    except Exception as exc:
        _event(data, "error", f"The restart was refused: {exc}")
        _save(data)


# ----------------------------------------------------------------- loop --

def _daily_target(config: dict[str, Any], now: datetime) -> datetime:
    hour, minute = map(int, config["daily"]["time"].split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _tick() -> None:
    with _lock:
        data = _load()
    config, state = data["config"], data["state"]
    if not config["enabled"]:
        _live.update(outage_since=None, busy_since=None, note="")
        return
    now = time.time()

    # --- network watchdog
    w = config["watchdog"]
    if w["enabled"]:
        result = _check(w["mode"])
        _live.update(result, checked=int(now))
        if result["online"]:
            if _live["outage_since"]:
                _live["outage_since"] = None
            if state["net_restarts"]:
                with _lock:
                    data = _load()
                    data["state"]["net_restarts"] = 0
                    _event(data, "network", "The network is back: the restart count is reset")
                    _save(data)
                state = data["state"]
            _live.update(busy_since=None, note="")
        else:
            _live["outage_since"] = _live["outage_since"] or now
            out_for = now - _live["outage_since"]
            if out_for >= w["minutes"] * 60:
                if state["net_restarts"] >= w["max_restarts"]:
                    _live["note"] = f"Restarted {state['net_restarts']} times without the network coming back: waiting for it now"
                else:
                    busy = _busy()
                    if busy and (_live["busy_since"] is None or now - _live["busy_since"] < BUSY_GRACE):
                        _live["busy_since"] = _live["busy_since"] or now
                        _live["note"] = f"Network out; restart waits for {busy}"
                    else:
                        with _lock:
                            data = _load()
                            data["state"]["net_restarts"] += 1
                            count = data["state"]["net_restarts"]
                            _restart(data, "network", f"The network was out for {round(out_for / 60)} minutes: "
                                                      f"restart {count} of {w['max_restarts']}")
                        # A refused (or dry-run) restart waits a whole outage period before the next try.
                        _live.update(outage_since=now, busy_since=None)
                        return
    else:
        _live.update(online=None, router=None, internet=None, outage_since=None)

    # --- daily restart
    d = config["daily"]
    if d["enabled"]:
        local = datetime.now()
        today = local.strftime("%Y-%m-%d")
        target = _daily_target(config, local)
        if state.get("last_daily") != today and local >= target:
            late = (local - target).total_seconds()
            if late > DAILY_WINDOW or _uptime() < MIN_UPTIME:
                if late > DAILY_WINDOW:
                    with _lock:
                        data = _load()
                        data["state"]["last_daily"] = today
                        if _live.get("daily_waiting"):
                            _event(data, "daily", f"Today's restart was skipped: still busy {DAILY_WINDOW // 3600} hours after {d['time']}")
                        _save(data)
                    _live["daily_waiting"] = False
                return
            busy = _busy() if d["wait_idle"] else ""
            if busy:
                _live["daily_waiting"] = True
                _live["note"] = _live["note"] or f"Daily restart waits for {busy}"
                return
            with _lock:
                data = _load()
                data["state"]["last_daily"] = today
                _live["daily_waiting"] = False
                _restart(data, "daily", f"Daily restart ({d['time']})")


def background_loop() -> None:
    time.sleep(30)                  # let the network come up after a boot before the first check
    while True:
        try:
            _tick()
        except Exception:
            log.exception("maintenance check failed")
        time.sleep(TICK)


# ---------------------------------------------------------------- routes --

def _status() -> dict[str, Any]:
    data = _load()
    config, state = data["config"], data["state"]
    next_daily = None
    if config["enabled"] and config["daily"]["enabled"]:
        local = datetime.now()
        target = _daily_target(config, local)
        if state.get("last_daily") == local.strftime("%Y-%m-%d") or local > target + timedelta(seconds=DAILY_WINDOW):
            target += timedelta(days=1)
        next_daily = int(target.timestamp())
    outage = _live["outage_since"]
    return {
        "config": config,
        "net_restarts": state["net_restarts"],
        "history": state["history"][:20],
        "live": {"online": _live["online"], "router": _live["router"], "internet": _live["internet"],
                 "checked": _live["checked"], "outage_minutes": round((time.time() - outage) / 60, 1) if outage else 0,
                 "note": _live["note"]},
        "next_daily": next_daily,
        "can_restart": can_restart(),
        "dry_run": DRY_RUN,
    }


def handle(method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if method == "GET" and path == "/maintenance":
        return 200, _status()
    if method == "PUT" and path == "/maintenance":
        with _lock:
            data = _load()
            try:
                config = _clean(body, data["config"])
            except (TypeError, ValueError) as exc:
                return 400, {"error": str(exc)[:200]}
            if config["enabled"] != data["config"]["enabled"]:
                _event(data, "settings", "Maintenance switched " + ("on" if config["enabled"] else "off"))
            if config["enabled"] and not data["config"]["enabled"]:
                data["state"]["net_restarts"] = 0
                # Switching on after today's time shouldn't restart right away.
                local = datetime.now()
                if local >= _daily_target(config, local):
                    data["state"]["last_daily"] = local.strftime("%Y-%m-%d")
            if config["daily"]["time"] != data["config"]["daily"]["time"] or (config["daily"]["enabled"] and not data["config"]["daily"]["enabled"]):
                local = datetime.now()
                if local >= _daily_target(config, local):
                    data["state"]["last_daily"] = local.strftime("%Y-%m-%d")
            data["config"] = config
            _save(data)
        _live.update(outage_since=None, busy_since=None, note="")
        return 200, _status()
    if method == "POST" and path == "/maintenance/check":
        _live.update(_check(_load()["config"]["watchdog"]["mode"]), checked=int(time.time()))
        return 200, _status()
    return 404, {"error": "not found"}
