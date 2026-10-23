from __future__ import annotations

import copy
import json
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "engine_config.json"
CONFIGS_DIR = ROOT / "Configs"
VOICES_DIR = ROOT / "data" / "voices"
DOCUMENTS_DIR = ROOT / "Documents"
OUTPUTS_DIR = ROOT / "data" / "worker-outputs"
NOTIFICATIONS_DIR = ROOT / "UserData" / "Notifications"

DEFAULT_CONFIG = {
    "version": 2,
    "generation": {
        "engine": "indextts2",
        "device": "auto",
        "inference_profile": "balanced",
        "default_voice": "",
        "script": "Welcome to BunnyVoice. Choose a voice, shape the performance, and make it yours.",
    },
    "segments": {
        "auto_split": True,
        "max_chars": 280,
        "min_chars": 80,
        "gap_ms": 0,
        "crossfade_ms": 20,
        "normalize_audio": False,
        "index_batch": True,
    },
    "notifications": {
        "completion_sound": True,
        "completion_sound_name": "",
        "completion_sound_version": 0,
    },
    "engines": {
        "indextts2": {
            "emotion": "", "emotion_weight": 1.0, "fp16": True,
            "deepspeed": False, "cuda_kernel": False, "accel": False,
            "torch_compile": False, "verbose": False, "attention": "sdpa",
        },
        "fish_s2": {
            "temperature": 1.0, "top_p": 0.9, "top_k": 30, "seed": 42,
            "max_new_tokens": 0, "compile": False, "half": False,
            "iterative_prompt": True, "chunk_length": 300, "attention": "sdpa",
        },
        "fish_drama3": {
            "temperature": 1.0, "top_p": 0.9, "seed": 42,
            "max_new_tokens": 0, "context_length": 4096,
        },
    },
    "editor": {"font_size": 17, "autosave_draft": True, "wrap": True},
    "ui": {"theme": "midnight", "accent": "violet"},
}


def _merge(defaults, saved):
    if not isinstance(defaults, dict) or not isinstance(saved, dict):
        return copy.deepcopy(saved)
    result = copy.deepcopy(defaults)
    for key, value in saved.items():
        result[key] = _merge(result[key], value) if key in result else copy.deepcopy(value)
    return result


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def load_config(path: Path = CONFIG_PATH) -> dict:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        return _merge(DEFAULT_CONFIG, saved)
    except (OSError, ValueError, TypeError):
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config: dict, path: Path = CONFIG_PATH) -> dict:
    merged = _merge(DEFAULT_CONFIG, config)
    _atomic_json(path, merged)
    return merged


def safe_name(value: str, fallback: str = "untitled") -> str:
    value = re.sub(r"[^\w. -]+", "_", value.strip(), flags=re.UNICODE)
    value = value.strip(" .")[:80]
    return value or fallback


def list_named_configs() -> list[str]:
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    return sorted((path.stem for path in CONFIGS_DIR.glob("*.json")), key=str.casefold)


def save_named_config(name: str, config: dict) -> str:
    name = safe_name(name, "profile")
    _atomic_json(CONFIGS_DIR / f"{name}.json", _merge(DEFAULT_CONFIG, config))
    return name


def load_named_config(name: str) -> dict:
    path = CONFIGS_DIR / f"{safe_name(name)}.json"
    if not path.is_file():
        raise FileNotFoundError(name)
    return load_config(path)


def delete_named_config(name: str) -> None:
    path = CONFIGS_DIR / f"{safe_name(name)}.json"
    if path.is_file():
        path.unlink()


def list_documents() -> list[str]:
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    return sorted((path.stem for path in DOCUMENTS_DIR.glob("*.txt")), key=str.casefold)


def save_document(name: str, text: str) -> str:
    name = safe_name(name, "script")
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = DOCUMENTS_DIR / f"{name}.txt"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    temp_path.replace(path)
    return name


def load_document(name: str) -> str:
    path = DOCUMENTS_DIR / f"{safe_name(name)}.txt"
    if not path.is_file():
        raise FileNotFoundError(name)
    return path.read_text(encoding="utf-8")


def delete_document(name: str) -> None:
    path = DOCUMENTS_DIR / f"{safe_name(name)}.txt"
    if path.is_file():
        path.unlink()


