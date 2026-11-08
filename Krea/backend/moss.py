"""MOSS-Image INT8 text-to-image graph based on its ComfyUI API workflow.
With Reference 1 attached it becomes img2img: the image is VAE-encoded in
place of the empty latent and only the last moss_denoise of the schedule runs."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .models import GenerationRequest, MOSS_FAST_CHECKPOINT, MOSS_FAST_MODES
from .lora_graph import model_with_loras
from .qwen import krea_size_for_ratio

# Img2img nodes added to both templates (ids unused by either).
_SOURCE_NODE = "30"
_SCALE_NODE = "31"
_ENCODE_NODE = "32"
_SPLIT_NODE = "33"


def output_size(request: GenerationRequest) -> tuple[int, int]:
    """width/height, reshaped to the source image's ratio (same pixel budget)
    when img2img follows Reference 1."""
    width, height = request.normalized_size()
    references = request.active_references
    if references and request.match_reference_size and references[0].width and references[0].height:
        return krea_size_for_ratio(references[0].width, references[0].height, width * height)
    return width, height


def build_workflow(request: GenerationRequest, seed: int, clip_name: str, vae_name: str,
                   template_path: Path, fast_template_path: Path) -> dict[str, Any]:
    fast = request.speed in MOSS_FAST_MODES
    path = fast_template_path if fast else template_path
    try:
        workflow = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"The MOSS-Image workflow could not be loaded from {path}.") from exc
    width, height = output_size(request)
    references = request.active_references
    try:
        workflow["1"]["inputs"]["unet_name"] = MOSS_FAST_CHECKPOINT if fast else request.checkpoint
        workflow["2"]["inputs"]["clip_name"] = clip_name
        workflow["3"]["inputs"]["vae_name"] = vae_name
        workflow["4"]["inputs"]["text"] = request.prompt
        workflow["6"]["inputs"].update(width=width, height=height)
        model = model_with_loras(workflow, "1", request)
        if fast:
            adapter, steps = MOSS_FAST_MODES[request.speed]
            workflow["20"]["inputs"].update(model=model, adapter=adapter, steps=steps)
            workflow["22"]["inputs"]["noise_seed"] = seed
        else:
            workflow["5"]["inputs"]["text"] = request.negative_prompt
            workflow["16"]["inputs"].update(seed=seed, steps=request.moss_steps, cfg=request.moss_cfg)
            workflow["15"]["inputs"]["model"] = model
        if references:
            _apply_source_image(workflow, references[0].image, width, height, request.moss_denoise,
                                MOSS_FAST_MODES[request.speed][1] if fast else None)
        workflow["18"]["inputs"]["filename_prefix"] = f"MOSS/{request.speed}_{time.strftime('%Y%m%d-%H%M%S')}"
    except (KeyError, TypeError) as exc:
        raise ValueError("The MOSS-Image workflow is missing a required node or input.") from exc
    return workflow


def _apply_source_image(workflow: dict[str, Any], image: str, width: int, height: int,
                        denoise: float, fast_steps: int | None) -> None:
    workflow.pop("6")
    workflow[_SOURCE_NODE] = {"class_type": "LoadImage", "inputs": {"image": image}}
    workflow[_SCALE_NODE] = {"class_type": "ImageScale", "inputs": {
        "image": [_SOURCE_NODE, 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}}
    workflow[_ENCODE_NODE] = {"class_type": "VAEEncode", "inputs": {"pixels": [_SCALE_NODE, 0], "vae": ["3", 0]}}
    if fast_steps is None:
        workflow["16"]["inputs"].update(latent_image=[_ENCODE_NODE, 0], denoise=denoise)
        return
    # The few-step adapter makes its own sigmas; keep the tail of them. At least
    # one step, or SplitSigmasDenoise returns an empty schedule.
    denoise = max(denoise, 1 / fast_steps)
    workflow[_SPLIT_NODE] = {"class_type": "SplitSigmasDenoise", "inputs": {"sigmas": ["20", 2], "denoise": denoise}}
    workflow["23"]["inputs"].update(sigmas=[_SPLIT_NODE, 1], latent_image=[_ENCODE_NODE, 0])
