"""Route text encoding through ComfyUI's BunnyCachedEncode node (comfy_nodes/bunny_cond_cache).

At submit time every stock encoder node (CLIPTextEncode, TextEncodeQwenImage21,
TextEncodeQwenImageEditPlus) becomes a BunnyCachedEncode with the same node id, so outputs/links,
progress events and stats stay unchanged. Encoders whose text comes from another node aren't touched.
Only used while the running ComfyUI actually has the node; KREA_COND_CACHE=0 turns it off.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

NODE = "BunnyCachedEncode"
ENCODERS = {
    "CLIPTextEncode": ("text",),
    "TextEncodeQwenImage21": ("prompt", "negative_prompt", "resolution"),
    "TextEncodeQwenImageEditPlus": ("prompt",),
}
ENABLED = os.getenv("KREA_COND_CACHE", "1") != "0"


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)


def _image_slot(encoder: str, name: str) -> str | None:
    if encoder == "TextEncodeQwenImage21" and name.startswith("images.image_"):
        return "image_" + name.rsplit("_", 1)[-1]
    if encoder == "TextEncodeQwenImageEditPlus" and name in ("image1", "image2", "image3"):
        return "image_" + name[-1]
    return None


# Nodes a model may pass through without changing how CFG treats the negative (anything else, e.g.
# SAG/PerpNeg/CFG++ patches, can turn ComfyUI's cfg-1 shortcut off, so the negative must stay real).
_PLAIN_MODEL_NODES = {"UNETLoader", "UnetLoaderGGUF", "CheckpointLoaderSimple", "LoraLoaderModelOnly",
                      "LoraLoader", "QwenImage21Cache", "BunnyAttentionBackend", "ModelSamplingAuraFlow"}


def _plain_model(workflow: dict[str, Any], link: Any, depth: int = 0) -> bool:
    if not _is_link(link) or depth > 32:
        return False
    node = workflow.get(str(link[0])) or {}
    if node.get("class_type") not in _PLAIN_MODEL_NODES:
        return False
    upstream = (node.get("inputs") or {}).get("model")
    return upstream is None or _plain_model(workflow, upstream, depth + 1)


def negative_unused(workflow: dict[str, Any], node_id: str) -> bool:
    """True when the encoder's negative output (slot 1) changes nothing: no node reads it, or only
    plain KSamplers at CFG 1 do (ComfyUI then never evaluates the uncond)."""
    for node in workflow.values():
        for name, value in (node.get("inputs") or {}).items():
            if not (_is_link(value) and str(value[0]) == str(node_id) and value[1] == 1):
                continue
            inputs = node.get("inputs") or {}
            cfg = inputs.get("cfg")
            if not (node.get("class_type") in ("KSampler", "KSamplerAdvanced") and name == "negative"
                    and isinstance(cfg, (int, float)) and float(cfg) == 1.0
                    and "cfg_pp" not in str(inputs.get("sampler_name", ""))
                    and _plain_model(workflow, inputs.get("model"))):
                return False
    return True


def wrap(workflow: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(workflow)
    for node_id, node in out.items():
        encoder = node.get("class_type")
        if encoder not in ENCODERS:
            continue
        inputs = node.get("inputs") or {}
        params, new_inputs, ok = {}, {}, True
        for name, value in inputs.items():
            if name in ("clip", "vae"):
                new_inputs[name] = value
            elif (slot := _image_slot(encoder, name)) is not None:
                new_inputs[slot] = value
            elif name in ENCODERS[encoder] and not _is_link(value):
                params[name] = value
            else:
                ok = False   # linked text or an input we don't know: keep the stock node
                break
        if not ok or not all(p in params for p in ENCODERS[encoder] if p != "resolution"):
            continue
        if encoder == "TextEncodeQwenImage21" and negative_unused(workflow, node_id):
            params["negative_unused"] = True
        new_inputs.update(encoder=encoder, params=json.dumps(params, sort_keys=True))
        node["class_type"] = NODE
        node["inputs"] = new_inputs
    return out


def needs_node(workflow: dict[str, Any]) -> bool:
    return ENABLED and any(n.get("class_type") in ENCODERS for n in workflow.values())
