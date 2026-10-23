"""Versioned llama-swap builds with the Rocsy Addons source patch."""
from __future__ import annotations

import json
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from paths import HERMES_HOME, ROOT as ROCSY_ROOT

ROOT = Path(os.getenv("CORE_SWAP_ROOT", ROCSY_ROOT / "llama-swap-builds"))
REPO = ROOT / "repo.git"
BUILDS = ROOT / "builds"
ACTIVE = ROOT / "active"
STATE = ROOT / "state.json"
PATCH = Path(__file__).resolve().parent / "patches/llama-swap/rocsy-addons.patch"
UPSTREAM = "https://github.com/mostlygeek/llama-swap.git"
COMPOSE_DIR = ROCSY_ROOT / "BunnyBytes"
UNIT = "core-llama-swap-build"
TAG = re.compile(r"v[0-9]+")
BUILD_ID = re.compile(r"v[0-9]+-rocsy-[0-9a-f]{8}")
LOCK = threading.Lock()
FETCH = {"running": False, "error": ""}
MODEL_SUFFIXES = (".gguf", ".safetensors", ".ckpt", ".pt", ".pth", ".onnx")
SETTING_FILES = (ROCSY_ROOT / "Llama/config.yaml", ROCSY_ROOT / "BunnyBytes/.env")
OVERRIDES = ROCSY_ROOT / "Llama/overrides"


class Refused(Exception):
    pass


def git(*args: str, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:300] or "Git failed")
    return result


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".state-", delete=False) as file:
        json.dump(data, file)
        name = file.name
    os.replace(name, path)


def patch_hash() -> str:
    import hashlib
    return hashlib.sha256(PATCH.read_bytes()).hexdigest()[:8]


def ensure_repo() -> None:
    if (REPO / "HEAD").exists():
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", str(REPO)], check=True, capture_output=True)
    git("remote", "add", "origin", UPSTREAM)


def check_patch(tag: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="check-", dir=ROOT) as temp:
        git("worktree", "add", "--detach", "--quiet", temp, f"refs/tags/{tag}")
        try:
            result = subprocess.run(["git", "-C", temp, "apply", "--check", str(PATCH)],
                                    capture_output=True, text=True, timeout=30)
            return {"compatible": result.returncode == 0,
                    "reason": "" if result.returncode == 0 else (result.stderr or result.stdout).strip()[:240]}
        finally:
            git("worktree", "remove", "--force", temp, check=False)


def fetch() -> None:
    with LOCK:
        ensure_repo()
        remote = subprocess.run(["git", "ls-remote", "--tags", "--refs", UPSTREAM, "refs/tags/v*"],
                                capture_output=True, text=True, timeout=120,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if remote.returncode:
            raise RuntimeError((remote.stderr or "Could not check llama-swap releases").strip()[:300])
        names = sorted({line.rsplit("/", 1)[-1] for line in remote.stdout.splitlines() if TAG.fullmatch(line.rsplit("/", 1)[-1])},
                       key=lambda name: int(name[1:]), reverse=True)[:30]
        if names:
            git("fetch", "--depth=1", "--no-tags", "origin",
                *(f"+refs/tags/{name}:refs/tags/{name}" for name in names), timeout=600)
        rows = [{"tag": name, **check_patch(name)} for name in names]
        write_json(STATE, {"releases": rows, "checked_at": time.time(), "patch": patch_hash()})


def fetch_async() -> None:
    if FETCH["running"]:
        return
    FETCH.update(running=True, error="")

    def work() -> None:
        try:
            fetch()
        except Exception as exc:
            FETCH["error"] = str(exc)[:300]
        finally:
            FETCH["running"] = False
    threading.Thread(target=work, daemon=True).start()


def running() -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT + ".service"],
                          capture_output=True).returncode == 0


def active_id() -> str:
    try:
        target = ACTIVE.resolve(strict=True)
    except OSError:
        return "builtin"
    return target.name if BUILD_ID.fullmatch(target.name) and target.parent == BUILDS else "builtin"


def settings_references(folder: Path) -> list[str]:
    """Saved llama-swap settings that would point at a removed build."""
    refs = []
    override_files = (path for path in OVERRIDES.iterdir() if path.is_file() and path.stat().st_size < 1_000_000) if OVERRIDES.is_dir() else ()
    for source in (*SETTING_FILES, *override_files):
        with contextlib.suppress(OSError, UnicodeError):
            contents = source.read_text()
            if str(folder) in contents or folder.name in contents:
                refs.append(str(source).replace(str(Path.home()), "~"))
    return refs


