"""Core Backend -> Updates -> Rocsy: update this Rocsy from its GitHub releases.

Only installed copies update themselves: a folder the installer made (.rocsy-install.json at
ROCSY_ROOT) that is not a git checkout. The master copy (a git checkout, or a developer build)
is updated with git, so the button is off there.

An update downloads the latest release's installer and SHA256SUMS, checks the hash, unpacks it
and runs it unattended in Update mode (`Install Rocsy.sh --defaults --dir ROCSY_ROOT`): new app
files and rebuilt containers; settings, passwords, data and the chosen services are kept. It runs
as its own systemd user unit, so restarting the gateway and this helper mid-update doesn't stop it.

GET  /core/extras/rocsy-update          installed version, latest release (cached 6 h), allowed?
POST /core/extras/rocsy-update/check    check GitHub now
POST /core/extras/rocsy-update/start    update to the latest release
GET  /core/extras/rocsy-update/log
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

from paths import COMPOSE_DIR, ROOT

REPO = os.getenv("ROCSY_UPDATE_REPO", "BittyBunnyBytes/Rocsy")
VERSION_FILE = COMPOSE_DIR / "VERSION"
MARKER = ROOT / ".rocsy-install.json"
STATE_DIR = Path(os.getenv("CORE_EXTRAS_STATE", ROOT / "core-backend-extras"))
WORK = STATE_DIR / "rocsy-update"
LOG_FILE = STATE_DIR / "rocsy-update.log"
CHECK_FILE = STATE_DIR / "rocsy-update.json"
UNIT = "core-rocsy-update"
MAX_DOWNLOAD = 512 * 1024 * 1024


def installed_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return ""


def _vkey(value: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", value)[:4])


def blocked_reason() -> str:
    """Why this copy can't update itself ("" = it can)."""
    if os.getenv("ROCSY_MASTER_COPY") == "1" or (ROOT / ".git").exists():
        return "This is the master copy (a git checkout): update it with git, not from releases."
    if (COMPOSE_DIR / "gateway" / "developer.py").exists():
        return "This is a developer build: update it from its source."
    if not MARKER.is_file():
        return "This Rocsy wasn't set up by the installer, so it can't update itself."
    return ""


def running() -> bool:
    result = subprocess.run(["systemctl", "--user", "is-active", UNIT + ".service"], capture_output=True, text=True)
    return result.stdout.strip() == "active"


