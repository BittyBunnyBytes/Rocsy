"""Estimated time to complete: the average render time of the last few
finished jobs with the same render-time-relevant settings.

A job's render time is completed_at - started_at, i.e. from ComfyUI
submission (after the GPU queue) to the saved output. Only settings that
change how long a render takes go into the key - never the prompt, seed or
LoRA strengths - and only the ones that apply to that model/mode, so an
unused LTX field can't split two otherwise identical H3 runs apart."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .generation import REF2V_TURBO_LORA_CHECKPOINT, calculate_ltx_resolution, calculate_resolution, calculate_wan_resolution, reference_category, wan_high_steps
from .models import FFMPEG_MODELS, LTX_LORAS, LTX_WORKFLOW_FILES, MINIMAX_H3_LORA_SLOTS, MODE_LABELS, MODEL_LABELS, WAN_LORAS, GenerationRequest

SAMPLE_SIZE = 10  # average of the last 10 matching runs (or however many exist, 1-9)
# "Lots of best-quality results": at least this many extreme thumbs ups, making
# up at least half the ratings. The UI draws those scores with a gradient.
LEGENDARY_MIN_BEST = 3
LEGENDARY_MIN_SHARE = 0.5
# Tap-to-pick answers for "why was this best quality?" on an extreme thumbs up.
RATING_REASONS = {
    "no_artifacts": "No artifacting",
    "no_animation_errors": "No animation errors",
    "lip_sync": "Lip syncing",
    "realism": "Realism",
    "stylization": "Stylization",
    "prompt_adherence": "Prompt adherence",
    "smooth_motion": "Smooth motion",
    "consistency": "Character consistency",
    "audio": "Audio / voice",
    "composition": "Composition / camera",
    # Only offered for jobs that used them (see rating_reason_keys).
    "reference_adherence": "Adherence to references",
    "keyframe_use": "Utilization of keyframe",
    "start_end_coherent": "Starts and ends coherent",
}
_MODE_REASONS = {"reference_adherence", "keyframe_use", "start_end_coherent"}


def rating_reason_keys(item: dict[str, Any]) -> list[str]:
    """The questionnaire answers that apply to this job: the general ones,
    plus reference adherence in ref mode, keyframe use in keyframe mode and
    start/end coherence when it had both a first and a last frame."""
    keys = [key for key in RATING_REASONS if key not in _MODE_REASONS]
    try:
        request = GenerationRequest.model_validate_json(item["request_json"])
    except Exception:  # noqa: BLE001 - an older schema just gets the general answers
        return keys
    if request.mode == "ref2v":
        keys.append("reference_adherence")
    if request.mode == "fl2v":
        keys.append("keyframe_use")
        if request.last_frame_id:
            keys.append("start_end_coherent")
    return keys


def render_settings(request: GenerationRequest, width: int, height: int, categories: list[str]) -> dict[str, Any]:
    model, mode = request.model, request.mode
    settings: dict[str, Any] = {"model": model, "width": width, "height": height, "duration": float(request.duration)}
    if model == "ltx_2_5":
        settings.update(
            mode=mode, workflow=LTX_WORKFLOW_FILES.get(request.ltx_workflow, request.ltx_workflow),
            fps=request.ltx_fps, tiles=request.ltx_tile_preset, loras=len(request.ltx_loras),
            last_frame=bool(request.last_frame_id) if mode == "fl2v" else None,
        )
    elif model == "wan_2_2":
        # Both experts run every step; LoRAs add a little patching time, RIFE a pass after decode.
        settings.update(mode=mode, steps=request.steps, turbo=request.wan_turbo != "off",
                        cfg=request.wan_cfg > 1, loras=len(request.wan_loras),
                        fps=request.wan_fps if request.wan_fps != 16 else None,
                        last_frame=bool(request.last_frame_id) if mode == "fl2v" else None)
    elif model == "seedhunter":
        settings.update(
            steps=request.steps, checkpoint=request.checkpoint, candidates=request.candidate_count,
            live_preview=request.enable_live_preview, loras=sum(1 for slot in request.loras if slot.enabled),
            continued=bool(request.continue_from_job_id),
        )
    else:
        ref2v = mode == "ref2v"
        pdd = (ref2v and request.ref2v_turbo_enabled and request.ref2v_turbo == "pdd"
               and (not request.checkpoint or request.checkpoint == REF2V_TURBO_LORA_CHECKPOINT))
        settings.update(
            mode=mode, steps="pdd" if pdd else request.steps, checkpoint=request.checkpoint,
            loras=sum(1 for slot in request.loras if slot.enabled),
            kitchen=request.attention == "kitchen", block_cache=request.block_cache_enabled,
            ffn_chunking=request.ffn_chunking_enabled,
        )
        if request.attention == "flash":  # added only for flash, so Kitchen/PyTorch runs from before keep matching
            settings["attention"] = "flash"
        if request.low_vram_attention_enabled:  # only when on, so earlier runs keep matching
            settings["low_vram_attention"] = request.low_vram_attention_chunks
        if request.h3_fps != 24:  # RIFE/frame drop pass; left out at 24 so older runs still match
            settings["fps"] = request.h3_fps
        if request.cfg > 1:  # CFG runs a second (negative) forward per step
            settings["cfg"] = True
        if request.enable_upscale:  # latent upscale + second pass; width/height stay the render size
            settings["upscale"] = [request.upscale_factor, request.upscale_steps, request.upscale_denoise]
            if not request.upscale_chunking:  # older (pre-chunking) runs keep matching: short clips were one chunk anyway
                settings["upscale"].append("unchunked")
            if advanced := request.upscale_advanced.model_dump(exclude_defaults=True):  # only non-defaults, so older runs keep matching
                settings["upscale"].append(advanced)
        if ref2v:
            settings.update(turbo=request.ref2v_turbo if request.ref2v_turbo_enabled else None,
                            ref_video_downscale=request.ref_video_downscale,
                            experimental_ref_speedups=request.experimental_ref_speedups,
                            ref_sla=request.sla_enabled if request.experimental_ref_speedups else None,
                            ref_sla_sparsity=request.sla_sparsity if request.experimental_ref_speedups and request.sla_enabled else None,
                            ref_spectrum=request.spectrum_enabled if request.experimental_ref_speedups else None)
        else:
            settings.update(parasyte=request.parasyte_enabled, sla=request.sla_enabled,
                            sla_dense_step=request.sla_dense_step if request.sla_enabled else None,
                            spectrum=request.spectrum_enabled,
                            last_frame=bool(request.last_frame_id) if mode == "fl2v" else None)
    if categories:
        settings["references"] = {category: categories.count(category) for category in sorted(set(categories))}
    return {key: value for key, value in settings.items() if value is not None}


def quality_settings(request: GenerationRequest, width: int, height: int, categories: list[str]) -> dict[str, Any]:
    """The time-estimate settings plus the ones that change how a video looks
    but not how long it takes, so the thumbs up/down score tells them apart."""
    settings = render_settings(request, width, height, categories)
    model = request.model
    settings.update(sampler=request.sampler, scheduler=request.scheduler, steps=request.steps)
    if model == "ltx_2_5":
        settings["loras"] = [[item.lora, item.strength] for item in request.ltx_loras]
        return settings
    if model == "wan_2_2":
        settings.update(loras=[[item.lora, item.strength] for item in request.wan_loras], turbo=request.wan_turbo,
                        cfg=[request.wan_cfg, request.negative_prompt] if request.wan_cfg > 1 else None,
                        shift=request.wan_shift, high_steps=wan_high_steps(request))
        return {key: value for key, value in settings.items() if value is not None}
    settings["loras"] = [[index, slot.strength] for index, slot in enumerate(request.loras) if slot.enabled]
    if model == "minimax_h3":
        ref2v = request.mode == "ref2v"
        settings.update(
            parasyte_strength=request.parasyte_strength if request.parasyte_enabled and not ref2v else None,
            ref2v_strength=request.ref2v_strength if ref2v and request.ref2v_turbo_enabled and request.ref2v_turbo == "lightx" else None,
            sla_sparsity=request.sla_sparsity if request.sla_enabled else None,
            reference_sizing=request.reference_sizing if ref2v else None,
            shift_video=request.sigma_shift_video, shift_audio=request.sigma_shift_audio,
            cfg=[request.cfg, request.negative_prompt] if request.cfg > 1 else None,
            output_quality=request.output_quality if request.output_quality != "standard" else None,
        )
        if request.enable_upscale:
            settings["upscale"] = [request.upscale_factor, request.upscale_steps, request.upscale_denoise]
            if not request.upscale_chunking:  # older (pre-chunking) runs keep matching: short clips were one chunk anyway
                settings["upscale"].append("unchunked")
            if advanced := request.upscale_advanced.model_dump(exclude_defaults=True):  # only non-defaults, so older runs keep matching
                settings["upscale"].append(advanced)
    return {key: value for key, value in settings.items() if value is not None}


def settings_key(settings: dict[str, Any]) -> str:
    return json.dumps(settings, sort_keys=True, separators=(",", ":"))


def request_reference_ids(request: GenerationRequest) -> list[str]:
    if request.model == "seedhunter":
        return [*request.reference_ids, *request.reference_video_ids, *request.reference_audio_ids]
    return list(request.reference_ids) if request.mode == "ref2v" else []


def reference_categories(ids: list[str], known: dict[str, dict[str, Any]]) -> list[str]:
    return [reference_category(known[ref_id]) if ref_id in known else "unknown" for ref_id in ids]


def output_size(request: GenerationRequest) -> tuple[int, int]:
    if request.width is not None and request.height is not None:
        return request.width, request.height
    calculator = {"ltx_2_5": calculate_ltx_resolution, "wan_2_2": calculate_wan_resolution}.get(request.model, calculate_resolution)
    return calculator(request.megapixels, request.aspect)


def render_seconds(item: dict[str, Any]) -> float | None:
    if not item.get("started_at") or not item.get("completed_at"):
        return None
    seconds = (datetime.fromisoformat(item["completed_at"]) - datetime.fromisoformat(item["started_at"])).total_seconds()
    return seconds if seconds > 0 else None


def job_settings(item: dict[str, Any], known: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """A stored job's render settings, or None for jobs that can't be timed
    (stitches, unparseable old requests)."""
    if item.get("model") in FFMPEG_MODELS:
        return None
    try:
        request = GenerationRequest.model_validate_json(item["request_json"])
    except Exception:  # noqa: BLE001 - a request from an older schema just isn't timed
        return None
    width, height = (item["width"], item["height"]) if item.get("width") and item.get("height") else output_size(request)
    if request.model == "minimax_h3" and request.enable_upscale:
        width, height = output_size(request)  # the job stores the upscaled output size
    return render_settings(request, width, height, reference_categories(request_reference_ids(request), known))


def job_quality_settings(item: dict[str, Any], known: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """A stored job's quality settings (see quality_settings), or None."""
    if item.get("model") in FFMPEG_MODELS:
        return None
    try:
        request = GenerationRequest.model_validate_json(item["request_json"])
    except Exception:  # noqa: BLE001 - an older schema just isn't scored
        return None
    width, height = output_size(request)  # the render size, like the form sends
    return quality_settings(request, width, height, reference_categories(request_reference_ids(request), known))


