"""Qwen-Image 2.1 API-format workflow builders.

Unlike Krea 2 (which patches a checked-in template), these graphs are built
node-by-node because the reference count (0-10) changes the node set. The
wiring mirrors Comfy-Org's official templates:
  image_qwen_image_2_1_t2i.json / _image_edit.json / _background_removal.json
(UNETLoader -> QwenImage21Cache -> KSampler; CLIPLoader type "qwen_image" ->
TextEncodeQwenImage21 with `images.image_N` autogrow inputs + VAE; euler/simple,
25 steps, cfg 1).

The optional prompt enhancer runs as a separate ComfyUI prompt *before* the
image prompt: TextGenerate over the Qwen3.5-9B PE-T2I / PE-I2I checkpoints with
the model's own system prompt, surfaced through PreviewAny so the app can read
the JSON answer back from /history.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any

from .checkpoints import is_qwen_turbo, loader_node
from .config import BASE_DIR, Settings
from .models import (MAX_SIZE, QWEN_LORA_SLOTS, QWEN_MAX_PIXELS, QWEN_MAX_SIDE, QWEN_TEXT_ENCODERS, QWEN_TURBO_SIGMAS,
                     QWEN_VIGGLE_LORA, QWEN_VIGGLE_NODES, MIN_SIZE, GenerationRequest)


UNET_NAME = "qwen_image_2.1_int8_convrot.safetensors"
TEXT_ENCODER_NAME = QWEN_TEXT_ENCODERS["standard"]
VAE_NAME = "qwen_image_2.1_vae_bf16.safetensors"
PE_T2I_NAME = "qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors"
PE_I2I_NAME = "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors"
UPSCALE_MODEL_NAME = "RealESRGAN_x4plus.pth"

# Official transparency phrasing from the Qwen-Image 2.1 release notes.
TRANSPARENT_PREFIX = "This is an RGBA image with transparency."
TRANSPARENT_SUFFIX = "The image has alpha channel and the background is transparent."

# Node ids of the enhancer graph's text output (read back from /history).
ENHANCER_OUTPUT_NODE = "3"

_SYSTEM_PROMPT_FILES = {
    "t2i": "qwen_image_21_pe_t2i_system_prompt.txt",
    "i2i": "qwen_image_21_pe_i2i_system_prompt.txt",
}


class QwenWorkflowError(ValueError):
    pass


@dataclass(slots=True)
class EnhancerResult:
    prompt: str
    # Explicit ratio chosen by the enhancer ("16:9"), or None.
    wh_ratio: str | None = None
    # 1-based reference index whose aspect ratio to inherit (edit mode), or None.
    ratio_follow: int | None = None


def _round32(value: float) -> int:
    return max(32, round(value / 32) * 32)


def clamp_size(width: int, height: int) -> tuple[int, int]:
    """Round to multiples of 32 (16x VAE * 2x2 patch) and fit Qwen's limits
    while preserving the aspect ratio."""
    width, height = _round32(width), _round32(height)
    scale = 1.0
    if width * height > QWEN_MAX_PIXELS:
        scale = math.sqrt(QWEN_MAX_PIXELS / (width * height))
    scale = min(scale, QWEN_MAX_SIDE / max(width, height))
    scale = max(scale, MIN_SIZE / min(width, height))
    if scale != 1.0:
        width, height = int(width * scale) // 32 * 32, int(height * scale) // 32 * 32
    return max(MIN_SIZE, min(QWEN_MAX_SIDE, width)), max(MIN_SIZE, min(QWEN_MAX_SIDE, height))


def size_for_ratio(ratio_w: float, ratio_h: float, pixel_area: int) -> tuple[int, int]:
    """Keep the user's chosen pixel budget, reshape it to the given ratio."""
    ratio = ratio_w / ratio_h
    return clamp_size(math.sqrt(pixel_area * ratio), math.sqrt(pixel_area / ratio))


