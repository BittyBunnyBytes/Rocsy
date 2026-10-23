#!/usr/bin/env python3
"""Watch ComfyUI's output directory and send new files to Telegram."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Source:
    """One watched ComfyUI output directory, tied to the backend that fills it."""

    label: str
    output_dir: Path
    comfy_url: str
    state_path: Path


SCRIPT_DIR = Path(__file__).resolve().parent
# ROCSY_COMFY_SHARED / ROCSY_COMFY_ROOT as in BunnyBytes/.env (the --output-dir flags override these)
DEFAULT_OUTPUT_DIR = Path(os.path.expanduser(os.getenv("ROCSY_COMFY_SHARED") or os.getenv("ROCSY_COMFY_ROOT") or "~/ComfyUI")) / "output"
DEFAULT_CONFIG = SCRIPT_DIR / ".telegram-comfyui.json"
DEFAULT_STATE = SCRIPT_DIR / ".telegram-comfyui-state.json"
DEFAULT_COMFY_URL = "http://127.0.0.1:8189"
# H3 Machine renders through ComfyUI Desktop's Bunny instance (port 8189 per
# its .env), which writes to its own install's output dir, not the shared
# one above -- see H3Machine's docker-compose.yml for why that path is stale.
DEFAULT_H3_OUTPUT_DIR = Path(os.path.expanduser(os.getenv("ROCSY_COMFY_ROOT") or "~/ComfyUI")) / "output"
DEFAULT_H3_COMFY_URL = "http://127.0.0.1:8189"
DEFAULT_H3_STATE = SCRIPT_DIR / ".telegram-comfyui-state-h3.json"
MAX_UPLOAD_BYTES = 49 * 1024 * 1024
MAX_PHOTO_BYTES = 9 * 1024 * 1024
HISTORY_ITEMS = 200
MIN_UPLOAD_TIMEOUT = 20.0
MAX_UPLOAD_TIMEOUT = 90.0
UPLOAD_BYTES_PER_SECOND = 200 * 1024
BACKFILL_COUNT = 5
H3_INTERMEDIATE = re.compile(r"^H3Machine_\d+_\d{8}-\d{6}_\d{5}\.(?:png|mp4)$")


def _prefer_ipv4(
    host: str,
    port: object,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
    _original=socket.getaddrinfo,
):
    """Sort DNS results IPv4-first so a dead/blackholed IPv6 route to one
    resolved address can't stall every connection behind it (seen hanging
    in SYN-SENT for minutes with no error, since urllib has no Happy-Eyeballs
    fallback of its own)."""
    results = _original(host, port, family, type, proto, flags)
    return sorted(results, key=lambda item: item[0] != socket.AF_INET)


socket.getaddrinfo = _prefer_ipv4


def upload_timeout_for(size_bytes: int) -> float:
    """Cap connect/stall time relative to file size so a hung upload fails
    fast enough to retry instead of blocking the send queue for minutes."""
    estimate = size_bytes / UPLOAD_BYTES_PER_SECOND
    return max(MIN_UPLOAD_TIMEOUT, min(MAX_UPLOAD_TIMEOUT, estimate))


class TelegramError(RuntimeError):
    def __init__(self, message: str, error_code: int | None = None):
        super().__init__(message)
        self.error_code = error_code


def api_request(token: str, method: str, fields: dict[str, str] | None = None) -> object:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(fields or {}).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            result = json.loads(error.read())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TelegramError(f"Telegram returned HTTP {error.code}") from None
    except urllib.error.URLError as error:
        raise TelegramError(f"Could not reach Telegram: {error.reason}") from None

    if not result.get("ok"):
        raise TelegramError(
            result.get("description", "Unknown Telegram API error"), result.get("error_code")
        )
    return result["result"]


def comfy_request(comfy_url: str, endpoint: str) -> object | None:
    """Return JSON from ComfyUI, or None while ComfyUI is offline."""
    url = f"{comfy_url.rstrip('/')}/{endpoint.lstrip('/')}"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return None


def multipart_body(fields: dict[str, str], file_field: str, path: Path) -> tuple[bytes, str]:
    boundary = f"----comfyui-{secrets.token_hex(16)}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    safe_name = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'.encode(),
            f"Content-Type: {mime_type}\r\n\r\n".encode(),
            path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), boundary


def upload_as(
    token: str,
    method: str,
    file_field: str,
    chat_id: str,
    path: Path,
    caption: str,
) -> None:
    body, boundary = multipart_body(
        {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}, file_field, path
    )
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    timeout = upload_timeout_for(len(body))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            result = json.loads(error.read())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TelegramError(f"Telegram returned HTTP {error.code}") from None
    except urllib.error.URLError as error:
        raise TelegramError(f"Upload failed: {error.reason}") from None

    if not result.get("ok"):
        raise TelegramError(
            result.get("description", "Unknown Telegram upload error"), result.get("error_code")
        )


def upload_file(token: str, chat_id: str, path: Path, caption: str) -> None:
    suffix = path.suffix.lower()
    size = path.stat().st_size
    if suffix in {".jpg", ".jpeg", ".png", ".webp"} and size <= MAX_PHOTO_BYTES:
        method, file_field = "sendPhoto", "photo"
    elif suffix == ".gif":
        method, file_field = "sendAnimation", "animation"
    elif suffix == ".mp4":
        method, file_field = "sendVideo", "video"
    elif suffix in {".mp3", ".m4a"}:
        method, file_field = "sendAudio", "audio"
    else:
        method, file_field = "sendDocument", "document"

    try:
        upload_as(token, method, file_field, chat_id, path, caption)
    except TelegramError as error:
        if method == "sendDocument" or error.error_code != 400:
            raise
        upload_as(token, "sendDocument", "document", chat_id, path, caption)


def save_config(path: Path, token: str, chat_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as config_file:
        json.dump({"bot_token": token, "chat_id": chat_id}, config_file, indent=2)
        config_file.write("\n")
    path.chmod(0o600)


def setup(config_path: Path) -> None:
    print("Create a bot with @BotFather in Telegram, then paste its token here.")
    token = input("Bot token (input visible): ").strip()
    if not token:
        raise SystemExit("No bot token entered.")

    bot = api_request(token, "getMe")
    print(f"Connected to @{bot['username']}.")
    print(f"Open https://t.me/{bot['username']}, press Start, and send it any message.")
    input("Press Enter here after sending the message... ")

    updates = api_request(token, "getUpdates", {"timeout": "3", "allowed_updates": '["message"]'})
    messages = [
        update["message"]
        for update in updates
        if "message" in update and update["message"]["chat"].get("type") == "private"
    ]
    if not messages:
        raise SystemExit("No message found. Send the bot a message, then run --setup again.")

    chat = messages[-1]["chat"]
    chat_id = str(chat["id"])
    display_name = chat.get("username") or chat.get("first_name") or chat_id
    save_config(config_path, token, chat_id)
    print(f"Saved chat {display_name} ({chat_id}) to {config_path} with owner-only permissions.")
    api_request(token, "sendMessage", {"chat_id": chat_id, "text": "ComfyUI output watcher is connected."})


def load_credentials(config_path: Path) -> tuple[str, str]:
    config: dict[str, str] = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError) as error:
            raise SystemExit(f"Could not read {config_path}: {error}") from None

    token = os.environ.get("TELEGRAM_BOT_TOKEN") or config.get("bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or config.get("chat_id", "")
    if not token or not chat_id:
        raise SystemExit(f"Telegram is not configured. Run: {Path(__file__).name} --setup")
    return token, str(chat_id)


def output_files(output_dir: Path) -> dict[Path, tuple[int, int]]:
    files: dict[Path, tuple[int, int]] = {}
    for path in output_dir.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files[path] = (stat.st_size, stat.st_mtime_ns)
    return files


def is_h3_intermediate(source: Source, path: Path) -> bool:
    """H3's numbered jobs create a preview and silent video before the final -audio.mp4."""
    return source.label == "H3 Machine" and bool(H3_INTERMEDIATE.fullmatch(path.name))


