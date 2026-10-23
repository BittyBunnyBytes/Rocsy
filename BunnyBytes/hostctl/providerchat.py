"""Hermes providers (Kimi Coding, a second llama.cpp server ...) for the llama-based planners.

Settings -> Sub-agents offers every studio planner the Master Agent's model list. Models served by
llama-swap stay plain model ids; any other Hermes provider is named "hermes:<provider>/<model>" and
answered here, so planners swap URL only, as they do for coding CLIs:

  GET  /providers/info?provider=<slug>   {"local": bool}: whether the endpoint is on this machine
                                          (a local one still needs the planner's GPU turn)
  POST /providers/chat                   an OpenAI chat-completions request, streamed back as
                                          chat-completion SSE whatever the provider speaks

Endpoint, key and API style come from Hermes' own resolver inside its container, so keys stay in
~/.hermes and never reach the studios. Anthropic-style endpoints (Kimi Coding) are translated."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

HERMES_CONTAINER = os.getenv("HOSTCTL_HERMES_CONTAINER", "hermes-agent")
HERMES_PYTHON = os.getenv("HOSTCTL_HERMES_PYTHON", "/opt/hermes/.venv/bin/python")
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,80}")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,160}")
RESOLVE = """
import json, sys
from hermes_cli.runtime_provider import resolve_runtime_provider
r = resolve_runtime_provider(requested=sys.argv[1])
print(json.dumps({k: r.get(k) or "" for k in ("base_url", "api_key", "api_mode")}))
"""
# llama.cpp-only request fields a remote OpenAI-style endpoint would reject.
LLAMA_ONLY = ("top_k", "min_p", "repeat_penalty", "n_predict", "chat_template_kwargs", "max_tokens")
_cache: dict[str, tuple[float, dict[str, str]]] = {}
_lock = threading.Lock()


def split(model: str) -> tuple[str, str]:
    """"hermes:<provider>/<model>" -> (provider, model); the provider itself may hold a colon."""
    if not model.startswith("hermes:"):
        raise ValueError("Not a Hermes provider model")
    slug, _, name = model[7:].partition("/")
    if not SLUG.fullmatch(slug) or not MODEL.fullmatch(name):
        raise ValueError("Bad provider or model name")
    return slug, name


def resolve(slug: str) -> dict[str, str]:
    if not SLUG.fullmatch(slug):
        raise ValueError("Bad provider name")
    with _lock:
        hit = _cache.get(slug)
        if hit and time.monotonic() - hit[0] < 300:
            return hit[1]
    result = subprocess.run(["docker", "exec", "-w", "/opt/hermes", HERMES_CONTAINER, HERMES_PYTHON, "-c", RESOLVE, slug],
                            capture_output=True, text=True, timeout=60)
    lines = [x for x in result.stdout.splitlines() if x.startswith("{")]
    if result.returncode or not lines:
        last = ((result.stderr or result.stdout).strip().splitlines() or ["no output"])[-1]
        raise ValueError(f"Hermes couldn't resolve provider {slug}: {last.split(': ', 1)[-1][:300]}")
    found = json.loads(lines[-1])
    if not found.get("base_url"):
        raise ValueError(f"Hermes has no endpoint for provider {slug}")
    with _lock:
        _cache[slug] = (time.monotonic(), found)
    return found


def is_local(base_url: str) -> bool:
    return (urllib.parse.urlparse(base_url).hostname or "") in ("127.0.0.1", "localhost", "::1")


def info(slug: str) -> dict[str, Any]:
    return {"local": is_local(resolve(slug)["base_url"])}


def _chunk(model: str, delta: dict[str, Any], finish: str | None = None) -> str:
    return "data: " + json.dumps({"object": "chat.completion.chunk", "model": model,
                                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


def _open(url: str, body: dict[str, Any], headers: dict[str, str]):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    try:
        return urllib.request.urlopen(request, timeout=600)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Provider HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from None
    except urllib.error.URLError as exc:
        raise ValueError(f"Provider unreachable: {exc.reason}") from None


def _openai(found: dict[str, str], name: str, payload: dict[str, Any]) -> Iterator[str]:
    body = {**payload, "model": name, "stream": True}
    if not is_local(found["base_url"]):
        for key in LLAMA_ONLY:
            body.pop(key, None)
    headers = {"Authorization": f"Bearer {found['api_key']}"} if found.get("api_key") else {}
    with _open(found["base_url"].rstrip("/") + "/chat/completions", body, headers) as response:
        for raw in response:
            line = raw.decode(errors="replace").rstrip("\r\n")
            if line:
                yield line + "\n\n"


def _anthropic_content(content: Any) -> Any:
    if not isinstance(content, list):
        return str(content or "")
    blocks = []
    for block in content:
        if block.get("type") == "text":
            blocks.append({"type": "text", "text": str(block.get("text") or "")})
        elif block.get("type") == "image_url":
            url = str((block.get("image_url") or {}).get("url") or "")
            match = re.match(r"data:(image/[a-z+]+);base64,(.*)", url, re.S)
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": match[1], "data": match[2]}} if match
                          else {"type": "image", "source": {"type": "url", "url": url}})
    return blocks


def _anthropic(found: dict[str, str], name: str, payload: dict[str, Any]) -> Iterator[str]:
    system = [str(m.get("content") or "") for m in payload.get("messages") or [] if m.get("role") == "system"]
    messages = [{"role": "assistant" if m.get("role") == "assistant" else "user", "content": _anthropic_content(m.get("content"))}
                for m in payload.get("messages") or [] if m.get("role") != "system"]
    body: dict[str, Any] = {"model": name, "max_tokens": 32000, "messages": messages, "stream": True}
    if system:
        body["system"] = "\n\n".join(system)
    level = str(payload.get("reasoning_effort") or "")
    budget = {"minimal": 1024, "low": 4000, "medium": 10000, "high": 20000, "xhigh": 28000, "max": 31000}.get(level)
    if budget:
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
    else:
        if level == "none":
            body["thinking"] = {"type": "disabled"}
        if "temperature" in payload:
            body["temperature"] = min(float(payload["temperature"]), 1.0)
    headers = {"x-api-key": found.get("api_key", ""), "anthropic-version": "2023-06-01"}
    if found["base_url"].lower().startswith("https://api.kimi.com/coding"):
        headers.update({"User-Agent": "HermesAgent/rocsy", "X-Title": "Hermes Agent"})   # Kimi Coding 403s without one
    with _open(found["base_url"].rstrip("/") + "/v1/messages", body, headers) as response:
        for raw in response:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    yield _chunk(name, {"content": delta.get("text", "")})
                elif delta.get("type") == "thinking_delta":
                    yield _chunk(name, {"reasoning_content": delta.get("thinking", "")})
            elif kind == "message_delta" and (event.get("delta") or {}).get("stop_reason"):
                stop = event["delta"]["stop_reason"]
                yield _chunk(name, {}, "length" if stop == "max_tokens" else "stop")
            elif kind == "error":
                yield "data: " + json.dumps({"error": str((event.get("error") or {}).get("message") or event)[:300]}) + "\n\n"
    yield "data: [DONE]\n\n"


def chat_stream(payload: dict[str, Any]) -> Iterator[str]:
    """chat-completion SSE lines for a "hermes:<provider>/<model>" request. The first next() resolves
    the provider and opens the request, so a bad one still gets a plain error."""
    slug, name = split(str(payload.get("model") or ""))
    found = resolve(slug)
    stream = (_anthropic if found.get("api_mode") == "anthropic_messages" else _openai)(found, name, payload)
    first = next(stream, "data: [DONE]\n\n")   # opens the request: an HTTP error raises here
    yield first
    try:
        yield from stream
    except (ValueError, OSError) as exc:
        yield "data: " + json.dumps({"error": str(exc)[:300]}) + "\n\n"
        yield "data: [DONE]\n\n"
