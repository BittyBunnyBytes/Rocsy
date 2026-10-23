#!/usr/bin/env python3
"""Core Backend: llama.cpp versions converted to RDNA Boost (Models -> Manager -> Core Backend).

RDNA Boost (github.com/nasone32/llama.cpp-RDNA3-7900xtx-opt) is an upstream llama.cpp
commit plus a series of patches. "Converting" an upstream release replays that series
onto the release. A release is only offered when every patch applies without a conflict
(nothing is merged by hand or by AI); conflicting releases are listed with the patch and
files that clash.

Layout under ROOT (~/AI/llama.cpp-builds, mounted read-only into the llama container at
the same path, since llama-server's RUNPATH is absolute):
  repo.git/            bare mirror: upstream ggml-org/llama.cpp + the RDNA Boost fork
  builds/<id>/build/   cmake build dir (bin/llama-server + its .so files)
  builds/<id>/meta.json, build.log
  active               symlink -> builds/<id>/build; missing = the built-in build
  state.json           RDNA Boost versions seen (the fork can be force-pushed)

llama-launch uses `active` on every model load, so switching never kills a loaded model.
Builds run detached under systemd-run (survive hostctl restarts). Every build is a ROCm GPU build;
only the compiler runs on the CPU, at low priority.
Standard library only; also runnable as `corebackend.py build <id>` (the build job).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from paths import ROOT as ROCSY_ROOT
from typing import Any

ROOT = Path(os.getenv("CORE_LLAMA_ROOT", ROCSY_ROOT / "llama.cpp-builds"))
REPO = ROOT / "repo.git"
BUILDS = ROOT / "builds"
ACTIVE = ROOT / "active"
STATE = ROOT / "state.json"
COMPAT_CACHE = ROOT / "compat-cache.json"
UPSTREAM_URL = "https://github.com/ggml-org/llama.cpp.git"
BOOST_URL = "https://github.com/nasone32/llama.cpp-RDNA3-7900xtx-opt.git"
BOOST_BRANCH = "main"
UNIT = "core-llama-build"
ROCM = os.getenv("ROCM_PATH", "/opt/rocm")
GPU_TARGETS = os.getenv("CORE_LLAMA_GPU_TARGETS", "gfx1100;gfx1101")
BUILTIN = {"id": "builtin", "path": str(ROCSY_ROOT / "llama.cpp-rdna-boost/build-rocm-gfx1100-gfx1101-rdna-boost"),
           "label": "Built-in build (Oct 4)", "release": "b10773", "boost": "15995a12"}
RELEASES_SHOWN = 40
ID_RE = re.compile(r"(?:b\d+-(?:boost-[0-9a-f]{8}|stock)(?:-vk)?(?:-rocm\d+-\d+-\d+)?|rocmfpx-[0-9a-f]{8}-gfx[0-9a-f]+)")
HOME = Path.home()
LEGACY_ROOTS = [(ROCSY_ROOT / "llama.cpp-rdna-boost", "RDNA Boost clone"), (ROCSY_ROOT / "llama.cpp", "Upstream clone")]
LLAMA_CONFIG = ROCSY_ROOT / "Llama/config.yaml"
LLAMA_OVERRIDES = ROCSY_ROOT / "Llama/overrides"
BUNNY_ENV = ROCSY_ROOT / "BunnyBytes/.env"
SCRIPT_GLOBS = [str(ROCSY_ROOT / "*.sh"), str(HOME / "Desktop/*.sh"), str(ROCSY_ROOT / "*/*.sh"),
                str(HOME / ".config/systemd/user/*.service"), str(HOME / ".local/share/applications/*.desktop")]
# llama-server options only RDNA Boost has: a model using one won't start on a stock build.
BOOST_ONLY_FLAGS = [re.compile(r"--lazy-mode\s+on-direct"), re.compile(r"--moe-expert-cache"),
                    re.compile(r"draft-mtp-adaptive")]
TAG_RE = re.compile(r"b\d+")

_lock = threading.Lock()          # git operations on the mirror
_fetch = {"running": False, "error": "", "at": 0.0}
_compat_cache: dict[tuple[str, str], dict[str, Any]] = {}
_compat_dirty = {"value": False}
_counts = {"incompatible": 0}


class Refused(Exception):
    pass


def git(*args: str, timeout: float = 120, check: bool = True) -> str:
    for attempt in range(2):
        result = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=timeout,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_COMMITTER_NAME": "Rocsy Core Backend",
                                     "GIT_COMMITTER_EMAIL": "core-backend@rocsy.local"})
        # A shallow fetch can trip over git's own shallow-file check once; a retry gets through.
        if result.returncode == 0 or "shallow file has changed" not in result.stderr:
            break
        time.sleep(2)
    if check and result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:400] or f"git {args[0]} failed")
    return result.stdout


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_json(path: Path, data: Any) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


# ------------------------------------------------------------------ mirror --

def ensure_repo() -> None:
    if (REPO / "HEAD").exists():
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(REPO)], check=True)
    git("remote", "add", "upstream", UPSTREAM_URL)
    git("remote", "add", "boost", BOOST_URL)


def fetch() -> None:
    """Fetch both repos. Upstream history is kept shallow, from just before the oldest
    RDNA Boost base, which is all a conversion can ever need."""
    with _lock:
        ensure_repo()
        git("fetch", "-q", "--no-tags", "boost", f"+refs/heads/{BOOST_BRANCH}:refs/remotes/boost/{BOOST_BRANCH}", timeout=600)
        tip = git("rev-parse", f"refs/remotes/boost/{BOOST_BRANCH}").strip()
        state = load_state()
        versions = state.setdefault("boost_versions", {})
        # Keep every version seen so a force-push upstream can't make it disappear.
        git("update-ref", f"refs/rocsy/boost/{tip}", tip)
        if tip not in versions:
            versions[tip] = {"first_seen": time.time()}
        save_json(STATE, state)
        started = time.time()
        # The fork's own history already reaches its base; upstream only needs to go back
        # far enough to meet it (deepened if a base isn't reachable yet).
        oldest = min(_commit_time(sha) for sha in versions)
        git("fetch", "-q", "--no-tags", f"--shallow-since={_iso(oldest - 86400 * 45)}", "upstream",
            "+refs/heads/master:refs/remotes/upstream/master", "+refs/tags/b*:refs/tags/b*", timeout=900)
        for sha, info in versions.items():
            for attempt in range(3):
                base = git("merge-base", "refs/remotes/upstream/master", sha, check=False).strip()
                if base:
                    info["base"] = base
                    break
                git("fetch", "-q", "--no-tags", "--deepen=2000", "upstream",
                    "+refs/heads/master:refs/remotes/upstream/master", timeout=900, check=False)
        remote = git("ls-remote", "--tags", "--refs", "upstream", "refs/tags/b*", timeout=120, check=False)
        state["remote_tags"] = sorted({line.rsplit("/", 1)[-1] for line in remote.splitlines()} or set(state.get("remote_tags", [])))
        save_json(STATE, state)
        _compat_cache.clear()
        _compat_dirty["value"] = True
        _fetch["at"] = started
        latest = boost_versions()
        if latest:
            releases(latest[0])  # warm the compatibility cache while still in the background


def fetch_async() -> None:
    if _fetch["running"]:
        return
    _fetch.update(running=True, error="")

    def work() -> None:
        try:
            fetch()
        except Exception as exc:  # noqa: BLE001
            _fetch["error"] = str(exc)[:300]
        finally:
            _fetch["running"] = False
    threading.Thread(target=work, daemon=True).start()


def _commit_time(sha: str) -> int:
    return int(git("show", "-s", "--format=%ct", sha).strip())


def _iso(stamp: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(stamp))


# ------------------------------------------------------------ conversion --

def boost_versions() -> list[dict[str, Any]]:
    out = []
    for sha, info in load_state().get("boost_versions", {}).items():
        base = info.get("base")
        if not base:
            continue
        try:
            line = git("show", "-s", "--format=%ct%x00%s", sha).strip().split("\x00", 1)
            patches = int(git("rev-list", "--count", f"{base}..{sha}").strip())
            base_tag = git("describe", "--tags", "--exact-match", base, check=False).strip() or \
                git("describe", "--tags", "--abbrev=0", base, check=False).strip()
        except Exception:  # noqa: BLE001 - objects pruned or not fetched yet
            continue
        out.append({"sha": sha, "short": sha[:8], "date": int(line[0]), "subject": line[1], "base": base,
                    "base_tag": base_tag, "patches": patches, "first_seen": info.get("first_seen")})
    out.sort(key=lambda v: v["date"], reverse=True)
    return out


def _remember(key: tuple[str, str], result: dict[str, Any]) -> None:
    _compat_cache[key] = result
    _compat_dirty["value"] = True


def load_compat_cache() -> None:
    """Replay results survive hostctl restarts (re-checking every release takes ~20 s)."""
    try:
        rows = json.loads(COMPAT_CACHE.read_text())
    except (OSError, ValueError):
        return
    for tag_sha, boost_sha, result in rows:
        _compat_cache.setdefault((tag_sha, boost_sha), result)


def save_compat_cache() -> None:
    if _compat_dirty["value"]:
        _compat_dirty["value"] = False
        save_json(COMPAT_CACHE, [[*key, result] for key, result in _compat_cache.items()])


def convert(tag_sha: str, boost: dict[str, Any], detail: bool = False) -> dict[str, Any]:
    """Replays the RDNA Boost series onto a release in memory (git merge-tree, no worktree).
    Returns {"ok", "commit"} or {"ok": False, "reason", "patch", "files"}."""
    key = (tag_sha, boost["sha"])
    cached = _compat_cache.get(key)
    if cached and (cached["ok"] or not detail or "files" in cached):
        return cached
    base, tip = boost["base"], boost["sha"]
    if subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", base, tag_sha]).returncode != 0:
        result = {"ok": False, "reason": f"Older than RDNA Boost's base ({boost['base_tag'] or base[:8]})"}
        _remember(key, result)
        return result
    if not detail:
        # Fast check: git replay fails on the first conflict without touching any ref.
        ref = f"refs/heads/rocsy-boost-{tip[:12]}"
        git("update-ref", ref, tip)
        run = subprocess.run(["git", "-C", str(REPO), "replay", "--ref-action=print", "--onto", tag_sha, f"{base}..{ref}"],
                             capture_output=True, text=True, timeout=120)
        if run.returncode == 0:
            match = re.search(r"update \S+ ([0-9a-f]{40})", run.stdout)
            result = {"ok": bool(match), "commit": match.group(1) if match else ""}
            if not match:
                result["reason"] = "git replay gave no result"
            _remember(key, result)
            return result
        result = {"ok": False, "reason": "An RDNA Boost patch conflicts with this release"}
        _remember(key, result)
        return result
    # Detailed: one patch at a time, so the first conflicting patch and its files are known.
    head = tag_sha
    for patch in git("rev-list", "--reverse", f"{base}..{tip}").split():
        merged = subprocess.run(["git", "-C", str(REPO), "merge-tree", "--write-tree", "--name-only", "--no-messages",
                                 f"--merge-base={patch}^", head, patch], capture_output=True, text=True, timeout=120)
        lines = merged.stdout.split("\n")
        if merged.returncode == 1:
            subject = git("show", "-s", "--format=%s", patch).strip()
            files = [f for f in lines[1:] if f.strip()][:12]
            result = {"ok": False, "reason": f"Patch \"{subject}\" conflicts", "patch": patch[:8], "files": files}
            _remember(key, result)
            return result
        if merged.returncode != 0:
            raise RuntimeError(merged.stderr.strip()[:300] or "git merge-tree failed")
        tree = lines[0].strip()
        message = git("show", "-s", "--format=%B", patch)
        head = subprocess.run(["git", "-C", str(REPO), "commit-tree", tree, "-p", head, "-m", message],
                              capture_output=True, text=True, timeout=60,
                              env={**os.environ, "GIT_AUTHOR_NAME": "rdna-boost", "GIT_AUTHOR_EMAIL": "boost@rocsy.local",
                                   "GIT_COMMITTER_NAME": "Rocsy Core Backend",
                                   "GIT_COMMITTER_EMAIL": "core-backend@rocsy.local"}).stdout.strip()
    result = {"ok": True, "commit": head}
    _remember(key, result)
    return result


_refs_cache: dict[tuple, str] = {}


def tag_refs(fmt: str, *sort: str) -> str:
    """`git for-each-ref` over the ~7k release tags, cached until packed-refs or refs/tags change."""
    stamp = []
    for path in (REPO / "packed-refs", REPO / "refs/tags"):
        with contextlib.suppress(OSError):
            stat = path.stat()
            stamp.append((stat.st_mtime_ns, stat.st_size))
    key = (fmt, sort, tuple(stamp))
    if key not in _refs_cache:
        if len(_refs_cache) > 8:
            _refs_cache.clear()
        _refs_cache[key] = git("for-each-ref", *sort, f"--format={fmt}", "refs/tags/b*")
    return _refs_cache[key]


def releases(boost: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = tag_refs("%(refname:short)%00%(objectname)%00%(*objectname)%00%(creatordate:unix)", "--sort=-creatordate")
    rows = []
    for line in raw.splitlines():
        name, obj, peeled, stamp = (line.split("\x00") + ["", "", "", ""])[:4]
        if not TAG_RE.fullmatch(name):
            continue
        rows.append({"tag": name, "sha": peeled or obj, "date": int(stamp or 0)})
    rows.sort(key=lambda r: int(r["tag"][1:]), reverse=True)
    if boost is None:
        return [{**r, "compatible": False, "reason": "Fetch versions first"} for r in rows[:RELEASES_SHOWN]]
    shown = []
    _counts["incompatible"] = 0
    base_num = int(boost["base_tag"][1:]) if TAG_RE.fullmatch(boost.get("base_tag") or "") else 0
    for row in rows:
        num = int(row["tag"][1:])
        if base_num and num < base_num:
            break
        check = convert(row["sha"], boost)
        # Listed incompatible releases get the detailed reason; the scan below them stays fast.
        if not check["ok"] and len(shown) < RELEASES_SHOWN:
            check = convert(row["sha"], boost, detail=True)
        _counts["incompatible"] += not check["ok"]
        if check["ok"] or len(shown) < RELEASES_SHOWN:
            shown.append({**row, "compatible": check["ok"], "reason": check.get("reason", ""),
                          "patch": check.get("patch", ""), "files": check.get("files", [])})
    save_compat_cache()
    return shown


# ------------------------------------------------------------------ builds --

def build_dirs() -> list[Path]:
    return sorted((p for p in BUILDS.glob("*") if p.is_dir() and ID_RE.fullmatch(p.name)), reverse=True) if BUILDS.exists() else []


def read_meta(path: Path) -> dict[str, Any]:
    try:
        return json.loads((path / "meta.json").read_text())
    except (OSError, ValueError):
        return {"id": path.name, "phase": "unknown"}


def active_id() -> str:
    try:
        target = os.readlink(ACTIVE)
    except OSError:
        return "builtin"
    match = re.search(r"builds/(" + ID_RE.pattern + r")/build/?$", target)
    return match.group(1) if match else "builtin"


def unit_running() -> bool:
    result = subprocess.run(["systemctl", "--user", "is-active", "--quiet", f"{UNIT}.service"])
    return result.returncode == 0


def legacy_installs() -> dict[str, dict[str, Any]]:
    """Older llama.cpp build folders made outside Core Backend (build-* in the two source
    clones). Only build folders are ever offered for deletion, never a source tree."""
    found: dict[str, dict[str, Any]] = {}
    for root, label in LEGACY_ROOTS:
        for path in sorted(root.glob("build-*")):
            if not path.is_dir() or path.is_symlink() or str(path) == BUILTIN["path"]:
                continue
            slug = re.sub(r"[^a-z0-9.-]", "-", f"{root.name}.{path.name}".lower())
            rocm = (path / "bin/libggml-hip.so").exists()
            found[f"legacy-{slug}"] = {
                "id": f"legacy-{slug}", "legacy": True, "path": str(path), "phase": "ready",
                "label": f"{label} · {path.name}",
                "kind": "boost" if root.name == "llama.cpp-rdna-boost" else "stock",
                "backend": "ROCm" if rocm else "CPU only"}
    return found


def _system_rocm_changed() -> float:
    """When pacman last installed/upgraded the system ROCm (hip-runtime-amd); 0 if unknown."""
    stamp = 0.0
    with contextlib.suppress(OSError):
        for line in Path("/var/log/pacman.log").read_text(errors="replace").splitlines():
            if "[ALPM] " in line and " hip-runtime-amd " in line and ("upgraded" in line or "installed" in line):
                with contextlib.suppress(ValueError):
                    stamp = time.mktime(time.strptime(line[1:20], "%Y-%m-%dT%H:%M:%S"))
    return stamp


def build_rocm(build_dir: Path, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Which ROCm a build was compiled against: recorded by newer builds, else read from its
    CMake cache (hip_DIR). For /opt/rocm, the system version is exact only if pacman hasn't
    changed ROCm since the build was configured."""
    meta = meta or {}
    if meta.get("rocm_id"):
        return {"rocm_version": meta.get("rocm_version") or "", "rocm_id": meta["rocm_id"], "rocm_exact": True}
    cache = build_dir / "CMakeCache.txt"
    try:
        text = cache.read_text(errors="replace")
    except OSError:
        return {"rocm_version": "", "rocm_id": "", "rocm_exact": False}
    match = re.search(r"^hip_DIR:PATH=(.+)/lib/cmake/hip$", text, re.M)
    root = match.group(1).strip() if match else ""
    if not root:
        return {"rocm_version": "", "rocm_id": "", "rocm_exact": False}
    rocsy = re.fullmatch(re.escape(str(ROCSY_ROOT / "rocm-versions")) + r"/(\d+\.\d+\.\d+)", root)
    if rocsy:
        return {"rocm_version": rocsy.group(1), "rocm_id": rocsy.group(1), "rocm_exact": True}
    version = ""
    with contextlib.suppress(OSError):
        version = (Path(root) / ".info/version").read_text().strip()
    exact = cache.stat().st_mtime >= _system_rocm_changed() > 0
    return {"rocm_version": version, "rocm_id": "system" if root == "/opt/rocm" else root, "rocm_exact": exact}


