"""What the Master Agent can do in each studio, over loopback (the studios trust
127.0.0.1; the gateway is the login boundary). New jobs start from the user's
own settings: the chosen Vid preset, the user's latest Krea request for that
model, the user's latest Trainer run for that model."""
from __future__ import annotations

import json
import os
import re
from typing import Any

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
            response = await client.request(method, url, json=body)
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


async def cancel(service: str, job_id: str) -> Any:
    return await _call("POST", f"{UP[service]}/api/jobs/{job_id}/cancel", {})


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


async def agentic_cancel(run_id: str) -> Any:
    return await _call("POST", f"{UP['Vid']}/api/enhancement/jobs/{run_id}/cancel", {})


async def agentic_stage_decision(session_id: str, run_id: str, action: str, notes: str = "") -> dict[str, Any]:
    return await _call("POST", f"{UP['Vid']}/api/agentic/sessions/{session_id}/stage-decision",
                       {"action": action, "run_id": run_id, "notes": notes[:4000], "clip_notes": {}})


async def agentic_job(session_id: str, run_id: str) -> dict[str, Any]:
    """An Agentic run seen as one job: its state, what it is doing as the stage, and the clips
    it kept (the finished Post-Edit/Stitch, else the final clips, else the staging previews)."""
    session = await _call("GET", f"{UP['Vid']}/api/agentic/sessions/{session_id}")
    run = next((r for r in session.get("runs") or [] if r.get("job_id") == run_id), None) or {}
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
    stage = ("Staging previews are waiting for your approval in Vid" if state == "awaiting_approval"
             else str(live.get("phase") or ""))
    planned = len(run.get("clips") or [])
    return {"id": run_id, "state": state, "stage": stage[:160], "renders": renders,
            "full_output": bool(run.get("finished_job_id")),
            "iterations": iterations,
            "log": live.get("log") or [], "log_revision": live.get("log_revision"),
            "progress": round(100 * len(renders) / planned) if planned and state in AGENTIC_ACTIVE else None,
            "error": str(live.get("error") or session.get("error") or "")}


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
    service = url.strip("/").split("/")[0]
    if service not in UP:
        raise StudioError(f"can't fetch {url}")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("GET", UP[service][: -len(service) - 1] + url) as response:
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
