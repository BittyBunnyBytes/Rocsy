from __future__ import annotations

import re

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MODES = {"text": 0, "fl2v": 1, "ref2v": 2}
TEST_LORA_RE = re.compile(r"H3MachineCustom/epoch-tests/[0-9a-f]{32}/[A-Za-z0-9_.-]{1,150}\.safetensors")
MODE_LABELS = {"text": "Text", "fl2v": "Keyframe", "ref2v": "Reference"}
MODEL_LABELS = {"minimax_h3": "MiniMax H3", "ltx_2_5": "LTX 2.5", "wan_2_2": "Wan 2.2", "stitch": "Stitch", "postedit": "Post-Edit", "motion": "Motion"}
# Jobs this app renders itself with ffmpeg (no ComfyUI, no GPU lock, no timing/rating).
FFMPEG_MODELS = {"stitch", "postedit"}
LTX_WORKFLOWS = (
    ("final_optimized", "Stable Quality"),
    ("final_optimized_v2", "LTX Final Optimized V2"),
)
# Native-Fast corrupts long (15 second) renders on the current ROCm/ComfyUI
# stack, despite its direct model wiring looking equivalent on paper. V3 is
# derived from that unsafe graph. Keep accepting their persisted request keys,
# but route both to the verified stable workflow so old jobs/settings cannot
# select a known-bad graph.
#
# 2026-09-22: final_optimized (.flash.json) crashed ComfyUI's whole process
# twice in a row with an identical GPU page fault ("Memory access fault by
# GPU node-1 ... Fatal Python error: Aborted"), both times ~2s after loading
# the Stage2 LatentUpsampler model, regardless of resolution/duration/tile
# preset (that node is unconditionally part of the graph - see
# build_ltx_workflow, nothing in GenerationRequest can skip it). Testing
# the plain non-flash file here to see if the fault is specific to the
# flash-attention graph or is a node/driver-level issue that follows either
# file.
LTX_WORKFLOW_FILES = {
    "final_optimized": "LTX-2.5-Final-Optimized.json",
    # 2026-09-23: same graph as final_optimized minus the unused Gemma negative-prompt
    # encode (CFG 1 never reads it; output bit-identical). Pairs with the
    # comfyui-na3d-fast custom node on the ComfyUI side for a faster VAE decode.
    "final_optimized_v2": "LTX-2.5-Final-Optimized-V2.json",
    "native_fast": "LTX-2.5-Final-Optimized.flash.json",
    "v3": "LTX-2.5-Final-Optimized.flash.json",
}
ASPECTS = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1, "4:3": 4 / 3, "3:4": 3 / 4}
SAMPLERS = (
    "euler", "euler_cfg_pp", "euler_ancestral", "euler_ancestral_cfg_pp", "heun", "heunpp2",
    "exp_heun_2_x0", "exp_heun_2_x0_sde", "dpm_2", "dpm_2_ancestral", "lms", "dpm_fast",
    "dpm_adaptive", "dpmpp_2s_ancestral", "dpmpp_2s_ancestral_cfg_pp", "dpmpp_sde",
    "dpmpp_sde_gpu", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_2m_sde_gpu", "dpmpp_2m_sde_heun",
    "dpmpp_2m_sde_heun_gpu", "dpmpp_3m_sde", "dpmpp_3m_sde_gpu", "ddpm", "lcm", "ipndm",
    "ipndm_v", "deis", "res_multistep", "res_multistep_cfg_pp", "res_multistep_ancestral",
    "res_multistep_ancestral_cfg_pp", "gradient_estimation", "gradient_estimation_cfg_pp", "er_sde",
    "seeds_2", "seeds_3", "sa_solver", "sa_solver_pece", "ddim", "uni_pc", "uni_pc_bh2",
)
SCHEDULERS = (
    "simple", "sgm_uniform", "karras", "exponential", "ddim_uniform", "beta", "normal",
    "linear_quadratic", "kl_optimal", "ays", "ays+", "ays_30", "ays_30+", "gits", "beta_1_1",
)
REFERENCE_SIZING = ("match", "max")
UPSCALE_MODELS = (
    "minimax_h3_latent_upscaler_3d_bf16.safetensors",
    "ltx-2.3-spatial-upscaler-x2-1.1.safetensors",
    "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
)

try:
    from . import catalog_local as _local  # this machine's checkpoints/LoRAs (git-ignored)
except ImportError:
    _local = None

# Alternate MiniMax H3 checkpoints available on this host (models/diffusion_models).
# None/omitted keeps whichever checkpoint the workflow template already has
# hardcoded (the pre-existing default for that mode) - only an explicit
# selection here overrides it, so leaving this alone changes nothing.
MINIMAX_H3_CHECKPOINTS = _local.MINIMAX_H3_CHECKPOINTS if _local else (
    ("minimax_h3_fl2va_pruned_int8_convrot_official.safetensors", "MiniMax H3 (Official)"),
    ("minimax_h3_fl2va_pruned_int8_convrot.safetensors", "MiniMax H3 (Standard)"),
)
_MINIMAX_H3_CHECKPOINT_VALUES = {value for value, _label in MINIMAX_H3_CHECKPOINTS}

# MiniMax H3 LoRA slots (DaSiWa_LTX2LoraLoader), by position: requests send enable/strength
# per index, so slots only ever get appended. The checkpoints and LoRA lists come from
# catalog_local.py (this machine's own, git-ignored); a fresh install starts with the official
# checkpoints and no LoRAs, and adds LoRAs in Models or as custom LoRAs. The first slot is on
# by default.
MINIMAX_H3_LORA_SLOTS = _local.MINIMAX_H3_LORA_SLOTS if _local else ()
DEFAULT_MINIMAX_H3_LORA_ENABLED = tuple(index == 0 for index in range(len(MINIMAX_H3_LORA_SLOTS)))
MINIMAX_H3_LORA_SLOTS = list(MINIMAX_H3_LORA_SLOTS)  # custom slots append; existing indices never move

# LoRAs verified (from their safetensors headers) to target the LTX 2.x 22B
# transformer - 48 `transformer_blocks`, 4096-wide attention - so they load on
# the LTX-2.5 distilled INT8 model. Applied through the workflows' own Power
# Lora Loader (rgthree) node 428 (see build_ltx_workflow). Not for this list:
# LoRAs with MiniMax H3 shapes, the LTX 2.3 distilled LoRA (the base model is
# already distilled), and the IC-LoRAs (the 2.5 pixel upscaler, the CQ
# enhancer), which need their own LTXAddVideoICLoRAGuide reference-video wiring.
LTX_LORAS = _local.LTX_LORAS if _local else ()
LTX_LORAS = list(LTX_LORAS)