def load_state(state_path: Path, output_dir: Path) -> dict[Path, tuple[int, int]] | None:
    """Load sent-file fingerprints. None means this is the first run."""
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text())
        if state.get("output_dir") != str(output_dir):
            return None
        handled: dict[Path, tuple[int, int]] = {}
        for relative, fingerprint in state.get("handled", {}).items():
            if (
                isinstance(relative, str)
                and isinstance(fingerprint, list)
                and len(fingerprint) == 2
            ):
                handled[output_dir / relative] = (int(fingerprint[0]), int(fingerprint[1]))
        return handled
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def save_state(
    state_path: Path,
    output_dir: Path,
    handled: dict[Path, tuple[int, int]],
    current: dict[Path, tuple[int, int]],
) -> None:
    """Atomically persist fingerprints for sent files that still exist."""
    saved = {
        str(path.relative_to(output_dir)): list(fingerprint)
        for path, fingerprint in handled.items()
        if path in current
    }
    payload = {"output_dir": str(output_dir), "handled": saved}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_name(f".{state_path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as state_file:
        json.dump(payload, state_file, indent=2, sort_keys=True)
        state_file.write("\n")
    os.replace(temporary, state_path)
    state_path.chmod(0o600)


def walk_dicts(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from walk_dicts(child)


def history_item_for_path(
    history: object, path: Path, output_dir: Path
) -> dict[str, object] | None:
    """Find the completed ComfyUI job that produced path."""
    if not isinstance(history, dict):
        return None
    target = path.resolve()
    for item in reversed(list(history.values())):
        if not isinstance(item, dict):
            continue
        for descriptor in walk_dicts(item.get("outputs", {})):
            filename = descriptor.get("filename")
            if not isinstance(filename, str):
                continue
            fullpath = descriptor.get("fullpath")
            if isinstance(fullpath, str):
                candidate = Path(fullpath)
            else:
                subfolder = descriptor.get("subfolder", "")
                if not isinstance(subfolder, str):
                    subfolder = ""
                candidate = output_dir / subfolder / filename
            try:
                if candidate.resolve() == target:
                    return item
            except OSError:
                continue
    return None


def prompt_from_history(item: dict[str, object]) -> dict[str, object]:
    prompt_record = item.get("prompt")
    if isinstance(prompt_record, (list, tuple)) and len(prompt_record) > 2:
        prompt = prompt_record[2]
        if isinstance(prompt, dict):
            return prompt
    if isinstance(prompt_record, dict):
        return prompt_record
    return {}


def model_names(item: dict[str, object] | None) -> list[str]:
    """Extract primary diffusion/checkpoint model filenames from a job prompt."""
    if item is None:
        return []
    excluded_classes = ("upscale", "lora", "vae", "clip", "control", "detector")
    preferred_keys = {"ckpt_name", "unet_name", "diffusion_model", "model_path"}
    names: list[str] = []
    fallback: list[str] = []
    for node in prompt_from_history(item).values():
        if not isinstance(node, dict):
            continue
        class_name = str(node.get("class_type", "")).lower()
        if any(word in class_name for word in excluded_classes):
            continue
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            continue
        for key, value in inputs.items():
            if not isinstance(value, str) or not value.strip():
                continue
            key_lower = str(key).lower()
            if key_lower in preferred_keys:
                names.append(Path(value).name)
            elif key_lower == "model_name" and "loader" in class_name:
                fallback.append(Path(value).name)
    result: list[str] = []
    for name in names or fallback:
        if name not in result:
            result.append(name)
    return result


def generation_seconds(item: dict[str, object] | None) -> float | None:
    """Calculate execution duration from ComfyUI history timestamps."""
    if item is None:
        return None
    status = item.get("status")
    if not isinstance(status, dict):
        return None
    start: float | None = None
    end: float | None = None
    messages = status.get("messages", [])
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, (list, tuple)) or len(message) != 2:
            continue
        kind, details = message
        if not isinstance(details, dict):
            continue
        timestamp = details.get("timestamp")
        if not isinstance(timestamp, (int, float)):
            continue
        if kind == "execution_start":
            start = float(timestamp)
        elif kind == "execution_success":
            end = float(timestamp)
    if start is None or end is None or end < start:
        return None
    return (end - start) / 1000.0


def queue_pending(comfy_url: str) -> int | None:
    queue = comfy_request(comfy_url, "queue")
    if not isinstance(queue, dict):
        return None
    pending = queue.get("queue_pending")
    return len(pending) if isinstance(pending, list) else None


def _caption_text(value: object, limit: int = 100) -> str:
    clean = " ".join(str(value).split())
    escaped = html.escape(clean, quote=False)
    if len(escaped) > limit:
        clean = clean[:limit]
        while len(html.escape(clean, quote=False)) > limit - 1:
            clean = clean[:-1]
        escaped = html.escape(clean.rstrip(), quote=False) + "…"
    return escaped


def format_caption(path: Path, source_label: str, models: list[str], seconds: float | None) -> str:
    kind = {".mp4": "Video", ".png": "Image", ".jpg": "Image", ".jpeg": "Image",
            ".webp": "Image", ".gif": "Animation", ".mp3": "Audio", ".m4a": "Audio"}.get(path.suffix.lower(), "File")
    icon = {"Video": "🎬", "Image": "🖼", "Animation": "🎞", "Audio": "🎵"}.get(kind, "📎")
    title = path.stem.removesuffix("-audio") + path.suffix
    lines = [f"{icon} <b>{_caption_text(source_label, 40)} · {kind} ready</b>",
             f"<code>{_caption_text(title, 110)}</code>"]
    if models:
        names = ", ".join(_caption_text(Path(name).stem, 75) for name in models[:2])
        lines += ["", f"🎛 <b>Model</b>  {names}"]
    if seconds is not None:
        duration = f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}m {seconds % 60:.0f}s"
        lines.append(f"⏱ <b>Render time</b>  {duration}")
    try:
        size = path.stat().st_size / (1024 * 1024)
    except OSError:
        pass
    else:
        lines.append(f"📦 <b>File size</b>  {size:.1f} MB")
    return "\n".join(lines)


