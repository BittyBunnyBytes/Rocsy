"""External coding CLIs (Claude Code, Codex, Kimi Code, Gemini CLI ...) as sub-agent backends.

Settings -> Sub-agents lists the CLIs installed on this host under the local models, each with
the models it can use (read from the CLI's own config / model cache, never guessed from a
network call). A sub-agent set to one of them thinks through that CLI instead of llama or Hermes:

  GET  /clis          installed CLIs + detected models (cached a minute)
  POST /clis/run      one headless CLI turn, streamed back as NDJSON events:
                      {"type": "text"|"thinking", "delta"} {"type": "tool", "name", "detail"}
                      {"type": "terminal", "command", "output", "exit_code"} {"type": "ping"}
                      then {"type": "done", "text"} or {"type": "error", "message"}
  POST /clis/chat     an OpenAI chat-completions request (model "cli:<id>/<model>") answered by a
                      CLI, streamed as chat-completion SSE, so llama-based planners swap URL only
  POST /clis/stop     {"run": id} ends a run (its whole process group)

Nothing from a request reaches a shell: the CLI id picks an entry of CLIS, the model must look like
a model name and the working directory must be inside the Hermes home (where every sub-agent's
workspace lives). Runs use the user's own CLI login (~/.claude, ~/.codex ...).

access levels: "chat" (planners: answer only, may read the images it was given), "agent" (Master
Agent / Vibe Coding / REA: edits in its workspace, shell commands and the web, under the CLI's own
safety mode: Claude Code's auto mode, Codex's workspace-write sandbox, Kimi's Ask-when-needed)."""
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from paths import HERMES_HOME, ROOT

log = logging.getLogger("hostctl.clis")
HOME = Path.home()
SEARCH_DIRS = [HOME / ".local/bin", HOME / ".npm-global/bin", HOME / ".bun/bin", HOME / ".cargo/bin",
               HOME / ".opencode/bin", HOME / "go/bin", HOME / ".volta/bin", Path("/usr/local/bin"), Path("/usr/bin")]
MODEL_NAME = re.compile(r"[A-Za-z0-9._:/@\[\]+-]{1,160}")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MAX_RUN_SECONDS = 4 * 3600
ARG_LIMIT = 96_000          # one argv string must stay under Linux's 128 KiB MAX_ARG_STRLEN
# Hermes container paths in briefs and session folders -> this host (see the hermes-agent mounts).
PATH_MAP = [("/opt/data/rocsy-pinned", str(ROOT / "H3Machine" / "uploads")), ("/opt/data", str(HERMES_HOME))]


# ----------------------------------------------------------------- detection --

def _which(*names: str) -> str:
    path = os.pathsep.join([os.environ.get("PATH", ""), *map(str, SEARCH_DIRS)])
    for name in names:
        found = shutil.which(name, path=path)
        if found:
            return found
    return ""


