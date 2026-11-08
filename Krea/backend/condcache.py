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


def wrap(workflow: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(workflow)
    for node in out.values():
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
        new_inputs.update(encoder=encoder, params=json.dumps(params, sort_keys=True))
        node["class_type"] = NODE
        node["inputs"] = new_inputs
    return out


def needs_node(workflow: dict[str, Any]) -> bool:
    return ENABLED and any(n.get("class_type") in ENCODERS for n in workflow.values())
