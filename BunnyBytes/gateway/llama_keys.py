"""Scoped llama-swap API keys (Llama API page -> Keys).

llama-swap itself only knows one all-powerful key list (apiKeys: in Llama/config.yaml),
so extra keys live here in the gateway, which already authenticates /Llama/* and /v1/*
before forwarding with the real LLAMA_API_KEY. Each key has its own limits:

  models          which llama-swap model ids it may use (empty = all)
  max_context     prompt + reply budget in tokens (prompt size is estimated from its text)
  max_output      the most tokens one reply may have (max_tokens is clamped to it)
  per_minute      requests per minute (0 = no limit)
  per_day         requests per UTC day (0 = no limit)
  allow_images    vision: whether pictures (and video) may be sent
  max_images      most pictures per request (each counts IMAGE_TOKENS toward max_context)
  allow_audio     whether audio parts may be sent
  require_loaded  only answer when the model is already loaded, so this key never makes
                  llama-swap swap out whatever else is running
  character       a SillyTavern character card (its name): read LIVE from SillyTavern's
                  characters folder on every request and sent as the system prompt; the
                  caller's own system messages are dropped, so an app can't change who it is
  thinking        off = every request asks the model to answer without thinking
  system_extra    your own text added to the system prompt (after the card, if any), e.g.
                  formatting rules for the app; the caller can't change or remove it

A scoped key may only list models and call chat/completions; every other llama-swap
route (UI, /upstream, unload, ...) is refused. Keys are stored as SHA-256 hashes in
data/llama-keys.json; the plain key is shown once, when it is created or given a new
secret. Settings can be edited any time and apply to the next request. Revoking a key
keeps it in the list (greyed out) so it's clear what was cut off; deleting removes it.
"""
from __future__ import annotations

import hashlib
import hmac
import base64
import json
import math
import re
import struct
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / "data" / "llama-keys.json"
TOKEN_PREFIX = "rk-"
CHAT_PATHS = ("/v1/chat/completions", "/v1/completions")
MODEL_PATHS = ("/v1/models",)
CHARS_PER_TOKEN = 3.2          # conservative: overestimates tokens a little, never under
PER_MESSAGE_TOKENS = 8         # role markers / template overhead per message
IMAGE_TOKENS = 1024            # context one picture is counted as (Qwen vision: up to ~1k per image)
SAVE_EVERY = 30.0              # seconds between writes of usage counters
# SillyTavern's characters folder (mounted read-only): <name>.png with the card in tEXt "ccv3"/"chara".
CHARACTER_DIR = Path(os.getenv("ST_CHARACTER_DIR", "/st-characters"))
_CARD_NAME = re.compile(r"^[A-Za-z0-9 _.()'-]{1,80}$")
_card_cache: dict[str, tuple[int, dict]] = {}

_lock = threading.Lock()


@dataclass
class LlamaKey:
    id: str
    name: str
    hash: str
    hint: str                                  # "rk-ab12…" so the owner can tell keys apart
    models: list[str] = field(default_factory=list)
    max_context: int = 4096
    max_output: int = 512
    per_minute: int = 0
    per_day: int = 0
    allow_images: bool = False                 # vision
    max_images: int = 4
    allow_audio: bool = False
    require_loaded: bool = False
    character: str = ""                        # SillyTavern card name ("" = none)
    thinking: bool = True                      # False: chat_template_kwargs.enable_thinking = false
    system_extra: str = ""                     # added to the system prompt (after the card)
    created: float = 0.0
    revoked: float = 0.0                       # 0 = active
    last_used: float = 0.0
    uses_total: int = 0
    uses_day: int = 0
    uses_day_date: str = ""

    def public(self) -> dict:
        data = asdict(self)
        data.pop("hash")
        data["active"] = not self.revoked
        if data["uses_day_date"] != _today():
            data["uses_day"] = 0
        return data


_keys: dict[str, LlamaKey] = {}
_loaded = False
_dirty_since = 0.0
_minute: dict[str, deque[float]] = {}


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(DATA_FILE.read_text())
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        return
    known = set(LlamaKey.__dataclass_fields__)
    for item in raw.get("keys", []):
        try:
            key = LlamaKey(**{k: v for k, v in item.items() if k in known})
        except TypeError:
            continue
        _keys[key.id] = key