def rocm_label(info: dict[str, Any]) -> str:
    if not info.get("rocm_version"):
        return "ROCm unknown"
    where = "system" if info.get("rocm_id") == "system" else "Rocsy"
    return f"ROCm {info['rocm_version']}{'' if info.get('rocm_exact', True) else '?'} ({where})"


def builds_view() -> list[dict[str, Any]]:
    running = unit_running()
    active = active_id()
    out = [{**BUILTIN, "phase": "ready", "active": active == "builtin", "builtin": True, "kind": "boost",
            "exists": Path(BUILTIN["path"], "bin/llama-server").exists(), **build_rocm(Path(BUILTIN["path"]))}]
    for path in build_dirs():
        meta = read_meta(path)
        if meta.get("phase") not in ("ready", "failed", "cancelled") and not running:
            meta["phase"], meta["error"] = "failed", meta.get("error") or "The build stopped unexpectedly"
        meta["active"] = meta.get("id") == active
        meta.setdefault("kind", "boost")
        if meta.get("phase") == "ready" or meta.get("rocm_id"):
            meta.update(build_rocm(path / "build", meta))
        out.append(meta)
    out.extend({**item, "active": False, **(build_rocm(Path(item["path"])) if item.get("backend") == "ROCm" else {})}
               for item in legacy_installs().values())
    for item in out:
        if item.get("phase") == "ready" and not item.get("active"):
            item["refs"] = script_refs(Path(item["path"]))
        item["pinned"] = models_pinned_to(item["id"]) if not item.get("legacy") else []
    return out