def list_voices() -> list[dict]:
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    voices = []
    for profile in VOICES_DIR.glob("*/profile.json"):
        try:
            item = json.loads(profile.read_text(encoding="utf-8"))
            item.setdefault("summary", "")
            item.setdefault("icon", "🎙️")
            item.setdefault("icon_file", "")
            item.setdefault("reference_duration", None)
            audio = profile.parent / item["audio_file"]
            if audio.is_file():
                item["audio_path"] = str(audio.resolve())
                icon_path = profile.parent / item["icon_file"] if item["icon_file"] else None
                item["icon_url"] = f"/api/voices/{item['id']}/icon" if icon_path and icon_path.is_file() else ""
                voices.append(item)
        except (OSError, ValueError, KeyError):
            continue
    return sorted(voices, key=lambda item: (item.get("category", ""), item.get("name", "").casefold()))


def add_voice(
    name: str, transcript: str, category: str, source_audio: Path,
    summary: str = "", icon: str = "🎙️", reference_duration: float | None = None,
    source_icon: Path | None = None,
) -> dict:
    voice_id = f"{safe_name(name, 'voice').lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}"
    target_dir = VOICES_DIR / voice_id
    target_dir.mkdir(parents=True, exist_ok=False)
    suffix = source_audio.suffix.lower() if source_audio.suffix else ".wav"
    audio_name = f"reference{suffix}"
    shutil.copy2(source_audio, target_dir / audio_name)
    icon_name = "icon.webp" if source_icon else ""
    if source_icon:
        shutil.copy2(source_icon, target_dir / icon_name)
    profile = {
        "id": voice_id,
        "name": name.strip() or "Untitled voice",
        "category": category.strip() or "Uncategorized",
        "summary": summary.strip()[:500],
        "icon": icon.strip()[:12] or "🎙️",
        "icon_file": icon_name,
        "transcript": transcript.strip(),
        "reference_duration": round(reference_duration, 3) if reference_duration else None,
        "audio_file": audio_name,
    }
    _atomic_json(target_dir / "profile.json", profile)
    profile["audio_path"] = str((target_dir / audio_name).resolve())
    profile["icon_url"] = f"/api/voices/{voice_id}/icon" if icon_name else ""
    return profile


def update_voice(
    voice_id: str, name: str, transcript: str, category: str, summary: str,
    icon: str = "🎙️", source_icon: Path | None = None,
    remove_icon: bool = False,
) -> dict:
    """Update a voice profile without changing its stable ID or reference audio."""
    target_dir = VOICES_DIR / voice_id
    profile_path = target_dir / "profile.json"
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise FileNotFoundError(voice_id)
    if profile.get("id") != voice_id or not (target_dir / profile.get("audio_file", "")).is_file():
        raise FileNotFoundError(voice_id)

    old_icon = profile.get("icon_file", "")
    if source_icon:
        icon_name = "icon.webp"
        shutil.copy2(source_icon, target_dir / icon_name)
    elif remove_icon:
        icon_name = ""
        if old_icon:
            (target_dir / old_icon).unlink(missing_ok=True)
    else:
        icon_name = old_icon

    profile.update({
        "name": name.strip() or "Untitled voice",
        "category": category.strip() or "Uncategorized",
        "summary": summary.strip()[:500],
        "icon": icon.strip()[:12] or "🎙️",
        "icon_file": icon_name,
        "transcript": transcript.strip(),
        "updated_at": int(time.time()),
    })
    _atomic_json(profile_path, profile)
    return next(voice for voice in list_voices() if voice["id"] == voice_id)


def delete_voice(voice_id: str) -> None:
    valid_ids = {voice["id"] for voice in list_voices()}
    if voice_id not in valid_ids:
        raise FileNotFoundError(voice_id)
    shutil.rmtree(VOICES_DIR / voice_id)


def get_voice(voice_id_or_name: str) -> dict | None:
    needle = (voice_id_or_name or "").casefold()
    for voice in list_voices():
        if voice["id"].casefold() == needle or voice["name"].casefold() == needle:
            return voice
    return None


def list_outputs(limit: int = 30) -> list[dict]:
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(OUTPUTS_DIR.glob("*.wav"), key=lambda path: path.stat().st_mtime, reverse=True)
    outputs = []
    for path in files[:limit]:
        item = {"name": path.name, "url": f"/outputs/{path.name}", "size": path.stat().st_size}
        metadata_path = path.with_suffix(".meta.json")
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(metadata.get("request"), dict):
                    item["request"] = metadata["request"]
            except (OSError, ValueError):
                pass
        outputs.append(item)
    return outputs
