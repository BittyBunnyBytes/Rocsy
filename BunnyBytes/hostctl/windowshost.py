"""Windows support (UNTESTED): Rocsy on Windows runs inside a WSL2 Linux distro (Ubuntu).

`Install Rocsy (Windows).cmd` detects the Windows version, sets up WSL2 + systemd + Docker
Engine in the distro as root, then runs the normal Linux installer there with --windows.
Windows 7, 8, 8.1 and Windows 10 before version 2004 cannot run WSL2, so the .cmd only sets
them up to open a Rocsy server elsewhere on the network (see classify()).

Inside WSL this module adds what the Linux install doesn't have:
- docker-compose.wsl.yml: GPU containers get /dev/dxg (WSL's GPU) instead of /dev/kfd and
  /dev/dri, plus AMD's ROCm-for-WSL runtime when it is installed; COMPOSE_FILE loads it.
- Windows shortcuts (Desktop, Start menu, optional Startup) that start the containers, keep
  the WSL distro running and open Rocsy in an Edge app window.
- the WSL build of the ROCm HSA runtime inside ComfyUI's PyTorch (AMD's ROCm-on-WSL step).

stdlib only: the bundle installer, uninstall worker and host helper all import it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

UNTESTED = "Windows support is untested."
OVERRIDE_NAME = "docker-compose.wsl.yml"
WSL_LIBS = Path("/usr/lib/wsl")
# AMD's ROCm for WSL (amdgpu-install --usecase=wsl,rocm --no-dkms) ships an HSA runtime that
# talks to /dev/dxg; the stock ROCm/PyTorch one only knows /dev/kfd.
ROCM_WSL_HSA = Path("/opt/rocm/lib/libhsa-runtime64.so.1")
ROCM_WSL_DOCS = "https://rocm.docs.amd.com/projects/radeon/en/latest/docs/install/wsl/install-radeon.html"
INSTALL_MARK = "X-Rocsy-Install="
SAFE_DEST = re.compile(r"^/[A-Za-z0-9._/ -]+$")
SAFE_DISTRO = re.compile(r"^[A-Za-z0-9._-]+$")


def classify(version: str) -> dict:
    """'10.0.22631[.4317]' (the `ver` numbers) -> which Windows this is and whether WSL2 can run."""
    parts = [int(part) for part in re.findall(r"\d+", version or "")[:3]]
    major, minor, build = (parts + [0, 0, 0])[:3]
    if major == 6 and minor in (1, 2, 3):
        name = {1: "Windows 7", 2: "Windows 8", 3: "Windows 8.1"}[minor]
        return {"id": name.split()[-1], "name": name, "build": build, "wsl2": False,
                "reason": f"{name} can't run WSL2 or Docker, so Rocsy can't run on it. It can open a Rocsy "
                          "server elsewhere on your network."}
    if major == 10 and build >= 22000:
        return {"id": "11", "name": "Windows 11", "build": build, "wsl2": True, "reason": ""}
    if major == 10 and build >= 19041:
        return {"id": "10", "name": "Windows 10", "build": build, "wsl2": True, "reason": ""}
    if major == 10:
        return {"id": "10", "name": "Windows 10", "build": build, "wsl2": False,
                "reason": f"Windows 10 build {build} is older than version 2004 (build 19041), the first "
                          "with WSL2. Update Windows to run Rocsy here."}
    return {"id": "", "name": f"Windows {version}".strip(), "build": build, "wsl2": False,
            "reason": "This Windows version isn't recognised."}


def is_wsl() -> bool:
    if Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists() or os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _windows_run(command: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    # Windows programs started from a folder inside the Linux filesystem complain about UNC paths.
    cwd = "/mnt/c" if Path("/mnt/c").is_dir() else None
    return subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          errors="replace", timeout=timeout)


def _exe(name: str, fallback: str) -> str:
    return shutil.which(name) or fallback


def powershell(script: str, timeout: float = 60) -> str:
    exe = _exe("powershell.exe", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    result = _windows_run([exe, "-NoProfile", "-NonInteractive", "-Command", script], timeout)
    if result.returncode:
        raise RuntimeError(f"PowerShell failed: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout.strip()


def windows_version() -> dict | None:
    """The Windows this WSL distro runs on: ROCSY_WINDOWS from the .cmd launcher, else `ver`."""
    if not is_wsl():
        return None
    raw = os.environ.get("ROCSY_WINDOWS", "")
    if not raw:
        try:
            raw = _windows_run([_exe("cmd.exe", "/mnt/c/Windows/System32/cmd.exe"), "/c", "ver"], 15).stdout
        except (OSError, subprocess.TimeoutExpired):
            raw = ""
    found = re.search(r"\d+\.\d+\.\d+", raw)
    return classify(found.group(0)) if found else {"id": "", "name": "Windows", "build": 0, "wsl2": True, "reason": ""}


def platform() -> dict:
    """What Rocsy runs on, for Settings → Services and the installer."""
    windows = windows_version()
    if not windows:
        return {"os": "linux", "name": "Linux", "untested": False, "note": ""}
    distro = os.environ.get("WSL_DISTRO_NAME") or "WSL"
    return {"os": "windows", "name": f"{windows['name']} · WSL2 ({distro})", "windows": windows["id"],
            "build": windows["build"], "distro": distro, "untested": True,
            "note": f"{UNTESTED} Rocsy runs inside WSL2; GPU tuning and some host features are Linux-only."}


def quote_ps(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def to_windows(path: Path) -> str:
    result = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(f"wslpath could not convert {path}")
    return result.stdout.strip()


def to_linux(path: str) -> Path:
    result = subprocess.run(["wslpath", "-u", path], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(f"wslpath could not convert {path}")
    return Path(result.stdout.strip())


def windows_folders() -> dict[str, Path]:
    names = ("LocalApplicationData", "Desktop", "Programs", "Startup")
    script = "; ".join(f"[Environment]::GetFolderPath('{name}')" for name in names)
    lines = powershell(script).splitlines()
    if len(lines) != len(names) or not all(lines):
        raise RuntimeError("Could not read the Windows Desktop and Start menu folders.")
    return {name: to_linux(line.strip()) for name, line in zip(names, lines)}


def open_url(url: str) -> bool:
    """Open a page in the Windows browser (WSL has none of its own)."""
    try:
        powershell(f"Start-Process {quote_ps(url)}", timeout=20)
        return True
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return False


# ---- Docker Compose ----

def gpu_status() -> dict:
    return {"dxg": Path("/dev/dxg").exists(), "rocm_wsl": ROCM_WSL_HSA.exists()}


def _uses_gpu(service: dict) -> bool:
    for device in service.get("devices") or []:
        source = device.get("source", "") if isinstance(device, dict) else str(device).split(":", 1)[0]
        if source.startswith(("/dev/kfd", "/dev/dri", "/dev/dxg")):
            return True
    return False


def compose_override(config: dict, dxg: bool, hsa: bool) -> str:
    """docker-compose.wsl.yml for a resolved `docker compose config --format json`: every service
    that passes /dev/kfd or /dev/dri gets /dev/dxg instead (or no device when WSL has no GPU)."""
    lines = ["# Generated by the Rocsy installer for Windows (WSL2). UNTESTED.",
             "# Rewritten on every Update/Repair; put your own changes in docker-compose.override.yml",
             "# (COMPOSE_FILE in .env lists every file Compose loads).",
             "services:"]
    services = sorted(name for name, service in (config.get("services") or {}).items() if _uses_gpu(service))
    for name in services:
        lines.append(f"  {json.dumps(name)}:")
        if dxg:
            lines += ["    devices: !override", "      - /dev/dxg:/dev/dxg", "    volumes:",
                      f"      - {WSL_LIBS}:{WSL_LIBS}:ro"]
            if hsa:
                lines.append(f"      - {ROCM_WSL_HSA}:{ROCM_WSL_HSA}:ro")
        else:
            lines.append("    devices: !reset []")
    if not services:
        lines[-1] = "services: {}"
    return "\n".join(lines) + "\n"


def compose_files_value(compose_dir: Path) -> str:
    files = ["docker-compose.yml", OVERRIDE_NAME]
    if (compose_dir / "docker-compose.override.yml").is_file():
        files.append("docker-compose.override.yml")
    return ":".join(files)


def compose_supports_override(version_text: str) -> bool:
    """`!override` / `!reset` need Docker Compose 2.24 or newer."""
    found = re.search(r"v?(\d+)\.(\d+)", version_text or "")
    return bool(found) and (int(found.group(1)), int(found.group(2))) >= (2, 24)


# ---- ComfyUI's PyTorch ----

def fix_torch_hsa(comfy: Path) -> str:
    """AMD's ROCm-on-WSL step: PyTorch's bundled libhsa-runtime64 can't see /dev/dxg, so it is
    replaced with the WSL runtime from /opt/rocm. Returns what happened (for the install log)."""
    if not ROCM_WSL_HSA.exists():
        return f"ROCm for WSL is not installed, so ComfyUI can't use the GPU yet. See {ROCM_WSL_DOCS}"
    libs = sorted((comfy / ".venv").glob("lib/python3*/site-packages/torch/lib/libhsa-runtime64.so*"))
    if not libs:
        return "PyTorch's libhsa-runtime64 was not found; nothing to replace."
    for lib in libs:
        lib.unlink()
    shutil.copy2(ROCM_WSL_HSA.resolve(), libs[0].parent / "libhsa-runtime64.so")
    return "PyTorch now uses the ROCm-for-WSL runtime."


# ---- Windows shortcuts ----

def _install_id(dest: Path, distro: str) -> str:
    return f"{distro}:{dest}"


def _folder_name(dest: Path, distro: str) -> str:
    return f"{distro}-{hashlib.sha1(str(dest).encode()).hexdigest()[:8]}"


def _cmd(lines: list[str]) -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def launcher_scripts(dest: Path, distro: str, port: int) -> dict[str, bytes]:
    """background.cmd (start the containers, keep WSL running) and start.cmd (that, then open Rocsy)."""
    if not SAFE_DEST.match(str(dest)) or not SAFE_DISTRO.match(distro):
        raise ValueError("For the Windows shortcuts, the Rocsy folder and WSL distro names may only use "
                         "letters, digits, spaces and . _ - /")
    mark = f"rem Rocsy on Windows ({UNTESTED}) {INSTALL_MARK}{_install_id(dest, distro)}"
    compose_dir = dest / "BunnyBytes"
    background = _cmd([
        "@echo off", mark,
        "rem WSL stops an idle distro; a hidden `sleep` keeps it (and Rocsy's containers) running.",
        f'wsl.exe -d {distro} --exec pgrep -f "^rocsy-keepalive" >nul 2>&1',
        f"if errorlevel 1 powershell.exe -NoProfile -WindowStyle Hidden -Command \"Start-Process wsl.exe "
        f"-WindowStyle Hidden -ArgumentList '-d','{distro}','--exec','bash','-c','exec -a rocsy-keepalive sleep infinity'\"",
        f'wsl.exe -d {distro} --cd "{compose_dir}" --exec sh -c "docker compose up -d >/dev/null 2>&1 '
        f"|| sg docker -c 'docker compose up -d' >/dev/null 2>&1\"",
    ])
    url = f"http://127.0.0.1:{port}/"
    start = _cmd([
        "@echo off", mark,
        'call "%~dp0background.cmd"',
        f'start "" msedge --app={url} --user-data-dir="%~dp0desktop-profile" --no-first-run',
        f'if errorlevel 1 start "" {url}',
    ])
    return {"background.cmd": background, "start.cmd": start}


def _shortcut(lnk: Path, target: Path, icon: Path, minimized: bool) -> None:
    fields = {"TargetPath": to_windows(target), "WorkingDirectory": to_windows(target.parent),
              "IconLocation": to_windows(icon), "Description": f"Rocsy ({UNTESTED})"}
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({quote_ps(to_windows(lnk))}); "
              + " ".join(f"$s.{key} = {quote_ps(value)};" for key, value in fields.items())
              + f" $s.WindowStyle = {7 if minimized else 1}; $s.Save()")
    powershell(script)


def _shortcut_target(lnk: Path) -> str:
    try:
        return powershell(f"(New-Object -ComObject WScript.Shell).CreateShortcut({quote_ps(to_windows(lnk))}).TargetPath")
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return ""


def _free_link(folder: Path, base: str, ours: Path, distro: str) -> Path:
    """`Rocsy.lnk`, unless one that isn't this install's is already there."""
    for name in (f"{base}.lnk", f"{base} ({distro}).lnk", *(f"{base} ({distro} {n}).lnk" for n in range(2, 20))):
        candidate = folder / name
        if not candidate.exists() or _shortcut_target(candidate).lower().startswith(to_windows(ours).lower()):
            return candidate
    raise RuntimeError(f"No free shortcut name in {folder}.")


def install_shortcuts(dest: Path, distro: str, port: int, icon: Path, desktop: bool, autostart: bool) -> dict:
    """Windows shortcuts for this install; returns {'folder', 'links'} (Linux paths) for the marker."""
    folders = windows_folders()
    home = folders["LocalApplicationData"] / "Rocsy" / _folder_name(dest, distro)
    home.mkdir(parents=True, exist_ok=True)
    for name, data in launcher_scripts(dest, distro, port).items():
        (home / name).write_bytes(data)
    shutil.copy2(icon, home / "rocsy.ico")
    links = []
    if desktop:
        for key in ("Desktop", "Programs"):
            link = _free_link(folders[key], "Rocsy", home, distro)
            _shortcut(link, home / "start.cmd", home / "rocsy.ico", minimized=True)
            links.append(str(link))
    if autostart:
        link = _free_link(folders["Startup"], "Rocsy background", home, distro)
        _shortcut(link, home / "background.cmd", home / "rocsy.ico", minimized=True)
        links.append(str(link))
    return {"id": _install_id(dest, distro), "folder": str(home), "links": links}


def remove_shortcuts(record: dict | None, dest: Path) -> list[str]:
    """Remove what install_shortcuts() made for this install (`record` from the install marker).
    Only a folder whose start.cmd names this install, and `Rocsy…` links recorded with it."""
    if not isinstance(record, dict) or not str(record.get("id", "")).endswith(f":{dest}"):
        return []
    folder = Path(str(record.get("folder", "")))
    script = folder / "start.cmd"
    if not script.is_file() or f"{INSTALL_MARK}{record['id']}" not in script.read_text(errors="replace"):
        return []
    removed = []
    for raw in record.get("links") or []:
        link = Path(str(raw))
        if link.suffix.lower() == ".lnk" and link.name.startswith("Rocsy") and link.is_file():
            link.unlink()
            removed.append(str(link))
    shutil.rmtree(folder, ignore_errors=True)
    removed.append(str(folder))
    return removed
