"""Set up ComfyUI for Rocsy and its supervisor unit (stdlib only).

Shared by the installer (installer_runtime.py imports it from the bundle) and Models ->
Manager -> Core Backend (coreextras.py, job comfy-download / comfy-supervisor), so a
ComfyUI added after installing ends up exactly like one the installer set up:

- setup(): git clone into a staging folder and move it into the ComfyUI folder without
  replacing anything already there (the installer pre-creates models/ and input/, and
  model downloads may already be in them), make .venv, install PyTorch for ROCm from the
  index ComfyUI's own README gives for AMD on Linux, then requirements.txt. Every step
  is skipped when it is already done, so "Finish setup" can resume a failed attempt.
- h3_setup(): what Video's MiniMax H3 presets need on top: node packs at their tested
  commits, the PDD loader shipped in Video/comfy_nodes, and the Video/tools patches (VAE
  tile overlap, latent-upscale residency + chunks-first). Idempotent; problems are returned.
- install_supervisor(): the comfyui-supervisor user unit (port 8083) the studios ask to
  start ComfyUI on demand. A unit that belongs to another checkout is never replaced.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import windowshost

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
    if windowshost.is_wsl():
        # Windows (untested): PyTorch's own HSA runtime can't reach the GPU through WSL's /dev/dxg.
        windowshost.fix_torch_hsa(comfy)


# Video's MiniMax H3 settings (Fast Stable, Fastest Stable, the latent upscale presets) need
# these packs: (folder, repo, commit Rocsy's H3 renders were tested on, requirements file).
# The commit is checked out on the pack's branch, so Core Backend's update check can still
# move a pack later; PlagueKind stays put once patched (updates skip packs with local edits).
H3_PACKS = [
    ("ComfyUI-PlagueKind-Nodes", "https://github.com/PlagueKind/ComfyUI-PlagueKind-Nodes",
     "f46c3519ddd2afa0fea700d99c2a31112802f545", ""),  # SLA, latent upscale (MMH3*), AdaLN fix, cache
    ("ComfyUI-KJNodes", "https://github.com/kijai/ComfyUI-KJNodes",
     "d3cfe21625e5170126ce06fbfcfe1d88108688c3", "requirements.txt"),  # FFN chunking, low-VRAM attention
    ("ComfyUI-Spectrum-MiniMax-H3", "https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3",
     "120d72e2f48b781235b34149e39bbdf0f1317d82", ""),
    ("ComfyUI-Easy-Use", "https://github.com/yolain/ComfyUI-Easy-Use",
     "450b1ce4ce43b2280521c87f5fa388a898fb2ad2", "requirements.txt"),
    # Not its install.py: that one installs an unpinned torch and CuPy for CUDA.
    ("ComfyUI-Frame-Interpolation", "https://github.com/Fannovel16/ComfyUI-Frame-Interpolation",
     "26545cc2dd95bc3d27f056016300673bdeee78f5", "requirements-no-cupy.txt"),  # RIFE for output FPS
    ("ComfyUI-VideoHelperSuite", "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite",
     "4d907bee61e92c2e65af3bd6383a4e4d356126d1", "requirements.txt"),
    ("ComfyUI-MiniMaxH3-MultiGPU", "https://github.com/Kaihui-AMD/ComfyUI-MiniMaxH3-MultiGPU", "", ""),
]
# Shipped with Video (no upstream repo): the PDD Ref2V 8-step loader, Reference's default turbo.
H3_LOCAL_PACKS = ["comfyui-minimax-h3-pdd"]
# (folder under ComfyUI, patch in Video/tools), applied in order. VAE: 16 px decoder tile
# overlap (~18% faster decode); upscale: unload/residency options + "Upscale all chunks first".
H3_PATCHES = [
    ("", "comfyui-h3-vae-tile-overlap.patch"),
    ("custom_nodes/ComfyUI-PlagueKind-Nodes", "ultimate-upscale-residency.patch"),
    ("custom_nodes/ComfyUI-PlagueKind-Nodes", "ultimate-upscale-chunks-first.patch"),
]
# Never let a pack's requirements replace these (same list as comfynodes.py).
PINNED = re.compile(r"^(torch|torchvision|torchaudio|triton|pytorch-triton-rocm|numpy|rocm|rocm-.*|amd-.*|flash[-_]attn)$", re.I)


def _constraints(comfy: Path) -> Path:
    listed = subprocess.run([str(venv_python(comfy)), "-m", "pip", "list", "--format=json"],
                            capture_output=True, text=True, timeout=120, check=True)
    path = comfy / ".venv" / "rocsy-constraints.txt"
    path.write_text("".join(f"{p['name']}=={p['version']}\n" for p in json.loads(listed.stdout) if PINNED.match(p["name"])))
    return path


def patch_state(target: Path, patch: Path) -> str:
    """applied, clean (applies now; -C1 when upstream moved the context lines) or conflict."""
    def check(*extra: str) -> bool:
        return _ok(["git", "-C", str(target), "apply", "--check", *extra, str(patch)])
    if check("-R"):
        return "applied"
    return "clean" if check() or check("-C1") else "conflict"


def patch_states(comfy: Path, root: Path) -> dict[str, str]:
    """patch_state for each H3_PATCHES entry (missing: folder or patch file absent). Patches on
    the same folder stack: once a later one is applied, the earlier ones count as applied too
    (their own reverse check fails on top of the later change)."""
    states, later_applied = {}, set()
    for folder, name in reversed(H3_PATCHES):
        target, patch = comfy / folder, root / "Video" / "tools" / name
        if not target.is_dir() or not patch.is_file():
            states[name] = "missing"
        elif folder in later_applied:
            states[name] = "applied"
        else:
            states[name] = patch_state(target, patch)
        if states[name] == "applied":
            later_applied.add(folder)
    return {name: states[name] for _folder, name in H3_PATCHES}


def apply_patch(target: Path, patch: Path, run) -> str:
    state = patch_state(target, patch)
    if state == "clean":
        fuzz = [] if _ok(["git", "-C", str(target), "apply", "--check", str(patch)]) else ["-C1"]
        run(["git", "-C", str(target), "apply", *fuzz, str(patch)], f"Applying {patch.name}…", False)
    return state


def unpatch_core(comfy: Path, root: Path) -> list[str]:
    """Take the patches on ComfyUI's own files off before a git pull; h3_setup puts them back."""
    removed = []
    states = patch_states(comfy, root)
    for folder, name in reversed(H3_PATCHES):
        patch = root / "Video" / "tools" / name
        if not folder and states[name] == "applied":
            subprocess.run(["git", "-C", str(comfy), "apply", "-R", str(patch)], check=True, timeout=60)
            removed.append(name)
    return removed


