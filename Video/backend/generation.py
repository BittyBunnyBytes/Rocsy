from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .config import Settings, local_variant
from .models import ASPECTS, LTX_WORKFLOW_FILES, MINIMAX_H3_LORA_SLOTS, MODES, WAN_NATIVE_FPS, GenerationRequest, wan_lora_experts


class GenerationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def calculate_resolution(megapixels: float, aspect: str) -> tuple[int, int]:
    ratio = ASPECTS[aspect]
    # Floor of 64, the smallest width/height a custom resolution allows.
    width = max(64, round(math.sqrt(megapixels * 1_000_000 * ratio) / 32) * 32)
    height = max(64, round(width / ratio / 32) * 32)
    return int(width), int(height)


def calculate_ltx_resolution(megapixels: float, aspect: str) -> tuple[int, int]:
    ratio = ASPECTS[aspect]
    width = round(math.sqrt(megapixels * 1_000_000 * ratio) / 64) * 64
    height = round(math.sqrt(megapixels * 1_000_000 / ratio) / 64) * 64
    scale = min(1.0, 2048 / max(width, height))
    width = max(256, min(2048, round(width * scale / 64) * 64))
    height = max(256, min(2048, round(height * scale / 64) * 64))
    return int(width), int(height)


def calculate_wan_resolution(megapixels: float, aspect: str) -> tuple[int, int]:
    """Wan's VAE works on a 16 px grid, so 16:9 at 0.92 MP is a real 1280x720."""
    ratio = ASPECTS[aspect]
    width = max(64, round(math.sqrt(megapixels * 1_000_000 * ratio) / 16) * 16)
    height = max(64, round(width / ratio / 16) * 16)
    return int(width), int(height)


# MiniMax H3's own reference-conditioning nodes expect canonical <Picture N> /
# <Video N> / <Audio N> tokens in the prompt (see MiniMaxH3ReferenceToVideo's
# docstring). Users type those tokens directly rather than an @-alias, but
# each reference's own number (from its stored alias, e.g. "image3") is
# stable across deletions of *other* references, while the number MiniMax H3
# actually needs is the reference's contiguous position among references of
# its own type at generation time. This maps the stable token the user typed
# to the positional token the API needs, so removing <Picture 2> never
# silently changes what <Picture 3> in an existing prompt refers to.
# "Image" is accepted as a synonym for "Picture" - MiniMax H3's own canonical
# word (per MiniMaxH3ReferenceToVideo's docstring: "<Picture i> / <Video k> /
# <Audio j>"), but "image" is the far more natural word for someone typing a
# prompt by hand rather than using the UI's insert-token button. Confirmed
# 2026-09-22: a ref2v prompt that said "<Image 1>" passed through completely
# unrecognized (regex only matched Picture/Video/Audio), so the model never
# saw a valid reference token for its main character-replacement instruction
# even though the image itself was correctly wired into ref_images - the
# output was a coherent video that just ignored the reference identity.
REFERENCE_TOKEN_RE = re.compile(r"<\s*(Picture|Image|Video|Audio)\s+(\d+)\s*>", re.IGNORECASE)
_REFERENCE_TOKEN_ALIASES = {"image": "picture"}
REFERENCE_CATEGORY_LABELS = {"image": "Picture", "video": "Video", "audio": "Audio"}


def reference_category(ref: dict[str, Any]) -> str:
    """image/video/audio, derived from the validated upload mime type."""
    return (ref.get("mime_type") or "").split("/", 1)[0]


def _stable_alias_number(alias: str) -> str | None:
    match = re.search(r"(\d+)$", alias or "")
    return match.group(1) if match else None


def reference_token(ref: dict[str, Any]) -> str | None:
    """The canonical <Picture N>/<Video N>/<Audio N> token this reference is
    shown as everywhere (UI cards, autocomplete, Qwen enhance labels) -
    stable for as long as the reference exists, per _stable_alias_number."""
    # An audio-only video is sent to H3 as audio, so it is shown and written as <Audio N>.
    label = REFERENCE_CATEGORY_LABELS.get("audio" if ref.get("audio_only") else reference_category(ref))
    number = _stable_alias_number(ref.get("alias", ""))
    return f"<{label} {number}>" if label and number else None


def effective_duration(ref: dict[str, Any]) -> float:
    """The trimmed length actually sent to MiniMax H3, not the raw upload
    length - a long upload is meant to be trimmed down after the fact rather
    than rejected outright (see the trim editor)."""
    duration = ref.get("duration")
    if duration is None:
        return 0.0
    start = max(0.0, ref.get("trim_start") or 0.0)
    end = ref.get("trim_end")
    end = duration if end is None else min(end, duration)
    return max(0.0, end - start)


def _canonical_alias_map(label: str, refs: list[dict[str, Any] | None]) -> dict[str, str]:
    """`refs` is in the exact order MiniMax H3 assigns <Label N> tokens.
    A None entry is a real numbered slot with no reference row of its own
    (an unmuted video's embedded audio track - see _audio_presentation_order)
    and is skipped since there's no stable alias to map it back to."""
    mapping: dict[str, str] = {}
    for index, ref in enumerate(refs):
        if ref is None:
            continue
        stable_number = _stable_alias_number(ref["alias"])
        if stable_number is not None:
            mapping[f"{label.lower()} {stable_number}"] = f"<{label} {index + 1}>"
    return mapping