def _save() -> None:
    global _dirty_since
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"keys": [asdict(k) for k in _keys.values()]}, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, DATA_FILE)
    _dirty_since = 0.0


def _int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


# ---------------------------------------------------------------- management (Llama API page)

def list_keys() -> list[dict]:
    with _lock:
        _load()
        return [k.public() for k in sorted(_keys.values(), key=lambda k: k.created, reverse=True)]


def create(spec: dict) -> tuple[dict, str]:
    """Returns (public record, plain key). The plain key is never stored."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    key = LlamaKey(id=secrets.token_hex(6), name="", hash=_hash(token), hint=token[:7] + "…", created=time.time())
    _apply(key, {"name": "", **spec})
    with _lock:
        _load()
        _keys[key.id] = key
        _save()
    return key.public(), token


def _apply(key: LlamaKey, spec: dict) -> None:
    """Sets the editable fields that `spec` contains (missing ones stay as they are)."""
    if "name" in spec:
        name = str(spec.get("name") or "").strip()[:60]
        if not name:
            raise ValueError("Give the key a name.")
        key.name = name
    if "models" in spec:
        key.models = [str(m).strip() for m in (spec.get("models") or []) if str(m).strip()]
    if "max_context" in spec:
        key.max_context = _int(spec.get("max_context"), key.max_context, 256, 1_048_576)
    if "max_output" in spec or "max_context" in spec:
        key.max_output = _int(spec.get("max_output", key.max_output), key.max_output, 16, key.max_context)
    if "per_minute" in spec:
        key.per_minute = _int(spec.get("per_minute"), key.per_minute, 0, 10_000)
    if "per_day" in spec:
        key.per_day = _int(spec.get("per_day"), key.per_day, 0, 10_000_000)
    if "allow_images" in spec:
        key.allow_images = bool(spec.get("allow_images"))
    if "max_images" in spec:
        key.max_images = _int(spec.get("max_images"), key.max_images, 1, 64)
    if "allow_audio" in spec:
        key.allow_audio = bool(spec.get("allow_audio"))
    if "require_loaded" in spec:
        key.require_loaded = bool(spec.get("require_loaded"))
    if "character" in spec:
        name = str(spec.get("character") or "").strip()
        if name and (not _CARD_NAME.match(name) or name not in characters()):
            raise ValueError(f"There's no SillyTavern character called {name}.")
        key.character = name
    if "thinking" in spec:
        key.thinking = bool(spec.get("thinking"))
    if "system_extra" in spec:
        key.system_extra = str(spec.get("system_extra") or "").replace("\r\n", "\n").strip()[:8000]


def update(key_id: str, spec: dict) -> dict | None:
    """Edits a key's settings; takes effect on its next request. None if it doesn't exist."""
    with _lock:
        _load()
        key = _keys.get(key_id)
        if not key:
            return None
        _apply(key, spec)
        _save()
        return key.public()


def rotate(key_id: str) -> tuple[dict, str] | None:
    """Gives a key a new secret (same settings and history); the old secret stops working."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with _lock:
        _load()
        key = _keys.get(key_id)
        if not key or key.revoked:
            return None
        key.hash, key.hint = _hash(token), token[:7] + "…"
        _save()
        return key.public(), token


def revoke(key_id: str) -> bool:
    with _lock:
        _load()
        key = _keys.get(key_id)
        if not key:
            return False
        if not key.revoked:
            key.revoked = time.time()
            _save()
        return True


def delete(key_id: str) -> bool:
    with _lock:
        _load()
        if _keys.pop(key_id, None) is None:
            return False
        _minute.pop(key_id, None)
        _save()
        return True


# ---------------------------------------------------------------- SillyTavern character cards

def characters() -> list[str]:
    """Names of the cards in SillyTavern's characters folder."""
    try:
        return sorted(p.stem for p in CHARACTER_DIR.glob("*.png") if _CARD_NAME.match(p.stem))
    except OSError:
        return []