def score(store: Any, settings: dict[str, Any]) -> dict[str, int]:
    good, bad, best = store.rating_counts(settings_key(settings))
    return {"good": good, "bad": bad, "best": best, "legendary": is_legendary(good, bad, best)}


def is_legendary(good: int, bad: int, best: int) -> bool:
    return best >= LEGENDARY_MIN_BEST and best >= (good + bad) * LEGENDARY_MIN_SHARE


def rank_points(points: int, rated: int) -> float:
    """Average rating (extreme 2, up 1, down -1) with one extra neutral vote,
    so a single lucky render can't outrank a combo proven over many."""
    return points / (rated + 1)


_SETTING_LABELS = {
    "mode": "Mode", "duration": "Duration", "steps": "Steps", "sampler": "Sampler", "scheduler": "Scheduler",
    "checkpoint": "Checkpoint", "workflow": "Workflow", "fps": "FPS", "tiles": "Tiles", "cfg": "CFG",
    "shift_video": "Shift (video)", "shift_audio": "Shift (audio)", "upscale": "Upscale", "loras": "LoRAs",
    "turbo": "Ref turbo", "ref2v_strength": "Ref turbo strength", "ref_video_downscale": "Ref video downscale",
    "reference_sizing": "Reference sizing", "references": "References", "parasyte": "Parasyte",
    "parasyte_strength": "Parasyte strength", "sla": "SLA", "sla_sparsity": "SLA sparsity",
    "sla_dense_step": "SLA dense step", "spectrum": "Spectrum", "kitchen": "Kitchen attention", "attention": "Attention", "low_vram_attention": "Low-VRAM attention (head groups)",
    "block_cache": "Block cache", "ffn_chunking": "FFN chunking", "output_quality": "Output quality",
    "last_frame": "Last frame", "candidates": "Candidates", "live_preview": "Live preview", "continued": "Continued",
}