def h3_setup(comfy: Path, root: Path, run) -> list[str]:
    """Node packs, the PDD loader and the patches Video's H3 presets rely on. Every step is
    skipped when already done and an existing pack folder is never replaced, so it is safe on a
    ComfyUI the user brought along and after updates. Returns problems (empty = all in place);
    run(command, message, network) as for setup()."""
    problems = []
    nodes = comfy / "custom_nodes"
    nodes.mkdir(parents=True, exist_ok=True)
    constraints = None
    for folder, url, commit, requirements in H3_PACKS:
        final = nodes / folder
        if final.exists():
            continue
        stage = nodes / f".rocsy-install-{folder}"
        shutil.rmtree(stage, ignore_errors=True)
        try:
            constraints = constraints or _constraints(comfy)
            run(["git", "clone", "--filter=blob:none", url, str(stage)], f"Downloading {folder}…", True)
            if commit:
                run(["git", "-C", str(stage), "reset", "--hard", commit], f"Checking out {folder}’s tested version…", False)
            if requirements and (stage / requirements).is_file():
                run([str(venv_python(comfy)), "-m", "pip", "install", "--progress-bar", "off",
                     "-r", str(stage / requirements), "-c", str(constraints)], f"Installing {folder}’s requirements…", True)
            stage.rename(final)
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(stage, ignore_errors=True)
            problems.append(f"{folder}: {exc}")
    for folder in H3_LOCAL_PACKS:
        source = root / "Video" / "comfy_nodes" / folder
        if source.is_dir() and not (nodes / folder).exists():
            shutil.copytree(source, nodes / folder, ignore=shutil.ignore_patterns("__pycache__"))
    states = patch_states(comfy, root)
    for folder, name in H3_PATCHES:
        target, patch = comfy / folder, root / "Video" / "tools" / name
        if states[name] == "missing":
            problems.append(f"{name}: {'patch' if target.is_dir() else folder} is missing")
            continue
        try:
            # Not applied yet: check again now, on top of the patches applied just before it.
            if states[name] != "applied" and apply_patch(target, patch, run) == "conflict":
                problems.append(f"{name}: doesn’t apply to this version (left unpatched)")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: {exc}")
    return problems


def h3_status(comfy: Path, root: Path) -> dict:
    """What h3_setup would still do: missing packs and patches not applied."""
    packs = [p[0] for p in H3_PACKS] + H3_LOCAL_PACKS
    return {"missing_packs": [p for p in packs if not (comfy / "custom_nodes" / p).exists()],
            "patches": patch_states(comfy, root)}


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