# Wan 2.2 A14B (Comfy-Org fp8 repackage) LoRAs, applied with one
# LoraLoaderModelOnly per expert (build_wan_workflow). A14B LoRAs mostly come
# as a high-noise + low-noise pair: the list key is the high-noise file and
# LORA_INFO["experts"] names the file for each expert. A key without
# "experts" (a single-file LoRA, or one uploaded to the gallery) goes on
# both experts; an expert left out of "experts" gets nothing from it.
# "base" is what each was trained on: T2V LoRAs still load on the I2V experts
# (same 40 blocks, 5120 wide).
WAN_LORAS = _local.WAN_LORAS if _local else ()
WAN_LORAS = list(WAN_LORAS)
MAX_WAN_LORAS = 4
# Every LoRA list by model family: custom uploads, links, the gallery catalog
# and trigger aliases all look the list up here.
LORA_LISTS: dict[str, list] = {}
CUSTOM_LORA_INFO: dict[str, dict] = {}
GALLERY_LORA_OVERRIDES: dict[str, dict] = {}

# Per-file source page, author-recommended strength, and trigger words, for
# every LoRA list (from catalog_local.py). `strength_note` records what the
# author said; "not stated" means 1.0 is the fallback, not a recommendation.
# First trigger is the main one.
LORA_INFO: dict[str, dict] = _local.LORA_INFO if _local else {}
LORA_LISTS.update(minimax_h3=MINIMAX_H3_LORA_SLOTS, ltx_2_5=LTX_LORAS, wan_2_2=WAN_LORAS)


def wan_lora_experts(filename: str) -> dict[str, str]:
    """{"high": file, "low": file} for one WAN_LORAS key (see WAN_LORAS)."""
    info = {**LORA_INFO.get(filename, {}), **CUSTOM_LORA_INFO.get(filename, {})}
    return dict(info["experts"]) if info.get("experts") else {"high": filename, "low": filename}


def lora_option(filename: str, label: str) -> dict:
    """One LoRA row for /api/settings/options, with LORA_INFO merged in."""
    info = {**LORA_INFO.get(filename, {}), **CUSTOM_LORA_INFO.get(filename, {}), **GALLERY_LORA_OVERRIDES.get(filename, {})}
    return {
        "filename": filename, "label": info.get("label", label), "url": info.get("url"), "group": info.get("group", ""),
        "default_strength": info.get("strength", 1.0), "strength_note": info.get("strength_note", ""),
        "triggers": info.get("triggers", []), "base": info.get("base", ""),
        "custom_id": info.get("custom_id"), "source": info.get("source", ""), "deleted": info.get("deleted", False),
    }


MAX_LTX_LORAS = 4

# ffmpeg's xfade filter (see vf_xfade.c) supports many more named transitions
# than this - curated down to a set that reads well as distinct choices in a
# dropdown. A family with `variants` isn't itself a real xfade transition name
# (there's no bare "wipe") - the frontend always sends one of its variant
# values, defaulting to the first when the user never touches that picker.
# Every leaf value (a variant's, or a family's own `value` when it has none)
# is passed straight through as xfade's `transition=` parameter, so it must
# match ffmpeg's name exactly.
STITCH_TRANSITION_FAMILIES = (
    {"value": "cut", "label": "Hard Cut"},
    {"value": "fade", "label": "Fade", "variant_label": "Style", "variants": (
        ("fade", "Cross Dissolve"), ("fadeblack", "Through Black"), ("fadewhite", "Through White"),
    )},
    {"value": "dissolve", "label": "Dissolve"},
    {"value": "wipe", "label": "Wipe", "variant_label": "Direction", "variants": (
        ("wipeleft", "Left"), ("wiperight", "Right"), ("wipeup", "Up"), ("wipedown", "Down"),
    )},
    {"value": "slide", "label": "Slide", "variant_label": "Direction", "variants": (
        ("slideleft", "Left"), ("slideright", "Right"), ("slideup", "Up"), ("slidedown", "Down"),
    )},
    {"value": "circle", "label": "Circle", "variant_label": "Style", "variants": (
        ("circleopen", "Open"), ("circleclose", "Close"),
    )},
    {"value": "radial", "label": "Radial Wipe"},
    {"value": "pixelize", "label": "Pixelize"},
    {"value": "hblur", "label": "Horizontal Blur"},
    {"value": "zoomin", "label": "Zoom In"},
)
_STITCH_TRANSITION_VALUES = {
    leaf_value
    for family in STITCH_TRANSITION_FAMILIES
    for leaf_value in ([value for value, _label in family["variants"]] if "variants" in family else [family["value"]])
}


class MinimaxLoraSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    strength: float = Field(default=1.0, ge=0.0, le=2.0)