def _setting_value(key: str, value: Any, model: str) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if key == "mode":
        return MODE_LABELS.get(value, str(value))
    if key == "duration":
        return f"{value:g}s"
    if key == "loras":
        if not value:
            return "none"
        names = (dict(LTX_LORAS) if model == "ltx_2_5" else dict(WAN_LORAS) if model == "wan_2_2"
                 else dict(enumerate(label for _, label in MINIMAX_H3_LORA_SLOTS)))
        return ", ".join(f"{names.get(lora, lora)} @{strength:g}" for lora, strength in value)
    if key == "upscale":
        factor, steps, denoise, *extra = value
        text = f"{factor:g}x, {steps} steps, denoise {denoise:g}"
        for item in extra:
            text += ", " + (item if isinstance(item, str) else ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in item.items()))
        return text
    if key == "cfg":
        cfg, negative = value
        return f"{cfg:g}" + (f" (neg: {negative[:60]})" if negative else "")
    if key == "checkpoint":
        return str(value).removesuffix(".safetensors")
    if key == "references":
        return ", ".join(f"{count} {category}" for category, count in value.items())
    return str(value)


def describe_settings(settings: dict[str, Any]) -> list[list[str]]:
    model = settings.get("model", "")
    return [[_SETTING_LABELS.get(key, key.replace("_", " ").capitalize()), _setting_value(key, value, model)]
            for key, value in settings.items() if key not in ("model", "width", "height")]