def upstream_tags() -> list[str]:
    """Every llama.cpp release tag (bNNNN), newest first: local tags + the remote list."""
    names = {line.rsplit("/", 1)[-1] for line in tag_refs("%(refname)").split()}
    names |= set(load_state().get("remote_tags", []))
    return sorted((n for n in names if TAG_RE.fullmatch(n)), key=lambda n: int(n[1:]), reverse=True)


def boost_only_models() -> list[str]:
    """llama-swap models whose options exist only in RDNA Boost (they won't start on a stock build)."""
    try:
        text = LLAMA_CONFIG.read_text()
    except OSError:
        return []
    models, current = [], None
    section = text.split("\nmodels:", 1)[-1]
    for line in section.splitlines():
        match = re.match(r"""^  ["']?([A-Za-z0-9._-]+)["']?:(\s*&\S+)?\s*$""", line)
        if match:
            current = match.group(1)
            continue
        if re.match(r"^\S", line):
            break
        if current and any(flag.search(line) for flag in BOOST_ONLY_FLAGS) and current not in models:
            models.append(current)
    for args in LLAMA_OVERRIDES.glob("*.args"):
        with contextlib.suppress(OSError):
            if any(flag.search(args.read_text()) for flag in BOOST_ONLY_FLAGS) and args.stem not in models:
                models.append(args.stem)
    return models


