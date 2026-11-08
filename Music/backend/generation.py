from __future__ import annotations

import copy
import json
import random
import time
from typing import Any

from . import loras as lora_gallery
from .config import Settings
from .models import MAX_SEED, GenerationRequest


class GenerationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


MINIMAX_MODEL_FILES = (
    "diffusion_models/minimax_music3_dit_int8_convrot.safetensors",
    "text_encoders/minimax_music3_text_encoder_pruned_int8_convrot.safetensors",
    "vae/minimax_music3_dav.safetensors",
)


def _load_template(config: Settings, model: str = "yue2") -> dict[str, Any]:
    path = config.minimax_workflow_path if model == "minimax_music3" else config.workflow_path
    if not path.is_file():
        raise GenerationError("workflow_missing", f"The {model} workflow file is not configured on this server.")
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationError("workflow_invalid", f"The configured {model} workflow could not be loaded.") from exc


def apply_loras(config: Settings, workflow: dict[str, Any], request: GenerationRequest, loader: str, clip_output: int | None) -> None:
    """Chain the picked gallery LoRAs after the model loader: every consumer of the loader's MODEL (and,
    for YuE2, its CLIP: the AR expert) is rewired to the last LoRA node."""
    if not request.loras:
        return
    model_ref, clip_ref = [loader, 0], ([loader, clip_output] if clip_output is not None else None)
    added: list[str] = []
    for n, pick in enumerate(request.loras):
        try:
            entry = lora_gallery.resolve(config.models_dir, pick.file, request.model)
        except ValueError as exc:
            raise GenerationError("lora_invalid", str(exc)) from exc
        node_id = f"lora{n + 1}"
        if clip_ref is not None:
            workflow[node_id] = {"class_type": "LoraLoader", "inputs": {
                "model": model_ref, "clip": clip_ref, "lora_name": entry["file"],
                "strength_model": pick.strength, "strength_clip": pick.strength if entry["clip"] else 0.0}}
            clip_ref = [node_id, 1]
        else:
            workflow[node_id] = {"class_type": "LoraLoaderModelOnly", "inputs": {
                "model": model_ref, "lora_name": entry["file"], "strength_model": pick.strength}}
        model_ref = [node_id, 0]
        added.append(node_id)
    for node_id, node in workflow.items():
        if node_id in added or not isinstance(node, dict):
            continue
        for name, value in (node.get("inputs") or {}).items():
            if value == [loader, 0]:
                node["inputs"][name] = model_ref
            elif clip_ref is not None and value == [loader, clip_output]:
                node["inputs"][name] = clip_ref


SCORE_PREVIEW = "91"   # PreviewAny on the YuE2 graph's score (node 48): the job's result "score"