class LtxLora(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lora: str = Field(max_length=255)
    strength: float = Field(default=1.0, ge=0.0, le=2.0)

    @field_validator("lora")
    @classmethod
    def known_lora(cls, value: str) -> str:
        if value not in {filename for filename, _ in LTX_LORAS} or CUSTOM_LORA_INFO.get(value, {}).get("deleted"):
            raise ValueError("Unknown LTX LoRA.")
        return value


class WanLora(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lora: str = Field(max_length=255)
    strength: float = Field(default=1.0, ge=0.0, le=2.0)

    @field_validator("lora")
    @classmethod
    def known_lora(cls, value: str) -> str:
        if value not in {filename for filename, _ in WAN_LORAS} or CUSTOM_LORA_INFO.get(value, {}).get("deleted"):
            raise ValueError("Unknown Wan 2.2 LoRA.")
        return value


# Wan 2.2 renders 16 fps natively (its training rate); 24/32 are made after
# decoding with the same RIFE + every-Nth resample as MiniMax H3's frame rate.
WAN_NATIVE_FPS = 16
WAN_MAX_SECONDS = 10.0  # trained on 81 frames (5 s); past ~10 s it loops and drifts
WAN_MAX_MEGAPIXELS = 1.0  # the 14B experts are trained at 480p and 720p


class H3UpscaleAdvanced(BaseModel):
    """The MiniMax H3 latent upscale's model, time-chunk, tiling and second-pass
    knobs (nodes 81/88/89/87 in generation._apply_h3_latent_upscale). Every
    default is what the app ran before these were exposed, so older jobs,
    presets and ETA keys are unchanged."""
    model_config = ConfigDict(extra="ignore")

    # Models. "model" = the H3 3D upscaler network; "interpolate" = a plain
    # latent resize (MMH3LatentUpscaleParams), no upscaler weights loaded.
    upscaler: Literal["model", "interpolate"] = "model"
    interpolation: Literal["bilinear", "bicubic", "area", "nearest-exact"] = "bilinear"
    upscaler_device: Literal["cuda", "cpu"] = "cuda"
    upscaler_precision: Literal["fp16", "bf16", "fp32"] = "fp16"
    keep_upscaler_resident: bool = False  # stay on the GPU between chunks
    keep_model_resident: bool = False     # don't unload H3 before each chunk's upscale
    unload_model_after: bool = True       # unload H3 before the VAE decode
    upscale_chunks_first: bool = False    # upscale all chunks behind one H3 unload, then load H3 once

    # Temporal chunking (used when upscale_chunking is on). chunk_frames 0 =
    # auto: the longest chunk that fits the upscaler's VRAM budget.
    chunk_frames: int = Field(default=0, ge=0, le=1428)
    chunk_overlap: int = Field(default=17, ge=0, le=1411)
    anchor_strength: float = Field(default=0.999, ge=0.0, le=1.0)

    # Spatial tiling of the second pass (MMH3SpatialSplitParams). "off" samples
    # each chunk whole. Pixel values are multiples of 32.
    tiling: Literal["off", "auto", "rows_cols", "specific_size"] = "off"
    token_budget: int = Field(default=70000, ge=4096, le=1_000_000)
    grid_rows: int = Field(default=2, ge=1, le=9)
    grid_cols: int = Field(default=2, ge=1, le=9)
    tile_width: int = Field(default=768, ge=256, le=4096)
    tile_height: int = Field(default=768, ge=256, le=4096)
    overlap_width: int = Field(default=128, ge=0, le=2048)
    overlap_height: int = Field(default=128, ge=0, le=2048)
    fade_width: int = Field(default=32, ge=0, le=2048)
    fade_height: int = Field(default=32, ge=0, le=2048)
    min_tile_size: int = Field(default=256, ge=0, le=4096)
    overlap_mode: Literal["earlier", "later"] = "earlier"
    overlap_blend: Literal["linear", "smoothstep", "overwrite", "midpoint"] = "linear"
    joint_steps: bool = True

    # Second pass: "" = the same sampler / scheduler as the first pass.
    sampler: str = ""
    scheduler: str = ""

    @model_validator(mode="after")
    def validate_grid(self) -> "H3UpscaleAdvanced":
        if self.chunk_frames and (self.chunk_frames % 17 or self.chunk_frames < 34):
            raise ValueError("Upscale chunk length must be a multiple of 17 frames, at least 34.")
        if self.chunk_overlap % 17:
            raise ValueError("Upscale chunk overlap must be a multiple of 17 frames.")
        if self.chunk_frames and self.chunk_overlap >= self.chunk_frames:
            raise ValueError("Upscale chunk overlap must be shorter than the chunk.")
        for name in ("tile_width", "tile_height", "overlap_width", "overlap_height", "fade_width", "fade_height", "min_tile_size"):
            if getattr(self, name) % 32:
                raise ValueError(f"Upscale {name.replace('_', ' ')} must be a multiple of 32 px.")
        if self.fade_width > self.overlap_width or self.fade_height > self.overlap_height:
            raise ValueError("Upscale tile fade must not be wider than the tile overlap.")
        if self.tiling == "specific_size":
            if self.overlap_width >= self.tile_width or self.overlap_height >= self.tile_height:
                raise ValueError("Upscale tile overlap must be smaller than the tile.")
            if self.min_tile_size > min(self.tile_width, self.tile_height):
                raise ValueError("Upscale minimum tile size must not exceed the tile size.")
        if self.sampler and self.sampler not in SAMPLERS:
            raise ValueError("Unsupported upscale sampler.")
        if self.scheduler and self.scheduler not in SCHEDULERS:
            raise ValueError("Unsupported upscale scheduler.")
        return self


class H3UpscalePreset(BaseModel):
    """The latent-upscale card's settings as one named preset (no seed, no
    render size): see generation.H3_UPSCALE_BUILTIN_PRESETS and /api/upscale-presets."""
    model_config = ConfigDict(extra="ignore")
    upscale_factor: float = Field(default=2.0, ge=1.1, le=4.0)
    upscale_steps: int = Field(default=1, ge=1, le=40)
    upscale_denoise: float = Field(default=0.2, ge=0.05, le=1.0)
    upscale_chunking: bool = True
    upscale_advanced: H3UpscaleAdvanced = Field(default_factory=H3UpscaleAdvanced)


class UpscalePresetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    settings: H3UpscalePreset

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a preset name.")
        return value


class H3MultiGPUPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    multi_gpu_offload: Literal["off", "conditioning", "conditioning_vae", "split", "parallel"] = "off"
    multi_gpu_split_gb: int = Field(default=8, ge=2, le=12)


class MultiGPUPresetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    settings: H3MultiGPUPreset

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a preset name.")
        return value


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Literal["minimax_h3", "ltx_2_5", "wan_2_2"] = "minimax_h3"
    mode: Literal["text", "fl2v", "ref2v"] = "text"
    prompt: str = Field(min_length=1, max_length=20_000)
    reference_ids: list[str] = Field(default_factory=list, max_length=24)
    first_frame_id: str | None = None
    last_frame_id: str | None = None
    megapixels: float = Field(default=0.92, ge=0.004, le=4.2)  # 0.004 = 64x64, the smallest custom size
    width: int | None = Field(default=None, ge=64, le=2048)
    height: int | None = Field(default=None, ge=64, le=2048)
    aspect: Literal["16:9", "9:16", "1:1", "4:3", "3:4"] = "16:9"
    duration: float = Field(default=15, ge=1, le=15)
    steps: int = Field(default=6, ge=1, le=100)
    seed: int = Field(default=20260909, ge=0, le=2_147_483_647)
    sampler: str = "euler"
    scheduler: str = "simple"
    # MiniMax H3 quality settings (minimax_h3 only). cfg > 1 swaps the plain
    # guider for CFGGuider with negative_prompt as the uncond (about 2x the
    # sampling time); cfg == 1 ignores negative_prompt. Capped at 1.5: these
    # H3 checkpoints are guidance-distilled - measured 2026-09-25, CFG 1.75,
    # 2 and 4 (community turbo and official) rendered scenes unrelated to the
    # prompt, and even 1.5 did with the Parasyte turbo LoRA on. A shift of None keeps
    # the mode's own value (8 / 5, or PDD's 12 / 3 in Reference).
    cfg: float = Field(default=1.0, ge=1.0, le=1.5)
    negative_prompt: str = Field(default="", max_length=5_000)
    sigma_shift_video: float | None = Field(default=None, ge=1.0, le=20.0)
    sigma_shift_audio: float | None = Field(default=None, ge=1.0, le=20.0)
    output_quality: Literal["standard", "high", "near_lossless", "hevc_10bit"] = "standard"
    # "Speedup & Saves" (minimax_h3 only). Each *_enabled=False removes that
    # node from the model chain entirely - see build_workflow().
    parasyte_enabled: bool = True
    parasyte_strength: float = Field(default=0.6, ge=0.1, le=1.2)
    sla_enabled: bool = True
    sla_sparsity: float = Field(default=0.9, ge=0.6, le=0.95)
    ref2v_turbo_enabled: bool = True
    # Which Reference turbo LoRA: Alibaba PAI's official PDD 8-step
    # (MiniMax-H3-Ref2VA-Acc-8Step, fixed strength and schedule), or the
    # LightX 8-step. The model default stays "lightx" so older persisted
    # requests replay exactly; the app's own default (options) is "pdd".
    ref2v_turbo: Literal["pdd", "lightx"] = "lightx"
    ref2v_strength: float = Field(default=0.6, ge=0.1, le=1.2)
    # Dense attention backend, in every mode (generation._apply_h3_attention).
    # Measured 2026-09-29 (Fast Stable, 928x512 5 s, whole prompt): flash
    # 102.5 s / 34.9 dB vs PyTorch, Kitchen INT8 110.8 s / 29.4 dB, PyTorch
    # 175.1 s. Requests from before the selector only carry
    # kitchen_attention_enabled; legacy_attention maps it.
    attention: Literal["flash", "kitchen", "pytorch"] = "flash"
    kitchen_attention_enabled: bool = True  # legacy, read only when "attention" is absent
    # Savings: load Reference-mode videos at half size (see REFERENCE_VIDEO_AREA_FRACTION).
    ref_video_downscale: bool = True
    # Opt in to Reference speedups that have previously changed or damaged output.
    experimental_ref_speedups: bool = False
    # Measured 2026-09-24 (5s 1280x704 Text):
    # SLA all-sparse (no forced dense step) 150s vs 171s; Spectrum forecasting 117s;
    # H3 block cache 132s; FFN chunking exact output, 8.06 -> 6.30 GiB peak reserved.
    sla_dense_step: bool = True
    spectrum_enabled: bool = False
    block_cache_enabled: bool = False
    ffn_chunking_enabled: bool = False
    # KJNodes MiniMaxLowVRAMAttention: attention in head groups (exact math),
    # buffers freed early. Added 2026-09-30 after 15 s Fast Stable renders sat
    # at 24.4 of 24.6 GiB (plus 1.8 GiB spilled) while H3 sampled and came out
    # NaN / corrupted.
    low_vram_attention_enabled: bool = False
    low_vram_attention_chunks: int = Field(default=4, ge=2, le=56)
    # GPU 0 always runs diffusion; GPU 1 can hold stages or diffusion weights.
    multi_gpu_offload: Literal["off", "conditioning", "conditioning_vae", "split", "parallel"] = "off"
    multi_gpu_split_gb: int = Field(default=8, ge=2, le=12)
    torch_compile_enabled: bool = False  # TorchCompileModel (inductor) on the H3 model
    reference_sizing: str = "match"
    ltx_tile_preset: Literal["low", "medium", "large"] = "medium"
    # Ignored - stage 2 is always native now (see LTX_STAGE2_NATIVE). Still
    # accepted so older persisted requests (Retry, history) keep validating.
    ltx_stage2_mode: Literal["speed", "quality"] = "quality"
    ltx_workflow: Literal["final_optimized", "final_optimized_v2", "native_fast", "v3"] = "final_optimized"
    ltx_fps: int = Field(default=24, ge=1, le=30)
    # MiniMax H3 output frame rate. The model always renders at 24 fps (its
    # audio latent is sized from frames / 24), so any other rate is made
    # after decoding: RIFE up to a common multiple of 24, then every Nth frame.
    h3_fps: Literal[12, 16, 24, 30, 48, 60] = 24
    # Enabled LTX LoRAs only, in stacking order (ltx_2_5 only).
    ltx_loras: list[LtxLora] = Field(default_factory=list, max_length=MAX_LTX_LORAS)
    # Wan 2.2 (wan_2_2 only; see build_wan_workflow). Turbo = the lightx2v
    # 4-step LoRAs on both experts (CFG 1); "motionfix" is rzgar's Slow-Mo Fix
    # pair in Keyframe (I2V) mode and lightx2v in Text. Off = the full model
    # (about 20 steps, CFG 3.5). wan_high_steps=None splits the steps in half.
    wan_loras: list[WanLora] = Field(default_factory=list, max_length=MAX_WAN_LORAS)
    wan_turbo: Literal["lightx2v", "motionfix", "off"] = "lightx2v"
    wan_cfg: float = Field(default=1.0, ge=1.0, le=10.0)
    wan_shift: float = Field(default=8.0, ge=1.0, le=20.0)
    wan_high_steps: int | None = Field(default=None, ge=1, le=99)
    wan_fps: Literal[16, 24, 32] = 16

    # Legacy multi-candidate request fields, retained so old saved jobs can still be inspected.
    reference_video_ids: list[str] = Field(default_factory=list, max_length=3)
    reference_audio_ids: list[str] = Field(default_factory=list, max_length=3)
    continue_from_job_id: str | None = None
    overlap_frames: Literal[5, 22, 39, 56] = 22
    candidate_count: int = Field(default=3, ge=1, le=3)
    enable_live_preview: bool = False
    enable_upscale: bool = False
    upscale_model: str = UPSCALE_MODELS[0]
    upscale_steps: int = Field(default=6, ge=1, le=40)
    upscale_denoise: float = Field(default=0.4, ge=0.05, le=1.0)
    upscale_seed: int = Field(default=20260909, ge=0, le=2_147_483_647)
    upscale_megapixels: float = Field(default=1.0, ge=0.1, le=16.0)
    enable_rife: bool = False
    rife_target_fps: float = Field(default=48, ge=1, le=120)
    # MiniMax H3 latent upscale (minimax_h3 only; the enable_upscale /
    # upscale_steps / upscale_denoise / upscale_seed fields above drive it,
    # always with the H3 upscaler). Output = render size x this, snapped to 32 px.
    upscale_factor: float = Field(default=1.5, ge=1.1, le=4.0)
    # Run the H3 upscale + second pass in overlapping time chunks (off = the
    # whole clip at once, which OOMs on long clips; see h3_upscale_warnings).
    upscale_chunking: bool = True
    upscale_advanced: H3UpscaleAdvanced = Field(default_factory=H3UpscaleAdvanced)

    # MiniMax H3 model checkpoint + LoRA stack (ignored for ltx_2_5).
    # checkpoint=None keeps the workflow template's own
    # existing default for that mode.
    checkpoint: str | None = Field(default=None, max_length=255)
    loras: list[MinimaxLoraSlot] = Field(default_factory=list)
    # The Trainer's epoch tests: one checkpoint of a training run, loaded on top of the stack above
    # (H3) or the LTX LoRAs, from H3MachineCustom/epoch-tests/<job>/ — never listed in the gallery.
    test_lora: str | None = Field(default=None, max_length=255)
    test_lora_strength: float = Field(default=1.0, ge=0.0, le=2.0)

    @field_validator("test_lora")
    @classmethod
    def known_test_lora(cls, value: str | None) -> str | None:
        if not value:
            return None
        if not TEST_LORA_RE.fullmatch(value):
            raise ValueError("Unknown epoch-test LoRA.")
        if not (Path(os.getenv("H3_CUSTOM_LORA_DIR", "/models/loras/H3MachineCustom")).parent / value).is_file():
            raise ValueError("The epoch-test LoRA file is gone (the Trainer removes it when the test is deleted).")
        return value

    @model_validator(mode="before")
    @classmethod
    def legacy_attention(cls, data: Any) -> Any:
        """Jobs and presets saved before the selector keep what they ran on."""
        if isinstance(data, dict) and "attention" not in data and "kitchen_attention_enabled" in data:
            data = {**data, "attention": "kitchen" if data["kitchen_attention_enabled"] else "pytorch"}
        return data

    @model_validator(mode="after")
    def validate_resolution(self) -> "GenerationRequest":
        if (self.width is None) != (self.height is None):
            raise ValueError("Set both resolution dimensions.")
        if self.model == "wan_2_2":
            if self.mode == "ref2v":
                raise ValueError("Wan 2.2 has Text and Keyframe modes only.")
            if self.duration > WAN_MAX_SECONDS:
                raise ValueError(f"Wan 2.2 clips can be at most {WAN_MAX_SECONDS:g} seconds.")
            if self.wan_high_steps is not None and self.wan_high_steps >= self.steps:
                raise ValueError("High-noise steps must be fewer than the total steps.")
            if len({item.lora for item in self.wan_loras}) != len(self.wan_loras):
                raise ValueError("Each Wan 2.2 LoRA can only be selected once.")
            pixels = (self.width * self.height / 1_000_000) if self.width is not None else self.megapixels
            if pixels > WAN_MAX_MEGAPIXELS + 1e-9:
                raise ValueError(f"Wan 2.2 renders at most {WAN_MAX_MEGAPIXELS:g} megapixels (720p).")
        if self.width is None or self.height is None:
            if self.megapixels > 2.5:
                raise ValueError("Megapixels must be 2.5 or less without a custom resolution.")
            return self
        step = {"ltx_2_5": 64, "wan_2_2": 16}.get(self.model, 32)
        if self.width % step or self.height % step:
            raise ValueError(f"Resolution dimensions must be multiples of {step}.")
        self.megapixels = self.width * self.height / 1_000_000
        max_megapixels = {"ltx_2_5": 4.2, "wan_2_2": WAN_MAX_MEGAPIXELS}.get(self.model, 2.5)
        if self.megapixels > max_megapixels:
            raise ValueError(f"Resolution must be {max_megapixels:g} megapixels or less for this model.")
        return self

    @field_validator("prompt")
    @classmethod
    def prompt_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a prompt.")
        return value

    @field_validator("sampler")
    @classmethod
    def allowed_sampler(cls, value: str) -> str:
        if value not in SAMPLERS:
            raise ValueError("Unsupported sampler.")
        return value

    @field_validator("scheduler")
    @classmethod
    def allowed_scheduler(cls, value: str) -> str:
        if value not in SCHEDULERS:
            raise ValueError("Unsupported scheduler.")
        return value

    @field_validator("reference_sizing")
    @classmethod
    def allowed_reference_sizing(cls, value: str) -> str:
        if value not in REFERENCE_SIZING:
            raise ValueError("Unsupported reference sizing.")
        return value

    @field_validator("upscale_model")
    @classmethod
    def allowed_upscale_model(cls, value: str) -> str:
        if value not in UPSCALE_MODELS:
            raise ValueError("Unsupported upscale model.")
        return value

    @field_validator("checkpoint")
    @classmethod
    def known_checkpoint(cls, value: str | None) -> str | None:
        if value is not None and value not in _MINIMAX_H3_CHECKPOINT_VALUES:
            raise ValueError("Unknown model checkpoint.")
        return value

    @field_validator("loras")
    @classmethod
    def loras_bounded(cls, value: list[MinimaxLoraSlot]) -> list[MinimaxLoraSlot]:
        if len(value) > len(MINIMAX_H3_LORA_SLOTS):
            raise ValueError(f"At most {len(MINIMAX_H3_LORA_SLOTS)} LoRA slots are supported.")
        for index, slot in enumerate(value):
            if slot.enabled and CUSTOM_LORA_INFO.get(MINIMAX_H3_LORA_SLOTS[index][0], {}).get("deleted"):
                raise ValueError("A selected H3 LoRA was deleted from the gallery.")
        return value

    @field_validator("ltx_loras")
    @classmethod
    def ltx_loras_unique(cls, value: list[LtxLora]) -> list[LtxLora]:
        if len({item.lora for item in value}) != len(value):
            raise ValueError("Each LTX LoRA can only be selected once.")
        return value


class PresetRenameRequest(BaseModel):
    model: Literal["minimax_h3", "ltx_2_5", "wan_2_2"]
    name: str
    new_name: str = Field(min_length=1, max_length=60)

    @field_validator("new_name")
    @classmethod
    def new_name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a preset name.")
        return value


# Built-in unofficial camera moves: aliases -> official H3 camera wording
# (Official-H3-Prompting-Guidelines.md 4.3: motion type + amplitude + speed).
# Seeded once into the "camera" alias family; the user can edit or delete them
# and POST /api/trigger-aliases/camera-defaults puts back any that are missing.
CAMERA_ALIAS_DEFAULTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("whip pan left",), "The camera pans left with large amplitude at fast speed"),
    (("whip pan right", "whip pan"), "The camera pans right with large amplitude at fast speed"),
    (("crash zoom", "snap zoom"), "The camera zooms in with large amplitude at fast speed"),
    (("dolly zoom", "vertigo shot"), "The camera pushes in while it zooms out"),
    (("crane up", "jib up"), "The camera pedestals up with large amplitude while it tilts down"),
    (("crane down", "jib down"), "The camera pedestals down with large amplitude while it tilts up"),
    (("orbit clockwise",), "The camera arcs around the subject in a clockwise direction"),
    (("orbit counterclockwise",), "The camera arcs around the subject in a counterclockwise direction"),
    (("lead tracking", "leading shot"), "The camera tracks the subject from the front, pulling out as they move toward it"),
    (("tail tracking", "follow shot"), "The camera tracks the subject from behind, pushing in as they move forward"),
    (("side tracking",), "The camera tracks the subject from the side, trucking alongside them"),
    (("drone shot", "aerial shot"), "The camera trucks smoothly high above the scene at slow speed in an aerial view"),
    (("handheld camera",), "The camera shakes slightly in a handheld style"),
    (("slow push-in", "slow push"), "The camera pushes in with small amplitude at slow speed"),
    (("pull-back reveal", "reveal shot"), "The camera pulls out with large amplitude, revealing"),
)