def leaderboard(store: Any, limit: int = 30) -> list[dict[str, Any]]:
    """The highest-scoring rated settings combos, best first, with how long
    their rated renders took on average."""
    rows = []
    for row in store.rated_settings():
        settings = json.loads(row["settings_json"])
        good, bad, best = int(row["good"]), int(row["bad"]), int(row["best"])
        model = settings.get("model", "")
        rows.append({
            "key": row["quality_key"], "model": model, "model_label": MODEL_LABELS.get(model, model),
            "width": settings.get("width"), "height": settings.get("height"),
            "settings": describe_settings(settings),
            "good": good, "bad": bad, "best": best, "legendary": is_legendary(good, bad, best),
            "score": round(rank_points(int(row["points"]), good + bad), 3),
            "seconds": round(row["seconds"]) if row["seconds"] is not None else None, "timed": int(row["timed"]),
            "reasons": [[RATING_REASONS[key], count] for key, count in
                        sorted(row["reasons"].items(), key=lambda pair: -pair[1]) if key in RATING_REASONS],
        })
    rows.sort(key=lambda item: (item["score"], item["best"], item["good"] + item["bad"]), reverse=True)
    return rows[:limit]


def uses_torch_compile(item: dict[str, Any]) -> bool:
    try:
        return bool(json.loads(item["request_json"]).get("torch_compile_enabled"))
    except Exception:  # noqa: BLE001
        return False


# torch.compile recompiles ("cools off") when the model's graph or tensor
# shapes change: resolution, duration, mode, references, checkpoint and the
# attention/caching patches. These render-time settings don't touch it.
_COMPILE_IGNORED = ("steps", "loras", "fps")


def compile_signature(settings: dict[str, Any]) -> dict[str, Any]:
    """The part of render_settings() a warm torch.compile depends on."""
    return {key: value for key, value in settings.items() if key not in _COMPILE_IGNORED}


