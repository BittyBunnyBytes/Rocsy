"""Models -> Manager -> Core Backend -> ComfyUI: install and update custom node packs.

GET  /core/comfynodes          installed packs ({dir, remote, disabled}) + the job + the last update check
GET  /core/comfynodes/log      install / update log
POST /core/comfynodes/install  {"repos": ["https://github.com/<owner>/<repo>", ...]}
POST /core/comfynodes/check    compare every git pack with its remote branch (git ls-remote, no download)
POST /core/comfynodes/update   {"dirs": ["ComfyUI-KJNodes", ...]} packs the last check found behind

An install runs as the systemd user unit core-comfy-nodes (Nice 19), one at a time:
git clone (depth 1) into a staging folder in custom_nodes, then `pip install -r
requirements.txt` in ComfyUI's venv with the installed ROCm torch/triton/numpy stack
pinned as constraints (a pack that asks for another torch fails instead of replacing
the ROCm build), then the pack's install.py if it has one (ComfyUI-Manager does the
same). A pack whose dependencies fail is removed again. ComfyUI loads new packs on its
next restart, which the caller asks for separately (busy-guarded in hostctl.py).
Only github.com repo URLs are accepted, and an existing folder is never touched.

An update only moves a pack that is clean and has no commits of its own (the tracked
files match a commit of its remote branch): it fetches that branch, resets to it, then
reinstalls the requirements with the same pins. If they fail, the pack goes back to
the commit it was on. Packs with local edits, local commits, a detached HEAD or no git
remote are reported but never changed.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from paths import COMFY_ROOT, ROOT as ROCSY_ROOT

COMFY = Path(os.getenv("CORE_COMFY_DIR", COMFY_ROOT))
NODES = COMFY / "custom_nodes"
PYTHON = COMFY / ".venv/bin/python3"
STATE_DIR = Path(os.getenv("CORE_EXTRAS_STATE", ROCSY_ROOT / "core-backend-extras"))
STATE = STATE_DIR / "comfy-nodes.json"
LOG = STATE_DIR / "comfy-nodes.log"
UPDATES = STATE_DIR / "comfy-nodes-updates.json"
CHECK_LOCK = threading.Lock()
UNIT = "core-comfy-nodes"
REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
# Never let a pack's requirements replace these (the ROCm torch build and its friends).
PINNED = re.compile(r"^(torch|torchvision|torchaudio|triton|pytorch-triton-rocm|numpy|rocm|rocm-.*|amd-.*|flash[-_]attn)$", re.I)
LOCK = threading.Lock()


class Refused(Exception):
    pass


def running() -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT + ".service"],
                          capture_output=True).returncode == 0


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, STATE)


def remote(path: Path) -> str:
    if not (path / ".git").exists():
        return ""
    result = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"],
                            capture_output=True, text=True, timeout=10)
    return result.stdout.strip() if result.returncode == 0 else ""


def packs() -> list[dict]:
    out = []
    for path in sorted(NODES.iterdir()) if NODES.is_dir() else []:
        if not path.is_dir() or path.name.startswith(".") or path.name == "__pycache__":
            continue
        disabled = path.name.endswith(".disabled")
        out.append({"dir": path.name.removesuffix(".disabled"), "remote": remote(path), "disabled": disabled})
    return out


def view() -> dict:
    state = load_state()
    if state.get("phase") in ("queued", "installing") and not running():
        state.update(phase="failed", error=state.get("error") or "The install job stopped unexpectedly")
        save_state(state)
    return {"installed": COMFY.is_dir() and PYTHON.is_file(), "packs": packs(),
            "job": {**state, "running": running()}, "updates": load_updates()}


def start(repos: list) -> dict:
    if not isinstance(repos, list) or not repos or len(repos) > 20:
        raise Refused("Choose 1-20 node packs")
    clean = []
    for url in repos:
        match = REPO.match(str(url).strip())
        if not match:
            raise Refused(f"Only github.com repositories can be installed: {str(url)[:120]}")
        name = match.group(2)
        if name.startswith(".") or (NODES / name).exists() or (NODES / f"{name}.disabled").exists():
            raise Refused(f"custom_nodes/{name} already exists; it won't be overwritten")
        clean.append({"url": f"https://github.com/{match.group(1)}/{name}", "dir": name, "state": "queued"})
    if not PYTHON.is_file():
        raise Refused("ComfyUI's Python environment is missing")
    with LOCK:
        if running():
            raise Refused("A node install is already running")
        save_state({"phase": "queued", "kind": "install", "started": time.time(), "repos": clean, "error": "",
                    "restart_needed": load_state().get("restart_needed", False)})
        _launch(clean)
    return {"message": f"Installing {len(clean)} node pack{'s' if len(clean) != 1 else ''}"}


def _launch(clean: list) -> None:
    """Start `comfynodes.py run` as the job unit (callers hold LOCK and have saved the state)."""
    args = ["systemd-run", "--user", "--collect", "--quiet", f"--unit={UNIT}",
            "-p", "Nice=19", "-p", "IOSchedulingClass=idle", sys.executable, str(Path(__file__).resolve()), "run"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode:
        save_state({"phase": "failed", "repos": clean, "error": (result.stderr or result.stdout).strip()[:300]})
        raise RuntimeError((result.stderr or result.stdout).strip()[:300])


# ---- update detection ----

def _git(path: Path, *args: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"})


def _is_ancestor(path: Path, older: str, newer: str) -> bool:
    return _git(path, "merge-base", "--is-ancestor", older, newer).returncode == 0


def pack_path(name: str) -> Path | None:
    for candidate in (NODES / name, NODES / f"{name}.disabled"):
        if candidate.is_dir() and candidate.parent == NODES:
            return candidate
    return None


def inspect(path: Path) -> dict:
    """One pack against its remote branch. status: current | behind | ahead | modified | diverged |
    detached | local (no git remote) | error. Only "behind" can be updated."""
    info = {"dir": path.name.removesuffix(".disabled"), "disabled": path.name.endswith(".disabled"), "status": "local"}
    if not (path / ".git").exists():
        return info
    try:
        url = _git(path, "config", "--get", "remote.origin.url").stdout.strip()  # as configured, before insteadOf
        info["remote"] = url
        if not url.startswith(("https://", "http://")):
            return {**info, "status": "local"}
        local = _git(path, "rev-parse", "HEAD").stdout.strip()
        branch = _git(path, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
        info.update(commit=local[:12], branch=branch)
        when = _git(path, "log", "-1", "--format=%ct", "HEAD").stdout.strip()
        info["committed"] = int(when) if when.isdigit() else None
        if not branch:
            return {**info, "status": "detached"}
        listed = _git(path, "ls-remote", "origin", f"refs/heads/{branch}", timeout=25)
        if listed.returncode:
            return {**info, "status": "error", "error": (listed.stderr.strip().splitlines() or ["git ls-remote failed"])[-1][:200]}
        head = listed.stdout.split()[0] if listed.stdout.split() else ""
        if not head:
            return {**info, "status": "error", "error": f"The remote has no branch {branch}"}
        info["latest"] = head[:12]
        dirty = _git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip()
        tracking = _git(path, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}").stdout.strip()
        own_commits = bool(tracking) and local != tracking and not _is_ancestor(path, local, tracking)
        if head == local:
            status = "current"
        elif _git(path, "cat-file", "-e", head + "^{commit}").returncode == 0 and _is_ancestor(path, head, local):
            status = "ahead"  # the remote tip is already in this checkout
        else:
            status = "diverged" if own_commits else "behind"
        if dirty and status in ("behind", "current"):
            info["modified_files"] = len(dirty.splitlines())
            status = "modified" if status == "behind" else status
        return {**info, "status": status}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**info, "status": "error", "error": f"{type(exc).__name__}: {exc}"[:200]}


def load_updates() -> dict:
    try:
        return json.loads(UPDATES.read_text())
    except (OSError, ValueError):
        return {}


def check() -> dict:
    """Check every pack now (parallel ls-remote, ~10 s for 40 packs). One check at a time."""
    if not CHECK_LOCK.acquire(blocking=False):
        raise Refused("An update check is already running")
    try:
        paths = [p for p in sorted(NODES.iterdir()) if p.is_dir() and not p.name.startswith(".")
                 and p.name != "__pycache__"] if NODES.is_dir() else []
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(inspect, paths))
        data = {"checked_at": time.time(), "packs": results,
                "available": sum(1 for r in results if r["status"] == "behind")}
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = UPDATES.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, UPDATES)
        return data
    finally:
        CHECK_LOCK.release()


def start_update(dirs: list) -> dict:
    if not isinstance(dirs, list) or not dirs or len(dirs) > 100:
        raise Refused("Choose the node packs to update")
    known = {item["dir"]: item for item in load_updates().get("packs", [])}
    clean = []
    for name in dict.fromkeys(str(d) for d in dirs):
        item = known.get(name)
        if not item or item.get("status") != "behind" or not pack_path(name):
            raise Refused(f"{name[:80]} has no update from the last check; check again")
        clean.append({"dir": name, "url": item.get("remote", ""), "from": item.get("commit", ""),
                      "to": item.get("latest", ""), "state": "queued"})
    if not PYTHON.is_file():
        raise Refused("ComfyUI's Python environment is missing")
    with LOCK:
        if running():
            raise Refused("A node install or update is already running")
        save_state({"phase": "queued", "kind": "update", "started": time.time(), "repos": clean, "error": "",
                    "restart_needed": load_state().get("restart_needed", False)})
        _launch(clean)
    return {"message": f"Updating {len(clean)} node pack{'s' if len(clean) != 1 else ''}"}


def mark_restarted() -> None:
    state = load_state()
    if state.get("restart_needed"):
        state["restart_needed"] = False
        save_state(state)


def _step(output, args: list[str], cwd: Path | None = None, timeout: int = 1800) -> None:
    output.write("$ " + " ".join(args) + "\n")
    output.flush()
    result = subprocess.run(args, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, timeout=timeout,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} {args[1] if len(args) > 1 else ''} exited with {result.returncode}")


def _constraints() -> Path:
    frozen = subprocess.run([str(PYTHON), "-m", "pip", "list", "--format=json"],
                            capture_output=True, text=True, timeout=120, check=True)
    lines = [f"{p['name']}=={p['version']}" for p in json.loads(frozen.stdout) if PINNED.match(p["name"])]
    path = STATE_DIR / "comfy-nodes-constraints.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


def run() -> int:
    state = load_state()
    state["phase"] = "installing"
    save_state(state)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    installed = 0
    with LOG.open("w") as output:
        try:
            constraints = _constraints()
            output.write("Pinned (never changed by a pack):\n" + constraints.read_text() + "\n")
        except Exception as exc:  # noqa: BLE001
            state.update(phase="failed", error=f"Couldn't read ComfyUI's Python packages: {exc}"[:300])
            save_state(state)
            return 1
        for repo in state.get("repos", []):
            if state.get("kind") == "update":
                installed += _update_one(output, repo, constraints)
                output.flush()
                save_state(state)
                continue
            final = NODES / repo["dir"]
            stage = NODES / f".rocsy-install-{repo['dir']}"
            repo["state"] = "installing"
            save_state(state)
            output.write(f"\n=== {repo['url']} ===\n")
            try:
                if final.exists():
                    raise RuntimeError(f"custom_nodes/{repo['dir']} appeared meanwhile; skipped")
                shutil.rmtree(stage, ignore_errors=True)
                _step(output, ["git", "clone", "--depth", "1", "--recurse-submodules", "--shallow-submodules",
                               repo["url"], str(stage)], timeout=900)
                requirements = stage / "requirements.txt"
                if requirements.is_file():
                    _step(output, [str(PYTHON), "-m", "pip", "install", "-r", str(requirements), "-c", str(constraints)],
                          cwd=stage)
                if (stage / "install.py").is_file():
                    _step(output, [str(PYTHON), "install.py"], cwd=stage)
                if final.exists():
                    raise RuntimeError(f"custom_nodes/{repo['dir']} appeared meanwhile; skipped")
                stage.rename(final)
                repo["state"] = "installed"
                installed += 1
            except Exception as exc:  # noqa: BLE001
                shutil.rmtree(stage, ignore_errors=True)
                repo.update(state="failed", error=str(exc)[:300])
                output.write(f"FAILED: {exc}\n")
            output.flush()
            save_state(state)
    if state.get("kind") == "update":
        with contextlib.suppress(Exception):
            check()  # refresh the statuses the page shows
    failed = [r for r in state.get("repos", []) if r["state"] == "failed"]
    state.update(phase="failed" if failed and not installed else "ready", finished=time.time(),
                 restart_needed=bool(installed) or state.get("restart_needed", False),
                 error=f"{len(failed)} pack(s) failed; see the log" if failed else "")
    save_state(state)
    return 1 if failed else 0


def _update_one(output, repo: dict, constraints: Path) -> int:
    """Move one clean pack to its remote branch tip; back to where it was if anything fails."""
    repo["state"] = "updating"
    path = pack_path(repo["dir"])
    output.write(f"\n=== update {repo['dir']} ===\n")
    old = ""
    try:
        if not path:
            raise RuntimeError("the folder is gone")
        branch = _git(path, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
        old = _git(path, "rev-parse", "HEAD").stdout.strip()
        if not branch:
            raise RuntimeError("HEAD is detached (pinned to a commit); not updated")
        if _git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            raise RuntimeError("it has local edits; not updated")
        tracking = _git(path, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}").stdout.strip()
        if tracking and old != tracking and not _is_ancestor(path, old, tracking):
            raise RuntimeError("it has commits of its own; not updated")
        shallow = _git(path, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
        _step(output, ["git", "-C", str(path), "fetch", *(["--depth", "1"] if shallow else []), "origin", branch], timeout=900)
        new = _git(path, "rev-parse", "FETCH_HEAD").stdout.strip()
        if not new:
            raise RuntimeError("the fetch returned no commit")
        if not tracking and not shallow and not _is_ancestor(path, old, new):
            raise RuntimeError("its commit isn't on the remote branch (local commits?); not updated")
        _step(output, ["git", "-C", str(path), "reset", "--hard", "-q", new])
        if (path / ".gitmodules").is_file():
            _step(output, ["git", "-C", str(path), "submodule", "update", "--init", "--recursive", "--depth", "1"], timeout=900)
        try:
            if (path / "requirements.txt").is_file():
                _step(output, [str(PYTHON), "-m", "pip", "install", "-r", str(path / "requirements.txt"), "-c", str(constraints)],
                      cwd=path)
            if (path / "install.py").is_file():
                _step(output, [str(PYTHON), "install.py"], cwd=path)
        except Exception:
            output.write(f"Requirements failed; going back to {old[:12]}\n")
            _git(path, "reset", "--hard", "-q", old)
            if (path / ".gitmodules").is_file():
                _git(path, "submodule", "update", "--init", "--recursive", timeout=900)
            raise
        repo.update(state="updated", to=new[:12])
        output.write(f"{old[:12]} -> {new[:12]}\n")
        return 1
    except Exception as exc:  # noqa: BLE001
        repo.update(state="failed", error=str(exc)[:300])
        output.write(f"FAILED: {exc}\n")
        return 0


def handle(method: str, path: str, body: dict) -> tuple[int, dict]:
    try:
        if method == "GET" and path == "/core/comfynodes":
            return 200, view()
        if method == "GET" and path == "/core/comfynodes/log":
            return 200, {"log": LOG.read_text(errors="replace")[-64000:] if LOG.exists() else ""}
        if method == "POST" and path == "/core/comfynodes/install":
            return 200, start(body.get("repos"))
        if method == "POST" and path == "/core/comfynodes/check":
            return 200, check()
        if method == "POST" and path == "/core/comfynodes/update":
            return 200, start_update(body.get("dirs"))
        return 404, {"error": "not found"}
    except Refused as exc:
        return 409, {"error": str(exc)}


if __name__ == "__main__":
    import dockeraccess  # the docker group in the session that installed Rocsy (see dockeraccess.py)
    dockeraccess.reexec_with_docker_group()
    if sys.argv[1:] != ["run"]:
        sys.exit("usage: comfynodes.py run")
    sys.exit(run())