# Built-in unofficial shot compositions. The guide has no shot-size vocabulary;
# its examples only use "a medium-wide shot", "a medium shot", "a close shot",
# "a close-up" and "an extreme close-up" (those are the official chips), so the
# rest expand into a descriptor + shot noun, so "a wide shot of the city" still
# reads naturally after expansion ("a wide full-environment shot of the city").
SHOT_ALIAS_DEFAULTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("two shot", "two-shot"), "a two-character medium shot"),
    (("over-the-shoulder shot", "OTS shot"), "an over-the-shoulder medium shot"),
    (("full shot", "full body shot"), "a head-to-toe full shot"),
    (("cowboy shot",), "a mid-thigh-up medium-wide shot"),
    (("wide shot", "long shot"), "a wide full-environment shot"),
    (("establishing shot", "extreme wide shot"), "an extreme wide establishing shot"),
    (("insert shot", "detail shot"), "an extreme close-up insert shot"),
    (("low angle shot",), "a low-angle upward-looking shot"),
    (("high angle shot",), "a high-angle downward-looking shot"),
    (("birds eye view", "top-down shot"), "a top-down bird's-eye view"),
    (("worms eye view",), "a ground-level upward worm's-eye view"),
    (("dutch angle", "canted angle"), "a tilted-horizon dutch-angle shot"),
)
# Unofficial alias families (no LoRA) and their built-ins.
ALIAS_DEFAULTS = {"camera": CAMERA_ALIAS_DEFAULTS, "shot": SHOT_ALIAS_DEFAULTS}


