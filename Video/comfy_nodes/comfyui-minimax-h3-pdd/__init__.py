"""MiniMax H3 PDD LoRA loader (H3 Machine, 2026-09-24).

Loads Alibaba PAI's Parallel Decoding Distillation acceleration LoRAs
(huggingface.co/alibaba-pai/MiniMax-H3-Acc-LoRAs) straight from their
original Diffusers-format file onto ComfyUI's native MiniMax H3 model:

* Backbone LoRA: Diffusers names are mapped to ComfyUI's module names, and
  the separate to_q/to_k/to_v LoRAs are fused exactly into one LoRA on the
  fused qkv_proj (stacked downs, block-diagonal ups, alpha scaled so the
  effective alpha/rank stays the same).
* Output heads: PDD replaces each final head with a bank of N per-interval
  heads. ComfyUI's MiniMax FinalLayer already implements the bank
  (comfy/ldm/minimax/model.py, _pdd_head) when the head's weight holds N
  row blocks, with block 0 a full head and blocks 1..N-1 offsets from it.
  The absolute heads in the file are converted to that form and swapped in
  with an object patch.

Sample with the PDD contract: 8 evaluations (N=32 / block 4), Euler, CFG 1,
video shift 12, audio shift 3, sigmas = shift(12, linspace(1, 0, 9)).
"""
import copy
import logging
import re

import torch

import comfy.sd
import comfy.utils
import folder_paths

log = logging.getLogger("minimax-h3-pdd")

_BLOCK_MAP = (
    (re.compile(r"^transformer_blocks\.(\d+)\.attn\.to_out\.0$"), "blocks.{}.attn.out_proj"),
    (re.compile(r"^transformer_blocks\.(\d+)\.ff\.net\.0\.proj$"), "blocks.{}.mlp.fc1"),
    (re.compile(r"^transformer_blocks\.(\d+)\.ff\.net\.2$"), "blocks.{}.mlp.fc2"),
    (re.compile(r"^transformer_blocks\.(\d+)\.adaln_proj\.linear$"), "blocks.{}.adaln_proj.linear"),
    (re.compile(r"^token_refiner\.refiner_blocks\.(\d+)\.attn\.to_out\.0$"), "token_refiner.blocks.{}.attn.out_proj"),
    (re.compile(r"^token_refiner\.refiner_blocks\.(\d+)\.ff\.net\.0\.proj$"), "token_refiner.blocks.{}.mlp.fc1"),
    (re.compile(r"^token_refiner\.refiner_blocks\.(\d+)\.ff\.net\.2$"), "token_refiner.blocks.{}.mlp.fc2"),
)
_QKV = re.compile(r"^(transformer_blocks|token_refiner\.refiner_blocks)\.(\d+)\.attn\.to_([qkv])$")
_HEADS = {"proj_out": "video_out", "audio_proj_out": "audio_out"}


def convert_lora(state, alpha, rank):
    """Diffusers PDD LoRA pairs -> ComfyUI LoRA dict (lora_down/lora_up/alpha)."""
    pairs = {}
    for key, tensor in state.items():
        match = re.match(r"^(.*)\.lora_(down|up)(?:\.weight)?$", key)
        if match:
            pairs.setdefault(match.group(1), {})[match.group(2)] = tensor
    out, qkv, unmapped = {}, {}, []
    for base, pair in pairs.items():
        m = _QKV.match(base)
        if m:
            prefix = "blocks" if m.group(1) == "transformer_blocks" else "token_refiner.blocks"
            qkv.setdefault(f"{prefix}.{m.group(2)}.attn.qkv_proj", {})[m.group(3)] = pair
            continue
        for pattern, template in _BLOCK_MAP:
            m = pattern.match(base)
            if m:
                name = template.format(m.group(1))
                out[f"{name}.lora_down.weight"] = pair["down"]
                out[f"{name}.lora_up.weight"] = pair["up"]
                out[f"{name}.alpha"] = torch.tensor(alpha * pair["down"].shape[0] / rank)
                break
        else:
            unmapped.append(base)
    for name, parts in qkv.items():
        if set(parts) != {"q", "k", "v"}:
            raise ValueError(f"PDD LoRA has an incomplete q/k/v set for {name}: {sorted(parts)}")
        downs = [parts[x]["down"] for x in "qkv"]
        ups = [parts[x]["up"] for x in "qkv"]
        down = torch.cat(downs, dim=0)
        up = torch.zeros(sum(u.shape[0] for u in ups), down.shape[0], dtype=ups[0].dtype)
        row = col = 0
        for d, u in zip(downs, ups):
            up[row:row + u.shape[0], col:col + d.shape[0]] = u
            row += u.shape[0]
            col += d.shape[0]
        out[f"{name}.lora_down.weight"] = down
        out[f"{name}.lora_up.weight"] = up
        out[f"{name}.alpha"] = torch.tensor(alpha * down.shape[0] / rank)
    return out, unmapped


