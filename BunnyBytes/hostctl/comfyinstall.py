"""Set up ComfyUI for Rocsy and its supervisor unit (stdlib only).

Shared by the installer (installer_runtime.py imports it from the bundle) and Models ->
Manager -> Core Backend (coreextras.py, job comfy-download / comfy-supervisor), so a
ComfyUI added after installing ends up exactly like one the installer set up:

- setup(): git clone into a staging folder and move it into the ComfyUI folder without
  replacing anything already there (the installer pre-creates models/ and input/, and
  model downloads may already be in them), make .venv, install PyTorch for ROCm from the
  index ComfyUI's own README gives for AMD on Linux, then requirements.txt. Every step
  is skipped when it is already done, so "Finish setup" can resume a failed attempt.
- install_supervisor(): the comfyui-supervisor user unit (port 8083) the studios ask to
  start ComfyUI on demand. A unit that belongs to another checkout is never replaced.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

REPO = "https://github.com/Comfy-Org/ComfyUI.git"
ROCM_INDEX = re.compile(r"--index-url\s+(https://download\.pytorch\.org/whl/rocm[0-9]+(?:\.[0-9]+)*)\b")
UNIT = "comfyui-supervisor.service"


def user_units() -> Path:
    return Path.home() / ".config/systemd/user"


def merge_into(source: Path, target: Path, skipped: list | None = None) -> list:
    """Move source's tree into target; entries target already has are kept (theirs wins).
    A folder Docker created as root (an older install) can't take new entries: those are
    skipped and returned, never fatal (they are ComfyUI's placeholder files)."""
    skipped = [] if skipped is None else skipped
    try:
        target.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        skipped.append(target)
        return skipped
    for entry in source.iterdir():
        there = target / entry.name
        if there.is_dir() and entry.is_dir() and not there.is_symlink():
            merge_into(entry, there, skipped)
        elif not there.exists() and not there.is_symlink():
            try:
                entry.rename(there)
            except PermissionError:
                skipped.append(there)
    return skipped


def _ok(command: list[str]) -> bool:
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_python() -> str:
    """The newest Python 3.10-3.14 on PATH (PyTorch's ROCm wheels cover those)."""
    for name in ("python3.13", "python3.12", "python3.14", "python3.11", "python3.10", "python3"):
        path = shutil.which(name)
        if path and _ok([path, "-c", "import sys; raise SystemExit(not (3, 10) <= sys.version_info[:2] <= (3, 14))"]):
            return path
    raise RuntimeError("ComfyUI needs Python 3.10-3.14; install one (for example python3.13), then try again.")


def venv_python(comfy: Path) -> Path:
    return comfy / ".venv" / "bin" / "python"


def ready(comfy: Path) -> bool:
    """A checkout with a Python environment the supervisor can launch."""
    return (comfy / "main.py").is_file() and venv_python(comfy).exists()


def setup(comfy: Path, run, find=find_python) -> None:
    """run(command, message, network) runs one step and raises on failure (callers add retries
    for network steps and their own progress/logging); find() picks the Python for a new .venv."""
    if not (comfy / "main.py").is_file():
        stage = comfy.parent / f".{comfy.name}-download"
        shutil.rmtree(stage, ignore_errors=True)
        comfy.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--depth", "1", REPO, str(stage)], "Downloading ComfyUI…", True)
        merge_into(stage, comfy)
        shutil.rmtree(stage, ignore_errors=True)
    python_bin = venv_python(comfy)
    if not python_bin.exists():
        run([find(), "-m", "venv", str(comfy / ".venv")], "Creating ComfyUI’s Python environment…", False)
    match = ROCM_INDEX.search((comfy / "README.md").read_text(errors="replace"))
    if not match:
        raise RuntimeError("ComfyUI’s README no longer shows the AMD PyTorch command; "
                           "install PyTorch for ROCm into ComfyUI/.venv by hand.")
    pip = [str(python_bin), "-m", "pip", "install", "--progress-bar", "off"]
    run([*pip, "torch", "torchvision", "torchaudio", "--index-url", match.group(1)],
        f"Installing PyTorch for ROCm ({match.group(1).rsplit('/', 1)[-1]}, several GB)…", True)
    run([*pip, "-r", str(comfy / "requirements.txt")], "Installing ComfyUI’s requirements…", True)


def supervisor_status(root: Path) -> dict:
    """state: ours (this install's supervisors/ folder), other (set up for another checkout) or none."""
    unit = user_units() / UNIT
    try:
        text = unit.read_text() if unit.is_file() else ""
    except OSError:
        text = ""
    if not text:
        return {"state": "none", "active": False}
    active = _ok(["systemctl", "--user", "is-active", "--quiet", UNIT])
    if str(root / "BunnyBytes" / "supervisors") in text:
        return {"state": "ours", "active": active}
    found = re.search(r"^ExecStart=\S+\s+(\S+)", text, re.M)
    return {"state": "other", "active": active, "script": found.group(1) if found else ""}


def require_running(unit: str, wait: float = 4) -> None:
    """A unit that dies at start-up (Restart=on-failure) would crash-loop silently: check it stayed up."""
    time.sleep(wait)
    if _ok(["systemctl", "--user", "is-active", "--quiet", unit]):
        return
    journal = subprocess.run(["journalctl", "--user", "-u", unit, "-n", "15", "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, errors="replace", timeout=15).stdout
    last = next((line for line in reversed(journal.splitlines()) if line.strip()), "see its journal")
    raise RuntimeError(f"{unit} did not stay running: {last[:200]}")


def install_supervisor(root: Path, comfy: Path) -> None:
    status = supervisor_status(root)
    if status["state"] == "other":
        raise RuntimeError(f"{UNIT} is already set up for another ComfyUI; Rocsy did not replace it.")
    if not venv_python(comfy).exists():
        raise RuntimeError(f"{venv_python(comfy)} is missing, so the supervisor could not start ComfyUI.")
    template = root / "BunnyBytes" / "supervisors" / UNIT
    user_units().mkdir(parents=True, exist_ok=True)
    (user_units() / UNIT).write_text(template.read_text().replace("@ROCSY_ROOT@", str(root).replace("%", "%%"))
                                   .replace("@COMFY_ROOT@", str(comfy).replace("%", "%%")))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=30)
    subprocess.run(["systemctl", "--user", "enable", UNIT], check=True, timeout=30)
    subprocess.run(["systemctl", "--user", "restart", UNIT], check=True, timeout=30)
    require_running(UNIT)


def remove_supervisor(root: Path) -> None:
    if supervisor_status(root)["state"] == "ours":
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT], check=False, timeout=30)
        (user_units() / UNIT).unlink(missing_ok=True)