def model_files_in(folder: Path) -> list[str]:
    found = []
    for base, _dirs, files in os.walk(folder, followlinks=False):
        for name in files:
            path = Path(base) / name
            if path.is_symlink():
                continue
            with contextlib.suppress(OSError):
                if name.lower().endswith(MODEL_SUFFIXES) or (name.lower().endswith(".bin") and path.stat().st_size > 50 * 1024 ** 2):
                    found.append(str(path.relative_to(folder)))
                    if len(found) >= 5:
                        return found
    return found


def mounts_inside(folder: Path) -> list[str]:
    # A mounted model directory must never be removed, even when it sits under a build.
    with contextlib.suppress(OSError):
        prefix = str(folder) + "/"
        return [mount for line in Path("/proc/self/mountinfo").read_text().splitlines()
                if len(fields := line.split()) > 4 and
                (mount := fields[4].replace("\\040", " ")) and (mount == str(folder) or mount.startswith(prefix))]
    return []


def processes_using(folder: Path) -> list[str]:
    prefix = str(folder) + "/"
    users = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            paths = [(proc / "exe").resolve(), (proc / "cwd").resolve()]
            args = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            if any(str(path) == str(folder) or str(path).startswith(prefix) for path in paths) or prefix in args:
                users.append(proc.name)
    return users


def builds() -> list[dict]:
    active = active_id()
    items = [{"id": "builtin", "tag": "v255", "phase": "ready", "active": active == "builtin",
              "label": "Built-in upstream v255 (gateway Rocsy Addons)", "builtin": True}]
    if BUILDS.exists():
        for folder in sorted(BUILDS.iterdir(), reverse=True):
            if not folder.is_dir() or not BUILD_ID.fullmatch(folder.name):
                continue
            meta = read_json(folder / "meta.json")
            if meta.get("phase") not in ("ready", "failed", "cancelled") and not running() and time.time() - meta.get("created", 0) > 15:
                meta.update(phase="failed", error="Build stopped unexpectedly")
                write_json(folder / "meta.json", meta)
            items.append({**meta, "active": folder.name == active, "exists": (folder / "llama-swap").is_file(),
                          "path": str(folder), "refs": settings_references(folder)})
    return items


def view() -> dict:
    ensure_repo()
    state = read_json(STATE)
    if (not state or state.get("patch") != patch_hash()) and not FETCH["running"] and not FETCH["error"]:
        fetch_async()
    return {"releases": state.get("releases", []), "checked_at": state.get("checked_at", 0),
            "fetch": dict(FETCH), "builds": builds(), "building": running(), "active": active_id(),
            "patch": patch_hash()}


def start_build(tag: str) -> dict:
    if not TAG.fullmatch(tag or ""):
        raise Refused("Unknown llama-swap release")
    if running():
        raise Refused("A llama-swap build is already running")
    state = read_json(STATE)
    if state.get("patch") != patch_hash():
        raise Refused("The Rocsy Addons patch changed; check versions again")
    rows = {row["tag"]: row for row in state.get("releases", [])}
    if tag not in rows or not rows[tag]["compatible"]:
        raise Refused("Rocsy Addons do not apply cleanly to that release")
    ident = f"{tag}-rocsy-{patch_hash()}"
    folder = BUILDS / ident
    if folder.exists():
        raise Refused("That version already has a build; delete it before rebuilding")
    folder.mkdir(parents=True)
    meta = {"id": ident, "tag": tag, "patch": patch_hash(), "phase": "queued", "created": time.time()}
    write_json(folder / "meta.json", meta)
    result = subprocess.run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={UNIT}",
                             "-p", "Nice=19", "-p", "IOSchedulingClass=idle",
                             sys.executable, str(Path(__file__).resolve()), "build", ident],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        meta.update(phase="failed", error=(result.stderr or "systemd-run failed").strip()[:300])
        write_json(folder / "meta.json", meta)
        raise RuntimeError(meta["error"])
    return meta


