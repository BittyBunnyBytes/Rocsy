"""The info panel on a job card: every setting a generation ran with, read
back from its stored request_json and labelled for display.

CFG (and LTX's sampler/steps) is never set by the app - it's fixed in the
workflow templates - so those rows are traced from the *current* template
file for that model; the panel footnote says so."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings, local_variant
from .generation import (
    H3_LATENT_UPSCALER, PDD_AUDIO_SHIFT, PDD_EVALUATIONS, PDD_VIDEO_SHIFT, REF2V_TURBO_LORA_CHECKPOINT, _frontend_workflow_to_prompt,
    calculate_resolution, h3_frame_count, h3_latent_frames, h3_upscale_chunk_plan, h3_upscale_segments, h3_upscale_target,
    h3_upscale_tile_grid,
    reference_token, WAN_EXPERTS, wan_frame_count,
)
from .models import (
    FFMPEG_MODELS, GenerationRequest, LTX_LORAS, LTX_WORKFLOW_FILES, LTX_WORKFLOWS, MINIMAX_H3_CHECKPOINTS, MINIMAX_H3_LORA_SLOTS, MODE_LABELS, MODEL_LABELS,
    WAN_LORAS,
)

_CHECKPOINT_LABELS = dict(MINIMAX_H3_CHECKPOINTS)
_LTX_LORA_LABELS = dict(LTX_LORAS)
_WAN_LORA_LABELS = dict(WAN_LORAS)
_WAN_TURBO_LABELS = {"lightx2v": "lightx2v 4-step", "motionfix": "Slow-Mo Fix (rzgar)", "off": "Off"}
_LTX_WORKFLOW_LABELS = dict(LTX_WORKFLOWS)

Row = list[str]


_OUTPUT_QUALITY_LABELS = {
    "standard": "Standard (H.264, CRF 19)", "high": "High (H.264, CRF 12)",
    "near_lossless": "Near-lossless (H.264, CRF 4)", "hevc_10bit": "10-bit HEVC (CRF 12)",
}

def _on(value: Any) -> str:
    return "On" if value else "Off"


def _num(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _duration(start: str | None, end: str | None) -> str | None:
    if not start or not end:
        return None
    seconds = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    minutes, seconds = divmod(max(0, seconds), 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def _load_api_template(path: Path, output_node: int | None = None) -> dict[str, Any] | None:
    try:
        workflow = json.loads(path.read_text())
        return _frontend_workflow_to_prompt(workflow, output_node) if output_node is not None else workflow
    except Exception:  # noqa: BLE001 - a missing/changed template only drops these rows
        return None


def _linked(prompt: dict[str, Any], value: Any) -> dict[str, Any] | None:
    return prompt.get(str(value[0])) if isinstance(value, list) and value else None


def _guidance(guider: dict[str, Any] | None) -> str | None:
    if not guider:
        return None
    inputs = guider.get("inputs", {})
    cfgs = {key: val for key, val in inputs.items() if "cfg" in key.lower() and not isinstance(val, list)}
    if cfgs:
        if len(cfgs) == 1:
            return _num(next(iter(cfgs.values())))
        return ", ".join(f"{key.replace('_cfg', '')} {_num(val)}" for key, val in cfgs.items())
    if guider.get("class_type") == "BasicGuider":
        return "1 (BasicGuider - no CFG / negative prompt)"
    return None


def _template_stages(prompt: dict[str, Any] | None) -> list[dict[str, str]]:
    """Sampler / steps / CFG for each SamplerCustomAdvanced in a template."""
    if not prompt:
        return []
    stages = []
    for node_id in sorted((key for key, node in prompt.items() if node.get("class_type") == "SamplerCustomAdvanced"), key=int):
        inputs = prompt[node_id]["inputs"]
        stage: dict[str, str] = {}
        sampler = _linked(prompt, inputs.get("sampler"))
        if sampler and "sampler_name" in sampler.get("inputs", {}):
            stage["sampler"] = str(sampler["inputs"]["sampler_name"])
        sigmas = _linked(prompt, inputs.get("sigmas"))
        if sigmas and sigmas.get("class_type") == "ManualSigmas":
            values = [part for part in str(sigmas["inputs"].get("sigmas", "")).split(",") if part.strip()]
            stage["steps"] = f"{max(0, len(values) - 1)} (manual sigmas)"
        elif sigmas and "steps" in sigmas.get("inputs", {}):
            stage["steps"] = _num(sigmas["inputs"]["steps"])
        cfg = _guidance(_linked(prompt, inputs.get("guider")))
        if cfg:
            stage["cfg"] = cfg
        stages.append(stage)
    return stages


def _references(ids: list[str | None], by_id: dict[str, dict[str, Any]]) -> str | None:
    names = []
    for ref_id in ids:
        if not ref_id:
            continue
        ref = by_id.get(ref_id)
        if not ref:
            names.append("(deleted reference)")
            continue
        token = reference_token(ref) or ref.get("alias") or ""
        label = f" “{ref['label']}”" if ref.get("label") else ""
        trim = ""
        if ref.get("trim_start") or ref.get("trim_end"):
            trim = f" [{_num(ref.get('trim_start') or 0)}s-{_num(ref['trim_end']) + 's' if ref.get('trim_end') else 'end'}]"
        muted = " (muted)" if ref.get("muted") else ""
        names.append(f"{token} {ref.get('filename') or ''}{label}{trim}{muted}".strip())
    return "\n".join(names) or None


def job_details(config: Settings, item: dict[str, Any], references: list[dict[str, Any]], snapshot: dict[str, Any] | None) -> dict[str, Any]:
    request = json.loads(item.get("request_json") or "{}")
    model = item.get("model") or "minimax_h3"
    mode = item.get("mode") or ""
    # Live references first; the retry snapshot fills in any since deleted.
    by_id = {ref["id"]: ref for ref in (snapshot or {}).get("references", [])}
    by_id.update({ref["id"]: ref for ref in references})
    sections: list[dict[str, Any]] = []

    def section(title: str, rows: list[Row | None]) -> None:
        rows = [row for row in rows if row and row[1] not in (None, "")]
        if rows:
            sections.append({"title": title, "rows": rows})

    if model == "postedit":
        voices = request.get("voices") or []
        section("Post-Edit", [
            ["Source job", (request.get("source_job_id") or "")[:8]],
            # The prompt is "<source prompt> · <summary>", and a Post-Edit source's own
            # prompt has " · " in it too: the summary is the last part, not everything after the first.
            ["Edits", item.get("prompt", "").rsplit(" · ", 1)[-1]],
            ["Trim", f"{_num(request.get('trim_start') or 0)}s - {_num(request['trim_end']) + 's' if request.get('trim_end') is not None else 'end'}"],
            ["Speed", f"{_num(request['speed'])}x" if request.get("speed") not in (None, 1, 1.0) else None],
            ["Crop", "yes" if request.get("crop") else None],
            ["Size", f"{request['width']}x{request['height']}" if request.get("width") else None],
            ["FPS", str(request["fps"]) + (" (interpolated)" if request.get("interpolate") else "") if request.get("fps") else None],
            ["Music", _references([request.get("music_reference_id")], by_id)],
            ["Music part", f"{_num(request.get('music_in') or 0)}s - {_num(request['music_out']) + 's' if request.get('music_out') is not None else 'end'}" if request.get("music_reference_id") else None],
            ["Voice clips", _references([v.get("reference_id") for v in voices], by_id) if voices else None],
            ["Captions", "burned in" if request.get("captions") else None],
            ["Text overlays", str(len(request.get("texts") or [])) if request.get("texts") else None],
            ["Quality", request.get("quality")],
        ])
    elif model == "stitch":
        section("Stitch", [
            ["Clips", str(len(request.get("source_job_ids") or []))],
            ["Transition", request.get("transition")],
            ["Crossfade", f"{_num(request['crossfade_seconds'])}s" if request.get("transition") != "cut" and request.get("crossfade_seconds") else None],
            ["FPS", _num(request["fps"]) if request.get("fps") else None],
            ["Music", _references([request.get("music_reference_id")], by_id)],
        ])
    else:
        ltx = model == "ltx_2_5"
        width, height = item.get("width"), item.get("height")
        wan = model == "wan_2_2"
        fps = item.get("fps") or (request.get("ltx_fps") if ltx else request.get("wan_fps") or 16 if wan else request.get("h3_fps") or 24)
        section("Output", [
            ["Model", MODEL_LABELS.get(model, model)],
            ["Mode", MODE_LABELS.get(mode, mode)],
            ["Resolution", f"{width}×{height}" + (f" ({width * height / 1_000_000:.2f} MP)" if width and height else "") if width else None],
            ["Aspect", request.get("aspect") if request.get("width") is None else None],
            ["Duration", f"{_num(item.get('duration') or request.get('duration'))}s"],
            ["FPS", _num(fps) if fps else None],
            # What H3 actually renders: the requested length snapped up to its 17k+5 frame grid.
            ["Frames", f"{h3_frame_count(float(request.get('duration') or 0))} at 24 fps" if model == "minimax_h3"
             else f"{wan_frame_count(float(request.get('duration') or 0))} at 16 fps" if wan else None],
        ])

        if wan:
            steps = int(request.get("steps") or 4)
            high = request.get("wan_high_steps") or max(1, steps // 2)
            section("Sampling", [
                ["Seed", _num(request.get("seed"))], ["Steps", steps], ["High-noise steps", high],
                ["Sampler", request.get("sampler")], ["Scheduler", request.get("scheduler")],
                ["CFG", _num(request.get("wan_cfg", 1.0))], ["Shift", _num(request.get("wan_shift", 8.0))],
                ["Negative prompt", request.get("negative_prompt") or None],
            ])
            experts = WAN_EXPERTS["fl2v" if mode == "fl2v" else "text"]
            section("Workflow", [
                ["Workflow", f"Wan 2.2 A14B ({config.wan_workflow_path.name})"],
                ["Experts", "\n".join(experts)],
                ["Turbo", _WAN_TURBO_LABELS.get(request.get("wan_turbo") or "lightx2v")],
                ["LoRAs", "\n".join(
                    f"{_WAN_LORA_LABELS.get(lora['lora'], lora['lora'])} @ {_num(lora.get('strength', 1.0))}"
                    for lora in request.get("wan_loras") or []
                ) or "None"],
                ["Output quality", _OUTPUT_QUALITY_LABELS.get(request.get("output_quality") or "standard")],
            ])
        elif ltx:
            workflow_key = request.get("ltx_workflow") or "final_optimized"
            file_name = LTX_WORKFLOW_FILES.get(workflow_key)
            stages = _template_stages(_load_api_template(local_variant(config.ltx_workflow_path.parent / file_name), 75)) if file_name else []
            rows: list[Row | None] = [["Seed", _num(request.get("seed"))]]
            for index, stage in enumerate(stages, start=1):
                prefix = f"Stage {index} " if len(stages) > 1 else ""
                rows += [[f"{prefix}sampler", stage.get("sampler")], [f"{prefix}steps", stage.get("steps")],
                         [f"{prefix}CFG", stage.get("cfg")]]
            section("Sampling", rows)
            section("Workflow", [
                ["Workflow", f"{_LTX_WORKFLOW_LABELS.get(workflow_key, workflow_key)} ({file_name})"],
                ["VAE decode tiles", (request.get("ltx_tile_preset") or "").title()],
                ["LoRAs", "\n".join(
                    f"{_LTX_LORA_LABELS.get(lora['lora'], lora['lora'])} @ {_num(lora.get('strength', 1.0))}"
                    for lora in request.get("ltx_loras") or []
                ) or "None"],
            ])
        else:
            # PDD (Reference turbo) swaps in its own fixed sigma schedule - see _apply_ref2v_pdd.
            pdd = (model == "minimax_h3" and mode == "ref2v" and request.get("ref2v_turbo_enabled", True)
                   and request.get("ref2v_turbo", "lightx") == "pdd"
                   and (not request.get("checkpoint") or request.get("checkpoint") == REF2V_TURBO_LORA_CHECKPOINT))
            template_path = config.seedhunter_candidates_workflow_path if model == "seedhunter" else config.workflow_path
            template = _load_api_template(template_path)
            stages = _template_stages(template)
            section("Sampling", [
                ["Seed", _num(request.get("seed")) + (" (+0/+1/+2 per candidate)" if model == "seedhunter" and (request.get("candidate_count") or 1) > 1 else "")],
                ["Steps", f"{PDD_EVALUATIONS} (PDD fixed; form's {_num(request.get('steps'))} not used)" if pdd else _num(request.get("steps"))],
                ["Sampler", request.get("sampler")],
                ["Scheduler", f"PDD manual sigmas (video shift {_num(PDD_VIDEO_SHIFT)}, audio shift {_num(PDD_AUDIO_SHIFT)})" if pdd else request.get("scheduler")],
                ["CFG", _num(request.get("cfg")) if model == "minimax_h3" and (request.get("cfg") or 1) > 1
                 else (stages[0].get("cfg") if stages else None)],
                ["Negative prompt", request.get("negative_prompt") if model == "minimax_h3" and (request.get("cfg") or 1) > 1 else None],
                ["Video shift", _num(request.get("sigma_shift_video")) if request.get("sigma_shift_video") is not None else None],
                ["Audio shift", _num(request.get("sigma_shift_audio")) if request.get("sigma_shift_audio") is not None else None],
                ["Output quality", _OUTPUT_QUALITY_LABELS.get(request.get("output_quality")) if model == "minimax_h3" else None],
            ])

            checkpoint = request.get("checkpoint")
            if not checkpoint and template:
                node = "4" if model == "seedhunter" else ("14" if mode == "ref2v" else "5")
                default = template.get(node, {}).get("inputs", {}).get("unet_name")
                checkpoint_text = f"Workflow default ({default})" if default else "Workflow default"
            else:
                checkpoint_text = f"{_CHECKPOINT_LABELS.get(checkpoint, checkpoint)} ({checkpoint})" if checkpoint else "Workflow default"
            slots = request.get("loras") or []
            loras = [
                f"{label} @ {_num(slot.get('strength', 1.0))}"
                for slot, (_filename, label) in zip(slots, MINIMAX_H3_LORA_SLOTS) if slot and slot.get("enabled")
            ]
            section("Model", [["Checkpoint", checkpoint_text], ["LoRAs", "\n".join(loras) or "None"]])
            if model == "minimax_h3" and request.get("enable_upscale"):
                section("Latent upscale", _upscale_rows(request, pdd))

            if model == "minimax_h3":
                ref2v = mode == "ref2v"
                turbo_applies = not checkpoint or checkpoint == REF2V_TURBO_LORA_CHECKPOINT
                section("Speedup & Saves", [
                    ["Parasyte turbo LoRA", f"On @ {_num(request.get('parasyte_strength'))}" if request.get("parasyte_enabled", True) else "Off"] if not ref2v else None,
                    ["SLA attention", f"On, sparsity {_num(request.get('sla_sparsity'))}, dense first step {_on(request.get('sla_dense_step', True))}"
                     if request.get("sla_enabled", True) else "Off"] if not ref2v else None,
                    ["Reference turbo LoRA", ("Off" if not request.get("ref2v_turbo_enabled", True)
                                              else "PDD 8-step @ 1" if request.get("ref2v_turbo", "lightx") == "pdd"
                                              else f"LightX 8-step @ {_num(request.get('ref2v_strength'))}")
                     + ("" if turbo_applies or not request.get("ref2v_turbo_enabled", True) else " (skipped - only applies to the default Ref2V checkpoint)")] if ref2v else None,
                    ["Attention", {"flash": "Flash", "kitchen": "Comfy-Kitchen INT8", "pytorch": "PyTorch"}.get(
                        request.get("attention") or ("kitchen" if request.get("kitchen_attention_enabled", True) else "pytorch"))],
                    ["Spectrum forecasting", _on(request.get("spectrum_enabled"))] if not ref2v else None,
                    ["H3 block cache", _on(request.get("block_cache_enabled"))],
                    ["FFN chunking", _on(request.get("ffn_chunking_enabled"))],
                    ["Low-VRAM attention", f"on, {request.get('low_vram_attention_chunks', 4)} head groups"
                     if request.get("low_vram_attention_enabled") else "off"],
                    ["torch.compile", _on(request.get("torch_compile_enabled"))],
                    ["Ref video downscale", _on(request.get("ref_video_downscale", True))] if ref2v else None,
                    ["Reference sizing", request.get("reference_sizing")] if ref2v else None,
                ])
            else:
                section("SeedHunter", [
                    ["Candidates", _num(request.get("candidate_count"))],
                    ["Reference sizing", request.get("reference_sizing")],
                    ["Live preview", _on(request.get("enable_live_preview"))],
                ])

        continued = request.get("continue_from_job_id")
        section("Inputs", [
            ["First frame", _references([request.get("first_frame_id")], by_id)],
            ["Last frame", _references([request.get("last_frame_id")], by_id)],
            ["References", _references(request.get("reference_ids") or [], by_id)],
            ["Reference videos", _references(request.get("reference_video_ids") or [], by_id)],
            ["Reference audio", _references(request.get("reference_audio_ids") or [], by_id)],
            ["Continued from", f"Job {continued[:8]} ({_num(request.get('overlap_frames'))} overlap frames)" if continued else None],
        ])

    section("Run", [
        ["Queued", item.get("created_at")],
        ["Render time", _duration(item.get("started_at"), item.get("completed_at"))],
        ["Job ID", item["id"]],
        ["ComfyUI prompt ID", item.get("prompt_id")],
    ])
    # The raw request lets the panel's "load settings" button fill the form exactly.
    return {"prompt": item.get("prompt") or "", "sections": sections, "request": request if model not in FFMPEG_MODELS else None,
            "note": "CFG and LTX sampler/steps are fixed in the workflow template and are read from the current template file."}


def _size(width: int, height: int) -> str:
    return f"{width}×{height} ({width * height / 1_000_000:.2f} MP)"


def _upscale_rows(request: dict[str, Any], pdd: bool) -> list[Row]:
    """Every latent-upscale setting a job ran with, plus the sizes before and
    after it. Factor/Steps/Denoise/Seed keep their labels: the Load settings
    button reads them back (settingsFromDetails in app.js)."""
    rows: list[Row] = [["Factor", f"{_num(request.get('upscale_factor'))}x (rendered at the base size, then upscaled)"]]
    try:
        req = GenerationRequest.model_validate({**request, "prompt": request.get("prompt") or "-"})
    except Exception:  # an old/odd request: the plain rows are still worth showing
        req = None
    if req is not None:
        a = req.upscale_advanced
        render_w, render_h = (req.width, req.height) if req.width is not None else calculate_resolution(req.megapixels, req.aspect)
        target_w, target_h = h3_upscale_target(render_w, render_h, req.upscale_factor)
        latent_t = h3_latent_frames(h3_frame_count(req.duration))
        rows += [
            ["Before upscale", f"{_size(render_w, render_h)} · latent {render_w // 16}×{render_h // 16}×{latent_t}"],
            ["After upscale", f"{_size(target_w, target_h)} · latent {target_w // 16}×{target_h // 16}×{latent_t}"],
            ["Upscaler", f"{H3_LATENT_UPSCALER.removesuffix('.safetensors')} on {'GPU' if a.upscaler_device == 'cuda' else 'CPU'}, {a.upscaler_precision}"],
        ]
    rows += [
        ["Steps", f"{_num(request.get('upscale_steps'))} (last steps of PDD's {PDD_EVALUATIONS}-step grid)" if pdd else _num(request.get("upscale_steps"))],
        ["Denoise", "Not used (PDD grid)" if pdd else _num(request.get("upscale_denoise"))],
        ["Seed", _num(request.get("upscale_seed"))],
    ]
    if req is None:
        rows.append(["Chunking", "On (overlapping time chunks)" if request.get("upscale_chunking", True) else "Off (whole clip at once)"])
        return rows
    rows.append(["Second-pass sampler", f"{a.sampler or req.sampler} / {'PDD sigma tail' if pdd else a.scheduler or req.scheduler}"
                 + ("" if a.sampler or a.scheduler else " (same as render)")])
    segments = h3_upscale_segments(req)
    if req.upscale_chunking:
        chunk, overlap = h3_upscale_chunk_plan(req, target_w, target_h)
        length = f"{chunk}-frame{' (auto)' if not a.chunk_frames else ''} chunk length"
        rows.append(["Chunking", f"On: one chunk (the clip is within the {length})" if len(segments) == 1 else
                     f"On: {len(segments)} chunks, {length}, overlap {overlap}, anchor {_num(a.anchor_strength)}"])
    else:
        rows.append(["Chunking", "Off (whole clip at once)"])
    if a.tiling == "off":
        rows.append(["Spatial tiling", "Off (whole frame)"])
    else:
        mode = {"auto": f"auto, {a.token_budget:,}-token budget", "rows_cols": "rows × columns", "specific_size": "fixed tile size"}[a.tiling]
        try:
            grid_rows, grid_cols, tile_w, tile_h = h3_upscale_tile_grid(a, target_w, target_h, segments[0][1] - segments[0][0])
            grid = f"{grid_rows}×{grid_cols} tiles of {tile_w}×{tile_h}"
        except ValueError as exc:
            grid = f"unsolvable ({exc})"
        rows.append(["Spatial tiling", f"{grid} ({mode}); overlap {a.overlap_width}/{a.overlap_height}px, fade {a.fade_width}/{a.fade_height}px, "
                     f"{a.overlap_blend} ({a.overlap_mode} wins), joint steps {'on' if a.joint_steps else 'off'}"])
    rows.append(["VRAM", "\n".join([
        "Upscaler kept on the GPU between chunks" if a.keep_upscaler_resident else "Upscaler moved back to RAM after each chunk",
        "H3 kept loaded during the upscale" if a.keep_model_resident
        else "H3 unloaded once, all chunks upscaled first" if a.upscale_chunks_first
        else "H3 unloaded before each chunk's upscale",
        "H3 unloaded before VAE decode" if a.unload_model_after else "H3 kept loaded into VAE decode",
    ])])
    return rows


# ---------------------------------------------------------------- Stats tab --
# Where a generation's wall time went: the GPU-queue wait, every ComfyUI node
# that ran (from jobs.timings, see app.py's note_job_timing) and the app's own
# finishing work, grouped into categories for the panel's workflow map.

_CATEGORY_LABELS = {
    "wait": "Waiting", "load": "Load models & inputs", "encode": "Encode", "sample": "Sampling",
    "upscale": "Upscale & interpolate", "decode": "Decode", "output": "Save & finish", "other": "Other nodes",
}


def _category(class_type: str) -> str:
    name = class_type.lower()
    if "upscale" in name or "rife" in name or "vfi" in name or "interpolat" in name:
        return "upscale"
    if "sampler" in name:
        return "sample"
    if "decode" in name:
        return "decode"
    if "encode" in name or "imagetovideo" in name or "conditioning" in name:
        return "encode"
    if "load" in name:
        return "load"
    if "combine" in name or "save" in name or "preview" in name:
        return "output"
    return "other"


def _node_info(nodes: dict[str, list[str]], node_id: str) -> list[str]:
    """[class, title]; a node expanded at run time (a group / subgraph node) has an id like "87:3" or "87.3"."""
    return nodes.get(node_id) or nodes.get(re.split(r"[.:]", node_id)[0]) or ["", ""]


def _epoch(timestamp: str | None) -> float | None:
    try:
        return datetime.fromisoformat(timestamp).timestamp() if timestamp else None
    except ValueError:
        return None


def _step_seconds(ticks: list[list[Any]], total: float) -> list[float]:
    """Seconds per progress tick (sampler steps, upscale tiles/chunks): the
    first one includes everything before it, e.g. moving the model onto the GPU."""
    times = [float(tick[0]) for tick in ticks if tick and isinstance(tick[0], (int, float))]
    if len(times) < 2:
        return []
    return [round(end - start, 3) for start, end in zip([0.0, *times], times)] + ([round(total - times[-1], 3)] if total - times[-1] >= 0.5 else [])


def job_stats(item: dict[str, Any], live: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        timings = live or json.loads(item.get("timings") or "{}")
    except (TypeError, ValueError):
        timings = {}
    runs = [run for run in (timings.get("runs") or []) if run.get("segments")]
    if not runs:
        message = ("Stage timings are recorded for generations started after the Stats update."
                   if item.get("model") not in FFMPEG_MODELS else "Stitch and Post-Edit jobs run in ffmpeg, not ComfyUI - no stage timings.")
        return {"available": False, "message": message}
    origin = _epoch(item.get("created_at")) or runs[0]["submitted"]
    stages: list[dict[str, Any]] = []

    def add(label: str, category: str, start: float, end: float, **extra: Any) -> None:
        if end - start >= 0.05:
            stages.append({"label": label, "category": category, "start": round(start - origin, 3),
                           "seconds": round(end - start, 3), **extra})

    cursor = origin
    for number, run in enumerate(runs, 1):
        prefix = f"Run {number} · " if len(runs) > 1 else ""
        first = run["segments"][0]["start"]
        add(f"{prefix}Waiting for the GPU" if number == 1 else f"{prefix}Waiting before this run", "wait", cursor, run["submitted"],
            detail="Queued behind other GPU work (shared lock), plus any ComfyUI profile switch" if number == 1
            else "Time between runs, e.g. picking a SeedHunter candidate")
        add(f"{prefix}ComfyUI queue & setup", "wait", run["submitted"], first,
            detail=f"Validating the prompt; {len(run.get('cached') or [])} cached node(s) skipped")
        for segment in run["segments"]:
            class_type, title = _node_info(run.get("nodes") or {}, segment["node"])
            seconds = segment["end"] - segment["start"]
            add(prefix + (title or class_type or f"Node {segment['node']}"), _category(class_type), segment["start"], segment["end"],
                node=segment["node"], class_type=class_type, running=bool(segment.get("running")),
                steps=_step_seconds(segment.get("ticks") or [], seconds))
        cursor = run.get("finished") or run["segments"][-1]["end"]
    completed = _epoch(item.get("completed_at"))
    if completed and not live:
        add("Saving & finishing", "output", cursor, completed, detail="Collecting the output from ComfyUI, probing it and updating the job")
    end = (completed if not live else None) or max(stage["start"] + stage["seconds"] for stage in stages) + origin
    total = round(end - origin, 3)
    categories: dict[str, float] = {}
    for stage in stages:
        categories[stage["category"]] = categories.get(stage["category"], 0.0) + stage["seconds"]
    return {
        "available": True, "running": bool(live), "total": total,
        "stages": stages,
        "categories": [{"key": key, "label": _CATEGORY_LABELS[key], "seconds": round(seconds, 3)}
                       for key, seconds in sorted(categories.items(), key=lambda pair: -pair[1])],
        "nodes_run": sum(len(run["segments"]) for run in runs),
        "nodes_cached": sum(len(run.get("cached") or []) for run in runs),
    }
