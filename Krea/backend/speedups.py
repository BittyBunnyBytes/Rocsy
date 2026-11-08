"""Speedup & Saves: per-job text-encoder cache switch and attention backend.

Attention: "auto" = the backend measured fastest for that model (ATTENTION_BEST, see the
benchmark notes below); "flash" is ComfyUI's process default (--use-flash-attention), so it
adds no node and the graph stays exactly as before. Any other choice puts a
BunnyAttentionBackend (comfy_nodes/bunny_cond_cache) in front of every sampler/guider model input.
"""
from __future__ import annotations

from typing import Any

ATTENTION_NODE = "BunnyAttentionBackend"
# UI value -> name registered in comfy.ldm.modules.attention (None = process default, no node)
ATTENTION_BACKENDS: dict[str, str | None] = {
    "flash": None,
    "pytorch": "pytorch",
    "sage": "sage",
    "kitchen": "comfy_kitchen_int8",
}
# Fastest backend per model (Krea webapp model ids); anything missing = process default.
# Benchmark 2026-10-31, XTX, 1024², seed 777, median s/step (2nd round; pixel check vs PyTorch):
#   Krea 2 int8   flash 1.52  kitchen 1.51  pytorch 1.82  sage 1.97 (sage: different composition)
#   Lustify fp8   flash 1.55  kitchen 1.55  pytorch 1.91  sage 1.98
#   Qwen Turbo    flash 0.86  kitchen 0.84  pytorch 1.14  sage 1.13
#   MOSS sft int8 all 1.48, pixel-identical: MOSS's own attention ignores the override.
# Kitchen ties Flash (within noise) but changes pixels, so Flash stays "auto". Anima untested.
ATTENTION_BEST: dict[str, str] = {"krea2": "flash", "lustify": "flash", "qwen_image_21": "flash"}
# Models whose attention can't be switched per job (the patch is skipped for them).
ATTENTION_FIXED: dict[str, str] = {"moss": "MOSS runs its own attention; this setting has no effect (measured)."}
SAMPLER_CLASSES = {"KSampler", "KSamplerAdvanced", "BasicGuider", "CFGGuider", "SamplerCustom", "DualCFGGuider"}


def resolve_attention(model: str | None, choice: str | None) -> str:
    if model in ATTENTION_FIXED:
        return "flash"
    choice = choice or "auto"
    if choice == "auto":
        return ATTENTION_BEST.get(model or "", "flash")
    return choice if choice in ATTENTION_BACKENDS else "flash"


def apply_attention(workflow: dict[str, Any], backend_name: str) -> bool:
    """Insert one attention patch per distinct model link feeding a sampler. True if changed."""
    registered = ATTENTION_BACKENDS.get(backend_name)
    if registered is None:
        return False
    patched: dict[tuple[str, int], str] = {}
    next_id = max((int(k) for k in workflow if str(k).isdigit()), default=0) + 1000
    for node in list(workflow.values()):
        if node.get("class_type") not in SAMPLER_CLASSES:
            continue
        link = node.get("inputs", {}).get("model")
        if not (isinstance(link, list) and len(link) == 2):
            continue
        key = (str(link[0]), int(link[1]))
        if key not in patched:
            node_id = str(next_id)
            next_id += 1
            workflow[node_id] = {"class_type": ATTENTION_NODE,
                                 "inputs": {"model": [key[0], key[1]], "attention": registered},
                                 "_meta": {"title": f"Attention: {backend_name}"}}
            patched[key] = node_id
        node["inputs"]["model"] = [patched[key], 0]
    return bool(patched)