def overview() -> dict[str, Any]:
    ensure_repo()
    versions = boost_versions() if (REPO / "HEAD").exists() else []
    if not versions and not _fetch["running"] and not _fetch["error"]:
        fetch_async()
    boost = versions[0] if versions else None
    with _lock:
        rels = releases(boost) if boost else []
        tags = upstream_tags()
    compatible = {r["tag"] for r in rels if r["compatible"]}
    return {"fetch": dict(_fetch), "boost": versions, "boost_selected": boost["sha"] if boost else "",
            "releases": rels, "incompatible_total": _counts["incompatible"],
            "all_versions": [[tag, int(tag in compatible)] for tag in tags],
            "builds": builds_view(), "active": active_id(), "building": unit_running(),
            "boost_only_models": boost_only_models(), "gpu_targets": GPU_TARGETS,
            "vulkan_buildable": vulkan_buildable(_default_sdk()), "vulkan_missing": vulkan_missing(_default_sdk()),
            "runtime": _runtime_label()}


def _launch(build_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    folder = BUILDS / build_id
    save_json(folder / "meta.json", meta)
    result = subprocess.run(["systemd-run", "--user", f"--unit={UNIT}", "--collect", "--quiet",
                             "-p", "Nice=19", "-p", "IOSchedulingClass=idle", "-p", "CPUWeight=20",
                             sys.executable, str(Path(__file__).resolve()), "build", build_id],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        meta.update(phase="failed", error=(result.stderr or "systemd-run failed").strip()[:300])
        save_json(folder / "meta.json", meta)
        raise RuntimeError(meta["error"])
    return meta


def _new_folder(build_id: str) -> Path:
    folder = BUILDS / build_id
    if folder.exists():
        if read_meta(folder).get("phase") == "ready":
            raise Refused("That build already exists")
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    return folder


def _default_sdk() -> str | None:
    with contextlib.suppress(Exception):
        return runtime_choice().get("vulkan_sdk") or None
    return None


def _runtime_label() -> str:
    with contextlib.suppress(Exception):
        env = runtime_choice()
        return f"ROCm {env['rocm_version']}{' (system)' if env['rocm_id'] == 'system' else ''}"
    return "ROCm (system)"


def runtime_choice() -> dict[str, Any]:
    """ROCm + Vulkan tools for a new build (Core Backend → GPU runtimes defaults)."""
    import coreruntime  # noqa: PLC0415
    choice = coreruntime.defaults()
    try:
        env = coreruntime.build_env(choice["build_rocm"], choice["build_vulkan"])
    except coreruntime.Refused as exc:
        raise Refused(str(exc)) from None
    env["rocm_version"] = coreruntime.system_rocm()["version"] if env["rocm_id"] == "system" else env["rocm_id"]
    return env


def id_suffix(env: dict[str, Any]) -> str:
    vk = "-vk" if vulkan_buildable(env.get("vulkan_sdk") or None) else ""
    rocm = "" if env["rocm_id"] == "system" else "-rocm" + env["rocm_id"].replace(".", "-")
    return vk + rocm


def start_build(tag: str, boost_sha: str, stock: bool = False) -> dict[str, Any]:
    if not TAG_RE.fullmatch(tag or ""):
        raise Refused("Unknown release")
    if unit_running():
        raise Refused("A build is already running - wait for it or cancel it")
    if stock:
        return start_stock(tag)
    versions = {v["sha"]: v for v in boost_versions()}
    boost = versions.get(boost_sha) or (next(iter(versions.values()), None) if not boost_sha else None)
    if not boost:
        raise Refused("Unknown RDNA Boost version - fetch versions first")
    with _lock:
        tag_sha = git("rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}", check=False).strip()
        if not tag_sha:
            raise Refused(f"{tag} is not compatible with RDNA Boost {boost['short']}: it is older than its base")
        check = convert(tag_sha, boost, detail=True)
        if check["ok"] and subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{check['commit']}^{{commit}}"],
                                          capture_output=True).returncode:
            # A saved result whose replayed commit git has since pruned: replay it again.
            _compat_cache.pop((tag_sha, boost["sha"]), None)
            check = convert(tag_sha, boost, detail=True)
    if not check["ok"]:
        raise Refused(f"{tag} is not compatible with RDNA Boost {boost['short']}: {check['reason']}")
    env = runtime_choice()
    build_id = f"{tag}-boost-{boost['sha'][:8]}" + id_suffix(env)
    folder = _new_folder(build_id)
    with _lock:
        git("update-ref", f"refs/rocsy/builds/{build_id}", check["commit"])
    return _launch(build_id, {
        "id": build_id, "kind": "boost", "release": tag, "release_sha": tag_sha, "boost": boost["sha"],
        "boost_short": boost["short"], "patches": boost["patches"], "commit": check["commit"], "phase": "queued",
        "progress": 0, "created": time.time(), "path": str(folder / "build"), "gpu_targets": GPU_TARGETS, **env})


def start_stock(tag: str) -> dict[str, Any]:
    """Plain upstream llama.cpp, built as released. RDNA Boost is never applied here."""
    if tag not in upstream_tags():
        raise Refused("Unknown release")
    with _lock:
        tag_sha = git("rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}", check=False).strip()
        if not tag_sha:
            git("fetch", "-q", "--no-tags", "--depth=1", "upstream", f"+refs/tags/{tag}:refs/tags/{tag}", timeout=900)
            tag_sha = git("rev-parse", f"refs/tags/{tag}^{{commit}}").strip()
    env = runtime_choice()
    build_id = f"{tag}-stock" + id_suffix(env)
    folder = _new_folder(build_id)
    with _lock:
        git("update-ref", f"refs/rocsy/builds/{build_id}", tag_sha)
    return _launch(build_id, {
        "id": build_id, "kind": "stock", "release": tag, "release_sha": tag_sha, "commit": tag_sha,
        "phase": "queued", "progress": 0, "created": time.time(), "path": str(folder / "build"),
        "gpu_targets": GPU_TARGETS, **env})


def cancel_build() -> str:
    if not unit_running():
        raise Refused("No build is running")
    subprocess.run(["systemctl", "--user", "stop", f"{UNIT}.service"], timeout=60)
    for path in build_dirs():
        meta = read_meta(path)
        if meta.get("phase") not in ("ready", "failed", "cancelled"):
            meta.update(phase="cancelled", finished=time.time())
            save_json(path / "meta.json", meta)
            _remove_worktree(path)
    return "Build cancelled"


def _check_id(build_id: str) -> Path:
    if build_id == "builtin":
        return Path(BUILTIN["path"])
    if not ID_RE.fullmatch(build_id or "") or not (BUILDS / build_id).is_dir():
        raise Refused("Unknown build")
    return BUILDS / build_id


def activate(build_id: str) -> str:
    folder = _check_id(build_id)
    if build_id == "builtin":
        if not Path(BUILTIN["path"], "bin/llama-server").exists():
            raise Refused("The built-in build was deleted")
        if ACTIVE.is_symlink():
            ACTIVE.unlink()
        with contextlib.suppress(Exception):
            write_backend_info()
        return "llama-swap uses the built-in build from its next model load"
    meta = read_meta(folder)
    if meta.get("phase") != "ready" or not (folder / "build/bin/llama-server").exists():
        raise Refused("Only a finished build can be used")
    tmp = ROOT / ".active-new"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(folder / "build")
    os.replace(tmp, ACTIVE)
    with contextlib.suppress(Exception):
        write_backend_info()
    name = (f"ROCmFPX {meta['release']}" if meta.get("kind") == "rocmfpx" else
            f"{meta['release']} + RDNA Boost {meta['boost_short']}" if meta.get("kind") != "stock" else
            f"stock {meta['release']} (no RDNA Boost)")
    return f"llama-swap uses {name} from its next model load"


# ---------------------------------------------------------------- deleting --
# Deleting is the one destructive action here, so it is fenced in: only build folders
# (never a source tree), nothing that holds a model file, nothing in use.

MODEL_SUFFIXES = (".gguf", ".safetensors", ".ckpt", ".pt", ".pth", ".onnx", ".bin")


def model_files_in(path: Path) -> list[str]:
    """Model-like files inside a folder (symlinks are not followed: rmtree removes the link only)."""
    hits = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            full = Path(dirpath, name)
            if full.is_symlink():
                continue
            if name.lower().endswith(MODEL_SUFFIXES[:-1]) or (name.lower().endswith(".bin") and full.stat().st_size > 50 * 1024 ** 2):
                hits.append(str(full.relative_to(path)))
        if len(hits) >= 5:
            break
    return hits


def mounts_inside(path: Path) -> list[str]:
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return []
    prefix = str(path).rstrip("/") + "/"
    return [line.split()[4] for line in lines if line.split()[4].startswith(prefix) or line.split()[4] == str(path)]


def processes_using(path: Path) -> list[str]:
    """Processes started from this folder - on the host or in a container (same host paths)."""
    prefix = str(path).rstrip("/") + "/"
    users = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            argv0 = (proc / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
            if argv0.startswith(prefix):
                users.append(f"{Path(argv0).name} (pid {proc.name})")
    return users


def config_refs(path: Path) -> list[str]:
    """llama-swap config / .env lines that point inside this folder."""
    prefix = str(path).rstrip("/") + "/"
    refs = []
    for source in (LLAMA_CONFIG, BUNNY_ENV):
        with contextlib.suppress(OSError):
            for number, line in enumerate(source.read_text().splitlines(), 1):
                if prefix in line or line.rstrip().endswith(str(path)):
                    refs.append(f"{source.name}:{number}")
    return refs


def models_pinned_to(build_id: str) -> list[str]:
    """Models whose Parameters tab picked this build ("--build <id>" in overrides/<model>.args)."""
    pinned = []
    for args in sorted(LLAMA_OVERRIDES.glob("*.args")):
        with contextlib.suppress(OSError):
            if f"--build {build_id}" in [line.strip() for line in args.read_text().splitlines()]:
                pinned.append(args.stem)
    return pinned


def script_refs(path: Path) -> list[str]:
    """Launch scripts that would stop working (they're warned about, not blocking)."""
    needle = str(path)
    refs = []
    for pattern in SCRIPT_GLOBS:
        for script in Path("/").glob(pattern.lstrip("/")):
            with contextlib.suppress(OSError):
                if script.is_file() and script.stat().st_size < 1_000_000 and needle in script.read_text(errors="replace"):
                    refs.append(str(script).replace(str(Path.home()), "~"))
    return refs


def delete(build_id: str, force: bool = False) -> dict[str, Any]:
    legacy = legacy_installs()
    if build_id in legacy:
        target, name = Path(legacy[build_id]["path"]), legacy[build_id]["label"]
    elif build_id == "builtin":
        target, name = Path(BUILTIN["path"]), BUILTIN["label"]
    else:
        folder = _check_id(build_id)
        target, name = folder, build_id
        meta = read_meta(folder)
        if unit_running() and meta.get("phase") not in ("ready", "failed", "cancelled"):
            raise Refused("This build is still running - cancel it first")
    resolved = target.resolve()
    allowed = [BUILDS.resolve()] + [root.resolve() for root, _ in LEGACY_ROOTS]
    if target.is_symlink() or resolved.parent not in allowed or not (resolved.name.startswith("build") or ID_RE.fullmatch(resolved.name)):
        raise Refused("Only llama.cpp build folders can be deleted here")
    if active_id() == build_id:
        raise Refused("This build is in use by llama-swap - switch to another one first")
    if build_id == "builtin" and active_id() == "builtin":
        raise Refused("The built-in build is in use by llama-swap - switch to another build first")
    build_dir = resolved / "build" if build_id not in legacy and build_id != "builtin" else resolved
    if models := model_files_in(resolved):
        raise Refused("It contains model files, so it won't be deleted: " + ", ".join(models))
    if mounts := mounts_inside(resolved):
        raise Refused("Something is mounted inside it: " + ", ".join(mounts))
    if users := processes_using(build_dir):
        raise Refused("It is running right now: " + ", ".join(users[:4]) + " - unload the model first")
    if pinned := models_pinned_to(build_id):
        raise Refused("These models are set to use it in llama-swap → Parameters: " + ", ".join(pinned) + " - change them first")
    if refs := config_refs(build_dir):
        raise Refused("llama-swap settings point at it (" + ", ".join(refs) + ") - change those first")
    if (refs := script_refs(build_dir)) and not force:
        return {"confirm": True, "message": "These scripts use it and will stop working: " + ", ".join(refs)}
    if build_id not in legacy and build_id != "builtin":
        _remove_worktree(resolved)
    shutil.rmtree(resolved)
    with _lock, contextlib.suppress(RuntimeError):
        git("update-ref", "-d", f"refs/rocsy/builds/{build_id}")
    with contextlib.suppress(Exception):
        write_backend_info()
    return {"message": f"Deleted {name}"}


def log_tail(build_id: str, lines: int = 3000) -> str:
    folder = _check_id(build_id)
    try:
        with (folder / "build.log").open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 400_000))
            return b"\n".join(handle.read().splitlines()[-lines:]).decode(errors="replace")
    except OSError:
        return ""