def krea_size_for_ratio(ratio_w: float, ratio_h: float, pixel_area: int) -> tuple[int, int]:
    """Reshape Krea's selected pixel budget to a ratio while honoring its
    512..1536 per-side limits and 32-pixel latent grid."""
    ratio = ratio_w / ratio_h
    width, height = math.sqrt(pixel_area * ratio), math.sqrt(pixel_area / ratio)
    scale = min(1.0, MAX_SIZE / max(width, height))
    width, height = width * scale, height * scale
    # Very extreme ratios cannot satisfy both the minimum short side and the
    # maximum long side. Preserve the ratio and max side in that case.
    if min(width, height) < MIN_SIZE:
        scale = MIN_SIZE / min(width, height)
        if max(width, height) * scale <= MAX_SIZE:
            width, height = width * scale, height * scale
    width, height = _round32(width), _round32(height)
    return min(MAX_SIZE, width), min(MAX_SIZE, height)


def parse_ratio(value: str | None) -> tuple[float, float] | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:x/]\s*(\d+(?:\.\d+)?)\s*", value or "")
    if not match:
        return None
    w, h = float(match.group(1)), float(match.group(2))
    return (w, h) if w > 0 and h > 0 else None


def apply_transparency(prompt: str) -> str:
    if "RGBA" in prompt and "transparen" in prompt.lower():
        return prompt
    body = prompt.strip().rstrip(".")
    return f"{TRANSPARENT_PREFIX} {body}. {TRANSPARENT_SUFFIX}"


def _save_prefix() -> str:
    return f"QwenImage21/QwenImage21_{time.strftime('%Y%m%d-%H%M%S')}"


def _add_reference_loaders(workflow: dict[str, Any], request: GenerationRequest) -> list[list[Any]]:
    """LoadImage + JoinImageWithAlpha per active reference so transparent
    inputs reach the RGBA VAE with their alpha intact (opaque images get a
    solid alpha). Returns the RGBA IMAGE links in slot order."""
    links: list[list[Any]] = []
    for index, ref in enumerate(request.active_references, start=1):
        load_id, join_id = str(100 + index), str(200 + index)
        workflow[load_id] = {"class_type": "LoadImage", "inputs": {"image": ref.image}}
        workflow[join_id] = {"class_type": "JoinImageWithAlpha", "inputs": {"image": [load_id, 0], "alpha": [load_id, 1]}}
        links.append([join_id, 0])
    return links


def _add_alpha_safe_upscale(workflow: dict[str, Any], image: list[Any]) -> list[Any]:
    """Real-ESRGAN only accepts RGB. Split off the alpha, upscale the colour
    with the model and the alpha bilinearly, then rejoin."""
    workflow["20"] = {"class_type": "SplitImageWithAlpha", "inputs": {"image": image}}
    workflow["21"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": UPSCALE_MODEL_NAME}}
    workflow["22"] = {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["21", 0], "image": ["20", 0]}}
    workflow["23"] = {"class_type": "MaskToImage", "inputs": {"mask": ["20", 1]}}
    workflow["24"] = {"class_type": "ImageScaleBy", "inputs": {"image": ["23", 0], "upscale_method": "bilinear", "scale_by": 4.0}}
    workflow["25"] = {"class_type": "ImageToMask", "inputs": {"image": ["24", 0], "channel": "red"}}
    workflow["26"] = {"class_type": "JoinImageWithAlpha", "inputs": {"image": ["22", 0], "alpha": ["25", 0]}}
    return ["26", 0]