class TriggerAliasRequest(BaseModel):
    """Short words typed in the prompt that stand for one LoRA trigger keyword
    (see Store.save_trigger_alias_group); the browser swaps them in on Generate.
    `aliases` may be one comma-separated string ("tag, tag2" or "tag,tag2") or a list."""
    # The LoRA family, "camera" for an unofficial
    # camera move that expands into official H3 camera wording, or "shot" for an
    # unofficial shot composition that expands into a plain description.
    model: Literal["minimax_h3", "ltx_2_5", "wan_2_2", "camera", "shot"]
    aliases: list[str]
    lora: str = Field(default="", max_length=300)  # the LoRA's filename ("" for camera)
    target: str = Field(min_length=1, max_length=400)  # the text they become
    # When editing: the entry being replaced (its aliases are all rewritten).
    previous_lora: str | None = None
    previous_target: str | None = None

    @field_validator("aliases", mode="before")
    @classmethod
    def split_aliases(cls, value: Any) -> list[str]:
        items = value.split(",") if isinstance(value, str) else [part for item in (value or []) for part in str(item).split(",")]
        aliases: list[str] = []
        for item in items:
            alias = " ".join(item.split())
            if not alias:
                continue
            if len(alias) > 60 or not re.fullmatch(r"[\w][\w\- ]*[\w]|[\w]", alias):
                raise ValueError(f'"{alias}": an alias is letters, digits, _ and - (spaces allowed inside), up to 60 characters.')
            if alias.lower() not in (known.lower() for known in aliases):
                aliases.append(alias)
        if not aliases:
            raise ValueError("Enter at least one alias.")
        return aliases

    @field_validator("target")
    @classmethod
    def target_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("The target keyword can't be blank.")
        return value


