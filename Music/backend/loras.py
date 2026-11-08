"""Music LoRA gallery: YuE2 and MiniMax Music 3 LoRAs found anywhere under ComfyUI's models/loras.

A file is recognised by its safetensors HEADER (key names only), so LoRAs the Trainer publishes
(models/loras/Music/<model>/, with a <name>.json sidecar and a .preview song) and ones downloaded
from elsewhere both show up. Headers are cached by mtime/size.
"""
from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from typing import Any

MODELS = ("yue2", "minimax_music3")
AUDIO_EXTS = (".flac", ".wav", ".mp3", ".ogg", ".opus", ".m4a")
MAX_DEPTH = 4
_cache: dict[str, tuple[float, int, dict[str, Any] | None]] = {}


def _keys(path: Path) -> tuple[list[str], dict[str, str]] | None:
    try:
        with path.open("rb") as fh:
            size = struct.unpack("<Q", fh.read(8))[0]
            if not 0 < size < 64 * 1024 * 1024:
                return None
            header = json.loads(fh.read(size))
    except (OSError, ValueError, struct.error):
        return None
    meta = header.pop("__metadata__", None) or {}
    return list(header), {str(k): str(v) for k, v in meta.items()}


def music_model(keys: list[str], meta: dict[str, str], name: str) -> str | None:
    """yue2 / minimax_music3 / None from a LoRA's tensor names (with the Trainer's sidecar as a tie-breaker)."""
    if any("diffusion_transformer." in k for k in keys):
        return "minimax_music3"
    for k in keys:
        if k.startswith("diffusion_model.llm2vae") or (
                k.startswith(("diffusion_model.model.layers.", "text_encoders.model.layers."))
                and (".gate_up_proj." in k or ".qkv_proj." in k)):
            return "yue2"
    if meta.get("yue2_lora_branch"):
        return "yue2"
    return None


def _entry(root: Path, path: Path) -> dict[str, Any] | None:
    st = path.stat()
    hit = _cache.get(str(path))
    if hit and hit[:2] == (st.st_mtime, st.st_size):
        return hit[2]
    entry = None
    if (parsed := _keys(path)) is not None:
        keys, meta = parsed
        sidecar: dict[str, Any] = {}
        with_json = path.with_suffix(".json")
        if with_json.is_file():
            try:
                sidecar = json.loads(with_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                sidecar = {}
        model = sidecar.get("model") if sidecar.get("model") in MODELS else music_model(keys, meta, path.name)
        if model:
            preview = next((path.with_name(path.stem + ".preview" + ext) for ext in AUDIO_EXTS
                            if path.with_name(path.stem + ".preview" + ext).is_file()), None)
            entry = {
                "file": path.relative_to(root).as_posix(), "model": model,
                "name": str(sidecar.get("name") or path.stem.replace("_", " ")),
                "trigger": str(sidecar.get("trigger") or ""), "dataset": str(sidecar.get("dataset") or ""),
                "made_by": str(sidecar.get("made_by") or ""), "steps": sidecar.get("steps"), "rank": sidecar.get("rank"),
                "preview": preview.relative_to(root).as_posix() if preview else None,
                "size_mb": round(st.st_size / 1048576, 1), "mtime": int(st.st_mtime),
                # YuE2 LoRAs train the AR (ComfyUI's CLIP side) too; a NAR-only one has no text_encoders keys.
                "clip": model == "yue2" and any(k.startswith("text_encoders.") for k in keys),
            }
    _cache[str(path)] = (st.st_mtime, st.st_size, entry)
    return entry


def scan(models_dir: Path) -> list[dict[str, Any]]:
    root = models_dir / "loras"
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        rel = Path(dirpath).relative_to(root)
        if len(rel.parts) >= MAX_DEPTH:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.endswith(".safetensors") and not name.startswith("."):
                path = Path(dirpath) / name
                try:
                    if entry := _entry(root, path):
                        out.append(entry)
                except OSError:
                    continue
    out.sort(key=lambda e: (e["made_by"] != "Trainer", -e["mtime"]))
    return out


def resolve(models_dir: Path, rel: str, model: str) -> dict[str, Any]:
    """A gallery LoRA by its path under models/loras; ValueError unless it exists and is for `model`."""
    root = (models_dir / "loras").resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.suffix != ".safetensors":
        raise ValueError(f"LoRA {rel} is not in models/loras.")
    entry = _entry(root, path)
    if not entry:
        raise ValueError(f"{Path(rel).name} is not a YuE2 or MiniMax Music 3 LoRA.")
    if entry["model"] != model:
        raise ValueError(f"{entry['name']} is a {'YuE2' if entry['model'] == 'yue2' else 'MiniMax Music 3'} LoRA; switch the model to use it.")
    return entry


def preview_path(models_dir: Path, rel: str) -> Path | None:
    root = (models_dir / "loras").resolve()
    path = (root / rel).resolve()
    if path.is_relative_to(root) and path.is_file() and path.suffix.lower() in AUDIO_EXTS and ".preview" in path.name:
        return path
    return None