def build_image_workflow(
    request: GenerationRequest,
    seed: int,
    prompt: str,
    size: tuple[int, int] | None,
) -> dict[str, Any]:
    """`size` None means "render at the first reference's aspect ratio"
    (the encoder's own latent output); otherwise an explicit width/height."""
    references = request.active_references
    edit_mode = bool(references)
    if not edit_mode and size is None:
        raise QwenWorkflowError("Text-to-image needs an explicit output size.")
    if request.transparent:
        prompt = apply_transparency(prompt)

    workflow: dict[str, Any] = {
        "1": loader_node(request.checkpoint or UNET_NAME),
        "2": text_encoder_node(request.text_encoder),
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_NAME}},
        # Prefix KV cache (text + reference tokens) reused across steps.
        "4": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": request.kv_cache_device, "dtype": request.kv_cache_dtype}},
    }
    # Enabled LoRA slots (QWEN_LORA_SLOTS by index) as a model-only chain before the KV cache.
    model = ["1", 0]
    for index, slot in enumerate(request.loras[:len(QWEN_LORA_SLOTS)]):
        if slot.enabled and slot.strength:
            node = str(40 + index)
            workflow[node] = {"class_type": "LoraLoaderModelOnly",
                              "inputs": {"model": model, "lora_name": QWEN_LORA_SLOTS[index], "strength_model": slot.strength}}
            model = [node, 0]
    workflow["4"]["inputs"]["model"] = model
    encoder_inputs: dict[str, Any] = {
        "clip": ["2", 0],
        "prompt": prompt,
        "negative_prompt": request.negative_prompt,
        "resolution": request.reference_resolution,
    }
    if edit_mode:
        encoder_inputs["vae"] = ["3", 0]
        for index, link in enumerate(_add_reference_loaders(workflow, request), start=1):
            encoder_inputs[f"images.image_{index}"] = link
    workflow["5"] = {"class_type": "TextEncodeQwenImage21", "inputs": encoder_inputs}

    if size is None:
        latent = ["5", 2]
    else:
        workflow["6"] = {"class_type": "EmptyLatentImage", "inputs": {"width": size[0], "height": size[1], "batch_size": 1}}
        latent = ["6", 0]

    workflow["7"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": ["4", 0], "positive": ["5", 0], "negative": ["5", 1], "latent_image": latent,
            "seed": seed, "steps": request.steps, "cfg": request.cfg,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
        },
    }
    if is_qwen_turbo(request.checkpoint):
        _apply_turbo(workflow)
    elif request.speed == "viggle":
        _apply_viggle(workflow, size or reference_output_size(request) or request.normalized_size())
    workflow["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}}
    image = ["8", 0]
    if request.enhance:
        image = _add_alpha_safe_upscale(workflow, image)
    workflow["9"] = {"class_type": "SaveImage", "inputs": {"images": image, "filename_prefix": _save_prefix()}}
    return workflow


def text_encoder_node(choice: str) -> dict[str, Any]:
    return {"class_type": "CLIPLoader", "inputs": {"clip_name": QWEN_TEXT_ENCODERS[choice], "type": "qwen_image", "device": "default"}}


def _apply_turbo(workflow: dict[str, Any]) -> None:
    """Qwen-Image-2.1-Turbo: euler on the fixed 8-step schedule at CFG 1 (official
    turbo template; BasicGuider = CFG 1 without the unused negative pass)."""
    sampler = workflow["7"]["inputs"]
    workflow["71"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": sampler["seed"]}}
    workflow["72"] = {"class_type": "BasicGuider", "inputs": {"model": sampler["model"], "conditioning": sampler["positive"]}}
    workflow["73"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}
    workflow["74"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": ", ".join(str(s) for s in QWEN_TURBO_SIGMAS)}}
    workflow["7"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
        "noise": ["71", 0], "guider": ["72", 0], "sampler": ["73", 0], "sigmas": ["74", 0], "latent_image": sampler["latent_image"]}}


def viggle_sigmas(width: int, height: int) -> list[float]:
    """Viggle's raw 6-step nodes with the pipeline's resolution-dependent time shift
    (same maths as their ViggleTurboSigmas node, so no custom node is needed)."""
    tokens = (height // 16) * (width // 16)
    mu = 0.5 + (0.9 - 0.5) * (tokens - 256) / (8192 - 256)
    return [math.exp(mu) / (math.exp(mu) + (1 / t - 1)) for t in QWEN_VIGGLE_NODES] + [0.0]


def _apply_viggle(workflow: dict[str, Any], size: tuple[int, int]) -> None:
    """Swap node 7's KSampler for the turbo LoRA + fixed sigmas, euler, no CFG
    (BasicGuider: the negative prompt is unused, as the author prescribes)."""
    sampler = workflow["7"]["inputs"]
    workflow["70"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": sampler["model"], "lora_name": QWEN_VIGGLE_LORA, "strength_model": 1.0}}
    workflow["71"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": sampler["seed"]}}
    workflow["72"] = {"class_type": "BasicGuider", "inputs": {"model": ["70", 0], "conditioning": sampler["positive"]}}
    workflow["73"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}
    workflow["74"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": ", ".join(f"{s:.6f}" for s in viggle_sigmas(*size))}}
    workflow["7"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
        "noise": ["71", 0], "guider": ["72", 0], "sampler": ["73", 0], "sigmas": ["74", 0], "latent_image": sampler["latent_image"]}}


def _system_prompt(kind: str, config: Settings) -> str:
    name = _SYSTEM_PROMPT_FILES[kind]
    for directory in (config.workflow_path.parent, BASE_DIR / "workflows"):
        path = directory / name
        if path.is_file():
            return path.read_text().strip()
    raise QwenWorkflowError(f"The Qwen prompt-enhancer system prompt ({name}) is missing from the workflows directory.")


def build_enhancer_workflow(config: Settings, request: GenerationRequest, seed: int) -> dict[str, Any]:
    references = request.active_references
    kind = "i2i" if references else "t2i"
    vision = "<|vision_start|><|image_pad|><|vision_end|>" * len(references)
    # A prompt that starts with <|im_start|> bypasses ComfyUI's built-in chat
    # template, which is what lets us supply the PE model's own system prompt.
    chat = (
        f"<|im_start|>system\n{_system_prompt(kind, config)}<|im_end|>\n"
        f"<|im_start|>user\n{vision}{request.prompt}<|im_end|>\n"
        "<|im_start|>assistant\n"
    )
    chat += "<think>\n" if request.enhancer_thinking else "<think>\n\n</think>\n\n"

    workflow: dict[str, Any] = {
        "1": {"class_type": "CLIPLoader", "inputs": {"clip_name": PE_I2I_NAME if references else PE_T2I_NAME, "type": "qwen_image", "device": "default"}},
    }
    generate_inputs: dict[str, Any] = {
        "clip": ["1", 0],
        "prompt": chat,
        "max_length": 16384 if request.enhancer_thinking else 2048,
        # Official PE sampling: temperature 1.0, top_p 0.95, top_k 20.
        "sampling_mode": "on",
        "sampling_mode.temperature": 1.0,
        "sampling_mode.top_k": 20,
        "sampling_mode.top_p": 0.95,
        "sampling_mode.min_p": 0.0,
        "sampling_mode.repetition_penalty": 1.0,
        "sampling_mode.seed": seed % (2**63),
        "thinking": request.enhancer_thinking,
        "use_default_template": True,
        "mtp": "auto",
    }
    if references:
        # TextGenerate takes one IMAGE batch; ImageBatch resizes each later
        # image to the first so differently-sized references can share it.
        batch: list[Any] | None = None
        for index, ref in enumerate(references, start=1):
            load_id = str(100 + index)
            workflow[load_id] = {"class_type": "LoadImage", "inputs": {"image": ref.image}}
            if batch is None:
                batch = [load_id, 0]
            else:
                batch_id = str(300 + index)
                workflow[batch_id] = {"class_type": "ImageBatch", "inputs": {"image1": batch, "image2": [load_id, 0]}}
                batch = [batch_id, 0]
        generate_inputs["image"] = batch
    workflow["2"] = {"class_type": "TextGenerate", "inputs": generate_inputs}
    workflow[ENHANCER_OUTPUT_NODE] = {"class_type": "PreviewAny", "inputs": {"source": ["2", 0]}}
    return workflow


def enhancer_text_from_history(history: dict[str, Any]) -> str | None:
    node = (history.get("outputs") or {}).get(ENHANCER_OUTPUT_NODE) or {}
    text = node.get("text")
    if isinstance(text, list):
        text = text[0] if text else None
    return str(text) if text else None


def parse_enhancer_output(text: str) -> EnhancerResult:
    """The PE models answer `{"rewritten_prompt": ..., "wh_ratio": ..., "ratio_follow": ...}`
    after an optional <think> block."""
    answer = text.rpartition("</think>")[2] if "</think>" in text else text
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end <= start:
        raise QwenWorkflowError("The prompt enhancer did not return a JSON answer.")
    try:
        data = json.loads(answer[start:end + 1])
    except json.JSONDecodeError as exc:
        raise QwenWorkflowError("The prompt enhancer returned malformed JSON.") from exc
    prompt = str(data.get("rewritten_prompt") or "").strip()
    if not prompt:
        raise QwenWorkflowError("The prompt enhancer returned an empty prompt.")
    wh_ratio = str(data.get("wh_ratio") or "").strip() or None
    follow_match = re.search(r"image\s*(\d+)", str(data.get("ratio_follow") or ""))
    return EnhancerResult(prompt=prompt, wh_ratio=wh_ratio, ratio_follow=int(follow_match.group(1)) if follow_match else None)


def reference_output_size(request: GenerationRequest) -> tuple[int, int] | None:
    """The size TextEncodeQwenImage21 gives its latent (first reference at the
    reference-resolution budget, multiples of 32) - for display only."""
    references = request.active_references
    if not references or not (references[0].width and references[0].height):
        return None
    ref, budget = references[0], request.reference_resolution
    if budget <= 0:
        return max(32, round(ref.width / 32) * 32), max(32, round(ref.height / 32) * 32)
    ratio = ref.width / ref.height
    return max(32, round(math.sqrt(budget * budget * ratio) / 32) * 32), max(32, round(math.sqrt(budget * budget / ratio) / 32) * 32)


def resolve_size(request: GenerationRequest, enhanced: EnhancerResult | None = None) -> tuple[int, int] | None:
    """Output size for the image prompt; None = follow the first reference."""
    width, height = request.normalized_size()
    references = request.active_references
    use_enhancer_size = enhanced is not None and request.enhancer_picks_size
    if use_enhancer_size and enhanced.ratio_follow and references:
        if enhanced.ratio_follow == 1:
            return None
        if enhanced.ratio_follow <= len(references):
            ref = references[enhanced.ratio_follow - 1]
            if ref.width and ref.height:
                return size_for_ratio(ref.width, ref.height, width * height)
    if use_enhancer_size and (ratio := parse_ratio(enhanced.wh_ratio)):
        return size_for_ratio(*ratio, width * height)
    if references and request.match_reference_size:
        return None
    return clamp_size(width, height)


def resolve_krea_size(request: GenerationRequest, enhanced: EnhancerResult) -> tuple[int, int]:
    """Apply the PE model's ratio decision to Krea/Lustify while keeping the
    user's selected pixel budget. Invalid or unusable decisions fall back to
    the requested size."""
    width, height = request.normalized_size()
    if not request.enhancer_picks_size:
        return width, height
    references = request.active_references
    if enhanced.ratio_follow and enhanced.ratio_follow <= len(references):
        ref = references[enhanced.ratio_follow - 1]
        if ref.width and ref.height:
            return krea_size_for_ratio(ref.width, ref.height, width * height)
    if ratio := parse_ratio(enhanced.wh_ratio):
        return krea_size_for_ratio(*ratio, width * height)
    return width, height