def _quiet(*args: str, timeout: float = 8) -> str:
    try:
        done = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, env=child_env(),
                              stdin=subprocess.DEVNULL, cwd=tempfile.gettempdir())
        return (done.stdout or "") + (done.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def _json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _models_claude(binary: str) -> tuple[list[dict[str, str]], str]:
    # The --model help names the current aliases ("e.g. 'fable', 'opus', or 'sonnet'").
    help_text = _quiet(binary, "--help")
    block = help_text.split("--model", 1)[1][:400] if "--model" in help_text else ""
    aliases = list(dict.fromkeys(re.findall(r"'([a-z][a-z0-9-]{2,30})'", block))) or ["opus", "sonnet"]
    if "haiku" not in aliases:
        aliases.append("haiku")
    configured = str(_json(HOME / ".claude" / "settings.json").get("model") or "")
    models = [{"id": a, "label": f"{a.capitalize()} (latest)"} for a in aliases]
    if configured and configured not in aliases:
        models.insert(0, {"id": configured, "label": configured})
    return models, configured


def _models_codex(binary: str) -> tuple[list[dict[str, str]], str]:
    home = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
    configured = str(_toml(home / "config.toml").get("model") or "")
    rows = _json(home / "models_cache.json").get("models") or []
    rows = sorted((r for r in rows if isinstance(r, dict) and r.get("slug") and r.get("visibility", "list") == "list"),
                  key=lambda r: r.get("priority", 99))
    models = [{"id": r["slug"], "label": r.get("display_name") or r["slug"]} for r in rows]
    if configured and not any(m["id"] == configured for m in models):
        models.insert(0, {"id": configured, "label": configured})
    return models, configured


def _models_kimi(binary: str) -> tuple[list[dict[str, str]], str]:
    config = next((c for c in (_toml(HOME / ".kimi-code" / "config.toml"), _toml(HOME / ".kimi" / "config.toml")) if c), {})
    rows = config.get("models") if isinstance(config.get("models"), dict) else {}
    models = [{"id": k, "label": (v.get("display_name") if isinstance(v, dict) else "") or k} for k, v in rows.items()]
    return models, str(config.get("default_model") or "")


def _models_settings(folder: str) -> Callable[[str], tuple[list[dict[str, str]], str]]:
    """Gemini CLI and its forks keep the chosen model in ~/.<cli>/settings.json."""
    def read(binary: str) -> tuple[list[dict[str, str]], str]:
        data = _json(HOME / folder / "settings.json")
        model = data.get("model")
        name = str((model.get("name") if isinstance(model, dict) else model) or "")
        return ([{"id": name, "label": name}] if name else []), name
    return read


def _models_listed(*args: str) -> Callable[[str], tuple[list[dict[str, str]], str]]:
    """CLIs that print their models (`opencode models`, `cursor-agent models`): one id per line."""
    def read(binary: str) -> tuple[list[dict[str, str]], str]:
        ids = []
        for line in _quiet(binary, *args, timeout=15).splitlines():
            word = re.sub(r"\x1b\[[0-9;]*m", "", line).strip().split(" ")[0].strip("*- ")
            if MODEL_NAME.fullmatch(word) and ("/" in word or "-" in word) and word not in ids:
                ids.append(word)
        return [{"id": i, "label": i} for i in ids[:80]], ""
    return read


def _no_models(binary: str) -> tuple[list[dict[str, str]], str]:
    return [], ""


# id -> how to find it, read its models and run one headless turn. `efforts`: thinking levels it takes.
CLIS: dict[str, dict[str, Any]] = {
    "claude": {"name": "Claude Code", "bins": ("claude",), "models": _models_claude, "efforts": EFFORTS, "images": "read"},
    "codex": {"name": "Codex", "bins": ("codex",), "models": _models_codex, "efforts": EFFORTS, "images": "flag"},
    "kimi": {"name": "Kimi Code", "bins": ("kimi",), "models": _models_kimi, "efforts": (), "images": "read"},
    "gemini": {"name": "Gemini CLI", "bins": ("gemini",), "models": _models_settings(".gemini"), "efforts": (), "images": "read"},
    "qwen": {"name": "Qwen Code", "bins": ("qwen",), "models": _models_settings(".qwen"), "efforts": (), "images": "read"},
    "opencode": {"name": "OpenCode", "bins": ("opencode",), "models": _models_listed("models"), "efforts": (), "images": "read"},
    "cursor": {"name": "Cursor Agent", "bins": ("cursor-agent",), "models": _models_listed("models"), "efforts": (), "images": "read"},
    "copilot": {"name": "GitHub Copilot CLI", "bins": ("copilot",), "models": _no_models, "efforts": (), "images": "read"},
    "droid": {"name": "Factory Droid", "bins": ("droid",), "models": _no_models, "efforts": (), "images": "read"},
}

_cache: dict[str, Any] = {"t": 0.0, "data": None}
_cache_lock = threading.Lock()


def detect(fresh: bool = False) -> dict[str, Any]:
    with _cache_lock:
        if not fresh and _cache["data"] is not None and time.time() - _cache["t"] < 60:
            return _cache["data"]
        found = []
        for cid, spec in CLIS.items():
            binary = _which(*spec["bins"])
            if not binary:
                continue
            version = next((l.strip() for l in _quiet(binary, "--version").splitlines() if l.strip()), "")[:80]
            try:
                models, default = spec["models"](binary)
            except Exception:  # noqa: BLE001 - a CLI's odd config must not hide the others
                log.exception("Could not read %s models", cid)
                models, default = [], ""
            found.append({"id": cid, "name": spec["name"], "path": binary, "version": version,
                          "models": models, "default": default, "efforts": list(spec["efforts"])})
        _cache.update(t=time.time(), data={"clis": found})
        return _cache["data"]


# ---------------------------------------------------------------------- runs --

def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_", "HOSTCTL_")) and k not in ("ANTHROPIC_API_KEY_HELPER",)}
    env["PATH"] = os.pathsep.join([*map(str, SEARCH_DIRS), env.get("PATH", "")])
    env.setdefault("HOME", str(HOME))
    env["NO_COLOR"] = "1"
    return env


def host_paths(text: str) -> str:
    for inside, host in PATH_MAP:
        text = re.sub(re.escape(inside) + r"(?=/|\b|$)", host.replace("\\", "\\\\"), text)
    return text


def safe_cwd(raw: str) -> Path:
    path = Path(host_paths(raw or "/opt/data/rocsy-workspace")).resolve()
    root = HERMES_HOME.resolve()
    if path != root and root not in path.parents:
        raise ValueError("The working folder must be inside the Hermes home")
    path.mkdir(parents=True, exist_ok=True)
    return path


RUNS: dict[str, subprocess.Popen] = {}


def stop(run_id: str) -> bool:
    proc = RUNS.get(run_id)
    if not proc or proc.poll() is not None:
        return False
    with_group(proc, signal.SIGTERM)
    threading.Timer(8, with_group, (proc, signal.SIGKILL)).start()
    return True


def with_group(proc: subprocess.Popen, sig: int) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def command(cid: str, model: str, effort: str, access: str, cwd: Path, prompt: str, images: list[Path],
            rocsy_script: str) -> tuple[list[str], str | None]:
    """argv and stdin for one headless turn (stdin None = the prompt is in argv)."""
    binary = _which(*CLIS[cid]["bins"])
    if not binary:
        raise ValueError(f"{CLIS[cid]['name']} isn't installed on this host")
    model = "" if model in ("", "default") else model
    effort = effort if effort in CLIS[cid]["efforts"] else {"none": "low", "minimal": "low"}.get(effort, "")
    if cid == "claude":
        argv = [binary, "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
        if access == "chat":
            argv += ["--tools", "Read", "--permission-mode", "dontAsk", "--allowedTools", "Read"]
        else:
            argv += ["--permission-mode", "auto"]
            if rocsy_script:
                argv += ["--allowedTools", f"Bash(python3 {rocsy_script}:*)"]
        argv += ["--model", model] if model else []
        argv += ["--effort", effort] if effort else []
        return argv, prompt
    if cid == "codex":
        argv = [binary, "exec", "--json", "--skip-git-repo-check", "-C", str(cwd)]
        argv += ["-s", "read-only"] if access == "chat" else ["-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]
        argv += ["-m", model] if model else []
        argv += ["-c", f"model_reasoning_effort={effort}"] if effort else []
        for image in images:
            argv += ["-i", str(image)]
        return argv + ["-"], prompt
    if cid == "kimi":
        argv = [binary, "--output-format", "stream-json"] + (["-m", model] if model else [])
        argv += [] if access == "chat" else ["-y"]
        return argv + ["-p", prompt], None
    if cid in ("gemini", "qwen"):
        argv = [binary, "-p", "Follow the instructions given on standard input."] + (["-m", model] if model else [])
        return argv + ([] if access == "chat" else ["--yolo"]), prompt
    if cid == "opencode":
        return [binary, "run"] + (["-m", model] if model else []) + [prompt], None
    if cid == "cursor":
        argv = [binary, "-p", "--output-format", "text"] + (["--model", model] if model else [])
        return argv + ([] if access == "chat" else ["--force"]) + [prompt], None
    if cid == "copilot":
        return [binary, "-p", prompt] + (["--model", model] if model else []) + ([] if access == "chat" else ["--allow-all-tools"]), None
    if cid == "droid":
        return [binary, "exec"] + (["-m", model] if model else []) + ([] if access == "chat" else ["--auto", "medium"]) + [prompt], None
    raise ValueError("Unknown CLI")


def _short(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text[:limit]


def parse_claude(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
    kind = event.get("type")
    if kind == "assistant":
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                yield {"type": "text", "delta": block["text"]}
            elif block.get("type") == "thinking" and block.get("thinking"):
                yield {"type": "thinking", "delta": block["thinking"]}
            elif block.get("type") == "tool_use":
                args = block.get("input") or {}
                if block.get("name") == "Bash":
                    yield {"type": "terminal_start", "id": block.get("id"), "command": str(args.get("command") or "")}
                else:
                    yield {"type": "tool", "name": block.get("name") or "tool", "detail": _short(args)}
    elif kind == "user":
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                yield {"type": "tool_result", "id": block.get("tool_use_id"), "output": str(content or "")[:20000],
                       "error": bool(block.get("is_error"))}
    elif kind == "result":
        if event.get("is_error") or event.get("subtype") not in (None, "success"):
            yield {"type": "error", "message": str(event.get("result") or event.get("subtype") or "Claude Code failed")[:2000]}
        else:
            yield {"type": "done", "text": str(event.get("result") or "")}


def parse_codex(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
    kind = event.get("type", "")
    item = event.get("item") or {}
    itype = item.get("type") or item.get("item_type") or ""
    if kind == "item.started" and itype == "command_execution":
        yield {"type": "terminal_start", "id": item.get("id"), "command": str(item.get("command") or "")}
    elif kind == "item.completed":
        if itype in ("agent_message", "assistant_message"):
            yield {"type": "text", "delta": str(item.get("text") or ""), "final": True}
        elif itype == "reasoning" and item.get("text"):
            yield {"type": "thinking", "delta": str(item["text"])}
        elif itype == "command_execution":
            yield {"type": "tool_result", "id": item.get("id"), "output": str(item.get("aggregated_output") or "")[:20000],
                   "exit_code": item.get("exit_code"), "error": item.get("status") == "failed"}
        elif itype == "file_change":
            yield {"type": "tool", "name": "edit", "detail": ", ".join(str(c.get("path")) for c in item.get("changes") or [])[:400]}
        elif itype in ("mcp_tool_call", "web_search", "todo_list"):
            yield {"type": "tool", "name": itype.replace("_", " "), "detail": _short(item.get("query") or item.get("tool") or "")}
        elif itype == "error":
            yield {"type": "warn", "message": str(item.get("message") or "")[:400]}
    elif kind == "turn.failed":
        yield {"type": "error", "message": str((event.get("error") or {}).get("message") or "Codex failed")[:2000]}
    elif kind == "error":
        yield {"type": "error", "message": str(event.get("message") or "Codex failed")[:2000]}


def parse_generic(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """stream-json from CLIs without their own parser (Kimi Code ...): pick out the usual fields."""
    role = event.get("role") or event.get("type") or ""
    content = event.get("content", event.get("text", event.get("message")))
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("text", "output_text") and block.get("text"):
                yield {"type": "text", "delta": block["text"]}
            elif block.get("type") in ("think", "thinking", "reasoning"):
                yield {"type": "thinking", "delta": str(block.get("think") or block.get("thinking") or block.get("text") or "")}
        for call in event.get("tool_calls") or []:
            fn = (call or {}).get("function") or {}
            yield {"type": "tool", "name": fn.get("name") or "tool", "detail": _short(fn.get("arguments") or "")}
    elif isinstance(content, str) and content and role in ("assistant", "text", "message", "result"):
        yield {"type": "text", "delta": content}
    elif role == "tool" and isinstance(content, str):
        yield {"type": "tool_result", "id": event.get("tool_call_id"), "output": content[:20000]}
    elif role == "error":
        yield {"type": "error", "message": _short(content or event, 2000)}


PARSERS = {"claude": parse_claude, "codex": parse_codex, "kimi": parse_generic}


def run(body: dict[str, Any]) -> tuple[str, Iterator[dict[str, Any]]]:
    """Start one headless turn; returns (run id, events). Raises ValueError for a bad request."""
    cid = str(body.get("cli") or "")
    if cid not in CLIS:
        raise ValueError("Unknown CLI")
    model = str(body.get("model") or "")
    if model and not MODEL_NAME.fullmatch(model):
        raise ValueError("That isn't a model name")
    access = "chat" if body.get("access") == "chat" else "agent"
    prompt = host_paths(str(body.get("prompt") or "")) if body.get("hermes_paths") else str(body.get("prompt") or "")
    if not prompt.strip():
        raise ValueError("Empty prompt")
    scratch = Path(tempfile.mkdtemp(prefix="rocsy-cli-"))
    cwd = scratch if access == "chat" else safe_cwd(str(body.get("cwd") or ""))
    images = []
    for i, url in enumerate(body.get("images") or []):
        match = re.fullmatch(r"data:image/(png|jpe?g|webp|gif);base64,(.+)", str(url), re.S)
        if match and len(images) < 16:
            path = scratch / f"image-{i + 1}.{match.group(1).replace('jpeg', 'jpg')}"
            path.write_bytes(base64.b64decode(match.group(2)))
            images.append(path)
    if images and CLIS[cid]["images"] == "read":
        prompt += "\n\nATTACHED IMAGES (open each one and look at it before answering):\n" + "\n".join(map(str, images))
    script = str(HERMES_HOME / "skills/rocsy/master-agent/scripts/rocsy.py") if access == "agent" else ""
    argv, stdin = command(cid, model, str(body.get("effort") or ""), access, cwd, prompt, images, script)
    if stdin is None and len(prompt) > ARG_LIMIT:
        # Too long for one argument: hand the CLI a file to read instead.
        task = scratch / "TASK.md"
        task.write_text(prompt)
        argv[argv.index(prompt)] = f"Your full instructions are in {task}. Read that whole file first, then do exactly what it says."
    run_id = uuid.uuid4().hex
    proc = subprocess.Popen(argv, cwd=cwd, env=child_env(), stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, bufsize=1)
    RUNS[run_id] = proc
    log.info("CLI run %s: %s (%s, %s) in %s", run_id[:8], cid, model or "default", access, cwd)
    return run_id, _events(run_id, cid, proc, stdin, scratch)


def _events(run_id: str, cid: str, proc: subprocess.Popen, stdin: str | None, scratch: Path) -> Iterator[dict[str, Any]]:
    lines: queue.Queue[str | None] = queue.Queue()
    errors: list[str] = []

    def feed() -> None:
        if stdin is not None and proc.stdin:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    def pump() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.put(line)
        lines.put(None)

    def pump_err() -> None:
        for line in proc.stderr:  # type: ignore[union-attr]
            errors.append(line)
            del errors[:-60]

    for target in (feed, pump, pump_err):
        threading.Thread(target=target, daemon=True).start()
    parser = PARSERS.get(cid)
    started = time.monotonic()
    plain: list[str] = []
    last_text = ""
    finished = False
    try:
        while True:
            try:
                line = lines.get(timeout=15)
            except queue.Empty:
                if time.monotonic() - started > MAX_RUN_SECONDS:
                    stop(run_id)
                    yield {"type": "error", "message": f"Stopped after {MAX_RUN_SECONDS // 3600} h"}
                    finished = True
                    return
                yield {"type": "ping"}
                continue
            if line is None:
                break
            event = None
            if parser and line.lstrip().startswith("{"):
                try:
                    event = json.loads(line)
                except ValueError:
                    event = None
            if event is None or not isinstance(event, dict):
                if not parser:
                    plain.append(line)
                    yield {"type": "text", "delta": line}
                continue
            for out in parser(event):
                if out["type"] == "text":
                    last_text = out["delta"] if out.pop("final", False) else last_text + out["delta"]
                if out["type"] == "done":
                    finished = True
                    out["text"] = out["text"] or last_text
                if out["type"] == "error":
                    finished = True
                yield out
        code = proc.wait(timeout=30)
        if not finished:
            text = last_text or "".join(plain).strip()
            if code == 0 or text:
                yield {"type": "done", "text": text}
            else:
                tail = "".join(errors).strip()[-1500:] or f"exit code {code}"
                yield {"type": "error", "message": f"{CLIS[cid]['name']} failed: {tail}"}
    finally:
        with_group(proc, signal.SIGTERM)
        RUNS.pop(run_id, None)
        shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------- OpenAI chat-completions --

def chat_request(payload: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI chat request for model "cli:<id>/<model>" -> a /clis/run body (chat access)."""
    model = str(payload.get("model") or "")
    if not model.startswith("cli:"):
        raise ValueError("Not a CLI model")
    cid, _, name = model[4:].partition("/")
    parts, images = [], []
    for message in payload.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            text = []
            for block in content:
                if block.get("type") == "text":
                    text.append(str(block.get("text") or ""))
                elif block.get("type") == "image_url":
                    images.append(str((block.get("image_url") or {}).get("url") or ""))
                    text.append(f"[image {len(images)}]")
            content = "\n".join(text)
        role = str(message.get("role") or "user").upper()
        parts.append(f"=== {'INSTRUCTIONS' if role == 'SYSTEM' else role} ===\n{content}")
    prompt = ("You are answering one request from an app. Reply with the answer only: no tools other than "
              "opening the attached images, no file changes, no questions back.\n\n" + "\n\n".join(parts))
    return {"cli": cid, "model": name, "effort": str(payload.get("reasoning_effort") or ""), "access": "chat",
            "prompt": prompt, "images": images}


def chat_stream(payload: dict[str, Any]) -> Iterator[str]:
    """chat-completion SSE lines for a CLI model: thinking streams live (reasoning_content), the
    answer comes as one chunk at the end, since a CLI's in-between remarks aren't part of it."""
    run_id, events = run(chat_request(payload))
    chunk = lambda delta, finish=None: "data: " + json.dumps(
        {"id": run_id, "object": "chat.completion.chunk", "model": payload.get("model"),
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
    for e in events:
        if e["type"] == "thinking":
            yield chunk({"reasoning_content": e["delta"]})
        elif e["type"] == "ping":
            yield ": ping\n\n"
        elif e["type"] == "done":
            yield chunk({"content": e["text"]})
            yield chunk({}, "stop")
        elif e["type"] == "error":
            yield "data: " + json.dumps({"error": e["message"]}) + "\n\n"
    yield "data: [DONE]\n\n"