class PresetRequest(BaseModel):
    """A named settings snapshot, stored server-side (see Store.save_preset)."""
    model: Literal["minimax_h3", "ltx_2_5", "wan_2_2"]
    name: str = Field(min_length=1, max_length=60)
    settings: dict[str, Any]

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a preset name.")
        return value

    @field_validator("settings")
    @classmethod
    def settings_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value)) > 64_000:
            raise ValueError("Preset is too large.")
        return value


class UpscaleCheckRequest(BaseModel):
    """The render settings the H3 upscale panel needs for its size readout and warnings."""
    mode: Literal["text", "fl2v", "ref2v"] = "text"
    width: int = Field(ge=64, le=2048)
    height: int = Field(ge=64, le=2048)
    upscale_factor: float = Field(ge=1.1, le=4.0)
    upscale_steps: int = Field(default=1, ge=1, le=40)
    upscale_denoise: float = Field(default=0.2, ge=0.05, le=1.0)
    upscale_chunking: bool = True
    upscale_advanced: H3UpscaleAdvanced = Field(default_factory=H3UpscaleAdvanced)
    duration: float = Field(default=5.0, ge=1.0, le=60.0)
    ref2v_turbo_enabled: bool = True
    ref2v_turbo: Literal["pdd", "lightx"] = "pdd"
    checkpoint: str | None = Field(default=None, max_length=255)


class ResolutionRequest(BaseModel):
    model: Literal["minimax_h3", "ltx_2_5", "wan_2_2"] = "minimax_h3"
    megapixels: float = Field(ge=0.004, le=4.2)
    aspect: Literal["16:9", "9:16", "1:1", "4:3", "3:4"]

    @model_validator(mode="after")
    def megapixels_for_model(self) -> "ResolutionRequest":
        if self.model != "ltx_2_5" and self.megapixels > 2.5:
            raise ValueError("Megapixels must be 2.5 or less for this model.")
        return self


class EnhanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["minimax_h3", "ltx_2_5", "wan_2_2"] = "minimax_h3"
    prompt: str = Field(min_length=1)
    additional_prompt: str = ""
    mode: Literal["text", "fl2v", "ref2v"]
    reference_ids: list[str] = Field(default_factory=list, max_length=24)
    first_frame_id: str | None = None
    last_frame_id: str | None = None
    duration: float | None = Field(default=None, ge=1, le=60)
    available_reference_ids: list[str] = Field(default_factory=list, max_length=24)


class AgenticGenerationRequest(GenerationRequest):
    prompt: str = Field(min_length=1)


class AgenticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resume_session_id: str | None = None
    generation: AgenticGenerationRequest
    guide_as_prompt: bool = False
    guide: str = ""
    available_reference_ids: list[str] = Field(default_factory=list, max_length=24)

    @model_validator(mode="before")
    @classmethod
    def use_guide_when_prompt_is_empty(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("generation"), dict):
            generation = value["generation"]
            guide = value.get("guide")
            if not str(generation.get("prompt") or "").strip() and isinstance(guide, str) and guide.strip():
                return {**value, "guide_as_prompt": True, "generation": {**generation, "prompt": guide.strip()}}
        return value


class FinalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate: Literal[1, 2, 3]
    enable_upscale: bool = False
    upscale_model: str = UPSCALE_MODELS[0]
    upscale_steps: int = Field(default=6, ge=1, le=40)
    upscale_denoise: float = Field(default=0.4, ge=0.05, le=1.0)
    upscale_seed: int = Field(default=20260909, ge=0, le=2_147_483_647)
    upscale_megapixels: float = Field(default=1.0, ge=0.1, le=16.0)
    enable_rife: bool = False
    rife_target_fps: float = Field(default=48, ge=1, le=120)

    @field_validator("upscale_model")
    @classmethod
    def allowed_upscale_model(cls, value: str) -> str:
        if value not in UPSCALE_MODELS:
            raise ValueError("Unsupported upscale model.")
        return value


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Retrying a reference job has to swap out whatever is in that
    # pool right now (every reference in a pool is sent), so the client must
    # opt in after the user confirms - see retry_job() in app.py.
    replace_references: bool = False


class StitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # One clip is enough when music goes over it (a music video from a single shot).
    job_ids: list[str] = Field(min_length=1, max_length=100)   # long Agentic sessions stitch every final clip
    transition: str = "cut"
    crossfade_seconds: float = Field(default=0.5, ge=0.1, le=5.0)
    fps: int = Field(default=24, ge=1, le=60)
    # When true, `fps` above is ignored and the output instead matches the
    # first selected clip's own recorded frame rate (see the `fps` column
    # jobs now save on completion) - same "match the first clip" precedent
    # width/height=None already follows below.
    match_fps: bool = False
    # None/None keeps the auto behavior: match the first selected clip's
    # resolution. Both must be given together, or left blank together.
    width: int | None = Field(default=None, ge=64, le=2048)
    height: int | None = Field(default=None, ge=64, le=2048)
    # Optional background music, mixed under the stitch's own audio for the
    # whole output. References `id` from the same reference pool/upload flow
    # every other audio reference uses (role="stitch-music"); trim_start/
    # trim_end on that reference select which segment to use, looped or
    # truncated to fit the final video's actual length.
    music_reference_id: str | None = None
    music_volume: float = Field(default=0.35, ge=0.0, le=1.0)
    # The clips' own audio under the music: 1 keeps it as is, 0 drops it so the
    # music is the only soundtrack (a music video). Only used with music.
    clip_volume: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_resolution(self) -> "StitchRequest":
        if (self.width is None) != (self.height is None):
            raise ValueError("Set both width and height, or leave both blank to match the first clip.")
        if self.width is not None and (self.width % 2 or self.height % 2):
            raise ValueError("Width and height must be even numbers.")
        return self

    @field_validator("transition")
    @classmethod
    def allowed_transition(cls, value: str) -> str:
        if value not in _STITCH_TRANSITION_VALUES:
            raise ValueError("Unsupported transition.")
        return value


class PostEditCrop(BaseModel):
    """Crop window as fractions of the source frame."""
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0.02, le=1)
    h: float = Field(gt=0.02, le=1)

    @model_validator(mode="after")
    def inside_frame(self) -> "PostEditCrop":
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValueError("The crop window must stay inside the frame.")
        return self


class PostEditAudioTrack(BaseModel):
    """A voice (TTS or uploaded) clip placed on the output timeline."""
    model_config = ConfigDict(extra="forbid")
    reference_id: str
    start: float = Field(default=0.0, ge=0, le=3600)
    volume: float = Field(default=1.0, ge=0, le=3)
    captions: bool = True


class PostEditText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)
    start: float = Field(default=0.0, ge=0, le=3600)
    end: float | None = Field(default=None, ge=0, le=3600)
    position: Literal["top", "middle", "bottom"] = "bottom"
    align: Literal["left", "center", "right"] = "center"
    size: int = Field(default=48, ge=8, le=300)
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    box: bool = False
    outline: bool = True
    fade: bool = True
    # TikTok-style: scales in with a bounce (and a slight tilt). Wins over `fade`.
    pop: bool = False


class PostEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_job_id: str
    # Timing
    trim_start: float = Field(default=0.0, ge=0)
    trim_end: float | None = Field(default=None, ge=0)
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    reverse: bool = False
    hold_end: float = Field(default=0.0, ge=0, le=60)
    fade_in: float = Field(default=0.0, ge=0, le=10)
    fade_out: float = Field(default=0.0, ge=0, le=10)
    fade_color: Literal["black", "white"] = "black"
    # Fade into another video: a finished generation joined at the end with an
    # xfade/acrossfade, optionally faded out at its own end (in fade_color).
    next_job_id: str | None = None
    next_reference_id: str | None = None  # an uploaded video (role postedit-video) instead of a generation
    next_transition: Literal["fade", "fadeblack", "fadewhite", "dissolve"] = "fade"
    next_transition_duration: float = Field(default=1.0, ge=0.1, le=5.0)
    next_fade_out: float = Field(default=0.0, ge=0, le=10)
    # Volume leveling: gain the next video's audio so it plays about as loud as
    # the edit before it (postedit.next_video_gain).
    next_match_loudness: bool = True

    @field_validator("next_job_id", "next_reference_id", mode="before")
    @classmethod
    def blank_next_job(cls, value: Any) -> Any:
        return value or None

    # Frame
    crop: PostEditCrop | None = None
    flip_h: bool = False
    flip_v: bool = False
    rotate: Literal[0, 90, 180, 270] = 0
    width: int | None = Field(default=None, ge=64, le=4096)
    height: int | None = Field(default=None, ge=64, le=4096)
    fps: int | None = Field(default=None, ge=6, le=120)
    interpolate: bool = False
    stabilize: bool = False
    # Look
    brightness: float = Field(default=0.0, ge=-1, le=1)
    contrast: float = Field(default=1.0, ge=0, le=3)
    saturation: float = Field(default=1.0, ge=0, le=3)
    gamma: float = Field(default=1.0, ge=0.1, le=3)
    hue: float = Field(default=0.0, ge=-180, le=180)
    temperature: int = Field(default=6500, ge=1000, le=12000)
    sharpen: float = Field(default=0.0, ge=-2, le=3)
    denoise: float = Field(default=0.0, ge=0, le=10)
    grain: int = Field(default=0, ge=0, le=60)
    vignette: float = Field(default=0.0, ge=0, le=1)
    # Effects (postedit.effect_filters)
    tone_curve: Literal["none", "lighter", "darker", "increase_contrast", "medium_contrast", "strong_contrast",
                        "vintage", "cross_process", "color_negative"] = "none"
    teal_orange: float = Field(default=0.0, ge=0, le=1)
    glow: float = Field(default=0.0, ge=0, le=1)
    rgb_split: int = Field(default=0, ge=0, le=20)
    pixelate: int = Field(default=0, ge=0, le=64)
    letterbox: Literal["off", "2.39", "2.00", "1.85"] = "off"
    # Slow zoom over the whole output: +0.2 = push in to 1.2x by the end, negative = pull out from that zoom.
    slow_zoom: float = Field(default=0.0, ge=-0.5, le=0.5)
    # Audio
    original_volume: float = Field(default=1.0, ge=0, le=3)
    mute_original: bool = False
    audio_denoise: bool = False
    # Music removal on the source's own audio (GPU, BandIt Plus DnR through
    # ComfyUI's BunnyRemoveMusic node): "no_music" keeps speech + effects,
    # "voices" keeps speech only.
    remove_music: Literal["off", "no_music", "voices"] = "off"
    normalize: bool = False
    # The video's own sound: tone/character, pitch (semitones, rubberband) and space.
    original_eq: Literal["off", "voice", "bass", "bright", "telephone", "radio", "muffled"] = "off"
    original_pitch: float = Field(default=0.0, ge=-12, le=12)
    original_echo: Literal["off", "room", "hall", "echo", "stadium"] = "off"
    # Voice-over bus (every voice clip together).
    voice_fx: Literal["off", "clear", "deep", "high", "radio", "telephone", "echo", "hall", "robot"] = "off"
    voice_deess: bool = False
    # Master bus (the finished mix).
    master_bass: float = Field(default=0.0, ge=-12, le=12)
    master_treble: float = Field(default=0.0, ge=-12, le=12)
    master_compress: Literal["off", "light", "punchy", "broadcast"] = "off"
    stereo_width: float = Field(default=1.0, ge=0, le=2)
    music_reference_id: str | None = None
    music_volume: float = Field(default=0.3, ge=0, le=3)
    music_start: float = Field(default=0.0, ge=0, le=3600)
    # Which part of the music file plays: from music_in (seconds into the
    # file) to music_out (None = the file's end). Looping repeats that part.
    music_in: float = Field(default=0.0, ge=0, le=36000)
    music_out: float | None = Field(default=None, ge=0, le=36000)
    music_loop: bool = True
    music_fade_out: float = Field(default=1.5, ge=0, le=10)
    music_fade_in: float = Field(default=0.0, ge=0, le=10)
    voices: list[PostEditAudioTrack] = Field(default_factory=list, max_length=20)
    duck: bool = True
    duck_strength: float = Field(default=0.6, ge=0, le=1)
    # Captions from TTS voice clips or Whisper on the source video audio.
    captions: bool = False
    caption_source: Literal["tts", "whisper"] = "tts"
    # Whisper captions: split the vocals from any music first (GPU), so songs give their lyrics and
    # captions hold only sung/spoken words (no music markers).
    caption_vocals: bool = True
    caption_size: int = Field(default=22, ge=8, le=80)
    caption_position: Literal["bottom", "middle", "top"] = "bottom"
    caption_color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    # "pop" = fast TikTok-style captions: a few words at a time popping in,
    # the spoken word highlighted (see postedit.pop_caption_events).
    caption_style: Literal["classic", "pop"] = "classic"
    pop_words: int = Field(default=2, ge=1, le=6)
    pop_highlight: str = Field(default="#ffe14d", pattern=r"^#[0-9a-fA-F]{6}$")
    pop_uppercase: bool = True
    pop_tilt: bool = True
    texts: list[PostEditText] = Field(default_factory=list, max_length=20)
    # Output
    quality: Literal["standard", "high", "near_lossless"] = "high"
    # Editor-only state (crop aspect/zoom/pan) saved so a re-edit restores the
    # controls exactly; the render itself only uses `crop`.
    editor: dict[str, Any] | None = None

    @model_validator(mode="after")
    def check(self) -> "PostEditRequest":
        if self.captions and self.caption_source == "whisper" and self.reverse:
            raise ValueError("Whisper captions are unavailable for reversed audio.")
        if (self.width is None) != (self.height is None):
            raise ValueError("Set both width and height, or leave both blank to keep the size.")
        if self.width is not None and (self.width % 2 or self.height % 2):
            raise ValueError("Width and height must be even numbers.")
        if self.trim_end is not None and self.trim_end - self.trim_start < 0.1:
            raise ValueError("The trimmed clip must be at least 0.1s long.")
        if self.music_out is not None and self.music_out - self.music_in < 0.1:
            raise ValueError("The selected part of the music must be at least 0.1s long.")
        if self.editor is not None and len(json.dumps(self.editor)) > 2000:
            raise ValueError("Editor state is too large.")
        return self


class TTSRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["edge", "tiktok"] = "edge"
    voice: str = Field(min_length=2, max_length=80)
    text: str = Field(min_length=1, max_length=5000)
    rate: int = Field(default=0, ge=-50, le=100)
    pitch: int = Field(default=0, ge=-50, le=50)
    volume: int = Field(default=0, ge=-50, le=50)
    role: Literal["postedit-voice", "references"] = "postedit-voice"
    label: str | None = Field(default=None, max_length=80)


class Reference(BaseModel):
    id: str
    filename: str
    alias: str
    mime_type: str
    preview_url: str
    created_at: datetime


class Job(BaseModel):
    id: str
    model: str = "minimax_h3"
    prompt_id: str | None = None
    mode: str
    prompt: str
    state: str
    stage: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    output_url: str | None = None
    download_url: str | None = None
    candidate_urls: list[str] | None = None
    error: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