def read_card(name: str) -> dict | None:
    """The card's data (ccv3 preferred, else chara v2), re-read whenever the file changes."""
    if not _CARD_NAME.match(name):
        return None
    path = CHARACTER_DIR / f"{name}.png"
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    hit = _card_cache.get(name)
    if hit and hit[0] == stamp:
        return hit[1]
    found: dict[str, dict] = {}
    try:
        raw = path.read_bytes()
        i = 8
        while i + 8 <= len(raw):
            length = struct.unpack(">I", raw[i:i + 4])[0]
            kind, data = raw[i + 4:i + 8], raw[i + 8:i + 8 + length]
            if kind == b"tEXt":
                keyword, _, value = data.partition(b"\0")
                if keyword in (b"ccv3", b"chara"):
                    card = json.loads(base64.b64decode(value))
                    found[keyword.decode()] = card.get("data") if isinstance(card.get("data"), dict) else card
            if kind == b"IEND":
                break
            i += 12 + length
    except (OSError, ValueError, struct.error):
        return None
    card = found.get("ccv3") or found.get("chara")
    if card:
        _card_cache[name] = (stamp, card)
    return card


def card_prompt(card: dict) -> str:
    """The card as a system prompt, the way SillyTavern lays out its fields."""
    name = str(card.get("name") or "the character")

    def text(field_name: str) -> str:
        value = card.get(field_name)
        value = value if isinstance(value, str) else ""
        return value.replace("{{char}}", name).replace("{{user}}", "User").replace("<BOT>", name).replace("<USER>", "User").strip()

    parts = [text("system_prompt") or f"You are {name}. Stay in character as {name}."]
    parts += [part for part in (text("description"),
                                f"{name}'s personality: {text('personality')}" if text("personality") else "",
                                f"Scenario: {text('scenario')}" if text("scenario") else "",
                                f"Example messages:\n{text('mes_example')}" if text("mes_example") else "",
                                text("post_history_instructions")) if part]
    return "\n\n".join(parts)


# ---------------------------------------------------------------- request checks