def caption_for(path: Path, output_dir: Path, comfy_url: str, source_label: str) -> tuple[str, bool]:
    history = comfy_request(comfy_url, f"history?max_items={HISTORY_ITEMS}")
    item = history_item_for_path(history, path, output_dir)
    models = model_names(item)
    seconds = generation_seconds(item)
    return format_caption(path, source_label, models, seconds), item is not None


def send_with_retries(token: str, chat_id: str, path: Path, caption: str) -> bool:
    for attempt in range(1, 4):
        try:
            upload_file(token, chat_id, path, caption)
            return True
        except (TelegramError, OSError) as error:
            print(f"Upload attempt {attempt}/3 failed for {path.name}: {error}", file=sys.stderr)
            if attempt < 3:
                time.sleep(2**attempt)
    return False


def send_worker(
    send_queue: "queue.Queue[tuple[Source, Path]]",
    token: str,
    chat_id: str,
    handled_by_source: dict[str, dict[Path, tuple[int, int]]],
    queued: set[tuple[str, Path]],
    lock: threading.Lock,
) -> None:
    """Runs on its own thread so a slow/stuck upload never blocks detection
    of the next file. Telegram is one chat, so sends still happen one at a
    time here (across all watched sources), but the watcher keeps noticing
    and queuing new output while this thread is busy or retrying."""
    while True:
        source, path = send_queue.get()
        try:
            try:
                fingerprint = (path.stat().st_size, path.stat().st_mtime_ns)
            except OSError:
                continue  # file vanished before we got to it

            relative_path = path.relative_to(source.output_dir)
            deadline = time.monotonic() + 10
            while True:
                caption, found_history = caption_for(path, source.output_dir, source.comfy_url, source.label)
                if found_history or time.monotonic() >= deadline:
                    break
                time.sleep(1)

            print(f"Sending [{source.label}] {relative_path}...")
            if send_with_retries(token, chat_id, path, caption):
                with lock:
                    handled = handled_by_source[source.label]
                    handled[path] = fingerprint
                    save_state(source.state_path, source.output_dir, handled, output_files(source.output_dir))
                print(f"Sent [{source.label}] {relative_path}")
            else:
                print(
                    f"Giving up on [{source.label}] {relative_path} for now; will retry once it's noticed again.",
                    file=sys.stderr,
                )
        finally:
            with lock:
                queued.discard((source.label, path))
            send_queue.task_done()


