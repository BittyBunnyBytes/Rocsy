from __future__ import annotations

import copy
import json
import random
import time
from typing import Any

from . import anima, moss, qwen
from .checkpoints import loader_node
from .config import Settings
from .models import (
    DEFAULT_LORA_ENABLED,
    DEFAULT_REFERENCE_STRENGTHS,
    LORA_SLOTS,
    MAX_SEED,
    KREA_STYLE_REFERENCE_LORA,
    REFERENCE_SLOT_COUNT,
    STANDARD_STEPS,
    TURBO_LORAS,
    MODEL_ANIMA,
    MODEL_MOSS,
    GenerationRequest,
)


class GenerationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# Node-id map for workflows/Krea2-Advanced-LoRA-API.json -- see
# BunnyBytes/workflows/NOTES.md for the full derivation from the source
# frontend graph (user/default/workflows/Image/Krea 2/Krea2-Advanced-LoRA.json).
_UNET_NODE = "1"
_PROMPT_NODE = "5"
_SIZE_NODE = "7"
_SAMPLER_NODE = "8"
_SAVE_NODE = "10"
_LORA_LOADER_NODE = "4"
_ENHANCE_TOGGLE_NODE = "19"
# The template's own reference branch (LoadImage -> VAEEncode -> LatentMultiply
# -> ReferenceLatent with method "index", plus its toggle/switch nodes) only
# appended VAE latents, which Krea 2 alone doesn't follow, so it is always
# removed. Active references get ComfyUI's official Krea 2 reference graph
# instead (template image_krea2_turbo_int8_image_style_reference): the
# krea2_style_reference LoRA, TextEncodeQwenImageEditPlus (images go to the
# Qwen3-VL text encoder AND become reference latents) and the
# "index_timestep_zero" reference method.
_TEMPLATE_REFERENCE_NODES = (
    "12", "13", "14", "15", "16", "21", "22", "23", "24", "25", "26", "27", "28",
    "29", "30", "31", "32", "33", "34", "35", "36", "37", "38", "39", "40", "41", "42",
)
_NEGATIVE_NODE = "6"
_REF_LORA_NODE = "50"
_REF_ENCODE_NODE = "51"
_REF_METHOD_NODE = "52"
_REF_IMAGE_NODES = ("53", "54", "55")
_REF_SAMPLING_NODE = "56"
_SAVE_FOLDERS = {"krea2": "Krea2/Krea2", "lustify": "Lustify/Lustify"}


