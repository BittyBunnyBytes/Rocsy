"""The "Rocsy Voice" virtual microphone, for Voice → Live voice → Send to my microphone.

Two PipeWire/PulseAudio modules in the user's session: a null sink the browser plays the converted
voice into ("Rocsy-Voice-Output"), and its monitor remapped as a microphone ("Rocsy Voice") that
games, Discord or Signal can pick. Creating them never changes the default speaker or microphone:
both are read before and put back afterwards if the new devices took over.

"Make it my default microphone" is opt-in and kept alive by the page: it sends a heartbeat every
10 s, and without one for HEARTBEAT_GRACE the previous default microphone is restored (closed tab,
crashed browser, stopped session). The previous default is also saved to disk, so a hostctl
restart still restores it.

Routes (hostctl, X-Hostctl-Secret; the gateway only forwards them for a logged-in visitor on this
machine): GET /voice/mic, POST /voice/mic/create | remove | default {"on": bool} | heartbeat.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("voicemic")

SINK = "rocsy_voice_sink"
MIC = "rocsy_voice_mic"
SINK_LABEL = "Rocsy-Voice-Output"
MIC_LABEL = "Rocsy-Voice"
HEARTBEAT_GRACE = 30.0
STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "rocsy" / "voicemic.json"
LOCK = threading.RLock()
_last_beat = 0.0
_watchdog: threading.Thread | None = None


class Refused(Exception):
    pass


def _pactl(*args: str, timeout: float = 10) -> str:
    if not shutil.which("pactl"):
        raise Refused("pactl isn't installed (PipeWire's pipewire-pulse or PulseAudio).")
    result = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:200] or f"pactl {args[0]} failed")
    return result.stdout.strip()


def _names(kind: str) -> list[str]:
    return [line.split("\t")[1] for line in _pactl("list", "short", kind).splitlines() if "\t" in line]


def _defaults() -> tuple[str, str]:
    return _pactl("get-default-sink"), _pactl("get-default-source")


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


def _our_modules() -> list[str]:
    """Module ids of our sink and mic, found by their arguments (works after a hostctl restart)."""
    ids = []
    for line in _pactl("list", "short", "modules").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and (f"sink_name={SINK}" in parts[2] or f"source_name={MIC}" in parts[2]):
            ids.append(parts[0])
    return ids


def status() -> dict:
    with LOCK:
        try:
            sink, source = _defaults()
            exists = MIC in _names("sources") and SINK in _names("sinks")
        except (Refused, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "error": str(exc)[:200]}
        state = _load_state()
        return {"available": True, "exists": exists, "is_default": source == MIC,
                "default_source": source, "default_sink": sink, "previous_source": state.get("previous_source"),
                "sink_label": SINK_LABEL, "mic_label": MIC_LABEL}


def create() -> dict:
    with LOCK:
        if MIC in _names("sources") and SINK in _names("sinks"):
            return status()
        before_sink, before_source = _defaults()
        for module in _our_modules():          # half-made leftovers
            _pactl("unload-module", module)
        _pactl("load-module", "module-null-sink", f"sink_name={SINK}", "rate=48000", "channels=1",
               f"sink_properties=device.description={SINK_LABEL}")
        _pactl("load-module", "module-remap-source", f"master={SINK}.monitor", f"source_name={MIC}",
               "channels=1", f"source_properties=device.description={MIC_LABEL}")
        # New devices must not take over the speakers or the microphone in use (a call, a game).
        time.sleep(0.3)
        after_sink, after_source = _defaults()
        if after_sink != before_sink:
            _pactl("set-default-sink", before_sink)
        if after_source != before_source:
            _pactl("set-default-source", before_source)
        log.info("Rocsy Voice microphone created (defaults kept: %s / %s)", before_sink, before_source)
        return status()


def _restore_default(reason: str) -> None:
    state = _load_state()
    previous = state.get("previous_source")
    try:
        current = _pactl("get-default-source")
        if current == MIC:
            sources = _names("sources")
            target = previous if previous in sources else next((s for s in sources if s != MIC and not s.endswith(".monitor")), None)
            if target:
                _pactl("set-default-source", target)
                log.info("default microphone back to %s (%s)", target, reason)
    finally:
        state.pop("previous_source", None)
        state["default_active"] = False
        _save_state(state)


def remove() -> dict:
    with LOCK:
        if _pactl("get-default-source") == MIC or _load_state().get("default_active"):
            _restore_default("microphone removed")
        for module in reversed(_our_modules()):
            _pactl("unload-module", module)
        log.info("Rocsy Voice microphone removed")
        return status()


def _watch() -> None:
    while True:
        time.sleep(5)
        with LOCK:
            state = _load_state()
            if not state.get("default_active"):
                continue
            if time.monotonic() - _last_beat > HEARTBEAT_GRACE:
                try:
                    _restore_default("the Live voice page stopped checking in")
                except Exception:  # noqa: BLE001 - try again next round
                    log.exception("could not restore the default microphone")


def _ensure_watchdog() -> None:
    global _watchdog
    if _watchdog is None or not _watchdog.is_alive():
        _watchdog = threading.Thread(target=_watch, daemon=True, name="voicemic-watchdog")
        _watchdog.start()


def heartbeat() -> dict:
    global _last_beat
    with LOCK:
        _last_beat = time.monotonic()
    return {"ok": True}


def set_default(on: bool) -> dict:
    global _last_beat
    with LOCK:
        if not on:
            if _pactl("get-default-source") == MIC or _load_state().get("default_active"):
                _restore_default("turned off")
            return status()
        if MIC not in _names("sources"):
            create()
        current = _pactl("get-default-source")
        state = _load_state()
        if current != MIC:
            state["previous_source"] = current
        state["default_active"] = True
        _save_state(state)
        _last_beat = time.monotonic()
        _ensure_watchdog()
        _pactl("set-default-source", MIC)
        log.info("default microphone -> Rocsy Voice (was %s)", state.get("previous_source"))
        return status()


def startup() -> None:
    """hostctl start: a default override left from before still gets restored if no page checks in."""
    global _last_beat
    if _load_state().get("default_active"):
        _last_beat = time.monotonic()
        _ensure_watchdog()


def handle(method: str, path: str, body: dict) -> tuple[int, dict]:
    try:
        if method == "GET" and path == "/voice/mic":
            return 200, status()
        if method == "POST" and path == "/voice/mic/create":
            return 200, create()
        if method == "POST" and path == "/voice/mic/remove":
            return 200, remove()
        if method == "POST" and path == "/voice/mic/default":
            return 200, set_default(bool(body.get("on")))
        if method == "POST" and path == "/voice/mic/heartbeat":
            return 200, heartbeat()
        return 404, {"error": "not found"}
    except Refused as exc:
        return 409, {"error": str(exc)}
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return 500, {"error": str(exc)[:200]}