def run_build(ident: str) -> int:
    folder = BUILDS / ident
    meta = read_json(folder / "meta.json")
    with (folder / "build.log").open("w", buffering=1) as log:
        def phase(name: str) -> None:
            meta["phase"] = name
            write_json(folder / "meta.json", meta)
            log.write(f"\n=== {name} ===\n")

        def step(args: list[str], cwd: Path | None = None) -> None:
            log.write("$ " + " ".join(args) + "\n")
            build_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
            build_env["PATH"] = f"{Path.home() / '.local/bin'}:{HERMES_HOME / 'node/bin'}:" + build_env.get("PATH", "/usr/bin:/bin")
            result = subprocess.run(args, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                    env=build_env)
            if result.returncode:
                raise RuntimeError(f"{args[0]} exited with {result.returncode}; see build log")

        try:
            phase("source")
            with LOCK:
                step(["git", "-C", str(REPO), "worktree", "add", "--detach", str(folder / "src"),
                      f"refs/tags/{meta['tag']}"])
            phase("patch")
            step(["git", "-C", str(folder / "src"), "apply", "--check", str(PATCH)])
            step(["git", "-C", str(folder / "src"), "apply", str(PATCH)])
            phase("ui")
            step(["npm", "ci"], folder / "src/ui")
            step(["npm", "run", "build"], folder / "src/ui")
            phase("compile")
            step(["go", "build", "-tags", "embed_ui", "-o", str(folder / "llama-swap"), "."], folder / "src")
            phase("check")
            step(["go", "test", "-short", "./internal/..."], folder / "src")
            if not (folder / "llama-swap").is_file():
                raise RuntimeError("Compiled binary is missing")
            with LOCK:
                git("worktree", "remove", "--force", str(folder / "src"), check=False)
            meta.update(phase="ready", finished=time.time(), size=(folder / "llama-swap").stat().st_size)
            write_json(folder / "meta.json", meta)
            return 0
        except Exception as exc:
            log.write(f"\nFAILED: {exc}\n")
            meta.update(phase="failed", error=str(exc)[:300], finished=time.time())
            write_json(folder / "meta.json", meta)
            return 1


def cancel_build() -> str:
    if not running():
        raise Refused("No llama-swap build is running")
    subprocess.run(["systemctl", "--user", "stop", UNIT + ".service"], check=True, timeout=60)
    for folder in BUILDS.iterdir():
        if not folder.is_dir() or not BUILD_ID.fullmatch(folder.name):
            continue
        meta = read_json(folder / "meta.json")
        if meta.get("phase") in ("ready", "failed", "cancelled"):
            continue
        git("worktree", "remove", "--force", str(folder / "src"), check=False)
        shutil.rmtree(folder / "src", ignore_errors=True)
        meta.update(phase="cancelled", finished=time.time())
        write_json(folder / "meta.json", meta)
    return "llama-swap build cancelled"


def delete_build(ident: str) -> str:
    if not BUILD_ID.fullmatch(ident or ""):
        raise Refused("Unknown build")
    folder = BUILDS / ident
    if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != BUILDS.resolve():
        raise Refused("Only a versioned llama-swap build folder can be deleted")
    if ident == active_id():
        raise Refused("This llama-swap version is in use; switch versions first")
    if ACTIVE.is_symlink() and Path(os.readlink(ACTIVE)).name == ident:
        raise Refused("This llama-swap version is selected; switch versions first")
    if running():
        raise Refused("Cancel the running build first")
    if models := model_files_in(folder):
        raise Refused("This folder contains model files: " + ", ".join(models) + ". Move them out before deleting it")
    if mounts := mounts_inside(folder):
        raise Refused("Something is mounted inside this build: " + ", ".join(mounts[:4]))
    if users := processes_using(folder):
        raise Refused("This build is used by running processes: " + ", ".join(users[:4]))
    if refs := settings_references(folder):
        raise Refused("Saved settings refer to this build (" + ", ".join(refs[:4]) + "); change them before deleting it")
    git("worktree", "remove", "--force", str(folder / "src"), check=False)
    shutil.rmtree(folder)
    return f"Deleted {ident}"


def verify_container(ident: str) -> bool:
    expected = "/usr/local/bin/llama-swap" if ident == "builtin" else "/opt/rocsy-llama-swap/active/llama-swap"
    for _ in range(15):
        top = subprocess.run(["docker", "top", "llama", "-eo", "pid,args"], capture_output=True, text=True, timeout=15)
        health = subprocess.run(["docker", "exec", "llama", "sh", "-c",
                                 "curl -fsS http://127.0.0.1:${LLAMA_PORT:-8400}/health >/dev/null"],
                                capture_output=True, text=True, timeout=15)
        if top.returncode == 0 and expected in top.stdout and health.returncode == 0:
            return True
        time.sleep(1)
    return False


