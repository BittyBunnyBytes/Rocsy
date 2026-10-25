"""Model-family registry for the Trainer: what each trainable model needs, how to spot its files
in ComfyUI's models/ folder, and which installed trainer backend runs it.

Detection reads only safetensors headers (key names, dtypes, shapes, metadata - never tensor data),
so a model the user installs later (a new finetune, an SDXL checkpoint, a Flux.1 DiT) is recognised
by its architecture, not its file name. Families whose base model is not installed yet are still
listed, so the Train tab can say what to download.

Backends:
  fizgig     - vendor/Fizgig (Krea 2, MiniMax H3, Flux.2 Klein 9B); vendor/Fizgig-latest (Qwen Image 2.1, MOSS-Image,
               and every full fine-tune: Krea 2, Qwen Image 2.1, Klein 9B, MOSS-Image, MiniMax H3, SDXL, Anima)
  sd-scripts - a kohya sd-scripts checkout (TRAINER_SDSCRIPTS_DIR) with its ROCm venv
               (Anima, SDXL, SD 1.5, Flux.1 / Chroma, SD3, Lumina 2, HunyuanImage 2.1)
  ai-toolkit - vendor/ai-toolkit (LTX 2.5, plus one recipe per upstream example config)
  llm        - vendor/llm-lora (PEFT LoRAs for chat/text LLMs from Hugging Face, converted to GGUF for llama.cpp)
"""
from __future__ import annotations

import json
import os
import re
import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

MODEL_EXTS = {".safetensors", ".sft", ".gguf", ".ckpt", ".pt", ".pth", ".bin"}
BASE_FOLDERS = ("diffusion_models", "unet", "checkpoints")
TRAINER_ROOT = Path(os.getenv("TRAINER_REPO", "/opt/trainers")).resolve()
# kohya sd-scripts checkout (mounted by docker-compose.override.yml); missing = those families are unavailable
SDSCRIPTS_DIR = Path(os.getenv("TRAINER_SDSCRIPTS_DIR", "/opt/sd-scripts"))
SDSCRIPTS_PYTHON = Path(os.getenv("TRAINER_SDSCRIPTS_PYTHON", str(SDSCRIPTS_DIR.parent / ".venv-rocm7.10" / "bin" / "python")))
AITK_DIR = TRAINER_ROOT / "vendor" / "ai-toolkit"
LLM_DIR = TRAINER_ROOT / "vendor" / "llm-lora"
HF_HOME = Path(os.getenv("HF_HOME", "/data/hf-cache"))


# ---- safetensors headers ---------------------------------------------------------------------

@dataclass
class Header:
    keys: list[str]
    dtypes: Counter
    shapes: dict[str, list[int]]
    meta: dict[str, str]

    def has(self, *fragments: str) -> bool:
        return any(any(f in k for k in self.keys) for f in fragments)

    def stripped(self) -> list[str]:
        """Keys without the usual checkpoint wrappers (model.diffusion_model., net., ...)."""
        out = []
        for k in self.keys:
            for prefix in ("model.diffusion_model.", "diffusion_model.", "net.", "model."):
                if k.startswith(prefix):
                    k = k[len(prefix):]
                    break
            out.append(k)
        return out

    def count_blocks(self, pattern: str) -> int:
        rx = re.compile(pattern)
        return len({m.group(1) for k in self.stripped() if (m := rx.match(k))})

    @property
    def int8(self) -> bool:
        return bool(self.dtypes.get("I8")) and self.has(".weight_scale")

    @property
    def fp8(self) -> bool:
        return any(d.startswith("F8") for d in self.dtypes)


_header_cache: dict[str, tuple[float, int, Header | None]] = {}


def read_header(path: Path) -> Header | None:
    try:
        st = path.stat()
    except OSError:
        return None
    hit = _header_cache.get(str(path))
    if hit and hit[:2] == (st.st_mtime, st.st_size):
        return hit[2]
    result = None
    if path.suffix.lower() in {".safetensors", ".sft"}:
        try:
            with path.open("rb") as f:
                size = struct.unpack("<Q", f.read(8))[0]
                if 0 < size < 200_000_000:
                    raw = json.loads(f.read(size))
                    meta = raw.pop("__metadata__", None) or {}
                    result = Header(keys=list(raw), dtypes=Counter(v.get("dtype", "") for v in raw.values()),
                                    shapes={k: v.get("shape", []) for k, v in raw.items()},
                                    meta={k: str(v)[:200] for k, v in meta.items()})
        except (OSError, ValueError, struct.error):
            result = None
    _header_cache[str(path)] = (st.st_mtime, st.st_size, result)
    return result


# ---- family definitions ----------------------------------------------------------------------

READY, UNTESTED, UNSUPPORTED = "ready", "untested", "unsupported"


@dataclass
class Slot:
    key: str                      # request field / preset key
    label: str
    folders: tuple[str, ...]
    required: bool = True
    suggest: str = ""             # regex on the relative path; matching files are offered first
    base: bool = False            # the base-model slot: candidates come from header detection
    check: Callable[[Header | None, str], tuple[str, str]] | None = None  # aux-file format check
    get: dict[str, Any] | None = None   # where to download it: {repo, path, dest, gb, gated}
    note: str = ""


@dataclass
class Family:
    id: str
    title: str
    backend: str                  # fizgig | sd-scripts | ai-toolkit
    group: str                    # Image | Video
    media: frozenset[str]         # dataset kinds it trains on: image / video / audio
    slots: list[Slot]
    knobs: tuple[str, ...]        # form fields this family uses
    defaults: dict[str, Any]      # the "Recommended" built-in preset
    hint: str
    detect: Callable[[Header, str], tuple[str, str, str] | None] | None = None  # -> (variant, verdict, reason)
    name_hint: str = ""           # filename regex used only when there is no readable header (.gguf etc.)
    extra: dict[str, Any] = field(default_factory=dict)


def _verdict_for_quant(h: Header, label: str, *, int8_ok: bool, fp8_ok: bool, bf16_reason: str = "") -> tuple[str, str, str]:
    if h.int8:
        return (f"{label} · ComfyUI int8", READY if int8_ok else UNSUPPORTED,
                "" if int8_ok else "ComfyUI int8 (ConvRot) file: this trainer needs the bf16 release (it quantises it itself)")
    if h.fp8:
        return (f"{label} · fp8", UNTESTED if fp8_ok else UNSUPPORTED,
                "fp8 checkpoint: loads, but the bf16 release is the tested recipe" if fp8_ok else "fp8 checkpoint: this trainer needs the bf16 release")
    return (f"{label} · bf16", READY, bf16_reason)


