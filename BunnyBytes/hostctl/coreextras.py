"""Host-side backend downloads and updates for Model Manager."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, unquote, urlsplit
import yaml
import zipfile
from pathlib import Path

import comfyinstall
import dockeraccess
from paths import COMFY_ROOT, HERMES_HOME as ROCSY_HERMES_HOME, ROOT as ROCSY_ROOT

COMFY = Path(os.getenv("CORE_COMFY_DIR", COMFY_ROOT))
# The build script ships next to this file; the source checkout lives with the other Core Backend extras.
FLASH_SOURCE = Path(os.getenv("CORE_FLASH_SOURCE", ROCSY_ROOT / "core-backend-extras" / "flash-attention"))
FLASH_SCRIPT = Path(os.getenv("CORE_FLASH_SCRIPT", Path(__file__).resolve().with_name("build-rocm-flash-attention.sh")))
HERMES = Path(os.getenv("CORE_HERMES_DIR", ROCSY_ROOT / "hermes-rocsy"))
PERSONAL_HERMES = Path(os.getenv("CORE_PERSONAL_HERMES_DIR", ROCSY_HERMES_HOME / "hermes-agent"))
PERSONAL_LAUNCHER = Path(os.getenv("CORE_PERSONAL_HERMES_LAUNCHER", Path.home() / ".local/bin/hermes"))
COMPOSE_DIR = Path(os.getenv("CORE_COMPOSE_DIR", ROCSY_ROOT / "BunnyBytes"))
MOTION_PACKAGE = COMPOSE_DIR / "motion/package.json"
REA_VERSIONS = COMPOSE_DIR / "rea/versions.json"
HERMES_HOME = Path(os.getenv("CORE_HERMES_HOME", ROCSY_HERMES_HOME))
REA_MCP_URL = "http://127.0.0.1:8975/mcp"
HERMES_URL = "https://github.com/NousResearch/hermes-agent.git"
HERMES_PATCH = Path(__file__).resolve().parent / "patches/hermes/rocsy-addons.patch"
# The saved Rocsy patch was made against this commit. A fresh install must
# start here; the older stable tag does not contain all of its patch context.
HERMES_PATCH_BASE = "a12c3417c82d9495869962e320b4e73a907e849d"
STATE_DIR = Path(os.getenv("CORE_EXTRAS_STATE", ROCSY_ROOT / "core-backend-extras"))
FLASH_STATE = STATE_DIR / "flash.json"
HERMES_STATE = STATE_DIR / "hermes.json"
PERSONAL_HERMES_STATE = STATE_DIR / "personal-hermes.json"
HERMES_ADDON_STATE = STATE_DIR / "hermes-addons.json"
HERMES_ADDON_LOG = STATE_DIR / "hermes-addons.log"
_addon_lock = threading.Lock()
_catalog_lock = threading.Lock()
_catalog_cache: dict[str, object] = {"at": 0.0, "skills": [], "plugins": []}
MOTION_STATE = STATE_DIR / "motion.json"
REA_STATE = STATE_DIR / "rea.json"
REA_ENGINE_DIR = STATE_DIR / "rea-engines"
GODOTSTEAM_STATE = STATE_DIR / "rea-godotsteam.json"
GODOTSTEAM_VERSION = "3.5.2"
GODOTSTEAM_ARCHIVE = "linux64-g352-s158-gs321.zip"
GODOTSTEAM_URL = f"https://codeberg.org/godotsteam/godotsteam/releases/download/v3.21/{GODOTSTEAM_ARCHIVE}"
GODOTSTEAM_SHA256 = "19d627a4a31057eca6848abcd03b722e020bb60d1bf110f0e61a7d94490c688b"
GODOT_STATE = STATE_DIR / "rea-godot.json"
GODOT_DIR = REA_ENGINE_DIR
GODOT_VERSION_RE = re.compile(r"(?:3|4)\.[0-9]+(?:\.[0-9]+)?")
PORTABLE_ENGINE_DIR = REA_ENGINE_DIR
PORTABLE_ENGINES = {
    "love2d": {"label": "LÖVE", "version": "11.5", "filename": "love-11.5-x86_64.AppImage",
               "url": "https://github.com/love2d/love/releases/download/11.5/love-11.5-x86_64.AppImage",
               "size": 5072064, "asset_id": 138701701, "kind": "appimage"},
    "tic80": {"label": "TIC-80", "version": "1.2.0", "filename": "tic80-v1.2-linux.zip",
              "url": "https://github.com/nesbox/TIC-80/releases/download/v1.2.0/tic80-v1.2-linux.zip",
              "size": 5432275, "sha256": "7f29fed516d81140515cacfd836ca9deedd4af430297a97d14d04795497d00a0",
              "kind": "zip"},
}
# REA Tool Addons: single-file helper tools (not runtimes), pinned to an official
# release asset and installed into the same read-only REA mount, under addons/.
REA_ADDON_DIR = REA_ENGINE_DIR / "addons"
REA_ADDONS = {
    "godotpcktool": {"label": "GodotPckTool", "version": "2.3", "filename": "godotpcktool",
                     "repo": "hhyyrylainen/GodotPckTool", "tag": "v2.3",
                     "url": "https://github.com/hhyyrylainen/GodotPckTool/releases/download/v2.3/godotpcktool",
                     "size": 1683784,
                     "sha256": "1add2c22aeacdd7a8372288c885ed0ff4d2f673787aed924c54522a0f000d655",
                     "use": "List, extract, add to and repack Godot .pck files"},
}
JOBS = {"comfy-download": "core-comfy-download", "comfy-update": "core-comfy-update", "flash-install": "core-flash-install",
        "comfy-supervisor": "core-comfy-supervisor", "trainer-install": "core-trainer-install",
        "hermes-download": "core-hermes-download", "hermes-update": "core-hermes-update",
        "personal-hermes-update": "core-personal-hermes-update", "motion-install": "core-motion-install",
        "rea-install": "core-rea-install", "rea-update": "core-rea-update",
        "rea-godotsteam-download": "core-rea-godotsteam-download",
        "rea-godot-download": "core-rea-godot-download",
        "rea-love2d-download": "core-rea-love2d-download",
        "rea-tic80-download": "core-rea-tic80-download",
        **{f"rea-{addon_id}-download": f"core-rea-{addon_id}-download" for addon_id in REA_ADDONS},
        "ytdlp-install": "core-ytdlp-install"}
LOCK = threading.Lock()
ARCH_RE = re.compile(r"gfx[0-9a-z]+")
UPDATE_CHECKS: dict[str, dict] = {}
UPDATE_REFRESHING: set[str] = set()
REFRESH_LOCK = threading.Lock()
_ARCHS: dict = {"at": 0.0, "archs": []}


class Refused(Exception):
    pass


def gpu_archs() -> list[str]:
    """Read the currently visible ROCm devices from ComfyUI's own Python (cached: importing torch takes seconds)."""
    if _ARCHS["archs"] and time.monotonic() - _ARCHS["at"] < 600:
        return _ARCHS["archs"]
    python = COMFY / ".venv/bin/python3"
    if not python.is_file():
        return []
    code = "import torch; print(';'.join(torch.cuda.get_device_properties(i).gcnArchName.split(':')[0] for i in range(torch.cuda.device_count())))"
    try:
        result = subprocess.run([str(python), "-c", code], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    archs = sorted(set(ARCH_RE.findall(result.stdout))) if result.returncode == 0 else []
    _ARCHS.update(at=time.monotonic(), archs=archs)
    return archs


def flash_installed() -> bool:
    """Check the ComfyUI environment, including installs made outside this manager."""
    python = COMFY / ".venv/bin/python3"
    if not python.is_file():
        return False
    try:
        result = subprocess.run([str(python), "-c", "import importlib.util; assert importlib.util.find_spec('flash_attn')"],
                                capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


_ACTIVE: dict = {"at": 0.0, "jobs": set()}


def running(job: str) -> bool:
    """One `systemctl is-active` for every job unit, reused for a second (a status view asks ~17 times)."""
    if time.monotonic() - _ACTIVE["at"] > 1:
        result = subprocess.run(["systemctl", "--user", "is-active", *(unit + ".service" for unit in JOBS.values())],
                                capture_output=True, text=True)
        _ACTIVE.update(at=time.monotonic(), jobs={name for name, state in zip(JOBS, result.stdout.split()) if state == "active"})
    return job in _ACTIVE["jobs"]


def state() -> dict:
    try:
        return json.loads(FLASH_STATE.read_text())
    except (OSError, ValueError):
        return {}


def hermes_state() -> dict:
    try:
        return json.loads(HERMES_STATE.read_text())
    except (OSError, ValueError):
        return {}


def personal_hermes_state() -> dict:
    try:
        return json.loads(PERSONAL_HERMES_STATE.read_text())
    except (OSError, ValueError):
        return {}


def _addon_state() -> dict:
    try:
        return json.loads(HERMES_ADDON_STATE.read_text())
    except (OSError, ValueError):
        return {"phase": "idle"}


def _addon_command(*args: str, timeout: int = 45) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", "-u", "1000:1000", "hermes-agent", "hermes", *args],
                          capture_output=True, text=True, timeout=timeout)


def hermes_extension_status() -> dict:
    installed = (HERMES / ".git").exists()
    try:
        running = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", "hermes-agent"],
                                 capture_output=True, text=True, timeout=8).stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        running = False
    try:
        lock = json.loads((HERMES_HOME / "skills/.hub/lock.json").read_text())
        skills = {name: entry.get("identifier", "") for name, entry in lock.get("installed", {}).items()
                  if isinstance(entry, dict)}
    except (OSError, ValueError):
        skills = {}
    job = _addon_state()
    if job.get("phase") == "running" and time.time() - job.get("started", 0) > 1000:
        job = {**job, "phase": "failed", "error": "The add-on installer stopped before reporting a result"}
        HERMES_ADDON_STATE.write_text(json.dumps(job))
    return {"installed": installed, "running": running, "skills": skills, "job": job}


def _hermes_local_catalog() -> dict[str, list[dict]]:
    """Read the catalogs shipped with Hermes once per minute; browsing needs no CLI startup."""
    with _catalog_lock:
        if time.monotonic() - float(_catalog_cache["at"]) < 60:
            return _catalog_cache
        skills: list[dict] = []
        for path in (HERMES / "optional-skills").glob("*/*/SKILL.md"):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                header = content.split("---", 2)[1]
                meta = yaml.safe_load(header) or {}
                rel = path.parent.relative_to(HERMES / "optional-skills")
                linked_repo = re.search(r"https://github\.com/([A-Za-z0-9-]+)/[A-Za-z0-9._-]+", content)
                icon_owner = linked_repo.group(1) if linked_repo else ""
                if icon_owner.lower() in {"user", "owner", "username", "your-org", "yourname", "example", "org"}:
                    icon_owner = ""
                skills.append({"name": str(meta.get("name") or path.parent.name),
                               "identifier": f"official/{rel.as_posix()}",
                               "description": str(meta.get("description") or "Official Hermes skill"),
                               "source": "official", "trust_level": "builtin",
                               "category": rel.parts[0],
                               "repo": f"https://github.com/NousResearch/hermes-agent/tree/main/optional-skills/{rel.as_posix()}",
                               "icon_owner": icon_owner})
            except (OSError, ValueError, TypeError, AttributeError, IndexError, yaml.YAMLError):
                continue
        plugins: list[dict] = []
        for path in (HERMES / "plugin-catalog").glob("*.yaml"):
            try:
                meta = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if not isinstance(meta, dict) or not meta.get("name"):
                    continue
                if meta.get("platforms") and "linux" not in meta["platforms"]:
                    continue
                plugins.append({key: meta.get(key, "") for key in
                                ("name", "title", "description", "category", "tier", "version",
                                 "repo", "image", "maintainer", "platforms", "capabilities")})
            except (OSError, ValueError, TypeError, yaml.YAMLError):
                continue
        skills.sort(key=lambda entry: (entry["category"], entry["name"].lower()))
        plugins.sort(key=lambda entry: (entry.get("tier") != "official", entry["name"].lower()))
        _catalog_cache.update(at=time.monotonic(), skills=skills, plugins=plugins)
        return _catalog_cache


