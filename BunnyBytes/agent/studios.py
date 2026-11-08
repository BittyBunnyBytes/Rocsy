"""What the Master Agent can do in each studio, over loopback (the studios trust
127.0.0.1; the gateway is the login boundary). New jobs start from the user's
own settings: the chosen Vid preset, the user's latest Krea request for that
model, the user's latest Trainer run for that model."""
from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

UP = {
    "Vid": os.getenv("VID_UPSTREAM", "http://127.0.0.1:8000").rstrip("/") + "/Video",
    "Krea": os.getenv("KREA_UPSTREAM", "http://127.0.0.1:8300").rstrip("/") + "/Image",
    "Mus": os.getenv("MUS_UPSTREAM", "http://127.0.0.1:8200").rstrip("/") + "/Music",
    "Trainer": os.getenv("TRAINER_UPSTREAM", "http://127.0.0.1:8500").rstrip("/") + "/Trainer",
    "Queue": os.getenv("QUEUE_UPSTREAM", "http://127.0.0.1:8600").rstrip("/") + "/Queue",
    "Motion": os.getenv("MOTION_UPSTREAM", "http://127.0.0.1:8970").rstrip("/"),      # loopback only, no gateway route
    "Voice": os.getenv("VOICE_UPSTREAM", "http://127.0.0.1:8950").rstrip("/") + "/Voice",
}
# Gateway route ("Video") -> its upstream base URL, for output URLs like /Video/api/outputs/ID/stream.
ROUTES = {urlsplit(u).path.strip("/"): u for u in UP.values() if urlsplit(u).path.strip("/")}
SERVICES = ("Vid", "Krea", "Mus", "Trainer", "Motion", "Voice")
ACTIVE = {"bundling", "encoding", "waiting_for_gpu", "preparing", "queued", "rendering", "running", "finalizing", "cancelling", "uploading", "training", "queuing"}
DONE = {"complete", "completed", "failed", "cancelled", "canceled", "error", "interrupted"}
KREA_MODELS = ("krea2", "lustify", "qwen_image_21", "moss")
VIDEO_MODELS = ("minimax_h3", "ltx_2_5", "wan_2_2")
AGENTIC_VIDEO_MODELS = ("minimax_h3", "wan_2_2")
VOICE_MODELS = ("qwen3", "indextts2", "fish_s2", "fish_drama3")
MUSIC_MODELS = ("yue2", "minimax_music3")
# Shown with every job the Master Agent starts, so the user sees which model a service runs.
MODEL_LABELS = {"minimax_h3": "MiniMax H3", "ltx_2_5": "LTX 2.5", "wan_2_2": "Wan 2.2", "krea2": "Krea 2", "lustify": "Lustify",
                "qwen_image_21": "Qwen Image 2.1", "moss": "MOSS-Image", "qwen3": "Qwen3 TTS", "indextts2": "IndexTTS2",
                "fish_s2": "Fish S2", "fish_drama3": "Fish Drama 3", "yue2": "YuE 2", "minimax_music3": "MiniMax Music 3", "ltx25": "LTX 2.5"}
# The image studio's id stays "Krea" (routes, containers), but the agent and the chat call it Image:
# "Krea" next to the "krea2" model kept being read as one thing.
SERVICE_NAMES = {"Krea": "Image"}


class StudioError(Exception):
    pass


async def _call(method: str, url: str, body: Any = None, timeout: float = 30) -> Any:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # X-Rocsy-Agent: Vid keeps the chat model unloaded while the renders it queues for us run.
            response = await client.request(method, url, json=body, headers={"X-Rocsy-Agent": "master"})
    except httpx.HTTPError as exc:
        raise StudioError(f"{url.split('/')[3]} is not reachable ({type(exc).__name__})") from exc
    try:
        data = json.loads(response.content or b"null", strict=False)   # prompts can hold raw control chars
    except ValueError:
        data = None
    if response.status_code >= 400:
        detail = data.get("detail") or data.get("message") or data.get("error") if isinstance(data, dict) else None
        if isinstance(detail, list):
            detail = "; ".join(f"{'.'.join(map(str, d.get('loc', [])[1:]))}: {d.get('msg')}" for d in detail if isinstance(d, dict))
        raise StudioError(f"{response.status_code}: {detail or response.text[:300]}")
    return data


