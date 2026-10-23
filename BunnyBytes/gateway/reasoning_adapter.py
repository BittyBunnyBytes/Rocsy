"""Reasoning-effort adapter for /Llama chat requests.

Clients (Hermes, OpenAI SDKs) send whatever reasoning effort they were
configured with -- "minimal", "max", "none" -- but each llama.cpp model's chat
template only accepts its own short list and raises a Jinja exception (HTTP
500) for anything else, e.g. Qwen3.8 Flash Next:

    Unexpected reasoning effort minimal. Supported types are xhigh (default),
    medium, and low.

No config needed per model: the supported list is learned from that error the
first time a model rejects an effort, cached per model, and the request is
retried once with the closest supported effort. Later requests to that model
are adapted before they're sent, so the failure costs one fast (~10ms) retry
per model, not per request.

Mapping, for an effort the model doesn't support:
  1. parity aliases first: minimal <-> low, high <-> xhigh
  2. above everything supported (e.g. max, ultra) -> the highest supported
  3. below everything supported (e.g. none)       -> the lowest supported
  4. in between -> nearest on the ladder (a tie leans toward the side the
     request is on: medium and up rounds up, below medium rounds down)
"""
from __future__ import annotations

import json
import logging
import re
import time

log = logging.getLogger("gateway.reasoning")

LADDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_RANK = {effort: index for index, effort in enumerate(LADDER)}
PARITY = {"minimal": "low", "low": "minimal", "high": "xhigh", "xhigh": "high"}

# Upstream (llama-swap, prefix already stripped) paths whose JSON body carries
# a reasoning effort.
ADAPT_PATHS = frozenset({"/v1/chat/completions", "/v1/completions", "/v1/responses",
                         "/chat/completions", "/completions", "/responses"})

# A model's template can change when llama-swap's config is edited, so a
# learned list is only trusted for a while; an expired one is re-learned on
# the next rejection.
CACHE_TTL_SECONDS = 6 * 3600

_supported: dict[str, tuple[tuple[str, ...], float]] = {}

_REJECTION = re.compile(r"unexpected reasoning effort", re.IGNORECASE)
_SUPPORTED_LIST = re.compile(r"supported\s+\w+\s+(?:are|is)\s*:?\s*([^.\n\"\\]+)", re.IGNORECASE)


def applies(path: str) -> bool:
    return path.rstrip("/") in ADAPT_PATHS


def choose(requested: str, supported: tuple[str, ...]) -> str | None:
    """The supported effort to send instead of `requested`, or None to leave
    it untouched (already supported, or nothing on the ladder to map onto)."""
    effort = requested.strip().lower()
    if not supported or effort in supported:
        return None
    alias = PARITY.get(effort)
    if alias in supported:
        return alias
    if effort not in _RANK:
        return None
    ranked = sorted((s for s in supported if s in _RANK), key=_RANK.__getitem__)
    if not ranked:
        return None
    rank = _RANK[effort]
    if rank > _RANK[ranked[-1]]:
        return ranked[-1]
    if rank < _RANK[ranked[0]]:
        return ranked[0]
    lean_up = rank >= _RANK["medium"]
    return min(ranked, key=lambda s: (abs(_RANK[s] - rank), -_RANK[s] if lean_up else _RANK[s]))


def parse_supported(error_body: bytes) -> tuple[str, ...] | None:
    """Supported efforts named in an upstream rejection, or None if this
    error isn't a reasoning-effort rejection we can read."""
    text = error_body.decode("utf-8", "replace")
    try:
        payload = json.loads(text)
        error = payload.get("error", payload) if isinstance(payload, dict) else payload
        message = error.get("message", text) if isinstance(error, dict) else text
    except (ValueError, AttributeError):
        message = text
    if not _REJECTION.search(message):
        return None
    match = _SUPPORTED_LIST.search(message)
    if not match:
        return None
    items = re.split(r",|\bor\b|\band\b|/", re.sub(r"\([^)]*\)", "", match.group(1)))
    efforts = tuple(dict.fromkeys(item.strip().strip("'\"`").lower() for item in items if item.strip()))
    return efforts or None


def _known(model: str) -> tuple[str, ...]:
    entry = _supported.get(model)
    if not entry:
        return ()
    efforts, learned_at = entry
    if time.monotonic() - learned_at > CACHE_TTL_SECONDS:
        _supported.pop(model, None)
        return ()
    return efforts


def _effort_slots(body: dict) -> list[tuple[dict, str]]:
    """(container, key) pairs holding a reasoning effort string in an
    OpenAI-style body: top-level reasoning_effort, reasoning.effort
    (Responses / OpenRouter style), chat_template_kwargs.reasoning_effort."""
    slots = []
    if isinstance(body.get("reasoning_effort"), str):
        slots.append((body, "reasoning_effort"))
    for parent, key in (("reasoning", "effort"), ("chat_template_kwargs", "reasoning_effort")):
        container = body.get(parent)
        if isinstance(container, dict) and isinstance(container.get(key), str):
            slots.append((container, key))
    return slots


def adapt_body(raw: bytes) -> bytes:
    """`raw` with every reasoning effort the target model is known not to
    support swapped for its closest supported one; `raw` itself when nothing
    changes (unknown model, not JSON, no effort, already supported)."""
    try:
        body = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(body, dict):
        return raw
    model = body.get("model")
    supported = _known(model) if isinstance(model, str) else ()
    if not supported:
        return raw
    changed = False
    for container, key in _effort_slots(body):
        replacement = choose(container[key], supported)
        if replacement:
            log.info("reasoning effort %s -> %s for model %s (supports %s)",
                     container[key], replacement, model, ", ".join(supported))
            container[key] = replacement
            changed = True
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if changed else raw


def retry_body(raw: bytes, sent: bytes, error_body: bytes) -> bytes | None:
    """After an upstream rejection: learn the model's supported efforts from
    the error and return the adapted body to retry with, or None when the
    error isn't an effort rejection or adapting wouldn't change what was sent."""
    supported = parse_supported(error_body)
    if not supported:
        return None
    try:
        model = json.loads(raw).get("model")
    except (ValueError, AttributeError):
        return None
    if not isinstance(model, str):
        return None
    _supported[model] = (supported, time.monotonic())
    log.info("learned reasoning efforts for model %s: %s", model, ", ".join(supported))
    adapted = adapt_body(raw)
    return adapted if adapted != sent else None