def hermes_addons_catalog(kind: str, query: str) -> dict:
    if kind not in {"skills", "plugins"} or len(query) > 120:
        raise Refused("Invalid add-on catalog request")
    if not (HERMES / ".git").exists():
        raise Refused("Rocsy Hermes is not installed")
    items = _hermes_local_catalog()[kind]
    terms = query.lower().split()
    if terms:
        items = [entry for entry in items if all(term in " ".join(str(entry.get(key) or "") for key in
                    ("name", "title", "description", "category", "identifier", "maintainer")).lower() for term in terms)]
    try:
        lock = json.loads((HERMES_HOME / "skills/.hub/lock.json").read_text())
        skill_installed = {name: record.get("identifier", "") for name, record in lock.get("installed", {}).items()
                           if isinstance(record, dict)}
    except (OSError, ValueError):
        skill_installed = {}
    try:
        records = json.loads((HERMES_HOME / "plugins/.install-metadata.json").read_text())
        plugin_installed = {record["catalog"]["name"]: "installed" for record in records.values()
                            if isinstance(record, dict) and isinstance(record.get("catalog"), dict)
                            and record["catalog"].get("name")}
    except (OSError, ValueError):
        plugin_installed = {}
    return {"results": items[:120], "total": len(items),
            "installed": skill_installed if kind == "skills" else plugin_installed}


def hermes_addons_search(kind: str, query: str) -> dict:
    if kind not in {"skills", "plugins"} or len(query) > 120:
        raise Refused("Invalid add-on search")
    status = hermes_extension_status()
    if not status["running"]:
        raise Refused("Rocsy Hermes must be running to search add-ons")
    if kind == "skills":
        if not query.strip():
            return {"results": [], "installed": status["skills"]}
        args = ("skills", "search", query, "--limit", "30", "--json")
    else:
        args = ("plugins", "search", query, "--json")
    result = _addon_command(*args, timeout=60)
    if result.returncode:
        raise Refused((result.stderr or result.stdout or "Search failed")[-500:])
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise Refused("Hermes returned an unreadable catalog response") from exc
    installed = status["skills"]
    if kind == "plugins":
        listed = _addon_command("plugins", "list", "--json", timeout=30)
        if listed.returncode == 0:
            try:
                installed = {row["name"]: row.get("status", "") for row in json.loads(listed.stdout)}
            except (ValueError, TypeError, KeyError):
                installed = {}
    results = payload if kind == "skills" else payload.get("results", [])
    if kind == "plugins":
        results = [entry for entry in results if not entry.get("platforms") or "linux" in entry["platforms"]]
        results.sort(key=lambda entry: (entry.get("tier") != "official", entry.get("name", "")))
    return {"results": results[:60], "total": len(results), "installed": installed}


def _install_hermes_addon(kind: str, identifier: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HERMES_ADDON_LOG.open("w") as log:
        try:
            args = (["skills", "install", identifier, "--yes"] if kind == "skills" else
                    ["plugins", "install", identifier, "--no-enable", "--yes-deps"])
            result = subprocess.run(["docker", "exec", "-u", "1000:1000", "hermes-agent", "hermes", *args],
                                    stdout=log, stderr=subprocess.STDOUT, timeout=900)
            outcome = {"phase": "complete" if result.returncode == 0 else "failed",
                       "kind": kind, "identifier": identifier, "finished": time.time()}
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.write(f"\n{exc}\n")
            outcome = {"phase": "failed", "kind": kind, "identifier": identifier,
                       "error": str(exc)[:300], "finished": time.time()}
        HERMES_ADDON_STATE.write_text(json.dumps(outcome))


def hermes_addons_install(kind: str, identifier: str) -> dict:
    if kind not in {"skills", "plugins"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~:/?#=&%+@-]{0,499}", identifier):
        raise Refused("Invalid add-on identifier")
    if not hermes_extension_status()["running"]:
        raise Refused("Rocsy Hermes must be running to install add-ons")
    # Plugins must come from Hermes's reviewed catalog. Skills retain Hermes's
    # own source resolution and security scan, but arbitrary URLs are excluded.
    if kind == "plugins":
        catalog_result = _addon_command("plugins", "search", identifier, "--json", timeout=60)
        if catalog_result.returncode:
            raise Refused("Hermes plugin catalog is unavailable")
        try:
            catalog = json.loads(catalog_result.stdout).get("results", [])
        except ValueError as exc:
            raise Refused("Hermes returned an unreadable plugin catalog") from exc
        if identifier not in {entry.get("name") for entry in catalog}:
            raise Refused("Plugin is not in the Hermes catalog")
    with _addon_lock:
        if _addon_state().get("phase") == "running":
            raise Refused("An add-on is already downloading")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HERMES_ADDON_STATE.write_text(json.dumps({"phase": "running", "kind": kind,
                                                  "identifier": identifier, "started": time.time()}))
        threading.Thread(target=_install_hermes_addon, args=(kind, identifier), daemon=True).start()
    return {"message": f"Downloading {identifier}", "job": _addon_state()}


def trainer_status() -> dict:
    """The Trainer container (opt-in: Compose profile "trainer"), without building anything."""
    try:
        result = subprocess.run(["docker", "inspect", "--format", "{{json .State}}", "bunny-trainer"],
                                capture_output=True, text=True, timeout=10)
        container = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        container = {}
    return {"installed": bool(container), "running": container.get("Status") == "running",
            "install_running": running("trainer-install")}


def motion_status() -> dict:
    """Report the installed Motion container without pulling or building anything."""
    try:
        result = subprocess.run(["docker", "inspect", "--format", "{{json .State}}", "bunny-motion"],
                                capture_output=True, text=True, timeout=10)
        container = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        container = {}
    try:
        version = json.loads(MOTION_PACKAGE.read_text())["dependencies"]["remotion"]
    except (OSError, ValueError, KeyError):
        version = ""
    try:
        meta = json.loads(MOTION_STATE.read_text())
    except (OSError, ValueError):
        meta = {}
    health = container.get("Health") or {}
    return {"installed": bool(container), "running": container.get("Status") == "running",
            "healthy": health.get("Status") == "healthy", "health": health.get("Status", ""),
            "version": version, "install_running": running("motion-install"),
            "phase": meta.get("phase", ""), "error": meta.get("error", "")}


# ---- REA (Reverse Engineer Anything): REA, its MCP bridge and prerequisites in bunny-rea ----
# Update rule: REA's own release pins Ghidra, the JADX bridge JAR and WABT; those follow the newest
# REA release and never move on their own. Apktool, Rizin, Wakaru (same major as now) and
# supergateway follow their latest releases. Debian tools refresh on every rebuild.
REA_TOOLS = {"rea-agents": "REA", "ghidra": "Ghidra", "jadx-headless-mcp": "JADX bridge", "wabt": "WABT",
             "apktool": "Apktool", "rizin": "Rizin", "wakaru": "Wakaru", "supergateway": "MCP bridge",
             "dotnet-sdk": ".NET SDK", "ilspycmd": "ILSpy", "netfx-refs": ".NET Fx refs", "bepinex-win-x64": "BepInEx 5",
             "bepinex6-il2cpp-win-x64": "BepInEx 6 IL2CPP", "ue4ss": "UE4SS", "gdre": "GDRE Tools", "unrpyc": "unrpyc",
             "asar": "asar"}
# Updated with bepinex-win-x64 but not listed separately in the Manager.
REA_FOLLOWERS = {"bepinex-win-x86": "bepinex-win-x64", "bepinex-linux-x64": "bepinex-win-x64",
                 "bepinex6-il2cpp-linux-x64": "bepinex6-il2cpp-win-x64"}
TOFU: dict[str, str] = {}  # url -> sha256 of downloads whose source publishes no checksum
REA_CHECK: dict = {}
REA_CHECK_LOCK = threading.Lock()
REA_MANIFEST: dict = {}


# ---- yt-dlp: one managed copy for the Master Agent ----
# The official zipapp from yt-dlp's latest GitHub release (SHA-256 checked), in a folder that
# Hermes (its terminal, on PATH) and the Agent service (import-media) mount read-only, so an
# update here reaches both without a rebuild. Sites change often: update when downloads break.
YTDLP_DIR = STATE_DIR / "yt-dlp"
YTDLP_BIN = YTDLP_DIR / "yt-dlp"
YTDLP_STATE = STATE_DIR / "ytdlp.json"


def ytdlp_status() -> dict:
    try:
        meta = json.loads(YTDLP_STATE.read_text())
    except (OSError, ValueError):
        meta = {}
    installed = YTDLP_BIN.is_file()
    status = {"installed": installed, "version": meta.get("version", "") if installed else "", "path": str(YTDLP_BIN),
              "running": running("ytdlp-install"), "phase": meta.get("phase", ""), "error": meta.get("error", ""),
              "latest": "", "update_available": False, "update_error": ""}
    cached = UPDATE_CHECKS.get("ytdlp")
    if not cached or time.monotonic() - cached["at"] > (60 if cached["error"] else 3600):
        latest, error = "", ""
        try:
            latest = github_asset("yt-dlp/yt-dlp", None, r"yt-dlp", r"^(\d{4}\.\d{2}\.\d{2}(?:\.\d+)?)$")["version"]
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            error = str(exc)[:200]
        cached = UPDATE_CHECKS["ytdlp"] = {"at": time.monotonic(), "latest": latest, "error": error}
    status.update(latest=cached["latest"], update_error=cached["error"],
                  update_available=bool(installed and cached["latest"] and cached["latest"] != status["version"]))
    return status


def run_ytdlp(output) -> int:
    meta = {"phase": "downloading", "started": time.time(), "error": ""}
    YTDLP_STATE.write_text(json.dumps(meta))
    try:
        target = github_asset("yt-dlp/yt-dlp", None, r"yt-dlp", r"^(\d{4}\.\d{2}\.\d{2}(?:\.\d+)?)$")
        output.write(f"yt-dlp {target['version']}: {target['url']}\n")
        data = fetch(target["url"], "application/octet-stream")
        if hashlib.sha256(data).hexdigest() != target["sha256"]:
            raise RuntimeError("SHA-256 mismatch: the download was not the published file")
        YTDLP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = YTDLP_DIR / ".yt-dlp.new"
        tmp.write_bytes(data)
        tmp.chmod(0o755)
        check = subprocess.run([sys.executable, str(tmp), "--version"], capture_output=True, text=True, timeout=60)
        if check.returncode or not check.stdout.strip():
            raise RuntimeError("the new yt-dlp does not run: " + (check.stderr or check.stdout).strip()[:200])
        os.replace(tmp, YTDLP_BIN)
        meta.update(phase="ready", version=check.stdout.strip(), finished=time.time())
        YTDLP_STATE.write_text(json.dumps(meta))
        UPDATE_CHECKS.pop("ytdlp", None)
        output.write(f"Installed yt-dlp {meta['version']} at {YTDLP_BIN}\n")
        return 0
    except Exception as exc:
        meta.update(phase="failed", error=str(exc)[:300], finished=time.time())
        YTDLP_STATE.write_text(json.dumps(meta))
        output.write(meta["error"] + "\n")
        return 1


def fetch(url: str, accept: str = "application/json") -> bytes:
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "rocsy-hostctl"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def npm_latest(package: str) -> dict:
    return json.loads(fetch(f"https://registry.npmjs.org/{package.replace('/', '%2F')}"))


def github_asset(repo: str, tag: str | None, name_re: str, version_re: str) -> dict:
    release = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases/" + (f"tags/{tag}" if tag else "latest"),
                               "application/vnd.github+json"))
    version = re.search(version_re, release["tag_name"])
    for asset in release.get("assets", []):
        if re.fullmatch(name_re, asset["name"]) and str(asset.get("digest", "")).startswith("sha256:"):
            if not version:
                break
            return {"version": version.group(1), "url": asset["browser_download_url"],
                    "sha256": asset["digest"].split(":", 1)[1]}
    raise RuntimeError(f"{repo} {release.get('tag_name')}: no checksummed asset matching {name_re}")