def head_bank(weight, bias):
    """[N, out, in] absolute heads -> ComfyUI bank: block 0 full, later blocks offsets."""
    w = weight.float()
    b = bias.float()
    w_bank = torch.cat([w[:1], w[1:] - w[:1]], dim=0).reshape(-1, w.shape[-1])
    b_bank = torch.cat([b[:1], b[1:] - b[:1]], dim=0).reshape(-1)
    return w_bank, b_bank


class MiniMaxH3PDDLoader:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": ("MODEL",),
            "lora_name": (folder_paths.get_filename_list("loras"),),
            "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05,
                                   "tooltip": "Backbone LoRA strength. The PDD head bank always applies in full."}),
        }}

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "load"
    CATEGORY = "model/patch/minimax"
    DESCRIPTION = "Alibaba PAI PDD 8-step acceleration LoRA for MiniMax H3 (backbone LoRA + per-interval output head bank)."

    def load(self, model, lora_name, strength):
        path = folder_paths.get_full_path_or_raise("loras", lora_name)
        state, meta = comfy.utils.load_torch_file(path, safe_load=True, return_metadata=True)
        meta = meta or {}
        rank = float(meta.get("lora_rank", 64))
        alpha = float(meta.get("lora_alpha", rank))
        num_steps = int(meta.get("pdd_num_steps", 32))
        if not all(k in state for k in ("proj_out.weight", "proj_out.bias", "audio_proj_out.weight", "audio_proj_out.bias")):
            raise ValueError(f"{lora_name} has no PDD head bank (proj_out / audio_proj_out) - not a PDD LoRA.")

        lora, unmapped = convert_lora(state, alpha, rank)
        if unmapped:
            raise ValueError(f"PDD LoRA has modules this loader can't map: {unmapped[:5]}")
        patched, _ = comfy.sd.load_lora_for_models(model, None, lora, strength, 0)

        diffusion_model = patched.get_model_object("diffusion_model")
        for src, dst in _HEADS.items():
            original = getattr(diffusion_model.final_layer, dst)
            weight, bias = state[f"{src}.weight"], state[f"{src}.bias"]
            if weight.shape[0] != num_steps or weight.shape[1] != original.out_features or weight.shape[2] != original.in_features:
                raise ValueError(f"PDD {src} bank {list(weight.shape)} doesn't match {dst} "
                                 f"({original.out_features}x{original.in_features}, N={num_steps}).")
            w_bank, b_bank = head_bank(weight, bias)
            head = copy.copy(original)
            head._parameters = dict(original._parameters)
            head.weight = torch.nn.Parameter(w_bank, requires_grad=False)
            head.bias = torch.nn.Parameter(b_bank, requires_grad=False)
            patched.add_object_patch(f"diffusion_model.final_layer.{dst}", head)
        log.warning("[PDD] %s: %d LoRA modules (q/k/v fused), %d-interval head bank, strength %.2f",
                    lora_name, len([k for k in lora if k.endswith(".lora_down.weight")]), num_steps, strength)
        return (patched,)


NODE_CLASS_MAPPINGS = {"MiniMaxH3PDDLoader": MiniMaxH3PDDLoader}
NODE_DISPLAY_NAME_MAPPINGS = {"MiniMaxH3PDDLoader": "MiniMax H3 PDD LoRA Loader"}
