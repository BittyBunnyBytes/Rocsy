"""Core Backend -> SillyTavern: Rocsy's optional SillyTavern service.

SillyTavern is the `sillytavern` service in BunnyBytes/docker-compose.yml (compose profile
"sillytavern"); ROOT/SillyTavern holds its README and the Rocsy add-ons (addons/), plus this
machine's own config/, data/ (chats, characters, keys), plugins/ and extensions/, which git and
the installer leave out. Turning it on adds the profile to COMPOSE_PROFILES in BunnyBytes/.env,
pulls the official image, starts it and recreates the gateway (which serves /SillyTavern only
while the profile is on: gateway/sillytavern.py). Turning it off undoes that and keeps the data.
Add-on packages (addons/<name>/ with a SillyTavern manifest.json) install into its third-party
extensions folder.

GET  /core/extras/sillytavern                       status (cheap; the GitHub check is cached)
POST /core/extras/sillytavern/check                 re-check the latest SillyTavern release
POST /core/extras/sillytavern/{install|update|off}
POST /core/extras/sillytavern/addon/<name>/{install|remove}
GET  /core/extras/sillytavern/log
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from paths import COMPOSE_DIR, ROOT

HOME = ROOT / "SillyTavern"
ADDONS_DIR = HOME / "addons"
ENV_FILE = COMPOSE_DIR / ".env"
PROFILE = "sillytavern"
SERVICE = "sillytavern"             # compose service
CONTAINER = "rocsy-sillytavern"     # its container_name
UPSTREAM = "http://127.0.0.1:8140"
STATE_DIR = Path(os.getenv("CORE_EXTRAS_STATE", ROOT / "core-backend-extras"))
LOG_FILE = STATE_DIR / "sillytavern.log"
ADDONS = {"rocsy-addons": "Rocsy Addons", "rocsy-visual-overhaul": "Rocsy Visual Overhaul"}
# Add-ons with a server half: addons/<name>/server/ goes to SillyTavern's plugins/<folder> (server plugins on).
SERVER_PLUGINS = {"rocsy-addons": "rocsy"}
DATA_DIRS = ("config", "data", "plugins", "extensions")
LOCK = threading.Lock()
_job: dict = {"name": "", "thread": None}
_latest: dict = {"at": 0.0, "tag": "", "error": ""}


class Refused(Exception):
    pass


# ------------------------------------------------------------------ status --

def _profiles() -> list[str]:
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError:
        return []
    current = next((line.partition("=")[2] for line in reversed(lines) if line.startswith("COMPOSE_PROFILES=")), "")
    return [name.strip() for name in current.strip().strip("\"'").split(",") if name.strip()]


def _set_profile(on: bool, output) -> None:
    profiles = _profiles()
    if (PROFILE in profiles) == on:
        return
    profiles = [*profiles, PROFILE] if on else [name for name in profiles if name != PROFILE]
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    kept = [line for line in lines if not line.startswith("COMPOSE_PROFILES=")]
    ENV_FILE.write_text("\n".join([*kept, "COMPOSE_PROFILES=" + ",".join(profiles)]) + "\n")
    output.write(f"COMPOSE_PROFILES={','.join(profiles)} in BunnyBytes/.env\n")


def _enabled() -> bool:
    return PROFILE in _profiles()


def _container() -> dict:
    try:
        result = subprocess.run(["docker", "inspect", "--format", "{{json .State}}", CONTAINER],
                                capture_output=True, text=True, timeout=10)
        return json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def _version() -> str:
    try:
        with urllib.request.urlopen(UPSTREAM + "/version", timeout=3) as response:
            return str(json.loads(response.read()).get("pkgVersion") or "")
    except Exception:  # noqa: BLE001 - not running, or starting
        return ""


def _latest_release(force: bool = False) -> dict:
    if force or time.time() - _latest["at"] > 6 * 3600:
        try:
            request = urllib.request.Request("https://api.github.com/repos/SillyTavern/SillyTavern/releases/latest",
                                             headers={"Accept": "application/json", "User-Agent": "Rocsy-hostctl"})
            with urllib.request.urlopen(request, timeout=20) as response:
                _latest.update(tag=str(json.loads(response.read()).get("tag_name") or ""), error="")
        except Exception as exc:  # noqa: BLE001
            _latest["error"] = f"{type(exc).__name__}: {exc}"[:200]
        _latest["at"] = time.time()
    return _latest


def _vkey(value: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", value)[:4])


def _addon_state(name: str) -> dict:
    package = ADDONS_DIR / name
    ready = (package / "manifest.json").is_file()
    return {"id": name, "label": ADDONS[name], "available": ready,
            "installed": (HOME / "extensions" / name).is_dir()}


def status(force: bool = False) -> dict:
    container = _container()
    installed = (HOME / "data").is_dir()
    enabled = _enabled()
    version = _version() if container.get("Status") == "running" else ""
    latest = _latest_release(force) if installed or enabled else {"tag": "", "error": ""}
    update = bool(version and latest["tag"] and _vkey(latest["tag"]) > _vkey(version))
    running = _job["thread"] is not None and _job["thread"].is_alive()
    return {"shipped": HOME.is_dir(), "installed": installed, "enabled": enabled,
            "container": container.get("Status", ""), "health": (container.get("Health") or {}).get("Status", ""),
            "version": version, "latest": latest["tag"], "latest_error": latest["error"], "update_available": update,
            "job": _job["name"] if running else "", "path": str(HOME),
            "addons": [_addon_state(name) for name in ADDONS]}


# ---------------------------------------------------------------- jobs --

def _run(output, *args: str, timeout: int = 1800) -> None:
    output.write(f"$ {' '.join(args)}\n")
    output.flush()
    result = subprocess.run(list(args), cwd=COMPOSE_DIR, stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} exited with {result.returncode}")


def _recreate_gateway(output) -> None:
    """The gateway reads COMPOSE_PROFILES at start, so it is recreated (not just restarted) to add or drop
    /SillyTavern. Delayed: this request itself came through the gateway."""
    output.write("Recreating the gateway in a moment so it picks up the change (this page reloads).\n")

    def later() -> None:
        time.sleep(3)
        subprocess.run(["docker", "compose", "up", "-d", "--no-deps", "--no-build", "gateway"], cwd=COMPOSE_DIR,
                       capture_output=True, timeout=300)
    threading.Thread(target=later, daemon=True).start()


def _wait_healthy(output, seconds: int = 120) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _container().get("Health", {}).get("Status") == "healthy" or _version():
            output.write("SillyTavern is up.\n")
            return
        time.sleep(3)
    output.write("SillyTavern hasn't answered yet; it may still be starting (see Settings → Services).\n")


def _install(output) -> None:
    if (HOME / "data").is_dir():
        output.write("Keeping your existing SillyTavern data (settings, chats and characters stay).\n")
    for name in DATA_DIRS:
        (HOME / name).mkdir(parents=True, exist_ok=True)
    _set_profile(True, output)
    _run(output, "docker", "compose", "pull", SERVICE)
    _run(output, "docker", "compose", "up", "-d", "--no-deps", SERVICE)
    _wait_healthy(output)
    _recreate_gateway(output)


def _update(output) -> None:
    _run(output, "docker", "compose", "pull", SERVICE)
    _run(output, "docker", "compose", "up", "-d", "--no-deps", SERVICE)
    _wait_healthy(output)
    _latest_release(force=True)


def _off(output) -> None:
    _run(output, "docker", "compose", "--profile", PROFILE, "rm", "--stop", "--force", SERVICE, timeout=300)
    _set_profile(False, output)
    output.write(f"Your SillyTavern data stays in {HOME}; Turn on picks it up again with everything in it.\n")
    _recreate_gateway(output)


def _addon(name: str, action: str, output) -> None:
    target = HOME / "extensions" / name
    package = ADDONS_DIR / name
    plugin = SERVER_PLUGINS.get(name)
    if action == "remove":
        shutil.rmtree(target, ignore_errors=True)
        if plugin:
            shutil.rmtree(HOME / "plugins" / plugin, ignore_errors=True)
            _run(output, "docker", "restart", CONTAINER, timeout=180)
        output.write(f"Removed {ADDONS[name]}. Reload SillyTavern to drop it.\n")
        return
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package, target, ignore=shutil.ignore_patterns("server", "*.bak*"))
    output.write(f"Installed {ADDONS[name]} into SillyTavern's third-party extensions.\n")
    if plugin and (package / "server").is_dir():
        destination = HOME / "plugins" / plugin
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(package / "server", destination, ignore=shutil.ignore_patterns("*.bak*"))
        output.write(f"Installed its server part into SillyTavern's plugins/{plugin}\n")
        _run(output, "docker", "restart", CONTAINER, timeout=180)
        _wait_healthy(output)
    output.write("Reload SillyTavern to load it.\n")


def _worker(name: str, func, *args) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("w") as output:
        output.write(f"=== SillyTavern: {name} ({time.strftime('%Y-%m-%d %H:%M:%S')}) ===\n")
        try:
            func(*args, output)
            output.write("\nDone.\n")
        except Exception as exc:  # noqa: BLE001 - shown in the log
            output.write(f"\nFailed: {exc}\n")


def start(name: str, addon: str = "") -> dict:
    with LOCK:
        if _job["thread"] and _job["thread"].is_alive():
            raise Refused(f"SillyTavern is busy ({_job['name']})")
        current = status()
        if not current["shipped"]:
            raise Refused("This Rocsy has no SillyTavern folder (ROOT/SillyTavern)")
        if name == "install":
            func, args, message = _install, (), "Installing SillyTavern…"
        elif name == "update":
            if not current["enabled"]:
                raise Refused("Install SillyTavern first")
            func, args, message = _update, (), "Updating SillyTavern…"
        elif name == "off":
            if not current["enabled"]:
                raise Refused("SillyTavern is already off")
            func, args, message = _off, (), "Turning SillyTavern off…"
        elif name in {"addon-install", "addon-remove"}:
            if addon not in ADDONS:
                raise Refused("Unknown add-on")
            if not current["installed"]:
                raise Refused("Install SillyTavern first")
            if name == "addon-install" and not _addon_state(addon)["available"]:
                raise Refused(f"{ADDONS[addon]} is empty for now; nothing to install yet")
            func, args = _addon, (addon, name.removeprefix("addon-"))
            message = f"{'Installing' if name == 'addon-install' else 'Removing'} {ADDONS[addon]}…"
        else:
            raise Refused("Unknown action")
        thread = threading.Thread(target=_worker, args=(name, func, *args), daemon=True)
        _job.update(name=name, thread=thread)
        thread.start()
    return {"message": message}


def handle(method: str, path: str) -> tuple[int, dict]:
    try:
        if method == "GET" and path == "/core/extras/sillytavern":
            return 200, status()
        if method == "GET" and path == "/core/extras/sillytavern/log":
            return 200, {"log": LOG_FILE.read_text(errors="replace")[-32000:] if LOG_FILE.exists() else ""}
        if method == "POST" and path == "/core/extras/sillytavern/check":
            return 200, status(force=True)
        match = re.fullmatch(r"/core/extras/sillytavern/(install|update|off)", path)
        if method == "POST" and match:
            return 202, start(match.group(1))
        match = re.fullmatch(r"/core/extras/sillytavern/addon/([a-z-]{1,40})/(install|remove)", path)
        if method == "POST" and match:
            return 202, start(f"addon-{match.group(2)}", match.group(1))
        return 404, {"error": "not found"}
    except Refused as exc:
        return 409, {"error": str(exc)}