def rea_targets(pins: dict) -> dict:
    """The newest compatible version of every REA component (see the rule above)."""
    rea = npm_latest("rea-agents")["dist-tags"]["latest"]

    def pinned(path: str, pattern: str) -> re.Match | None:
        """A pin in that REA release's source; None when the release doesn't have the file yet."""
        try:
            text = fetch(f"https://raw.githubusercontent.com/morluto/rea/rea-agents-{rea}/{path}", "text/plain").decode()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return re.search(pattern, text)

    ghidra = pinned("src/ghidra/GhidraInstallationPolicy.ts", r'SUPPORTED_GHIDRA_VERSION = "([0-9.]+)"')
    ghidra = ghidra.group(1) if ghidra else pins["ghidra"]["version"]
    jadx = pinned("src/android/JadxRelease.ts", r'version: "([^"]+)",\s*revision: "[^"]+",\s*sha256: "([0-9a-f]{64})",\s*url: "(https://github\.com/[^"]+)"')
    jadx = {"version": jadx.group(1), "sha256": jadx.group(2), "url": jadx.group(3)} if jadx else pins["jadx-headless-mcp"]
    wabt = pinned("src/wasm/WabtRelease.ts", r"wabt@([0-9.]+)")
    wabt = wabt.group(1) if wabt else pins["wabt"]["version"]
    wakaru_major = pinned("docs/javascript-recovery.md", r"Wakaru \*\*\^(\d+)\.")
    wakaru_major = wakaru_major.group(1) if wakaru_major else pins["wakaru"]["version"].split(".")[0]
    wakaru = npm_latest("@wakaru/cli")
    wakaru_versions = [v for v in wakaru["versions"] if re.fullmatch(rf"{wakaru_major}\.\d+\.\d+", v)]
    targets = {
        "rea-agents": {"version": rea},
        "supergateway": {"version": npm_latest("supergateway")["dist-tags"]["latest"]},
        "wakaru": {"version": max(wakaru_versions, key=lambda v: tuple(map(int, v.split("."))))
                   if wakaru_versions else pins["wakaru"]["version"]},
        "ghidra": github_asset("NationalSecurityAgency/ghidra", f"Ghidra_{ghidra}_build",
                               r"ghidra_[0-9.]+_PUBLIC_\d+\.zip", r"Ghidra_([0-9.]+)_build"),
        "jadx-headless-mcp": jadx,
        "wabt": github_asset("WebAssembly/wabt", wabt, rf"wabt-{re.escape(wabt)}-linux-x64\.tar\.gz", r"([0-9.]+)"),
        "apktool": github_asset("iBotPeaches/Apktool", None, r"apktool_[0-9.]+\.jar", r"v?([0-9.]+)$"),
        "rizin": github_asset("rizinorg/rizin", None, r"rizin-v[0-9.]+-static-x86_64\.tar\.xz", r"v?([0-9.]+)$"),
    }
    # Modding toolchain: newest .NET SDK in the pinned major, newest stable ILSpy/ref assemblies,
    # newest BepInEx 5 (6 is a different loader, so a 6.x release never replaces 5).
    sdk_major = pins["dotnet-sdk"]["version"].split(".")[0]
    channel = json.loads(fetch(f"https://dotnetcli.blob.core.windows.net/dotnet/release-metadata/{sdk_major}.0/releases.json"))
    release = next(r for r in channel["releases"] if r.get("sdk", {}).get("version") == channel["latest-sdk"])
    sdk = next(f for f in release["sdk"]["files"] if f["rid"] == "linux-x64" and f["name"].endswith(".tar.gz"))
    targets["dotnet-sdk"] = {"version": channel["latest-sdk"], "url": sdk["url"], "sha512": sdk["hash"].lower()}

    def nuget_stable(package: str) -> str:
        versions = json.loads(fetch(f"https://api.nuget.org/v3-flatcontainer/{package}/index.json"))["versions"]
        return [v for v in versions if "-" not in v][-1]
    targets["ilspycmd"] = {"version": nuget_stable("ilspycmd")}
    targets["netfx-refs"] = {"version": nuget_stable("microsoft.netframework.referenceassemblies")}
    for flavor in ("win-x64", "win-x86", "linux-x64"):
        asset = github_asset("BepInEx/BepInEx", None, rf"BepInEx_{flavor.replace('-', '_')}_5\.[0-9.]+\.zip", r"v?(5\.[0-9.]+)$")
        targets[f"bepinex-{flavor}"] = asset

    # Other engines' mod loaders and extractors.
    targets["gdre"] = github_asset("GDRETools/gdsdecomp", None, r"GDRE_tools-v[0-9.]+-linux\.zip", r"v?([0-9.]+)$")
    release = json.loads(fetch("https://api.github.com/repos/UE4SS-RE/RE-UE4SS/releases/tags/experimental-latest", "application/vnd.github+json"))
    ue = next(a for a in release["assets"] if re.fullmatch(r"UE4SS_v[0-9.]+-[0-9]+-g[0-9a-f]+\.zip", a["name"]))
    targets["ue4ss"] = {"version": re.search(r"UE4SS_v(.+)\.zip", ue["name"]).group(1), "url": ue["browser_download_url"],
                        "sha256": ue["digest"].split(":", 1)[1]}
    page = fetch("https://builds.bepinex.dev/projects/bepinex_be", "text/html").decode()
    for flavor in ("win-x64", "linux-x64"):
        link = re.search(rf'href="(/projects/bepinex_be/(\d+)/BepInEx-Unity\.IL2CPP-{flavor}-6\.0\.0-be\.\d+[^"]*\.zip)"', page)
        key, pin = f"bepinex6-il2cpp-{flavor}", pins.get(f"bepinex6-il2cpp-{flavor}", {})
        if not link:
            targets[key] = pin
            continue
        version, url = f"6.0.0-be.{link.group(2)}", "https://builds.bepinex.dev" + link.group(1)
        targets[key] = pin if version == pin.get("version") else {"version": version, "url": url, "sha256": tofu_sha256(url)}
    tag = json.loads(fetch("https://api.github.com/repos/CensoredUsername/unrpyc/releases/latest", "application/vnd.github+json"))["tag_name"]
    version = tag.lstrip("v")
    if version == pins.get("unrpyc", {}).get("version"):
        targets["unrpyc"] = pins["unrpyc"]
    else:
        url = f"https://github.com/CensoredUsername/unrpyc/archive/refs/tags/{tag}.tar.gz"
        targets["unrpyc"] = {"version": version, "url": url, "sha256": tofu_sha256(url)}
    targets["asar"] = {"version": npm_latest("@electron/asar")["dist-tags"]["latest"]}
    return targets


def tofu_sha256(url: str, limit: int = 300 * 1024 * 1024) -> str:
    """SHA-256 of a download whose publisher gives no checksum, taken once over HTTPS and then pinned."""
    if url in TOFU:
        return TOFU[url]
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "rocsy-hostctl"})
    with urllib.request.urlopen(request, timeout=120) as response:
        total = 0
        while chunk := response.read(1 << 20):
            total += len(chunk)
            if total > limit:
                raise RuntimeError(f"{url} is larger than expected")
            digest.update(chunk)
    TOFU[url] = digest.hexdigest()
    return TOFU[url]


def rea_pins() -> dict:
    try:
        return json.loads(REA_VERSIONS.read_text())
    except (OSError, ValueError):
        return {}


def rea_check(force: bool = False) -> dict:
    """Check upstream in a background thread; callers get the last result immediately."""
    with REA_CHECK_LOCK:
        stale = time.time() - REA_CHECK.get("at", 0) > (600 if REA_CHECK.get("error") else 6 * 3600)
        if (force or stale) and not REA_CHECK.get("running") and REA_VERSIONS.is_file():
            REA_CHECK["running"] = True

            def work() -> None:
                try:
                    result = {"targets": rea_targets(rea_pins()), "error": ""}
                except Exception as exc:  # network, rate limit, moved source files
                    result = {"targets": REA_CHECK.get("targets", {}), "error": str(exc)[:300]}
                with REA_CHECK_LOCK:
                    REA_CHECK.update(result, at=time.time(), running=False)
            threading.Thread(target=work, daemon=True).start()
        return dict(REA_CHECK)


