"""Host access for the Rocsy Master Agent ("Files: full" in Settings -> Agentic).

The Agent service only forwards these when that session's Files permission is "full" (it answers
"Unknown tool" otherwise), so the Hermes container never reaches the host on its own:

  POST /host/exec   {"command", "cwd"?, "timeout"?}  -> {"exit_code", "output", "cwd"}
                    bash -lc COMMAND as the host user (login PATH: ~/.local/bin, claude, ...)
  POST /host/read   {"path"}                           -> {"path", "text"} for small text files, else
                    {"path", "copy"}: a copy under the workspace (host-files/) at its Hermes path
  POST /host/write  {"path", "text"? | "from"?}        -> {"path", "backup"}
                    "from" is a file the agent made (Hermes paths like /opt/data/... are mapped);
                    an existing file is backed up next to itself first (NAME.bak-rocsy-YYYYmmdd-HHMMSS)

Paths starting /opt/data are the Hermes container's names and are mapped to the host
(codingclis.host_paths); everything else is a real host path. ~ is the host user's home.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from codingclis import HERMES_HOME, host_paths

MAX_OUTPUT = 60_000          # characters of command output returned (the tail)
MAX_TEXT = 200_000           # bytes of a file returned inline
MAX_TIMEOUT = 1800
COPIES = HERMES_HOME / "rocsy-workspace" / "host-files"


def _path(raw: Any) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Give a path")
    return Path(os.path.expanduser(host_paths(text)))


def _hermes_name(path: Path) -> str:
    """How the Hermes container names a host path under ~/.hermes (else the host path itself)."""
    try:
        return "/opt/data/" + str(path.resolve().relative_to(HERMES_HOME.resolve()))
    except ValueError:
        return str(path)


def execute(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    command = str(body.get("command") or "")
    if not command.strip():
        return 400, {"error": "Give a command"}
    try:
        cwd = _path(body.get("cwd") or "~")
    except ValueError as exc:
        return 400, {"error": str(exc)}
    if not cwd.is_dir():
        return 400, {"error": f"No such folder on the host: {cwd}"}
    try:
        timeout = max(1, min(MAX_TIMEOUT, int(body.get("timeout") or 120)))
    except (TypeError, ValueError):
        timeout = 120
    try:
        proc = subprocess.run(["bash", "-lc", host_paths(command)], cwd=cwd, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                              timeout=timeout, start_new_session=True)
        code, output = proc.returncode, proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        code, output = 124, (out.decode(errors="replace") if isinstance(out, bytes) else out) + f"\n[stopped after {timeout} s]"
    if len(output) > MAX_OUTPUT:
        output = f"[first {len(output) - MAX_OUTPUT} characters cut]\n" + output[-MAX_OUTPUT:]
    return 200, {"exit_code": code, "output": output, "cwd": str(cwd)}


def read(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        path = _path(body.get("path"))
    except ValueError as exc:
        return 400, {"error": str(exc)}
    if not path.is_file():
        return 404, {"error": f"No such file on the host: {path}"}
    try:
        size = path.stat().st_size
        if size <= MAX_TEXT:
            data = path.read_bytes()
            if b"\0" not in data[:8192]:
                try:
                    return 200, {"path": str(path), "text": data.decode()}
                except UnicodeDecodeError:
                    pass
        COPIES.mkdir(parents=True, exist_ok=True)
        dest = COPIES / f"{time.strftime('%Y%m%d-%H%M%S')}-{path.name}"
        shutil.copy2(path, dest)
    except OSError as exc:
        return 400, {"error": f"Cannot read {path}: {exc}"}
    return 200, {"path": str(path), "copy": _hermes_name(dest), "size": size}


def write(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        path = _path(body.get("path"))
        source = _path(body["from"]) if body.get("from") else None
    except ValueError as exc:
        return 400, {"error": str(exc)}
    if source is None and not isinstance(body.get("text"), str):
        return 400, {"error": "Give the new contents (--text) or a file to copy (--from)"}
    if source is not None and not source.is_file():
        return 404, {"error": f"No such file to copy: {source}"}
    if path.is_dir():
        return 400, {"error": f"{path} is a folder"}
    backup = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = f"{path}.bak-rocsy-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(path, backup)
        if source is not None:
            shutil.copyfile(source, path)
        else:
            path.write_text(body["text"])
    except OSError as exc:
        return 400, {"error": f"Cannot write {path}: {exc}"}
    return 200, {"path": str(path), "backup": backup}


ROUTES = {"/host/exec": execute, "/host/read": read, "/host/write": write}