def _distilled(name: str) -> str:
    return "turbo/distilled checkpoint: LoRAs usually train better on the base model" if re.search(r"turbo|distill|lightning|hyper|schnell|lcm", name, re.I) else ""


# --- detectors (header -> variant/verdict) ---

def _detect_h3(h: Header, name: str):
    if not (h.has("audio_patch_proj.") and h.has("video_patch_proj.") or h.has("token_refiner.") and h.has("adaln_t_table")):
        return None
    official = "minimax_h3_fl2va_pruned_int8_convrot_official" in name
    verdict = READY if official else UNTESTED
    reason = "" if official else "H3 finetune/merge: Fizgig reads ComfyUI int8 H3 files, but only the official fl2va build is tested here"
    if "ref2va" in name.lower():
        reason = "ref2va build: train with Ostris' ref2va adapter for LoRAs that live in Reference mode"
    if (d := _distilled(name)):
        reason = d
    return ("MiniMax H3", verdict, reason)


def _detect_krea2(h: Header, name: str):
    if not (h.has("txtfusion.") and h.has("txtmlp.")):
        return None
    if h.int8:
        return ("Krea 2 · ComfyUI int8", UNSUPPORTED, "ComfyUI int8 (ConvRot) file: Fizgig's Krea 2 trainer needs krea2_raw_bf16")
    if h.fp8:
        scaled = h.has(".weight_scale")
        return ("Krea 2 · fp8", UNTESTED, ("pre-quantised fp8: Fizgig loads it, but" if scaled else "fp8 checkpoint without scales: may not load;")
                + " the RAW bf16 DiT is the tested recipe" + ("; " + _distilled(name) if _distilled(name) else ""))
    return ("Krea 2 · bf16", UNTESTED if _distilled(name) else READY, _distilled(name))


def _detect_qwen21(h: Header, name: str):
    if not (h.has("transformer_blocks.0.attn.to_q") and h.has("txt_in") and h.has("img_in")):
        return None
    # 2.1 feeds Qwen3-VL-8B through a txt_in MLP (in_layer/out_layer); v1 has one txt_in Linear from Qwen2.5-VL.
    if not h.has("txt_in.in_layer"):
        return ("Qwen-Image (v1)", UNSUPPORTED, "Qwen-Image v1: no installed trainer has it (AI Toolkit's qwen_image recipe can, from Hugging Face)")
    return _verdict_for_quant(h, "Qwen Image 2.1", int8_ok=False, fp8_ok=False, bf16_reason=_distilled(name))


def _detect_klein(h: Header, name: str):
    flux2 = h.has("double_stream_modulation_img") or h.has("single_stream_modulation")
    if not flux2 and not re.search(r"klein|flux[._-]?2", name, re.I):
        return None
    if not (h.has("double_blocks.") or h.has("single_blocks.")):
        return None
    if not re.search(r"klein", name, re.I) and h.count_blocks(r"double_blocks\.(\d+)\.") > 8:
        return ("Flux.2 (dev)", UNSUPPORTED, "Flux.2 dev: no installed trainer has it (AI Toolkit's flux2 recipe can)")
    reason = _distilled(name) or ("" if "base" in name.lower() else "use the klein *base* checkpoint for training; the distilled one is for previews")
    return ("Flux.2 Klein 9B", READY if not reason else UNTESTED, reason)


def _detect_moss_image(h: Header, name: str):
    if not (h.has("shared_block_modulation.") and h.has("text_embedder.blocks.") and h.has("final_linear_video.")):
        return None
    sft = "sft" in name.lower()
    label = "MOSS-Image " + ("SFT" if sft else "Base")
    if h.int8:
        return (f"{label} · ComfyUI int8", UNTESTED,
                "ComfyUI int8 (ConvRot) file: Fizgig decodes it at load, but the bf16 Base is the tested recipe")
    if sft:
        return (f"{label} · bf16", UNTESTED,
                "SFT checkpoint: Fizgig trains attention only on SFT (its feed-forward layers break anatomy); "
                "train on Base and use the LoRA on SFT")
    return (f"{label} · bf16", UNTESTED if _distilled(name) else READY, _distilled(name))


def _detect_anima(h: Header, name: str):
    if not h.has("adaln_modulation_cross_attn"):
        return None
    blocks = h.count_blocks(r"blocks\.(\d+)\.")
    if h.has("anima_v2_connector") or blocks != 28:
        return (f"Anima ({blocks} blocks)", UNSUPPORTED, "newer/larger Anima build: sd-scripts only knows the 28-block Anima 2B layout")
    return ("Anima 2B", UNTESTED if _distilled(name) else READY, _distilled(name))


def _detect_sdxl(h: Header, name: str):
    if h.has("conditioner.embedders.1.model.") or (h.has("label_emb.0.0.weight") and h.has("input_blocks.")):
        return ("SDXL", UNTESTED if _distilled(name) else READY, _distilled(name) or ("checkpoint has no text encoders: sd-scripts needs a full SDXL checkpoint" if not h.has("conditioner.embedders") else ""))
    return None


def _detect_sd15(h: Header, name: str):
    if h.has("cond_stage_model.transformer.text_model") and h.has("model.diffusion_model.input_blocks."):
        return ("SD 1.5", READY, _distilled(name))
    return None


def _detect_flux1(h: Header, name: str):
    if not (h.has("double_blocks.0.img_attn.qkv") and h.has("single_blocks.0.linear1")):
        return None
    if h.has("double_stream_modulation_img"):
        return None
    if h.has("distilled_guidance_layer."):
        return ("Chroma", READY, "")
    if h.int8:
        return ("Flux.1 · ComfyUI int8", UNSUPPORTED, "ComfyUI int8 file: sd-scripts needs the bf16/fp8 Flux.1 DiT")
    return ("Flux.1", UNTESTED if _distilled(name) else READY, _distilled(name))


def _detect_sd3(h: Header, name: str):
    if h.has("joint_blocks.0.x_block.attn.qkv"):
        return ("SD3 / 3.5", READY, "")
    return None


