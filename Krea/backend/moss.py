"""MOSS-Image INT8 text-to-image graph based on its ComfyUI API workflow."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .models import GenerationRequest, MOSS_FAST_CHECKPOINT, MOSS_FAST_MODES
from .lora_graph import model_with_loras


def build_workflow(request: GenerationRequest, seed: int, clip_name: str, vae_name: str,
                   template_path: Path, fast_template_path: Path) -> dict[str, Any]:
    fast = request.speed in MOSS_FAST_MODES
    path = fast_template_path if fast else template_path
    try:
        workflow = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"The MOSS-Image workflow could not be loaded from {path}.") from exc
    width, height = request.normalized_size()
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
        workflow["18"]["inputs"]["filename_prefix"] = f"MOSS/{request.speed}_{time.strftime('%Y%m%d-%H%M%S')}"
    except (KeyError, TypeError) as exc:
        raise ValueError("The MOSS-Image workflow is missing a required node or input.") from exc
    return workflow
