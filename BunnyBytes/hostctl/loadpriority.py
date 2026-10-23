"""Model loading priority (Settings -> Services): the CPU and disk priority each model server starts with.

Each engine keeps its own copy where its launcher reads it, so a level applies on that engine's next load:
- Strata: started by hostctl itself (coreengines.start_strata -> systemd-run Nice / IOScheduling* properties);
- llama.cpp models: llama-launch inside the llama container reads Llama/overrides/_load_priority on every load;
- ComfyUI: its supervisor (PUT /priority, stored next to it) wraps the ComfyUI it starts in nice/ionice.

Only levels at or below normal: this user's services may not raise their own priority (user@.service has
LimitNICE=0 and the llama container lacks CAP_SYS_NICE)."""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from paths import LLAMA_OVERRIDES

LEVELS = {
    "background": {"label": "Background", "nice": 19, "io_class": "idle", "io_prio": None},
    "low": {"label": "Low", "nice": 10, "io_class": "best-effort", "io_prio": 7},
    "normal": {"label": "Normal", "nice": 0, "io_class": "best-effort", "io_prio": 4},
}
ENGINES = {
    "strata": {"name": "Strata", "note": "Qwen Flash Next sizes served by Strata", "default": "background"},
    "llama": {"name": "llama.cpp models", "note": "Every other model in Llama", "default": "normal"},
    "comfyui": {"name": "ComfyUI", "note": "Image, video, music and voice workflows (the whole ComfyUI process)",
                "default": "normal"},
}
STORE = Path(__file__).resolve().parent / "load_priority.json"
LLAMA_FILE = LLAMA_OVERRIDES / "_load_priority"
SUPERVISOR = os.getenv("COMFYUI_SUPERVISOR_URL", "http://127.0.0.1:8083")


def levels() -> dict[str, str]:
    try:
        saved = json.loads(STORE.read_text())
    except (OSError, ValueError):
        saved = {}
    return {engine: saved.get(engine) if saved.get(engine) in LEVELS else spec["default"]
            for engine, spec in ENGINES.items()}


def systemd_props(engine: str) -> tuple[str, ...]:
    """systemd-run properties for a unit that starts this engine."""
    level = LEVELS[levels()[engine]]
    props = [f"Nice={level['nice']}", f"IOSchedulingClass={level['io_class']}"]
    if level["io_prio"] is not None:
        props.append(f"IOSchedulingPriority={level['io_prio']}")
    return tuple(props)


def settings() -> dict:
    current = levels()
    return {"levels": [{"id": key, "label": spec["label"], "nice": spec["nice"]} for key, spec in LEVELS.items()],
            "engines": [{"id": key, "name": spec["name"], "note": spec["note"], "level": current[key],
                         "default": spec["default"]} for key, spec in ENGINES.items()]}


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.chmod(0o644)                      # the llama container reads it as its own user
    os.replace(tmp, path)


def save(body: dict) -> tuple[int, dict]:
    supplied = body.get("engines") if isinstance(body, dict) else None
    if not isinstance(supplied, dict) or not supplied or set(supplied) - set(ENGINES) \
            or any(value not in LEVELS for value in supplied.values()):
        return 400, {"error": "Unknown engine or priority level"}
    updated = {**levels(), **supplied}
    if "comfyui" in supplied:
        request = urllib.request.Request(f"{SUPERVISOR}/priority", method="PUT",
                                         data=json.dumps({"level": updated["comfyui"]}).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
        except Exception as exc:  # noqa: BLE001
            return 503, {"error": f"ComfyUI supervisor unavailable: {str(exc)[:160]}"}
    LLAMA_OVERRIDES.mkdir(parents=True, exist_ok=True)
    _write(LLAMA_FILE, updated["llama"] + "\n")
    _write(STORE, json.dumps(updated, indent=2) + "\n")
    return 200, settings()