VULKAN_HEADER = Path("/usr/include/vulkan/vulkan.h")
BACKEND_INFO = ROCSY_ROOT / "Llama/overrides/_backend.json"


def vulkan_missing(sdk: str | None = None) -> list[str]:
    """Host packages llama.cpp's Vulkan backend still needs to compile (none with a Vulkan SDK)."""
    if sdk:
        root = Path(sdk)
        return [] if (root / "include/vulkan/vulkan.h").exists() and (root / "bin/glslc").exists() else ["Vulkan SDK files"]
    missing = []
    if not VULKAN_HEADER.exists():
        missing.append("vulkan-headers")
    if not any(Path(p).exists() for p in ("/usr/share/cmake/SPIRV-Headers", "/usr/lib/cmake/SPIRV-Headers",
                                           "/usr/share/SPIRV-Headers/cmake")) and not list(Path("/usr").glob("*/cmake/SPIRV-Headers*")):
        missing.append("spirv-headers")
    if shutil.which("glslc") is None:
        missing.append("shaderc")
    return missing


def vulkan_buildable(sdk: str | None = None) -> bool:
    return not vulkan_missing(sdk)


def active_bin_dir() -> tuple[Path, dict[str, Any]]:
    build = active_id()
    if build == "builtin":
        rocm = rocm_label(build_rocm(Path(BUILTIN["path"])))
        return Path(BUILTIN["path"], "bin"), {"name": f"{BUILTIN['release']} + RDNA Boost {BUILTIN['boost']} · {rocm}", "boost": True}
    meta = read_meta(BUILDS / build)
    stock = meta.get("kind") == "stock"
    rocm = rocm_label(build_rocm(BUILDS / build / "build", meta))
    name = (f"ROCmFPX {meta.get('release')}" if meta.get("kind") == "rocmfpx" else
            f"{meta.get('release')} stock" if stock else
            f"{meta.get('release')} + RDNA Boost {meta.get('boost_short')}")
    return BUILDS / build / "build/bin", {"name": name + f" · {rocm}", "boost": not stock and meta.get("kind") != "rocmfpx"}