def rea_manifest(container_id: str) -> dict:
    if REA_MANIFEST.get("id") != container_id:
        try:
            result = subprocess.run(["docker", "exec", "bunny-rea", "cat", "/opt/rea-tools/manifest.json"],
                                    capture_output=True, text=True, timeout=10)
            data = json.loads(result.stdout) if result.returncode == 0 else {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            data = {}
        if data:
            REA_MANIFEST.clear()
            REA_MANIFEST.update(id=container_id, data=data)
        return data
    return REA_MANIFEST["data"]


def rea_hermes_registered() -> bool:
    try:
        return REA_MCP_URL in (HERMES_HOME / "config.yaml").read_text()
    except OSError:
        return False


REA_ENGINES = {"java", "chromium", "chrome", "tshark", "rizin", "gdb", "binwalk", "sigrok-cli", "adb", "wasm-objdump"}


def rea_busy() -> list[str]:
    """Analysis engines running inside bunny-rea (Ghidra/JADX/Apktool run as java)."""
    try:
        result = subprocess.run(["docker", "exec", "bunny-rea", "ps", "-eo", "comm="],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return sorted({name for name in result.stdout.split() if name in REA_ENGINES}) if result.returncode == 0 else []


def rea_cached_engines() -> tuple[list[str], bool]:
    """List runtimes that REA downloaded into its own test volume."""
    try:
        result = subprocess.run(["docker", "exec", "bunny-rea", "find", "/home/node/rea-test/engines",
                                 "-mindepth", "1", "-maxdepth", "1", "-type", "d", "-printf", "%f\n"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return [], False
    if result.returncode:
        return [], False
    names = result.stdout.splitlines()
    versions = sorted((name.removeprefix("godot-") for name in names
                       if name.startswith("godot-") and GODOT_VERSION_RE.fullmatch(name.removeprefix("godot-"))),
                      key=lambda v: tuple(map(int, v.split("."))))
    return versions, f"godotsteam-local-{GODOTSTEAM_VERSION}" in names


def portable_engine_status() -> list[dict]:
    result = []
    for engine_id, spec in PORTABLE_ENGINES.items():
        target = PORTABLE_ENGINE_DIR / f"{engine_id}-{spec['version']}"
        try:
            manifest = json.loads((target / "manifest.json").read_text())
        except (OSError, ValueError):
            manifest = {}
        binary = manifest.get("binary", "")
        rel = Path(binary) if isinstance(binary, str) else Path()
        installed = bool(binary and not rel.is_absolute() and ".." not in rel.parts and
                         manifest.get("version") == spec["version"] and not target.is_symlink() and
                         (target / rel).is_file())
        try:
            meta = json.loads((STATE_DIR / f"rea-{engine_id}.json").read_text())
        except (OSError, ValueError):
            meta = {}
        result.append({"id": engine_id, "label": spec["label"], "version": spec["version"],
                       "size_mb": round(spec["size"] / 1048576, 1), "installed": installed,
                       "path": f"/home/node/rea-test/engines/portable/{target.name}/{binary}" if installed else "",
                       "running": running(f"rea-{engine_id}-download"), "phase": meta.get("phase", ""),
                       "error": meta.get("error", "")})
    return result


def rea_addon_status() -> list[dict]:
    result = []
    for addon_id, spec in REA_ADDONS.items():
        target = REA_ADDON_DIR / f"{addon_id}-{spec['version']}"
        try:
            manifest = json.loads((target / "manifest.json").read_text())
        except (OSError, ValueError):
            manifest = {}
        binary = target / spec["filename"]
        installed = (manifest.get("version") == spec["version"] and manifest.get("sha256") == spec["sha256"] and
                     not target.is_symlink() and binary.is_file() and not binary.is_symlink())
        try:
            meta = json.loads((STATE_DIR / f"rea-{addon_id}.json").read_text())
        except (OSError, ValueError):
            meta = {}
        result.append({"id": addon_id, "label": spec["label"], "version": spec["version"], "use": spec["use"],
                       "repo": spec["repo"], "size_mb": round(spec["size"] / 1048576, 1), "installed": installed,
                       "path": f"/home/node/rea-test/engines/portable/addons/{target.name}/{spec['filename']}" if installed else "",
                       "running": running(f"rea-{addon_id}-download"), "phase": meta.get("phase", ""),
                       "error": meta.get("error", "")})
    return result


def rea_status() -> dict:
    try:
        result = subprocess.run(["docker", "inspect", "--format", "{{json .}}", "bunny-rea"],
                                capture_output=True, text=True, timeout=10)
        container = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        container = {}
    state = container.get("State") or {}
    manifest = rea_manifest(container.get("Id", "")) if state.get("Status") == "running" else {}
    installed = {**manifest.get("downloads", {}), **manifest.get("npm", {})}
    pins = rea_pins()
    check = rea_check()
    targets = check.get("targets") or {}
    tools = [{"id": key, "label": label, "pinned": pins.get(key, {}).get("version", ""),
              "installed": installed.get(key, ""), "latest": targets.get(key, {}).get("version", "")}
             for key, label in REA_TOOLS.items()]
    try:
        meta = json.loads(REA_STATE.read_text())
    except (OSError, ValueError):
        meta = {}
    health = state.get("Health") or {}
    engine = REA_ENGINE_DIR / f"godotsteam-{GODOTSTEAM_VERSION}"
    try:
        engine_meta = json.loads(GODOTSTEAM_STATE.read_text())
    except (OSError, ValueError):
        engine_meta = {}
    try:
        godot_meta = json.loads(GODOT_STATE.read_text())
    except (OSError, ValueError):
        godot_meta = {}
    godot_versions = sorted((p.name.removeprefix("godot-") for p in GODOT_DIR.glob("godot-*")
                             if p.is_dir() and not p.is_symlink()
                             and GODOT_VERSION_RE.fullmatch(p.name.removeprefix("godot-"))
                             and any(p.glob("Godot_v*-stable_linux*"))),
                            key=lambda v: tuple(map(int, v.split("."))))
    cached_versions, steam_cached = rea_cached_engines() if state.get("Status") == "running" else ([], False)
    return {"installed": bool(container), "running": state.get("Status") == "running",
            "healthy": health.get("Status") == "healthy", "health": health.get("Status", ""),
            "version": installed.get("rea-agents") or pins.get("rea-agents", {}).get("version", ""),
            "tools": tools, "apt": manifest.get("apt", {}), "built": manifest.get("built", ""),
            "update_available": any(t["latest"] and t["latest"] != t["pinned"] for t in tools) if targets else None,
            "update_error": check.get("error", ""), "checking": bool(check.get("running")),
            "checked_at": check.get("at", 0), "hermes_registered": rea_hermes_registered(), "url": REA_MCP_URL,
            "install_running": running("rea-install") or running("rea-update"),
            "phase": meta.get("phase", ""), "error": meta.get("error", ""), "note": meta.get("note", ""),
            "godotsteam": {"version": GODOTSTEAM_VERSION,
                           "installed": (engine / "linux-352-editor.64").is_file() and (engine / "libsteam_api.so").is_file(),
                           "cached": steam_cached,
                           "running": running("rea-godotsteam-download"), "phase": engine_meta.get("phase", ""),
                           "error": engine_meta.get("error", "")},
            "godot": {"installed": godot_versions, "cached": cached_versions, "version": godot_meta.get("version", ""),
                      "running": running("rea-godot-download"), "phase": godot_meta.get("phase", ""),
                      "error": godot_meta.get("error", "")},
            "portable": portable_engine_status(), "addons": rea_addon_status()}


def git_version(path: Path) -> str:
    if not (path / ".git").exists():
        return ""
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True, timeout=10)
    return result.stdout.strip() if result.returncode == 0 else ""


def hermes_stable_target(checkout: Path = HERMES, *, forward_only: bool = True) -> dict:
    """Resolve the published stable release against Rocsy's own checkout."""
    code = ("import json, os; from hermes_cli.source_releases import resolve_source_target; "
            "target = resolve_source_target('stable', ['git'], os.getcwd(), "
            "forward_only=bool(int(os.environ['CORE_HERMES_FORWARD_ONLY']))); "
            "print(json.dumps({'commit': target.commit, 'version': target.version}))")
    result = subprocess.run([sys.executable, "-c", code], cwd=checkout,
                            env={**os.environ, "PYTHONPATH": str(checkout), "GIT_TERMINAL_PROMPT": "0",
                                 "CORE_HERMES_FORWARD_ONLY": "1" if forward_only else "0"},
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-300:] or "Hermes stable release check failed")
    target = json.loads(result.stdout.strip())
    if not target.get("commit") or not target.get("version"):
        raise RuntimeError("Hermes stable release check returned no pinned commit")
    return target


def update_status(kind: str, path: Path, wait: bool = False) -> dict:
    """Check the update target used by the corresponding Manager action.

    The check can take over a minute (Hermes resolves its stable release over the network), so status
    polls answer from the last result and refresh it in the background; only the Check button waits."""
    head = git_version(path)
    if not head:
        return {"update_available": False, "update_error": ""}
    cached = UPDATE_CHECKS.get(kind)
    if cached and cached["head"] == head and time.monotonic() - cached["at"] < (60 if cached["error"] else 600):
        return {"update_available": cached["available"], "update_error": cached["error"]}
    if wait:
        return _check_update(kind, path, head)
    with REFRESH_LOCK:
        start = kind not in UPDATE_REFRESHING
        UPDATE_REFRESHING.add(kind)
    if start:
        threading.Thread(target=_check_update, args=(kind, path, head), daemon=True).start()
    if cached and cached["head"] == head:
        return {"update_available": cached["available"], "update_error": cached["error"]}
    return {"update_available": False, "update_error": "", "update_checking": True}


def _check_update(kind: str, path: Path, head: str) -> dict:
    try:
        return _run_update_check(kind, path, head)
    finally:
        with REFRESH_LOCK:
            UPDATE_REFRESHING.discard(kind)


def _run_update_check(kind: str, path: Path, head: str) -> dict:
    available = None
    error = ""
    try:
        if kind in {"hermes", "personal-hermes"}:
            target = hermes_stable_target(path)
            current = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                                     capture_output=True, text=True, timeout=10, check=True).stdout.strip()
            available = target["commit"] != current
        else:
            branch = subprocess.run(["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"],
                                    capture_output=True, text=True, timeout=10, check=True).stdout.strip()
            remote_ref = f"refs/remotes/origin/{branch}"
            subprocess.run(["git", "-C", str(path), "fetch", "--quiet", "--no-tags", "origin",
                            f"+refs/heads/{branch}:{remote_ref}"], capture_output=True, text=True, timeout=45,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, check=True)
            ancestor = subprocess.run(["git", "-C", str(path), "merge-base", "--is-ancestor", "HEAD", remote_ref],
                                      capture_output=True, timeout=10)
            if ancestor.returncode:
                error = "Local branch has diverged from origin; review it before updating"
            else:
                count = subprocess.run(["git", "-C", str(path), "rev-list", "--count", f"HEAD..{remote_ref}"],
                                       capture_output=True, text=True, timeout=10, check=True)
                available = int(count.stdout.strip()) > 0
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
        error = str(exc)[:300]

    UPDATE_CHECKS[kind] = {"head": head, "at": time.monotonic(), "available": available, "error": error}
    return {"update_available": available, "update_error": error}


def hermes_addons_status() -> dict:
    if not (HERMES / ".git").exists() or not HERMES_PATCH.is_file():
        return {"addons_applied": False, "addons_error": "Rocsy Addons patch is missing"}
    command = ["git", "-C", str(HERMES), "apply"]
    reverse = subprocess.run([*command, "--reverse", "--check", str(HERMES_PATCH)],
                             capture_output=True, text=True, timeout=15)
    if reverse.returncode == 0:
        return {"addons_applied": True, "addons_error": ""}
    forward = subprocess.run([*command, "--check", str(HERMES_PATCH)],
                             capture_output=True, text=True, timeout=15)
    return {"addons_applied": False, "addons_error": "" if forward.returncode == 0 else
            "Rocsy Addons patch conflicts with this Hermes checkout"}


def personal_hermes_detected() -> bool:
    if (PERSONAL_HERMES / ".git").exists():
        return True
    if not PERSONAL_LAUNCHER.is_file():
        return False
    try:
        return subprocess.run([str(PERSONAL_LAUNCHER), "--version"], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def auto_provision_rocsy() -> None:
    """Queue one safe Rocsy install when an existing personal install is found."""
    if HERMES.exists() or HERMES.is_symlink() or not personal_hermes_detected():
        return
    if hermes_state().get("auto_requested"):
        return
    try:
        start("hermes-download", auto=True)
    except (Refused, OSError, RuntimeError):
        # A busy job can be retried on the next scan. A queued auto-install
        # records auto_requested, so a failed download does not loop forever.
        pass


def view(wait: bool = False) -> dict:
    auto_provision_rocsy()
    personal_installed = personal_hermes_detected()
    personal_managed = (PERSONAL_HERMES / ".git").exists() and PERSONAL_LAUNCHER.is_file()
    archs = gpu_archs()
    built = state()
    checkout = (COMFY / ".git").exists()
    current = git_version(COMFY)
    installed_flash = flash_installed()
    hermes = hermes_state()
    comfy_status = update_status("comfy", COMFY, wait) if checkout and not running("comfy-update") else {}
    hermes_status = update_status("hermes", HERMES, wait) if (HERMES / ".git").exists() and not running("hermes-update") else {}
    personal_status = update_status("personal-hermes", PERSONAL_HERMES, wait) if (PERSONAL_HERMES / ".git").exists() and not running("personal-hermes-update") else {}
    personal = personal_hermes_state()
    return {"comfy": {"installed": checkout, "path": str(COMFY), "version": current,
                      "ready": comfyinstall.ready(COMFY), "supervisor": comfyinstall.supervisor_status(ROCSY_ROOT),
                      "supervisor_running": running("comfy-supervisor"),
                      "download_running": running("comfy-download"), "update_running": running("comfy-update"),
                      **comfy_status},
            "flash": {"installed": installed_flash, "phase": built.get("phase", ""),
                      "error": built.get("error", ""), "built_archs": built.get("archs", []),
                      "current_archs": archs, "rebuild_needed": bool(installed_flash and built.get("archs") and archs and
                                                   set(archs) - set(built.get("archs", []))),
                      "running": running("flash-install"), "source": str(FLASH_SOURCE)},
            "hermes": {"installed": (HERMES / ".git").exists(), "path": str(HERMES), "version": git_version(HERMES),
                       "download_running": running("hermes-download"), "update_running": running("hermes-update"),
                       "phase": hermes.get("phase", ""), "error": hermes.get("error", ""),
                       **hermes_status, **hermes_addons_status()},
            "personal_hermes": {"installed": personal_installed, "managed": personal_managed, "path": str(PERSONAL_HERMES),
                                "version": git_version(PERSONAL_HERMES), "update_running": running("personal-hermes-update"),
                                "phase": personal.get("phase", ""), "error": personal.get("error", ""),
                                **personal_status},
            "motion": motion_status(), "rea": rea_status(), "ytdlp": ytdlp_status(), "trainer": trainer_status()}


def start(job: str, *, auto: bool = False, version: str = "") -> dict:
    with LOCK:
        if any(running(name) for name in JOBS):
            raise Refused("Another core backend install or update is running")
        if job == "comfy-download" and comfyinstall.ready(COMFY):
            raise Refused("ComfyUI is already set up here")
        if job == "comfy-supervisor":
            if not comfyinstall.ready(COMFY):
                raise Refused("Set up ComfyUI first (its Python environment is missing)")
            if comfyinstall.supervisor_status(ROCSY_ROOT)["state"] == "other":
                raise Refused("The ComfyUI supervisor is already set up for another ComfyUI; it won't be replaced")
        if job == "trainer-install" and not (COMPOSE_DIR / "docker-compose.yml").is_file():
            raise Refused("Docker Compose configuration is missing")
        if job == "comfy-update" and not (COMFY / ".git").exists():
            raise Refused("Download ComfyUI first")
        if job in {"hermes-download", "hermes-update", "personal-hermes-update"} and HERMES.resolve() == PERSONAL_HERMES.resolve():
            raise Refused("Personal and Rocsy Hermes must use separate source directories")
        if job == "hermes-download" and (HERMES.exists() or HERMES.is_symlink()):
            raise Refused("Hermes already exists at the install path")
        if job == "hermes-download" and (PERSONAL_HERMES.exists() or PERSONAL_HERMES.is_symlink()) and not personal_hermes_detected():
            raise Refused("Personal Hermes path exists but is not a Git checkout; it will not be overwritten")
        if job == "hermes-download" and PERSONAL_LAUNCHER.exists() and not personal_hermes_detected():
            raise Refused("Personal Hermes launcher exists without its expected checkout; it will not be overwritten")
        if job == "hermes-update" and not (HERMES / ".git").exists():
            raise Refused("Download Hermes first")
        if job == "personal-hermes-update" and (not (PERSONAL_HERMES / ".git").exists() or not PERSONAL_LAUNCHER.is_file()):
            raise Refused("Personal Hermes is not installed")
        if job == "motion-install":
            if not MOTION_PACKAGE.is_file() or not (COMPOSE_DIR / "docker-compose.yml").is_file():
                raise Refused("Motion source or Docker Compose configuration is missing")
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            MOTION_STATE.write_text(json.dumps({"phase": "queued", "started": time.time(), "error": ""}))
        if job in {"rea-install", "rea-update"}:
            if not REA_VERSIONS.is_file() or not (COMPOSE_DIR / "docker-compose.yml").is_file():
                raise Refused("REA source or Docker Compose configuration is missing")
            targets = {}
            if job == "rea-update":
                pins = rea_pins()
                targets = {key: value for key, value in (REA_CHECK.get("targets") or {}).items()
                           if value.get("version") and value.get("version") != pins.get(key, {}).get("version")}
                if not targets:
                    raise Refused("No REA update found; check for updates first")
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            REA_STATE.write_text(json.dumps({"phase": "queued", "started": time.time(), "error": "", "targets": targets}))
        if job == "ytdlp-install":
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            YTDLP_STATE.write_text(json.dumps({"phase": "queued", "started": time.time(), "error": "",
                                               "version": ytdlp_status()["version"]}))
        if job == "rea-godotsteam-download":
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            GODOTSTEAM_STATE.write_text(json.dumps({"phase": "queued", "started": time.time(), "error": ""}))
        if job == "rea-godot-download":
            if not GODOT_VERSION_RE.fullmatch(version):
                raise Refused("Enter a stable Godot 3 or 4 version, such as 4.2.2")
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            GODOT_STATE.write_text(json.dumps({"phase": "queued", "version": version, "started": time.time(), "error": ""}))
        if job in {f"rea-{engine_id}-download" for engine_id in PORTABLE_ENGINES}:
            if os.uname().machine != "x86_64":
                raise Refused("This Linux engine build requires an x86-64 host")
            engine_id = job.removeprefix("rea-").removesuffix("-download")
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            (STATE_DIR / f"rea-{engine_id}.json").write_text(json.dumps({"phase": "queued", "started": time.time(), "error": ""}))
        if job in {f"rea-{addon_id}-download" for addon_id in REA_ADDONS}:
            if os.uname().machine != "x86_64":
                raise Refused("This Linux tool build requires an x86-64 host")
            addon_id = job.removeprefix("rea-").removesuffix("-download")
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            (STATE_DIR / f"rea-{addon_id}.json").write_text(json.dumps({"phase": "queued", "started": time.time(), "error": ""}))
        if job.startswith("hermes-"):
            if not (COMPOSE_DIR / "docker-compose.yml").is_file():
                raise Refused("BunnyBytes Docker Compose configuration is missing")
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            previous = hermes_state()
            HERMES_STATE.write_text(json.dumps({"phase": "queued", "started": time.time(), "error": "",
                                                "auto_requested": auto or previous.get("auto_requested", False),
                                                "deployed_commit": previous.get("deployed_commit", "")}))
        if job == "personal-hermes-update":
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            PERSONAL_HERMES_STATE.write_text(json.dumps({"phase": "queued", "started": time.time(), "error": ""}))
        if job == "flash-install":
            if not (COMFY / ".venv/bin/python3").exists():
                raise Refused("ComfyUI's Python environment is needed to build FlashAttention")
            if not FLASH_SCRIPT.is_file():
                raise Refused("FlashAttention build script is missing")
            archs = gpu_archs()
            if not archs:
                raise Refused("No ROCm GPU architecture was detected; rescan after the GPU is available")
            # hipcc, its HIP version vs ComfyUI's torch, supported targets: refuse now, not mid-compile.
            check = subprocess.run(["bash", str(FLASH_SCRIPT), "--check", str(FLASH_SOURCE)], capture_output=True,
                                   text=True, timeout=120, env={**os.environ, "GPU_ARCHS": ";".join(archs),
                                                                "FLASH_ATTN_VENV": str(COMFY / ".venv")})
            if check.returncode:
                errors = [line[4:] for line in check.stdout.splitlines() if line.startswith("ERR ")]
                raise Refused("; ".join(errors) or (check.stderr or check.stdout).strip()[-300:] or "FlashAttention preflight failed")
            archs = next((line[6:].split(";") for line in check.stdout.splitlines() if line.startswith("ARCHS=")), archs)
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            FLASH_STATE.write_text(json.dumps({"phase": "queued", "archs": archs, "started": time.time()}))
        args = ["systemd-run", "--user", "--collect", "--quiet", f"--unit={JOBS[job]}",
                "-p", "Nice=19", "-p", "IOSchedulingClass=idle", sys.executable, str(Path(__file__).resolve()), job]
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        _ACTIVE["at"] = 0.0
        if result.returncode:
            if job.startswith("hermes-"):
                HERMES_STATE.write_text(json.dumps({"phase": "failed", "error": (result.stderr or result.stdout).strip()[:300]}))
            raise RuntimeError((result.stderr or result.stdout).strip()[:300])
        messages = {"comfy-download": "ComfyUI download and setup started", "comfy-update": "ComfyUI update started",
                    "hermes-download": "Rocsy Hermes download and build started", "hermes-update": "Rocsy Hermes update and rebuild started",
                    "personal-hermes-update": "Personal Hermes update started", "motion-install": "Remotion install started",
                    "comfy-supervisor": "Setting up the ComfyUI supervisor", "trainer-install": "Trainer build and start started",
                    "rea-install": "REA download and install started", "rea-update": "REA update started",
                    "rea-godotsteam-download": "GodotSteam engine download started",
                    "rea-godot-download": f"Godot {version} engine download started",
                    "rea-love2d-download": "LÖVE engine download started",
                    "rea-tic80-download": "TIC-80 engine download started",
                    "ytdlp-install": "yt-dlp download started",
                    **{f"rea-{addon_id}-download": f"{spec['label']} download started" for addon_id, spec in REA_ADDONS.items()}}
        return {"message": messages.get(job) or f"FlashAttention build started for {', '.join(archs)}"}


def uninstall_rea_engine(name: str, version: str = "") -> dict:
    """Remove one selected managed runtime or REA test cache."""
    import shutil

    with LOCK:
        if any(running(job) for job in JOBS):
            raise Refused("Wait for the current backend job to finish")
        try:
            result = subprocess.run(["docker", "exec", "bunny-rea", "ps", "-eo", "args="],
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result and result.returncode == 0 and any("rea-test/engines/" in line and
                                                      any(engine in line.lower() for engine in ("godot", "love", "tic80"))
                                                      for line in result.stdout.splitlines()):
            raise Refused("An REA engine is active; wait for it to finish")
        if name == "godot":
            if not GODOT_VERSION_RE.fullmatch(version):
                raise Refused("Invalid Godot version")
            target = GODOT_DIR / f"godot-{version}"
            label = f"Godot {version}"
        elif name == "godot-cache":
            if not GODOT_VERSION_RE.fullmatch(version):
                raise Refused("Invalid Godot version")
            target = None
            cache = f"/home/node/rea-test/engines/godot-{version}"
            label = f"Godot {version} test cache"
        elif name == "godotsteam":
            target = REA_ENGINE_DIR / f"godotsteam-{GODOTSTEAM_VERSION}"
            label = f"GodotSteam {GODOTSTEAM_VERSION}"
        elif name == "godotsteam-cache":
            target = None
            cache = f"/home/node/rea-test/engines/godotsteam-local-{GODOTSTEAM_VERSION}"
            label = f"GodotSteam {GODOTSTEAM_VERSION} test cache"
        elif name in REA_ADDONS:
            spec = REA_ADDONS[name]
            target = REA_ADDON_DIR / f"{name}-{spec['version']}"
            label = f"{spec['label']} {spec['version']}"
        elif name in PORTABLE_ENGINES:
            spec = PORTABLE_ENGINES[name]
            target = PORTABLE_ENGINE_DIR / f"{name}-{spec['version']}"
            label = f"{spec['label']} {spec['version']}"
        else:
            raise Refused("Unknown REA engine")
        if target is None:
            try:
                present = subprocess.run(["docker", "exec", "bunny-rea", "test", "-d", cache],
                                         capture_output=True, timeout=10)
                if present.returncode:
                    raise Refused(f"{label} is not cached")
                removed = subprocess.run(["docker", "exec", "-u", "1000:1000", "bunny-rea", "rm", "-rf", "--", cache],
                                         capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise Refused(f"Could not access REA test cache: {exc}") from exc
            if removed.returncode:
                raise Refused((removed.stderr or "Could not remove REA test cache")[:300])
            return {"message": f"{label} uninstalled"}
        if not target.is_dir() or target.is_symlink():
            raise Refused(f"{label} is not installed by the manager")
        shutil.rmtree(target)
        return {"message": f"{label} uninstalled"}


def run_step(output, args: list[str], cwd: Path | None = None, env_extra: dict | None = None) -> None:
    output.write("$ " + " ".join(args) + "\n")
    output.flush()
    result = subprocess.run(args, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env_extra or {})})
    if result.returncode:
        raise RuntimeError(f"{args[0]} exited with {result.returncode}; see the log")


def pull_hermes(output) -> bool:
    """Move only Rocsy's checkout to a verified stable release and keep its patch."""
    target = hermes_stable_target()
    def rev(ref: str) -> str:
        return subprocess.run(["git", "-C", str(HERMES), "rev-parse", ref],
                              capture_output=True, text=True, timeout=10, check=True).stdout.strip()

    before = rev("HEAD")
    if target["commit"] == before:
        return False
    run_step(output, ["git", "-C", str(HERMES), "fetch", "--depth=1", "origin",
                      f"refs/tags/v{target['version']}"])
    if rev("FETCH_HEAD^{commit}") != target["commit"]:
        raise RuntimeError("Fetched Hermes release does not match the published stable commit")

    with tempfile.TemporaryDirectory(prefix="hermes-rocsy-check-") as scratch:
        checkout = str(Path(scratch) / "checkout")
        run_step(output, ["git", "-C", str(HERMES), "worktree", "add", "--detach", checkout, target["commit"]])
        try:
            check = subprocess.run(["git", "-C", checkout, "apply", "--check", str(HERMES_PATCH)],
                                   capture_output=True, text=True, timeout=15)
            if check.returncode:
                raise RuntimeError("Rocsy Addons patch conflicts with the new stable release; checkout was not changed")
        finally:
            run_step(output, ["git", "-C", str(HERMES), "worktree", "remove", "--force", checkout])

    if not hermes_addons_status()["addons_applied"]:
        raise RuntimeError("Current Rocsy patch must be applied before updating")
    run_step(output, ["git", "-C", str(HERMES), "apply", "--reverse", str(HERMES_PATCH)])
    try:
        dirty = subprocess.run(["git", "-C", str(HERMES), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=15, check=True).stdout.strip()
        if dirty:
            raise RuntimeError("Rocsy checkout has other local edits; update stopped before changing its version")
        run_step(output, ["git", "-C", str(HERMES), "switch", "--detach", target["commit"]])
        run_step(output, ["git", "-C", str(HERMES), "apply", str(HERMES_PATCH)])
    except Exception:
        if rev("HEAD") != before:
            run_step(output, ["git", "-C", str(HERMES), "switch", "--detach", before])
        if not hermes_addons_status()["addons_applied"]:
            run_step(output, ["git", "-C", str(HERMES), "apply", str(HERMES_PATCH)])
        raise
    return True


def apply_hermes_addons(output) -> bool:
    status = hermes_addons_status()
    if status["addons_applied"]:
        output.write("Rocsy Addons patch is already applied.\n")
        return False
    if status["addons_error"]:
        raise RuntimeError(status["addons_error"])
    run_step(output, ["git", "-C", str(HERMES), "apply", str(HERMES_PATCH)])
    return True


def create_rocsy_checkout(output, on_personal_install=None) -> None:
    """Stage an independent checkout at the patch base, then publish it atomically."""
    if HERMES.exists() or HERMES.is_symlink():
        raise RuntimeError("Rocsy Hermes path already exists; it will not be overwritten")
    if not HERMES_PATCH.is_file():
        raise RuntimeError("Rocsy Addons patch is missing")
    HERMES.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hermes-rocsy-stage-", dir=HERMES.parent) as scratch:
        checkout = Path(scratch) / "checkout"
        local_base = subprocess.run(["git", "-C", str(PERSONAL_HERMES), "cat-file", "-e",
                                     f"{HERMES_PATCH_BASE}^{{commit}}"], capture_output=True).returncode == 0 \
            if (PERSONAL_HERMES / ".git").exists() else False
        if local_base:
            # --no-hardlinks keeps Rocsy's objects independent of the personal repo.
            run_step(output, ["git", "clone", "--no-hardlinks", "--no-checkout", str(PERSONAL_HERMES), str(checkout)])
            run_step(output, ["git", "-C", str(checkout), "remote", "set-url", "origin", HERMES_URL])
        else:
            run_step(output, ["git", "clone", "--filter=blob:none", "--no-checkout", HERMES_URL, str(checkout)])
            run_step(output, ["git", "-C", str(checkout), "fetch", "--depth=1", "origin", HERMES_PATCH_BASE])
        run_step(output, ["git", "-C", str(checkout), "switch", "--detach", HERMES_PATCH_BASE])
        check = subprocess.run(["git", "-C", str(checkout), "apply", "--check", str(HERMES_PATCH)],
                               capture_output=True, text=True, timeout=15)
        if check.returncode:
            raise RuntimeError("Rocsy Addons patch does not apply to its pinned base; no checkout was installed")

        if not personal_hermes_detected():
            # The upstream installer owns the clean terminal/Desktop source.
            # It runs before Rocsy's patch is applied, so it cannot copy addons.
            if on_personal_install:
                on_personal_install()
            run_step(output, ["bash", str(checkout / "scripts/install.sh"), "--commit", HERMES_PATCH_BASE,
                              "--dir", str(PERSONAL_HERMES), "--hermes-home", str(PERSONAL_HERMES.parent),
                              "--non-interactive"], env_extra={"HERMES_REPO_URL": HERMES_URL})
            if not (PERSONAL_HERMES / ".git").exists() or not PERSONAL_LAUNCHER.is_file():
                raise RuntimeError("Personal Hermes installer did not finish; Rocsy checkout was not installed")

        if HERMES.exists() or HERMES.is_symlink():
            raise RuntimeError("Rocsy Hermes path appeared while staging; it will not be overwritten")
        checkout.rename(HERMES)
        output.write(f"Published separate Rocsy checkout at {HERMES}.\n")


def prepare_binds(output) -> None:
    """Create missing bind-mount folders as this user before `docker compose up` (dockeraccess.py)."""
    made = dockeraccess.precreate_binds(COMPOSE_DIR, log=lambda line: output.write(line + "\n"))
    if made:
        output.write(f"Created {len(made)} folder(s) the services mount, so they stay yours.\n")


def enable_profile(name: str) -> None:
    """Keep a service installed here in later `docker compose up` runs: installer-made setups list their
    optional services (Compose profiles) in COMPOSE_PROFILES in BunnyBytes/.env."""
    env = COMPOSE_DIR / ".env"
    try:
        lines = env.read_text().splitlines()
    except OSError:
        return
    current = next((line.partition("=")[2] for line in reversed(lines) if line.startswith("COMPOSE_PROFILES=")), None)
    profiles = [p for p in (current or "").split(",") if p]
    if current is None and name not in ("rea", "trainer"):
        return  # a setup without profiles runs these services anyway
    if name in profiles:
        return
    kept = [line for line in lines if not line.startswith("COMPOSE_PROFILES=")]
    env.write_text("\n".join([*kept, "COMPOSE_PROFILES=" + ",".join([*profiles, name])]) + "\n")


def comfy_step(output):
    """run(command, message, network) for comfyinstall: logged, network steps retried."""
    def step(command, message, network):
        output.write(f"\n=== {message} ===\n")
        for attempt in range(1, 4 if network else 2):
            try:
                return run_step(output, command)
            except RuntimeError:
                if attempt == (3 if network else 1):
                    raise
                output.write(f"Download failed; retrying ({attempt + 1}/3)\n")
                time.sleep(5 * attempt)
    return step


def run_h3_setup(output) -> None:
    """Video's H3 packs, PDD loader and patches; problems are logged, never fatal."""
    output.write("\n=== H3 optimizations (node packs, PDD loader, patches) ===\n")
    problems = comfyinstall.h3_setup(COMFY, ROCSY_ROOT, comfy_step(output))
    output.write("".join(f"Not done: {p}\n" for p in problems) or "All in place.\n")


def run_comfy_setup(output) -> int:
    """Download ComfyUI (or finish a partial setup), then its supervisor: what the installer does."""
    comfyinstall.setup(COMFY, comfy_step(output))
    return run_comfy_supervisor(output, after_setup=True)


def run_comfy_supervisor(output, after_setup: bool = False) -> int:
    run_h3_setup(output)
    status = comfyinstall.supervisor_status(ROCSY_ROOT)
    if status["state"] == "other":
        output.write(f"\nThe ComfyUI supervisor is set up for another ComfyUI ({status.get('script') or 'see its unit'}); "
                     "left as it is.\n")
        return 0 if after_setup else 1
    output.write("\n=== Setting up the ComfyUI supervisor ===\n")
    comfyinstall.install_supervisor(ROCSY_ROOT, COMFY)
    output.write("comfyui-supervisor is running; the studios start ComfyUI through it when they need it.\n")
    return 0


def run_trainer(output) -> int:
    run_step(output, ["docker", "compose", "build", "trainer"], cwd=COMPOSE_DIR)
    prepare_binds(output)
    run_step(output, ["docker", "compose", "up", "-d", "--no-deps", "trainer"], cwd=COMPOSE_DIR)
    enable_profile("trainer")
    return 0


def run_hermes(job: str, output) -> int:
    meta = hermes_state()

    def phase(name: str, error: str = "") -> None:
        meta.update(phase=name, error=error)
        HERMES_STATE.write_text(json.dumps(meta))
        output.write(f"\n=== {name} ===\n")
        output.flush()

    try:
        changed = False
        if job == "hermes-download":
            phase("downloading")
            create_rocsy_checkout(output, on_personal_install=lambda: phase("installing-personal"))
        else:
            phase("updating")
            changed = pull_hermes(output)
        phase("patching")
        patched = apply_hermes_addons(output)
        current_commit = subprocess.run(["git", "-C", str(HERMES), "rev-parse", "HEAD"],
                                        capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        if job == "hermes-update" and not changed and not patched and meta.get("deployed_commit") == current_commit:
            phase("ready")
            output.write("Already up to date; no container restart needed.\n")
            return 0
        phase("building")
        run_step(output, ["docker", "compose", "build", "hermes-agent"], cwd=COMPOSE_DIR)
        phase("deploying")
        prepare_binds(output)
        run_step(output, ["docker", "compose", "up", "-d", "--no-deps", "hermes-agent", "hermes-dashboard"], cwd=COMPOSE_DIR)
        meta["deployed_commit"] = current_commit
        enable_profile("hermes")
        phase("ready")
        return 0
    except Exception as exc:
        phase("failed", str(exc)[:300])
        return 1


def run_personal_hermes(output) -> int:
    """Use the personal install's updater without touching Rocsy's checkout or containers."""
    meta = personal_hermes_state()

    def phase(name: str, error: str = "") -> None:
        meta.update(phase=name, error=error)
        PERSONAL_HERMES_STATE.write_text(json.dumps(meta))
        output.write(f"\n=== {name} ===\n")
        output.flush()

    try:
        phase("checking")
        if PERSONAL_HERMES.resolve() == HERMES.resolve():
            raise RuntimeError("Personal and Rocsy Hermes paths must be different")
        dirty = subprocess.run(["git", "-C", str(PERSONAL_HERMES), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=15, check=True).stdout.strip()
        if dirty:
            raise RuntimeError("Personal Hermes has local edits; update stopped to preserve them")
        target = hermes_stable_target(PERSONAL_HERMES)
        current = subprocess.run(["git", "-C", str(PERSONAL_HERMES), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        if target["commit"] == current:
            phase("ready")
            output.write("Personal Hermes is already up to date.\n")
            return 0
        phase("updating")
        run_step(output, [str(PERSONAL_LAUNCHER), "update", "--yes", "--no-gateway-restart"])
        phase("ready")
        return 0
    except Exception as exc:
        phase("failed", str(exc)[:300])
        return 1


def run_motion(output) -> int:
    meta = {"phase": "building", "started": time.time(), "error": ""}
    MOTION_STATE.write_text(json.dumps(meta))
    try:
        run_step(output, ["docker", "compose", "build", "motion"], cwd=COMPOSE_DIR)
        meta["phase"] = "starting"
        MOTION_STATE.write_text(json.dumps(meta))
        prepare_binds(output)
        run_step(output, ["docker", "compose", "up", "-d", "--no-deps", "motion"], cwd=COMPOSE_DIR)
        enable_profile("motion")
        meta["phase"] = "ready"
        meta["finished"] = time.time()
        MOTION_STATE.write_text(json.dumps(meta))
        return 0
    except Exception as exc:
        meta.update(phase="failed", error=str(exc)[:300], finished=time.time())
        MOTION_STATE.write_text(json.dumps(meta))
        output.write(meta["error"] + "\n")
        return 1


def register_rea_in_hermes(output) -> str:
    """Point Rocsy's Hermes at the REA MCP endpoint; Hermes reconciles mcp_servers from config.yaml by itself."""
    if rea_hermes_registered():
        output.write("Hermes already has the REA MCP server.\n")
        return ""
    ok = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", "hermes-agent"],
                        capture_output=True, text=True, timeout=10).stdout.strip() == "true"
    if not ok:
        output.write("hermes-agent is not running; REA was not registered in Hermes yet.\n")
        return "Hermes was not running, so REA is not registered in it yet. Run Rebuild once Hermes is up."
    for key, value in (("url", REA_MCP_URL), ("protocol", "legacy"), ("timeout", "600"), ("connect_timeout", "60")):
        run_step(output, ["docker", "exec", "-u", "1000:1000", "hermes-agent", "hermes", "config", "set",
                          f"mcp_servers.rea.{key}", value])
    return "Registered in Hermes (it connects on its own within a few seconds)."


def run_rea(job: str, output) -> int:
    meta = json.loads(REA_STATE.read_text()) if REA_STATE.is_file() else {}

    def phase(name: str, **extra) -> None:
        meta.update(phase=name, **extra)
        REA_STATE.write_text(json.dumps(meta))
        output.write(f"\n=== {name} ===\n")
        output.flush()

    previous = REA_VERSIONS.read_text()
    try:
        if job == "rea-update":
            phase("updating")
            pins = json.loads(previous)
            for key, target in meta.get("targets", {}).items():
                output.write(f"{REA_TOOLS.get(key, key)}: {pins.get(key, {}).get('version', '?')} -> {target['version']}\n")
                pins[key] = target
            REA_VERSIONS.write_text(json.dumps(pins, indent=2) + "\n")
        phase("building")
        try:
            run_step(output, ["docker", "compose", "build", "--build-arg", f"APT_STAMP={int(time.time())}", "rea"],
                     cwd=COMPOSE_DIR)
        except Exception:
            if job == "rea-update":
                REA_VERSIONS.write_text(previous)
                output.write("Build failed; versions.json restored. The running REA was not touched.\n")
            raise
        busy = rea_busy()
        if busy:
            raise RuntimeError(f"REA is analysing ({', '.join(busy)}); the new image is built, run it again when idle")
        phase("starting")
        prepare_binds(output)
        run_step(output, ["docker", "compose", "up", "-d", "--no-deps", "rea"], cwd=COMPOSE_DIR)
        phase("registering")
        note = register_rea_in_hermes(output)
        meta.pop("targets", None)
        enable_profile("rea")
        phase("ready", note=note, finished=time.time(), error="")
        return 0
    except Exception as exc:
        phase("failed", error=str(exc)[:300], finished=time.time())
        output.write(meta["error"] + "\n")
        return 1


def run_godotsteam(output) -> int:
    """Install the pinned GodotSteam 3.5.2 Linux engine into REA's read-only engine mount."""
    target = REA_ENGINE_DIR / f"godotsteam-{GODOTSTEAM_VERSION}"
    staging = REA_ENGINE_DIR / f".godotsteam-{GODOTSTEAM_VERSION}-{os.getpid()}"
    archive = staging / GODOTSTEAM_ARCHIVE

    def phase(name: str, error: str = "") -> None:
        GODOTSTEAM_STATE.write_text(json.dumps({"phase": name, "error": error, "at": time.time()}))
        output.write(name + (f": {error}" if error else "") + "\n")
        output.flush()

    try:
        staging.mkdir(parents=True)
        phase("downloading")
        digest = hashlib.sha256()
        request = urllib.request.Request(GODOTSTEAM_URL, headers={"User-Agent": "rocsy-hostctl"})
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as dest:
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                dest.write(chunk)
        if digest.hexdigest() != GODOTSTEAM_SHA256:
            raise RuntimeError("GodotSteam archive SHA-256 mismatch")
        phase("extracting")
        with zipfile.ZipFile(archive) as source:
            for name in ("linux-352-editor.64", "libsteam_api.so"):
                with source.open(name) as incoming, (staging / name).open("wb") as outgoing:
                    import shutil
                    shutil.copyfileobj(incoming, outgoing)
        (staging / "linux-352-editor.64").chmod(0o755)
        archive.unlink()
        if target.exists():
            import shutil
            shutil.rmtree(target)
        staging.rename(target)
        phase("ready")
        return 0
    except Exception as exc:
        phase("failed", str(exc)[:300])
        return 1
    finally:
        if staging.exists():
            import shutil
            shutil.rmtree(staging)


def run_godot(output) -> int:
    """Download an official Linux Godot runtime and verify its published SHA-512."""
    import shutil

    meta = json.loads(GODOT_STATE.read_text())
    version = meta["version"]
    if not GODOT_VERSION_RE.fullmatch(version):
        raise RuntimeError("Invalid Godot version")
    filename = (f"Godot_v{version}-stable_linux.x86_64.zip" if version.startswith("4.")
                else f"Godot_v{version}-stable_linux_server.64.zip")
    binary = filename.removesuffix(".zip")
    base = f"https://github.com/godotengine/godot/releases/download/{version}-stable/"
    target = GODOT_DIR / f"godot-{version}"
    staging = GODOT_DIR / f".godot-{version}-{os.getpid()}"

    def phase(name: str, error: str = "") -> None:
        meta.update(phase=name, error=error, at=time.time())
        GODOT_STATE.write_text(json.dumps(meta))
        output.write(name + (f": {error}" if error else "") + "\n")
        output.flush()

    try:
        GODOT_DIR.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        phase("checking checksum")
        sums = fetch(base + "SHA512-SUMS.txt", "text/plain").decode()
        match = next((re.fullmatch(r"([0-9a-fA-F]{128})\s+\*?(?:\./)?" + re.escape(filename), line.strip())
                      for line in sums.splitlines() if line.strip().endswith(filename)), None)
        if not match:
            raise RuntimeError(f"No published SHA-512 checksum for Godot {version} Linux runtime")
        phase("downloading")
        archive = staging / filename
        digest = hashlib.sha512()
        request = urllib.request.Request(base + filename, headers={"User-Agent": "rocsy-hostctl"})
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as dest:
            size = 0
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > 300 * 1024 * 1024:
                    raise RuntimeError("Godot archive is larger than expected")
                digest.update(chunk)
                dest.write(chunk)
        if digest.hexdigest().lower() != match.group(1).lower():
            raise RuntimeError("Godot archive SHA-512 mismatch")
        phase("extracting")
        with zipfile.ZipFile(archive) as source:
            with source.open(binary) as incoming, (staging / binary).open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
        (staging / binary).chmod(0o755)
        archive.unlink()
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
        phase("ready")
        return 0
    except Exception as exc:
        phase("failed", str(exc)[:300])
        return 1
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def run_portable_engine(engine_id: str, output) -> int:
    """Install a small official Linux runtime into REA's read-only portable mount."""
    import shutil

    spec = PORTABLE_ENGINES[engine_id]
    state_file = STATE_DIR / f"rea-{engine_id}.json"
    target = PORTABLE_ENGINE_DIR / f"{engine_id}-{spec['version']}"
    staging = PORTABLE_ENGINE_DIR / f".{engine_id}-{spec['version']}-{os.getpid()}"
    archive = staging / spec["filename"]

    def phase(name: str, error: str = "") -> None:
        state_file.write_text(json.dumps({"phase": name, "error": error, "at": time.time()}))
        output.write(name + (f": {error}" if error else "") + "\n")
        output.flush()

    try:
        PORTABLE_ENGINE_DIR.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        expected_sha = spec.get("sha256", "")
        if spec.get("asset_id"):
            phase("checking release")
            release = json.loads(fetch("https://api.github.com/repos/love2d/love/releases/tags/11.5",
                                       "application/vnd.github+json"))
            asset = next((a for a in release.get("assets", []) if a.get("name") == spec["filename"]), None)
            if not asset or asset.get("id") != spec["asset_id"] or asset.get("size") != spec["size"]:
                raise RuntimeError("The official release asset changed; installer pin needs review")
            digest = asset.get("digest") or ""
            if digest.startswith("sha256:"):
                expected_sha = digest.split(":", 1)[1]
        phase("downloading")
        digest = hashlib.sha256()
        request = urllib.request.Request(spec["url"], headers={"User-Agent": "rocsy-hostctl"})
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as dest:
            size = 0
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > 20 * 1024 * 1024:
                    raise RuntimeError("Engine download is larger than expected")
                digest.update(chunk)
                dest.write(chunk)
        if size != spec["size"] or (expected_sha and digest.hexdigest() != expected_sha):
            raise RuntimeError("Engine download size or SHA-256 mismatch")
        phase("installing")
        if spec["kind"] == "appimage":
            with archive.open("rb") as source:
                header = source.read(4)
            if header != b"\x7fELF":
                raise RuntimeError("LÖVE AppImage is not a Linux executable")
            archive.chmod(0o755)
            binary = spec["filename"]
        else:
            with zipfile.ZipFile(archive) as source:
                members = source.infolist()
                if len(members) > 100 or sum(info.file_size for info in members) > 80 * 1024 * 1024:
                    raise RuntimeError("Engine archive exceeds extraction limits")
                for info in members:
                    parts = Path(info.filename).parts
                    mode = (info.external_attr >> 16) & 0o170000
                    if not parts or Path(info.filename).is_absolute() or ".." in parts or stat.S_ISLNK(mode):
                        raise RuntimeError("Unsafe path in engine archive")
                    dest = staging.joinpath(*parts)
                    if info.is_dir():
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with source.open(info) as incoming, dest.open("wb") as outgoing:
                            shutil.copyfileobj(incoming, outgoing)
            archive.unlink()
            executables = []
            for path in staging.rglob("*"):
                if path.is_file() and path.name.lower() in {"tic80", "tic-80"}:
                    with path.open("rb") as source:
                        if source.read(4) == b"\x7fELF":
                            executables.append(path)
            if not executables:
                raise RuntimeError("TIC-80 archive contains no Linux tic80 executable")
            executable = min(executables, key=lambda p: len(p.parts))
            executable.chmod(0o755)
            binary = str(executable.relative_to(staging))
        (staging / "manifest.json").write_text(json.dumps({"version": spec["version"], "binary": binary,
                                                       "sha256": digest.hexdigest()}))
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
        phase("ready")
        return 0
    except Exception as exc:
        phase("failed", str(exc)[:300])
        return 1
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def run_rea_addon(addon_id: str, output) -> int:
    """Install one pinned single-file tool into REA's read-only addons mount."""
    import shutil

    spec = REA_ADDONS[addon_id]
    state_file = STATE_DIR / f"rea-{addon_id}.json"
    target = REA_ADDON_DIR / f"{addon_id}-{spec['version']}"
    staging = REA_ADDON_DIR / f".{addon_id}-{spec['version']}-{os.getpid()}"
    binary = staging / spec["filename"]

    def phase(name: str, error: str = "") -> None:
        state_file.write_text(json.dumps({"phase": name, "error": error, "at": time.time()}))
        output.write(name + (f": {error}" if error else "") + "\n")
        output.flush()

    try:
        REA_ADDON_DIR.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        phase("checking release")
        release = json.loads(fetch(f"https://api.github.com/repos/{spec['repo']}/releases/tags/{spec['tag']}",
                                   "application/vnd.github+json"))
        asset = next((a for a in release.get("assets", []) if a.get("name") == spec["filename"]), None)
        if not asset or asset.get("size") != spec["size"] or asset.get("digest") not in (None, "", f"sha256:{spec['sha256']}"):
            raise RuntimeError("The official release asset changed; installer pin needs review")
        phase("downloading")
        digest = hashlib.sha256()
        size = 0
        request = urllib.request.Request(spec["url"], headers={"User-Agent": "rocsy-hostctl"})
        with urllib.request.urlopen(request, timeout=120) as response, binary.open("wb") as dest:
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > spec["size"]:
                    raise RuntimeError("Tool download is larger than expected")
                digest.update(chunk)
                dest.write(chunk)
        if size != spec["size"] or digest.hexdigest() != spec["sha256"]:
            raise RuntimeError("Tool download size or SHA-256 mismatch")
        phase("installing")
        with binary.open("rb") as source:
            if source.read(4) != b"\x7fELF":
                raise RuntimeError(f"{spec['label']} download is not a Linux executable")
        binary.chmod(0o755)
        (staging / "manifest.json").write_text(json.dumps({"version": spec["version"], "binary": spec["filename"],
                                                       "sha256": spec["sha256"], "source": spec["url"]}))
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
        phase("ready")
        return 0
    except Exception as exc:
        phase("failed", str(exc)[:300])
        return 1
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def run(job: str) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = STATE_DIR / (job + ".log")
    with log.open("w") as output:
        try:
            if job == "personal-hermes-update":
                return run_personal_hermes(output)
            if job == "motion-install":
                return run_motion(output)
            if job == "comfy-download":
                return run_comfy_setup(output)
            if job == "comfy-supervisor":
                return run_comfy_supervisor(output)
            if job == "trainer-install":
                return run_trainer(output)
            if job == "ytdlp-install":
                return run_ytdlp(output)
            if job in {"rea-install", "rea-update"}:
                return run_rea(job, output)
            if job == "rea-godotsteam-download":
                return run_godotsteam(output)
            if job == "rea-godot-download":
                return run_godot(output)
            if job in {f"rea-{engine_id}-download" for engine_id in PORTABLE_ENGINES}:
                return run_portable_engine(job.removeprefix("rea-").removesuffix("-download"), output)
            if job in {f"rea-{addon_id}-download" for addon_id in REA_ADDONS}:
                return run_rea_addon(job.removeprefix("rea-").removesuffix("-download"), output)
            if job.startswith("hermes-"):
                return run_hermes(job, output)
            if job == "comfy-update":
                # Fast-forward only. The H3 patch on ComfyUI's own files comes off first (a pull
                # refuses to touch a locally edited file) and goes back on with run_h3_setup.
                for name in comfyinstall.unpatch_core(COMFY, ROCSY_ROOT):
                    output.write(f"Took {name} off for the update\n")
                output.flush()
                result = subprocess.run(["git", "-C", str(COMFY), "pull", "--ff-only", "origin"],
                                        stdout=output, stderr=subprocess.STDOUT)
                run_h3_setup(output)
                return result.returncode
            else:
                built = state()
                archs = built.get("archs", [])
                if not archs or any(not ARCH_RE.fullmatch(a) for a in archs):
                    raise RuntimeError("No valid GPU target was recorded")
                built["phase"] = "building"
                FLASH_STATE.write_text(json.dumps(built))
                command = ["bash", str(FLASH_SCRIPT), str(FLASH_SOURCE)]
                env = {**os.environ, "GPU_ARCHS": ";".join(archs), "PYTORCH_ROCM_ARCH": ";".join(archs),
                       "AMDGPU_TARGETS": ";".join(archs), "FLASH_ATTN_VENV": str(COMFY / ".venv"),
                       "FLASH_ATTN_LOG": str(log)}
            if job == "flash-install":
                # The build script writes directly to this log; avoid simultaneous writers.
                result = subprocess.run(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            else:
                result = subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT)
            if job == "flash-install":
                built = state()
                built.update(phase="ready" if result.returncode == 0 else "failed", finished=time.time())
                if result.returncode:
                    built["error"] = "Build failed; see the log"
                FLASH_STATE.write_text(json.dumps(built))
            return result.returncode
        except Exception as exc:
            output.write(str(exc) + "\n")
            if job == "flash-install":
                built = state()
                built.update(phase="failed", error=str(exc)[:300], finished=time.time())
                FLASH_STATE.write_text(json.dumps(built))
            return 1


def handle(method: str, path: str) -> tuple[int, dict]:
    try:
        route = urlsplit(path)
        if method == "GET" and route.path == "/core/extras/hermes-addons":
            return 200, hermes_extension_status()
        if method == "GET" and route.path == "/core/extras/hermes-addons/log":
            return 200, {"log": HERMES_ADDON_LOG.read_text(errors="replace")[-24000:] if HERMES_ADDON_LOG.exists() else ""}
        match = re.fullmatch(r"/core/extras/hermes-addons/catalog/(skills|plugins)", route.path)
        if method == "GET" and match:
            return 200, hermes_addons_catalog(match.group(1), parse_qs(route.query).get("q", [""])[0])
        match = re.fullmatch(r"/core/extras/hermes-addons/search/(skills|plugins)", route.path)
        if method == "GET" and match:
            query = parse_qs(route.query).get("q", [""])[0]
            return 200, hermes_addons_search(match.group(1), query)
        match = re.fullmatch(r"/core/extras/hermes-addons/install/(skills|plugins)/([^/]+)", route.path)
        if method == "POST" and match:
            return 200, hermes_addons_install(match.group(1), unquote(match.group(2)))
        if method == "GET" and path == "/core/extras":
            return 200, view()
        if method == "POST" and path == "/core/extras/check":
            UPDATE_CHECKS.clear()
            rea_check(force=True)
            return 200, view(wait=True)
        match = re.fullmatch(r"/core/extras/log/(comfy-download|comfy-update|comfy-supervisor|trainer-install|flash-install|hermes-download|hermes-update|personal-hermes-update|motion-install|rea-install|rea-update|rea-godotsteam-download|rea-godot-download|rea-love2d-download|rea-tic80-download|rea-godotpcktool-download|ytdlp-install)", path)
        if method == "GET" and match:
            log = STATE_DIR / (match.group(1) + ".log")
            return 200, {"log": log.read_text(errors="replace")[-64000:] if log.exists() else ""}
        match = re.fullmatch(r"/core/extras/(comfy-download|comfy-update|comfy-supervisor|trainer-install|flash-install|hermes-download|hermes-update|personal-hermes-update|motion-install|rea-install|rea-update|rea-godotsteam-download|rea-love2d-download|rea-tic80-download|rea-godotpcktool-download|ytdlp-install)", path)
        if method == "POST" and match:
            return 200, start(match.group(1))
        match = re.fullmatch(r"/core/extras/rea-(love2d|tic80|godotpcktool)-uninstall", path)
        if method == "POST" and match:
            return 200, uninstall_rea_engine(match.group(1))
        match = re.fullmatch(r"/core/extras/rea-godot-download/([0-9.]+)", path)
        if method == "POST" and match:
            return 200, start("rea-godot-download", version=match.group(1))
        match = re.fullmatch(r"/core/extras/rea-godot-uninstall/([0-9.]+)", path)
        if method == "POST" and match:
            return 200, uninstall_rea_engine("godot", match.group(1))
        match = re.fullmatch(r"/core/extras/rea-godot-cache-uninstall/([0-9.]+)", path)
        if method == "POST" and match:
            return 200, uninstall_rea_engine("godot-cache", match.group(1))
        if method == "POST" and path == "/core/extras/rea-godotsteam-uninstall":
            return 200, uninstall_rea_engine("godotsteam")
        if method == "POST" and path == "/core/extras/rea-godotsteam-cache-uninstall":
            return 200, uninstall_rea_engine("godotsteam-cache")
        return 404, {"error": "not found"}
    except Refused as exc:
        return 409, {"error": str(exc)}


if __name__ == "__main__":
    import dockeraccess  # the docker group in the session that installed Rocsy (see dockeraccess.py)
    dockeraccess.reexec_with_docker_group()
    if len(sys.argv) != 2 or sys.argv[1] not in JOBS:
        sys.exit("unknown job")
    sys.exit(run(sys.argv[1]))