def lookup(token: str) -> LlamaKey | None:
    """The active key matching this bearer token, if any (timing-safe)."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    digest = _hash(token)
    with _lock:
        _load()
        found = None
        for key in _keys.values():
            if hmac.compare_digest(key.hash, digest) and not key.revoked:
                found = key
        return found


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: int = 0) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


def models_response(key: LlamaKey) -> dict:
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "rocsy"} for m in key.models]}


IMAGE_PARTS = {"image_url", "input_image", "image", "video", "video_url"}
AUDIO_PARTS = {"input_audio", "audio"}


def _text_size(content) -> tuple[int, int, int]:
    """(characters of text, pictures, audio parts) in an OpenAI message content."""
    if content is None:
        return 0, 0, 0
    if isinstance(content, str):
        return len(content), 0, 0
    chars = images = audio = 0
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                chars += len(str(part))
                continue
            kind = part.get("type", "")
            if kind in ("text", "input_text"):
                chars += len(str(part.get("text", "")))
            elif kind in IMAGE_PARTS:
                images += 1
            elif kind in AUDIO_PARTS:
                audio += 1
            else:
                chars += len(json.dumps(part))
        return chars, images, audio
    return len(json.dumps(content)), 0, 0


def check_body(key: LlamaKey, path: str, raw: bytes) -> bytes:
    """Validates a chat/completions body against the key and returns the body to send
    (max_tokens clamped). Raises Refused."""
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise Refused(400, "bad_request", "The request body isn't valid JSON.")
    if not isinstance(body, dict):
        raise Refused(400, "bad_request", "The request body must be a JSON object.")

    if (key.character or key.system_extra) and path.endswith("/chat/completions"):
        parts = []
        if key.character:
            card = read_card(key.character)
            if not card:
                raise Refused(503, "character_unavailable", f"The character card {key.character} can't be read right now.")
            parts.append(card_prompt(card))
        if key.system_extra:
            parts.append(key.system_extra)
        others = [m for m in body.get("messages") or [] if isinstance(m, dict) and m.get("role") not in ("system", "developer")]
        if not key.character:   # additions only: keep the app's own system prompt, then ours
            parts = [str(m.get("content")) for m in body.get("messages") or []
                     if isinstance(m, dict) and m.get("role") in ("system", "developer") and isinstance(m.get("content"), str)] + parts
        body["messages"] = [{"role": "system", "content": "\n\n".join(parts)}] + others
    if not key.thinking:
        kwargs = body.get("chat_template_kwargs") if isinstance(body.get("chat_template_kwargs"), dict) else {}
        body["chat_template_kwargs"] = {**kwargs, "enable_thinking": False}
        body.pop("reasoning_budget", None)

    model = str(body.get("model") or "")
    if key.models and model not in key.models:
        allowed = ", ".join(key.models)
        raise Refused(403, "model_not_allowed", f"This key may only use: {allowed}.")

    chars = images = audio = messages = 0
    if path.endswith("/chat/completions"):
        for message in body.get("messages") or []:
            if not isinstance(message, dict):
                continue
            messages += 1
            size, pictures, sounds = _text_size(message.get("content"))
            chars += size + len(json.dumps(message.get("tool_calls") or ""))
            images += pictures
            audio += sounds
    else:
        prompt = body.get("prompt")
        chars += len(prompt) if isinstance(prompt, str) else len(json.dumps(prompt or ""))
    for extra in ("tools", "functions", "response_format", "grammar", "json_schema"):
        if body.get(extra):
            chars += len(json.dumps(body[extra]))
    if images and not key.allow_images:
        raise Refused(403, "vision_not_allowed", "This key doesn't have vision permission (no pictures).")
    if images > key.max_images:
        raise Refused(403, "too_many_images", f"This key allows {key.max_images} picture(s) per request.")
    if audio and not key.allow_audio:
        raise Refused(403, "audio_not_allowed", "This key doesn't accept audio.")

    prompt_tokens = math.ceil(chars / CHARS_PER_TOKEN) + PER_MESSAGE_TOKENS * messages + IMAGE_TOKENS * images
    room = key.max_context - prompt_tokens
    if room < 16:
        raise Refused(
            400, "context_too_long",
            f"The prompt is about {prompt_tokens} tokens; this key allows {key.max_context} tokens of context in total.",
        )
    asked = [v for v in (body.get("max_tokens"), body.get("max_completion_tokens"), body.get("n_predict"))
             if isinstance(v, int) and v > 0]
    out = min([key.max_output, room] + asked)
    body["max_tokens"] = out
    body["n_predict"] = out                      # llama-server's own name for the same limit
    body.pop("max_completion_tokens", None)
    body["n"] = 1                                # one reply per request
    for slot_field in ("id_slot", "slot_id"):    # keep the key away from other users' slots
        body.pop(slot_field, None)
    return json.dumps(body).encode("utf-8")


def take(key: LlamaKey) -> None:
    """Counts one request against the key's rate limits. Raises Refused when over."""
    global _dirty_since
    now = time.time()
    with _lock:
        stored = _keys.get(key.id)
        if stored is None or stored.revoked:
            raise Refused(401, "unauthorized", "This key was revoked.")
        window = _minute.setdefault(key.id, deque())
        while window and now - window[0] >= 60:
            window.popleft()
        if stored.per_minute and len(window) >= stored.per_minute:
            raise Refused(429, "rate_limited", f"This key allows {stored.per_minute} requests per minute.",
                          retry_after=max(1, math.ceil(60 - (now - window[0]))))
        today = _today()
        if stored.uses_day_date != today:
            stored.uses_day_date, stored.uses_day = today, 0
        if stored.per_day and stored.uses_day >= stored.per_day:
            midnight = math.ceil(now / 86400) * 86400
            raise Refused(429, "daily_limit", f"This key allows {stored.per_day} requests per day.",
                          retry_after=max(1, int(midnight - now)))
        window.append(now)
        stored.uses_day += 1
        stored.uses_total += 1
        stored.last_used = now
        if not _dirty_since:
            _dirty_since = now
        if now - _dirty_since >= SAVE_EVERY:
            _save()


def flush() -> None:
    """Writes pending usage counters (gateway shutdown)."""
    with _lock:
        if _dirty_since:
            _save()