def _api(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Rocsy-hostctl"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def check(force: bool = False) -> dict:
    try:
        cached = json.loads(CHECK_FILE.read_text())
    except (OSError, ValueError):
        cached = {}
    if not force and cached and time.time() - cached.get("at", 0) < 6 * 3600:
        return cached
    data = {"at": time.time(), "tag": "", "installer": "", "sums": "", "notes": "", "url": "", "error": ""}
    try:
        release = _api(f"https://api.github.com/repos/{REPO}/releases/latest")
        assets = {asset["name"]: asset["browser_download_url"] for asset in release.get("assets") or []}
        installer = next((name for name in assets if re.fullmatch(r"rocsy-v[0-9.]+-installer\.tar\.gz", name)), "")
        data.update(tag=str(release.get("tag_name") or ""), url=str(release.get("html_url") or ""),
                    notes=str(release.get("body") or "")[:4000],
                    installer=assets.get(installer, ""), sums=assets.get("SHA256SUMS", ""), installer_name=installer)
        if not installer or not data["sums"]:
            data["error"] = "The latest release has no installer or SHA256SUMS"
    except Exception as exc:  # noqa: BLE001 - shown on the row
        data["error"] = f"GitHub: {exc}"[:200]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CHECK_FILE.write_text(json.dumps(data))
    return data


def status(force: bool = False) -> dict:
    latest = check(force)
    current = installed_version()
    reason = blocked_reason()
    newer = bool(latest.get("tag") and current and _vkey(latest["tag"]) > _vkey(current))
    return {"version": current, "latest": latest.get("tag", ""), "release_url": latest.get("url", ""),
            "notes": latest.get("notes", ""), "error": latest.get("error", ""),
            "update_available": newer, "allowed": not reason, "blocked": reason, "running": running()}


def start() -> dict:
    reason = blocked_reason()
    if reason:
        return {"error": reason, "status": 409}
    if running():
        return {"error": "Rocsy is already updating", "status": 409}
    latest = check(force=True)
    if latest.get("error"):
        return {"error": latest["error"], "status": 502}
    if not (latest.get("tag") and _vkey(latest["tag"]) > _vkey(installed_version() or "0")):
        return {"error": "This Rocsy is already on the latest release", "status": 409}
    args = ["systemd-run", "--user", "--collect", "--quiet", f"--unit={UNIT}", "-p", "Nice=10",
            sys.executable, str(Path(__file__).resolve()), "run"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode:
        return {"error": (result.stderr or result.stdout).strip()[:300] or "Couldn't start the update", "status": 500}
    return {"message": f"Updating Rocsy to {latest['tag']}. The site restarts during the update; this takes a while."}


def _download(url: str, target: Path, output) -> None:
    output.write(f"Downloading {url}\n")
    output.flush()
    request = urllib.request.Request(url, headers={"User-Agent": "Rocsy-hostctl"})
    size = 0
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as handle:
        while chunk := response.read(1 << 20):
            size += len(chunk)
            if size > MAX_DOWNLOAD:
                raise RuntimeError("The download is larger than expected")
            handle.write(chunk)


def run_update() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("w") as output:
        try:
            output.write(f"=== Rocsy update ({time.strftime('%Y-%m-%d %H:%M:%S')}) from {installed_version() or 'unknown'} ===\n")
            reason = blocked_reason()
            if reason:
                raise RuntimeError(reason)
            latest = check(force=True)
            if latest.get("error"):
                raise RuntimeError(latest["error"])
            shutil.rmtree(WORK, ignore_errors=True)
            WORK.mkdir(parents=True)
            archive = WORK / latest["installer_name"]
            _download(latest["installer"], archive, output)
            sums = WORK / "SHA256SUMS"
            _download(latest["sums"], sums, output)
            expected = next((line.split()[0] for line in sums.read_text().splitlines()
                             if line.split() and line.split()[-1].lstrip("*") == archive.name), "")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if not expected or digest != expected:
                raise RuntimeError(f"SHA-256 mismatch for {archive.name}: the download was not used")
            output.write(f"SHA-256 verified: {digest}\n")
            with tarfile.open(archive) as bundle:
                bundle.extractall(WORK / "bundle", filter="data")
            launcher = WORK / "bundle" / "Rocsy Installer" / "Install Rocsy.sh"
            if not launcher.is_file():
                raise RuntimeError("The installer package has no 'Install Rocsy.sh'")
            output.write(f"\n=== Running the installer in Update mode for {ROOT} ===\n")
            output.flush()
            result = subprocess.run(["sh", str(launcher), "--defaults", "--dir", str(ROOT)], cwd=launcher.parent,
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                    env={**os.environ, "DISPLAY": "", "WAYLAND_DISPLAY": ""})
            if result.returncode:
                raise RuntimeError(f"The installer exited with {result.returncode} (see above)")
            output.write(f"\nRocsy is now {installed_version() or latest['tag']}.\n")
            shutil.rmtree(WORK, ignore_errors=True)
            return 0
        except Exception as exc:  # noqa: BLE001 - shown in the log
            output.write(f"\nUpdate failed: {exc}\nNothing was changed unless the installer had started.\n")
            return 1


def handle(method: str, path: str) -> tuple[int, dict]:
    if method == "GET" and path == "/core/extras/rocsy-update":
        return 200, status()
    if method == "GET" and path == "/core/extras/rocsy-update/log":
        return 200, {"log": LOG_FILE.read_text(errors="replace")[-64000:] if LOG_FILE.exists() else ""}
    if method == "POST" and path == "/core/extras/rocsy-update/check":
        return 200, status(force=True)
    if method == "POST" and path == "/core/extras/rocsy-update/start":
        reply = start()
        return (reply.pop("status"), reply) if "error" in reply else (202, reply)
    return 404, {"error": "not found"}


if __name__ == "__main__":
    if sys.argv[1:] == ["run"]:
        import dockeraccess  # the docker group of the installing session (see dockeraccess.py)
        dockeraccess.reexec_with_docker_group()
        sys.exit(run_update())
    sys.exit("usage: selfupdate.py run")
