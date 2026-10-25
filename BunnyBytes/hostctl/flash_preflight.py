"""Can ROCm FlashAttention be built into this ComfyUI environment? (run with ComfyUI's own Python)

    .venv/bin/python flash_preflight.py [<flash-attention source dir>]

Reads GPU_ARCHS ("gfx1100;gfx1101") from the environment. Prints one line per finding and
exits 1 when the build can't work, before anything is downloaded or compiled:
  ERR <text>          a blocker
  WARN <text>         builds, but worth knowing
  ROCM_HOME=<path>    the ROCm whose hipcc compiles it (exported by the build script)
  ARCHS=<a;b>         the GPU targets the CK backend supports (others are dropped)
build-rocm-flash-attention.sh runs it first, and coreextras.py before queueing the job.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# setup.py's allowed_archs (ROCm/flash-attention, branch tridao, 2026-08); read from the
# source instead when it is already cloned.
SUPPORTED = ["gfx90a", "gfx942", "gfx950", "gfx1100", "gfx1101", "gfx1102", "gfx1150", "gfx1151", "gfx1200", "gfx1201"]
SDK_HINT = ("install ROCm's HIP SDK (Arch: rocm-hip-sdk; Ubuntu/Fedora: rocm-hip-sdk from AMD's repository) "
            "or point ROCM_PATH at a ROCm folder with bin/hipcc")


def supported(source: Path | None) -> list[str]:
    try:
        found = re.search(r"allowed_archs\s*=\s*\[([^\]]*)\]", (source / "setup.py").read_text())
        archs = re.findall(r"\"(gfx[0-9a-z]+)\"", found.group(1)) if found else []
        return archs or SUPPORTED
    except (OSError, TypeError):
        return SUPPORTED


def rocm_home() -> str:
    """What torch.utils.cpp_extension would pick, minus ComfyUI's own .venv/bin (a pip ROCm
    runtime there has hipcc but no development headers)."""
    for name in ("ROCM_HOME", "ROCM_PATH"):
        if os.environ.get(name):
            return os.environ[name]
    venv_bin = str(Path(sys.executable).parent)
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p != venv_bin)
    hipcc = shutil.which("hipcc", path=path)
    if hipcc:
        return str(Path(os.path.realpath(hipcc)).parent.parent)
    return "/opt/rocm" if Path("/opt/rocm/bin/hipcc").exists() else ""


def hip_version(hipcc: Path) -> tuple[int, int] | None:
    try:
        out = subprocess.run([str(hipcc), "--version"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.search(r"HIP version:\s*(\d+)\.(\d+)", out)
    return (int(found.group(1)), int(found.group(2))) if found else None


def main() -> int:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    errors = []
    try:
        import torch
    except Exception as exc:  # noqa: BLE001
        print(f"ERR ComfyUI's Python can't import PyTorch ({exc}); finish the ComfyUI setup first")
        return 1
    torch_hip = getattr(torch.version, "hip", None)
    if not torch_hip:
        errors.append("ComfyUI's PyTorch is not a ROCm build; install PyTorch for ROCm first")

    home = rocm_home()
    hipcc = Path(home) / "bin" / "hipcc" if home else None
    if not hipcc or not os.access(hipcc, os.X_OK):
        errors.append(f"No ROCm compiler (hipcc) found: {SDK_HINT}")
    else:
        print(f"ROCM_HOME={home}")
        have = hip_version(hipcc)
        want = tuple(int(x) for x in re.findall(r"\d+", torch_hip or "")[:2]) or None
        if have is None:
            errors.append(f"{hipcc} --version didn't report a HIP version")
        elif want and have[0] != want[0]:
            errors.append(f"hipcc is HIP {have[0]}.{have[1]} but ComfyUI's PyTorch was built for HIP "
                          f"{want[0]}.{want[1]}; install a ROCm {want[0]}.x HIP SDK or point ROCM_PATH at one")
        elif want and have != want:
            print(f"WARN hipcc is HIP {have[0]}.{have[1]}, PyTorch was built for HIP {want[0]}.{want[1]} "
                  "(same major version: builds and runs)")

    allowed = supported(source)
    archs = [a for a in os.environ.get("GPU_ARCHS", "").split(";") if a]
    usable = [a for a in archs if a in allowed]
    if not archs:
        errors.append("No ROCm GPU architecture was detected; rescan after the GPU is available")
    elif not usable:
        errors.append(f"FlashAttention's ROCm backend doesn't support {', '.join(archs)} "
                      f"(supported: {', '.join(allowed)}); ComfyUI keeps its default attention")
    else:
        if len(usable) < len(archs):
            print(f"WARN skipping unsupported GPU target(s): {', '.join(a for a in archs if a not in allowed)}")
        print(f"ARCHS={';'.join(usable)}")

    if not shutil.which("git"):
        errors.append("git is missing")
    for message in errors:
        print(f"ERR {message}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
