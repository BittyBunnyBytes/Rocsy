"""ComfyUI API graphs for the Anima checkpoints installed on this host."""
from __future__ import annotations

import time
from typing import Any

from .models import ANIMA_VERSIONS, GenerationRequest
from .lora_graph import model_with_loras


def build_workflow(request: GenerationRequest, seed: int) -> dict[str, Any]:
    name = ANIMA_VERSIONS[request.anima_version][0]
    width, height = request.normalized_size()
    vae = {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}}
    clip = {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_06b_base.safetensors", "type": "stable_diffusion", "device": "default"}}
    if request.anima_version == "v38":
        # The installed 3.8B model has its own loader and dual-encoder prompt nodes.
        graph = {
            "1": {"class_type": "Anima38BV2Loader", "inputs": {"model_name": name}},
            "2": clip,
            "3": {"class_type": "AnimaQwen35Loader", "inputs": {"qwen35_model": "qwen35_4b.safetensors"}},
            "4": {"class_type": "Anima38BV2Prompt", "inputs": {"model": ["1", 0], "native_clip": ["2", 0], "qwen35_clip": ["3", 0], "prompt": request.prompt}},
            "5": {"class_type": "Anima38BV2Prompt", "inputs": {"model": ["1", 0], "native_clip": ["2", 0], "qwen35_clip": ["3", 0], "prompt": request.negative_prompt}},
            "6": {"class_type": "CFGGuider", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "cfg": request.anima_cfg}},
            "7": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
            "8": {"class_type": "BasicScheduler", "inputs": {"model": ["1", 0], "scheduler": "beta", "steps": request.anima_steps or 36, "denoise": 1.0}},
            "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
            "10": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
            "11": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["9", 0], "guider": ["6", 0], "sampler": ["7", 0], "sigmas": ["8", 0], "latent_image": ["10", 0]}},
            "12": vae,
            "13": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 1], "vae": ["12", 0]}},
        }
        image = ["13", 0]
    else:
        steps = request.anima_steps or (8 if request.anima_version == "turbo" else 30)
        graph = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": name, "weight_dtype": "default"}},
            "2": clip,
            "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": request.prompt}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": request.negative_prompt}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
            "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["3", 0], "negative": ["4", 0], "latent_image": ["5", 0], "seed": seed, "steps": steps, "cfg": request.anima_cfg, "sampler_name": "er_sde", "scheduler": "simple", "denoise": 1.0}},
            "7": vae,
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["7", 0]}},
        }
        image = ["8", 0]
    model_output = model_with_loras(graph, "1", request)
    for node in graph.values():
        if node["class_type"] in {"Anima38BV2Prompt", "CFGGuider", "BasicScheduler", "KSampler"}:
            if node["inputs"].get("model") == ["1", 0]:
                node["inputs"]["model"] = model_output
    if request.enhance:
        graph["20"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": "RealESRGAN_x4plus.pth"}}
        graph["21"] = {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["20", 0], "image": image}}
        image = ["21", 0]
    graph["30"] = {"class_type": "SaveImage", "inputs": {"images": image, "filename_prefix": f"Anima/{request.anima_version}_{time.strftime('%Y%m%d-%H%M%S')}"}}
    return graph