def _load_template(config: Settings) -> dict[str, Any]:
    if not config.workflow_path.is_file():
        raise GenerationError("workflow_missing", "The Krea 2 workflow file is not configured on this server.")
    try:
        return json.loads(config.workflow_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationError("workflow_invalid", "The configured Krea 2 workflow could not be loaded.") from exc


def build_workflow(config: Settings, request: GenerationRequest, seed: int | None = None) -> tuple[dict[str, Any], int, str]:
    """Returns (workflow, seed, stage). stage is "enhance" when the returned
    workflow is the Qwen prompt-enhancer pre-pass (the image workflow is built
    later by build_qwen_image_workflow once its answer is back), else "render"."""
    if seed is None:
        seed = request.seed if request.seed is not None else random.randint(0, MAX_SEED)
    if request.prompt_enhancer:
        try:
            return qwen.build_enhancer_workflow(config, request, seed), seed, "enhance"
        except qwen.QwenWorkflowError as exc:
            raise GenerationError("workflow_incompatible", str(exc)) from exc
    if request.is_qwen:
        try:
            return qwen.build_image_workflow(request, seed, request.prompt, qwen.resolve_size(request)), seed, "render"
        except qwen.QwenWorkflowError as exc:
            raise GenerationError("workflow_incompatible", str(exc)) from exc
    if request.model == MODEL_ANIMA:
        return anima.build_workflow(request, seed), seed, "render"
    if request.model == MODEL_MOSS:
        try:
            return moss.build_workflow(request, seed, config.moss_clip_name, config.moss_vae_name,
                                       config.moss_workflow_path, config.moss_fast_workflow_path), seed, "render"
        except ValueError as exc:
            raise GenerationError("workflow_incompatible", str(exc)) from exc
    return _build_krea_workflow(config, request, seed), seed, "render"


def batch_seed(seed: int, index: int) -> int:
    """Image `index` of a multi-image generation uses seed + index (wrapping at the max)."""
    return (seed + index) % (MAX_SEED + 1)


def build_workflows(config: Settings, request: GenerationRequest) -> tuple[list[dict[str, Any]], int, str]:
    """Like build_workflow, but one workflow per requested image (seeds seed,
    seed+1, ...). The prompt-enhancer stage is a single workflow; its images are
    built after it answers (build_qwen_image_workflows)."""
    workflow, seed, stage = build_workflow(config, request)
    if stage == "enhance" or request.count == 1:
        return [workflow], seed, stage
    return [workflow] + [build_workflow(config, request, batch_seed(seed, index))[0] for index in range(1, request.count)], seed, stage


def build_qwen_image_workflows(request: GenerationRequest, seed: int, enhancer_text: str) -> tuple[list[dict[str, Any]], str, tuple[int, int] | None]:
    workflow, prompt, size = build_qwen_image_workflow(request, seed, enhancer_text)
    workflows = [workflow] + [build_qwen_image_workflow(request, batch_seed(seed, index), enhancer_text)[0] for index in range(1, request.count)]
    return workflows, prompt, size


def build_qwen_image_workflow(request: GenerationRequest, seed: int, enhancer_text: str) -> tuple[dict[str, Any], str, tuple[int, int] | None]:
    """Second pass after the prompt enhancer. Returns (workflow, final prompt,
    output size or None when it follows the first reference)."""
    try:
        enhanced = qwen.parse_enhancer_output(enhancer_text)
    except qwen.QwenWorkflowError as exc:
        raise GenerationError("enhancer_failed", str(exc)) from exc
    size = qwen.resolve_size(request, enhanced)
    workflow = qwen.build_image_workflow(request, seed, enhanced.prompt, size)
    return workflow, enhanced.prompt, size or qwen.reference_output_size(request)


def build_enhanced_workflows(config: Settings, request: GenerationRequest, seed: int, enhancer_text: str) -> tuple[list[dict[str, Any]], str, tuple[int, int]]:
    """Build the render pass after Qwen PE for any selectable image model."""
    if request.is_qwen:
        workflows, prompt, size = build_qwen_image_workflows(request, seed, enhancer_text)
        resolved = size or qwen.reference_output_size(request) or request.normalized_size()
        return workflows, prompt, resolved
    try:
        enhanced = qwen.parse_enhancer_output(enhancer_text)
    except qwen.QwenWorkflowError as exc:
        raise GenerationError("enhancer_failed", str(exc)) from exc
    width, height = qwen.resolve_krea_size(request, enhanced)
    render_request = request.model_copy(update={
        "prompt": enhanced.prompt,
        "width": width,
        "height": height,
        "prompt_enhancer": False,
    })
    if request.model == MODEL_ANIMA:
        builder = lambda current_seed: anima.build_workflow(render_request, current_seed)
    elif request.model == MODEL_MOSS:
        builder = lambda current_seed: moss.build_workflow(render_request, current_seed, config.moss_clip_name,
                                                           config.moss_vae_name, config.moss_workflow_path,
                                                           config.moss_fast_workflow_path)
    else:
        builder = lambda current_seed: _build_krea_workflow(config, render_request, current_seed)
    workflows = [builder(batch_seed(seed, index)) for index in range(request.count)]
    return workflows, enhanced.prompt, (width, height)


def display_size(request: GenerationRequest) -> tuple[int, int]:
    """Best-known output size for the job card before any enhancer pass."""
    if request.is_qwen and not request.prompt_enhancer:
        return qwen.resolve_size(request) or qwen.reference_output_size(request) or request.normalized_size()
    return request.normalized_size()


def _build_krea_workflow(config: Settings, request: GenerationRequest, seed: int) -> dict[str, Any]:
    """Deep-copy the Krea2-Advanced-LoRA template and inject the per-request
    fields documented in BunnyBytes/workflows/NOTES.md's node-id map. Reference
    slots and LoRA slots that the request leaves unspecified keep the
    template's own defaults (disabled) rather than being touched."""
    template = _load_template(config)
    workflow = copy.deepcopy(template)

    width, height = request.normalized_size()

    try:
        workflow[_UNET_NODE] = {**workflow[_UNET_NODE], **loader_node(request.checkpoint or template[_UNET_NODE]["inputs"]["unet_name"])}
        workflow[_PROMPT_NODE]["inputs"]["text"] = request.prompt
        workflow[_SIZE_NODE]["inputs"]["width"] = width
        workflow[_SIZE_NODE]["inputs"]["height"] = height
        workflow[_SAMPLER_NODE]["inputs"]["seed"] = seed
        workflow[_ENHANCE_TOGGLE_NODE]["inputs"]["value"] = request.enhance
        workflow[_SAVE_NODE]["inputs"]["filename_prefix"] = f"{_SAVE_FOLDERS[request.model]}_{time.strftime('%Y%m%d-%H%M%S')}"

        loader_inputs = workflow[_LORA_LOADER_NODE]["inputs"]
        # Turbo distillation LoRA: an extra loader row after every slot, fewer steps.
        workflow[_SAMPLER_NODE]["inputs"]["steps"] = STANDARD_STEPS
        if request.speed in TURBO_LORAS:
            lora_name, steps = TURBO_LORAS[request.speed]
            loader_inputs[f"lora_{len(LORA_SLOTS) + 1}"] = {
                "on": True, "lora": lora_name, "strength": request.turbo_strength, "strengthTwo": None,
            }
            workflow[_SAMPLER_NODE]["inputs"]["steps"] = steps
        if request.krea_steps:
            workflow[_SAMPLER_NODE]["inputs"]["steps"] = request.krea_steps
        for index, slot in enumerate(request.loras):
            # The template has rows for the first 4 slots only; later slots get
            # their own row. The filename always comes from LORA_SLOTS.
            loader_inputs[f"lora_{index + 1}"] = {
                "on": slot.enabled, "lora": LORA_SLOTS[index], "strength": slot.strength, "strengthTwo": None,
            }
        # Any LoRA slot the request didn't mention keeps the template's own
        # default (see LORA_SLOTS / DEFAULT_LORA_ENABLED in models.py).

        _apply_krea_references(workflow, request)
    except (KeyError, TypeError) as exc:
        raise GenerationError("workflow_incompatible", "The configured workflow is missing an expected Krea 2 node.") from exc

    return workflow


def _apply_krea_references(workflow: dict[str, Any], request: GenerationRequest) -> None:
    for node_id in _TEMPLATE_REFERENCE_NODES:
        workflow.pop(node_id, None)
    references = request.active_references
    sampler = workflow[_SAMPLER_NODE]["inputs"]
    if not references:
        sampler["positive"] = [_PROMPT_NODE, 0]
        return
    # The style-reference LoRA carries the reference strength: the strongest
    # enabled slot's value (the LoRA is one weight for all references).
    workflow[_REF_LORA_NODE] = {"class_type": "LoraLoaderModelOnly", "inputs": {
        "model": [_LORA_LOADER_NODE, 0], "lora_name": KREA_STYLE_REFERENCE_LORA,
        "strength_model": max(ref.strength for ref in references),
    }}
    encode_inputs: dict[str, Any] = {"clip": [_LORA_LOADER_NODE, 1], "prompt": request.prompt, "vae": ["3", 0]}
    for index, (ref, node_id) in enumerate(zip(references, _REF_IMAGE_NODES), start=1):
        workflow[node_id] = {"class_type": "LoadImage", "inputs": {"image": ref.image}}
        encode_inputs[f"image{index}"] = [node_id, 0]
    workflow[_REF_ENCODE_NODE] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": encode_inputs}
    workflow[_REF_METHOD_NODE] = {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {
        "conditioning": [_REF_ENCODE_NODE, 0], "reference_latents_method": "index_timestep_zero",
    }}
    # The official reference graph scales the sampling shift with the output
    # size (Krea 2's built-in shift only matches it at 1024x1024).
    width, height = request.normalized_size()
    workflow[_REF_SAMPLING_NODE] = {"class_type": "ModelSamplingFlux", "inputs": {
        "model": [_REF_LORA_NODE, 0], "max_shift": 1.15, "base_shift": 0.5, "width": width, "height": height,
    }}
    sampler["model"] = [_REF_SAMPLING_NODE, 0]
    sampler["positive"] = [_REF_METHOD_NODE, 0]
    workflow[_NEGATIVE_NODE]["inputs"]["conditioning"] = [_REF_METHOD_NODE, 0]


__all__ = [
    "GenerationError",
    "build_workflow",
    "build_qwen_image_workflow",
    "build_qwen_image_workflows",
    "build_enhanced_workflows",
    "build_workflows",
    "batch_seed",
    "display_size",
    "LORA_SLOTS",
    "DEFAULT_LORA_ENABLED",
    "REFERENCE_SLOT_COUNT",
    "DEFAULT_REFERENCE_STRENGTHS",
]
