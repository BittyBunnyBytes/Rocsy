from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


MIN_SIZE = 512
MAX_SIZE = 1536
DEFAULT_SIZE = 1024

# Selectable image models. "krea2" and "lustify" drive the Krea2-Advanced-LoRA
# template (Lustify v10 is a Krea 2 finetune -- only the DiT file differs);
# "qwen_image_21" is built node-by-node in backend/qwen.py.
MODEL_KREA2 = "krea2"
MODEL_LUSTIFY = "lustify"
MODEL_QWEN_IMAGE_21 = "qwen_image_21"
MODEL_ANIMA = "anima"
MODEL_MOSS = "moss"
MOSS_FAST_CHECKPOINT = "moss_image_sft_bf16.safetensors"
MOSS_FAST_MODES = {"dmd8": ("lodestones-8step", 8), "dmad4": ("rapid-moss-4step", 4)}
ANIMA_VERSIONS = {
    "base": ("anima_baseV10.safetensors", "Base v1.0"),
    "aesthetic": ("anima_aestheticV11.safetensors", "Aesthetic v1.1"),
    "turbo": ("anima_turboV10.safetensors", "Turbo v1.0"),
    "preview_29": ("Anima-2.9B-preview-v1.safetensors", "2.9B preview v1"),
    "v38": ("Anima-3.8B-v1.1.safetensors", "3.8B v1.1"),
}
ModelId = Literal["krea2", "lustify", "qwen_image_21", "anima", "moss"]
# DiT loaded into the template's UNETLoader per Krea-family model.
KREA_UNET_NAMES = {
    MODEL_KREA2: "krea2_turbo_int8_convrot.safetensors",
    MODEL_LUSTIFY: "lustifyNSFWCheckpoint_v10Krea2_fp8.safetensors",
}

# Qwen-Image 2.1 renders natively up to 2K (2048x2048 .. 2752x1536); the
# pixel-area cap keeps a single render inside the 24GB card's budget.
QWEN_MAX_SIDE = 2752
QWEN_MAX_PIXELS = 2752 * 1536
QWEN_REFERENCE_SLOT_COUNT = 10
QWEN_DEFAULT_STEPS = 25
QWEN_DEFAULT_CFG = 1.0

MIN_SEED = 0
MAX_SEED = 0xFFFFFFFFFFFFFFFF

try:
    from . import catalog_local as _local  # this machine's checkpoints/LoRAs (git-ignored)
except ImportError:
    _local = None

# LoRA slots of the template's Power Lora Loader (rgthree, node 4), by position: requests send
# enable/strength per index, so slots only ever get appended. They come from catalog_local.py
# (this machine's own LoRAs, git-ignored); a fresh install starts empty and adds LoRAs in
# Models or as custom LoRAs.
LORA_SLOTS = _local.LORA_SLOTS if _local else []
DEFAULT_LORA_ENABLED = tuple(index == 0 for index in range(len(LORA_SLOTS)))

# Qwen-Image 2.1 LoRAs (from catalog_local.py like LORA_SLOTS). Positional: only append.
# Applied as a LoraLoaderModelOnly chain.
QWEN_LORA_SLOTS = _local.QWEN_LORA_SLOTS if _local else []
DEFAULT_QWEN_LORA_ENABLED = (False,) * len(QWEN_LORA_SLOTS)
MOSS_LORA_SLOTS: list[str] = []
ANIMA_LORA_SLOTS: list[str] = _local.ANIMA_LORA_SLOTS if _local else []


def lora_slots_for(model: str) -> list[str]:
    return {"krea": LORA_SLOTS, "qwen": QWEN_LORA_SLOTS,
            "moss": MOSS_LORA_SLOTS, "anima": ANIMA_LORA_SLOTS}[model]


def lora_family(model: str) -> str:
    return {MODEL_KREA2: "krea", MODEL_LUSTIFY: "krea",
            MODEL_QWEN_IMAGE_21: "qwen", MODEL_MOSS: "moss", MODEL_ANIMA: "anima"}[model]

