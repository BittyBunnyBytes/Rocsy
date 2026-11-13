"""MiniMax H3 Image graphs (ComfyUI's native support, official templates image_minimax_h3_image_t2i /
image_minimax_h3_image_edit): UNETLoader -> KSampler (6 steps, CFG 1, euler / simple, both fixed by the
distilled checkpoint) with MiniMaxH3ImageConditioning for the positive and a zeroed copy for the negative.

With references on, each picture goes to the conditioning node as images.image_N; the prompt names
them <Picture 1>, <Picture 2>, ... and image_1 is the picture being edited. With the image VAE
connected they are also anchored as reference latents. Width/height 0 lets the node size the output
from image_1 (short edge 1024..2048) - "Output follows Reference 1".
The template's Qwen3.5 prompt expander is left out (Image has its own prompt enhancer)."""
from __future__ import annotations

import time
from typing import Any

from .checkpoints import loader_node
from .lora_graph import model_with_loras
from .models import H3_IMAGE_CFG, H3_IMAGE_STEPS, GenerationRequest

# MiniMaxH3ImageConditioning's own sizing (comfy_extras/nodes_minimax_h3.py), for the job card.
_CANVAS = 32
_REF_SHORT_EDGE = (1024, 2048)


def _follow_size(width: int, height: int) -> tuple[int, int]:
    low, high = _REF_SHORT_EDGE
    scale = min(max(min(width, height), low), high) / min(width, height)
    return tuple(max(_CANVAS, round(side * scale / _CANVAS) * _CANVAS) for side in (width, height))  # type: ignore[return-value]


def follows_reference(request: GenerationRequest) -> bool:
    return bool(request.active_references) and request.match_reference_size


def output_size(request: GenerationRequest) -> tuple[int, int]:
    references = request.active_references
    if follows_reference(request) and references[0].width and references[0].height:
        return _follow_size(references[0].width, references[0].height)
    return request.normalized_size()


def build_workflow(request: GenerationRequest, seed: int, clip_name: str, vae_name: str) -> dict[str, Any]:
    if not request.checkpoint:
        raise ValueError("No MiniMax H3 Image checkpoint selected.")
    width, height = (0, 0) if follows_reference(request) else request.normalized_size()
    graph: dict[str, Any] = {
        "1": loader_node(request.checkpoint),
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip_name, "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
        "5": {"class_type": "MiniMaxH3ImageConditioning", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "prompt": request.prompt, "width": width, "height": height}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["5", 0], "negative": ["6", 0], "latent_image": ["5", 1],
            "seed": seed, "steps": H3_IMAGE_STEPS, "cfg": H3_IMAGE_CFG, "sampler_name": "euler",
            "scheduler": "simple", "denoise": 1.0}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
    }
    for index, ref in enumerate(request.active_references, start=1):
        node_id = str(20 + index)
        graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": ref.image}}
        graph["5"]["inputs"][f"images.image_{index}"] = [node_id, 0]
    graph["7"]["inputs"]["model"] = model_with_loras(graph, "1", request)
    graph["9"] = {"class_type": "SaveImage", "inputs": {
        "images": ["8", 0], "filename_prefix": f"H3Image/H3Image_{time.strftime('%Y%m%d-%H%M%S')}"}}
    return graph