def _jobs(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = data.get("jobs")
    return data if isinstance(data, list) else []


async def jobs(service: str) -> list[dict[str, Any]]:
    if service == "Voice":
        return _jobs(await _call("GET", f"{UP['Voice']}/api/state"))
    return _jobs(await _call("GET", f"{UP[service]}/api/jobs"))


async def job(service: str, job_id: str) -> dict[str, Any]:
    return await _call("GET", f"{UP[service]}/api/jobs/{job_id}")


async def cancel(service: str, job_id: str, timeout: float = 30) -> Any:
    return await _call("POST", f"{UP[service]}/api/jobs/{job_id}/cancel", {}, timeout=timeout)


async def queue_cancel(entry_id: str, timeout: float = 15) -> Any:
    return await _call("POST", f"{UP['Queue']}/api/queue/{entry_id}/cancel", None, timeout=timeout)


async def queue() -> list[dict[str, Any]]:
    data = await _call("GET", f"{UP['Queue']}/api/queue")
    return data.get("entries", []) if isinstance(data, dict) else []


# ---------- options ----------

async def vid_presets(model: str = "minimax_h3") -> dict[str, Any]:
    data = await _call("GET", f"{UP['Vid']}/api/presets")
    return (data or {}).get(model, {}) if isinstance(data, dict) else {}


async def trainer_datasets() -> list[dict[str, Any]]:
    data = await _call("GET", f"{UP['Trainer']}/api/datasets")
    return [{"name": d["name"], "items": d["items"], "missing_captions": d.get("missing_captions", 0)} for d in data.get("datasets", [])]


async def options(allowed: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "Vid" in allowed:
        out["video_models"] = list(VIDEO_MODELS)
        try:
            out["video_presets"] = sorted(await vid_presets())
        except StudioError as exc:
            out["video_presets"] = f"unavailable ({exc})"
        out["video"] = {"modes": ["text", "reference (needs --ref attachments)"], "aspects": ["16:9", "9:16", "1:1", "4:3", "3:4"],
                        "duration": "5-15 s per clip (Wan 2.2: 1-10 s, no sound)",
                        "agentic": "Agentic mode (MiniMax H3 or Wan 2.2): any length / several clips, planned and reviewed by Vid",
                        "wan_2_2": "Wan 2.2: Text or image to video (--ref = first/last frame image), no sound, no Reference mode"}
    if "Krea" in allowed:
        out["image_models"] = list(KREA_MODELS)
    if "Mus" in allowed:
        out["music"] = {"models": list(MUSIC_MODELS), "duration": "10-300 s", "lyrics": "optional; [verse]/[chorus] tags work"}
    if "Voice" in allowed:
        try:
            state = await _call("GET", f"{UP['Voice']}/api/state")
            out["voice"] = {"models": state.get("engines", []), "voices": state.get("voices", [])}
        except StudioError as exc:
            out["voice"] = f"unavailable ({exc})"
    if "Motion" in allowed:
        out["motion"] = {"what": "code-based video: React (Remotion 4) projects you write in your workspace, rendered on the GPU",
                         "steps": "motion new NAME -> edit src/*.tsx -> motion check NAME -> motion still NAME --frame N -> motion render NAME"}
    if "Trainer" in allowed:
        try:
            out["datasets"] = await trainer_datasets()
        except StudioError as exc:
            out["datasets"] = f"unavailable ({exc})"
        out["train_models"] = ["minimax_h3", "krea2", "qwen_image_21", "ltx25"]
    return out


# ---------- submit ----------

def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


async def _vid_base(preset: str, default_preset: str, aspect: str | None, model: str = "minimax_h3") -> dict[str, Any]:
    """A saved Vid preset of that model (the Master Agent's default preset is an H3 one, so only H3 uses it)."""
    base: dict[str, Any] = {}
    name = preset or (default_preset if model == "minimax_h3" else "")
    if name:
        presets = await vid_presets(model)
        if name not in presets:
            raise StudioError(f'No {model} Vid preset called "{name}". Presets: {", ".join(sorted(presets)) or "none"}')
        base = dict(presets[name])
    if aspect:
        base.pop("width", None); base.pop("height", None)
    for key in ("reference_ids", "reference_video_ids", "reference_audio_ids", "first_frame_id", "last_frame_id", "continue_from_job_id"):
        base.pop(key, None)
    return base


async def video(prompt: str, duration: float | None, aspect: str | None, mode: str, preset: str, default_preset: str, model: str,
                reference_ids: list[str] | None = None) -> dict[str, Any]:
    if model not in VIDEO_MODELS:
        raise StudioError(f"Unknown video model {model}; use one of {', '.join(VIDEO_MODELS)}")
    if model == "ltx_2_5" and preset:
        raise StudioError("Vid presets here are for MiniMax H3 and Wan 2.2; omit --preset for LTX 2.5")
    base = await _vid_base(preset, default_preset, aspect, model)
    if model == "wan_2_2":
        return await _call("POST", f"{UP['Vid']}/api/generate", {**base, **_wan_frames(reference_ids), "model": model,
                           "prompt": prompt, **_drop_none({"duration": duration, "aspect": aspect})}, timeout=90)
    mode = mode or ("reference" if reference_ids else "text")
    mode = "ref2v" if mode == "reference" else mode
    body = {**base, "model": model, "mode": mode, "prompt": prompt,
            **_drop_none({"duration": duration, "aspect": aspect})}
    if reference_ids:
        if mode != "ref2v":
            raise StudioError("attachments go to H3 in Reference mode: use --mode reference (six-section prompt) or Agentic mode")
        body["reference_ids"] = reference_ids
    return await _call("POST", f"{UP['Vid']}/api/generate", body, timeout=90)


def _wan_frames(reference_ids: list[str] | None) -> dict[str, Any]:
    """Wan has no Reference mode: --ref images are its first (and last) frame, i.e. image to video."""
    ids = list(reference_ids or [])
    if len(ids) > 2:
        raise StudioError("Wan 2.2 takes at most 2 images (first and last frame); it has no Reference mode")
    if not ids:
        return {"mode": "text"}
    return {"mode": "fl2v", "first_frame_id": ids[0], "last_frame_id": ids[1] if len(ids) > 1 else None}


AGENTIC_ACTIVE = {"waiting_for_gpu", "running", "queuing", "rendering", "reviewing", "awaiting_approval"}


async def agentic_video(prompt: str, guide: str, duration: float | None, aspect: str | None, mode: str, preset: str,
                        default_preset: str, reference_ids: list[str], resume: str | None, model: str = "minimax_h3") -> dict[str, Any]:
    """Vid's Agentic mode: Video's own Hermes plans the clips (continuations, references,
    reviews, staging, finishing) and renders them. Returns {job_id, session_id, state}."""
    # Agentic plans the whole film from the request. Its generation.duration is only
    # the seed clip length, and Vid validates that field at 15 seconds or less.
    clip_max = 10 if model == "wan_2_2" else 15
    if duration is not None and duration > clip_max:
        guide = (guide + "\n" if guide else "") + (
            f"Target finished video length: about {duration:g} seconds. "
            + ("Plan enough 5 second clips (continuations) to cover it." if model == "wan_2_2" else
               "Plan enough 5-15 second clips to cover it.")
        )
        duration = None
    if model not in AGENTIC_VIDEO_MODELS:
        raise StudioError("Vid Agentic mode supports MiniMax H3 and Wan 2.2; use video for other models")
    base = await _vid_base(preset, default_preset, aspect, model)
    if model == "wan_2_2":
        # Wan: --ref images are the first (and last) keyframe the plan can start from; no reference pool.
        generation = {**base, **_wan_frames(reference_ids), "model": model, "prompt": prompt,
                      **_drop_none({"duration": duration, "aspect": aspect})}
        reference_ids = []
    else:
        generation = {**base, "model": model, "mode": "ref2v" if mode == "reference" else (mode or "text"),
                      "prompt": prompt, **_drop_none({"duration": duration, "aspect": aspect})}
        if generation["mode"] == "ref2v":
            generation["reference_ids"] = reference_ids
    body = {"generation": generation, "guide": guide or "", "available_reference_ids": reference_ids[:24],
            **({"resume_session_id": resume} if resume else {})}
    return await _call("POST", f"{UP['Vid']}/api/enhancement/agentic", body, timeout=90)


async def agentic_edit(session_id: str, clips: list[int], notes: str, scope: str) -> dict[str, Any]:
    """Edit a finished film by overall clip number (Vid's prompt ledger): scope clip | onward | future."""
    return await _call("POST", f"{UP['Vid']}/api/agentic/sessions/{session_id}/edit",
                       {"clips": clips, "notes": notes, "scope": scope}, timeout=60)


async def agentic_prompt_decision(session_id: str, action: str, notes: str, clip_notes: dict[str, str]) -> dict[str, Any]:
    """The user's verdict on the clip prompts Vid is waiting on: approve | reject (notes = reason) | flag (clip_notes)."""
    return await _call("POST", f"{UP['Vid']}/api/agentic/sessions/{session_id}/prompt-decision",
                       {"action": action, "notes": notes, "clip_notes": clip_notes}, timeout=30)


async def agentic_cancel(run_id: str, timeout: float = 30) -> Any:
    return await _call("POST", f"{UP['Vid']}/api/enhancement/jobs/{run_id}/cancel", {}, timeout=timeout)


async def agentic_pause(run_id: str) -> dict[str, Any]:
    """Pause a Vid Agentic run: its Hermes step or renders stop now and the GPU is free; the session is kept."""
    return await _call("POST", f"{UP['Vid']}/api/enhancement/jobs/{run_id}/pause", {}, timeout=120)


async def agentic_resume(run_id: str) -> dict[str, Any]:
    """Resume a paused Vid Agentic run in its own session; what the pause interrupted is generated again."""
    return await _call("POST", f"{UP['Vid']}/api/enhancement/jobs/{run_id}/resume", {}, timeout=60)


async def agentic_stage_decision(session_id: str, run_id: str, action: str, notes: str = "") -> dict[str, Any]:
    return await _call("POST", f"{UP['Vid']}/api/agentic/sessions/{session_id}/stage-decision",
                       {"action": action, "run_id": run_id, "notes": notes[:4000], "clip_notes": {}})


async def agentic_job(session_id: str, run_id: str) -> dict[str, Any]:
    """An Agentic run seen as one job: its state, what it is doing as the stage, and the clips
    it kept (the finished Post-Edit/Stitch, else the final clips, else the staging previews)."""
    session = await _call("GET", f"{UP['Vid']}/api/agentic/sessions/{session_id}")
    chain = _long_haul_chain(session, run_id)
    if chain:
        return await _long_haul_job(session, chain)
    run = next((r for r in session.get("runs") or [] if r.get("job_id") == run_id), None) or {}
    return await _agentic_run_view(session, run, run_id)


async def _agentic_run_view(session: dict[str, Any], run: dict[str, Any], run_id: str) -> dict[str, Any]:
    state = str(run.get("state") or session.get("state") or "running")
    ids: list[str] = []
    if run.get("finished_job_id"):
        ids = [run["finished_job_id"]]
    else:
        for key in ("final_clip_ids", "staging_clip_ids"):
            if clips := run.get(key) or {}:
                ids = [clips[k] for k in sorted(clips, key=lambda k: int(k) if str(k).isdigit() else 0)]
                break
    renders = []
    for render_id in ids:
        with_output = None
        try:
            with_output = await job("Vid", render_id)
        except StudioError:
            pass
        if with_output and with_output.get("state") == "complete":
            renders.append(with_output)
    iterations = []
    seen_iterations: set[str] = set()
    metadata = session.get("generation_meta") or {}
    selected_ids = set((run.get("final_clip_ids") or {}).values()) | set((run.get("staging_clip_ids") or {}).values())
    if run.get("finished_job_id"):
        selected_ids.add(run["finished_job_id"])
    for render_id in run.get("generation_ids") or []:
        if render_id in seen_iterations:
            continue
        seen_iterations.add(render_id)
        try:
            attempt_job = await job("Vid", render_id)
        except StudioError:
            continue
        if attempt_job.get("state") != "complete":
            continue
        meta = metadata.get(render_id) or {}
        for output in outputs("Vid", attempt_job):
            iterations.append({**output, "iteration": len(iterations) + 1, "phase": meta.get("phase") or "full",
                               "clip": meta.get("clip"), "attempt": meta.get("attempt", 1),
                               "selected": render_id in selected_ids})
    # Every attempt of every clip, final renders (retries too) and staging previews alike: per clip
    # the kept final first, so a finished card opens on the finished clips, not on the previews.
    iterations.sort(key=lambda item: (int(item["clip"]) if str(item.get("clip")).isdigit() else 1 << 30,
                                      item["phase"] != "full", not item["selected"], item["attempt"], item["iteration"]))
    live: dict[str, Any] = {}
    try:
        live = await _call("GET", f"{UP['Vid']}/api/enhancement/jobs/{run_id}")    # pruned after a while
    except StudioError:
        pass
    awaiting = run.get("awaiting") if state == "awaiting_approval" else None
    stage = ("Clip prompts are waiting for your approval" if awaiting == "prompts"
             else "Staging previews are waiting for your approval in Vid" if state == "awaiting_approval"
             else str(live.get("phase") or ""))
    planned = len(run.get("clips") or [])
    prompts = [{"id": clip.get("id"), "prompt": clip.get("prompt", ""), "duration": clip.get("duration"), "action": clip.get("action"),
                "mode": clip.get("mode"), "source": clip.get("source")} for clip in run.get("clips") or []] if awaiting == "prompts" else None
    return {"id": run_id, "state": state, "stage": stage[:160], "renders": renders, "overview": session.get("overview"),
            "awaiting": awaiting, "prompts": prompts, "prompt_offset": int(run.get("clip_offset") or 0),
            "full_output": bool(run.get("finished_job_id")),
            "iterations": iterations,
            "log": live.get("log") or [], "log_revision": live.get("log_revision"),
            "progress": round(100 * len(renders) / planned) if planned and state in AGENTIC_ACTIVE else None,
            "error": str(live.get("error") or session.get("error") or "")}


# Long-haul sessions (Vid writes a long request in batches, one run each, numbered across the session; see
# Video/backend/ledger.py): the Master Agent's one job follows every batch run from the one it started.
_finished_run_views: dict[str, dict[str, Any]] = {}
_CLIP_NUMBER = re.compile(r"\b((?:Staging|Full|staging|full) clip |Clip |clip )(\d+)\b")


def _long_haul_chain(session: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    runs = session.get("runs") or []
    if not session.get("long_haul") and not (session.get("ledger") or {}).get("outline"):
        return []
    start = next((i for i, r in enumerate(runs) if r.get("job_id") == run_id), None)
    return runs[start:] if start is not None else []


async def _long_haul_job(session: dict[str, Any], chain: list[dict[str, Any]]) -> dict[str, Any]:
    book = session.get("ledger") or {}
    outline = book.get("outline") or []
    clips = book.get("clips") or {}
    reached = lambda entry: entry.get("status") == "verified" or bool(entry.get("was_verified"))   # Video's ledger.reached
    done = 0
    while reached(clips.get(str(done + 1)) or {}):
        done += 1
    # The outline may start after clips the session already had (Video: ledger.outline_total).
    total = (int(book.get("outline_start") or 0) + len(outline)) if outline else 0
    views = []
    for run in chain:
        rid = run.get("job_id") or ""
        if rid in _finished_run_views and run.get("state") in ("complete", "completed", "failed", "cancelled"):
            views.append((run, _finished_run_views[rid]))
            continue
        view = await _agentic_run_view(session, run, rid)
        if run.get("state") in ("complete", "completed", "failed", "cancelled"):
            _finished_run_views[rid] = view
        views.append((run, view))
    latest_run, latest = views[-1]
    long_haul = session.get("long_haul") or {}
    state = latest["state"]
    more = bool(total) and done < total and not long_haul.get("stopped")
    if state in ("complete", "completed") and more:
        state = "running"                       # between batches: the next run is starting
    elif state in ("complete", "completed") and total and done < total and long_haul.get("stopped"):
        state = "failed"                        # a batch kept nothing: stopped rather than looping on it
    # The film's clips in story order, each its newest kept render (an edit re-renders a clip in place; runs append).
    by_id = {render.get("id"): render for _, view in views for render in view["renders"] if not view["full_output"]}
    ordered = [by_id[entry["render"]] for entry in sorted(clips.values(), key=lambda e: e["n"])
               if reached(entry) and entry.get("render") in by_id]
    renders = latest["renders"] if latest["full_output"] else (ordered or list(by_id.values()))
    iterations = []
    for run, view in views:
        offset = int(run.get("clip_offset") or 0)
        for item in view["iterations"]:
            iterations.append({**item, "clip": (int(item["clip"]) + offset) if str(item.get("clip")).isdigit() else item.get("clip")})
    # The live log of the batch running now, its clip numbers moved to overall numbers, after one line per clip
    # that earlier batches finished, so the chat's clip strip covers the whole film.
    offset = int(latest_run.get("clip_offset") or 0)
    if latest_run.get("mode") == "edit":      # an edit run's clips carry overall numbers; it covers only the edited ones
        covered = {int(n) for n in latest_run.get("edit_clips") or []}
    else:
        covered = set(range(offset + 1, offset + len(latest_run.get("clips") or []) + 1)) or set(range(offset + 1, done + 1 if done > offset else offset + 1))
    lines = [{"kind": "ok", "text": f"Validated {total or done} clip(s)"}]
    for n in range(1, done + 1):
        if n not in covered:
            lines += [{"kind": "tool", "text": f"Full clip {n}, attempt 1: queued earlier"}, {"kind": "ok", "text": f"Clip {n} finished: earlier run"}]
    for entry in latest["log"]:
        text = str(entry.get("text") or "")
        text = re.sub(r"\bValidated (\d+) clip\(s\)", r"Batch validated \1 clip(s)", text)
        text = _CLIP_NUMBER.sub(lambda m: m.group(1) + str(int(m.group(2)) + offset), text) if offset else text
        lines.append({**entry, "text": text})
    batches = max(1, -(-len(outline) // int(long_haul.get("batch") or 10))) if outline else len(chain)
    batch_no = sum(1 for run in chain if not run.get("mode")) or 1      # edit/rewrite runs aren't batches
    stage = (f"Batch {min(batch_no, batches)} of {batches} · {done}/{total} clips verified" if total else f"Run {len(chain)}")
    if latest["stage"]:
        stage += " · " + latest["stage"]
    elif state == "running" and latest["state"] in ("complete", "completed"):
        stage += " · starting the next batch"
    if long_haul.get("stopped"):
        stage += f" · stopped: {long_haul['stopped']}"
    return {"id": chain[0].get("job_id"), "state": state, "stage": stage[:160], "renders": renders, "overview": session.get("overview"),
            "awaiting": latest.get("awaiting"), "prompts": latest.get("prompts"), "prompt_offset": latest.get("prompt_offset", 0),
            "full_output": latest["full_output"] and not more, "iterations": iterations,
            "log": lines, "log_revision": f"{len(chain)}:{latest.get('log_revision')}:{done}",
            "progress": round(100 * done / total) if total and state in AGENTIC_ACTIVE | {"running"} else latest["progress"],
            "error": latest["error"] or (f"Stopped at {done}/{total} clips: {long_haul['stopped']}" if long_haul.get("stopped") and not more and done < total else "")}


# Vid's private Agentic pool: no 9-image cap, kept out of the user's Reference picker, and
# H3 deletes each one once no session, render or running job uses it. Uploading into the
# user's "references" pool filled it with old sessions' faces (2026-10-18).
VID_REF_ROLE = "agentic-references"


async def vid_reference_ids() -> set[str]:
    return {str(r.get("id")) for r in await _call("GET", f"{UP['Vid']}/api/references") or [] if isinstance(r, dict)}


async def upload(service: str, path: str, filename: str, mime: str, role: str = VID_REF_ROLE) -> dict[str, Any]:
    """Put a user's attachment into a studio as a reference. Vid -> its reference record plus
    "token" (<Picture N> / <Video N> / <Audio N>); Krea -> {"image": ComfyUI input name}."""
    url = {"Vid": f"{UP['Vid']}/api/references", "Krea": f"{UP['Krea']}/api/upload"}.get(service)
    if not url:
        raise StudioError(f"{service} doesn't take attachments")
    try:
        with open(path, "rb") as handle:
            async with httpx.AsyncClient(timeout=300) as client:
                response = await client.post(url, files={"file": (filename, handle, mime)},
                                             data={"role": role} if service == "Vid" else None)
    except (OSError, httpx.HTTPError) as exc:
        raise StudioError(f"couldn't send {filename} to {service} ({type(exc).__name__})") from exc
    try:
        result = json.loads(response.content or b"null", strict=False)
    except ValueError:
        result = None
    if response.status_code >= 400 or not isinstance(result, dict):
        detail = (result.get("detail") or result.get("message") or result.get("error")) if isinstance(result, dict) else None
        raise StudioError(f"{response.status_code}: {detail or response.text[:300]}")
    if service == "Vid":
        alias = str(result.get("alias") or "")
        found = re.fullmatch(r"(image|video|audio)(\d+)", alias)
        label = {"image": "Picture", "video": "Video", "audio": "Audio"}[found.group(1)] if found else ""
        result["token"] = f"<{label} {found.group(2)}>" if found else alias
    return result


async def image(prompt: str, model: str | None, width: int | None, height: int | None, count: int | None,
                references: list[dict[str, Any]] | None = None, enhance: bool = False) -> dict[str, Any]:
    model = model or "krea2"
    if model not in KREA_MODELS:
        raise StudioError(f"Unknown image model {model}; use one of {', '.join(KREA_MODELS)}")
    # The user's latest settings for this model, minus anything tied to that one image.
    base: dict[str, Any] = {}
    for item in await jobs("Krea"):
        request = item.get("request")
        if isinstance(request, dict) and request.get("model") == model:
            base = {k: v for k, v in request.items() if k not in ("prompt", "seed", "references", "use_references", "loras", "count")}
            break
    # The Master Agent has already written the prompt. Avoid a second LLM pass that can
    # fail on malformed JSON; --enhance remains available when explicitly requested.
    body = {**base, "model": model, "prompt": prompt, "prompt_enhancer": enhance,
            **_drop_none({"width": width, "height": height, "count": count})}
    if references:
        body.update(use_references=True, references=references)
    return await _call("POST", f"{UP['Krea']}/api/generate", body, timeout=60)


async def music(style: str, lyrics: str | None, duration: float | None, model: str) -> dict[str, Any]:
    if model not in MUSIC_MODELS:
        raise StudioError(f"Unknown music model {model}; use one of {', '.join(MUSIC_MODELS)}")
    body = {"model": model, "style": style, **_drop_none({"lyrics": lyrics, "duration": duration})}
    return await _call("POST", f"{UP['Mus']}/api/generate", body, timeout=60)


async def voice(text: str, voice_id: str, model: str, language: str = "English") -> dict[str, Any]:
    if model not in VOICE_MODELS:
        raise StudioError(f"Unknown voice model {model}; use one of {', '.join(VOICE_MODELS)}")
    return await _call("POST", f"{UP['Voice']}/api/generate",
                       {"text": text, "voice_id": voice_id, "engine": model, "language": language}, timeout=60)


async def train(dataset: str, name: str, trigger: str | None, model: str | None) -> dict[str, Any]:
    model = model or "minimax_h3"
    base: dict[str, Any] = {}
    for item in _jobs(await _call("GET", f"{UP['Trainer']}/api/jobs")):
        request = item.get("request")
        if isinstance(request, dict) and request.get("model") == model and item.get("state") == "complete":
            base = dict(request)
            break
    if not base:
        presets = await _call("GET", f"{UP['Trainer']}/api/presets")
        builtin = (presets.get("builtin") or {}).get(model) or {}
        if builtin:
            base = dict(next(iter(builtin.values())))
    body = {**base, "model": model, "dataset": dataset, "name": re.sub(r"[^A-Za-z0-9_-]+", "_", name)[:64] or dataset,
            **_drop_none({"trigger": trigger})}
    return await _call("POST", f"{UP['Trainer']}/api/jobs", body, timeout=120)


async def fetch_output(url: str, path: str) -> str:
    """Save a job's output (a gateway URL like /Image/api/outputs/ID/stream?index=0) to path;
    returns its content type."""
    # Output URLs carry the gateway route (/Video, /Image, /Music, /Voice, ...), not the service id
    # (Vid, Krea, Mus): match on the route; old /Vid/ /Krea/ /Mus/ URLs map onto today's routes.
    route, _, rest = url.lstrip("/").partition("/")
    base = ROUTES.get(route) or UP.get(route)
    if not base or not urlsplit(base).path.strip("/"):
        raise StudioError(f"can't fetch {url}")
    service = route
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("GET", base + "/" + rest) as response:
                if response.status_code >= 400:
                    raise StudioError(f"{service} output {response.status_code}")
                with open(path, "wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
                return response.headers.get("content-type", "").split(";")[0]
    except (OSError, httpx.HTTPError) as exc:
        raise StudioError(f"couldn't fetch the {service} output ({type(exc).__name__})") from exc


async def stitch(job_ids: list[str], music_id: str | None, music_volume: float, clip_volume: float,
                 transition: str) -> dict[str, Any]:
    """Vid's Stitch (GPU encode): the clips in order, optionally with music over them."""
    body = {"job_ids": job_ids, "transition": transition, "match_fps": True,
            **({"music_reference_id": music_id, "music_volume": music_volume, "clip_volume": clip_volume} if music_id else {})}
    return await _call("POST", f"{UP['Vid']}/api/stitch", body, timeout=60)


async def postedit_transcribe(job_id: str) -> dict[str, Any]:
    """Start Vid's GPU Whisper (ComfyUI BunnyTranscribe, never the CPU) on a finished video's audio."""
    return await _call("POST", f"{UP['Vid']}/api/postedit/transcribe/{job_id}", {}, timeout=60)


async def postedit_transcript(job_id: str, vocals: bool = True) -> dict[str, Any]:
    return await _call("GET", f"{UP['Vid']}/api/postedit/transcribe/{job_id}?vocals={'true' if vocals else 'false'}", timeout=30)


async def tts(text: str, voice: str, provider: str = "edge", rate: int = 0, pitch: int = 0) -> dict[str, Any]:
    """Vid's text-to-speech (Edge / TikTok voices, online): a Post-Edit voice clip with caption timings."""
    return await _call("POST", f"{UP['Vid']}/api/tts", {"provider": provider, "voice": voice, "text": text, "rate": rate,
                                                       "pitch": pitch, "role": "postedit-voice"}, timeout=120)


async def postedit(body: dict[str, Any]) -> dict[str, Any]:
    """A Vid Post-Edit render (ffmpeg); with a cached transcript Whisper captions start at once."""
    return await _call("POST", f"{UP['Vid']}/api/postedit", body, timeout=300)


async def motion(path: str, body: dict[str, Any] | None = None, method: str = "POST", timeout: float = 600) -> Any:
    """Rocsy Motion (Remotion renderer): projects, compile checks, stills, render jobs."""
    return await _call(method, f"{UP['Motion']}{path}", body, timeout=timeout)


async def motion_download(job_id: str, path: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=600)) as client:
            async with client.stream("GET", f"{UP['Motion']}/api/outputs/{job_id}") as response:
                if response.status_code >= 400:
                    raise StudioError(f"Motion output {response.status_code}")
                with open(path, "wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
    except (OSError, httpx.HTTPError) as exc:
        raise StudioError(f"couldn't fetch the Motion render ({type(exc).__name__})") from exc


async def vid_import(path: str, label: str) -> dict[str, Any]:
    """A finished video (a Motion render) as a complete Vid job, for Post-Edit / Stitch / the gallery."""
    try:
        with open(path, "rb") as handle:
            async with httpx.AsyncClient(timeout=600) as client:
                response = await client.post(f"{UP['Vid']}/api/jobs/import", files={"file": ("motion.mp4", handle, "video/mp4")},
                                             data={"label": label[:500], "model": "motion"})
    except (OSError, httpx.HTTPError) as exc:
        raise StudioError(f"couldn't import the render into Vid ({type(exc).__name__})") from exc
    data = json.loads(response.content or b"null", strict=False) if response.content else None
    if response.status_code >= 400 or not isinstance(data, dict):
        detail = (data or {}).get("detail") if isinstance(data, dict) else None
        raise StudioError(f"Vid import {response.status_code}: {detail or response.text[:200]}")
    return data


# ---------- results ----------

def outputs(service: str, item: dict[str, Any]) -> list[dict[str, str]]:
    """Viewable results of a finished job, as gateway URLs."""
    if service == "Vid" and item.get("output_url"):
        return [{"kind": "video", "url": item["output_url"], "thumb": item.get("poster_url") or "",
                 "open": f"/Video/?job={item.get('id')}"}]
    if service == "Krea":
        return [{"kind": "image", "url": image["url"], "thumb": image["url"], "open": "/Image/"} for image in item.get("images") or []]
    if service == "Mus" and item.get("output_url"):
        return [{"kind": "audio", "url": item["output_url"], "thumb": "", "open": "/Music/"}]
    if service == "Voice" and item.get("state") == "complete":
        return [{"kind": "audio", "url": f"/Voice/api/audio/{item['id']}", "thumb": "", "open": "/Voice/"}]
    if service == "Trainer":
        return [{"kind": "file", "url": "", "thumb": "", "name": out.get("name", ""), "open": "/Trainer/"} for out in item.get("outputs") or []]
    return []


def agentic_outputs(item: dict[str, Any]) -> list[dict[str, str]]:
    return [out for render in item.get("renders") or [] for out in outputs("Vid", render)]


def agentic_iterations(item: dict[str, Any]) -> list[dict[str, Any]]:
    return list(item.get("iterations") or [])


def progress(item: dict[str, Any]) -> float | None:
    value = item.get("progress")
    if isinstance(value, dict):
        value = value.get("overall")
        return round(value * 100) if isinstance(value, (int, float)) else None
    return round(value) if isinstance(value, (int, float)) else None


async def status(allowed: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for service in SERVICES:
        if service not in allowed:
            continue
        try:
            items = await jobs(service)
        except StudioError as exc:
            out[SERVICE_NAMES.get(service, service)] = f"offline ({exc})"
            continue
        active = [i for i in items if i.get("state") in ACTIVE]
        out[SERVICE_NAMES.get(service, service)] = [{"id": i.get("id"), "state": i.get("state"), "progress": progress(i),
                         "what": str(i.get("prompt") or i.get("style") or i.get("name") or "")[:80]} for i in active] or "idle"
    try:
        out["gpu_queue"] = [{"app": e.get("app"), "state": e.get("state"), "label": str(e.get("label") or "")[:80]} for e in await queue()] or "empty"
    except StudioError as exc:
        out["gpu_queue"] = f"offline ({exc})"
    return out


# ---------------- Agents page: every studio's Agentic sessions ----------------
# Video runs its own Agentic sessions; Image/Music/Voice run theirs in the shared framework,
# which the Master Agent reaches directly with the internal secret (as the studios' proxies do).
FRAMEWORK = os.getenv("BUNNY_FRAMEWORK_URL", "http://127.0.0.1:8700").rstrip("/")
FW_SECRET = os.getenv("QUEUE_INTERNAL_SECRET", "")
AGENT_STUDIOS = {"video": "Video", "image": "Image", "music": "Music", "voice": "Voice"}
FW_ACTIVE = {"planning", "reviewing_plan", "queued", "rendering", "reviewing_output", "retrying"}


async def _fw(method: str, path: str, body: Any = None, timeout: float = 30) -> Any:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, FRAMEWORK + path, json=body, headers={"X-Queue-Internal-Secret": FW_SECRET})
    except httpx.HTTPError as exc:
        raise StudioError(f"The Agentic framework is not reachable ({type(exc).__name__})") from exc
    try:
        data = json.loads(response.content or b"null", strict=False)
    except ValueError:
        data = None
    if response.status_code >= 400:
        detail = data.get("detail") if isinstance(data, dict) else None
        raise StudioError(str(detail or response.text[:300]))
    return data


def _busy(studio: str, state: str) -> bool:
    return state in (AGENTIC_ACTIVE if studio == "video" else FW_ACTIVE | {"awaiting_approval", "awaiting_feedback"})


async def agent_sessions(studio: str) -> list[dict[str, Any]]:
    """Light rows: {studio, id, title, state, busy, created, updated, runs, ...}."""
    if studio == "video":
        rows = (await _call("GET", f"{UP['Vid']}/api/agentic/sessions", timeout=15) or {}).get("sessions") or []
        return [{**r, "studio": "video", "busy": _busy("video", r.get("state", ""))} for r in rows]
    rows = await _fw("GET", f"/v1/agentic/{studio}/sessions?limit=0", timeout=15) or []
    return [{"studio": studio, "id": r["id"], "title": " ".join(str(r.get("request") or "").split())[:140] or "Agentic session",
             "state": r.get("state") or "", "stage": str(r.get("stage") or "")[:200], "busy": _busy(studio, r.get("state") or ""),
             "created": float(r.get("created") or 0), "updated": float(r.get("updated") or r.get("created") or 0),
             "runs": 1, "parent": r.get("parent_session_id") or "",
             "model": str((r.get("settings") or {}).get("model") or "")} for r in rows if isinstance(r, dict) and r.get("id")]


def _log_rows(entries: list[Any]) -> list[dict[str, Any]]:
    out = []
    for e in entries or []:
        if isinstance(e, dict):
            out.append({"t": e.get("t") or e.get("at"), "kind": str(e.get("kind") or "system"), "text": str(e.get("text") or "")[:20000]})
    return out


async def agent_session(studio: str, sid: str) -> dict[str, Any]:
    """One session with its log (Video: every run whose log Video still keeps, oldest first)."""
    if studio == "video":
        session = await _call("GET", f"{UP['Vid']}/api/agentic/sessions/{sid}", timeout=20)
        runs = session.get("runs") or []
        log: list[dict[str, Any]] = []
        run_state = ""
        for n, run in enumerate(runs, 1):
            rid = run.get("job_id")
            if not rid:
                continue
            log.append({"kind": "run", "text": f"Run {n}" + (f" · {run['mode']}" if run.get("mode") else ""), "t": run.get("created_at")})
            try:
                live = await _call("GET", f"{UP['Vid']}/api/enhancement/jobs/{rid}", timeout=15)
            except StudioError:
                log.append({"kind": "system", "text": "Video no longer keeps this run's log (it keeps the latest 20 runs).", "t": run.get("created_at")})
                if run.get("input_prompt"):
                    log.append({"kind": "user", "text": str(run["input_prompt"])[:20000], "t": run.get("created_at")})
                continue
            log += _log_rows(live.get("log"))
            if n == len(runs):
                run_state = str(live.get("state") or "")
        last = runs[-1] if runs else {}
        state = run_state or str(last.get("state") or session.get("state") or "")
        title = " ".join(str(session.get("master_prompt") or "").split())[:300]
        return {"studio": "video", "id": sid, "title": title or "Agentic session", "state": state,
                "busy": _busy("video", state), "paused": state == "paused", "run_id": last.get("job_id") or sid,
                "stage": "", "log": log[-1500:], "open": "/Video/",
                "can_pause": _busy("video", state), "can_resume": state == "paused",
                "help": "Plain text steers a running run, or starts a new run of this session once it has finished. "
                        "Running runs also take /status, /plan, /clip N <text>, /accept, /exclude, /help."}
    row = await _fw("GET", f"/v1/agentic/{studio}/sessions/{sid}", timeout=20)
    state = str(row.get("state") or "")
    return {"studio": studio, "id": sid, "title": " ".join(str(row.get("request") or "").split())[:300] or "Agentic session",
            "state": state, "busy": _busy(studio, state), "paused": state == "paused", "stage": str(row.get("stage") or "")[:300],
            "log": _log_rows(row.get("log"))[-1500:], "open": f"/{AGENT_STUDIOS[studio]}/",
            "can_pause": state in FW_ACTIVE, "can_resume": state == "paused",
            "help": "Plain text steers the next Hermes step, or continues a finished session in a new one. "
                    "Also /status, /plan, /approve, /revise <notes>, /retry, /keep, /help."}


async def agent_message(studio: str, sid: str, text: str) -> dict[str, Any]:
    """Talk to a session: steer it while it runs, or continue it once it has finished.
    Returns {session_id} (a framework continuation is a new session) and an optional reply."""
    if studio == "video":
        session = await _call("GET", f"{UP['Vid']}/api/agentic/sessions/{sid}", timeout=20)
        runs = session.get("runs") or []
        rid = (runs[-1] if runs else {}).get("job_id")
        state = ""
        if rid:
            try:
                state = str((await _call("GET", f"{UP['Vid']}/api/enhancement/jobs/{rid}", timeout=15)).get("state") or "")
            except StudioError:
                state = ""
        if rid and (state in AGENTIC_ACTIVE or state == "paused" or text.startswith("/")):
            if not state:
                return {"session_id": sid, "reply": "Video no longer has this run loaded; type plain text to start a new run of the session."}
            r = await _call("POST", f"{UP['Vid']}/api/enhancement/jobs/{rid}/steer", {"text": text}, timeout=120)
            return {"session_id": sid, "reply": r.get("reply", "")}
        r = await _call("POST", f"{UP['Vid']}/api/agentic/sessions/{sid}/continue", {"text": text}, timeout=90)
        return {"session_id": r.get("session_id") or sid, "reply": "Started a new run of this session."}
    r = await _fw("POST", f"/v1/agentic/{studio}/sessions/{sid}/steer", {"text": text}, timeout=60)
    return {"session_id": r.get("session_id") or sid, "reply": ""}


async def agent_control(studio: str, sid: str, action: str) -> dict[str, Any]:
    """pause | resume | stop."""
    if studio == "video":
        session = await _call("GET", f"{UP['Vid']}/api/agentic/sessions/{sid}", timeout=20)
        rid = ((session.get("runs") or [{}])[-1]).get("job_id") or sid
        path = {"pause": "pause", "resume": "resume", "stop": "cancel"}[action]
        return await _call("POST", f"{UP['Vid']}/api/enhancement/jobs/{rid}/{path}", {}, timeout=120)
    path = {"pause": "pause", "resume": "resume", "stop": "cancel"}[action]
    return await _fw("POST", f"/v1/agentic/{studio}/sessions/{sid}/{path}", {}, timeout=60)


async def agent_delete(studio: str, sid: str) -> str:
    """deleted | busy | gone | error: <why>. Video deletes the session with its renders (its own trash button)."""
    try:
        if studio == "video":
            await _call("DELETE", f"{UP['Vid']}/api/agentic/sessions/{sid}", timeout=120)
        else:
            await _fw("DELETE", f"/v1/agentic/{studio}/sessions/{sid}", timeout=60)
        return "deleted"
    except StudioError as exc:
        text = str(exc)
        if text.startswith("409") or "busy" in text.lower() or "active" in text.lower() or "end this" in text.lower() or "running" in text.lower():
            return "busy"
        if text.startswith("404") or "not found" in text.lower():
            return "gone"
        return "error: " + text[:200]