def watch(
    token: str,
    chat_id: str,
    sources: list[Source],
    poll_seconds: float,
    settle_seconds: float,
    send_existing: bool,
) -> None:
    for source in sources:
        if not source.output_dir.is_dir():
            raise SystemExit(f"ComfyUI output directory not found ({source.label}): {source.output_dir}")

    handled_by_source: dict[str, dict[Path, tuple[int, int]]] = {}
    current_by_source: dict[str, dict[Path, tuple[int, int]]] = {}
    for source in sources:
        current = output_files(source.output_dir)
        current_by_source[source.label] = current
        saved_state = load_state(source.state_path, source.output_dir)
        if send_existing:
            handled: dict[Path, tuple[int, int]] = {}
        elif saved_state is None:
            # No state for this directory yet (first run, or the watched
            # directory just changed) — backfill the most recent files instead
            # of silently marking everything as already seen, so a reconfigure
            # or fresh start doesn't miss the latest generations.
            recent_paths = {
                path
                for path, _ in sorted(
                    current.items(), key=lambda item: item[1][1], reverse=True
                )[:BACKFILL_COUNT]
            }
            handled = {path: fp for path, fp in current.items() if path not in recent_paths}
            save_state(source.state_path, source.output_dir, handled, current)
        else:
            handled = saved_state
        handled_by_source[source.label] = handled

    lock = threading.Lock()
    queued: set[tuple[str, Path]] = set()
    send_queue: "queue.Queue[tuple[Source, Path]]" = queue.Queue()
    threading.Thread(
        target=send_worker,
        args=(send_queue, token, chat_id, handled_by_source, queued, lock),
        daemon=True,
    ).start()

    # Each entry is (fingerprint, stable-since). Only tracks whether a file's
    # size/mtime have stopped changing; ComfyUI history readiness is handled
    # by the send worker so a slow write elsewhere can't stall detection.
    pending: dict[tuple[str, Path], tuple[tuple[int, int], float]] = {}
    for source in sources:
        print(f"Watching {source.output_dir} [{source.label}]")
    print("Existing files will be sent." if send_existing else "Existing files skipped; waiting for new output.")

    while True:
        now = time.monotonic()
        for source in sources:
            handled = handled_by_source[source.label]
            current = output_files(source.output_dir)
            current_by_source[source.label] = current
            for path, fingerprint in current.items():
                key = (source.label, path)
                with lock:
                    already_handled = handled.get(path) == fingerprint
                    already_queued = key in queued
                if already_handled:
                    pending.pop(key, None)
                    continue
                if already_queued:
                    continue

                if is_h3_intermediate(source, path):
                    with lock:
                        handled[path] = fingerprint
                        save_state(source.state_path, source.output_dir, handled, current)
                    pending.pop(key, None)
                    continue

                previous = pending.get(key)
                if previous is None or previous[0] != fingerprint:
                    pending[key] = (fingerprint, now)
                    continue
                if now - previous[1] < settle_seconds:
                    continue

                relative_path = path.relative_to(source.output_dir)
                if fingerprint[0] == 0:
                    # A completed zero-byte output can never be sent to Telegram.
                    # Remember it so the watcher does not retry it on every poll.
                    print(f"Skipping empty output [{source.label}]: {relative_path}", file=sys.stderr)
                    with lock:
                        handled[path] = fingerprint
                        save_state(source.state_path, source.output_dir, handled, current)
                    pending.pop(key, None)
                    continue
                if fingerprint[0] > MAX_UPLOAD_BYTES:
                    size_mb = fingerprint[0] / (1024 * 1024)
                    message = (
                        f"ComfyUI output is too large for Telegram ({size_mb:.1f} MB) "
                        f"[{source.label}]: {relative_path}"
                    )
                    try:
                        api_request(token, "sendMessage", {"chat_id": chat_id, "text": message})
                    except TelegramError as error:
                        print(f"Could not report oversized file: {error}", file=sys.stderr)
                    print(message, file=sys.stderr)
                    with lock:
                        handled[path] = fingerprint
                        save_state(source.state_path, source.output_dir, handled, current)
                    pending.pop(key, None)
                    continue

                with lock:
                    queued.add(key)
                send_queue.put((source, path))
                pending.pop(key, None)

        all_current_keys = {
            (source.label, path)
            for source in sources
            for path in current_by_source[source.label]
        }
        missing = set(pending) - all_current_keys
        for key in missing:
            pending.pop(key, None)
        time.sleep(poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send newly created ComfyUI output files to a Telegram chat."
    )
    parser.add_argument("--setup", action="store_true", help="connect the bot and save its chat ID")
    parser.add_argument("--test", action="store_true", help="send a test message and exit")
    parser.add_argument("--send-existing", action="store_true", help="also send files already in the output folder")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--comfy-url", default=DEFAULT_COMFY_URL)
    parser.add_argument(
        "--no-h3", action="store_true", help="don't also watch H3 Machine's output directory"
    )
    parser.add_argument("--h3-output-dir", type=Path, default=DEFAULT_H3_OUTPUT_DIR)
    parser.add_argument("--h3-state", type=Path, default=DEFAULT_H3_STATE)
    parser.add_argument("--h3-comfy-url", default=DEFAULT_H3_COMFY_URL)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--settle-seconds", type=float, default=4.0)
    args = parser.parse_args()
    if args.poll_seconds <= 0 or args.settle_seconds < 0:
        parser.error("poll and settle times must be positive")
    return args


def main() -> None:
    args = parse_args()
    try:
        if args.setup:
            setup(args.config.expanduser())
            return

        token, chat_id = load_credentials(args.config.expanduser())
        if args.test:
            api_request(token, "sendMessage", {"chat_id": chat_id, "text": "ComfyUI Telegram test succeeded."})
            print("Test message sent.")
            return

        sources = [
            Source(
                "ComfyUI",
                args.output_dir.expanduser().resolve(),
                args.comfy_url,
                args.state.expanduser().resolve(),
            )
        ]
        if not args.no_h3:
            sources.append(
                Source(
                    "H3 Machine",
                    args.h3_output_dir.expanduser().resolve(),
                    args.h3_comfy_url,
                    args.h3_state.expanduser().resolve(),
                )
            )

        watch(
            token,
            chat_id,
            sources,
            args.poll_seconds,
            args.settle_seconds,
            args.send_existing,
        )
    except TelegramError as error:
        if error.error_code == 401 or "unauthorized" in str(error).lower():
            raise SystemExit(
                "Telegram rejected the bot token as unauthorized. Run --setup again and paste "
                "the complete current token from @BotFather; revoked and old tokens will not work."
            ) from None
        raise SystemExit(f"Telegram error: {error}") from None
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
