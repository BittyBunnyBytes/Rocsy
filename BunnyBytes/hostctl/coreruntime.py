#!/usr/bin/env python3
"""Core Backend → GPU runtimes: Rocsy's own ROCm versions and Vulkan tooling, side by side.

Nothing here touches the system: /opt/rocm and every pacman package stay exactly as the
user installed them (other programs on this machine keep using those). Rocsy's copies live
in folders of their own and are only used by Rocsy's llama.cpp builds:

  ~/AI/rocm-versions/<version>/   AMD "TheRock" ROCm tarballs (relocatable, no root needed)
  ~/AI/vulkan-sdk/<version>/      LunarG Vulkan SDK tarballs (headers, glslc, SPIRV for builds)

A llama.cpp build is compiled against one ROCm (recorded in build/rocm-path) and llama-launch
runs it with that ROCm's libraries, so several ROCm versions coexist and switching is just a
matter of picking another build - nothing is reinstalled. The Vulkan *driver* used at run
time is RADV inside the llama container; "Update" rebuilds only that container image.

Downloads run detached (systemd-run unit core-runtime-job), resumable, size-checked and
archive-tested before an install is moved into place. Standard library only; also runnable
as `coreruntime.py job <kind> <version>` (the job itself).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from paths import ROOT as ROCSY_ROOT
from typing import Any

HOME = Path.home()
ROCM_ROOT = Path(os.getenv("CORE_ROCM_ROOT", ROCSY_ROOT / "rocm-versions"))
VULKAN_ROOT = Path(os.getenv("CORE_VULKAN_ROOT", ROCSY_ROOT / "vulkan-sdk"))
STATE = ROCM_ROOT / "state.json"
JOB = ROCM_ROOT / ".job.json"
JOB_LOG = ROCM_ROOT / ".job.log"
UNIT = "core-runtime-job"
SYSTEM_ROCM = Path("/opt/rocm")
COMPOSE_DIR = ROCSY_ROOT / "BunnyBytes"
FAMILY = os.getenv("CORE_ROCM_FAMILY", "gfx110X-all")  # RDNA3: gfx1100 (7900 XTX), gfx1101 (7800 XT)
# AMD publishes production releases on stable.repo.amd.com and the older 7.x line on repo.amd.com.
ROCM_SOURCES = [("https://stable.repo.amd.com/rocm/core/tarball/", "stable"),
                ("https://repo.amd.com/rocm/tarball/", "older line")]
ROCM_PROBE = ["7.14.1"]  # published but missing from the (script-built) 7.x index page
VULKAN_VERSIONS = "https://vulkan.lunarg.com/sdk/versions/linux.json"
VULKAN_TARBALL = "https://sdk.lunarg.com/sdk/download/{v}/linux/vulkansdk-linux-x86_64-{v}.tar.xz"
ARCH_PKG = "https://archlinux.org/packages/search/json/?name={name}&repo=Extra"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?")
CATALOG_TTL = 6 * 3600
LLAMA_IDLE_WAIT = 6 * 3600

_lock = threading.Lock()
_catalog: dict[str, Any] = {"at": 0.0, "rocm": [], "vulkan": [], "radv_latest": "", "error": ""}
CATALOG_CACHE = ROCM_ROOT / "catalog.json"
_catalog_refreshing = threading.Event()


class Refused(Exception):
    pass


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def fetch_text(url: str, timeout: float = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Rocsy-CoreBackend"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode(errors="replace")


def head_size(url: str) -> int:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Rocsy-CoreBackend"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.headers.get("Content-Length") or 0)
    except Exception:  # noqa: BLE001
        return 0


def vkey(version: str) -> tuple[int, ...]:
    version = version.split(":", 1)[-1]  # drop a pacman epoch ("1:26.2.4-1")
    return tuple(int(p) for p in re.findall(r"\d+", version))


# ----------------------------------------------------------------- catalog --

def refresh_catalog(force: bool = False) -> dict[str, Any]:
    """The version lists (several seconds of downloads). A status view answers from the saved list
    and refreshes a stale one in the background; only the refresh button waits."""
    if not _catalog["rocm"]:
        _catalog.update(load_json(CATALOG_CACHE, {}))
    if force or not _catalog["rocm"]:
        return _fetch_catalog()
    if time.time() - _catalog["at"] >= CATALOG_TTL and not _catalog_refreshing.is_set():
        _catalog_refreshing.set()
        threading.Thread(target=_fetch_catalog, daemon=True).start()
    return _catalog


def _fetch_catalog() -> dict[str, Any]:
    try:
        return _download_catalog()
    finally:
        _catalog_refreshing.clear()


def _download_catalog() -> dict[str, Any]:
    rocm: dict[str, dict[str, Any]] = {}
    errors = []
    pattern = re.compile(rf"therock-dist-linux-{re.escape(FAMILY)}-(\d+\.\d+\.\d+)\.tar\.gz")
    for base, channel in ROCM_SOURCES:
        try:
            for version in set(pattern.findall(fetch_text(base))):
                rocm.setdefault(version, {"version": version, "channel": channel,
                                          "url": f"{base}therock-dist-linux-{FAMILY}-{version}.tar.gz"})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{channel}: {exc}")
    for version in ROCM_PROBE:
        url = f"{ROCM_SOURCES[1][0]}therock-dist-linux-{FAMILY}-{version}.tar.gz"
        if version not in rocm and head_size(url):
            rocm[version] = {"version": version, "channel": ROCM_SOURCES[1][1], "url": url}
    with ThreadPoolExecutor(8) as pool:
        for item, size in zip(rocm.values(), pool.map(head_size, [item["url"] for item in rocm.values()])):
            item["size"] = size
    vulkan = []
    try:
        vulkan = [v for v in json.loads(fetch_text(VULKAN_VERSIONS)) if VERSION_RE.fullmatch(v)][:12]
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Vulkan SDK: {exc}")
    radv = ""
    with contextlib.suppress(Exception):
        results = json.loads(fetch_text(ARCH_PKG.format(name="vulkan-radeon"))).get("results") or []
        if results:
            radv = f"{results[0]['pkgver']}-{results[0]['pkgrel']}"
    _catalog.update(at=time.time(), rocm=sorted(rocm.values(), key=lambda i: vkey(i["version"]), reverse=True),
                    vulkan=vulkan, radv_latest=radv, error="; ".join(errors))
    if _catalog["rocm"]:
        with contextlib.suppress(OSError):
            CATALOG_CACHE.parent.mkdir(parents=True, exist_ok=True)
            CATALOG_CACHE.write_text(json.dumps(_catalog))
    return _catalog


# ---------------------------------------------------------------- installs --

def system_rocm() -> dict[str, Any]:
    version = ""
    with contextlib.suppress(OSError):
        version = (SYSTEM_ROCM / ".info/version").read_text().strip()
    return {"id": "system", "version": version or "?", "path": str(SYSTEM_ROCM), "system": True,
            "exists": (SYSTEM_ROCM / "lib/libamdhip64.so").exists()}


def rocm_installs() -> list[dict[str, Any]]:
    out = []
    if ROCM_ROOT.exists():
        for path in sorted(ROCM_ROOT.iterdir(), key=lambda p: vkey(p.name) if VERSION_RE.fullmatch(p.name) else (), reverse=True):
            if path.is_dir() and VERSION_RE.fullmatch(path.name) and (path / ".rocsy-installed").exists():
                out.append({"id": path.name, "version": path.name, "path": str(path),
                            **load_json(path / ".rocsy-installed", {})})
    return out


def rocm_path(rocm_id: str) -> Path:
    """Resolves a ROCm choice for a build ("system" or an installed version)."""
    if rocm_id in ("", "system"):
        return SYSTEM_ROCM
    if VERSION_RE.fullmatch(rocm_id) and (ROCM_ROOT / rocm_id / ".rocsy-installed").exists():
        return ROCM_ROOT / rocm_id
    raise Refused(f"ROCm {rocm_id} isn't installed")


def vulkan_installs() -> list[dict[str, Any]]:
    out = []
    if VULKAN_ROOT.exists():
        for path in sorted(VULKAN_ROOT.iterdir(), key=lambda p: vkey(p.name) if VERSION_RE.fullmatch(p.name) else (), reverse=True):
            if path.is_dir() and VERSION_RE.fullmatch(path.name) and (path / ".rocsy-installed").exists():
                out.append({"id": path.name, "version": path.name, "path": str(path / "x86_64"),
                            **load_json(path / ".rocsy-installed", {})})
    return out


def vulkan_sdk_path(sdk_id: str) -> Path | None:
    """None = the system's packages (vulkan-headers, spirv-headers, shaderc)."""
    if sdk_id in ("", "system"):
        return None
    if VERSION_RE.fullmatch(sdk_id) and (VULKAN_ROOT / sdk_id / ".rocsy-installed").exists():
        return VULKAN_ROOT / sdk_id / "x86_64"
    raise Refused(f"Vulkan SDK {sdk_id} isn't installed")