def compile_changes(warm: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Labels of the settings that differ from the warm compile's."""
    labels: list[str] = []
    for key in sorted(set(warm) | set(current)):
        if warm.get(key) == current.get(key):
            continue
        label = ("Resolution" if key in ("width", "height") else "Model" if key == "model"
                 else _SETTING_LABELS.get(key, key.replace("_", " ").capitalize()))
        if label not in labels:
            labels.append(label)
    return labels


def record(store: Any, item: dict[str, Any], known: dict[str, dict[str, Any]]) -> bool:
    if uses_torch_compile(item):
        return False  # compile time varies run to run, so it stays out of the estimate
    seconds = render_seconds(item)
    settings = job_settings(item, known) if seconds else None
    if not settings:
        return False
    store.record_render_time(item["id"], settings_key(settings), json.dumps(settings), seconds, item["completed_at"])
    return True


def step_fit(store: Any, settings: dict[str, Any]) -> dict[str, Any] | None:
    """No run with exactly these settings yet: fit time = overhead + per_step * steps
    by least squares over runs whose settings match in everything except the
    step count (the last SAMPLE_SIZE runs of each step count), and read it off
    at this step count. Needs two or more different step counts. Sampling time
    is linear in steps (every step is one forward pass of the same size) and the
    model load, text encode and VAE decode are the fixed part."""
    steps = settings.get("steps")
    if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1:
        return None  # e.g. "pdd": a fixed 8-step schedule
    base = {key: value for key, value in settings.items() if key != "steps"}
    by_steps: dict[int, list[float]] = {}
    for settings_json, seconds in store.render_times_all():
        other = dict(_parsed_settings(settings_json))  # a copy: "steps" is popped below
        other_steps = other.pop("steps", None)
        if (isinstance(other_steps, int) and not isinstance(other_steps, bool) and other_steps != steps
                and other == base and len(by_steps.setdefault(other_steps, [])) < SAMPLE_SIZE):
            by_steps[other_steps].append(seconds)
    by_steps = {count: times for count, times in by_steps.items() if times}
    if len(by_steps) < 2:
        return None
    points = [(count, seconds) for count, times in by_steps.items() for seconds in times]
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    per_step = sum((x - mean_x) * (y - mean_y) for x, y in points) / sxx
    overhead = mean_y - per_step * mean_x
    if overhead < 0:  # can't take negative time at 0 steps: fit through the origin instead
        overhead, per_step = 0.0, sum(x * y for x, y in points) / sum(x * x for x, _ in points)
    if per_step <= 0:
        return None  # more steps never makes it faster; the runs are too noisy to fit
    predicted = overhead + per_step * steps
    ss_tot = sum((y - mean_y) ** 2 for _, y in points)
    ss_res = sum((y - (overhead + per_step * x)) ** 2 for x, y in points)
    counts = sorted(by_steps)
    return {
        "seconds": round(predicted),
        "overhead": round(overhead, 1),
        "per_step": round(per_step, 2),
        "r2": round(1 - ss_res / ss_tot, 3) if ss_tot > 0 else None,
        "runs": n,
        "steps": counts,
        "extrapolated": not counts[0] <= steps <= counts[-1],
    }


_PARSED: dict[str, dict[str, Any]] = {}


def _parsed_settings(settings_json: str) -> dict[str, Any]:
    """step_fit re-parsed every render_times row on each estimate (~20 ms at
    5k rows, on the event loop); rows never change, so parse each once."""
    parsed = _PARSED.get(settings_json)
    if parsed is None:
        if len(_PARSED) > 50_000:
            _PARSED.clear()
        parsed = _PARSED[settings_json] = json.loads(settings_json)
    return parsed


def estimate(store: Any, settings: dict[str, Any]) -> dict[str, Any]:
    times = store.recent_render_times(settings_key(settings), SAMPLE_SIZE)
    if times:
        return {"seconds": round(sum(times) / len(times)), "samples": len(times)}
    fit = step_fit(store, settings)
    if fit:
        return {"seconds": fit["seconds"], "samples": 0, "fit": fit}
    return {"seconds": None, "samples": 0}