def activate(ident: str) -> str:
    if ident != "builtin" and (not BUILD_ID.fullmatch(ident or "") or
                               (BUILDS / ident).is_symlink() or
                               read_json(BUILDS / ident / "meta.json").get("phase") != "ready" or
                               not (BUILDS / ident / "llama-swap").is_file()):
        raise Refused("Only a finished Rocsy Addons build can be used")
    if ident == active_id():
        return "That llama-swap version is already in use"
    # Restarting llama-swap unloads its child model. Require the user to unload it first.
    process = subprocess.run(["docker", "top", "llama", "-eo", "pid,comm"], capture_output=True, text=True, timeout=15)
    if process.returncode:
        raise Refused("Cannot verify whether a model is loaded; keep the current version")
    if any("llama-server" in line for line in process.stdout.splitlines()):
        raise Refused("A model is loaded. Unload it before switching llama-swap versions")
    image = subprocess.run(["docker", "compose", "build", "llama"], cwd=COMPOSE_DIR,
                           capture_output=True, text=True, timeout=900)
    if image.returncode:
        raise RuntimeError((image.stderr or image.stdout).strip()[-300:] or "Could not build llama container")
    # The model may have started during the image build; check once more before switching.
    process = subprocess.run(["docker", "top", "llama", "-eo", "pid,comm"], capture_output=True, text=True, timeout=15)
    if process.returncode or any("llama-server" in line for line in process.stdout.splitlines()):
        raise Refused("A model started while preparing the switch; unload it and try again")
    previous = os.readlink(ACTIVE) if ACTIVE.is_symlink() else None
    if ident == "builtin":
        ACTIVE.unlink(missing_ok=True)
    else:
        temp = ROOT / ".active-new"
        temp.unlink(missing_ok=True)
        temp.symlink_to(Path("builds") / ident)
        os.replace(temp, ACTIVE)
    result = subprocess.run(["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "llama"],
                            cwd=COMPOSE_DIR, capture_output=True, text=True, timeout=180)
    if result.returncode or not verify_container(ident):
        ACTIVE.unlink(missing_ok=True)
        if previous:
            ACTIVE.symlink_to(previous)
        restored = subprocess.run(["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "llama"],
                                  cwd=COMPOSE_DIR, capture_output=True, text=True, timeout=180)
        detail = (result.stderr or result.stdout).strip()[-300:] if result.returncode else "The selected llama-swap binary did not start and become healthy"
        if restored.returncode:
            detail += "; restoring the previous container also failed: " + (restored.stderr or restored.stdout).strip()[-200:]
        raise RuntimeError(detail)
    return f"llama-swap switched to {ident}"


def log_tail(ident: str) -> str:
    if not BUILD_ID.fullmatch(ident or ""):
        raise Refused("Unknown build")
    log = BUILDS / ident / "build.log"
    return log.read_text(errors="replace")[-64000:] if log.is_file() else ""


def handle(method: str, path: str, body: dict) -> tuple[int, dict]:
    try:
        if method == "GET" and path == "/core/swap":
            return 200, view()
        match = re.fullmatch(r"/core/swap/log/(v[0-9]+-rocsy-[0-9a-f]{8})", path)
        if method == "GET" and match:
            return 200, {"log": log_tail(match.group(1))}
        if method == "POST" and path == "/core/swap/fetch":
            fetch_async()
            return 200, {"message": "Checking llama-swap releases"}
        if method == "POST" and path == "/core/swap/build":
            return 200, {"message": "Rocsy Addons build started", "build": start_build(str(body.get("tag") or ""))}
        if method == "POST" and path == "/core/swap/cancel":
            return 200, {"message": cancel_build()}
        if method == "POST" and path == "/core/swap/delete":
            return 200, {"message": delete_build(str(body.get("id") or ""))}
        if method == "POST" and path == "/core/swap/activate":
            return 200, {"message": activate(str(body.get("id") or ""))}
        return 404, {"error": "not found"}
    except Refused as exc:
        return 409, {"error": str(exc)}


if __name__ == "__main__":
    import dockeraccess  # the docker group in the session that installed Rocsy (see dockeraccess.py)
    dockeraccess.reexec_with_docker_group()
    if len(sys.argv) == 3 and sys.argv[1] == "build" and BUILD_ID.fullmatch(sys.argv[2]):
        sys.exit(run_build(sys.argv[2]))
    sys.exit("usage: coreswap.py build <id>")
