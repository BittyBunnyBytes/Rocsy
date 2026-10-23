"""Where Rocsy's folders are on this machine - one place, no hardcoded home directory.

ROOT is the folder that holds BunnyBytes/, Video/, Llama/ ... (hostctl lives in
ROOT/BunnyBytes/hostctl, so it is found from this file). The other folders come from the
environment, then BunnyBytes/.env (the same values docker compose uses), then a default.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(os.getenv("ROCSY_ROOT") or Path(__file__).resolve().parents[2])
COMPOSE_DIR = ROOT / "BunnyBytes"
BUNNY_ENV = COMPOSE_DIR / ".env"


def env_setting(name: str) -> str:
    """NAME from the environment, else from BunnyBytes/.env ("" when neither has it)."""
    if os.getenv(name):
        return os.environ[name]
    try:
        lines = BUNNY_ENV.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        match = re.fullmatch(rf"\s*(?:export\s+)?{re.escape(name)}\s*=(.*)", line)
        if match:
            return os.path.expanduser(os.path.expandvars(match.group(1).strip().strip("\"'")))
    return ""


COMFY_ROOT = Path(env_setting("ROCSY_COMFY_ROOT") or ROOT / "external" / "ComfyUI")
HERMES_HOME = Path(env_setting("ROCSY_HERMES_HOME") or Path.home() / ".hermes")
LLAMA_DIR = ROOT / "Llama"
LLAMA_CONFIG = LLAMA_DIR / "config.yaml"
LLAMA_OVERRIDES = LLAMA_DIR / "overrides"