def _detect_lumina2(h: Header, name: str):
    if not (h.has("cap_embedder.") and h.has("noise_refiner.") and h.has("layers.0.attention.qkv")):
        return None
    dim = next((s[0] for k, s in h.shapes.items() if k.endswith("x_embedder.weight") and s), 0)
    if dim and dim != 2304:
        return ("Z-Image / other NextDiT", UNSUPPORTED, "NextDiT model that isn't Lumina 2 (Z-Image?): AI Toolkit's z_image recipe can train it")
    return ("Lumina Image 2.0", READY, "")


def _detect_hunyuan_image(h: Header, name: str):
    if h.has("byt5_in.") or (h.has("double_blocks.0.img_attn_qkv") and re.search(r"hunyuan.?image", name, re.I)):
        return ("HunyuanImage 2.1", READY if not _distilled(name) else UNTESTED, _distilled(name))
    return None


def _detect_ltx(h: Header, name: str):
    if not (h.has("audio_embeddings_connector") or h.has("transformer_blocks.0.audio_attn") or h.has("audio_scale_shift_table")
            or h.has("adaln_single.") and h.has("caption_projection.")):
        return None
    ver = "LTX 2.5" if "2.5" in name or "2_5" in name else "LTX 2.3" if "2.3" in name else "LTX-2"
    return (ver, UNTESTED, "AI Toolkit trains LTX from the Hugging Face release (Lightricks/LTX-2.5), not this ComfyUI file; it confirms LTX is set up")


# Recognised, but nothing here can train them.
def _detect_other(h: Header, name: str):
    if h.has("vid_out_ada") or "seedvr" in name.lower():
        return ("SeedVR2 upscaler", UNSUPPORTED, "video upscaler: there is no LoRA trainer for it")
    if h.has("patch_embedding.") and h.has("blocks.0.cross_attn.k"):
        return ("Wan 2.x", UNSUPPORTED, "Wan video model: AI Toolkit's wan21/wan22 recipes train it from Hugging Face")
    if h.has("sam") and "sam" in name.lower():
        return ("SAM segmentation", UNSUPPORTED, "segmentation model, not a generator")
    return None


# --- aux-file checks ---

def _qwen21_vae_check(h: Header | None, name: str):
    if h is None:
        return (UNTESTED, "")
    if h.has("decoder.up_blocks.") or h.has("encoder.down_blocks."):
        return (READY, "diffusers-format VAE")
    return (UNSUPPORTED, "ComfyUI-format VAE: Fizgig's Qwen 2.1 cache step needs the diffusers vae/diffusion_pytorch_model.safetensors")


def _bf16_te_check(h: Header | None, name: str):
    if h is None:
        return (UNTESTED, "")
    if h.int8:
        return (UNSUPPORTED, "ComfyUI int8 text encoder: Fizgig needs the bf16 one")
    return (READY, "")


def _int8_untested(h: Header | None, name: str):
    if "nvfp4" in name.lower():  # the H3 encoder Fizgig ships with (packed nvfp4 also stores I8/scales)
        return (READY, "")
    if h is not None and h.int8:
        return (UNTESTED, "ComfyUI int8 file: the trainer's recipe uses the fp16/bf16 release")
    return (READY, "")


def _ae_vae(h: Header | None, name: str):
    return (READY, "") if h is not None else (UNTESTED, "")


def finetune_unfit(path: Path, family_id: str = "") -> str:
    """Why a base file can't be fully fine-tuned, or "" (Fizgig's families/ft.py rule, header only): the trainer builds
    its bf16 master from the file and writes every checkpoint in its layout, so fp8/int8/pre-quantised files can't be used.
    MiniMax H3 is the exception (fizgig/minimax/ft_backend.py): its master is decoded from the pruned int8 ConvRot codes."""
    h = read_header(path)
    if h is None:
        return "not a readable .safetensors file"
    if family_id == "minimax_h3":
        if not h.has("adaln_t_table"):
            return "it is not a pruned H3 build (the 66 GB bf16 file trains LoRAs only)"
        if h.has(".weight_codebook") or h.fp8 or not h.int8:
            return "it is not the int8 ConvRot build (w4a8/fp8 files can't be fine-tuned)"
        return ""
    if h.has(".weight_scale", ".scale_weight"):
        return "it is a pre-quantised checkpoint (weight scales); a fine-tune needs the bf16 release"
    low = sum(n for d, n in h.dtypes.items() if d.startswith(("F8", "I8", "U8")))
    if low:
        return f"it stores {low} tensor(s) below 16 bits (fp8/int8); a fine-tune needs the bf16 release"
    return ""


S = Slot
FIZGIG = "fizgig"
SDS = "sd-scripts"
AITK = "ai-toolkit"
LLM = "llm"
FT_NOTE = ("Fine-tune trains the whole model (Fizgig's rotating windows: one slice trains at a time on a 4-bit trunk, the bf16 master stays in system RAM). One rotation = every part of the model trained once; every save is a full checkpoint.")
IMG = frozenset({"image"})