# Per LoRA slot: display name, source page, recommended strength and trigger
# words - shown in the LoRA list and the LoRA gallery (static/lora-gallery,
# built by Video's tools/fetch_lora_gallery.py --root ~/AI/Krea).
# Source pages were found by the files' SHA-256 (Civitai by-hash API).
LORA_INFO = _local.LORA_INFO if _local else {}

# Speed for Krea 2 / Lustify: the template's 8 Turbo steps, or lvladikov's
# step-distillation LoRAs for Krea 2 Turbo (HF lvladikov/Krea2-Turbo-Distill-{4,2}step-LoRA,
# rank 64, 228/228 modules fit krea2_turbo_int8_convrot). Author settings: euler /
# simple, cfg 1, strength 1.0 (0.75 if 4-step looks too textured).
TURBO_LORAS = {
    "turbo4": ("krea2_turbo_4step_rank_64_lora_comfyui.safetensors", 4),
    "turbo2": ("krea2_turbo_2step_rank_64_lora_comfyui.safetensors", 2),
}
STANDARD_STEPS = 8
# Qwen-Image 2.1 speed: Viggle's DMD-distilled turbo LoRA (HF Viggle/Qwen-Image-2.1-viggle-turbo,
# v0.2.1 rank 128). Author settings: 6 steps on fixed raw sigma nodes with the pipeline's
# resolution shift, euler, no CFG, strength 1.0. Benchmarked 2026-09-27 at 1024²:
# 8.7 s vs 22.9 s for 25 base steps, quality equivalent on a portrait test.
QWEN_VIGGLE_LORA = "Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors"
QWEN_VIGGLE_NODES = (1.0, 0.9375, 0.875, 0.75, 0.5, 0.25)
# Qwen-Image-2.1-Turbo (Comfy-Org qwen_image_2.1_turbo_*): Qwen's 8-step distilled
# denoiser. Official template image_qwen_image_2_1_turbo_t2i: euler, CFG 1, these fixed
# sigmas (model_index.json sample_sigmas + 0; scheduler shift 1.0, no dynamic shift).
QWEN_TURBO_SIGMAS = (1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0)
# Qwen-Image text encoders: Comfy-Org's int8 Qwen3-VL-8B, or AtomicChat's abliterated one
# (HF Zyszztggd/Qwen-Image-2.1-Turbo-Uncensored-GGUF, BF16). That GGUF is the language model
# only, so ComfyUI took it for a Flux2/Klein Qwen3-8B (12288-wide features). It was rebuilt
# 2026-10-13 as a native BF16 safetensors: GGUF language model + the unmodified BF16 vision
# tower of the int8 file, so CLIPLoader detects Qwen3-VL-8B. Works with base and Turbo.
QWEN_TEXT_ENCODERS = {
    "standard": "qwen3vl_8b_int8_convrot.safetensors",
    "abliterated": "qwen3vl_8b_abliterated_bf16.safetensors",
}
QWEN_ABLITERATED_GGUF = "Qwen-Image-2.1-Turbo-Abliterated-Uncensored-BF16.gguf"
# Images per generation: one ComfyUI prompt each (seed, seed+1, ...), not one
# big latent batch - 16-20 images at once wouldn't fit in VRAM.
BATCH_COUNTS = (1, 2, 4, 8, 16, 20)
MAX_BATCH = max(BATCH_COUNTS)

# Krea 2 / Lustify: TextEncodeQwenImageEditPlus takes at most 3 images. The
# slot strength is the krea2_style_reference LoRA weight (strongest slot wins).
REFERENCE_SLOT_COUNT = 3
DEFAULT_REFERENCE_STRENGTHS = (1.0, 1.0, 1.0)
KREA_STYLE_REFERENCE_LORA = "krea2_style_reference.safetensors"


class LoraSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    strength: float = Field(default=0.8, ge=0.0, le=2.0)


class ReferenceSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Filename already uploaded into ComfyUI's input directory via POST /api/upload.
    image: str | None = Field(default=None, max_length=255)
    strength: float = Field(default=1.0, ge=0.0, le=2.0)
    # Pixel size read client-side at upload time. Only used by the Qwen prompt
    # enhancer when it asks to follow the aspect ratio of <image2>..<image10>.
    width: int | None = Field(default=None, ge=1, le=16384)
    height: int | None = Field(default=None, ge=1, le=16384)

    @field_validator("image")
    @classmethod
    def no_path_traversal(cls, value: str | None) -> str | None:
        if value and ("/" in value or "\\" in value or ".." in value):
            raise ValueError("Reference image must be a bare filename already uploaded via /api/upload.")
        return value


def _round_to_multiple(value: int, multiple: int = 32) -> int:
    return max(multiple, round(value / multiple) * multiple)


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: ModelId = MODEL_KREA2
    checkpoint: str | None = Field(default=None, max_length=255)
    anima_version: Literal["base", "aesthetic", "turbo", "preview_29", "v38"] = "aesthetic"
    anima_steps: int | None = Field(default=None, ge=1, le=100)
    anima_cfg: float = Field(default=4.0, ge=1.0, le=20.0)
    moss_steps: int = Field(default=50, ge=1, le=100)
    moss_cfg: float = Field(default=3.0, ge=1.0, le=20.0)
    prompt: str = Field(min_length=1, max_length=4_000)
    # Per-model upper bounds are enforced in model_limits() below.
    width: int = Field(default=DEFAULT_SIZE, ge=MIN_SIZE, le=QWEN_MAX_SIDE)
    height: int = Field(default=DEFAULT_SIZE, ge=MIN_SIZE, le=QWEN_MAX_SIDE)
    seed: int | None = Field(default=None, ge=MIN_SEED, le=MAX_SEED)
    enhance: bool = False
    use_references: bool = False
    references: list[ReferenceSlot] = Field(default_factory=list)
    loras: list[LoraSlot] = Field(default_factory=list)
    # Krea 2 / Lustify only (see TURBO_LORAS); ignored for Qwen.
    # standard / turbo4 / turbo2 = Krea 2 and Lustify; viggle = Qwen-Image 2.1 (6 steps).
    speed: Literal["standard", "turbo4", "turbo2", "viggle", "dmd8", "dmad4"] = "standard"
    turbo_strength: float = Field(default=1.0, ge=0.25, le=1.5)
    # Krea 2 / Lustify sampling steps; None = the speed's own count (8 / 4 / 2).
    krea_steps: int | None = Field(default=None, ge=1, le=30)
    count: int = Field(default=1, ge=1, le=MAX_BATCH)

    # Qwen-Image 2.1 only (ignored for Krea 2).
    negative_prompt: str = Field(default="", max_length=4_000)
    steps: int = Field(default=QWEN_DEFAULT_STEPS, ge=1, le=100)
    # CFG below 1 is not usable on Qwen-Image 2.1.
    cfg: float = Field(default=QWEN_DEFAULT_CFG, ge=1.0, le=20.0)
    transparent: bool = False
    # Edit mode: total pixel budget each reference is resized to (0 = native).
    reference_resolution: int = Field(default=1024, ge=0, le=2048)
    # Edit mode: render at the first reference's aspect ratio instead of width/height.
    match_reference_size: bool = True
    prompt_enhancer: bool = False
    enhancer_picks_size: bool = True
    enhancer_thinking: bool = False
    kv_cache_device: Literal["auto", "gpu", "cpu", "off"] = "auto"
    kv_cache_dtype: Literal["default", "int8", "int4"] = "default"
    text_encoder: Literal["standard", "abliterated"] = "standard"

    @field_validator("prompt")
    @classmethod
    def prompt_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a prompt.")
        return value

    @field_validator("references")
    @classmethod
    def references_bounded(cls, value: list[ReferenceSlot], info: ValidationInfo) -> list[ReferenceSlot]:
        limit = QWEN_REFERENCE_SLOT_COUNT if info.data.get("model") == MODEL_QWEN_IMAGE_21 else REFERENCE_SLOT_COUNT
        # Older Krea presets/jobs carry a 4th slot; drop trailing slots that are off.
        while len(value) > limit and not (value[-1].enabled and value[-1].image):
            value = value[:-1]
        if len(value) > limit:
            raise ValueError(f"At most {limit} reference images are supported.")
        if info.data.get("use_references", False):
            for slot in value:
                if slot.enabled and not slot.image:
                    raise ValueError("An enabled reference slot needs an uploaded image.")
        return value

    @field_validator("loras")
    @classmethod
    def loras_bounded(cls, value: list[LoraSlot], info: ValidationInfo) -> list[LoraSlot]:
        slots = lora_slots_for(lora_family(info.data.get("model", MODEL_KREA2)))
        if len(value) > len(slots):
            raise ValueError(f"At most {len(slots)} LoRA slots are supported.")
        if any(slot.enabled and LORA_INFO.get(slots[index], {}).get("deleted") for index, slot in enumerate(value)):
            raise ValueError("A removed LoRA cannot be enabled.")
        return value

    @model_validator(mode="after")
    def model_limits(self) -> "GenerationRequest":
        if self.model == MODEL_MOSS:
            if self.width > MAX_SIZE or self.height > MAX_SIZE:
                raise ValueError(f"MOSS-Image supports sizes up to {MAX_SIZE}px per side in this service.")
            if self.use_references:
                raise ValueError("MOSS-Image references are not configured in this image service yet.")
            if self.speed not in {"standard", *MOSS_FAST_MODES} or self.enhance:
                raise ValueError("Choose Standard, 8-step DMD, or 4-step DMAD for MOSS-Image.")
            return self
        if self.model == MODEL_ANIMA:
            if self.width > MAX_SIZE or self.height > MAX_SIZE:
                raise ValueError(f"Anima supports sizes up to {MAX_SIZE}px per side.")
            if self.use_references:
                raise ValueError("Anima references are not configured in this image service yet.")
            if self.speed != "standard":
                raise ValueError("Anima uses its version's own sampling speed.")
            return self
        if self.speed in MOSS_FAST_MODES:
            raise ValueError("MOSS few-step modes require the MOSS-Image model.")
        if self.model != MODEL_QWEN_IMAGE_21:
            if self.width > MAX_SIZE or self.height > MAX_SIZE:
                raise ValueError(f"Krea 2 models support sizes up to {MAX_SIZE}px per side.")
        else:
            width, height = self.normalized_size()
            if width * height > QWEN_MAX_PIXELS:
                raise ValueError(f"Qwen-Image 2.1 supports up to {QWEN_MAX_PIXELS / 1e6:.1f} megapixels (e.g. 2048x2048 or 2752x1536).")
        return self

    @property
    def is_qwen(self) -> bool:
        return self.model == MODEL_QWEN_IMAGE_21

    @property
    def active_references(self) -> list[ReferenceSlot]:
        if not self.use_references:
            return []
        return [ref for ref in self.references if ref.enabled and ref.image]

    def normalized_size(self) -> tuple[int, int]:
        return _round_to_multiple(self.width), _round_to_multiple(self.height)


class PresetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: ModelId
    name: str = Field(min_length=1, max_length=60)
    # Form settings minus prompt/seed/reference images (the frontend's presetSnapshot).
    settings: dict = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a preset name.")
        return value


class PresetRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: ModelId
    name: str = Field(min_length=1, max_length=60)
    new_name: str = Field(min_length=1, max_length=60)

    @field_validator("new_name")
    @classmethod
    def new_name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a preset name.")
        return value


class Job(BaseModel):
    id: str
    prompt_id: str | None = None
    model: str = MODEL_KREA2
    prompt: str
    enhanced_prompt: str | None = None
    seed: int
    width: int
    height: int
    enhance: bool
    state: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    output_url: str | None = None
    download_url: str | None = None
    error: str | None = None
    progress: int = 0