_pkg_cache: dict[str, Any] = {"key": None, "versions": {}}


def system_vulkan_tools() -> dict[str, Any]:
    """Installed package versions, from one `pacman -Q`, re-read only when the package database changes."""
    with contextlib.suppress(OSError):
        key = Path("/var/lib/pacman/local").stat().st_mtime_ns
        if key != _pkg_cache["key"]:
            result = subprocess.run(["pacman", "-Q", "vulkan-headers", "spirv-headers", "shaderc"], capture_output=True, text=True)
            _pkg_cache.update(key=key, versions=dict(line.split()[:2] for line in result.stdout.splitlines() if len(line.split()) > 1))
    pkg = _pkg_cache["versions"].get
    return {"id": "system", "system": True, "headers": pkg("vulkan-headers", ""), "spirv": pkg("spirv-headers", ""),
            "shaderc": pkg("shaderc", "")}


_radv_cache: dict[str, Any] = {"key": None, "version": ""}


def container_radv() -> str:
    """vulkan-radeon in the llama container; re-read when the container restarts or a runtime job writes its log."""
    started = subprocess.run(["docker", "inspect", "--format", "{{.State.StartedAt}}", "llama"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
    with contextlib.suppress(OSError):
        started += f"/{JOB_LOG.stat().st_mtime_ns}"
    if not started or started != _radv_cache["key"]:
        result = subprocess.run(["docker", "exec", "llama", "pacman", "-Q", "vulkan-radeon"], capture_output=True, text=True, timeout=20)
        version = result.stdout.split()[1] if result.returncode == 0 and len(result.stdout.split()) > 1 else ""
        _radv_cache.update(key=started if version else None, version=version)
    return _radv_cache["version"]


def defaults() -> dict[str, str]:
    """ROCm / Vulkan tools new builds use; a removed install falls back to the system's."""
    state = load_json(STATE, {})
    rocm, sdk = state.get("build_rocm", "system"), state.get("build_vulkan", "system")
    if rocm != "system" and not (ROCM_ROOT / rocm / ".rocsy-installed").exists():
        rocm = "system"
    if sdk != "system" and not (VULKAN_ROOT / sdk / ".rocsy-installed").exists():
        sdk = "system"
    return {"build_rocm": rocm, "build_vulkan": sdk}


def build_env(rocm_id: str, sdk_id: str) -> dict[str, Any]:
    """What corebackend's build job needs for a given ROCm / Vulkan SDK choice."""
    root = rocm_path(rocm_id)
    sdk = vulkan_sdk_path(sdk_id)
    return {"rocm_id": rocm_id or "system", "rocm": str(root), "vulkan_sdk": str(sdk) if sdk else "",
            "vulkan_id": sdk_id or "system"}


# -------------------------------------------------------------------- jobs --

def unit_running() -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", f"{UNIT}.service"]).returncode == 0


def job_view() -> dict[str, Any]:
    job = load_json(JOB, {})
    if job and job.get("phase") not in ("done", "failed", "cancelled") and not unit_running():
        job.update(phase="failed", error=job.get("error") or "The job stopped unexpectedly")
    return job


def start_job(kind: str, version: str = "") -> dict[str, Any]:
    if unit_running():
        raise Refused("Another runtime download or update is running - wait for it or cancel it")
    if kind == "rocm-install":
        entry = next((i for i in refresh_catalog()["rocm"] if i["version"] == version), None)
        if not entry:
            raise Refused(f"ROCm {version} isn't offered for {FAMILY}")
        if (ROCM_ROOT / version / ".rocsy-installed").exists():
            raise Refused(f"ROCm {version} is already installed")
        need = entry["size"] * 5  # tarball + extracted libraries, with room to spare
        if shutil.disk_usage(ROCM_ROOT.parent if not ROCM_ROOT.exists() else ROCM_ROOT).free < need:
            raise Refused(f"Not enough disk space: about {need // 1024 ** 3} GB needed")
        job = {"kind": kind, "version": version, "url": entry["url"], "total": entry["size"]}
    elif kind == "vulkan-install":
        if version not in refresh_catalog()["vulkan"]:
            raise Refused(f"Vulkan SDK {version} isn't offered")
        if (VULKAN_ROOT / version / ".rocsy-installed").exists():
            raise Refused(f"Vulkan SDK {version} is already installed")
        url = VULKAN_TARBALL.format(v=version)
        job = {"kind": kind, "version": version, "url": url, "total": head_size(url)}
    elif kind == "radv-update":
        job = {"kind": kind, "version": refresh_catalog().get("radv_latest", "")}
    else:
        raise Refused("Unknown job")
    ROCM_ROOT.mkdir(parents=True, exist_ok=True)
    job.update(phase="queued", bytes=0, started=time.time(), error="")
    save_json(JOB, job)
    JOB_LOG.write_text("")
    result = subprocess.run(["systemd-run", "--user", f"--unit={UNIT}", "--collect", "--quiet",
                             "-p", "Nice=15", "-p", "IOSchedulingClass=idle",
                             sys.executable, str(Path(__file__).resolve()), "job"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        job.update(phase="failed", error=(result.stderr or "systemd-run failed").strip()[:300])
        save_json(JOB, job)
        raise RuntimeError(job["error"])
    return job


def cancel_job() -> str:
    if not unit_running():
        raise Refused("Nothing is running")
    subprocess.run(["systemctl", "--user", "stop", f"{UNIT}.service"], timeout=60)
    job = load_json(JOB, {})
    job.update(phase="cancelled", finished=time.time())
    save_json(JOB, job)
    return "Cancelled (a partial download is kept and resumes next time)"


def run_job() -> int:
    job = load_json(JOB, {})
    log = JOB_LOG.open("a", buffering=1)

    def phase(name: str, **extra: Any) -> None:
        job.update(phase=name, **extra)
        save_json(JOB, job)
        log.write(f"=== {name} ===\n")

    try:
        if job["kind"] in ("rocm-install", "vulkan-install"):
            root = ROCM_ROOT if job["kind"] == "rocm-install" else VULKAN_ROOT
            downloads = root / ".downloads"
            downloads.mkdir(parents=True, exist_ok=True)
            part = downloads / (job["url"].rsplit("/", 1)[-1] + ".part")
            phase("download")
            download(job, part, log)
            phase("verify")
            log.write("Testing the whole archive before installing...\n")
            test = subprocess.run(["tar", "-tf", str(part)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if test.returncode != 0:
                part.unlink(missing_ok=True)
                raise RuntimeError("The download is damaged (archive test failed) - it was removed; try again")
            phase("extract")
            staging = Path(tempfile.mkdtemp(dir=root, prefix=f".extract-{job['version']}-"))
            if job["kind"] == "rocm-install":
                safe_extract(part, staging, log)
                target_root = staging
            else:
                safe_extract(part, staging, log)
                inner = staging / job["version"]
                if not (inner / "x86_64").is_dir():
                    raise RuntimeError("Unexpected Vulkan SDK layout (no x86_64 folder)")
                target_root = inner
            check_install(job["kind"], target_root, log)
            final = root / job["version"]
            if final.exists():
                shutil.rmtree(final)
            os.replace(target_root, final)
            shutil.rmtree(staging, ignore_errors=True)
            size = sum(p.stat().st_size for p in final.rglob("*") if p.is_file() and not p.is_symlink())
            save_json(final / ".rocsy-installed", {"installed": time.time(), "size": size, "source": job["url"]})
            part.unlink(missing_ok=True)
            phase("done", finished=time.time(), size=size)
            return 0
        if job["kind"] == "radv-update":
            phase("build")
            # --no-cache: otherwise Docker reuses the old package layer and nothing updates.
            # Only this image changes; the host's own Vulkan driver packages are untouched.
            try:
                run(["docker", "compose", "build", "--pull", "--no-cache", "llama"], log, cwd=COMPOSE_DIR)
            except RuntimeError:
                log.write("Retrying once (Arch mirrors are sometimes mid-sync)...\n")
                time.sleep(30)
                run(["docker", "compose", "build", "--pull", "--no-cache", "llama"], log, cwd=COMPOSE_DIR)
            phase("waiting")
            log.write("Image built. The llama container is recreated as soon as no model is loaded.\n")
            deadline = time.time() + LLAMA_IDLE_WAIT
            while not llama_idle():
                if time.time() > deadline:
                    raise RuntimeError("A model stayed loaded for 6 hours - the new image is built; it applies at the next llama restart")
                time.sleep(30)
            phase("restart")
            run(["docker", "compose", "up", "-d", "--no-deps", "llama"], log, cwd=COMPOSE_DIR)
            job["installed"] = container_radv()
            phase("done", finished=time.time())
            return 0
        raise RuntimeError("Unknown job")
    except Exception as exc:  # noqa: BLE001
        log.write(f"\nFAILED: {exc}\n")
        job.update(phase="failed", error=str(exc)[:300], finished=time.time())
        save_json(JOB, job)
        return 1


def download(job: dict[str, Any], part: Path, log) -> None:
    have = part.stat().st_size if part.exists() else 0
    total = job.get("total") or 0
    if total and have > total:
        part.unlink()
        have = 0
    if not total or have < total:
        log.write(f"Downloading {job['url']} (resuming at {have} bytes)\n")
        proc = subprocess.Popen(["curl", "-fL", "--retry", "5", "--retry-delay", "10", "-C", "-", "-o", str(part), job["url"]],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        while proc.poll() is None:
            job["bytes"] = part.stat().st_size if part.exists() else 0
            save_json(JOB, job)
            time.sleep(2)
        if proc.returncode not in (0, 33):  # 33: range not needed (already complete)
            raise RuntimeError(f"Download failed (curl {proc.returncode}): {(proc.stderr.read() or '').strip()[:200]}")
    job["bytes"] = part.stat().st_size
    if total and job["bytes"] != total:
        raise RuntimeError(f"Download incomplete: {job['bytes']} of {total} bytes")
    log.write(f"Downloaded {job['bytes']} bytes\n")


def safe_extract(archive: Path, dest: Path, log) -> None:
    """Extracts without letting an entry escape dest (absolute paths, .., outside links)."""
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")
    log.write(f"Extracted into {dest}\n")


def check_install(kind: str, root: Path, log) -> None:
    if kind == "rocm-install":
        needed = ["lib/libamdhip64.so", "lib/llvm/bin/clang", "bin/hipconfig"]
    else:
        needed = ["x86_64/include/vulkan/vulkan.h", "x86_64/bin/glslc"]
    missing = [n for n in needed if not (root / n).exists()]
    if missing:
        raise RuntimeError("The install is incomplete, missing: " + ", ".join(missing))
    log.write("Install looks complete: " + ", ".join(needed) + "\n")


def run(args: list[str], log, cwd: Path | None = None) -> None:
    log.write("$ " + " ".join(args) + "\n")
    proc = subprocess.run(args, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} {args[1]} failed (see the log)")


def llama_idle() -> bool:
    result = subprocess.run(["docker", "exec", "llama", "ps", "-eo", "args"], capture_output=True, text=True, timeout=20)
    return result.returncode == 0 and not any(line.startswith("/home/") and "llama-server" in line for line in result.stdout.splitlines())


# ------------------------------------------------------------------ actions --

def builds_using(kind: str, version: str) -> list[str]:
    import corebackend  # noqa: PLC0415 - avoids a module cycle at import time
    users = []
    for path in corebackend.build_dirs():
        meta = corebackend.read_meta(path)
        if (kind == "rocm" and meta.get("rocm_id") == version) or (kind == "vulkan" and meta.get("vulkan_id") == version):
            users.append(path.name)
    return users


def delete_install(kind: str, version: str) -> str:
    if not VERSION_RE.fullmatch(version or ""):
        raise Refused("Unknown version")
    root = ROCM_ROOT if kind == "rocm" else VULKAN_ROOT
    folder = root / version
    if not (folder / ".rocsy-installed").exists():
        raise Refused("Not installed")
    if unit_running():
        raise Refused("A runtime job is running - wait for it first")
    key = "build_rocm" if kind == "rocm" else "build_vulkan"
    if defaults()[key] == version:
        raise Refused("It is the default for new builds - pick another default first")
    if users := builds_using(kind, version):
        raise Refused("These llama.cpp builds use it: " + ", ".join(users) + " - delete them first")
    shutil.rmtree(folder)
    return f"Deleted {'ROCm' if kind == 'rocm' else 'Vulkan SDK'} {version}"


def set_default(kind: str, version: str) -> str:
    if kind == "rocm":
        rocm_path(version)
    else:
        vulkan_sdk_path(version)
    state = load_json(STATE, {})
    state["build_rocm" if kind == "rocm" else "build_vulkan"] = version or "system"
    save_json(STATE, state)
    name = "system" if version in ("", "system") else version
    return f"New llama.cpp builds use {'ROCm' if kind == 'rocm' else 'Vulkan tools'} {name}"


def overview() -> dict[str, Any]:
    catalog = refresh_catalog()
    installed = {i["version"] for i in rocm_installs()}
    vk_installed = {i["version"] for i in vulkan_installs()}
    radv = container_radv()
    return {
        "family": FAMILY,
        "rocm": {"system": system_rocm(), "installed": [{**i, "builds": builds_using("rocm", i["version"])} for i in rocm_installs()],
                 "available": [{**i, "installed": i["version"] in installed} for i in catalog["rocm"]]},
        "vulkan": {"system": system_vulkan_tools(), "installed": [{**i, "builds": builds_using("vulkan", i["version"])} for i in vulkan_installs()],
                   "available": [{"version": v, "installed": v in vk_installed} for v in catalog["vulkan"]],
                   "radv": {"installed": radv.split(":", 1)[-1], "latest": catalog.get("radv_latest", ""),
                            "update": bool(radv and catalog.get("radv_latest") and vkey(radv) < vkey(catalog["radv_latest"]))}},
        "defaults": defaults(), "job": job_view(), "catalog_error": catalog.get("error", ""),
        "paths": {"rocm": str(ROCM_ROOT), "vulkan": str(VULKAN_ROOT)},
    }


def log_tail(lines: int = 400) -> str:
    try:
        return "\n".join(JOB_LOG.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def handle(method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    """hostctl routes /core/runtime[/...] here."""
    try:
        if method == "GET" and path == "/core/runtime":
            with _lock:
                return 200, overview()
        if method == "GET" and path == "/core/runtime/log":
            return 200, {"log": log_tail()}
        if method != "POST":
            return 404, {"error": "not found"}
        version = str(body.get("version") or "")
        kind = str(body.get("kind") or "")
        if path == "/core/runtime/refresh":
            with _lock:
                refresh_catalog(force=True)
            return 200, {"message": "Checked for new versions"}
        if path == "/core/runtime/install":
            job_kind = {"rocm": "rocm-install", "vulkan": "vulkan-install"}.get(kind)
            if not job_kind:
                raise Refused("Unknown runtime")
            return 200, {"message": f"Downloading {'ROCm' if kind == 'rocm' else 'Vulkan SDK'} {version}", "job": start_job(job_kind, version)}
        if path == "/core/runtime/radv-update":
            return 200, {"message": "Updating the Vulkan driver in the llama container", "job": start_job("radv-update")}
        if path == "/core/runtime/cancel":
            return 200, {"message": cancel_job()}
        if path == "/core/runtime/delete":
            return 200, {"message": delete_install(kind, version)}
        if path == "/core/runtime/default":
            return 200, {"message": set_default(kind, version)}
        return 404, {"error": "not found"}
    except Refused as exc:
        return 409, {"error": str(exc)}


if __name__ == "__main__":
    import dockeraccess  # the docker group in the session that installed Rocsy (see dockeraccess.py)
    dockeraccess.reexec_with_docker_group()
    if len(sys.argv) == 2 and sys.argv[1] == "job":
        sys.exit(run_job())
    if len(sys.argv) == 2 and sys.argv[1] == "overview":
        print(json.dumps(overview(), indent=1)[:6000])
        sys.exit(0)
    sys.exit("usage: coreruntime.py job | overview")
