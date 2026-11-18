"""Core Backend -> Trainer tools: update checks for the LoRA trainers the Trainer runs.

The tools live in ROCSY_ROOT/Model Trainers/vendor (mounted at /opt/trainers in the Trainer
container), plus kohya sd-scripts wherever TRAINER_SDSCRIPTS_DIR points. Three kinds:

- git checkouts on a branch (AI Toolkit, SimpleTuner, ...): compared with their remote branch
  (comfynodes.inspect). "Update" fast-forwards a clean checkout only. A checkout with local
  edits (Fizgig and sd-scripts carry Rocsy's ROCm changes) is reported, never touched.
- pinned releases / commits (Fizgig-latest's tag, Heretic's commit): the newer upstream
  release or commit is shown; Rocsy moves the pin after testing it, so there is no button.
- pip pins of the LLM trainer (install_llm_trainer.sh): newer PyPI versions are listed.

GET  /core/extras/trainer-tools              last check (cheap: no network)
POST /core/extras/trainer-tools/check        check now (git ls-remote, GitHub, PyPI)
POST /core/extras/trainer-tools/update/<id>  fast-forward one clean checkout that is behind
GET  /core/extras/trainer-tools/log          the last update's output
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import comfynodes
from paths import ROOT

TRAINERS = Path(os.getenv("CORE_TRAINERS_DIR", ROOT / "Model Trainers"))
VENDOR = TRAINERS / "vendor"
TRAINER_APP = ROOT / "Trainer"
STATE_DIR = Path(os.getenv("CORE_EXTRAS_STATE", ROOT / "core-backend-extras"))
CHECK_FILE = STATE_DIR / "trainer-tools.json"
LOG_FILE = STATE_DIR / "trainer-tools-update.log"
LOCK = threading.Lock()
_running: dict = {"id": "", "thread": None}

# (id, label, folder under vendor/ or None, what it trains, extra git steps after a pull)
GIT_TOOLS = [
    ("fizgig", "Fizgig", "Fizgig", "Krea 2 · MiniMax H3 · Flux.2 Klein", []),
    ("ai-toolkit", "AI Toolkit", "ai-toolkit", "LTX 2.5 · YuE2 · upstream recipes", [["submodule", "update", "--init", "--recursive"]]),
    ("simpletuner", "SimpleTuner", "SimpleTuner", "MiniMax Music 3", []),
    ("diffsynth", "DiffSynth-Studio", "DiffSynth-Studio", "Qwen Image (manual runs)", []),
    ("abliteration", "Abliteration", "uncensor/abliteration", "Uncensor tools", []),
    ("sd-scripts", "kohya sd-scripts", None, "Anima · SDXL · SD 1.5 · Flux.1 …", []),
]
PINNED_TAG = [("fizgig-latest", "Fizgig (pinned release)", "Fizgig-latest", "shootthesound/Fizgig",
               "Qwen Image 2.1 · MOSS-Image")]
PINNED_PIP = [("heretic", "Heretic", "install_uncensor.sh", "p-e-w/heretic", "Uncensor tools")]
PIP_SCRIPT = ("llm", "LLM trainer packages", "install_llm_trainer.sh", "Chat/text LLM LoRAs")


def _git(path: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _fetch_json(url: str) -> dict | list:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Rocsy-hostctl"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def sdscripts_dir() -> Path | None:
    """kohya sd-scripts: TRAINER_SDSCRIPTS_DIR of the Trainer container (docker-compose.override.yml)."""
    value = os.getenv("TRAINER_SDSCRIPTS_DIR", "")
    if not value:
        try:
            result = subprocess.run(["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", "bunny-trainer"],
                                    capture_output=True, text=True, timeout=10)
            value = next((line.split("=", 1)[1] for line in result.stdout.splitlines()
                          if line.startswith("TRAINER_SDSCRIPTS_DIR=")), "")
        except (OSError, subprocess.TimeoutExpired):
            value = ""
    return Path(value) if value else None


def _tool_path(folder: str | None) -> Path | None:
    return sdscripts_dir() if folder is None else VENDOR / folder


def _check_git(tool_id: str, label: str, folder: str | None, covers: str) -> dict:
    path = _tool_path(folder)
    row = {"id": tool_id, "label": label, "covers": covers, "kind": "git"}
    if not path or not (path / ".git").exists():
        return {**row, "status": "missing"}
    probe = _git(path, "rev-parse", "HEAD")
    if probe.returncode and "dubious ownership" in probe.stderr:
        return {**row, "path": str(path), "status": "error",
                "error": "owned by another user (made inside the Trainer container); git won't read it from the host"}
    info = comfynodes.inspect(path)
    info.pop("dir", None)
    info.pop("disabled", None)
    row.update(info, path=str(path))
    if row["status"] == "behind":
        fetched = _git(path, "fetch", "--quiet", "origin", row.get("branch") or "HEAD", timeout=120)
        if fetched.returncode == 0:
            count = _git(path, "rev-list", "--count", "HEAD..FETCH_HEAD").stdout.strip()
            if count.isdigit():
                row["behind"] = int(count)
    row["updatable"] = row["status"] == "behind"
    return row


def _check_tag(tool_id: str, label: str, folder: str, repo: str, covers: str) -> dict:
    path = VENDOR / folder
    row = {"id": tool_id, "label": label, "covers": covers, "kind": "pinned", "updatable": False}
    if not (path / ".git").exists():
        return {**row, "status": "missing"}
    current = _git(path, "describe", "--tags", "--abbrev=0").stdout.strip()
    row.update(path=str(path), version=current)
    try:
        latest = str(_fetch_json(f"https://api.github.com/repos/{repo}/releases/latest").get("tag_name") or "")
    except Exception as exc:  # noqa: BLE001 - shown on the row
        return {**row, "status": "error", "error": f"GitHub: {exc}"[:200]}
    row["latest"] = latest
    row["status"] = "current" if not latest or latest == current else "newer"
    return row


def _check_pinned_commit(tool_id: str, label: str, script: str, repo: str, covers: str) -> dict:
    row = {"id": tool_id, "label": label, "covers": covers, "kind": "pinned", "updatable": False}
    try:
        text = (TRAINER_APP / script).read_text()
    except OSError:
        return {**row, "status": "missing"}
    match = re.search(re.escape(repo) + r"\.git@([0-9a-f]{7,40})", text)
    if not match:
        return {**row, "status": "error", "error": f"No pinned commit found in {script}"}
    pinned = match.group(1)
    row["version"] = pinned[:12]
    listed = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}.git", "HEAD"], capture_output=True, text=True,
                            timeout=30, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    head = listed.stdout.split()[0] if listed.returncode == 0 and listed.stdout.split() else ""
    if not head:
        return {**row, "status": "error", "error": (listed.stderr.strip().splitlines() or ["git ls-remote failed"])[-1][:200]}
    row["latest"] = head[:12]
    row["status"] = "current" if head.startswith(pinned) else "newer"
    return row


def _check_pip(tool_id: str, label: str, script: str, covers: str) -> dict:
    row = {"id": tool_id, "label": label, "covers": covers, "kind": "pip", "updatable": False}
    try:
        text = (TRAINER_APP / script).read_text()
    except OSError:
        return {**row, "status": "missing"}
    pins = dict(re.findall(r'"([A-Za-z0-9_.-]+)==([^"\s]+)"', text))
    newer, errors = [], []
    for name, version in sorted(pins.items()):
        try:
            latest = str(_fetch_json(f"https://pypi.org/pypi/{name}/json")["info"]["version"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}"[:80])
            continue
        if latest != version:
            newer.append({"name": name, "pinned": version, "latest": latest})
    row.update(version=f"{len(pins)} pinned packages", packages=newer)
    row["status"] = "error" if errors and not newer and len(errors) == len(pins) else "newer" if newer else "current"
    if errors:
        row["error"] = "; ".join(errors)[:300]
    return row


def check() -> dict:
    tools = [_check_git(*spec[:4]) for spec in GIT_TOOLS]
    tools += [_check_tag(*spec) for spec in PINNED_TAG]
    tools += [_check_pinned_commit(*spec) for spec in PINNED_PIP]
    tools.append(_check_pip(*PIP_SCRIPT))
    tools = [tool for tool in tools if tool["status"] != "missing" or tool["id"] in {"fizgig", "ai-toolkit"}]
    data = {"checked": time.time(), "tools": tools,
            "available": sum(1 for tool in tools if tool["status"] in {"behind", "newer"})}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CHECK_FILE.write_text(json.dumps(data, indent=1))
    return status()


def status() -> dict:
    try:
        data = json.loads(CHECK_FILE.read_text())
    except (OSError, ValueError):
        data = {"checked": None, "tools": [], "available": 0}
    data["updating"] = _running["id"] if _running["thread"] and _running["thread"].is_alive() else ""
    return data


def _update_worker(tool_id: str, path: Path, steps: list[list[str]]) -> None:
    with LOG_FILE.open("w") as output:
        output.write(f"=== Updating {tool_id} in {path} ===\n")
        output.flush()
        ok = True
        for args in [["pull", "--ff-only"], *steps]:
            output.write(f"$ git {' '.join(args)}\n")
            output.flush()
            result = subprocess.run(["git", "-C", str(path), *args], stdout=output, stderr=subprocess.STDOUT,
                                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=1800)
            if result.returncode:
                ok = False
                break
        output.write("\nDone. If the tool's requirements changed, reinstall it from the Trainer page "
                     "(its Install button) before the next run.\n" if ok else "\nThe update stopped; the checkout is as git left it.\n")
    try:
        check()
    except Exception:  # noqa: BLE001 - the next manual check refreshes it
        pass


def start_update(tool_id: str) -> tuple[int, dict]:
    spec = next((spec for spec in GIT_TOOLS if spec[0] == tool_id), None)
    if not spec:
        return 404, {"error": "Only git checkouts can be updated here"}
    with LOCK:
        if _running["thread"] and _running["thread"].is_alive():
            return 409, {"error": f"{_running['id']} is updating"}
        row = _check_git(*spec[:4])
        if row["status"] != "behind":
            reason = {"modified": "it has local edits (Rocsy's ROCm changes): update it by hand",
                      "diverged": "it has commits of its own", "current": "it is up to date",
                      "detached": "it is pinned to a commit", "missing": "it is not installed"}.get(row["status"], row["status"])
            return 409, {"error": f"{spec[1]} wasn't updated: {reason}"}
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        thread = threading.Thread(target=_update_worker, args=(tool_id, Path(row["path"]), spec[4]), daemon=True)
        _running.update(id=tool_id, thread=thread)
        thread.start()
    return 202, {"message": f"Updating {spec[1]}…"}


def handle(method: str, path: str) -> tuple[int, dict]:
    if method == "GET" and path == "/core/extras/trainer-tools":
        return 200, status()
    if method == "GET" and path == "/core/extras/trainer-tools/log":
        return 200, {"log": LOG_FILE.read_text(errors="replace")[-32000:] if LOG_FILE.exists() else ""}
    if method == "POST" and path == "/core/extras/trainer-tools/check":
        return 200, check()
    match = re.fullmatch(r"/core/extras/trainer-tools/update/([a-z0-9-]{1,40})", path)
    if method == "POST" and match:
        return start_update(match.group(1))
    return 404, {"error": "not found"}