def _audio_presentation_order(videos: list[dict[str, Any]], audios: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    """MiniMaxH3ReferenceToVideo assigns <Audio j> in presentation order: each
    unmuted video with its own audio track gets a slot (a None placeholder
    here - it isn't a separate uploaded reference) right before standalone
    audio references start counting. Muting a video removes its slot, which
    is why every existing <Audio N> in a prompt is re-derived at generation
    time rather than assigned once and cached."""
    order: list[dict[str, Any] | None] = [
        None for video in videos if not video.get("audio_only") and not video.get("muted") and video.get("has_audio")
    ]
    order.extend(video for video in videos if video.get("audio_only"))
    order.extend(audios)
    return order


def translate_reference_tokens(text: str, mapping: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        word = match.group(1).lower()
        word = _REFERENCE_TOKEN_ALIASES.get(word, word)
        key = f"{word} {match.group(2)}"
        return mapping.get(key, match.group(0))

    return REFERENCE_TOKEN_RE.sub(replace, text)


def alias_map(mode: str, references: list[dict[str, Any]]) -> dict[str, str]:
    if mode == "fl2v":
        return {"image": "<Picture 1>", "first": "<Picture 1>", "image1": "<Picture 1>", "last": "<Picture 2>", "image2": "<Picture 2>"}
    if mode == "ref2v":
        images = [ref for ref in references if reference_category(ref) == "image"]
        videos = [ref for ref in references if reference_category(ref) == "video"]
        audios = [ref for ref in references if reference_category(ref) == "audio"]
        mapping: dict[str, str] = {}
        mapping.update(_canonical_alias_map("Picture", images))
        visual_videos = [video for video in videos if not video.get("audio_only")]
        mapping.update(_canonical_alias_map("Video", visual_videos))
        audio_order = _audio_presentation_order(videos, audios)
        mapping.update(_canonical_alias_map("Audio", audio_order))
        for index, video in enumerate(audio_order):
            # Audio-only videos made before they were re-aliased still carry a "videoN" alias (and <Video N> tags).
            if video is not None and video.get("audio_only") and str(video.get("alias", "")).lower().startswith("video"):
                number = _stable_alias_number(video.get("alias", ""))
                if number is not None:
                    mapping[f"video {number}"] = f"<Audio {index + 1}>"
        return mapping
    return {}


def translate_aliases(text: str, mode: str, references: list[dict[str, Any]]) -> str:
    mapping = alias_map(mode, references)
    if mode == "ref2v":
        return translate_reference_tokens(text, mapping)
    return re.sub(r"@([A-Za-z0-9_]+)", lambda match: mapping.get(match.group(1).lower(), match.group(0)), text)


# The only checkpoint the ref2v workflow template's built-in turbo LoRA (node
# 15) and AdaLN fix (node 16) were tuned for - see build_workflow()'s bypass
# logic for why any other checkpoint skips both instead.
REF2V_TURBO_LORA_CHECKPOINT = "minimaxH3INT8INT4_ref2vaINT8Pruned.safetensors"

MAX_REFERENCE_IMAGES = 9
MAX_REFERENCE_VIDEOS = 3
MAX_REFERENCE_AUDIOS = 3
MAX_REFERENCE_VIDEO_SECONDS = 15.0
MAX_REFERENCE_AUDIO_SECONDS = 15.0


# Reference videos enter MiniMax H3's sequence frame-for-frame at their own
# size (MiniMaxH3ReferenceToVideo gives them a 768-short-edge canvas and never
# upscales, and unlike images there's no "match" option), so a reference the
# size of the output doubles the token count. "Downscale reference videos"
# (Speedup & Saves -> Savings) caps each one to a quarter of the output's
# pixel area (half per side). Measured 2026-09-24, 5s 800x448 with a 5s
# 800x448 scene reference: 170s capped vs 341s full size, same shot.
# (Earlier NaN renders with video refs turned out to be a degraded ComfyUI
# session - plain Text renders went NaN too until ComfyUI was restarted.)
REFERENCE_VIDEO_AREA_FRACTION = 0.25


def reference_video_width(path: Path, output_area: int) -> int:
    """LoadVideoUI custom_width for a reference video: 0 (native size) when
    it's already within REFERENCE_VIDEO_AREA_FRACTION of the output area,
    otherwise the width (multiple of 8, aspect kept) that fits it."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        width, height = (int(value) for value in probe.stdout.strip().split("x")[:2])
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise GenerationError("invalid_reference", "Could not read a reference video's size.") from exc
    scale = math.sqrt(output_area * REFERENCE_VIDEO_AREA_FRACTION / (width * height))
    if scale >= 1.0:
        return 0
    return max(64, int(width * scale) // 8 * 8)


def validate_reference_limits(references: list[dict[str, Any]]) -> None:
    """MiniMax H3's Ref2V node hard-caps each reference type (see
    MiniMaxH3ReferenceToVideo's ref_images/ref_videos/ref_audios Autogrow
    limits) and the combined clip length per type is capped independently
    of per-clip length. Checked again here (upload already enforces it) so a
    stale/edited request can't silently drop references instead of erroring."""
    images = [ref for ref in references if reference_category(ref) == "image"]
    videos = [ref for ref in references if reference_category(ref) == "video"]
    audios = [ref for ref in references if reference_category(ref) == "audio"]
    if len(images) > MAX_REFERENCE_IMAGES:
        raise GenerationError(
            "too_many_references",
            f"MiniMax H3 allows at most {MAX_REFERENCE_IMAGES} image references; {len(images)} are attached.",
        )
    if len(videos) > MAX_REFERENCE_VIDEOS:
        raise GenerationError(
            "too_many_references",
            f"MiniMax H3 allows at most {MAX_REFERENCE_VIDEOS} video references; {len(videos)} are attached.",
        )
    voice_videos = [ref for ref in videos if ref.get("audio_only")]
    visual_videos = [ref for ref in videos if not ref.get("audio_only")]
    if any(ref.get("muted") or not ref.get("has_audio") for ref in voice_videos):
        raise GenerationError("invalid_reference", "Audio-only conditioning requires an unmuted video with audio.")
    if len(audios) + len(voice_videos) > MAX_REFERENCE_AUDIOS:
        raise GenerationError(
            "too_many_references",
            f"MiniMax H3 allows at most {MAX_REFERENCE_AUDIOS} audio references; {len(audios) + len(voice_videos)} are attached.",
        )
    video_seconds = sum(effective_duration(ref) for ref in visual_videos)
    if video_seconds > MAX_REFERENCE_VIDEO_SECONDS + 1e-6:
        raise GenerationError(
            "reference_duration_exceeded",
            f"Video references total {video_seconds:.1f}s (after trim), over the {MAX_REFERENCE_VIDEO_SECONDS:.1f}s combined limit.",
        )
    audio_seconds = sum(effective_duration(ref) for ref in audios) + sum(min(5.0, effective_duration(ref)) for ref in voice_videos)
    if audio_seconds > MAX_REFERENCE_AUDIO_SECONDS + 1e-6:
        raise GenerationError(
            "reference_duration_exceeded",
            f"Audio references total {audio_seconds:.1f}s (after trim), over the {MAX_REFERENCE_AUDIO_SECONDS:.1f}s combined limit.",
        )


def stage(config: Settings, references: list[dict[str, Any]]) -> list[str]:
    target_dir = config.input_dir / "H3MachineRefs"
    target_dir.mkdir(parents=True, exist_ok=True)
    staged: list[str] = []
    for ref in references:
        source = (config.upload_dir / ref["stored_name"]).resolve()
        try:
            source.relative_to(config.upload_dir.resolve())
        except ValueError as exc:
            raise GenerationError("invalid_reference", "A reference path is invalid.") from exc
        if not source.is_file():
            raise GenerationError("missing_reference", f"Reference @{ref['alias']} is no longer available.")
        target_name = f"{uuid.uuid4().hex[:12]}{source.suffix.lower()}"
        shutil.copy2(source, target_dir / target_name)
        staged.append(f"H3MachineRefs/{target_name}")
    return staged


def _test_loras(request: Any) -> list[tuple[str, float]]:
    """The Trainer's epoch-test LoRA (one training checkpoint), loaded after the user's stack."""
    return [(request.test_lora, request.test_lora_strength)] if getattr(request, "test_lora", None) else []


def _minimax_h3_lora_stack_data(loras: list[Any], extra: list[tuple[str, float]] = ()) -> str:
    """JSON-encodes a request's LoRA slots into the exact `stack_data` shape
    DaSiWa_LTX2LoraLoader expects (confirmed via ComfyUI's own /object_info):
    one object per known slot, in MINIMAX_H3_LORA_SLOTS order, padding any
    slot the request omitted with disabled/default. `extra` rows (the epoch
    test LoRA) follow the slots; the node applies any number of rows."""
    padded = list(loras) + [None] * (len(MINIMAX_H3_LORA_SLOTS) - len(loras))
    entries = []
    for slot, (filename, _label) in zip(padded, MINIMAX_H3_LORA_SLOTS):
        enabled = bool(slot and slot.enabled)
        strength = slot.strength if slot else 1.0
        # vs/as are the node's video/audio MULTIPLIERS on top of str (its
        # _apply_slot uses str * vs), not strengths - they used to be set to
        # `strength` too, which silently squared every LoRA's real weight.
        entries.append({"on": enabled, "lora": filename if enabled else "None", "str": strength, "vs": 1.0, "as": 1.0})
    entries += [{"on": True, "lora": filename, "str": strength, "vs": 1.0, "as": 1.0} for filename, strength in extra]
    return json.dumps(entries)


def _wire_minimax_h3_loras(
    workflow: dict[str, Any], loras: list[Any], model_node: str, clip_node: str,
    consumers: list[tuple[str, str]], next_id: int, extra: list[tuple[str, float]] = (),
) -> int:
    """Inserts a DaSiWa_LTX2LoraLoader node reading model_node's output and
    repoints each (node_id, input_key) in `consumers` to read from it instead.
    No-op, leaving the workflow's existing wiring untouched, when every LoRA
    slot is disabled and there is no `extra` (epoch test) LoRA - this node
    isn't part of the base API templates."""
    if not any(slot.enabled for slot in loras) and not extra:
        return next_id
    loader_id = str(next_id)
    next_id += 1
    workflow[loader_id] = {
        "class_type": "DaSiWa_LTX2LoraLoader",
        "inputs": {
            "model": [model_node, 0], "clip": [clip_node, 0],
            "stack_data": _minimax_h3_lora_stack_data(loras, extra), "model_type": "Basic",
        },
        "_meta": {"title": "Video LoRA stack"},
    }
    for node_id, input_key in consumers:
        workflow[node_id]["inputs"][input_key] = [loader_id, 0]
    return next_id


# Diffusion loaders -> their ComfyUI-GGUF counterparts, for .gguf checkpoints.
_GGUF_LOADERS = {"UNETLoader": "UnetLoaderGGUF", "UNETLoaderDisTorch2MultiGPU": "UnetLoaderGGUFDisTorch2MultiGPU"}


def use_gguf_loaders(workflow: dict[str, Any]) -> None:
    """Loads .gguf diffusion models with ComfyUI-GGUF: the final graph's loaders (after the checkpoint
    choice, the multi-GPU rewrite and the Advanced workflow overrides) swap class. GGUF loaders have no
    weight_dtype; the DisTorch2 split keeps its allocation inputs."""
    for node in workflow.values():
        if not isinstance(node, dict) or not str(node.get("inputs", {}).get("unet_name", "")).lower().endswith(".gguf"):
            continue
        kind = node.get("class_type")
        if kind == "AMDMinimaxH3SequenceParallelLoader":
            raise GenerationError("workflow_incompatible",
                                  "GGUF checkpoints can't use the parallel GPU split. Pick another Multi-GPU setting.")
        if kind in _GGUF_LOADERS:
            node["class_type"] = _GGUF_LOADERS[kind]
            node["inputs"].pop("weight_dtype", None)


def _apply_h3_multi_gpu(workflow: dict[str, Any], extent: str, split_gb: int = 8) -> None:
    """Route H3 stages or a share of diffusion weights to GPU 1.

    Stage selectors are built into this ComfyUI installation; weight splitting
    uses the installed DisTorch2 loader. Rewire every consumer, including
    dynamically inserted LoRA and upscale nodes, so selected stages use GPU 1.
    """
    if extent == "off":
        return
    if extent == "parallel":
        loaders = [node for node in workflow.values() if node.get("class_type") == "UNETLoader"]
        if not loaders:
            raise GenerationError("workflow_incompatible", "H3 parallel split requires a diffusion loader.")
        for node in loaders:
            node["class_type"] = "AMDMinimaxH3SequenceParallelLoader"
            node["inputs"] = {
                "unet_name": node["inputs"]["unet_name"],
                "weight_dtype": "default",
                "devices": "0,1",
                "port": 29541,
            }
            node.setdefault("_meta", {})["title"] = "H3 parallel split: GPU 0 + GPU 1 (1 GiB GPU 1 free target)"
        return
    if extent == "split":
        loaders = [node for node in workflow.values() if node.get("class_type") == "UNETLoader"]
        if not loaders:
            raise GenerationError("workflow_incompatible", "H3 split offloading requires a diffusion loader.")
        for node in loaders:
            node["class_type"] = "UNETLoaderDisTorch2MultiGPU"
            node["inputs"].update(
                compute_device="cuda:0", donor_device="cuda:1",
                virtual_vram_gb=float(split_gb),
                expert_mode_allocations=f"cuda:1,{split_gb}g;cuda:0,*",
                eject_models=True,
            )
            node.setdefault("_meta", {})["title"] = f"H3 split offload: up to {split_gb} GiB on GPU 1"
        return
    selected = {"1": ("SelectCLIPDevice", "clip")}
    if extent == "conditioning_vae":
        selected.update({"2": ("SelectVAEDevice", "vae"), "3": ("SelectVAEDevice", "vae")})
    for source_id, (class_type, input_name) in selected.items():
        if source_id not in workflow:
            raise GenerationError("workflow_incompatible", f"H3 GPU offloading requires loader node {source_id}.")
        selector_id = str(max(map(int, workflow)) + 1)
        for node in workflow.values():
            for key, value in node.get("inputs", {}).items():
                if isinstance(value, list) and len(value) == 2 and value[0] == source_id:
                    node["inputs"][key] = [selector_id, value[1]]
        workflow[selector_id] = {
            "class_type": class_type,
            "inputs": {input_name: [source_id, 0], "device": "gpu:1"},
            "_meta": {"title": f"H3 {input_name} on GPU 1"},
        }


def build_workflow(config: Settings, request: GenerationRequest, references: list[dict[str, Any]], frames: list[dict[str, Any]]) -> tuple[dict[str, Any], int, int]:
    if request.mode == "fl2v" and not frames:
        raise GenerationError("invalid_reference", "A first frame is required for Keyframe mode.")
    if request.mode == "ref2v" and not references:
        raise GenerationError("invalid_reference", "At least one reference (image, video, or audio) is required for Reference mode.")
    if not config.workflow_path.is_file():
        raise GenerationError("workflow_missing", "The MiniMax H3 workflow file is not configured on this server.")
    try:
        workflow = json.loads(config.workflow_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationError("workflow_invalid", "The configured MiniMax H3 workflow could not be loaded.") from exc

    mode_number = MODES[request.mode]
    if request.mode == "ref2v":
        validate_reference_limits(references)
    ref_images = [ref for ref in references if reference_category(ref) == "image"] if request.mode == "ref2v" else []
    ref_videos = [ref for ref in references if reference_category(ref) == "video"] if request.mode == "ref2v" else []
    ref_audios = [ref for ref in references if reference_category(ref) == "audio"] if request.mode == "ref2v" else []
    staged_images = stage(config, ref_images)
    staged_videos = stage(config, ref_videos)
    staged_audios = stage(config, ref_audios)
    staged_frames = stage(config, frames) if request.mode == "fl2v" else []
    width, height = (request.width, request.height) if request.width is not None else calculate_resolution(request.megapixels, request.aspect)
    prompt = translate_aliases(request.prompt, request.mode, references)
    length = h3_frame_count(request.duration)  # already on H3's grid and within H3_MAX_FRAMES

    try:
        for node in ("10", "13", "20"):
            workflow[node]["inputs"].update(prompt=prompt, width=width, height=height, length=length)
        workflow["4"]["inputs"]["value"] = mode_number
        workflow["6"]["inputs"]["strength_model"] = request.parasyte_strength
        workflow["8"]["inputs"]["sparsity_ratio"] = request.sla_sparsity
        workflow["15"]["inputs"]["strength_model"] = request.ref2v_strength
        workflow["20"]["inputs"]["ref_image_size"] = request.reference_sizing
        for node in ("25", "26"):
            workflow[node]["inputs"].update(steps=request.steps, scheduler=request.scheduler)
        workflow["28"]["inputs"]["sampler_name"] = request.sampler
        workflow["29"]["inputs"]["noise_seed"] = request.seed
        workflow["34"]["inputs"]["filename_prefix"] = f"H3Machine_{mode_number}_{time.strftime('%Y%m%d-%H%M%S')}"
        if staged_frames:
            workflow["11"]["inputs"]["image"] = staged_frames[0]
            workflow["12"]["inputs"]["image"] = staged_frames[1] if len(staged_frames) > 1 else staged_frames[0]
        if staged_images:
            workflow["11"]["inputs"]["image"] = staged_images[0]
            workflow["12"]["inputs"]["image"] = staged_images[1] if len(staged_images) > 1 else staged_images[0]
            workflow["19"]["inputs"]["image"] = staged_images[0]
            # MiniMaxH3ReferenceToVideo's ref_images/ref_videos/ref_video_audios/ref_audios
            # are COMFY_AUTOGROW_V3 inputs. ComfyUI's raw /prompt API only recognizes these
            # as flat "<input_name>.<slot_name>" top-level keys (e.g. "ref_images.ref_image_0",
            # 0-indexed) directly on the node's own inputs dict - NOT as a nested
            # {"ref_images": {"ref_image_1": [...]}} dict. Confirmed 2026-09-22: submitting the
            # nested form is silently accepted (no validation error) but resolves to an empty
            # dict at execution time, so every ref2v generation ran with zero reference images
            # actually reaching the model - coherent, error-free output that just ignored the
            # supplied reference identity entirely.
            for index in range(len(staged_images)):
                workflow["20"]["inputs"][f"ref_images.ref_image_{index}"] = [str(35 + index), 0]
            for index, image in enumerate(staged_images):
                workflow[str(35 + index)] = {"class_type": "LoadImage", "inputs": {"image": image}}
        if staged_videos:
            # LoadVideoUI decodes frames (and audio) directly with built-in
            # start_time/end_time trimming - the same node SeedHunter's
            # reference wiring uses (see _wire_seedhunter_references), rather
            # than a hand-assembled LoadVideo/Video Slice/GetVideoComponents
            # chain. A video's own audio track is only wired into
            # ref_video_audios when unmuted *and* it actually has one - each
            # one wired consumes its own <Audio j> slot ahead of the user's
            # own audio references (see _audio_presentation_order, which
            # alias_map() uses to keep every existing <Audio N> in the prompt
            # pointed at the right clip).
            for index, (ref, video) in enumerate(zip(ref_videos, staged_videos)):
                node_id = str(100 + index)
                start = max(0.0, ref.get("trim_start") or 0.0)
                end = ref.get("trim_end") or 0.0
                if ref.get("audio_only"):
                    end = min(end or ref.get("duration") or (start + 5.0), start + 5.0)
                workflow[node_id] = {
                    "class_type": "LoadVideoUI",
                    "inputs": {
                        "video": video, "start_time": start,
                        "end_time": end, "duration": 0,
                        "start_frame": 0, "end_frame": 0, "duration_frames": 0,
                        "resize_method": "maintain aspect ratio",
                        "custom_width": (64 if ref.get("audio_only") else
                                         reference_video_width(config.input_dir / video, width * height)
                                         if request.ref_video_downscale else 0),
                        "custom_height": 0,
                        "frame_rate": 24, "display_mode": "seconds",
                        "crop_x": 0.0, "crop_y": 0.0, "crop_w": 1.0, "crop_h": 1.0,
                    },
                }
                if ref.get("audio_only"):
                    audio_index = sum(bool(previous.get("audio_only")) for previous in ref_videos[:index])
                    workflow["20"]["inputs"][f"ref_audios.ref_audio_{audio_index}"] = [node_id, 1]
                else:
                    visual_index = sum(not previous.get("audio_only") for previous in ref_videos[:index])
                    workflow["20"]["inputs"][f"ref_videos.ref_video_{visual_index}"] = [node_id, 0]
                if not ref.get("audio_only") and not ref.get("muted") and ref.get("has_audio"):
                    workflow["20"]["inputs"][f"ref_video_audios.ref_video_audio_{visual_index}"] = [node_id, 1]
        if staged_audios:
            for index, (ref, audio) in enumerate(zip(ref_audios, staged_audios)):
                node_id = str(150 + index)
                workflow[node_id] = {
                    "class_type": "LoadAudioUI",
                    "inputs": {
                        "audio": audio, "start_time": max(0.0, ref.get("trim_start") or 0.0),
                        "end_time": ref.get("trim_end") or 0.0, "duration": 0,
                    },
                }
                audio_index = index + sum(bool(video.get("audio_only")) for video in ref_videos)
                workflow["20"]["inputs"][f"ref_audios.ref_audio_{audio_index}"] = [node_id, 0]

        model_node = "14" if request.mode == "ref2v" else "5"
        if request.checkpoint:
            workflow[model_node]["inputs"]["unet_name"] = request.checkpoint
        if request.mode == "ref2v" and request.checkpoint and request.checkpoint != REF2V_TURBO_LORA_CHECKPOINT:
            # Node 15 ("LightX Ref2V Turbo 8-step" LoRA) and node 16 ("Ref2V
            # AdaLN compatibility fix") were tuned specifically for
            # REF2V_TURBO_LORA_CHECKPOINT - confirmed 2026-09-22: applying
            # them on top of a different checkpoint (a community finetune, which a
            # working community workflow used bare, with no LoRA at all, for
            # correct <Picture N> identity conditioning) produced heavy
            # artifacting instead. Route node 17 straight from the checkpoint
            # loader for any other checkpoint, skipping both.
            workflow["17"]["inputs"]["model"] = [model_node, 0]
            lora_consumer = ("17", "model")
        else:
            lora_consumer = ("15", "model") if request.mode == "ref2v" else ("6", "model")
        _wire_minimax_h3_loras(workflow, request.loras, model_node, "1", [lora_consumer], next_id=500, extra=_test_loras(request))
        # "Speedup & Saves" toggles. Each turbo LoRA goes together with its
        # AdaLN compatibility fix; skip the fix first so the LoRA's own input
        # (the user LoRA stack) then passes through to the next node.
        if not request.parasyte_enabled:
            for node_id in ("7", "6"):  # Parasyte AdaLN fix, Parasyte Turbo 6-step LoRA
                _skip_model_node(workflow, node_id)
        if not request.sla_enabled:
            _skip_model_node(workflow, "8")  # H3SLAAttention
        if not request.ref2v_turbo_enabled:
            for node_id in ("16", "15"):  # Ref2V AdaLN fix, LightX Ref2V Turbo 8-step LoRA
                _skip_model_node(workflow, node_id)
        elif (request.mode == "ref2v" and request.ref2v_turbo == "pdd"
              and (not request.checkpoint or request.checkpoint == REF2V_TURBO_LORA_CHECKPOINT)):
            _apply_ref2v_pdd(workflow)
        _apply_h3_attention(workflow, request.attention)
        if request.mode == "ref2v" and request.experimental_ref_speedups and request.sla_enabled:
            workflow["61"] = {"class_type": "H3SLAAttention", "inputs": {
                "model": workflow["18"]["inputs"]["model"],
                "sparsity_ratio": request.sla_sparsity, "block_size": "64",
                "min_seq_len": 1024, "dense_last_steps": 1,
                "protect_audio": True, "reference_protection": "Light",
                "dense_backend": "auto", "dense_steps": ""},
                "_meta": {"title": "Experimental Reference SLA"}}
            workflow["18"]["inputs"]["model"] = ["61", 0]
        # SLA's optional dense_steps defaults to "1": one full dense step
        # (about 3x a sparse step). Off = every step sparse, which is what
        # community turbo checkpoints are distilled for.
        if "8" in workflow and not request.sla_dense_step:
            workflow["8"]["inputs"]["dense_steps"] = ""
        ref2v = request.mode == "ref2v"
        if request.spectrum_enabled and (not ref2v or request.experimental_ref_speedups):
            _append_model_patch(workflow, ref2v, "40", "SpectrumApplyMiniMaxH3", SPECTRUM_SETTINGS, "Spectrum forecasting")
        if request.block_cache_enabled:
            _append_model_patch(workflow, ref2v, "41", "H3MiniMaxCache", BLOCK_CACHE_SETTINGS, "H3 block cache")
        # Re-enabled in Ref2V 2026-09-30 at the user's request: the 2 of 2 NaN
        # renders there (2026-09-24) predate finding that H3 renders were
        # running the 24 GB card completely full, the likelier cause.
        if request.ffn_chunking_enabled:
            _append_model_patch(workflow, ref2v, "42", "MiniMaxChunkFeedForward",
                                {"chunks": 4, "seq_threshold": 4096}, "Chunked feed-forward (exact)")
        if request.low_vram_attention_enabled:  # before compile, so compile wraps it
            _append_model_patch(workflow, ref2v, "44", "MiniMaxLowVRAMAttention",
                                {"head_chunks": request.low_vram_attention_chunks}, "Low-VRAM attention (exact)")
        if request.torch_compile_enabled:  # last, so it wraps the fully patched model
            _append_model_patch(workflow, ref2v, "43", "TorchCompileModelAdvanced", TORCH_COMPILE_SETTINGS, "torch.compile")
        _apply_h3_output_fps(workflow, request.h3_fps)
        _apply_h3_quality(workflow, request, width, height, length)
        if request.enable_upscale:
            width, height = _apply_h3_latent_upscale(workflow, request, width, height)
    except (KeyError, TypeError) as exc:
        raise GenerationError("workflow_incompatible", "The configured workflow is missing an expected MiniMax H3 node.") from exc
    _collapse_mode_switches(workflow, mode_number)
    _apply_h3_multi_gpu(workflow, request.multi_gpu_offload, request.multi_gpu_split_gb)
    return workflow, width, height


def _collapse_mode_switches(workflow: dict[str, Any], mode_number: int) -> None:
    """Points every input of the template's mode switches (index = node 4) at
    the active mode's branch. ComfyUI validates every linked switch input, so
    otherwise the other modes' loaders must exist on disk too: Text failed with
    "node 14: unet_name ... not in [...]" once the official ref2va checkpoint
    was removed (2026-10-18), even with a community checkpoint picked."""
    for node in workflow.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict) or "IndexSwitch" not in str(node.get("class_type")) or inputs.get("index") != ["4", 0]:
            continue
        for prefix in ("value", "cond"):
            active = inputs.get(f"{prefix}{mode_number}")
            if active is not None:
                for key in [key for key in inputs if key.startswith(prefix) and key[len(prefix):].isdigit()]:
                    inputs[key] = active


# CFG + negative prompt is wired (CFGGuider + a text-only negative) but
# locked: every H3 checkpoint here is guidance-distilled. Measured
# 2026-09-25 (seed 401, 640x352 Text): CFG 1.75 / 2 / 4 on a community turbo finetune and the
# official fl2va, and 1.5 with Parasyte on or with shift 6, all rendered
# scenes unrelated to the prompt; only 1.5 at shift 8 with Parasyte off kept
# it. A negative equal to the positive at CFG 4 rendered the prompt, so the
# wiring itself is right. Flip this for a checkpoint that supports CFG.
H3_CFG_ENABLED = False
H3_CFG_LOCK_REASON = ("CFG is locked to 1: the H3 checkpoints here are guidance-distilled, and in testing CFG "
                      "1.5-4 rendered scenes unrelated to the prompt, so a negative prompt can't be used.")


# Output encoding for the final MP4 (VHS_VideoCombine, node 34):
# (format, pix_fmt, crf). "standard" is the workflow's own setting.
H3_OUTPUT_QUALITY = {
    "standard": ("video/h264-mp4", "yuv420p", 19),
    "high": ("video/h264-mp4", "yuv420p", 12),
    "near_lossless": ("video/h264-mp4", "yuv420p", 4),
    # 10-bit HEVC: less banding, but some browsers can't preview it.
    "hevc_10bit": ("video/h265-mp4", "yuv420p10le", 12),
}


def _apply_h3_quality(workflow: dict[str, Any], request: GenerationRequest, width: int, height: int, length: int) -> None:
    """CFG + negative prompt, sigma shift overrides, and output encoding."""
    if request.cfg > 1.0 and not H3_CFG_ENABLED:
        raise GenerationError("invalid_cfg", H3_CFG_LOCK_REASON)
    if request.cfg > 1.0:
        # A text-only negative at the same size; the positive keeps its own
        # keyframes/references (the model packs each cond separately).
        workflow["90"] = {"class_type": "MiniMaxH3ImageToVideo",
                          "inputs": {"clip": ["1", 0], "vae": ["2", 0], "prompt": request.negative_prompt,
                                     "width": width, "height": height, "length": length},
                          "_meta": {"title": "Negative prompt"}}
        workflow["24"] = {"class_type": "CFGGuider",
                          "inputs": {"model": ["21", 0], "positive": ["22", 0], "negative": ["90", 0], "cfg": request.cfg},
                          "_meta": {"title": f"CFG guider ({request.cfg:g})"}}
    shift_node = workflow["18" if request.mode == "ref2v" else "9"]["inputs"]
    if request.sigma_shift_video is not None:
        shift_node["shift_video"] = request.sigma_shift_video
    if request.sigma_shift_audio is not None:
        shift_node["shift_audio"] = request.sigma_shift_audio
    video_format, pix_fmt, crf = H3_OUTPUT_QUALITY[request.output_quality]
    workflow["34"]["inputs"].update(format=video_format, pix_fmt=pix_fmt, crf=crf)


# MiniMax H3 latent upscale: sample at the render size, upscale the video
# latent with the 3D H3 upscaler, then re-sample it at the new size with the
# same model/conditioning (a hires-fix pass) - see _apply_h3_latent_upscale.
H3_LATENT_UPSCALER = "minimax_h3_latent_upscaler_3d_bf16.safetensors"
# The H3 base model is trained at a 768 px short side (model card: "The
# shorter side is set to 768 pixels by default"; 2K output is a separate
# Regenerate-2K model), and the second pass runs the base model at the
# upscaled size. Past 1.5x that is marked unstable - a heuristic from the
# model card, not a measured limit.
H3_TRAINED_SHORT_SIDE = 768
H3_UPSCALE_STABLE_SHORT_SIDE = 1152
# A plain H3 render is capped at 2.5 MP (models.GenerationRequest); the
# second pass at the target size needs about as much VRAM as rendering there.
H3_UPSCALE_VRAM_MEGAPIXELS = 2.5
H3_UPSCALE_MAX_MEGAPIXELS = 4.2
# The upscaler's own training mix is 40% 2x, 10% each at 1.5/2.5/3/4x.
H3_UPSCALER_MAIN_SCALE = 2.0
# Second-pass settings (DaSiWa MythicAlchemy v2.3's defaults). Measured
# 2026-09-25 at 640x352 -> 960x512: 6 steps / 0.4 denoise rendered NaN
# (black) in 6 of 9 seeds across Text/Keyframe/Reference - it came from the
# turbo LoRA (Parasyte) re-entering mid-schedule, not SLA or Kitchen; 1 step
# / 0.2 kept the base composition and sharpened it.
H3_UPSCALE_TESTED_STEPS = 1
H3_UPSCALE_TESTED_DENOISE = 0.2
# The 3D upscaler's 3x3x3 convs (512 channels, fp16) unfold their input into
# one 27 x 512 x 2 byte buffer per latent voxel. Measured 2026-09-26: 15 s
# (112 latent frames) at 1856x1024 asked for exactly 21.41 GiB = this formula,
# and OOMed with 17.25 GiB free; 5 s at 1920x1056 (7.5 GiB) ran fine.
H3_UPSCALER_BYTES_PER_VOXEL = 27 * 512 * 2
H3_UPSCALER_WHOLE_CLIP_GIB = 12.0  # over this, an unchunked upscale is marked unstable
H3_UPSCALE_CHUNK_GIB = 9.0         # per-chunk budget the chunk length is sized to
# MMH3TemporalSplitParams: chunk lengths and overlap are whole 17-frame grid
# steps; 136 frames (~5.7 s) / 17 overlap are the node's defaults.
H3_UPSCALE_GRID = 17
H3_UPSCALE_CHUNK_FRAMES = 136
H3_UPSCALE_CHUNK_OVERLAP = 17


# Built-in latent-upscale presets (read-only; users save their own next to
# them). Measured 2026-09-29, 928x512 -> 2x 1856x1024, 5 s, 1 step / 0.2
# (measured 2026-09-29, latent upscale): tiling off
# 141 s upscale pass, auto 35k tokens (1x3 tiles) 91 s, auto 20k (1x6
# strips) 81 s - no seams, no black frames. 20k was also the fastest at
# 640x352 -> 2x (37 s vs 42 s untiled). 35k keeps wider tiles (more context
# per tile) for 10 s more.
# 2026-09-30: tiled 15 s renders broke (Part 5's upscale tiles 2-3 came out
# black from x=576, where tile 2 starts; other tiled 15 s runs went NaN) while
# the same seeds rendered clean untiled. All of those were 379 frames, past
# H3's 362-frame maximum (fixed the same day by H3_MAX_FRAMES), so the tiled
# presets are back and "Stable (untiled)" is the fallback.
H3_UPSCALE_BUILTIN_PRESETS = {
    "Best quality": {"upscale_factor": 2.0, "upscale_steps": 1, "upscale_denoise": 0.2, "upscale_chunking": True,
                     "upscale_advanced": {"tiling": "auto", "token_budget": 35000}},
    "Fastest": {"upscale_factor": 2.0, "upscale_steps": 1, "upscale_denoise": 0.2, "upscale_chunking": True,
                "upscale_advanced": {"tiling": "auto", "token_budget": 20000}},
    "Stable (untiled)": {"upscale_factor": 2.0, "upscale_steps": 1, "upscale_denoise": 0.2, "upscale_chunking": True,
                         "upscale_advanced": {"tiling": "off"}},
}


# The dense attention backend covers whatever runs dense: the whole Ref2V
# branch (node 17), and in Text/Keyframe SLA's dense fall-through (node 8's
# dense_backend) - or, with SLA off, every step (node 60, added). "flash" is
# no override at all: ComfyUI's h3 profile launches with --use-flash-attention,
# and SLA's "auto" dense_backend defers to that global attention.
H3_ATTENTION = {  # name: (ModelAttentionBackend value or None, SLA dense_backend)
    "flash": (None, "auto"),
    "kitchen": ("comfy kitchen attention", "comfy_kitchen"),
    "pytorch": ("pytorch attention", "pytorch"),
}


def _apply_h3_attention(workflow: dict[str, Any], attention: str) -> None:
    backend, dense_backend = H3_ATTENTION[attention]
    if backend is None:
        _skip_model_node(workflow, "17")  # Ref2V ModelAttentionBackend
    else:
        workflow["17"]["inputs"]["attention"] = backend
    if "8" in workflow:
        workflow["8"]["inputs"]["dense_backend"] = dense_backend
    elif backend is not None:
        workflow["60"] = {
            "class_type": "ModelAttentionBackend",
            "inputs": {"model": workflow["9"]["inputs"]["model"], "attention": backend},
            "_meta": {"title": f"Text/FL2V {attention} attention (SLA off)"},
        }
        workflow["9"]["inputs"]["model"] = ["60", 0]


def h3_decode_seconds(width: int, height: int, duration: float) -> float:
    """Rough VAE decode + mp4 write time for phase 3's progress bar (decoding
    sends no progress events). Fitted 2026-09-29 on the stock fp16 VAE:
    1856x1024 x 124 frames 57 s, 1280x704 x 124 frames 32 s (app-observed);
    the per-pixel part scaled by 44.7/54.8 for ComfyUI's H3 VAE running with
    the 16 px decoder tile overlap patch (tools/comfyui-h3-vae-tile-overlap.patch)."""
    return 9.5 + 0.1645 * width * height / 1_000_000 * h3_frame_count(duration)


# H3's longest clip: MiniMax's own 15 s generation is 362 frames (17*21+5,
# 15.08 s at 24 fps). Until 2026-09-30 the app sent duration*24 + 4 frames,
# which ComfyUI snapped up to 379 (15.79 s) for 15 s - a whole grid step past
# what H3 is documented to produce.
H3_MAX_FRAMES = 362
# ...and its shortest: ComfyUI's H3 nodes document the trained range as
# ~124-362 frames (comfy_extras/nodes_minimax_h3.py: "124 = ~5s; trained range
# is ~124-362"). MiniMax's model card accepts 4-15 s, but under 124 frames is
# outside what H3 was trained on, so shorter lengths round up to 124 (5.17 s).
H3_MIN_FRAMES = 124


def h3_frame_count(duration: float) -> int:
    """Seconds -> the frame count H3 renders: duration * 24 snapped up to the
    model's 17k+5 grid (comfy_extras/nodes_minimax_h3.align_frame_count, which
    leaves an on-grid count as is), kept inside H3's trained range: a length
    that would snap past H3_MAX_FRAMES rounds down to the largest grid count
    under it, and one under H3_MIN_FRAMES rounds up to it."""
    frames = max(H3_MIN_FRAMES, int(round(duration * 24)))
    frames += (5 - frames % H3_UPSCALE_GRID) % H3_UPSCALE_GRID
    while frames > H3_MAX_FRAMES:
        frames -= H3_UPSCALE_GRID
    return frames


def h3_latent_frames(frames: int) -> int:
    return 2 if frames <= 5 else ((frames - 5) // H3_UPSCALE_GRID) * 5 + 2


def h3_upscaler_gib(width: int, height: int, frames: int) -> float:
    """The 3D upscaler's largest single allocation for `frames` frames at the target size."""
    return H3_UPSCALER_BYTES_PER_VOXEL * h3_latent_frames(frames) * (height // 16) * (width // 16) / 2**30


# MMH3UltimateUpscale's chunking (ComfyUI-H3-UltimateUpscale-LD nodes.py
# compute_segments), mirrored so the app knows how many chunks phase 2 runs.
H3_FRAMES_PER_TOKEN = (1, 4, 4, 4, 4)


def _h3_token_frames(tokens: int) -> int:
    return sum(H3_FRAMES_PER_TOKEN[i % 5] for i in range(tokens))


def _h3_snap_frame(frame: int, max_tokens: int) -> tuple[int, int]:
    best_k, best_f, best_d = 0, 0, frame
    for k in range(0, max_tokens + 1, 5):
        acc = _h3_token_frames(k)
        if abs(acc - frame) < best_d:
            best_k, best_f, best_d = k, acc, abs(acc - frame)
    return best_k, best_f


def _h3_render_size(request: Any) -> tuple[int, int]:
    return (request.width, request.height) if request.width is not None else calculate_resolution(request.megapixels, request.aspect)


def h3_upscale_chunk_plan(request: Any, target_w: int, target_h: int) -> tuple[int, int]:
    """(chunk length, overlap) in frames for MMH3TemporalSplitParams: the
    user's values from upscale_advanced, or the VRAM-sized auto length."""
    advanced = request.upscale_advanced
    return advanced.chunk_frames or h3_upscale_chunk_frames(target_w, target_h), advanced.chunk_overlap


def h3_upscale_segments(request: Any) -> list[tuple[int, int]]:
    """Each time chunk's (first, end) video latent token, as the node's
    compute_segments cuts them. One segment when chunking is off."""
    tv = h3_latent_frames(h3_frame_count(request.duration))
    if not getattr(request, "upscale_chunking", True):
        return [(0, tv)]
    width, height = h3_upscale_target(*_h3_render_size(request), request.upscale_factor)
    chunk, overlap = h3_upscale_chunk_plan(request, width, height)
    if overlap >= chunk:
        # The models.py check only sees an explicit chunk length; an auto one can
        # be as short as 34 frames. hop <= 0 looped here forever (100% CPU, the
        # whole app frozen - found by UI fuzzing 2026-10-01), and the node's
        # compute_segments rejects it anyway.
        raise GenerationError(
            "invalid_upscale",
            f"Upscale chunk overlap ({overlap} frames) must be shorter than the chunk ({chunk} frames"
            f"{'' if request.upscale_advanced.chunk_frames else ', the auto length at this size'}). Lower the overlap.",
        )
    frame_count = _h3_token_frames(tv)
    hop = chunk - overlap
    segments, prev_end_k = [], 0
    while True:
        start, end = len(segments) * hop, min(len(segments) * hop + chunk, frame_count)
        k0 = 0 if not segments else min(_h3_snap_frame(start, tv)[0], prev_end_k)
        k1 = tv if end >= frame_count else max(_h3_snap_frame(end, tv)[0], k0 + 5)
        segments.append((k0, min(k1, tv)))
        if k1 >= tv:
            return segments
        prev_end_k = k1


def h3_upscale_chunk_count(request: Any) -> int:
    """How many time chunks the upscale + second pass runs (1 = chunking off or a short clip)."""
    return len(h3_upscale_segments(request))


# MMH3SpatialSplitParams / spatial_process*: how each chunk is tiled for the
# second pass (nodes.py resolve_grid_on_frame, compute_spatial_grid), mirrored
# so the app can size phase-2 progress and warn about the token ceiling.
H3_TILE_TOKEN_CEILING = 74898  # the node's INT8-Fast illegal-memory-access line


def _tile_tokens(tile_w: int, tile_h: int, video_tokens: int) -> int:
    return (tile_w // 32) * (tile_h // 32) * video_tokens


def _solve_equal_tiles(total: int, count: int, overlap: int, grid: int = 32) -> tuple[int, int]:
    if count <= 1:
        return -(-total // grid) * grid, 0
    start = -(-((total + (count - 1) * overlap) // count) // grid) * grid
    for size in range(start, total - grid * (count - 1) + 1, grid):
        num = count * size - total
        if num % (count - 1) == 0:
            solved = num // (count - 1)
            if solved % grid == 0 and 0 <= solved <= size - grid:
                return size, solved
    raise ValueError(f"No equal-tile split of {total}px into {count} tiles with {overlap}px overlap.")


def _grid_1d(size: int, tile: int, overlap: int, min_tile: int) -> int:
    """Tile count along one axis (latent units), as nodes.py _grid_1d lays them out."""
    if size <= tile:
        return 1
    hop = tile - overlap
    count = math.ceil((size - overlap) / hop)
    return count + 1 if (count - 1) * hop + tile < size else count


def h3_upscale_tile_grid(advanced: Any, width: int, height: int, video_tokens: int) -> tuple[int, int, int, int] | None:
    """(rows, cols, tile width, tile height) in pixels for one chunk of
    `video_tokens` at the upscaled size; None when tiling is off. Raises
    ValueError where the node would."""
    mode = advanced.tiling
    if mode == "off":
        return None
    if mode == "auto":
        for total in range(1, 82):
            best = None
            for rows in range(1, 10):
                if total % rows or total // rows > 9:
                    continue
                cols = total // rows
                try:
                    tile_w, _ = _solve_equal_tiles(width, cols, advanced.overlap_width)
                    tile_h, _ = _solve_equal_tiles(height, rows, advanced.overlap_height)
                except ValueError:
                    continue
                if min(tile_w, tile_h) < advanced.min_tile_size or _tile_tokens(tile_w, tile_h, video_tokens) > advanced.token_budget:
                    continue
                rank = (-(rows if height >= width else cols), abs(rows - cols))
                if best is None or rank < best[0]:
                    best = (rank, rows, cols, tile_w, tile_h)
            if best:
                return best[1:]
        raise ValueError(f"No tile grid up to 9x9 fits {advanced.token_budget} tokens at {width}×{height}. "
                         "Raise the token budget, lower the minimum tile size or shorten the chunks.")
    if mode == "rows_cols":
        tile_w, _ = _solve_equal_tiles(width, advanced.grid_cols, advanced.overlap_width)
        tile_h, _ = _solve_equal_tiles(height, advanced.grid_rows, advanced.overlap_height)
        if min(tile_w, tile_h) < advanced.min_tile_size:
            raise ValueError(f"A {advanced.grid_rows}×{advanced.grid_cols} grid at {width}×{height} gives "
                             f"{tile_w}×{tile_h} tiles, below the {advanced.min_tile_size}px minimum tile size.")
        return advanced.grid_rows, advanced.grid_cols, tile_w, tile_h
    if width < advanced.tile_width or height < advanced.tile_height:
        raise ValueError(f"The {advanced.tile_width}×{advanced.tile_height} tile is bigger than the {width}×{height} upscaled frame.")
    rows = _grid_1d(height // 16, advanced.tile_height // 16, advanced.overlap_height // 16, advanced.min_tile_size // 16)
    cols = _grid_1d(width // 16, advanced.tile_width // 16, advanced.overlap_width // 16, advanced.min_tile_size // 16)
    return rows, cols, advanced.tile_width, advanced.tile_height


def h3_upscale_second_pass_steps(request: Any, ref2v_pdd: bool) -> int:
    return min(request.upscale_steps, PDD_EVALUATIONS) if ref2v_pdd else request.upscale_steps


def h3_upscale_progress_ticks(request: Any) -> list[int]:
    """Full-progress events node 87 sends per time chunk: one per chunk
    untiled; one per tile tiled one after another; one per tile per step
    with joint steps (each tile forward is its own 1-step sample)."""
    advanced = request.upscale_advanced
    segments = h3_upscale_segments(request)
    if advanced.tiling == "off":
        return [1] * len(segments)
    width, height = h3_upscale_target(*_h3_render_size(request), request.upscale_factor)
    pdd = request.mode == "ref2v" and request.ref2v_turbo_enabled and request.ref2v_turbo == "pdd"
    steps = h3_upscale_second_pass_steps(request, pdd) if advanced.joint_steps else 1
    ticks = []
    for k0, k1 in segments:
        try:
            rows, cols, *_ = h3_upscale_tile_grid(advanced, width, height, k1 - k0)
        except ValueError:
            rows = cols = 1
        ticks.append(rows * cols * steps)
    return ticks


def h3_upscale_chunk_frames(width: int, height: int) -> int:
    """The longest chunk (a multiple of 17 frames, at most the node default)
    whose upscale fits H3_UPSCALE_CHUNK_GIB at the target size. Chunk ends
    snap outwards to latent keyframes, so size it with one grid step spare."""
    frames = H3_UPSCALE_CHUNK_FRAMES
    while frames > 2 * H3_UPSCALE_GRID and h3_upscaler_gib(width, height, frames + H3_UPSCALE_GRID + 5) > H3_UPSCALE_CHUNK_GIB:
        frames -= H3_UPSCALE_GRID
    return frames


def h3_upscale_target(width: int, height: int, factor: float) -> tuple[int, int]:
    return max(32, round(width * factor / 32) * 32), max(32, round(height * factor / 32) * 32)


def h3_upscale_warnings(request: Any, width: int, height: int) -> list[dict[str, str]]:
    """Why an upscale target may not render well. level "unstable" makes the
    UI ask before generating; "caution" is only shown."""
    target_w, target_h = h3_upscale_target(width, height, request.upscale_factor)
    short_side = min(target_w, target_h)
    megapixels = target_w * target_h / 1_000_000
    warnings = []
    if short_side > H3_UPSCALE_STABLE_SHORT_SIDE:
        warnings.append({"level": "unstable", "message": (
            f"{target_w}×{target_h} has a {short_side}px short side. H3 is trained at {H3_TRAINED_SHORT_SIDE}px, "
            f"and past {H3_UPSCALE_STABLE_SHORT_SIDE}px the second pass tends to duplicate subjects or tile textures.")})
    if megapixels > H3_UPSCALE_VRAM_MEGAPIXELS and request.upscale_advanced.tiling == "off":
        warnings.append({"level": "unstable", "message": (
            f"{megapixels:.2f} MP is over the {H3_UPSCALE_VRAM_MEGAPIXELS:g} MP H3 render cap. The second pass runs at "
            "this size, so it can run out of VRAM and come out black.")})
    lightx = (request.mode == "ref2v" and request.ref2v_turbo_enabled and request.ref2v_turbo == "lightx"
              and (not request.checkpoint or request.checkpoint == REF2V_TURBO_LORA_CHECKPOINT))
    if lightx and short_side >= 704:
        warnings.append({"level": "unstable", "message": (
            "The LightX Reference turbo ghosts Reference renders at 720p and up (tested 2026-09-24). "
            "Switch the Reference turbo to PDD.")})
    if request.upscale_steps > H3_UPSCALE_TESTED_STEPS or request.upscale_denoise > H3_UPSCALE_TESTED_DENOISE + 1e-9:
        warnings.append({"level": "unstable", "message": (
            f"A second pass heavier than {H3_UPSCALE_TESTED_STEPS} step at {H3_UPSCALE_TESTED_DENOISE:g} denoise "
            "can come out black: at 6 steps / 0.4 denoise, 6 of 9 test renders were NaN. "
            f"{H3_UPSCALE_TESTED_STEPS} step / {H3_UPSCALE_TESTED_DENOISE:g} is the tested setting.")})
    advanced = request.upscale_advanced
    frames = h3_frame_count(request.duration)
    whole_clip_gib = h3_upscaler_gib(target_w, target_h, frames)
    if not request.upscale_chunking and whole_clip_gib > H3_UPSCALER_WHOLE_CLIP_GIB:
        warnings.append({"level": "unstable", "message": (
            f"Chunking is off: the 3D upscaler takes all {frames} frames at {megapixels:.2f} MP in one go and needs "
            f"one {whole_clip_gib:.1f} GiB block of VRAM (safe up to about {H3_UPSCALER_WHOLE_CLIP_GIB:g} GiB). "
            "It will likely run out of memory after the whole first pass. Turn chunking on, shorten the clip "
            "or lower the factor.")})
    elif request.upscale_chunking and advanced.chunk_frames:
        chunk_gib = h3_upscaler_gib(target_w, target_h, advanced.chunk_frames + H3_UPSCALE_GRID + 5)
        if advanced.upscaler == "model" and chunk_gib > H3_UPSCALER_WHOLE_CLIP_GIB:
            warnings.append({"level": "unstable", "message": (
                f"{advanced.chunk_frames}-frame chunks need about {chunk_gib:.1f} GiB of VRAM for the 3D upscaler at "
                f"{megapixels:.2f} MP (safe up to about {H3_UPSCALER_WHOLE_CLIP_GIB:g} GiB). Use Auto or shorter chunks.")})
        if advanced.chunk_overlap == 0 and frames > advanced.chunk_frames:
            warnings.append({"level": "caution", "message": (
                "Chunk overlap 0 re-samples each chunk with no shared frames, so chunk boundaries can jump.")})
    elif request.upscale_chunking and frames > H3_UPSCALE_CHUNK_FRAMES:
        chunk = h3_upscale_chunk_frames(target_w, target_h)
        chunk_gib = h3_upscaler_gib(target_w, target_h, chunk + H3_UPSCALE_GRID + 5)
        if chunk_gib > H3_UPSCALE_CHUNK_GIB:
            warnings.append({"level": "unstable", "message": (
                f"Even the shortest chunk ({chunk} frames) needs about {chunk_gib:.1f} GiB of VRAM for the "
                f"3D upscaler at {megapixels:.2f} MP. Lower the factor.")})
    if request.upscale_chunking and advanced.anchor_strength < 0.9:
        warnings.append({"level": "caution", "message": (
            f"Anchor strength {advanced.anchor_strength:g} only loosely ties each chunk to the one before it "
            "(0.999 is the node default), so chunk seams may show.")})
    warnings.extend(_h3_upscale_advanced_warnings(request, target_w, target_h, megapixels))
    if request.upscale_factor > H3_UPSCALER_MAIN_SCALE:
        warnings.append({"level": "caution", "message": (
            f"{request.upscale_factor:g}x is past the upscaler's main {H3_UPSCALER_MAIN_SCALE:g}x training scale, "
            "so more of the detail comes from the second pass.")})
    return warnings


def _h3_upscale_advanced_warnings(request: Any, target_w: int, target_h: int, megapixels: float) -> list[dict[str, str]]:
    advanced = request.upscale_advanced
    warnings = []
    if advanced.keep_model_resident and advanced.upscaler == "model" and advanced.upscaler_device == "cuda":
        warnings.append({"level": "unstable", "message": (
            "Keeping H3 resident during the upscale: the second pass on a still-loaded H3 model came out as noise "
            "every time on 2026-09-25, and H3 plus the upscaler's working buffer can fill the VRAM the desktop "
            "also uses. The unload before each chunk is what fixed both.")})
    if not advanced.unload_model_after:
        warnings.append({"level": "caution", "message": (
            "H3 stays loaded into the VAE decode, which then has less VRAM for the upscaled video.")})
    if advanced.upscaler == "interpolate":
        warnings.append({"level": "caution", "message": (
            "Interpolation resizes the latent without the 3D upscaler, so the second pass has to invent all the "
            "detail (a soft result at 1 step / 0.2). Untested in this app; H3 is also not unloaded first.")})
    elif advanced.upscaler_device == "cpu":
        warnings.append({"level": "caution", "message": (
            "The 3D upscaler on the CPU avoids its VRAM buffer but takes many minutes per chunk.")})
    elif advanced.upscaler_precision == "fp32":
        warnings.append({"level": "caution", "message": "fp32 doubles the 3D upscaler's VRAM buffer."})
    if advanced.tiling != "off":
        for k0, k1 in h3_upscale_segments(request):
            try:
                grid = h3_upscale_tile_grid(advanced, target_w, target_h, k1 - k0)
            except ValueError as exc:
                warnings.append({"level": "unstable", "message": f"Spatial tiling will fail: {exc}"})
                break
            rows, cols, tile_w, tile_h = grid
            tokens = _tile_tokens(tile_w, tile_h, k1 - k0)
            if tokens > H3_TILE_TOKEN_CEILING:
                warnings.append({"level": "unstable", "message": (
                    f"{tile_w}×{tile_h} tiles are {tokens:,} tokens per forward, over the ~{H3_TILE_TOKEN_CEILING:,} "
                    "where the INT8 attention kernels crash. Use smaller tiles, more rows/cols or shorter chunks.")})
                break
        if not advanced.joint_steps:
            warnings.append({"level": "caution", "message": (
                "Joint steps off finishes each tile alone and blends them, which leaves visible seams "
                "(the node keeps it only for comparison).")})
    return warnings


def h3_upscale_tiling_summary(request: Any) -> str | None:
    """"2×2 tiles of 1024×576" for the first chunk, or None untiled."""
    advanced = request.upscale_advanced
    if advanced.tiling == "off":
        return None
    width, height = h3_upscale_target(*_h3_render_size(request), request.upscale_factor)
    k0, k1 = h3_upscale_segments(request)[0]
    try:
        rows, cols, tile_w, tile_h = h3_upscale_tile_grid(advanced, width, height, k1 - k0)
    except ValueError:
        return None
    return f"{rows}×{cols} tiles of {tile_w}×{tile_h} ({_tile_tokens(tile_w, tile_h, k1 - k0):,} tokens each)"


def pdd_sigma_tail(steps: int) -> str:
    """The last `steps` steps of PDD's 8-step grid, so the upscale pass stays
    on the grid PDD was distilled for (denoise doesn't apply). Measured
    2026-09-25: 1 step (0.63 -> 0) rendered clean; the 2-step tail from
    0.80 rendered NaN."""
    values = pdd_sigmas().split(", ")
    return ", ".join(values[-(min(steps, PDD_EVALUATIONS) + 1):])


def _apply_h3_latent_upscale(workflow: dict[str, Any], request: GenerationRequest, width: int, height: int) -> tuple[int, int]:
    target_w, target_h = h3_upscale_target(width, height, request.upscale_factor)
    if target_w * target_h / 1_000_000 > H3_UPSCALE_MAX_MEGAPIXELS:
        raise GenerationError("invalid_upscale", (
            f"Upscaling to {target_w}×{target_h} is over the {H3_UPSCALE_MAX_MEGAPIXELS:g} MP limit. Lower the factor."))
    ref2v = request.mode == "ref2v"
    # The re-sample runs inside MMH3UltimateUpscale (as in the DaSiWa
    # MythicAlchemy v2.3 workflow), not a second SamplerCustomAdvanced: it
    # unloads the H3 model before upscaling, so the second pass reloads it
    # fresh. Measured 2026-09-25 (5s 640x352 -> 960x512): a second pass on
    # the model still loaded from the first came out as noise every time, with
    # or without SLA, and a pass after an unload came out clean.
    advanced = request.upscale_advanced
    # Both still validate so older job records load, but neither renders:
    # tested 2026-09-29 (640x352 -> 1280x704, 1 step / 0.2), a second pass on
    # the still-loaded H3 came out as pure patch noise, and interpolation
    # (with H3 unloaded) came out ghosted/smeared.
    if advanced.keep_model_resident:
        raise GenerationError("invalid_upscale", "Keeping H3 resident during the upscale is disabled: the second pass on a still-loaded H3 model renders as noise.")
    if advanced.upscaler == "interpolate":
        raise GenerationError("invalid_upscale", "Latent interpolation is disabled: without the 3D upscaler the second pass renders ghosted and smeared.")
    workflow["81"] = {"class_type": "MMH3LatentUpscaleWithModelParams",
                      "inputs": {"model_name": H3_LATENT_UPSCALER, "width": target_w, "height": target_h,
                                 "device": advanced.upscaler_device, "precision": advanced.upscaler_precision,
                                 "keep_upscaler_resident": advanced.keep_upscaler_resident,
                                 "keep_model_resident": False,
                                 "unload_model_after": advanced.unload_model_after,
                                 "upscale_chunks_first": advanced.upscale_chunks_first},
                      "_meta": {"title": f"H3 latent upscale to {target_w}x{target_h}"}}
    conditioning: list = ["22", 0]
    if request.mode == "fl2v":
        # Keyframe latents live in the conditioning at the render size and
        # share the target's token grid. The upscale node would bilinear-resize
        # them; re-encoding the frames at the new size keeps them sharp.
        workflow["83"] = {"class_type": "MiniMaxH3ImageToVideo",
                          "inputs": {**workflow["13"]["inputs"], "width": target_w, "height": target_h},
                          "_meta": {"title": "Upscale: keyframes at the new size"}}
        conditioning = ["83", 0]
    scheduler = workflow["26" if ref2v else "25"]
    if scheduler["class_type"] == "ManualSigmas":  # PDD Reference: stay on its grid (denoise doesn't apply)
        workflow["85"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": pdd_sigma_tail(request.upscale_steps)},
                          "_meta": {"title": "Upscale: PDD sigma tail"}}
    else:
        workflow["85"] = {"class_type": "BasicScheduler",
                          "inputs": {"model": scheduler["inputs"]["model"], "scheduler": advanced.scheduler or request.scheduler,
                                     "steps": request.upscale_steps, "denoise": request.upscale_denoise},
                          "_meta": {"title": "Upscale scheduler"}}
    workflow["86"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": request.upscale_seed},
                      "_meta": {"title": "Upscale seed"}}
    if request.upscale_chunking:
        # Upscale + re-sample in overlapping time chunks: the 3D upscaler on a
        # whole 15 s clip needs one ~21 GiB buffer at 2x from 928x512 (OOM
        # 2026-09-26). Each chunk's frame 0 is anchored to the previous chunk.
        chunk, overlap = h3_upscale_chunk_plan(request, target_w, target_h)
        workflow["88"] = {"class_type": "MMH3TemporalSplitParams",
                          "inputs": {"chunk_length": chunk, "temporal_overlap": overlap,
                                     "anchor_strength": advanced.anchor_strength},
                          "_meta": {"title": "Upscale: time chunks"}}
    if advanced.tiling != "off":
        # Tile the second pass spatially; the grid is solved by the node
        # against each upscaled chunk (see h3_upscale_tile_grid).
        try:
            for k0, k1 in h3_upscale_segments(request):
                h3_upscale_tile_grid(advanced, target_w, target_h, k1 - k0)
        except ValueError as exc:
            raise GenerationError("invalid_upscale", f"Spatial tiling: {exc}") from exc
        workflow["89"] = {"class_type": "MMH3SpatialSplitParams",
                          "inputs": {"upscale_width": target_w, "upscale_height": target_h,
                                     "tile_size_mode": advanced.tiling,
                                     "tile_width": advanced.tile_width, "tile_height": advanced.tile_height,
                                     "grid_rows": advanced.grid_rows, "grid_cols": advanced.grid_cols,
                                     "spatial_w_overlap": advanced.overlap_width, "spatial_h_overlap": advanced.overlap_height,
                                     "fade_width": advanced.fade_width, "fade_height": advanced.fade_height,
                                     "min_tile_size": advanced.min_tile_size, "overlap_mode": advanced.overlap_mode,
                                     "overlap_blend": advanced.overlap_blend, "joint_steps": advanced.joint_steps,
                                     "token_budget": advanced.token_budget},
                          "_meta": {"title": "Upscale: spatial tiles"}}
    sampler: list = ["28", 0]
    if advanced.sampler:
        workflow["84"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": advanced.sampler},
                          "_meta": {"title": "Upscale sampler"}}
        sampler = ["84", 0]
    workflow["87"] = {"class_type": "MMH3UltimateUpscale",
                      "inputs": {"model": ["21", 0], "conditioning": conditioning, "latent": ["30", 0],
                                 "noise": ["86", 0], "sampler": sampler, "sigmas": ["85", 0], "cfg": 1.0,
                                 "latent_upscale_param": ["81", 0]},
                      "_meta": {"title": "Upscale + second pass"}}
    if request.upscale_chunking:
        workflow["87"]["inputs"]["temporal_split_param"] = ["88", 0]
    if advanced.tiling != "off":
        workflow["87"]["inputs"]["spatial_split_param"] = ["89", 0]
    workflow["31"]["inputs"]["av_latent"] = ["87", 0]
    return target_w, target_h


H3_NATIVE_FPS = 24


def h3_fps_resample(target: int) -> tuple[int, int]:
    """(RIFE multiplier, keep every Nth frame) that turns H3's native 24 fps
    into `target` fps at the same duration: 60 -> 5x then every 2nd,
    30 -> 5x then every 4th, 16 -> 2x then every 3rd, 12 -> every 2nd."""
    common = math.lcm(H3_NATIVE_FPS, target)
    return common // H3_NATIVE_FPS, common // target


def _apply_h3_output_fps(workflow: dict[str, Any], fps: int) -> None:
    """Resamples the decoded frames (32) to `fps` before the video combine
    (34). Audio is untouched: the duration stays the same, so it stays in sync."""
    if fps == H3_NATIVE_FPS:
        return
    multiplier, every_nth = h3_fps_resample(fps)
    images: list = workflow["34"]["inputs"]["images"]
    if multiplier > 1:
        workflow["70"] = {
            "class_type": "RIFE VFI",
            "inputs": {"frames": images, "ckpt_name": "rife49.pth", "clear_cache_after_n_frames": 10,
                       "multiplier": multiplier, "fast_mode": True, "ensemble": True, "scale_factor": 1.0,
                       "dtype": "float32", "torch_compile": False, "batch_size": 1},
            "_meta": {"title": f"RIFE {multiplier}x frame interpolation"},
        }
        images = ["70", 0]
    if every_nth > 1:
        workflow["71"] = {
            "class_type": "VHS_SelectEveryNthImage",
            "inputs": {"images": images, "select_every_nth": every_nth, "skip_first_images": 0},
            "_meta": {"title": f"Keep every {every_nth} frames"},
        }
        images = ["71", 0]
    workflow["34"]["inputs"]["images"] = images
    workflow["34"]["inputs"]["frame_rate"] = float(fps)


# Validated settings (2026-09-23/24 benches): Spectrum as in the Sept 23
# v3 variant; the block cache at its node defaults.
SPECTRUM_SETTINGS = {"enabled": True, "blend_weight": 0.5, "degree": 1, "ridge_lambda": 0.1, "window_size": 2.0,
                     "flex_window": 0.75, "warmup_steps": 1, "tail_actual_steps": 1, "max_history": 8, "debug": False}
# KJNodes' block-level compile: core TorchCompileModel compiles the whole
# forward, which traces into ComfyUI's dynamic-VRAM prefetch and fails with
# "'torch.Stream' object has no attribute 'cuda_stream'" (tested 2026-09-25).
TORCH_COMPILE_SETTINGS = {"backend": "inductor", "fullgraph": False, "mode": "default", "dynamic": "false",
                          "compile_transformer_blocks_only": True, "dynamo_cache_size_limit": 64,
                          "debug_compile_keys": False}
BLOCK_CACHE_SETTINGS = {"reuse_threshold": 0.05, "start_percent": 0.15, "end_percent": 0.9, "max_steps": 2,
                        "device": "auto", "verbose": False}


def _append_model_patch(workflow: dict[str, Any], ref2v: bool, node_id: str, class_type: str,
                        inputs: dict[str, Any], title: str) -> None:
    """Adds a MODEL -> MODEL node at the end of the active mode's model chain
    (right before its scheduler and the mode's model switch)."""
    scheduler, switch_keys = ("26", ("value2",)) if ref2v else ("25", ("value0", "value1"))
    workflow[node_id] = {"class_type": class_type, "inputs": {"model": workflow["21"]["inputs"][switch_keys[0]], **inputs},
                         "_meta": {"title": title}}
    if "model" in workflow[scheduler]["inputs"]:  # ManualSigmas (PDD) has no model input
        workflow[scheduler]["inputs"]["model"] = [node_id, 0]
    for key in switch_keys:
        workflow["21"]["inputs"][key] = [node_id, 0]


# Alibaba PAI PDD 8-step for Ref2VA (huggingface.co/alibaba-pai/MiniMax-H3-Acc-LoRAs),
# loaded by the comfyui-minimax-h3-pdd node (Diffusers keys -> ComfyUI, plus
# the 32-interval output head bank). Its contract: 8 evaluations on the
# shift-12 grid, audio shift 3, Euler, CFG 1, strength 1. Measured 2026-09-24,
# 5s 1280x704: LightX turned Reference renders to ghosted streaks at 720p
# (with Kitchen or plain attention alike); PDD rendered them clean at the
# same speed (400s).
REF2V_PDD_LORA = "MiniMax-H3-Ref2VA-Acc-8Step.safetensors"
PDD_VIDEO_SHIFT = 12.0
PDD_AUDIO_SHIFT = 3.0
PDD_EVALUATIONS = 8


def pdd_sigmas(shift: float = PDD_VIDEO_SHIFT, evaluations: int = PDD_EVALUATIONS) -> str:
    grid = [1 - k / evaluations for k in range(evaluations + 1)]
    return ", ".join(f"{shift * t / (1 + (shift - 1) * t):.6f}" for t in grid)


def _apply_ref2v_pdd(workflow: dict[str, Any]) -> None:
    """Swap LightX (15) for PDD, drop LightX's AdaLN fix (16), and give the
    Ref2V chain PDD's shifts and sigma schedule (Steps doesn't apply)."""
    workflow["15"] = {"class_type": "MiniMaxH3PDDLoader",
                      "inputs": {"model": workflow["15"]["inputs"]["model"], "lora_name": REF2V_PDD_LORA, "strength": 1.0},
                      "_meta": {"title": "PDD Ref2VA 8-step"}}
    _skip_model_node(workflow, "16")
    workflow["18"]["inputs"].update(shift_video=PDD_VIDEO_SHIFT, shift_audio=PDD_AUDIO_SHIFT)
    workflow["26"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": pdd_sigmas()},
                      "_meta": {"title": "PDD 8-step sigmas"}}


def _skip_model_node(workflow: dict[str, Any], node_id: str) -> None:
    """Removes a MODEL -> MODEL patch node (LoRA, attention patch, ...) from
    an API-format workflow, pointing everything that consumed its output at
    its own model input instead."""
    node = workflow.pop(node_id, None)
    if node is None:
        return
    upstream = node["inputs"]["model"]
    for other in workflow.values():
        for name, value in other["inputs"].items():
            if value == [node_id, 0]:
                other["inputs"][name] = upstream


LTX_TILE_PRESETS = {
    "low": "Low VRAM (12GB target, experimental)",
    "medium": "Medium Tiles (16GB recommended)",
    "large": "Large Tiles (24GB recommended)",
}
# Stage 2 always runs native attention. The workflows' "Speed — Sage + Sol"
# branch was removed from the app 2026-09-24: its Sage (405) and Sol (406)
# nodes are saved bypassed, so Speed rendered bit-identical to this; Sage
# can't run on ROCm at all (KJ's patch needs SageAttention 2.x CUDA kernels),
# and Sol alone was slower (60s vs 55s at 5s/960x512) and changed the output.
LTX_STAGE2_NATIVE = "Quality — Native exact (24GB+ recommended)"
LTX_MODE_NAMES = {
    "text": "T2V (text only)",
    "fl2v_first": "I2V (first frame)",
    "fl2v_both": "FLF2V (first + last frame)",
}


def _frontend_workflow_to_prompt(workflow: dict[str, Any], output_node: int) -> dict[str, Any]:
    nodes = {int(node["id"]): node for node in workflow.get("nodes", [])}
    links = {int(link[0]): link for link in workflow.get("links", [])}

    def resolve_link(link_id: int) -> list[Any]:
        """Follow frontend bypass nodes to the linked input they pass through."""
        link = links[link_id]
        seen: set[int] = set()
        while nodes[int(link[1])].get("mode") == 4:
            bypass = nodes[int(link[1])]
            bypass_id = int(bypass["id"])
            if bypass_id in seen:
                raise GenerationError("workflow_incompatible", "The LTX workflow contains a bypass cycle.")
            seen.add(bypass_id)
            output_slot = int(link[2])
            outputs = bypass.get("outputs", [])
            output_type = outputs[output_slot].get("type") if output_slot < len(outputs) else None
            candidates = [
                item for item in bypass.get("inputs", [])
                if item.get("link") is not None and (output_type is None or item.get("type") == output_type)
            ]
            if len(candidates) != 1:
                raise GenerationError(
                    "workflow_incompatible",
                    f"Bypassed LTX node {bypass_id} does not have one unambiguous pass-through input.",
                )
            link = links[int(candidates[0]["link"])]
        return link

    required: set[int] = set()
    pending = [output_node]
    while pending:
        node_id = pending.pop()
        if node_id in required:
            continue
        if node_id not in nodes:
            raise GenerationError("workflow_incompatible", f"The LTX workflow is missing node {node_id}.")
        required.add(node_id)
        for item in nodes[node_id].get("inputs", []):
            if item.get("link") is not None:
                pending.append(int(resolve_link(int(item["link"]))[1]))

    prompt: dict[str, Any] = {}
    ignored_widgets = {"control_after_generate", "upload", "divider", "PowerLoraLoaderHeaderWidget", "➕ Add Lora"}
    for node_id in required:
        node = nodes[node_id]
        inputs = {
            key: value for key, value in (node.get("widgets_values_named") or {}).items()
            if key not in ignored_widgets
        }
        for item in node.get("inputs", []):
            if item.get("link") is None:
                continue
            link = resolve_link(int(item["link"]))
            inputs[item["name"]] = [str(link[1]), int(link[2])]
        if node["type"] == "SaveVideo":
            inputs.pop("format.codec", None)
            codec = inputs.get("codec", "auto")
            inputs["codec"] = codec if isinstance(codec, dict) else {"codec": codec}
        prompt[str(node_id)] = {
            "class_type": node["type"],
            "inputs": inputs,
            "_meta": {"title": node.get("title") or node["type"]},
        }
    return prompt


def _apply_ltx_loras(widgets: dict[str, Any], loras: list[Any]) -> None:
    """Replaces the Power Lora Loader (rgthree) node's saved lora_N rows with
    exactly the request's selections, so whatever the ComfyUI-side workflow
    file happens to have listed there (including non-LTX LoRAs, all saved
    "off") never reaches the render. No rows at all is a pass-through."""
    for key in [key for key in widgets if key.startswith("lora_")]:
        del widgets[key]
    for index, item in enumerate(loras, start=1):
        widgets[f"lora_{index}"] = {"on": True, "lora": item.lora, "strength": item.strength, "strengthTwo": None}


# ── LTX 2.5 Reference mode (LiconStudio MSR V2) ───────────────────────────
#
# LiconStudio's Multiple-Subject-Reference LoRA, applied through its own
# ComfyUI-LTX2.5-MSR node pack (custom_nodes/ComfyUI-LTX2.5-MSR on the
# ComfyUI side). The MSR loader goes on the model right after the user's LTX
# LoRA stack, and its Multi-Reference Guide replaces LTX25ApplyGuides in both
# stages - the same inputs/outputs (positive, negative, latent), so every
# downstream consumer (guiders, AV concat, LTXVCropGuides) is untouched.
# Settings follow the V2 sample workflow (strength 1.0 in both stages,
# 33 reference frames). See https://huggingface.co/LiconStudio/LTX-2.5-Multiple-Subject-Reference
LTX_MSR_LORA = "LTX-2.5-Licon-MSR-V2.safetensors"
LTX_MSR_MAX_IMAGES = 5  # pic1..pic4 + one background/scene slot
LTX_MSR_MAX_AUDIOS = 2  # voices for pic1/pic2 only (the guide exposes audio_ref1/2)
LTX_MSR_AUDIO_SLOT_SECONDS = 5.0  # LoRA metadata reference_audio_slot_duration_seconds
LTX_MSR_GUIDE_STRENGTH = 1.0
LTX_MSR_REFERENCE_FRAMES = "33"
_LTX_MSR_PIC_INPUTS = ("pic1", "pic2", "pic3", "pic4", "background")
_LTX_MSR_NODE_BASE = 500


def validate_ltx_msr_references(references: list[dict[str, Any]]) -> None:
    images = [ref for ref in references if reference_category(ref) == "image"]
    videos = [ref for ref in references if reference_category(ref) == "video"]
    audios = [ref for ref in references if reference_category(ref) == "audio"]
    if videos:
        raise GenerationError("invalid_reference", "LTX 2.5 Reference mode takes images and audio only - remove the video references.")
    if not images:
        raise GenerationError("invalid_reference", "LTX 2.5 Reference mode needs at least one reference image.")
    if len(images) > LTX_MSR_MAX_IMAGES:
        raise GenerationError(
            "too_many_references",
            f"LTX 2.5 Reference mode allows at most {LTX_MSR_MAX_IMAGES} images (4 subjects + 1 background); {len(images)} are attached.",
        )
    if len(audios) > LTX_MSR_MAX_AUDIOS:
        raise GenerationError(
            "too_many_references",
            f"LTX 2.5 Reference mode allows at most {LTX_MSR_MAX_AUDIOS} audio references (voices for Image 1 and Image 2); {len(audios)} are attached.",
        )
    if len(audios) > min(len(images), 4):
        raise GenerationError(
            "invalid_reference",
            "Each audio reference is the voice of the image in the same position (Audio 1 -> Image 1), so add an image for every audio clip.",
        )
    for index, ref in enumerate(audios, start=1):
        if effective_duration(ref) > LTX_MSR_AUDIO_SLOT_SECONDS + 1e-6:
            raise GenerationError(
                "reference_duration_exceeded",
                f"Audio {index} is {effective_duration(ref):.1f}s after trim; LTX 2.5 Reference mode uses at most "
                f"{LTX_MSR_AUDIO_SLOT_SECONDS:.0f}s per voice - trim it down.",
            )


def translate_ltx_msr_tokens(text: str, images: list[dict[str, Any]], audios: list[dict[str, Any]]) -> str:
    """<Picture N>/<Audio N> (stable upload numbers) -> the positional
    "Image k"/"Audio k" labels the MSR LoRA was trained on."""
    mapping: dict[str, str] = {}
    for label, word, refs in (("picture", "Image", images), ("audio", "Audio", audios)):
        for index, ref in enumerate(refs, start=1):
            number = _stable_alias_number(ref["alias"])
            if number is not None:
                mapping[f"{label} {number}"] = f"{word} {index}"
    return translate_reference_tokens(text, mapping)


def _apply_ltx_msr(config: Settings, prompt: dict[str, Any], images: list[dict[str, Any]], audios: list[dict[str, Any]]) -> None:
    next_id = _LTX_MSR_NODE_BASE
    loader_id = str(next_id)
    next_id += 1
    # Rewire everything that took the LoRA-stacked model (428's slot 0; slot 1
    # is CLIP) to take MSR's instead - by value, not input name: stage 2 gets
    # it through ComfySwitchNode's on_true/on_false (the Speed/Quality switch).
    for node in prompt.values():
        for name, value in node["inputs"].items():
            if value == ["428", 0]:
                node["inputs"][name] = [loader_id, 0]
    prompt[loader_id] = {
        "class_type": "ComfyUILTX25MSRICLoRALoader",
        "inputs": {"model": ["428", 0], "lora_name": LTX_MSR_LORA, "strength_model": 1.0},
        "_meta": {"title": "MSR V2 IC-LoRA Loader"},
    }
    guide_inputs: dict[str, Any] = {"msr_parameters": [loader_id, 1]}
    for index, path in enumerate(stage(config, images)):
        node_id = str(next_id)
        next_id += 1
        prompt[node_id] = {"class_type": "LoadImage", "inputs": {"image": path}, "_meta": {"title": f"MSR Image {index + 1}"}}
        guide_inputs[_LTX_MSR_PIC_INPUTS[index]] = [node_id, 0]
    for index, (ref, path) in enumerate(zip(audios, stage(config, audios)), start=1):
        load_id, encode_id = str(next_id), str(next_id + 1)
        next_id += 2
        prompt[load_id] = {
            "class_type": "LoadAudioUI",
            "inputs": {
                "audio": path, "start_time": max(0.0, ref.get("trim_start") or 0.0),
                "end_time": ref.get("trim_end") or 0.0, "duration": 0,
            },
            "_meta": {"title": f"MSR Audio {index}"},
        }
        prompt[encode_id] = {
            "class_type": "LTXVAudioVAEEncode",
            "inputs": {"audio": [load_id, 0], "audio_vae": ["386", 0]},
            "_meta": {"title": f"MSR Audio {index} encode"},
        }
        guide_inputs[f"audio_ref{index}"] = [encode_id, 0]
    # Stage 2 re-guides from stage 1's clean LTXVConditioning (what 412 got),
    # not from LTXVCropGuides' output as LTX25ApplyGuides did: CropGuides sets
    # guide_attention_entries to None rather than removing it, and MSR's
    # guide then fails unpacking it ("Value after * must be an iterable").
    # Same wiring as the MSR V2 sample workflow; 413 still crops the latent.
    clean_positive, clean_negative = prompt["412"]["inputs"]["positive"], prompt["412"]["inputs"]["negative"]
    for guide_id in ("412", "414"):
        old = prompt[guide_id]["inputs"]
        prompt[guide_id] = {
            "class_type": "ComfyUILTX25MSRMultiReferenceGuide",
            "inputs": {
                "positive": clean_positive, "negative": clean_negative, "vae": old["vae"], "latent": old["latent"],
                **guide_inputs,
                "strength": LTX_MSR_GUIDE_STRENGTH, "reference_frames": LTX_MSR_REFERENCE_FRAMES,
                "use_tiled_encode": False, "tile_size": 256, "tile_overlap": 64,
            },
            "_meta": {"title": f"MSR Multi-Reference Guide (stage {1 if guide_id == '412' else 2})"},
        }
    # The keyframe loaders only fed LTX25ApplyGuides.
    for unused in ("409", "411"):
        prompt.pop(unused, None)


def build_ltx_workflow(
    config: Settings, request: GenerationRequest, frames: list[dict[str, Any]],
    references: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int, int]:
    references = references or []
    if request.mode == "ref2v":
        validate_ltx_msr_references(references)
    if request.mode == "fl2v" and not frames:
        raise GenerationError("invalid_reference", "A first frame is required for LTX 2.5 Keyframe mode.")
    workflow_path = local_variant(config.ltx_workflow_path.parent / LTX_WORKFLOW_FILES[request.ltx_workflow])
    if not workflow_path.is_file():
        raise GenerationError("workflow_missing", "The LTX 2.5 workflow file is not configured on this server.")
    try:
        workflow = json.loads(workflow_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationError("workflow_invalid", "The configured LTX 2.5 workflow could not be loaded.") from exc

    nodes = {str(node["id"]): node for node in workflow.get("nodes", [])}
    staged_frames = stage(config, frames) if frames and request.mode == "fl2v" else []
    width, height = (request.width, request.height) if request.width is not None else calculate_ltx_resolution(request.megapixels, request.aspect)
    ltx_mode = "fl2v_both" if len(staged_frames) > 1 else ("fl2v_first" if staged_frames else "text")
    msr_images = [ref for ref in references if reference_category(ref) == "image"] if request.mode == "ref2v" else []
    msr_audios = [ref for ref in references if reference_category(ref) == "audio"] if request.mode == "ref2v" else []
    try:
        controls = nodes["408"]["widgets_values_named"]
        controls.update(
            mode=LTX_MODE_NAMES[ltx_mode],
            vae_decode_preset=LTX_TILE_PRESETS[request.ltx_tile_preset],
            final_width=width,
            final_height=height,
            timing_mode="Duration drives frames",
            fps=request.ltx_fps,
            duration_seconds=request.duration,
            seed=request.seed,
        )
        prompt_text = translate_ltx_msr_tokens(request.prompt, msr_images, msr_audios) if msr_images else request.prompt
        if msr_images:
            # Anything still in <...> form points at a reference that isn't
            # attached (or a video) - MSR would just read it as literal text.
            leftover = sorted({match.group(0) for match in REFERENCE_TOKEN_RE.finditer(prompt_text)})
            if leftover:
                raise GenerationError(
                    "invalid_reference",
                    f"{', '.join(leftover)} {'is' if len(leftover) == 1 else 'are'} not attached as "
                    "LTX 2.5 Reference images/audio - attach it or remove the tag from the prompt.",
                )
        nodes["376"]["widgets_values_named"]["value"] = prompt_text
        nodes["409"]["widgets_values_named"]["image"] = staged_frames[0] if staged_frames else ""
        nodes["411"]["widgets_values_named"]["image"] = staged_frames[1] if len(staged_frames) > 1 else ""
        if "423" in nodes:
            nodes["423"]["widgets_values_named"]["stage2_mode"] = LTX_STAGE2_NATIVE
        nodes["75"]["widgets_values_named"]["filename_prefix"] = f"H3Machine_LTX25_{time.strftime('%Y%m%d-%H%M%S')}"
        _apply_ltx_loras(nodes["428"]["widgets_values_named"], [*request.ltx_loras, *(SimpleNamespace(lora=f, strength=s) for f, s in _test_loras(request))])
        prompt = _frontend_workflow_to_prompt(workflow, 75)
        if msr_images:
            _apply_ltx_msr(config, prompt, msr_images, msr_audios)
    except (KeyError, TypeError) as exc:
        raise GenerationError("workflow_incompatible", "The configured workflow is missing an expected LTX 2.5 node.") from exc
    return prompt, width, height


# ── Wan 2.2 A14B ──────────────────────────────────────────────────────────
#
# workflows/Wan-2.2-A14B.json (API format) holds every branch: Text samples an
# empty latent through the T2V experts, Keyframe the I2V experts through
# WanImageToVideo (first frame) or WanFirstLastFrameToVideo (first + last).
# The unused branch is deleted here. Two KSamplerAdvanced passes share one
# step schedule: the high-noise expert takes the first wan_high_steps steps
# and hands its leftover noise to the low-noise expert, as in the official
# ComfyUI template. Turbo LoRAs (12/13) and each picked LoRA sit between the
# expert loaders (10/11) and their shift nodes (14/15).
WAN_EXPERTS = {
    "text": ("wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors", "wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors"),
    "fl2v": ("wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors", "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"),
}
WAN_TURBO_LORAS = {
    ("text", "lightx2v"): ("Wan22/wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors",
                           "Wan22/wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors"),
    ("fl2v", "lightx2v"): ("Wan22/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors",
                           "Wan22/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors"),
    ("fl2v", "motionfix"): ("Wan22/wan2.2_i2v_lightx2v_motion_fix_high.safetensors",
                            "Wan22/wan2.2_i2v_lightx2v_motion_fix_low.safetensors"),
}
WAN_TURBO_LORAS[("text", "motionfix")] = WAN_TURBO_LORAS[("text", "lightx2v")]  # the fix is I2V-only
_WAN_LORA_NODE_BASE = {"high": 100, "low": 200}


def wan_frame_count(duration: float) -> int:
    """Wan's VAE packs 4 frames per latent after the first: lengths are 4n+1 (5 s = 81)."""
    return max(5, round(duration * WAN_NATIVE_FPS / 4) * 4 + 1)


def wan_high_steps(request: GenerationRequest) -> int:
    return request.wan_high_steps if request.wan_high_steps is not None else max(1, request.steps // 2)


def build_wan_workflow(config: Settings, request: GenerationRequest, frames: list[dict[str, Any]]) -> tuple[dict[str, Any], int, int]:
    mode = "fl2v" if request.mode == "fl2v" else "text"
    if mode == "fl2v" and not frames:
        raise GenerationError("invalid_reference", "A first frame is required for Wan 2.2 Keyframe mode.")
    try:
        workflow = json.loads(config.wan_workflow_path.read_text())
    except OSError as exc:
        raise GenerationError("workflow_missing", "The Wan 2.2 workflow file is not configured on this server.") from exc
    except json.JSONDecodeError as exc:
        raise GenerationError("workflow_invalid", "The configured Wan 2.2 workflow could not be loaded.") from exc
    width, height = (request.width, request.height) if request.width is not None else calculate_wan_resolution(request.megapixels, request.aspect)
    length = wan_frame_count(request.duration)
    try:
        nodes = {key: workflow[key]["inputs"] for key in ("2", "3", "10", "11", "12", "13", "14", "15", "30", "31", "34")}
        nodes["2"]["text"] = request.prompt
        if request.negative_prompt.strip():
            nodes["3"]["text"] = request.negative_prompt
        high_unet, low_unet = WAN_EXPERTS[mode]
        nodes["10"]["unet_name"], nodes["11"]["unet_name"] = high_unet, low_unet
        # Turbo off: the loaders go straight to the shift nodes.
        if request.wan_turbo == "off":
            del workflow["12"], workflow["13"]
            high, low = ["10", 0], ["11", 0]
        else:
            nodes["12"]["lora_name"], nodes["13"]["lora_name"] = WAN_TURBO_LORAS[(mode, request.wan_turbo)]
            high, low = ["12", 0], ["13", 0]
        for index, item in enumerate(request.wan_loras):
            experts = wan_lora_experts(item.lora)
            for expert in ("high", "low"):
                if not experts.get(expert):
                    continue
                node_id = str(_WAN_LORA_NODE_BASE[expert] + index)
                workflow[node_id] = {
                    "class_type": "LoraLoaderModelOnly",
                    "inputs": {"model": high if expert == "high" else low, "lora_name": experts[expert], "strength_model": item.strength},
                    "_meta": {"title": f"LoRA {index + 1} ({expert} noise)"},
                }
                if expert == "high":
                    high = [node_id, 0]
                else:
                    low = [node_id, 0]
        nodes["14"]["model"], nodes["15"]["model"] = high, low
        nodes["14"]["shift"] = nodes["15"]["shift"] = request.wan_shift

        if mode == "text":
            del workflow["21"], workflow["22"], workflow["23"], workflow["24"]
            workflow["20"]["inputs"].update(width=width, height=height, length=length)
            positive, negative, latent = ["2", 0], ["3", 0], ["20", 0]
        else:
            staged = stage(config, frames[:2])
            workflow["21"]["inputs"]["image"] = staged[0]
            del workflow["20"]
            if len(staged) > 1:
                workflow["22"]["inputs"]["image"] = staged[1]
                del workflow["23"]
                cond = "24"
            else:
                del workflow["22"], workflow["24"]
                cond = "23"
            workflow[cond]["inputs"].update(width=width, height=height, length=length)
            positive, negative, latent = [cond, 0], [cond, 1], [cond, 2]

        split = wan_high_steps(request)
        for node_id, start, end in (("30", 0, split), ("31", split, 10000)):
            nodes[node_id].update(noise_seed=request.seed, steps=request.steps, cfg=request.wan_cfg,
                                  sampler_name=request.sampler, scheduler=request.scheduler,
                                  positive=positive, negative=negative, start_at_step=start, end_at_step=end)
        nodes["30"]["latent_image"] = latent

        video_format, pix_fmt, crf = H3_OUTPUT_QUALITY[request.output_quality]
        nodes["34"].update(format=video_format, pix_fmt=pix_fmt, crf=crf,
                           filename_prefix=f"H3Machine_Wan22_{time.strftime('%Y%m%d-%H%M%S')}")
        _apply_wan_output_fps(workflow, request.wan_fps)
    except (KeyError, TypeError) as exc:
        raise GenerationError("workflow_incompatible", "The configured workflow is missing an expected Wan 2.2 node.") from exc
    return workflow, width, height


def _apply_wan_output_fps(workflow: dict[str, Any], fps: int) -> None:
    """Native 16 fps, or RIFE up to a common multiple then every Nth frame (24 = 3x, every 2nd)."""
    workflow["34"]["inputs"]["frame_rate"] = float(fps)
    if fps == WAN_NATIVE_FPS:
        return
    common = math.lcm(WAN_NATIVE_FPS, fps)
    multiplier, every_nth = common // WAN_NATIVE_FPS, common // fps
    images: list = workflow["34"]["inputs"]["images"]
    workflow["70"] = {
        "class_type": "RIFE VFI",
        "inputs": {"frames": images, "ckpt_name": "rife49.pth", "clear_cache_after_n_frames": 10,
                   "multiplier": multiplier, "fast_mode": True, "ensemble": True, "scale_factor": 1.0,
                   "dtype": "float32", "torch_compile": False, "batch_size": 1},
        "_meta": {"title": f"RIFE {multiplier}x frame interpolation"},
    }
    images = ["70", 0]
    if every_nth > 1:
        workflow["71"] = {
            "class_type": "VHS_SelectEveryNthImage",
            "inputs": {"images": images, "select_every_nth": every_nth, "skip_first_images": 0},
            "_meta": {"title": f"Keep every {every_nth} frames"},
        }
        images = ["71", 0]
    workflow["34"]["inputs"]["images"] = images


# ── H3 SeedHunter ─────────────────────────────────────────────────────────

SEEDHUNTER_CANDIDATE_COMBINE_NODES = ("26", "36", "46")
SEEDHUNTER_CANDIDATE_GUIDER_NODES = {1: "21", 2: "31", 3: "41"}
SEEDHUNTER_CANDIDATE_SEED_OFFSETS = {1: 0, 2: 1, 3: 2}


def calculate_seedhunter_length(seconds: float) -> int:
    """Seconds -> frame count, rounded up to satisfy the model's 17-frame-chunk
    temporal-VAE alignment. Verified against the source workflow: 5s -> 124 frames."""
    frames = max(5, round(seconds * 24))
    return frames + (5 - (frames % 17)) % 17


def _seedhunter_alias_map(references: list[dict[str, Any]], videos: list[dict[str, Any]], audios: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    mapping.update(_canonical_alias_map("Picture", references))
    mapping.update(_canonical_alias_map("Video", videos))
    mapping.update(_canonical_alias_map("Audio", audios))
    return mapping


def translate_seedhunter_aliases(text: str, references: list[dict[str, Any]], videos: list[dict[str, Any]], audios: list[dict[str, Any]]) -> str:
    mapping = _seedhunter_alias_map(references, videos, audios)
    return translate_reference_tokens(text, mapping)


def _maybe_insert_live_preview(workflow: dict[str, Any], enabled: bool, next_id: int) -> tuple[str, int]:
    """Optionally inserts Kijai's ModelPreviewOverrideKJ (live low-res preview
    during sampling; ComfyUI's own UI shows it, Video does not - it's
    purely for anyone watching the ComfyUI window directly. ~6-16% slower.).
    Rewires the scheduler to source `model` from it when enabled. Returns
    (model_source_node_id, next_free_id) for guiders to read `model` from."""
    if not enabled:
        return "12", next_id
    preview_id = str(next_id)
    next_id += 1
    workflow[preview_id] = {
        "class_type": "ModelPreviewOverrideKJ",
        "inputs": {
            "model": ["12", 0],
            "max_resolution": 1024,
            "jpeg_quality": 80,
            "suppress_default_preview": True,
            "preview_frames": 1,
            "preview_fps": 6,
            "tiny_vae": "taeh3.safetensors",
        },
        "_meta": {"title": "Live sampling preview"},
    }
    workflow["10"]["inputs"]["model"] = [preview_id, 0]
    return preview_id, next_id


def _prune_seedhunter_candidates(workflow: dict[str, Any], candidate_count: int) -> None:
    """Removes the 2nd/3rd candidate branches from a loaded Candidates template
    copy when the request asks for fewer than 3 (each branch = 7 sequential
    node ids: RandomNoise, BasicGuider, SamplerCustomAdvanced, AV split,
    VAEDecode, VAEDecodeAudio, VHS_VideoCombine)."""
    bases = {2: 30, 3: 40}
    for candidate, base in bases.items():
        if candidate > candidate_count:
            for offset in range(7):
                workflow.pop(str(base + offset), None)


def stage_continuation_source(config: Settings, store: Any, job_id: str) -> str:
    """Copies a previously-completed job's output video into the ComfyUI-visible
    input staging area, for use as a "continue from" source. Returns the staged
    path (relative to config.input_dir, as ComfyUI expects for LoadVideoUI)."""
    job = store.job(job_id)
    if not job or job.get("state") != "complete" or not job.get("output_path"):
        raise GenerationError("invalid_reference", "The selected video to continue from is no longer available.")
    source = (config.output_dir / job["output_path"]).resolve()
    try:
        source.relative_to(config.output_dir.resolve())
    except ValueError as exc:
        raise GenerationError("invalid_reference", "The continuation source path is invalid.") from exc
    if not source.is_file():
        raise GenerationError("missing_reference", "The video to continue from is no longer available.")
    target_dir = config.input_dir / "H3MachineRefs"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_name = f"{uuid.uuid4().hex[:12]}{source.suffix.lower()}"
    shutil.copy2(source, target_dir / target_name)
    return f"H3MachineRefs/{target_name}"


def _wire_continuation_anchor(
    workflow: dict[str, Any], staged_video_path: str, overlap_frames: int,
    width: int, height: int, conditioning_node: str, next_id: int,
) -> tuple[str, str, int]:
    """Builds the "guide the new generation to continue from the tail of a
    previous video" chain: loads the previous video, extracts its last
    `overlap_frames` frames + matching audio, and feeds them into a
    MiniMaxH3AddGuide anchored at frame_idx=0 (start of the new clip).
    Chains onto (and returns) a new conditioning node; every candidate branch's
    BasicGuider should read `conditioning` from it instead of the ref-wired one.
    Also returns the LoadVideoUI node id, since the finalize-stage stitch step
    needs to re-read its frame_count/duration/audio outputs independently
    (candidates and finalize are separate ComfyUI submissions)."""
    load_id = str(next_id); next_id += 1
    workflow[load_id] = {
        "class_type": "LoadVideoUI",
        "inputs": {
            "video": staged_video_path, "start_time": 0, "end_time": 0, "duration": 0,
            "start_frame": 0, "end_frame": 0, "duration_frames": 0,
            "resize_method": "maintain aspect ratio", "custom_width": 0, "custom_height": 0,
            "frame_rate": 24, "display_mode": "seconds",
            "crop_x": 0, "crop_y": 0, "crop_w": 0, "crop_h": 0,
        },
        "_meta": {"title": "Continue-from video"},
    }
    resize_id = str(next_id); next_id += 1
    workflow[resize_id] = {
        "class_type": "ImageResizeKJv2",
        "inputs": {
            "image": [load_id, 0], "width": width, "height": height,
            "upscale_method": "lanczos", "keep_proportion": "stretch", "pad_color": "0, 0, 0",
            "crop_position": "center", "divisible_by": 16,
        },
        "_meta": {"title": "Resize continuation source to generation size"},
    }
    overlap_start_id = str(next_id); next_id += 1
    workflow[overlap_start_id] = {
        "class_type": "ComfyMathExpression",
        "inputs": {"expression": "b - a", "values.a": overlap_frames, "values.b": [load_id, 3]},
        "_meta": {"title": "OG overlap start index"},
    }
    og_overlap_frames_id = str(next_id); next_id += 1
    workflow[og_overlap_frames_id] = {
        "class_type": "GetImageRangeFromBatch",
        "inputs": {"images": [resize_id, 0], "start_index": [overlap_start_id, 1], "num_frames": overlap_frames},
        "_meta": {"title": "OG overlap-only frames"},
    }
    overlap_duration_id = str(next_id); next_id += 1
    workflow[overlap_duration_id] = {
        "class_type": "ComfyMathExpression",
        "inputs": {"expression": "a / 24", "values.a": overlap_frames},
        "_meta": {"title": "Overlap duration (seconds)"},
    }
    nonoverlap_duration_id = str(next_id); next_id += 1
    workflow[nonoverlap_duration_id] = {
        "class_type": "ComfyMathExpression",
        "inputs": {"expression": "a - (b / 24)", "values.a": [load_id, 2], "values.b": overlap_frames},
        "_meta": {"title": "OG non-overlap duration (seconds)"},
    }
    og_overlap_audio_id = str(next_id); next_id += 1
    workflow[og_overlap_audio_id] = {
        "class_type": "TrimAudioDuration",
        "inputs": {"audio": [load_id, 1], "start_index": [nonoverlap_duration_id, 0], "duration": [overlap_duration_id, 0]},
        "_meta": {"title": "OG overlap-only audio"},
    }
    guide_id = str(next_id); next_id += 1
    workflow[guide_id] = {
        "class_type": "MiniMaxH3AddGuide",
        "inputs": {
            "positive": [conditioning_node, 0], "latent": ["11", 1], "frame_idx": 0,
            "vae": ["2", 0], "audio_vae": ["3", 0],
            "image": [og_overlap_frames_id, 0], "audio": [og_overlap_audio_id, 0],
        },
        "_meta": {"title": "Anchor to previous video's ending"},
    }
    return guide_id, load_id, next_id


def _wire_continuation_stitch(
    workflow: dict[str, Any], load_video_node: str, overlap_frames: int,
    new_gen_images: list, new_gen_audio: list, next_id: int,
) -> tuple[list, list, int]:
    """Blends the new generation's start back onto the previous video's tail
    (crossfade over `overlap_frames`) and concatenates the previous video's
    kept prefix + the blended seam + the rest of the new generation. Returns
    ([node_id, slot] for the stitched IMAGE and AUDIO)."""
    resize_id = str(next_id); next_id += 1
    size_probe_id = str(next_id); next_id += 1
    workflow[size_probe_id] = {
        "class_type": "GetImageSizeAndCount",
        "inputs": {"image": new_gen_images},
        "_meta": {"title": "New generation size/count"},
    }
    workflow[resize_id] = {
        "class_type": "ImageResizeKJv2",
        "inputs": {
            "image": [load_video_node, 0], "width": [size_probe_id, 1], "height": [size_probe_id, 2],
            "upscale_method": "lanczos", "keep_proportion": "stretch", "pad_color": "0, 0, 0",
            "crop_position": "center", "divisible_by": 16,
        },
        "_meta": {"title": "Resize continuation source to final output size"},
    }
    og_nonoverlap_count_id = str(next_id); next_id += 1
    workflow[og_nonoverlap_count_id] = {
        "class_type": "ComfyMathExpression",
        "inputs": {"expression": "a - b", "values.a": [load_video_node, 3], "values.b": overlap_frames},
        "_meta": {"title": "OG non-overlap frame count"},
    }
    og_nonoverlap_frames_id = str(next_id); next_id += 1
    workflow[og_nonoverlap_frames_id] = {
        "class_type": "ImageFromBatch",
        "inputs": {"image": [resize_id, 0], "batch_index": 0, "length": [og_nonoverlap_count_id, 1]},
        "_meta": {"title": "OG kept (non-overlap) frames"},
    }
    og_overlap_start_id = str(next_id); next_id += 1
    workflow[og_overlap_start_id] = {
        "class_type": "ComfyMathExpression",
        "inputs": {"expression": "b - a", "values.a": overlap_frames, "values.b": [load_video_node, 3]},
        "_meta": {"title": "OG overlap start index"},
    }
    og_overlap_frames_id = str(next_id); next_id += 1
    workflow[og_overlap_frames_id] = {
        "class_type": "GetImageRangeFromBatch",
        "inputs": {"images": [resize_id, 0], "start_index": [og_overlap_start_id, 1], "num_frames": overlap_frames},
        "_meta": {"title": "OG overlap-only frames (final size)"},
    }
    newgen_overlap_frames_id = str(next_id); next_id += 1
    workflow[newgen_overlap_frames_id] = {
        "class_type": "GetImageRangeFromBatch",
        "inputs": {"images": new_gen_images, "start_index": 0, "num_frames": overlap_frames},
        "_meta": {"title": "New generation's overlap-only frames"},
    }
    newgen_nonoverlap_count_id = str(next_id); next_id += 1
    workflow[newgen_nonoverlap_count_id] = {
        "class_type": "ComfyMathExpression",
        "inputs": {"expression": "b - a", "values.a": overlap_frames, "values.b": [size_probe_id, 3]},
        "_meta": {"title": "New generation non-overlap frame count"},
    }
    newgen_nonoverlap_frames_id = str(next_id); next_id += 1
    workflow[newgen_nonoverlap_frames_id] = {
        "class_type": "GetImageRangeFromBatch",
        "inputs": {"images": new_gen_images, "start_index": overlap_frames, "num_frames": [newgen_nonoverlap_count_id, 1]},
        "_meta": {"title": "New generation's non-overlap frames"},
    }
    blend_id = str(next_id); next_id += 1
    workflow[blend_id] = {
        "class_type": "ImageBlend",
        "inputs": {"image1": [og_overlap_frames_id, 0], "image2": [newgen_overlap_frames_id, 0], "blend_factor": 0.5, "blend_mode": "normal"},
        "_meta": {"title": "Crossfade the seam"},
    }
    stitch1_id = str(next_id); next_id += 1
    workflow[stitch1_id] = {
        "class_type": "ImageBatchMulti",
        "inputs": {"inputcount": 2, "image_1": [blend_id, 0], "image_2": [newgen_nonoverlap_frames_id, 0]},
        "_meta": {"title": "Blended seam + rest of new generation"},
    }
    stitch2_id = str(next_id); next_id += 1
    workflow[stitch2_id] = {
        "class_type": "ImageBatchMulti",
        "inputs": {"inputcount": 2, "image_1": [og_nonoverlap_frames_id, 0], "image_2": [stitch1_id, 0]},
        "_meta": {"title": "Full continued video"},
    }
    # og_nonoverlap_count above is a FRAME count (video-domain); audio trimming
    # needs a separate seconds-domain duration computed from the source's own
    # `duration` output, matching how the anchor step derives it.
    og_nonoverlap_duration_id = str(next_id); next_id += 1
    workflow[og_nonoverlap_duration_id] = {
        "class_type": "ComfyMathExpression",
        "inputs": {"expression": "a - (b / 24)", "values.a": [load_video_node, 2], "values.b": overlap_frames},
        "_meta": {"title": "OG non-overlap duration (seconds)"},
    }
    og_audio_nonoverlap_id = str(next_id); next_id += 1
    workflow[og_audio_nonoverlap_id] = {
        "class_type": "TrimAudioDuration",
        "inputs": {"audio": [load_video_node, 1], "start_index": 0, "duration": [og_nonoverlap_duration_id, 0]},
        "_meta": {"title": "OG kept (non-overlap) audio"},
    }
    final_audio_id = str(next_id); next_id += 1
    workflow[final_audio_id] = {
        "class_type": "AudioConcat",
        "inputs": {"audio1": [og_audio_nonoverlap_id, 0], "audio2": new_gen_audio, "direction": "after"},
        "_meta": {"title": "Full continued audio"},
    }
    return [stitch2_id, 0], [final_audio_id, 0], next_id


def _wire_seedhunter_references(
    workflow: dict[str, Any],
    staged_refs: list[str],
    staged_videos: list[str],
    staged_audios: list[str],
    first_frame: str | None,
    last_frame: str | None,
    next_id: int,
    videos: list[dict[str, Any]] | None = None,
    audios: list[dict[str, Any]] | None = None,
) -> tuple[str, int]:
    """Appends LoadImage/LoadVideoUI/LoadAudioUI ref nodes (wired into node 11's
    ref_images/ref_videos/ref_audios) and MiniMaxH3AddGuide first/last-frame guide
    nodes (chained after node 11's conditioning). Mutates `workflow` in place.
    Returns (conditioning_node_id, next_free_id) — the node whose output slot 0
    downstream BasicGuider nodes should read `conditioning` from."""
    node11_inputs = workflow["11"]["inputs"]

    # ref_images/ref_videos/ref_audios are COMFY_AUTOGROW_V3 inputs - ComfyUI's raw
    # /prompt API only recognizes these as flat "<input_name>.<slot_name>" keys
    # (0-indexed, e.g. "ref_images.ref_image_0") directly on the node's own inputs
    # dict, not as a nested {"ref_images": {...}} dict (see build_workflow()'s
    # identical fix for node 20 - same underlying bug, same root cause).
    if staged_refs:
        for index, path in enumerate(staged_refs):
            node_id = str(next_id)
            next_id += 1
            workflow[node_id] = {"class_type": "LoadImage", "inputs": {"image": path}, "_meta": {"title": f"Reference image {index + 1}"}}
            node11_inputs[f"ref_images.ref_image_{index}"] = [node_id, 0]

    if staged_videos:
        for index, path in enumerate(staged_videos):
            ref = (videos or [])[index] if videos and index < len(videos) else {}
            node_id = str(next_id)
            next_id += 1
            workflow[node_id] = {
                "class_type": "LoadVideoUI",
                "inputs": {
                    "video": path, "start_time": max(0.0, ref.get("trim_start") or 0.0),
                    "end_time": ref.get("trim_end") or 0.0, "duration": 0,
                    "start_frame": 0, "end_frame": 0, "duration_frames": 0,
                    "resize_method": "maintain aspect ratio", "custom_width": 0, "custom_height": 0,
                    "frame_rate": 24, "display_mode": "seconds",
                    # Full frame, no crop - this node's own crop_x/y/w/h feature
                    # isn't exposed anywhere in Video's UI, so it must stay
                    # a no-op (crop_w/crop_h=0 would crop everything down to a
                    # sliver, since they're fractions of the frame, not pixels).
                    "crop_x": 0.0, "crop_y": 0.0, "crop_w": 1.0, "crop_h": 1.0,
                },
                "_meta": {"title": f"Reference video {index + 1}"},
            }
            node11_inputs[f"ref_videos.ref_video_{index}"] = [node_id, 0]

    if staged_audios:
        for index, path in enumerate(staged_audios):
            ref = (audios or [])[index] if audios and index < len(audios) else {}
            node_id = str(next_id)
            next_id += 1
            workflow[node_id] = {
                "class_type": "LoadAudioUI",
                "inputs": {
                    "audio": path, "start_time": max(0.0, ref.get("trim_start") or 0.0),
                    "end_time": ref.get("trim_end") or 0.0, "duration": 0,
                },
                "_meta": {"title": f"Reference audio {index + 1}"},
            }
            node11_inputs[f"ref_audios.ref_audio_{index}"] = [node_id, 0]

    conditioning_node = "11"
    for frame_idx, path, label in ((0, first_frame, "First"), (-1, last_frame, "Last")):
        if not path:
            continue
        image_id = str(next_id)
        next_id += 1
        workflow[image_id] = {"class_type": "LoadImage", "inputs": {"image": path}, "_meta": {"title": f"{label} frame guide image"}}
        guide_id = str(next_id)
        next_id += 1
        workflow[guide_id] = {
            "class_type": "MiniMaxH3AddGuide",
            "inputs": {
                "positive": [conditioning_node, 0],
                "latent": ["11", 1],
                "frame_idx": frame_idx,
                "vae": ["2", 0],
                "image": [image_id, 0],
            },
            "_meta": {"title": f"{label} frame guide"},
        }
        conditioning_node = guide_id

    return conditioning_node, next_id


def _stage_seedhunter_inputs(
    config: Settings, references: list[dict[str, Any]], frames: list[dict[str, Any]],
    videos: list[dict[str, Any]], audios: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str], str | None, str | None]:
    staged_refs = stage(config, references) if references else []
    staged_videos = stage(config, videos) if videos else []
    staged_audios = stage(config, audios) if audios else []
    staged_frames = stage(config, frames) if frames else []
    first_frame = staged_frames[0] if staged_frames else None
    last_frame = staged_frames[1] if len(staged_frames) > 1 else None
    return staged_refs, staged_videos, staged_audios, first_frame, last_frame


def build_seedhunter_candidates_workflow(
    config: Settings, request: GenerationRequest,
    references: list[dict[str, Any]], frames: list[dict[str, Any]],
    videos: list[dict[str, Any]], audios: list[dict[str, Any]],
    continuation_video_path: str | None = None,
) -> tuple[dict[str, Any], int, int]:
    if not config.seedhunter_candidates_workflow_path.is_file():
        raise GenerationError("workflow_missing", "The H3 SeedHunter candidates workflow file is not configured on this server.")
    try:
        workflow = json.loads(config.seedhunter_candidates_workflow_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationError("workflow_invalid", "The configured H3 SeedHunter workflow could not be loaded.") from exc

    width, height = (request.width, request.height) if request.width is not None else calculate_resolution(request.megapixels, request.aspect)
    length = calculate_seedhunter_length(request.duration)
    prompt = translate_seedhunter_aliases(request.prompt, references, videos, audios)
    staged_refs, staged_videos, staged_audios, first_frame, last_frame = _stage_seedhunter_inputs(config, references, frames, videos, audios)

    _prune_seedhunter_candidates(workflow, request.candidate_count)
    try:
        workflow["11"]["inputs"].update(prompt=prompt, width=width, height=height, length=length, ref_image_size=request.reference_sizing)
        workflow["9"]["inputs"]["sampler_name"] = request.sampler
        workflow["10"]["inputs"].update(scheduler=request.scheduler, steps=request.steps)

        if request.checkpoint:
            workflow["4"]["inputs"]["unet_name"] = request.checkpoint
        _wire_minimax_h3_loras(workflow, request.loras, "4", "1", [("5", "model")], next_id=500, extra=_test_loras(request))

        model_source, next_id = _maybe_insert_live_preview(workflow, request.enable_live_preview, next_id=200)
        conditioning_node, next_id = _wire_seedhunter_references(
            workflow, staged_refs, staged_videos, staged_audios, first_frame, last_frame, next_id=next_id,
            videos=videos, audios=audios,
        )
        if continuation_video_path:
            conditioning_node, _load_video_node, next_id = _wire_continuation_anchor(
                workflow, continuation_video_path, request.overlap_frames, width, height, conditioning_node, next_id,
            )
        combine_nodes = SEEDHUNTER_CANDIDATE_COMBINE_NODES[:request.candidate_count]
        for candidate in range(1, request.candidate_count + 1):
            guider_node = SEEDHUNTER_CANDIDATE_GUIDER_NODES[candidate]
            workflow[guider_node]["inputs"]["conditioning"] = [conditioning_node, 0]
            workflow[guider_node]["inputs"]["model"] = [model_source, 0]
            offset = SEEDHUNTER_CANDIDATE_SEED_OFFSETS[candidate]
            seed_node = str(int(guider_node) - 1)  # RandomNoise immediately precedes its BasicGuider in each branch
            workflow[seed_node]["inputs"]["noise_seed"] = request.seed + offset

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        for index, combine_node in enumerate(combine_nodes, start=1):
            workflow[combine_node]["inputs"]["filename_prefix"] = f"H3Machine_SeedHunter_cand{index}_{timestamp}"
    except (KeyError, TypeError) as exc:
        raise GenerationError("workflow_incompatible", "The configured H3 SeedHunter workflow is missing an expected node.") from exc
    _apply_h3_multi_gpu(workflow, request.multi_gpu_offload, request.multi_gpu_split_gb)
    return workflow, width, height


def build_seedhunter_finalize_workflow(
    config: Settings, original_request: GenerationRequest, finalize_request: Any,
    references: list[dict[str, Any]], frames: list[dict[str, Any]],
    videos: list[dict[str, Any]], audios: list[dict[str, Any]],
    continuation_video_path: str | None = None,
) -> tuple[dict[str, Any], int, int]:
    if not config.seedhunter_finalize_workflow_path.is_file():
        raise GenerationError("workflow_missing", "The H3 SeedHunter finalize workflow file is not configured on this server.")
    try:
        workflow = json.loads(config.seedhunter_finalize_workflow_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationError("workflow_invalid", "The configured H3 SeedHunter workflow could not be loaded.") from exc

    width, height = (
        (original_request.width, original_request.height)
        if original_request.width is not None
        else calculate_resolution(original_request.megapixels, original_request.aspect)
    )
    length = calculate_seedhunter_length(original_request.duration)
    prompt = translate_seedhunter_aliases(original_request.prompt, references, videos, audios)
    staged_refs, staged_videos, staged_audios, first_frame, last_frame = _stage_seedhunter_inputs(config, references, frames, videos, audios)

    try:
        workflow["11"]["inputs"].update(prompt=prompt, width=width, height=height, length=length, ref_image_size=original_request.reference_sizing)
        workflow["9"]["inputs"]["sampler_name"] = original_request.sampler
        workflow["10"]["inputs"].update(scheduler=original_request.scheduler, steps=original_request.steps)

        if original_request.checkpoint:
            workflow["4"]["inputs"]["unet_name"] = original_request.checkpoint
        _wire_minimax_h3_loras(workflow, original_request.loras, "4", "1", [("5", "model")], next_id=500, extra=_test_loras(original_request))

        model_source, next_id = _maybe_insert_live_preview(workflow, original_request.enable_live_preview, next_id=200)
        conditioning_node, next_id = _wire_seedhunter_references(
            workflow, staged_refs, staged_videos, staged_audios, first_frame, last_frame, next_id=next_id,
            videos=videos, audios=audios,
        )
        load_video_node: str | None = None
        if continuation_video_path:
            conditioning_node, load_video_node, next_id = _wire_continuation_anchor(
                workflow, continuation_video_path, original_request.overlap_frames, width, height, conditioning_node, next_id,
            )
        workflow["21"]["inputs"]["conditioning"] = [conditioning_node, 0]
        workflow["21"]["inputs"]["model"] = [model_source, 0]
        workflow["36"]["inputs"]["conditioning"] = [conditioning_node, 0]
        workflow["36"]["inputs"]["model"] = [model_source, 0]
        offset = SEEDHUNTER_CANDIDATE_SEED_OFFSETS[finalize_request.candidate]
        workflow["20"]["inputs"]["noise_seed"] = original_request.seed + offset

        workflow["31"]["inputs"].update(model_name=finalize_request.upscale_model)
        workflow["31"]["inputs"]["mode.megapixels"] = finalize_request.upscale_megapixels

        decode_source = ["22", 1]  # chosen-candidate denoised_output, no upscale
        if finalize_request.enable_upscale:
            workflow["33"]["inputs"]["sampler_name"] = original_request.sampler
            workflow["34"]["inputs"].update(scheduler=original_request.scheduler, steps=finalize_request.upscale_steps, denoise=finalize_request.upscale_denoise)
            workflow["34"]["inputs"]["model"] = [model_source, 0]
            workflow["35"]["inputs"]["noise_seed"] = finalize_request.upscale_seed
            decode_source = ["37", 0]  # "2nd Pass" output
        workflow["50"]["inputs"]["av_latent"] = decode_source

        combine_images_source: list = ["51", 0]
        combine_audio_source: list = ["52", 0]
        if continuation_video_path and load_video_node:
            combine_images_source, combine_audio_source, next_id = _wire_continuation_stitch(
                workflow, load_video_node, original_request.overlap_frames,
                combine_images_source, combine_audio_source, next_id,
            )

        if finalize_request.enable_rife:
            rife_id = str(next_id)
            next_id += 1
            workflow[rife_id] = {
                "class_type": "RIFEInterpolation",
                "inputs": {
                    "images": combine_images_source, "source_fps": 24, "target_fps": finalize_request.rife_target_fps,
                    "scale": 1, "model_name": "flownet.pkl", "batch_size": 8, "use_fp16": True,
                },
                "_meta": {"title": "RIFE frame interpolation"},
            }
            combine_images_source = [rife_id, 0]
            workflow["60"]["inputs"]["frame_rate"] = finalize_request.rife_target_fps
        workflow["60"]["inputs"]["images"] = combine_images_source
        workflow["60"]["inputs"]["audio"] = combine_audio_source
        workflow["60"]["inputs"]["filename_prefix"] = f"H3Machine_SeedHunter_final_{time.strftime('%Y%m%d-%H%M%S')}"
    except (KeyError, TypeError) as exc:
        raise GenerationError("workflow_incompatible", "The configured H3 SeedHunter finalize workflow is missing an expected node.") from exc
    _apply_h3_multi_gpu(workflow, original_request.multi_gpu_offload, original_request.multi_gpu_split_gb)
    return workflow, width, height


def resolve_references(store: Any, ids: list[str]) -> list[dict[str, Any]]:
    result = []
    for ref_id in ids:
        item = store.reference(ref_id)
        if not item:
            raise GenerationError("invalid_reference", "One of the selected references no longer exists.")
        result.append(item)
    return result
