"""Validated, per-engine Voice settings stored with Rocsy's Voice data."""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

DATA = Path(os.getenv("VOICE_DATA_DIR", "/data"))
STORE = DATA / "engine_settings.json"
LOCK = threading.RLock()


def number(key: str, label: str, default: int | float, low: int | float,
           high: int | float, step: int | float, group: str, hint: str) -> dict:
    return dict(key=key, label=label, default=default, min=low, max=high,
                step=step, kind="number", group=group, hint=hint)


def toggle(key: str, label: str, default: bool, group: str, hint: str) -> dict:
    return dict(key=key, label=label, default=default, kind="toggle", group=group, hint=hint)


def text(key: str, label: str, default: str, group: str, hint: str) -> dict:
    return dict(key=key, label=label, default=default, kind="text", group=group, hint=hint)


_SEGMENTS = [
    toggle("auto_split", "Split long text", True, "Audio & pacing", "Break long scripts at sentence boundaries."),
    number("max_chars", "Characters per segment", 280, 80, 2000, 10, "Audio & pacing", "Maximum text sent to the engine at once."),
    number("min_chars", "Minimum final segment", 80, 0, 500, 10, "Audio & pacing", "Merge a short final segment when it fits."),
    number("gap_ms", "Pause between segments (ms)", 0, 0, 5000, 50, "Audio & pacing", "Silence inserted between generated parts."),
    number("crossfade_ms", "Crossfade (ms)", 20, 0, 250, 10, "Audio & pacing", "Blend segment joins. Zero makes a hard join."),
    toggle("normalize_audio", "Normalize output level", False, "Audio & pacing", "Apply loudness normalization when joining segments."),
]

SCHEMAS = {
    "qwen3": {
        "name": "Qwen3-TTS 1.7B Base",
        "description": "Voice cloning from the selected character's reference clip.",
        "fields": [
            number("temperature", "Temperature", 0.9, 0.1, 2.0, 0.05, "Sampling", "Higher values give more variation."),
            number("top_p", "Top P", 1.0, 0.05, 1.0, 0.05, "Sampling", "Limits token choices by cumulative probability."),
            number("top_k", "Top K", 50, 1, 200, 1, "Sampling", "Limits token choices to the most likely candidates."),
            number("repetition_penalty", "Repetition penalty", 1.05, 1.0, 2.0, 0.05, "Sampling", "Higher values discourage repeated sounds."),
            number("chunk_chars", "Characters per chunk", 400, 80, 1000, 20, "Audio & pacing", "Long scripts are split into chunks of roughly this size."),
            number("chunk_gap_ms", "Pause between chunks (ms)", 250, 0, 2000, 50, "Audio & pacing", "Silence between Qwen chunks."),
        ],
    },
    "indextts2": {
        "name": "IndexTTS2",
        "description": "Reference-based speech with text-guided emotion. Use [EMO=...] cues in the script for changes within a passage.",
        "fields": [
            text("emotion", "Emotion prompt", "", "Expression", "Natural-language delivery guidance for the whole script, such as warm and calm. Inline [EMO=...] cues override it."),
            number("emotion_weight", "Emotion strength", 1.0, 0.0, 1.0, 0.05, "Expression", "How strongly text emotion guidance affects speech; IndexTTS2 recommends about 0.6 or lower for natural results."),
            toggle("fp16", "Use FP16", True, "Performance", "Lower GPU memory use; disable if this engine has precision issues."),
            toggle("index_batch", "Batch script segments", True, "Performance", "Load the model once for multi-segment scripts."),
            toggle("deepspeed", "Use DeepSpeed", False, "Performance", "Requires a working DeepSpeed installation in the engine environment."),
            toggle("cuda_kernel", "Use CUDA kernel", False, "Performance", "Enable the engine's optional CUDA kernel path."),
            toggle("accel", "Use acceleration", False, "Performance", "Enable the engine's optional acceleration path."),
            toggle("torch_compile", "Compile model", False, "Performance", "Longer first run; may improve repeated generations."),
            *_SEGMENTS,
        ],
    },
    "fish_s2": {
        "name": "Fish Audio S2 Pro",
        "description": "Reference-based speech with performance cues in the script.",
        "fields": [
            number("temperature", "Temperature", 1.0, 0.1, 2.0, 0.05, "Sampling", "Higher values give more variation."),
            number("top_p", "Top P", 0.9, 0.05, 1.0, 0.05, "Sampling", "Limits token choices by cumulative probability."),
            number("seed", "Seed", 42, 0, 2147483647, 1, "Sampling", "Use the same seed to make runs more repeatable."),
            number("chunk_length", "Model chunk length", 300, 100, 1000, 10, "Generation", "How much speech the model processes per step."),
            number("max_new_tokens", "Token limit", 0, 0, 4096, 32, "Generation", "Zero uses the engine's default limit."),
            toggle("half", "Use FP16", False, "Performance", "Lower GPU memory use; may change output quality."),
            toggle("compile", "Compile model", False, "Performance", "Longer first run; may improve repeated generations."),
            *_SEGMENTS,
        ],
    },
    "fish_v1": {
        "name": "Fish Audio 1.5",
        "description": "Reference-based speech with separate semantic and audio decoding steps.",
        "fields": [
            number("temperature", "Temperature", 0.7, 0.1, 2.0, 0.05, "Sampling", "Higher values give more variation."),
            number("top_p", "Top P", 0.7, 0.05, 1.0, 0.05, "Sampling", "Limits token choices by cumulative probability."),
            number("repetition_penalty", "Repetition penalty", 1.2, 1.0, 2.0, 0.05, "Sampling", "Higher values discourage repeated sounds."),
            number("seed", "Seed", 42, 0, 2147483647, 1, "Sampling", "Use the same seed to make runs more repeatable."),
            number("chunk_length", "Model chunk length", 100, 20, 1000, 10, "Generation", "How much speech the model processes per step."),
            number("max_new_tokens", "Token limit", 0, 0, 4096, 32, "Generation", "Zero uses the engine's default limit."),
            toggle("iterative_prompt", "Iterative prompt", True, "Generation", "Carry prompt context through longer passages."),
            toggle("half", "Use FP16", False, "Performance", "Lower GPU memory use; may change output quality."),
            toggle("compile", "Compile model", False, "Performance", "Longer first run; may improve repeated generations."),
            *_SEGMENTS,
        ],
    },
}