def write_backend_info() -> dict[str, Any]:
    """Tells the Llama Parameters tab (gateway) what the build llama-swap uses supports."""
    bin_dir, about = active_bin_dir()
    vulkan = (bin_dir / "libggml-vulkan.so").exists()
    check = subprocess.run(["docker", "exec", "llama", "sh", "-c", "ls /usr/share/vulkan/icd.d/*.json"],
                           capture_output=True, text=True, timeout=20)
    runtime = check.returncode == 0 and bool(check.stdout.strip())
    reason = ""
    if not vulkan:
        reason = ("The build in use has no Vulkan backend. Build a version in Models → Manager → Core Backend "
                  "(builds include Vulkan automatically now)." if vulkan_buildable() else
                  f"llama.cpp's Vulkan backend needs {', '.join(vulkan_missing())} on the host; install "
                  "that, then build a version in Models → Manager → Core Backend.")
    elif not runtime:
        reason = "The llama container has no Vulkan runtime (vulkan-radeon) - rebuild it."
    builds = []
    if Path(BUILTIN["path"], "bin/llama-server").exists():
        info = build_rocm(Path(BUILTIN["path"]))
        builds.append({"id": "builtin", "name": f"Built-in · {BUILTIN['release']} + RDNA Boost {BUILTIN['boost']} · {rocm_label(info)}",
                       "boost": True, "vulkan": Path(BUILTIN["path"], "bin/libggml-vulkan.so").exists(), "rocm": rocm_label(info)})
    for path in build_dirs():
        meta = read_meta(path)
        if meta.get("phase") != "ready" or not (path / "build/bin/llama-server").exists():
            continue
        stock = meta.get("kind") == "stock"
        fpx = meta.get("kind") == "rocmfpx"
        rocm = rocm_label(build_rocm(path / "build", meta))
        builds.append({"id": path.name, "boost": not stock and not fpx, "vulkan": (path / "build/bin/libggml-vulkan.so").exists(),
                       "rocm": rocm, "name": (f"ROCmFPX {meta.get('release')}" if fpx else f"{meta.get('release')} stock (no RDNA Boost)" if stock
                                else f"{meta.get('release')} + RDNA Boost {meta.get('boost_short')}") + f" · {rocm}"})
    info = {"build": about["name"], "boost": about["boost"], "vulkan": vulkan, "runtime": runtime,
            "vulkan_reason": reason, "active_id": active_id(), "builds": builds, "updated": time.time()}
    with contextlib.suppress(OSError):
        save_json(BACKEND_INFO, info)
        BACKEND_INFO.chmod(0o644)  # read by the gateway container's own user
    return info