def build_workflow(config: Settings, request: GenerationRequest) -> tuple[dict[str, Any], int]:
    """Deep-copy the YuE2-INT8 template and inject the per-request fields
    documented in BunnyBytes/workflows/NOTES.md's node-id map. Only the exact
    node inputs listed there are touched; every graph-internal reference
    (e.g. node 5's `seconds` <- node 22) is left exactly as the converted
    template has it.
    """
    if request.model == "minimax_music3":
        return build_minimax_workflow(config, request)
    template = _load_template(config)
    workflow = copy.deepcopy(template)

    seed = request.music_seed if request.music_seed is not None else (
        request.seed if request.seed is not None else random.randint(0, MAX_SEED)
    )

    try:
        # Style/genre + lyrics: written identically to both the ABC composer
        # (23) and the music generator (22).
        for node_id in ("22", "23"):
            node = workflow[node_id]["inputs"]
            node["style"] = request.style
            node["lyrics"] = request.lyrics
        # Remix: the composer writes the score from the original words (same seed, same score) while 22 sings the new ones.
        if request.score_lyrics.strip():
            workflow["23"]["inputs"]["lyrics"] = request.score_lyrics
            # Three independent seed fields fanned out from one API field, per
            # NOTES.md ("generate one random seed ... unless the user provides
            # one").
        workflow["22"]["inputs"]["seed"] = seed
        workflow["22"]["inputs"]["mode"] = request.music_mode
        workflow["23"]["inputs"]["seed"] = request.abc_seed if request.abc_seed is not None else seed
        workflow["23"]["inputs"].update({
            "mode": request.abc_mode,
            "max_abc_tokens": request.max_abc_tokens,
            "temperature": request.abc_temperature,
            "top_p": request.abc_top_p,
            "top_k": request.abc_top_k,
            "repetition_penalty": request.abc_repetition_penalty,
            "penalty_window": request.abc_penalty_window,
        })
        workflow["8"]["inputs"].update({
            "seed": request.sampler_seed if request.sampler_seed is not None else seed,
            "steps": request.steps,
            "cfg": request.cfg,
            "sampler_name": request.sampler_name,
            "scheduler": request.scheduler,
            "denoise": request.denoise,
        })

        # Requested max duration in seconds. Node 5's `seconds` stays wired as
        # a graph reference to node 22's second output -- never overwritten.
        workflow["22"]["inputs"]["max_duration"] = request.duration
        workflow["22"]["inputs"]["top_k"] = request.music_top_k

        # These are constants feeding the three YuE2 sampling expressions;
        # keeping the formulas and updating their common dial preserves the
        # workflow's reference-to-sampling relationship.
        for node_id in ("62", "63", "64"):
            workflow[node_id]["inputs"]["a"] = request.reference_strength
        if request.custom_music_sampling:
            workflow["22"]["inputs"].update({
                "temperature": request.music_temperature,
                "top_p": request.music_top_p,
                "repetition_penalty": request.music_repetition_penalty,
            })

        # Reference audio moved to covers (pack.py, task "cover"). The request fields stay so
        # saved jobs still load, but this graph always composes its own score.
        workflow["43"]["inputs"]["value"] = False
        workflow["42"]["inputs"]["value"] = False
        if request.score_abc.strip():
            # Remix with the saved score: sung exactly as written; the composer (23) then isn't run at all.
            workflow["22"]["inputs"]["abc"] = request.score_abc
        else:
            # Keep the score this song is sung from, so a later Remix can reuse it note for note.
            workflow[SCORE_PREVIEW] = {"class_type": "PreviewAny", "inputs": {"source": ["48", 0]}}
        workflow["17"]["inputs"]["tile_size"] = request.tile_size
        workflow["17"]["inputs"]["overlap"] = request.overlap
        # DynamicCombo V3 selects its option using the parent input value;
        # option-specific widgets (such as MP3/Opus quality) use dotted input
        # names. Passing the legacy nested dict makes ComfyUI drop this input
        # before calling SaveAudioAdvanced.execute().
        workflow["10"]["inputs"]["format"] = request.output_format
        if request.output_format in {"mp3", "opus"}:
            workflow["10"]["inputs"]["format.quality"] = request.output_quality

        # Output filename prefix so repeated generations don't collide;
        # format/output node left at the template's default (flac).
        workflow["10"]["inputs"]["filename_prefix"] = f"audio/Music_{time.strftime('%Y%m%d-%H%M%S')}"
    except (KeyError, TypeError) as exc:
        raise GenerationError("workflow_incompatible", "The configured workflow is missing an expected YuE2 node.") from exc

    apply_loras(config, workflow, request, "15", 1)      # CheckpointLoaderSimple: MODEL 0, CLIP 1
    if request.voice_id:
        # Between the decoded song and the save node, so every form setting still applies.
        workflow["rsp_voice_audio"] = {"class_type": "RSPCustomVoice", "inputs": {"voice": request.voice_id}}
        workflow["rsp_voice"] = {"class_type": "RSPApplyVoice", "inputs": {
            "song": workflow["10"]["inputs"]["audio"], "voice_audio": ["rsp_voice_audio", 0],
            "voice_strength": request.voice_strength, "voice_semitones": request.voice_semitones,
            "conversion_steps": request.conversion_steps}}
        workflow["10"]["inputs"]["audio"] = ["rsp_voice", 0]
    return workflow, seed


def build_minimax_workflow(config: Settings, request: GenerationRequest) -> tuple[dict[str, Any], int]:
    """Build the native ComfyUI MiniMax Music 3 graph from its INT8 template."""
    workflow = copy.deepcopy(_load_template(config, "minimax_music3"))
    seed = request.seed if request.seed is not None else random.randint(0, MAX_SEED)
    try:
        workflow["13"]["inputs"].update(caption=request.style, lyrics=request.lyrics, seed=seed,
                                          max_duration=request.duration, cfg_scale=request.minimax_cfg)
        workflow["9"]["inputs"].update(seed=seed, steps=request.minimax_steps, cfg=request.minimax_cfg)
        workflow["10"]["inputs"]["audio"] = ["42" if request.minimax_tiled_decode else "12", 0]
        workflow["10"]["inputs"]["format"] = request.output_format
        if request.output_format in {"mp3", "opus"}:
            workflow["10"]["inputs"]["format.quality"] = request.output_quality
        workflow["10"]["inputs"]["filename_prefix"] = f"audio/MiniMaxMusic3_{time.strftime('%Y%m%d-%H%M%S')}"
    except (KeyError, TypeError) as exc:
        raise GenerationError("workflow_incompatible", "The MiniMax Music 3 workflow is missing an expected node.") from exc
    apply_loras(config, workflow, request, "6", None)    # UNETLoader; LoRAs train the music DiT only
    return workflow, seed