def defaults(engine: str) -> dict[str, Any]:
    return {field["key"]: field["default"] for field in SCHEMAS[engine]["fields"]}


def _validate_field(field: dict, value: Any) -> Any:
    kind = field["kind"]
    if kind == "toggle":
        if type(value) is not bool:
            raise ValueError(f"{field['label']} must be on or off")
    elif kind == "text":
        if not isinstance(value, str) or len(value) > 120 or any(ord(char) < 32 for char in value):
            raise ValueError(f"{field['label']} must be short text")
        value = value.strip()
    else:
        integer = isinstance(field["default"], int) and not isinstance(field["default"], bool)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{field['label']} must be a number")
        if integer and not float(value).is_integer():
            raise ValueError(f"{field['label']} must be a whole number")
        if not field["min"] <= value <= field["max"]:
            raise ValueError(f"{field['label']} must be between {field['min']} and {field['max']}")
        value = int(value) if integer else float(value)
    return value


def validate(engine: str, values: dict) -> dict[str, Any]:
    if engine not in SCHEMAS:
        raise KeyError(engine)
    if not isinstance(values, dict):
        raise ValueError("Settings must be an object")
    fields = {field["key"]: field for field in SCHEMAS[engine]["fields"]}
    unknown = set(values) - set(fields)
    if unknown:
        raise ValueError(f"Unknown {engine} setting: {sorted(unknown)[0]}")
    clean = {key: _validate_field(fields[key], value) for key, value in values.items()}
    return clean


def _read() -> dict:
    try:
        data = json.loads(STORE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=DATA, prefix=".engine-settings-", delete=False) as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, STORE)


def get_engine(engine: str) -> dict[str, Any]:
    if engine not in SCHEMAS:
        raise KeyError(engine)
    with LOCK:
        raw = _read().get(engine, {})
        result = defaults(engine)
        if isinstance(raw, dict):
            for key, value in raw.items():
                try:
                    result.update(validate(engine, {key: value}))
                except ValueError:
                    continue
        return result


def save_engine(engine: str, changes: dict) -> dict[str, Any]:
    clean = validate(engine, changes)
    with LOCK:
        data = _read()
        current = get_engine(engine)
        current.update(clean)
        if engine != "qwen3" and current["min_chars"] > current["max_chars"]:
            raise ValueError("Minimum final segment cannot exceed characters per segment")
        data[engine] = current
        _write(data)
        return current


def reset_engine(engine: str) -> dict[str, Any]:
    if engine not in SCHEMAS:
        raise KeyError(engine)
    with LOCK:
        data = _read()
        data.pop(engine, None)
        _write(data)
        return defaults(engine)


def response() -> dict:
    return {"engines": [{"id": key, **schema, "values": get_engine(key)}
                        for key, schema in SCHEMAS.items()]}