def background_loop() -> None:
    """Warms the compatibility cache after a hostctl start, then checks for new versions every 6 h."""
    with _lock:
        load_compat_cache()
    with contextlib.suppress(Exception):
        versions = boost_versions()
        if versions:
            with _lock:
                releases(versions[0])
    last_fetch = time.time()
    while True:
        with contextlib.suppress(Exception):
            write_backend_info()
        time.sleep(300)
        if time.time() - last_fetch < 6 * 3600:
            continue
        last_fetch = time.time()
        try:
            fetch()
        except Exception as exc:  # noqa: BLE001
            _fetch["error"] = str(exc)[:300]


def handle(method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    """hostctl routes /core/llamacpp[/...] here."""
    try:
        if method == "GET" and path == "/core/llamacpp":
            return 200, overview()
        match = re.fullmatch(r"/core/llamacpp/log/([A-Za-z0-9-]+)", path)
        if method == "GET" and match:
            return 200, {"log": log_tail(match.group(1))}
        if method != "POST":
            return 404, {"error": "not found"}
        if path == "/core/llamacpp/fetch":
            fetch_async()
            return 200, {"message": "Checking for new versions"}
        if path == "/core/llamacpp/build":
            return 200, {"message": "Build started", "build": start_build(str(body.get("release") or ""), str(body.get("boost") or ""),
                                                                          stock=bool(body.get("stock")))}
        if path == "/core/llamacpp/cancel":
            return 200, {"message": cancel_build()}
        if path == "/core/llamacpp/activate":
            return 200, {"message": activate(str(body.get("id") or ""))}
        if path == "/core/llamacpp/delete":
            return 200, delete(str(body.get("id") or ""), force=bool(body.get("force")))
        return 404, {"error": "not found"}
    except Refused as exc:
        return 409, {"error": str(exc)}


# ------------------------------------------------------------ build job --

def _remove_worktree(folder: Path) -> None:
    src = folder / "src"
    if src.exists():
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(src)], capture_output=True, timeout=120)
        shutil.rmtree(src, ignore_errors=True)
    subprocess.run(["git", "-C", str(REPO), "worktree", "prune"], capture_output=True, timeout=60)