FAMILIES: list[Family] = [
    Family(
        "minimax_h3", "MiniMax H3", FIZGIG, "Video", frozenset({"image", "video", "audio"}),
        [S("dit", "Base model (DiT)", BASE_FOLDERS, base=True),
         S("vae", "Video VAE", ("vae",), suggest=r"h3_video_vae|minimax_h3_video", check=_int8_untested,
           get={"repo": "Comfy-Org/MiniMax-H3", "path": "vae/minimax_h3_video_vae_fp16.safetensors", "dest": "vae", "gb": 5.2}),
         S("text_encoder", "Text encoder", ("text_encoders", "clip"), suggest=r"qwen3vl_32b.*minimax|minimax.*qwen", check=_int8_untested,
           get={"repo": "Comfy-Org/MiniMax-H3", "path": "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "dest": "text_encoders", "gb": 15.7}),
         S("audio_vae", "Audio VAE (sound/voice only)", ("vae",), required=False, suggest=r"h3.*audio_vae|minimax.*audio",
           get={"repo": "Comfy-Org/MiniMax-H3", "path": "vae/minimax_h3_audio_vae_fp32.safetensors", "dest": "vae", "gb": 0.6}),
         S("training_adapter", "Training adapter (recommended)", ("training_adapters", "loras"), required=False, suggest=r"h3.*training_adapter|minimax.*adapter",
           get={"repo": "ostris/minimax_h3_training_adapter", "path": "minimax_h3_training_adapter_v1.safetensors", "dest": "training_adapters", "gb": 0.2})],
        ("rank", "epochs", "resolution", "trigger", "kind", "rotations", "save_every", "learning_rate"),
        {"rank": 16, "epochs": 10, "resolution": 512, "kind": "lora", "rotations": 4, "save_every": 1},
        "MiniMax H3 uses Fizgig ROCm (Automagic v3, EMA 0.98, TREAD on clips). Clips must be .mp4, 24 fps, 22/39/56/73 frames, sides divisible by 32. "
        "Add the audio VAE only for sound/voice data. Stop ComfyUI first so the trainer gets the whole GPU.",
        _detect_h3, extra={"finetune": {"lr": "3e-5", "fizgig": "minimax", "gb": 21.0, "lr_ft_only": True,
            "base": "the pruned int8 build, minimax_h3_fl2va_pruned_int8_convrot.safetensors (Comfy-Org/MiniMax-H3)"}}),
    Family(
        "krea2", "Krea 2", FIZGIG, "Image", IMG,
        [S("dit", "Base model (RAW DiT)", BASE_FOLDERS, base=True,
           get={"repo": "Comfy-Org/Krea-2", "path": "diffusion_models/krea2_raw_bf16.safetensors", "dest": "diffusion_models", "gb": 26.3}),
         S("vae", "VAE", ("vae",), suggest=r"(^|/)qwen_image_vae|krea",
           get={"repo": "Comfy-Org/Krea-2", "path": "vae/qwen_image_vae.safetensors", "dest": "vae", "gb": 0.25}),
         S("text_encoder", "Text encoder (Qwen3-VL 4B)", ("text_encoders", "clip"), suggest=r"qwen3vl_4b|qwen_3_4b|qwen3_4b",
           get={"repo": "Comfy-Org/Krea-2", "path": "text_encoders/qwen3vl_4b_bf16.safetensors", "dest": "text_encoders", "gb": 8.9})],
        ("rank", "epochs", "learning_rate", "trigger", "kind", "rotations", "save_every"),
        {"rank": 16, "epochs": 10, "learning_rate": "1e-4", "kind": "lora", "rotations": 4, "save_every": 1},
        "Krea 2 uses Fizgig ROCm, 4-bit base quantisation, 768 px buckets. It trains on the RAW bf16 DiT; Lustify and other Krea 2 finetunes "
        "are detected but are fp8/int8 files, which are untested or unsupported as a training base.",
        _detect_krea2, extra={"finetune": {"lr": "1e-5", "fizgig": "krea2", "gb": 26.3}}),
    Family(
        "qwen_image_21", "Qwen Image 2.1", FIZGIG, "Image", IMG,
        [S("dit", "Base model (bf16 DiT)", BASE_FOLDERS, base=True,
           get={"repo": "Comfy-Org/Qwen-Image-2.1", "path": "diffusion_models/qwen_image_2.1_bf16.safetensors", "dest": "diffusion_models", "gb": 14.2}),
         S("vae", "VAE (diffusers format)", ("vae",), suggest=r"qwen.?image.?2\.?1|qwen21|diffusion_pytorch_model", check=_qwen21_vae_check,
           get={"repo": "Qwen/Qwen-Image-2.1", "path": "vae/diffusion_pytorch_model.safetensors", "dest": "vae", "gb": 0.5,
                "save_as": "qwen_image_2.1_vae_diffusers.safetensors", "manual": True}),
         S("text_encoder", "Text encoder (Qwen3-VL 8B bf16)", ("text_encoders", "clip"), suggest=r"qwen3vl_8b", check=_bf16_te_check,
           get={"repo": "Comfy-Org/Qwen-Image-2.1", "path": "text_encoders/qwen3vl_8b_bf16.safetensors", "dest": "text_encoders", "gb": 17.5}),
         S("training_adapter", "Training adapter (recommended)", ("training_adapters", "loras"), required=False, suggest=r"qwen.*training_adapter|fizgig_qwen",
           get={"repo": "ShootTheSound/Fizgig-Qwen-Image-2.1-Training-Adapter", "path": "fizgig_qwen_image_2.1_training_adapter.safetensors", "dest": "training_adapters", "gb": 0.1})],
        ("rank", "epochs", "max_pixels", "learning_rate", "trigger", "kind", "rotations", "save_every"),
        {"rank": 16, "epochs": 30, "max_pixels": 524288, "learning_rate": "1e-4", "kind": "lora", "rotations": 4, "save_every": 1},
        "Qwen Image 2.1 uses Fizgig's family trainer (v8.7.9, from vendor/Fizgig-latest): Standard is rank 16 with adaptive LR 1e-4…2e-4, "
        "Fast is rank 8 with 2e-4…4e-4; EMA 0.98, the training adapter, auto precision, 0.5 MP. "
        "It needs the bf16 DiT, the diffusers-format VAE and the bf16 Qwen3-VL-8B encoder; the ComfyUI int8 files you render with can't be trained on.",
        _detect_qwen21, extra={"finetune": {"lr": "1e-5", "fizgig": "qwen_image21", "gb": 14.2}}),
    Family(
        "klein9b", "Flux.2 Klein 9B", FIZGIG, "Image", IMG,
        [S("dit", "Base model (klein base 9B)", BASE_FOLDERS, base=True,
           get={"repo": "black-forest-labs/FLUX.2-klein-base-9b-fp8", "path": "", "dest": "diffusion_models", "gated": True, "manual": True}),
         S("vae", "VAE (FLUX.2 ae.safetensors)", ("vae",), suggest=r"flux.?2.*ae|(^|/)ae\.safetensors|flux2",
           get={"repo": "black-forest-labs/FLUX.2-dev", "path": "ae.safetensors", "dest": "vae", "gated": True, "manual": True}),
         S("text_encoder", "Text encoder (Qwen3 8B)", ("text_encoders", "clip"), suggest=r"qwen_?3_?8b",
           get={"repo": "Comfy-Org/vae-text-encorder-for-flux-klein-9b", "path": "split_files/text_encoders/qwen_3_8b.safetensors", "dest": "text_encoders", "gb": 16.4})],
        ("rank", "epochs", "learning_rate", "trigger", "kind", "rotations", "save_every"),
        {"rank": 16, "epochs": 30, "learning_rate": "1e-4", "kind": "lora", "rotations": 4, "save_every": 1},
        "Flux.2 Klein 9B uses Fizgig's Klein recipe (fp8 scaled base, adaptive LR 5e-5…4e-4, flux2_shift). Its base model repos are gated on Hugging Face: accept the licence there first.",
        _detect_klein, name_hint=r"klein", extra={"finetune": {"lr": "1e-5", "fizgig": "klein", "gb": 17.0,
            "base": "the bf16 klein base (black-forest-labs/FLUX.2-klein-base-9B, gated), not the fp8 file the LoRA recipe uses"}}),
    Family(
        "moss_image", "MOSS-Image", FIZGIG, "Image", IMG,
        [S("dit", "Base model (Base bf16 DiT)", BASE_FOLDERS, base=True,
           get={"repo": "Comfy-Org/MOSS-Image", "path": "diffusion_models/moss_image_base_bf16.safetensors", "dest": "diffusion_models", "gb": 10.2}),
         S("vae", "VAE (FLUX.2 autoencoder)", ("vae",), suggest=r"flux.?2.?vae|flux2|(^|/)ae\.safetensors", check=_ae_vae,
           get={"repo": "Comfy-Org/MOSS-Image", "path": "vae/flux2-vae_small.safetensors", "dest": "vae", "gb": 0.25}),
         S("text_encoder", "Text encoder (Qwen3.5-9B)", ("text_encoders", "clip"), suggest=r"qwen3\.?5.?9b(?!.*qwen_image)",
           get={"repo": "Comfy-Org/Qwen3.5", "path": "text_encoders/qwen3.5_9b_bf16.safetensors", "dest": "text_encoders", "gb": 19.3})],
        ("rank", "epochs", "max_pixels", "learning_rate", "trigger", "kind", "rotations", "save_every"),
        {"rank": 32, "epochs": 70, "max_pixels": 1048576, "learning_rate": "1e-4", "kind": "lora", "rotations": 4, "save_every": 1},
        "MOSS-Image uses Fizgig's family trainer (v8.7.9, from vendor/Fizgig-latest). Standard: rank 32, AdamW 1e-4, EMA 0.98, auto INT8/NF4 base, "
        "1 MP; Fast: rank 16 at 0.26 MP. Both use Fizgig's Clean-end share of 30% (its MOSS training fix: likeness sooner, steadier composition). "
        "Train on Base, not SFT or Kromoss, and use the LoRA on SFT; an SFT base trains attention only. 70 epochs, then pick the epoch; "
        "under 50 pictures it can take longer. Write long, detailed captions. A NaN/Inf gradient step is skipped; 25 in a row stop the run.",
        _detect_moss_image, name_hint=r"moss.?image", extra={"finetune": {"lr": "1e-5", "fizgig": "moss_image", "gb": 10.2}}),
    Family(
        "anima", "Anima", SDS, "Image", IMG,
        [S("dit", "Base model (Anima DiT)", BASE_FOLDERS, base=True,
           get={"repo": "circlestone-labs/Anima", "path": "", "dest": "diffusion_models", "manual": True}),
         S("text_encoder", "Text encoder (Qwen3 0.6B)", ("text_encoders", "clip"), suggest=r"qwen_?3_?0?6b",
           get={"repo": "circlestone-labs/Anima", "path": "split_files/text_encoders/qwen_3_06b_base.safetensors", "dest": "text_encoders", "gb": 1.2}),
         S("vae", "VAE (Qwen-Image)", ("vae",), suggest=r"(^|/)qwen_image_vae",
           get={"repo": "circlestone-labs/Anima", "path": "split_files/vae/qwen_image_vae.safetensors", "dest": "vae", "gb": 0.25})],
        ("rank", "epochs", "resolution", "learning_rate", "repeats", "trigger", "kind", "rotations", "save_every"),
        {"rank": 32, "epochs": 30, "resolution": 1024, "learning_rate": "2e-5", "repeats": 1, "kind": "lora", "rotations": 4, "save_every": 1},
        "Anima uses kohya sd-scripts with the ROCm venv from its own checkout (the setup the tested character preset used): AdamW8bit with warmup, sigmoid "
        "timesteps, shuffled tags with the trigger kept first, cached latents, 1024 px buckets. Train on anima base or aesthetic; the 2.9B/3.8B builds aren't supported by sd-scripts yet.",
        _detect_anima, extra={"alpha_ratio": 0.5, "finetune": {"lr": "1e-5", "fizgig": "anima", "gb": 4.2}}),
    Family(
        "sdxl", "SDXL / Pony / Illustrious", SDS, "Image", IMG,
        [S("dit", "Checkpoint", BASE_FOLDERS, base=True, note="any SDXL-architecture checkpoint (Pony, Illustrious, NoobAI…)")],
        ("rank", "epochs", "resolution", "learning_rate", "repeats", "trigger", "kind", "rotations", "save_every"),
        {"rank": 16, "epochs": 10, "resolution": 1024, "learning_rate": "1e-4", "repeats": 1, "kind": "lora", "rotations": 4, "save_every": 1},
        "SDXL-family checkpoints train with sd-scripts (U-Net only, AdamW, bf16, cached latents). Not yet run on this machine; same ROCm venv as Anima.",
        _detect_sdxl, extra={"finetune": {"lr": "1e-5", "fizgig": "sdxl", "gb": 7.0}}),
    Family(
        "sd15", "SD 1.5", SDS, "Image", IMG,
        [S("dit", "Checkpoint", BASE_FOLDERS, base=True)],
        ("rank", "epochs", "resolution", "learning_rate", "repeats", "trigger"),
        {"rank": 16, "epochs": 10, "resolution": 512, "learning_rate": "1e-4", "repeats": 1},
        "SD 1.5 checkpoints train with sd-scripts' train_network.py at 512 px. Not yet run on this machine.",
        _detect_sd15),
    Family(
        "flux1", "Flux.1 / Chroma", SDS, "Image", IMG,
        [S("dit", "Base model (DiT)", BASE_FOLDERS, base=True),
         S("clip_l", "CLIP-L (Flux.1 only)", ("text_encoders", "clip"), required=False, suggest=r"clip_l"),
         S("t5xxl", "T5-XXL", ("text_encoders", "clip"), suggest=r"t5.?xxl",
           get={"repo": "comfyanonymous/flux_text_encoders", "path": "t5xxl_fp16.safetensors", "dest": "text_encoders", "gb": 9.8}),
         S("ae", "VAE (ae.safetensors)", ("vae",), suggest=r"(^|/)ae\.safetensors|flux.*vae", check=_ae_vae)],
        ("rank", "epochs", "resolution", "learning_rate", "repeats", "trigger"),
        {"rank": 16, "epochs": 10, "resolution": 768, "learning_rate": "1e-4", "repeats": 1},
        "Flux.1 dev and Chroma train with sd-scripts (fp8 base, 18 swapped blocks, cached text). Flux.1 needs CLIP-L + T5-XXL + ae; Chroma only T5-XXL + ae. "
        "Not yet run on this machine.",
        _detect_flux1),
    Family(
        "sd3", "SD3 / SD3.5", SDS, "Image", IMG,
        [S("dit", "Checkpoint", BASE_FOLDERS, base=True),
         S("clip_l", "CLIP-L", ("text_encoders", "clip"), required=False, suggest=r"clip_l"),
         S("clip_g", "CLIP-G", ("text_encoders", "clip"), required=False, suggest=r"clip_g"),
         S("t5xxl", "T5-XXL", ("text_encoders", "clip"), required=False, suggest=r"t5.?xxl")],
        ("rank", "epochs", "resolution", "learning_rate", "repeats", "trigger"),
        {"rank": 16, "epochs": 10, "resolution": 1024, "learning_rate": "1e-4", "repeats": 1},
        "SD3/3.5 train with sd-scripts. Leave the text encoders empty if the checkpoint already contains them. Not yet run on this machine.",
        _detect_sd3),
    Family(
        "lumina2", "Lumina Image 2.0", SDS, "Image", IMG,
        [S("dit", "Base model (DiT)", BASE_FOLDERS, base=True),
         S("gemma2", "Gemma 2 2B text encoder", ("text_encoders", "clip"), suggest=r"gemma.?2"),
         S("ae", "VAE (Flux ae.safetensors)", ("vae",), suggest=r"(^|/)ae\.safetensors|flux.*vae", check=_ae_vae)],
        ("rank", "epochs", "resolution", "learning_rate", "repeats", "trigger"),
        {"rank": 8, "epochs": 10, "resolution": 1024, "learning_rate": "1e-4", "repeats": 1},
        "Lumina Image 2.0 trains with sd-scripts (nextdit_shift, Gemma 2 2B). Not yet run on this machine.",
        _detect_lumina2),
    Family(
        "hunyuan_image21", "HunyuanImage 2.1", SDS, "Image", IMG,
        [S("dit", "Base model (DiT)", BASE_FOLDERS, base=True),
         S("text_encoder", "Qwen2.5-VL text encoder", ("text_encoders", "clip"), suggest=r"qwen_?2\.?5.?vl"),
         S("byt5", "byT5 glyph encoder", ("text_encoders", "clip"), suggest=r"byt5"),
         S("vae", "VAE", ("vae",), suggest=r"hunyuan.*image|hunyuan_image")],
        ("rank", "epochs", "resolution", "learning_rate", "repeats", "trigger"),
        {"rank": 16, "epochs": 10, "resolution": 1024, "learning_rate": "1e-4", "repeats": 1},
        "HunyuanImage 2.1 trains with sd-scripts (18 swapped blocks, cached text). Not yet run on this machine.",
        _detect_hunyuan_image),
    Family(
        "ltx25", "LTX 2.5", AITK, "Video", frozenset({"video"}),
        [S("dit", "Installed LTX model (for reference)", BASE_FOLDERS, base=True, required=False,
           note="AI Toolkit downloads Lightricks/LTX-2.5 from Hugging Face itself")],
        ("rank", "frames", "resolution", "steps", "trigger"),
        {"rank": 16, "frames": 25, "resolution": 512, "steps": 2000},
        "LTX 2.5 trains video LoRAs through AI Toolkit (int8 ConvRot, low-VRAM). Use short clips with same-stem .txt captions; 25 frames is the starter. "
        "AI Toolkit fetches the model from Hugging Face on first run.",
        _detect_ltx),
    Family(
        "llm", "LLM (chat / text model)", LLM, "Text", frozenset({"text"}), [],
        ("base_model", "rank", "epochs", "learning_rate", "max_length", "quant"),
        {"rank": 16, "epochs": 3, "learning_rate": "2e-4", "max_length": 2048, "quant": "auto"},
        "Trains a LoRA for a chat/text LLM with PEFT on ROCm. The base model must be the Hugging Face (safetensors) release: "
        "GGUF files can't be trained, so pick the unquantised weights your installed GGUF was made from and the LoRA fits it. "
        "Up to ~5 B parameters train in bf16, bigger models with a 4-bit (QLoRA) base. The dataset is .jsonl/.json "
        "(chat messages, ShareGPT, prompt/completion, Alpaca or plain text); only the assistant replies are learned. "
        "The result is a PEFT adapter plus a GGUF LoRA for llama.cpp (--lora). Unload chat models in Llama first so the VRAM is free.",
        None),
]

# Families detected only so the Trainer can say "recognised, but not trainable here".
_OTHER_DETECTORS = [_detect_other]

_BY_ID = {f.id: f for f in FAMILIES}


def ai_toolkit_installed() -> bool:
    return any((AITK_DIR / v / "bin" / "python3").is_file() or (AITK_DIR / v / "bin" / "python").is_file() for v in (".venv", "venv"))


def ai_toolkit_recipes() -> list[Family]:
    """One family per AI Toolkit example config - so models added upstream show up after an update."""
    folder = AITK_DIR / "config" / "examples"
    out: list[Family] = []
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("train_lora_*.yaml")):
        try:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
            proc = cfg["config"]["process"][0]
            model = proc.get("model") or {}
            repo = str(model.get("name_or_path") or "")
            ds = (proc.get("datasets") or [{}])[0]
        except Exception:
            continue
        if not repo:
            continue
        arch = str(model.get("arch") or ("flux" if model.get("is_flux") else "sd35" if model.get("is_v3") else "lumina2" if model.get("is_lumina2") else ""))
        # Wan's examples default to num_frames: 1 (stills), but the model is a video model.
        video = int(ds.get("num_frames") or 1) > 1 or bool(re.search(r"wan|ltx|video|hunyuan_?video", f"{arch} {path.stem}", re.I))
        stem = path.stem.removeprefix("train_lora_")
        title = stem.replace("_", " ").replace("24gb", "").replace("32gb", "").replace("48", "").strip()
        title = re.sub(r"\s+", " ", title).title().replace("Sd35", "SD 3.5").replace("Wan2", "Wan 2")
        out.append(Family(
            f"aitk:{stem}", f"{title} (AI Toolkit)", AITK, "Video" if video else "Image",
            frozenset({"video", "image"}) if video else IMG,
            [], ("rank", "steps", "resolution", "trigger") + (("frames",) if video else ()),
            {"rank": 16, "steps": 2000, "resolution": 512 if video else 1024} | ({"frames": max(33, int(ds.get("num_frames") or 33))} if video else {}),
            f"Upstream AI Toolkit recipe {path.name} ({repo}{', arch ' + arch if arch else ''}). AI Toolkit downloads the base model from Hugging Face "
            "on first run; untested on this machine.",
            None, extra={"example": str(path), "repo": repo, "arch": arch}))
    return out


def all_families() -> list[Family]:
    return FAMILIES + ai_toolkit_recipes()


def get(family_id: str) -> Family | None:
    if family_id in _BY_ID:
        return _BY_ID[family_id]
    if family_id.startswith("aitk:"):
        return next((f for f in ai_toolkit_recipes() if f.id == family_id), None)
    return None


def title(family_id: str) -> str:
    fam = get(family_id)
    return fam.title if fam else family_id


# ---- backends --------------------------------------------------------------------------------

def backend_python(backend: str) -> Path:
    if backend == LLM:
        return LLM_DIR / "venv" / "bin" / "python"
    if backend == FIZGIG:
        return TRAINER_ROOT / "vendor" / "Fizgig" / "venv" / "bin" / "python"
    if backend == SDS:
        return SDSCRIPTS_PYTHON
    for v in (".venv", "venv"):
        for exe in ("python3", "python"):
            if (AITK_DIR / v / "bin" / exe).is_file():
                return AITK_DIR / v / "bin" / exe
    return AITK_DIR / ".venv" / "bin" / "python3"


BACKENDS = {
    FIZGIG: {"title": "Fizgig", "install": "fizgig", "covers": "Krea 2 · MiniMax H3 · Qwen Image 2.1 · Flux.2 Klein · MOSS-Image",
             "note": "Qwen Image 2.1 and MOSS-Image run from vendor/Fizgig-latest (v8.7.9) with the same venv."},
    SDS: {"title": "kohya sd-scripts", "install": None,
          "covers": "Anima · SDXL · SD 1.5 · Flux.1/Chroma · SD3 · Lumina 2 · HunyuanImage",
          "note": "Uses the checkout and ROCm venv in TRAINER_SDSCRIPTS_DIR (docker-compose.override.yml)."},
    AITK: {"title": "AI Toolkit", "install": "ltx", "covers": "LTX 2.5 + every upstream example recipe (Wan, Flux, HiDream, Qwen-Image…)"},
    LLM: {"title": "LLM trainer", "install": "llm", "covers": "Chat/text LLMs (Qwen, Llama, Gemma, Mistral…) → PEFT + GGUF LoRA",
          "note": "PEFT on the container's ROCm PyTorch; llama.cpp's converter makes the GGUF LoRA."},
}


# ---- LLM base models ----------------------------------------------------------------------------

_gguf_cache: dict[str, tuple[float, int, dict[str, str]]] = {}
_GGUF_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def gguf_general(path: Path) -> dict[str, str]:
    """The general.* string metadata of a GGUF (name, base_model.N.repo_url, source.url…). Quantisers append
    some of it after the vocab arrays, so the whole key/value block is read (cached by mtime/size)."""
    try:
        st = path.stat()
    except OSError:
        return {}
    hit = _gguf_cache.get(str(path))
    if hit and hit[:2] == (st.st_mtime, st.st_size):
        return hit[2]
    out: dict[str, str] = {}
    try:
        with path.open("rb", buffering=1 << 20) as f:
            if f.read(4) != b"GGUF":
                raise ValueError
            f.read(4 + 8)
            count = struct.unpack("<Q", f.read(8))[0]

            def text() -> str:
                n = struct.unpack("<Q", f.read(8))[0]
                if n > 1 << 20:
                    raise ValueError
                return f.read(n).decode("utf-8", "replace")
            for _ in range(min(count, 4096)):
                key = text()
                kind = struct.unpack("<I", f.read(4))[0]
                if kind == 8:
                    value = text()
                    if key.startswith("general."):
                        out[key] = value[:300]
                elif kind == 9:
                    sub_kind, n = struct.unpack("<IQ", f.read(12))
                    for _ in range(n):
                        text() if sub_kind == 8 else f.seek(_GGUF_SIZES[sub_kind], 1)
                else:
                    f.seek(_GGUF_SIZES[kind], 1)
    except (OSError, ValueError, KeyError, struct.error):
        pass
    _gguf_cache[str(path)] = (st.st_mtime, st.st_size, out)
    return out


def _hf_repo(url: str) -> str:
    m = re.match(r"https?://huggingface\.co/([\w.-]+/[\w.-]+)/?$", url.strip())
    return m.group(1) if m else ""


def llm_bases(model_root: Path) -> list[dict[str, str]]:
    """Base models the LLM family can train: local Hugging Face folders under models/llm, repos already in
    the Trainer's Hugging Face cache, and the unquantised source/base repo of every installed GGUF."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(value: str, label: str, note: str) -> None:
        if value and value not in seen:
            seen.add(value)
            out.append({"value": value, "label": label, "note": note})
    root = model_root / "llm"
    ggufs = []
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            rel = Path(dirpath).relative_to(model_root)
            if len(rel.parts) > 3:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in {"build", "src", "ggml", "tools"}]
            if "config.json" in filenames and any(n.endswith(".safetensors") for n in filenames):
                add(str(rel), f"{rel.name} (local folder)", "Hugging Face weights in models/" + str(rel))
            ggufs += [Path(dirpath) / n for n in filenames
                      if n.endswith(".gguf") and not re.search(r"mmproj|^ggml-vocab", n, re.I)]
    hub = HF_HOME / "hub"
    if hub.is_dir():
        for repo in sorted(hub.glob("models--*")):
            snaps = [s for s in (repo / "snapshots").glob("*") if (s / "config.json").is_file() and any(s.glob("*.safetensors"))]
            arch = ""
            if snaps:
                try:
                    arch = " ".join(json.loads((snaps[0] / "config.json").read_text()).get("architectures") or [])
                except (OSError, ValueError):
                    pass
            if snaps and re.search(r"CausalLM|ConditionalGeneration", arch):
                add(repo.name[len("models--"):].replace("--", "/", 1), "", "already downloaded")
    for path in sorted(ggufs, key=lambda p: p.name.casefold()):
        meta = gguf_general(path)
        source = _hf_repo(meta.get("general.source.url", ""))
        base = _hf_repo(meta.get("general.base_model.0.repo_url", ""))
        if source:
            add(source, "", f"the unquantised weights of your {path.name}: the LoRA fits it exactly")
        if base:
            add(base, "", f"base model of your {path.name}" + (" (the LoRA also loads on the finetune, a little less exactly)" if source else ""))
    return out


# ---- scanning ----------------------------------------------------------------------------------

def classify(path: Path, rel: str) -> dict[str, Any]:
    name = path.name
    h = read_header(path)
    if h is None:
        if path.suffix.lower() == ".gguf":
            fam = next((f for f in FAMILIES if f.name_hint and re.search(f.name_hint, name, re.I)), None)
            return {"family": fam.id if fam else None, "variant": "GGUF", "verdict": UNSUPPORTED,
                    "reason": "GGUF quantised file: the trainers need .safetensors"}
        return {"family": None, "variant": "", "verdict": UNSUPPORTED, "reason": "not a readable .safetensors file"}
    for fam in FAMILIES:
        if fam.detect and (hit := fam.detect(h, name)):
            variant, verdict, reason = hit
            # A detector may route the file to another family (Flux.2 dev, Qwen-Image v1…): keep it unsupported.
            return {"family": fam.id, "variant": variant, "verdict": verdict, "reason": reason}
    for det in _OTHER_DETECTORS:
        if hit := det(h, name):
            return {"family": None, "variant": hit[0], "verdict": UNSUPPORTED, "reason": hit[2]}
    return {"family": None, "variant": "", "verdict": UNSUPPORTED,
            "reason": "architecture not recognised by any installed trainer"}


def _walk(model_root: Path, folders: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for folder in folders:
        root = model_root / folder
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            rel_dir = Path(dirpath).relative_to(model_root)
            if len(rel_dir.parts) > 3:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if Path(name).suffix.lower() in MODEL_EXTS and not name.startswith("."):
                    found.append(str(rel_dir / name))
    return sorted(set(found), key=str.casefold)


def scan(model_root: Path) -> dict[str, Any]:
    """Every family with its candidate files per slot and an overall state, plus base models no
    family can train. Header reads are cached by mtime/size, so repeat scans are cheap."""
    base_files = {rel: classify(model_root / rel, rel) for rel in _walk(model_root, BASE_FOLDERS)}
    folder_cache: dict[tuple[str, ...], list[str]] = {}
    aitk = ai_toolkit_installed()
    families = []
    for fam in all_families():
        py = backend_python(fam.backend)
        backend_ok = py.is_file() and (fam.backend != SDS or SDSCRIPTS_DIR.is_dir())
        slots, missing, untested_only, usable_base = [], [], [], 0
        for slot in fam.slots:
            if slot.base:
                cands = [{"file": rel, **info} for rel, info in base_files.items() if info["family"] == fam.id]
                if fam.extra.get("finetune"):
                    for c in cands:
                        c["ft_reason"] = finetune_unfit(model_root / c["file"], fam.id)
            else:
                files = folder_cache.setdefault(slot.folders, _walk(model_root, slot.folders))
                rx = re.compile(slot.suggest, re.I) if slot.suggest else None
                cands = []
                for rel in files:
                    if rx and not rx.search(rel):
                        continue
                    verdict, reason = (READY, "")
                    if slot.check:
                        verdict, reason = slot.check(read_header(model_root / rel), rel)
                    cands.append({"file": rel, "verdict": verdict, "reason": reason})
            usable = [c for c in cands if c["verdict"] != UNSUPPORTED]
            if slot.base:
                usable_base = len(usable)
            if slot.required and not usable:
                missing.append(slot.key)
            elif slot.required and not any(c["verdict"] == READY for c in usable):
                untested_only.append(slot.key)
            slots.append({"key": slot.key, "label": slot.label, "folders": list(slot.folders), "required": slot.required,
                          "base": slot.base, "suggest": slot.suggest, "note": slot.note, "candidates": cands,
                          "get": slot.get, "missing": slot.required and not usable,
                          "untested_only": slot.key in untested_only})
        has_base = any(s["candidates"] for s in slots if s["base"])
        if fam.id.startswith("aitk:") or fam.backend == LLM:
            state = "ready" if backend_ok else "needs_trainer"
        elif not has_base and any(s.base and s.required for s in fam.slots):
            state = "not_installed"
        elif not backend_ok:
            state = "needs_trainer"
        elif missing:
            state = "needs_files"
        else:
            state = "ready"
        families.append({
            "id": fam.id, "title": fam.title, "backend": fam.backend, "backend_title": BACKENDS[fam.backend]["title"],
            "group": fam.group, "media": sorted(fam.media), "knobs": list(fam.knobs), "defaults": fam.defaults,
            "hint": fam.hint, "state": state, "missing": missing, "untested_only": untested_only, "slots": slots, "recipe": fam.id.startswith("aitk:"),
            **({"bases": llm_bases(model_root)} if fam.backend == LLM else {}),
            **({"finetune": {**fam.extra["finetune"], "note": FT_NOTE}} if fam.extra.get("finetune") else {}),
        })
    others = [{"file": rel, **info} for rel, info in base_files.items() if info["family"] is None]
    return {"families": families, "unrecognised": others, "ai_toolkit": aitk,
            "backends": {k: {**v, "python": str(backend_python(k)), "installed": backend_python(k).is_file()} for k, v in BACKENDS.items()}}
