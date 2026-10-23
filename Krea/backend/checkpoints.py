"""Installed Krea 2 and Qwen Image 2.1 diffusion models from ComfyUI."""
from __future__ import annotations

import json
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .comfy import ComfyService
from .config import settings
from .models import MODEL_LUSTIFY, MODEL_MOSS, MODEL_QWEN_IMAGE_21, QWEN_ABLITERATED_GGUF

# File name -> family for models whose names don't say which base they are,
# reported by Rocsy Models from the download's base model (see register()).
REGISTRY = settings.data_dir / "checkpoint-families.json"
FAMILIES = ("krea2", "qwen_image_21", MODEL_MOSS)

DEFAULTS = {
    "krea2": "krea2_turbo_int8_convrot.safetensors",
    MODEL_LUSTIFY: "lustifyNSFWCheckpoint_v10Krea2_fp8.safetensors",
    MODEL_QWEN_IMAGE_21: "qwen_image_2.1_int8_convrot.safetensors",
    MODEL_MOSS: "moss_image_sft_int8_convrot.safetensors",
}
_EXTENSIONS = (".safetensors", ".ckpt", ".pt", ".pth", ".gguf")
# ComfyUI-GGUF's loader lists .gguf diffusion models (what Models downloads for GGUF repos).
GGUF_LOADER = "UnetLoaderGGUF"
# Text encoders that sit in diffusion_models (UnetLoaderGGUF lists them) but aren't denoisers.
NOT_DENOISERS = frozenset({QWEN_ABLITERATED_GGUF})


def is_qwen_turbo(filename: str | None) -> bool:
    """Qwen-Image-2.1-Turbo denoisers need their own 8-step schedule (see QWEN_TURBO_SIGMAS)."""
    return bool(filename) and "turbo" in filename.replace("\\", "/").split("/")[-1].lower()


def loader_node(filename: str) -> dict[str, Any]:
    """The UNET loader node for this file: ComfyUI-GGUF's for .gguf, else the core one."""
    if filename.lower().endswith(".gguf"):
        return {"class_type": GGUF_LOADER, "inputs": {"unet_name": filename}}
    return {"class_type": "UNETLoader", "inputs": {"unet_name": filename, "weight_dtype": "default"}}


def _registered() -> dict[str, str]:
    try:
        data = json.loads(REGISTRY.read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if v in FAMILIES} if isinstance(data, dict) else {}


def register(filename: str, group: str) -> None:
    if group not in FAMILIES:
        raise ValueError(f"Unknown model family: {group}")
    data = _registered()
    data[filename.replace("\\", "/").split("/")[-1]] = group
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(REGISTRY)


def family(filename: str, registered: dict[str, str] | None = None) -> str | None:
    # These workflows need different encoders and samplers. Other Qwen versions
    # are intentionally omitted until they have a compatible workflow.
    base = filename.replace("\\", "/").split("/")[-1]
    name = base.lower()
    if not name.endswith(_EXTENSIONS) or base in NOT_DENOISERS:
        return None
    if registered and base in registered:
        return registered[base]
    if re.search(r"qwen[._ -]*image[._ -]*2[._ -]*1", name):
        return "qwen_image_21"
    if re.search(r"moss[._ -]*image", name):
        return MODEL_MOSS
    if re.search(r"krea[._ -]*2|lustify", name):
        return "krea2"
    return None


def _choices(info: dict[str, Any], node: str) -> list:
    choices = info.get(node, {}).get("input", {}).get("required", {}).get("unet_name", [])
    if not isinstance(choices, list) or not choices:
        return []
    if isinstance(choices[0], list):
        return choices[0]
    if choices[0] == "COMBO" and len(choices) > 1 and isinstance(choices[1], dict):
        options = choices[1].get("options", [])
        return options if isinstance(options, list) else []
    return []


def from_object_info(info: dict[str, Any]) -> dict[str, list[str]]:
    names = _choices(info, "UNETLoader") + _choices(info, GGUF_LOADER)
    registered = _registered()
    found: dict[str, list[str]] = {group: [] for group in FAMILIES}
    for name in names:
        if isinstance(name, str) and (group := family(name, registered)) and group != MODEL_MOSS:
            found[group].append(name)
    for name in _choices(info, "MOSSImageUNETLoader"):
        if isinstance(name, str) and family(name, registered) == MODEL_MOSS:
            found[MODEL_MOSS].append(name)
    for group in found:
        found[group] = sorted(set(found[group]), key=str.casefold)
    return found


async def installed(comfy: ComfyService) -> dict[str, list[str]]:
    info = await comfy.request("GET", "/object_info/UNETLoader", timeout=10)
    try:
        info.update(await comfy.request("GET", f"/object_info/{GGUF_LOADER}", timeout=10))
    except Exception:
        pass  # ComfyUI-GGUF not installed: safetensors models only
    try:
        info.update(await comfy.request("GET", "/object_info/MOSSImageUNETLoader", timeout=10))
    except Exception:
        pass  # MOSS custom node not installed yet
    return from_object_info(info)


def resolve(model: str, selected: str | None, found: dict[str, list[str]]) -> str:
    group = MODEL_MOSS if model == MODEL_MOSS else "qwen_image_21" if model == MODEL_QWEN_IMAGE_21 else "krea2"
    options = found[group]
    if selected:
        if selected not in options:
            raise ValueError("The selected checkpoint is not installed in ComfyUI for this model.")
        return selected
    preferred = DEFAULTS.get(model)
    if preferred in options:
        return preferred
    if options:
        return options[0]
    raise ValueError("No compatible checkpoint is installed in ComfyUI for this model.")