def run_build(build_id: str) -> int:
    folder = BUILDS / build_id
    meta = read_meta(folder)
    log = (folder / "build.log").open("a", buffering=1)

    def phase(name: str, **extra: Any) -> None:
        meta.update(phase=name, **extra)
        save_json(folder / "meta.json", meta)
        log.write(f"\n=== {name} ===\n")

    def step(args: list[str], env: dict[str, str] | None = None, progress: bool = False) -> None:
        log.write("$ " + " ".join(args) + "\n")
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, bufsize=1)
        last = 0.0
        for line in proc.stdout:  # type: ignore[union-attr]
            log.write(line)
            match = progress and re.match(r"\[(\d+)/(\d+)\]", line)
            if match and time.time() - last > 2:
                meta["progress"] = round(int(match.group(1)) / int(match.group(2)) * 100)
                save_json(folder / "meta.json", meta)
                last = time.time()
        if proc.wait() != 0:
            raise RuntimeError(f"{Path(args[0]).name} exited with {proc.returncode} (see the build log)")

    try:
        phase("source", started=time.time())
        _remove_worktree(folder)
        step(["git", "-C", str(REPO), "worktree", "add", "--detach", "--force", str(folder / "src"), meta["commit"]])
        phase("configure")
        # The ROCm this build is compiled against: the system's /opt/rocm or a Rocsy copy
        # (Core Backend → GPU runtimes). It's also what the build runs with later.
        rocm = meta.get("rocm") or ROCM
        sdk = meta.get("vulkan_sdk") or ""
        base_env = {**os.environ, "ROCM_PATH": rocm, "HIP_PATH": rocm,
                    "PATH": f"{rocm}/bin:{rocm}/lib/llvm/bin:" + os.environ.get("PATH", ""),
                    "LD_LIBRARY_PATH": f"{rocm}/lib:{rocm}/lib/llvm/lib"}
        if sdk:  # a Rocsy Vulkan SDK instead of the system's vulkan-headers/spirv-headers/shaderc
            base_env.update(VULKAN_SDK=sdk, PATH=f"{sdk}/bin:" + base_env["PATH"],
                            CMAKE_PREFIX_PATH=sdk, LD_LIBRARY_PATH=f"{sdk}/lib:" + base_env["LD_LIBRARY_PATH"])
        hip = subprocess.run(["hipconfig", "-l"], capture_output=True, text=True, env=base_env)
        env = {**base_env, "HIPCXX": (hip.stdout.strip() or f"{rocm}/lib/llvm/bin") + "/clang"}
        with_vulkan = vulkan_buildable(sdk or None)
        log.write(f"ROCm {meta.get('rocm_version') or ''} at {rocm}; Vulkan tools: {sdk or 'system packages'}\n")
        # Same options as the built-in build (~/AI/build-llama-rocm.sh + its CMakeCache).
        step(["cmake", "-S", str(folder / "src"), "-B", str(folder / "build"), "-G", "Ninja",
              "-DCMAKE_BUILD_TYPE=Release", "-DGGML_HIP=ON", "-DGGML_HIP_GRAPHS=ON",
              f"-DCMAKE_HIP_FLAGS=--rocm-path={rocm} -mllvm --amdgpu-unroll-threshold-local=600",
              f"-DCMAKE_PREFIX_PATH={rocm}" + (f";{sdk}" if sdk else ""),
              f"-DAMDGPU_TARGETS={GPU_TARGETS}", f"-DGPU_TARGETS={GPU_TARGETS}",
              "-DLLAMA_BUILD_SERVER=ON", "-DLLAMA_BUILD_TESTS=OFF",
              # Vulkan backend next to ROCm (Parameters tab -> GPU backend), when it can be built.
              f"-DGGML_VULKAN={'ON' if with_vulkan else 'OFF'}"], env=env)
        meta["backends"] = ["ROCm"] + (["Vulkan"] if with_vulkan else [])
        if rocm != ROCM:
            # llama-launch reads this to run the build with the ROCm it was compiled against.
            (folder / "build/rocm-path").write_text(rocm + "\n")
        phase("compile", progress=0)
        step(["cmake", "--build", str(folder / "build"), "--target", "llama-server", "llama-cli",
              "--parallel", str(os.cpu_count() or 8)], env=env, progress=True)
        phase("check", progress=100)
        server = folder / "build/bin/llama-server"
        ldd = subprocess.run(["ldd", str(server)], capture_output=True, text=True,
                             env={**os.environ, "LD_LIBRARY_PATH": f"{rocm}/lib:{rocm}/lib/llvm/lib"})
        log.write(ldd.stdout)
        missing = [line.split()[0] for line in ldd.stdout.splitlines() if "not found" in line]
        if missing:
            raise RuntimeError("Missing libraries: " + ", ".join(missing))
        # Must be a GPU build: the ROCm/HIP backend has to be linked in.
        hip_libs = [name for name in ("libggml-hip.so", "libamdhip64.so") if name in ldd.stdout]
        if "libggml-hip.so" not in ldd.stdout:
            raise RuntimeError("Not a GPU build: the ROCm backend (libggml-hip) is missing")
        log.write(f"ROCm GPU build OK: {', '.join(hip_libs)} linked (ROCm {meta.get('rocm_version') or 'system'}), compiled for {GPU_TARGETS}\n")
        if "Vulkan" in meta.get("backends", []):
            if not (server.parent / "libggml-vulkan.so").exists():
                raise RuntimeError("The Vulkan backend was requested but libggml-vulkan.so wasn't built")
            log.write("Vulkan backend OK: libggml-vulkan.so built\n")
        # The version check runs with the GPUs hidden so it never touches the XTX; llama.cpp then
        # prints a "no ROCm-capable device" line, which is expected here and filtered out of the log.
        version = subprocess.run([str(server), "--version"], capture_output=True, text=True, timeout=60,
                                 env={**os.environ, "LD_LIBRARY_PATH": f"{rocm}/lib:{rocm}/lib/llvm/lib", "HIP_VISIBLE_DEVICES": "-1",
                                      "ROCR_VISIBLE_DEVICES": "-1"})
        text = "\n".join(line for line in (version.stdout + version.stderr).strip().splitlines()
                         if "ROCm-capable device" not in line and "failed to initialize ROCm" not in line)
        log.write(text + "\n")
        if "version" not in text:
            raise RuntimeError("llama-server --version failed")
        meta["version"] = next((l for l in text.splitlines() if l.startswith("version")), "")
        _remove_worktree(folder)  # the build only needs its own folder at run time
        meta["size"] = sum(p.stat().st_size for p in (folder / "build").rglob("*") if p.is_file())
        phase("ready", finished=time.time())
        with contextlib.suppress(Exception):
            write_backend_info()  # the Parameters tab can offer it right away
        return 0
    except Exception as exc:  # noqa: BLE001
        log.write(f"\nFAILED: {exc}\n")
        meta.update(phase="failed", error=str(exc)[:300], finished=time.time())
        save_json(folder / "meta.json", meta)
        return 1


if __name__ == "__main__":
    import dockeraccess  # the docker group in the session that installed Rocsy (see dockeraccess.py)
    dockeraccess.reexec_with_docker_group()
    if len(sys.argv) == 3 and sys.argv[1] == "build" and ID_RE.fullmatch(sys.argv[2]):
        sys.exit(run_build(sys.argv[2]))
    if len(sys.argv) == 2 and sys.argv[1] == "fetch":
        fetch()
        print(json.dumps(overview(), indent=1)[:4000])
        sys.exit(0)
    sys.exit("usage: corebackend.py build <id> | fetch")
