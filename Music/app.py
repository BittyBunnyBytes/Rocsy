from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import time
import httpx
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import uvicorn
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from fastapi import FastAPI, Form, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend.comfy import ComfyService, ServiceError, output_from_history
from backend.config import BASE_DIR, settings
from backend.events import EventBroker
from backend import loras as lora_gallery
from backend.job_stats import job_stats
from backend import pack
from backend.generation import GenerationError, MINIMAX_MODEL_FILES, SCORE_PREVIEW, build_workflow
from backend.bunnylock import BunnyLock, PendingStore
from backend.models import GenerationRequest
from backend.store import UNFINISHED_JOB_STATES, Store, utc_now
from backend.workflow_models import (MISSING_CODE, WorkflowModelRequest, WorkflowModels, missing_files, missing_message,
                                     slots_from_graph)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("music")

# Extra mime types mimetypes doesn't always know about out of the box.
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/wav", ".wav")
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/ogg", ".opus")

settings.prepare()
store = Store(settings.data_dir / "music.sqlite3")
comfy = ComfyService(settings)
lock = BunnyLock(app="Mus", url=settings.lock_url, secret=settings.queue_internal_secret)
events = EventBroker()
client_id = f"music-web-{uuid.uuid4().hex}"

submission_tasks: set[asyncio.Task[None]] = set()
# Jobs enqueued with bunny-lock but not yet given the go-ahead to submit to
# ComfyUI - populated in generate(), consumed (and popped) in the internal
# proceed endpoint once the coordinator calls back.
pending_submissions = PendingStore(settings.data_dir / "pending-submissions")

PREFIX = settings.mount_prefix  # e.g. "" standalone, "/Music" behind the BunnyBytes gateway


def _workflow_model_seeds() -> dict[str, dict[str, Any]]:
    """Advanced workflow card: every model file each Music template loads
    (no checkpoint menu here, so the main model is a slot too)."""
    seeds = {}
    for key, label, path in (("yue2", "YuE2", settings.workflow_path),
                             ("minimax_music3", "MiniMax Music 3", settings.minimax_workflow_path)):
        try:
            slots = slots_from_graph(json.loads(path.read_text()), frozenset({"lora_name"}))
        except (OSError, ValueError):
            slots = []
        seeds[key] = {"label": label, "slots": slots}
    return seeds


workflow_models = WorkflowModels(settings.data_dir / "workflow-models.json", settings.comfy_url, _workflow_model_seeds())


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str, extra: dict[str, Any] | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}


def p(path: str) -> str:
    """Prefix a route/asset path with the configured mount prefix."""
    return f"{PREFIX}{path}"


# ---- Lyric timing: a hidden GPU job after each song with words; Settings can turn it off ----
SETTINGS_PATH = settings.data_dir / "settings.json"

# ---- Song covers: Agentic writes an image prompt from the song, the Image studio renders it (Settings) ----
COVER_DIR = settings.data_dir / "covers"
IMAGE_URL = os.getenv("MUS_IMAGE_URL", "http://127.0.0.1:8300/Image").rstrip("/")
# The Image studio's models (Krea app.py MODEL_LABELS); Lustify is a Krea 2 checkpoint, so it uses Krea 2's guide.
COVER_IMAGE_MODELS = {"krea2": "Krea 2", "lustify": "Lustify (Krea 2)", "qwen_image_21": "Qwen Image 2.1",
                      "anima": "Anima", "moss": "MOSS-Image"}
COVER_GUIDE = {"lustify": "krea2"}
# Quick cover sizes per image model: the Image studio's own size presets (Krea static/js/app.js SIZE_PRESETS).
_KREA_COVER_SIZES = [
    ("1024x1024", "1:1 · 1024×1024 (1 MP)"), ("1536x1536", "1:1 · 1536×1536 (max)"),
    ("1152x896", "4:3 · 1152×896"), ("896x1152", "3:4 · 896×1152"),
    ("1280x1024", "5:4 · 1280×1024"), ("1024x1280", "4:5 · 1024×1280"),
    ("1216x832", "3:2 · 1216×832"), ("832x1216", "2:3 · 832×1216"),
    ("1344x768", "16:9 · 1344×768"), ("768x1344", "9:16 · 768×1344"),
    ("1536x640", "21:9 · 1536×640"),
]
COVER_SIZES = {
    "krea2": _KREA_COVER_SIZES, "lustify": _KREA_COVER_SIZES, "anima": _KREA_COVER_SIZES, "moss": _KREA_COVER_SIZES,
    "qwen_image_21": [
        ("1024x1024", "1:1 · 1024×1024 (1 MP)"), ("1536x1536", "1:1 · 1536×1536"), ("2048x2048", "1:1 · 2048×2048 (native 2K)"),
        ("2304x1728", "4:3 · 2304×1728"), ("1728x2304", "3:4 · 1728×2304"),
        ("2496x1664", "3:2 · 2496×1664"), ("1664x2496", "2:3 · 1664×2496"),
        ("2752x1536", "16:9 · 2752×1536"), ("1536x2752", "9:16 · 1536×2752"),
    ],
}
# The Image studio's limits (Krea backend/models.py): sides from 512, rounded to 32; at most 1536 a side,
# except Qwen-Image 2.1: up to 2752 a side and 2752×1536 pixels.
COVER_MIN_SIDE, COVER_MAX_SIDE, QWEN_MAX_SIDE, QWEN_MAX_PIXELS = 512, 1536, 2752, 2752 * 1536


def parse_cover_size(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d{3,5})\s*[x×]\s*(\d{3,5})\s*", value or "")
    return (int(match[1]), int(match[2])) if match else None


def cover_size_problem(image_model: str, width: int, height: int) -> str | None:
    """Why the Image studio would refuse this size for the model, or None."""
    rounded = [max(32, round(side / 32) * 32) for side in (width, height)]
    if min(width, height) < COVER_MIN_SIDE:
        return f"Cover sizes start at {COVER_MIN_SIDE} px a side."
    if image_model == "qwen_image_21":
        if max(width, height) > QWEN_MAX_SIDE:
            return f"Qwen Image 2.1 goes up to {QWEN_MAX_SIDE} px a side."
        if rounded[0] * rounded[1] > QWEN_MAX_PIXELS:
            return "Qwen Image 2.1 goes up to 4.2 megapixels (e.g. 2048×2048 or 2752×1536)."
    elif max(width, height) > COVER_MAX_SIDE:
        return f"{COVER_IMAGE_MODELS.get(image_model, image_model)} goes up to {COVER_MAX_SIDE} px a side."
    return None
DEFAULT_COVER_IMAGE_MODEL = "krea2"
DEFAULT_COVER_PROMPT = (
    "You are an album art director. From the song described below (its music direction and lyrics), write one prompt "
    "for its square cover art. Capture the song's genre, mood and imagery in a single striking composition: "
    "subject, setting, colour palette, lighting and art style. No text, letters, logos or watermarks, "
    "and no real people.")
COVER_READS = ("both", "direction", "lyrics")
cover_tasks: dict[str, asyncio.Task[None]] = {}
cover_stages: dict[str, str] = {}  # job id -> what a running cover is doing, for the card
cover_errors: dict[str, str] = {}  # job id -> why its last cover failed (until the next try or a restart)


def music_settings() -> dict[str, Any]:
    saved: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        loaded = json.loads(SETTINGS_PATH.read_text())
        saved = loaded if isinstance(loaded, dict) else {}
    model = saved.get("whisper_model")
    image_model = saved.get("cover_image_model")
    return {"lyric_timing": saved.get("lyric_timing", True) is not False,
            "whisper_model": model if model in pack.WHISPER_MODELS else pack.DEFAULT_WHISPER_MODEL,
            "song_cover": saved.get("song_cover") is True,
            "cover_system_prompt": str(saved.get("cover_system_prompt") or "").strip() or DEFAULT_COVER_PROMPT,
            "cover_agent": str(saved.get("cover_agent") or ""),
            # What the cover's prompt writer reads: both | direction | lyrics.
            "cover_reads": saved.get("cover_reads") if saved.get("cover_reads") in COVER_READS else "both",
            "cover_preset": str(saved.get("cover_preset") or ""),
            "cover_size": str(saved.get("cover_size") or ""),
            "cover_image_model": image_model if image_model in COVER_IMAGE_MODELS else DEFAULT_COVER_IMAGE_MODEL}


def lyric_timing_enabled() -> bool:
    return music_settings()["lyric_timing"]


def song_lyrics(item: dict[str, Any]) -> str:
    """The words a song sings: as written, or as recognised for a cover left without any."""
    return item.get("lyrics") or json.loads(item.get("result_json") or "{}").get("lyrics") or ""


def align_source(item: dict[str, Any]) -> str | None:
    """The song an alignment job is timing; None for every other job."""
    request = json.loads(item.get("request_json") or "{}")
    return request.get("source_job") if request.get("task") == "align" else None


def is_voice_preview(item: dict[str, Any]) -> bool:
    return json.loads(item.get("request_json") or "{}").get("task") == pack.PREVIEW_TASK


def public_jobs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The library's jobs. Voice previews stay out (the voice dialog follows its own by id)."""
    items = [item for item in items if not is_voice_preview(item)]
    timing = {}
    for item in reversed(items):  # oldest first, so a song's newest alignment wins
        if source := align_source(item):
            timing[source] = "running" if item["state"] in UNFINISHED_JOB_STATES else "failed" if item["state"] == "failed" else None
    jobs = [public_job(item) for item in items]
    for job in jobs:
        job["lyric_timing"] = timing.get(job["id"]) or job["lyric_timing"]
    return jobs


def public_job(item: dict[str, Any]) -> dict[str, Any]:
    output = bool(item.get("output_path"))
    request = json.loads(item.get("request_json") or "{}")
    state = item["state"]
    error = item.get("error")
    if state == "complete" and not output and request.get("task") not in pack.TEXT_TASKS:
        # Older versions could label an unfinished prompt complete simply
        # because an early ComfyUI history record lacked status fields.
        state = "failed"
        error = error or "No audio file was saved for this earlier generation. Please generate the track again."
    return {
        "id": item["id"],
        "prompt_id": item.get("prompt_id"),
        "style": item["style"],
        "model": request.get("model", "yue2"),
        "task": request.get("task"),   # YuE2 Studio only
        "result": json.loads(item.get("result_json") or "{}"),
        # The submitted settings: Retry loads them back into the form.
        "request": request,
        "lyrics": item["lyrics"],
        "sung_lyrics": song_lyrics(item),
        "seed": int(item["seed"]),
        # Seeds reach 2^64; a browser number is exact only to 2^53, so the page shows and reuses this one.
        "seed_text": str(int(item["seed"])),
        "duration": item["duration"],
        "state": state,
        "created_at": item["created_at"],
        "started_at": item.get("started_at"),
        "completed_at": item.get("completed_at"),
        "error": error,
        "progress": int(item.get("progress") or 0),
        "lyric_timing": "ready" if item.get("lyric_timings") else None,
        "cover_url": p(f"/api/jobs/{item['id']}/cover?v={int(cover.stat().st_mtime)}") if (cover := COVER_DIR / f"{item['id']}.png").is_file() else None,
        "cover_state": "running" if item["id"] in cover_tasks else "failed" if item["id"] in cover_errors else None,
        "cover_error": cover_errors.get(item["id"]),
        "cover_stage": cover_stages.get(item["id"]) if item["id"] in cover_tasks else None,
        "output_url": p(f"/api/outputs/{item['id']}/stream") if output else None,
        "output_files": [item.get("output_path")] if item.get("output_path") else [],
        "download_url": p(f"/api/outputs/{item['id']}/download") if output else None,
    }


async def publish_jobs() -> None:
    jobs = store.jobs()
    await events.publish("jobs", public_jobs(jobs))
    for item in jobs:   # the queue page's progress bar
        if lock.held(item.get("lock_token")):
            lock.progress(item["lock_token"], public_job(item))


async def finalize_job_lock(job_id: str, token: str | None) -> None:
    """Hand the GPU back. Called on every terminal state (complete/failed/
    cancelled) and on unhandled errors; the release is shielded in the client."""
    await lock.release(token)


# Per-node wall time of each ComfyUI prompt, for the info dialog's Stats tab
# (backend/job_stats.py). ComfyUI announces each node with an "executing"
# event, so a node runs from its event to the next one. Open nodes and their
# progress ticks live here; closed segments are written to jobs.timings.
job_timings: dict[str, dict[str, Any]] = {}  # job id -> {"timings": {"runs": [...]}, "open": segment | None}
MAX_TIMING_TICKS = 1000


def start_job_timing(job_id: str, prompt_id: str, workflow: dict[str, Any], label: str) -> None:
    """The job's ComfyUI prompt was accepted: start recording its nodes."""
    with contextlib.suppress(Exception):  # timing is best-effort
        entry = job_timings.get(job_id)
        if not entry:
            stored = json.loads((store.job(job_id) or {}).get("timings") or "{}")
            entry = job_timings[job_id] = {"timings": {"runs": list(stored.get("runs") or []) if isinstance(stored, dict) else []},
                                           "open": None}
        nodes = {str(node_id): [str(node.get("class_type") or ""), str((node.get("_meta") or {}).get("title") or "")]
                 for node_id, node in workflow.items() if isinstance(node, dict)}
        entry["timings"]["runs"].append({"prompt_id": prompt_id, "label": label, "submitted": time.time(),
                                         "nodes": nodes, "cached": [], "segments": []})
        store.update_job(job_id, timings=json.dumps(entry["timings"]))


def note_job_timing(job_id: str, event_type: str, data: dict[str, Any]) -> None:
    entry = job_timings.get(job_id)
    if not entry:
        return  # submitted before an app restart
    prompt_id = str(data.get("prompt_id"))
    run = next((item for item in entry["timings"]["runs"] if item["prompt_id"] == prompt_id), None)
    if not run:
        return
    now = time.time()
    current = entry["open"]
    if current and current.get("prompt_id") != prompt_id:
        current = None  # a stale node from a prompt that never reported its end
    if event_type == "progress":
        if current and len(current["ticks"]) < MAX_TIMING_TICKS:
            current["ticks"].append([round(now - current["start"], 3), data.get("value"), data.get("max")])
        return
    if event_type == "execution_start":
        run["started"] = now
    elif event_type == "execution_cached":
        run["cached"] = [str(node) for node in data.get("nodes") or []]
    elif event_type in {"executing", "execution_success", "execution_error", "execution_interrupted"}:
        if current:
            run["segments"].append({key: value for key, value in current.items() if key != "prompt_id"} | {"end": now})
        entry["open"] = None
        if event_type == "executing" and data.get("node") is not None:
            entry["open"] = {"prompt_id": prompt_id, "node": str(data["node"]), "start": now, "ticks": []}
        else:
            run.setdefault("finished", now)
            if all(item.get("finished") for item in entry["timings"]["runs"]):
                job_timings.pop(job_id, None)  # recreated from the database if another prompt follows
    else:
        return
    with contextlib.suppress(Exception):
        store.update_job(job_id, timings=json.dumps(entry["timings"]))


def live_job_timings(job_id: str) -> dict[str, Any] | None:
    """The stored timings plus the node running right now, while the job is in ComfyUI."""
    entry = job_timings.get(job_id)
    if not entry:
        return None
    timings = json.loads(json.dumps(entry["timings"]))
    if current := entry["open"]:
        run = next((item for item in timings["runs"] if item["prompt_id"] == current["prompt_id"]), None)
        if run:
            run["segments"].append({key: value for key, value in current.items() if key != "prompt_id"}
                                   | {"end": time.time(), "running": True})
    return timings


async def submit_job(job_id: str, workflow: dict[str, Any], lock_token: str | None, targets: list[str] | None = None) -> None:
    try:
        prompt_id = await comfy.submit(workflow, client_id, targets)
        start_job_timing(job_id, prompt_id, workflow, "Render")
        job = store.job(job_id)
        if job and job["state"] == "cancelling":
            store.update_job(job_id, prompt_id=prompt_id, started_at=utc_now())
            cancelled = await comfy.cancel(prompt_id)
            if cancelled:
                store.update_job(job_id, state="cancelled", completed_at=utc_now())
                await finalize_job_lock(job_id, lock_token)
                log.info("Cancelled generation job %s during submission", job_id[:8])
            else:
                store.update_job(
                    job_id, state="failed", completed_at=utc_now(),
                    error="ComfyUI accepted the prompt but no longer reported it as cancellable.",
                )
                await finalize_job_lock(job_id, lock_token)
        else:
            store.update_job(job_id, prompt_id=prompt_id, state="queued", started_at=utc_now(), progress=0)
            log.info("Submitted generation job %s as prompt %s", job_id[:8], prompt_id[:8])
    except Exception as exc:
        store.update_job(job_id, state="failed", error=str(exc), completed_at=utc_now())
        await finalize_job_lock(job_id, lock_token)
        log.exception("Generation submission failed for %s", job_id[:8])
    await publish_jobs()


async def monitor_jobs() -> None:
    comfy_nudge: asyncio.Task | None = None
    while True:
        try:
            comfy_state = await comfy.status()
            if not comfy_state["online"] and (comfy_nudge is None or comfy_nudge.done()):
                # One self-heal nudge at a time, kept referenced (asyncio holds tasks only weakly).
                comfy_nudge = asyncio.create_task(comfy.ensure_supervisor_started())
            running_ids = set(comfy_state.pop("running_ids", []))
            await events.publish("status", {"comfyui": comfy_state})
            changed = False
            for job in store.unfinished_jobs():
                prompt_id = job.get("prompt_id")
                if not prompt_id:
                    continue
                if prompt_id in running_ids and job["state"] != "rendering":
                    store.update_job(job["id"], state="rendering")
                    changed = True
                try:
                    history = await comfy.history(prompt_id)
                except ServiceError:
                    history = None
                if not history:
                    continue
                status = history.get("status", {})
                status_text = str(status.get("status_str", "")).lower()
                failed = status_text in {"error", "failed"} or status.get("completed") is False
                if failed:
                    messages = status.get("messages") or []
                    error = "ComfyUI reported a generation failure."
                    if messages:
                        error = str(messages[-1])[:1000]
                    store.update_job(job["id"], state="failed", error=error, completed_at=utc_now())
                else:
                    # /history may contain an entry while a queued prompt is
                    # still starting. Treat only an explicit terminal success
                    # as complete; a missing status used to turn these early
                    # entries into false 1-2 second successes.
                    completed = status.get("completed") is True or status_text in {"success", "completed"}
                    if not completed:
                        continue
                    request_saved = json.loads(job.get("request_json") or "{}")
                    task = request_saved.get("task")
                    if task:
                        result = pack.result_from_history(task, history)
                        take = output_from_history(history, settings.output_dir, pack.TAKE)
                        if take is not None:   # a cover's (or an edited cover's) take: what Edit a track edits
                            result["take"] = str(take.relative_to(settings.output_dir.resolve()))
                        store.update_job(job["id"], result_json=json.dumps(result))
                    elif request_saved.get("model", "yue2") == "yue2":
                        # The score it was sung from (Remix reuses it): captured, or the one a remix was given.
                        text = ((history.get("outputs") or {}).get(SCORE_PREVIEW) or {}).get("text") or []
                        score = str(text[0]) if text else str(request_saved.get("score_abc") or "")
                        if score.strip():
                            store.update_job(job["id"], result_json=json.dumps({"score": score}))
                    if task in pack.TEXT_TASKS:
                        if result:
                            store.update_job(job["id"], state="complete", progress=100, completed_at=utc_now())
                            if task == "transcribe" and request_saved.get("direction"):
                                start_direction(job["id"])
                        else:
                            store.update_job(job["id"], state="failed", completed_at=utc_now(),
                                             error="ComfyUI finished but the task returned no text.")
                        await finalize_job_lock(job["id"], job.get("lock_token"))
                        if task == "align" and result:
                            store.update_job(align_source(job) or "", lyric_timings=result.get("timings"))
                            store.delete_job(job["id"])
                        changed = True
                        continue
                    output = output_from_history(history, settings.output_dir)
                    if output is None:
                        store.update_job(
                            job["id"], state="failed", completed_at=utc_now(),
                            error="ComfyUI reported success but Music could not find the generated audio file in the mounted output folder.",
                        )
                        log.error("Generation job %s finished without a readable audio output", job["id"][:8])
                        await finalize_job_lock(job["id"], job.get("lock_token"))
                        changed = True
                        continue
                    relative_output = str(output.relative_to(settings.output_dir.resolve())) if output else None
                    store.update_job(job["id"], state="complete", progress=100, output_path=relative_output, completed_at=utc_now())
                    log.info("Generation job %s completed with output", job["id"][:8])
                await finalize_job_lock(job["id"], job.get("lock_token"))
                changed = True
                finished = store.job(job["id"])
                if (finished and finished["state"] == "complete" and finished.get("output_path") and music_settings()["song_cover"]
                        and not is_voice_preview(finished)):
                    start_song_cover(finished["id"])
                if finished and finished["state"] == "complete" and lyric_timing_enabled() and pack.has_sung_lines(song_lyrics(finished)):
                    try:
                        await queue_alignment(finished)
                    except (APIError, ServiceError) as exc:
                        log.warning("Couldn't queue lyric timing for %s: %s", job["id"][:8], exc)
            if changed:
                await publish_jobs()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background job monitor failed")
        await asyncio.sleep(settings.monitor_interval)


async def monitor_comfy_progress() -> None:
    """Persist ComfyUI websocket progress and forward it to connected browsers."""
    while True:
        try:
            async for event in comfy.events(client_id):
                event_type = event.get("type")
                data = event.get("data") or {}
                prompt_id = data.get("prompt_id")
                if not prompt_id:
                    continue
                job = store.job_by_prompt(str(prompt_id))
                if job:
                    note_job_timing(job["id"], str(event_type), data)
                if not job or job["state"] in {"cancelling", "cancelled", "complete", "failed"}:
                    continue
                if event_type == "executing" and data.get("node") is None and job["state"] in {"queued", "rendering"}:
                    # ComfyUI finished the prompt: hand the GPU on now (saving the
                    # output needs none); the monitor's later release is a no-op.
                    await lock.release(job.get("lock_token"))
                    continue
                if event_type == "progress":
                    maximum = float(data.get("max") or 0)
                    value = float(data.get("value") or 0)
                    reported = max(0, min(99, round(value / maximum * 100))) if maximum > 0 else 0
                    percentage = max(int(job.get("progress") or 0), reported)
                    store.update_job(job["id"], state="rendering", progress=percentage)
                    await publish_jobs()
                elif event_type in {"execution_error", "execution_interrupted"}:
                    store.update_job(
                        job["id"], state="failed", error="ComfyUI interrupted the generation.",
                        completed_at=utc_now(),
                    )
                    await finalize_job_lock(job["id"], job.get("lock_token"))
                    await publish_jobs()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("ComfyUI progress stream unavailable: %s", exc)
            await asyncio.sleep(settings.monitor_interval)


def _saved_waiting_jobs() -> set[str]:
    """Queued jobs that keep their place across a restart: saved and still waiting."""
    return {job_id for job_id in pending_submissions if (store.job(job_id) or {}).get("state") == "waiting_for_gpu"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    kept = _saved_waiting_jobs()
    pending_submissions.retain(kept)
    removed = store.delete_unfinished_jobs(kept)
    lock.start()
    requeue = asyncio.create_task(lock.requeue_lost(
        pending_submissions, lambda job_id, entry: store.update_job(job_id, queue_entry_id=entry["entry_id"])))
    log.info("Music starting; workflow=%s; comfy_url=%s; removed_unfinished_jobs=%d", settings.workflow_path.name, settings.comfy_url, removed)
    monitor = asyncio.create_task(monitor_jobs())
    progress_monitor = asyncio.create_task(monitor_comfy_progress())
    resume_directions()
    try:
        yield
    finally:
        monitor.cancel()
        progress_monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor
        with contextlib.suppress(asyncio.CancelledError):
            await progress_monitor
        for task in (*submission_tasks, *cover_tasks.values(), *direction_tasks.values()):
            task.cancel()
        if submission_tasks:
            await asyncio.gather(*tuple(submission_tasks), return_exceptions=True)
        requeue.cancel()
        removed = store.delete_unfinished_jobs(_saved_waiting_jobs())
        if removed:
            log.info("Removed %d unfinished generation job(s) during shutdown", removed)
        await lock.stop()
        store.close()
        log.info("Music stopped")


# docs_url/redoc_url/openapi_url disabled: defense-in-depth against the
# route/parameter map being exposed if ever reached directly (only the
# gateway is internet-facing in this stack).
app = FastAPI(title="Music", version="1.2.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount(p("/static"), StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _asset_version() -> str:
    digest = hashlib.sha256()
    for path in sorted((BASE_DIR / "static").rglob("*")):
        if path.is_file() and path.suffix in {".js", ".css"}:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


ASSET_VERSION = _asset_version()


@app.exception_handler(APIError)
async def api_error_handler(_: Request, exc: APIError) -> JSONResponse:
    return JSONResponse({**exc.extra, "error": exc.code, "message": exc.message}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    return JSONResponse({"error": "invalid_request", "message": first.get("msg", "The request is invalid.")}, status_code=422)


@app.get(p("/"))
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"app_version": ASSET_VERSION, "app_prefix": PREFIX})


@app.get(p("/api/health"))
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(p("/api/workflow-models"))
async def get_workflow_models(model: str | None = None) -> dict[str, Any]:
    """Advanced workflow card: per-workflow model-file overrides."""
    return await workflow_models.state(model)


@app.put(p("/api/workflow-models"))
async def put_workflow_models(request: WorkflowModelRequest) -> dict[str, Any]:
    try:
        if request.reset:
            workflow_models.reset(request.workflow)
        else:
            await workflow_models.set(request.workflow, request.slot or "", request.file)
    except ValueError as exc:
        raise APIError(400, "invalid_workflow_model", str(exc)) from exc
    return await workflow_models.state()


@app.get(p("/api/models"))
async def models() -> dict[str, Any]:
    missing = [name for name in MINIMAX_MODEL_FILES if not (settings.models_dir / name).is_file()]
    return {"yue2": {"ready": True}, "minimax_music3": {"ready": not missing, "missing": missing}}


@app.get(p("/api/loras"))
async def list_loras() -> dict[str, Any]:
    """The LoRA gallery: every YuE2 / MiniMax Music 3 LoRA under models/loras (Trainer-made first)."""
    items = await asyncio.to_thread(lora_gallery.scan, settings.models_dir)
    for item in items:
        item["preview_url"] = p(f"/api/loras/preview/{item['preview']}") if item["preview"] else None
    return {"loras": items, "folder": "models/loras/Music"}


@app.get(p("/api/loras/preview/{rel:path}"))
async def lora_preview(rel: str):
    path = lora_gallery.preview_path(settings.models_dir, rel)
    if not path:
        raise APIError(404, "not_found", "No preview song for that LoRA.")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "audio/flac",
                        headers={"Cache-Control": "private, max-age=3600"})


def _remix_source(remix_of: str) -> tuple[dict[str, Any], dict[str, Any]]:
    source = store.job(remix_of)
    if not source or source["state"] != "complete":
        raise APIError(404, "job_not_found", "The song to remix isn't available any more.")
    return source, json.loads(source.get("request_json") or "{}")


def remix_request(request: GenerationRequest) -> GenerationRequest:
    """A Remix is rebuilt here from the song's own saved request and exact seed (a browser rounds seeds over 2^53);
    only the new words come from the page. The score: the song's saved one, else written again from the words it
    was composed from."""
    source, original = _remix_source(request.remix_of)
    score = str(json.loads(source.get("result_json") or "{}").get("score") or "")
    fields = {**original, "seed": int(source["seed"]), "lyrics": request.lyrics, "remix_of": request.remix_of,
              "score_abc": score, "score_lyrics": "" if score else str(original.get("score_lyrics") or original.get("lyrics") or "")}
    try:
        return GenerationRequest.model_validate(fields)
    except ValueError as exc:
        raise APIError(422, "invalid_request", f"This song's saved settings can't be remixed: {exc}") from exc


@app.post(p("/api/generate"), status_code=202)
async def generate(request: GenerationRequest) -> dict[str, Any]:
    if request.remix_of:
        request = remix_request(request)
    comfy_state = await comfy.status()
    if not comfy_state["online"]:
        raise APIError(
            503, "comfy_unavailable",
            f"ComfyUI is offline at {settings.comfy_url}. Start ComfyUI or correct MUS_COMFY_URL, then try again.",
        )
    try:
        workflow, seed = build_workflow(settings, request)
    except GenerationError as exc:
        raise APIError(422 if exc.code in ("workflow_incompatible", "lora_invalid") else 503, exc.code, str(exc)) from exc
    workflow_models.apply(request.model, workflow)
    # Checked against ComfyUI before queueing; the Music UI offers to link or download what's missing.
    missing = await missing_files(settings.comfy_url, [workflow])
    if missing["files"] or missing["nodes"]:
        raise APIError(409, MISSING_CODE, missing_message(missing), {"missing": missing, "studio": "Music"})

    return await enqueue_job(workflow, seed, request.style, request.lyrics, request.duration, request.model_dump())


async def enqueue_job(workflow: dict[str, Any], seed: int, style: str, lyrics: str, duration: float,
                      request_data: dict[str, Any], targets: list[str] | None = None,
                      queue_model: str | None = None) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    label = f"Music: {style[:60]}"
    # model + a duration bucket file the job's measured VRAM peak (concurrent jobs, Queue page).
    bucket = next((b for b in (60, 120, 240, 480) if duration <= b), 960)
    queue_key = {"queue_model": queue_model or str(request_data.get("model") or "") or None, "queue_config": f"up to {bucket}s"}
    entry = await lock.enqueue(job_id, label, uses_llm=False, model=queue_key["queue_model"],
                               config=queue_key["queue_config"])
    if entry is None:
        raise APIError(503, "queue_unavailable", "Could not reach the GPU queue coordinator. Try again shortly.")
    item = {
        "id": job_id, "style": style, "lyrics": lyrics, "seed": seed, "duration": duration,
        "state": "waiting_for_gpu", "created_at": utc_now(), "request": request_data,
        "queue_entry_id": entry["entry_id"],
    }
    store.create_job(item)
    pending_submissions[job_id] = {"workflow": workflow, "label": label, "targets": targets, **queue_key}
    await publish_jobs()
    return public_job(store.job(job_id) or item)


async def queue_alignment(song: dict[str, Any]) -> dict[str, Any]:
    for item in store.jobs():
        if align_source(item) == song["id"]:
            if item["state"] in UNFINISHED_JOB_STATES:
                return public_job(item)
            store.delete_job(item["id"])
    workflow, targets = pack.align_workflow(song["output_path"], song_lyrics(song), music_settings()["whisper_model"])
    return await enqueue_job(workflow, 0, f"Lyric timing: {song['style'][:60]}", "", 0,
                             {"task": "align", "source_job": song["id"]}, targets)


# The label a pack job is listed under (pack.PackRequest.label) wraps its direction: strip it back off.
PACK_LABEL_PREFIXES = {"cover": "Cover: ", "plan": "Score: "}


def song_direction(song: dict[str, Any]) -> str:
    """The Music direction (style line) the song was made from. The jobs table's `style` is the card
    label, which for packs is "Cover: ..." or a file name, so the request (or a Write pack's result) comes first."""
    request = json.loads(song.get("request_json") or "{}")
    result = json.loads(song.get("result_json") or "{}")
    for value in (request.get("style"), result.get("style")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    task = request.get("task")
    label = str(song.get("style") or "").strip()
    if task in PACK_LABEL_PREFIXES:
        return label.removeprefix(PACK_LABEL_PREFIXES[task]).strip()
    return label if task in (None, "song") else ""   # vocals/edit labels name a file, not a direction


async def image_presets(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    """The Image studio's saved presets, {model: {name: settings}}; {} when it can't be reached."""
    try:
        response = await client.get(f"{IMAGE_URL}/api/presets", timeout=15)
        response.raise_for_status()
        presets = response.json()
        return presets if isinstance(presets, dict) else {}
    except (httpx.HTTPError, ValueError):
        return {}


def cover_request(image_model: str, prompt: str, preset: dict[str, Any] | None,
                  size: tuple[int, int] | None = None) -> dict[str, Any]:
    """The Image studio request for a cover: the preset's settings when there is one (one image, no
    reference images), else the current settings (the image model at 1024 x 1024). `size` (Settings →
    Song covers → Size) overrides either, and stops a preset's prompt enhancer from picking the size."""
    if not preset:
        width, height = size or (1024, 1024)
        return {"model": image_model, "prompt": prompt, "width": width, "height": height}
    request = {**preset, "model": image_model, "prompt": prompt, "count": 1, "use_references": False,
               "match_reference_size": False}
    if size:
        request.update(width=size[0], height=size[1], enhancer_picks_size=False)
    # LoRA slots are positional and presets keep switched-off slots the studio no longer has (it takes 8):
    # drop the trailing switched-off ones.
    loras = list(request.get("loras") or [])
    while loras and not (isinstance(loras[-1], dict) and loras[-1].get("enabled")):
        loras.pop()
    request["loras"] = loras
    return request


def cover_brief(song: dict[str, Any], reads: str = "both") -> str:
    """What the cover's prompt writer gets (Settings -> Song covers -> Cover art reads): the song's Music direction
    (with what the user asked for and the song's facts), its lyrics, or both. Lyrics only on an instrumental falls back
    to the direction: there would be nothing to draw from."""
    request = json.loads(song.get("request_json") or "{}")
    result = json.loads(song.get("result_json") or "{}")
    words = song_lyrics(song).strip() if pack.has_sung_lines(song_lyrics(song)) else ""
    if reads == "lyrics" and not words:
        reads = "direction"
    parts: list[str] = []
    if reads in {"both", "direction"}:
        direction = song_direction(song)
        parts.append("MUSIC DIRECTION (the genre, mood, instruments and voice the song was made with):\n"
                     + (direction[:2500] if direction else "not given"))
        notes = [str(request.get(key) or "").strip() for key in ("idea", "instructions")]
        if any(notes):
            parts.append("WHAT THE USER ASKED FOR:\n" + "\n".join(n for n in notes if n)[:1200])
        if facts := str(result.get("facts") or "").strip():
            parts.append("SONG FACTS:\n" + facts[:800])
    if reads in {"both", "lyrics"}:
        parts.append("LYRICS:\n" + words[:9000] if words else "LYRICS: none (instrumental)")
    if reads == "lyrics":
        parts.append("Draw the cover from the lyrics alone: their imagery, story and mood.")
    elif reads == "direction":
        parts.append("Draw the cover from the music alone (genre, mood, instruments, voice); the lyrics aren't given.")
    return "\n\n".join(parts)[:11900]   # the framework takes up to 12000


async def make_song_cover(job_id: str) -> None:
    """Agentic writes the prompt (its own GPU turn), then a hidden Image studio job renders it in the queue."""
    cover_errors.pop(job_id, None)
    cover_stages[job_id] = "Writing the prompt"
    await publish_jobs()
    headers = {"X-Queue-Internal-Secret": settings.queue_internal_secret}
    image_job = None
    try:
        song = store.job(job_id)
        if not song:
            return
        chosen = music_settings()
        image_model = chosen["cover_image_model"]
        base = os.getenv("BUNNY_FRAMEWORK_URL", "http://127.0.0.1:8700").rstrip("/")
        async with httpx.AsyncClient(timeout=4 * 3600) as client:
            response = await client.post(f"{base}/v1/agentic/image/write-prompt", headers=headers, json={
                "brief": cover_brief(song, chosen["cover_reads"]), "settings": {"model": COVER_GUIDE.get(image_model, image_model)},
                "purpose": "song cover", "system": chosen["cover_system_prompt"], "model": chosen["cover_agent"]})
            if response.status_code != 200:
                raise RuntimeError(f"Agentic couldn't write the cover prompt: {response.text[:300]}")
            prompt = response.json()["prompt"][:4000]
            preset = None
            if name := chosen["cover_preset"]:
                preset = (await image_presets(client)).get(image_model, {}).get(name)
                if not isinstance(preset, dict):
                    preset = None
                    log.info("Song cover for %s: no %s preset named %r; using the current settings", job_id[:8], image_model, name)
            size = parse_cover_size(chosen["cover_size"])
            if size and cover_size_problem(image_model, *size):
                size = None     # saved for another model; Settings checks it against the current one
            response = await client.post(f"{IMAGE_URL}/api/internal/generate", headers=headers, json={
                "request": cover_request(image_model, prompt, preset, size)})
            if response.status_code == 422 and preset:
                # Saved by an older Image studio (a setting it no longer takes): render with the current settings.
                log.warning("Song cover for %s: the Image studio refused preset %r (%s); using the current settings",
                            job_id[:8], name, response.text[:200])
                response = await client.post(f"{IMAGE_URL}/api/internal/generate", headers=headers, json={
                    "request": cover_request(image_model, prompt, None, size)})
            if response.status_code >= 400:
                raise RuntimeError(f"The Image studio refused the cover: {response.text[:300]}")
            image_job = response.json()["id"]
            cover_stages[job_id] = "Waiting for the GPU"
            await publish_jobs()
            log.info("Song cover for %s: Image job %s", job_id[:8], image_job[:8])
            while True:
                await asyncio.sleep(3)
                response = await client.get(f"{IMAGE_URL}/api/jobs/{image_job}")
                if response.status_code == 404:
                    raise RuntimeError("The Image studio lost the cover render.")
                state = response.json()
                stage = "Waiting for the GPU" if state.get("state") == "waiting_for_gpu" else "Rendering"
                if cover_stages.get(job_id) != stage:
                    cover_stages[job_id] = stage
                    await publish_jobs()
                if state.get("state") == "complete":
                    break
                if state.get("state") in {"failed", "cancelled"}:
                    raise RuntimeError(str(state.get("error") or f"The cover render {state.get('state')}.")[:300])
            response = await client.get(f"{IMAGE_URL}/api/outputs/{image_job}/download")
            response.raise_for_status()
        COVER_DIR.mkdir(parents=True, exist_ok=True)
        temporary = COVER_DIR / f"{job_id}.tmp"
        temporary.write_bytes(response.content)
        temporary.replace(COVER_DIR / f"{job_id}.png")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        cover_errors[job_id] = str(exc)[:400] or type(exc).__name__
        log.warning("Song cover for %s failed: %s", job_id[:8], exc)
    finally:
        cover_tasks.pop(job_id, None)
        cover_stages.pop(job_id, None)
        if image_job:
            with contextlib.suppress(httpx.HTTPError):
                async with httpx.AsyncClient(timeout=30) as client:
                    if job_id not in cover_errors and not (COVER_DIR / f"{job_id}.png").is_file():   # the song was deleted mid-render
                        await client.post(f"{IMAGE_URL}/api/jobs/{image_job}/cancel")
                    await client.delete(f"{IMAGE_URL}/api/internal/jobs/{image_job}", headers=headers)
        with contextlib.suppress(Exception):
            await publish_jobs()


def start_song_cover(job_id: str) -> None:
    if job_id not in cover_tasks:
        cover_errors.pop(job_id, None)
        cover_stages[job_id] = "Writing the prompt"
        cover_tasks[job_id] = asyncio.create_task(make_song_cover(job_id))


@app.post(p("/api/jobs/{job_id}/cover"), status_code=202)
async def request_song_cover(job_id: str) -> dict[str, Any]:
    song = store.job(job_id)
    if not song or song["state"] != "complete" or not song.get("output_path"):
        raise APIError(404, "job_not_found", "That song isn't finished.")
    start_song_cover(job_id)
    return public_job(store.job(job_id) or song)


@app.get(p("/api/jobs/{job_id}/cover"))
async def get_song_cover(job_id: str) -> FileResponse:
    path = COVER_DIR / f"{job_id}.png"
    if not re.fullmatch(r"[a-f0-9]{32}", job_id) or not path.is_file():
        raise APIError(404, "not_found", "This song has no cover.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=31536000"})


@app.get(p("/api/cover-presets"))
async def get_cover_presets() -> dict[str, list[str]]:
    """The Image studio presets a song cover can use, {image model: [names]}."""
    async with httpx.AsyncClient() as client:
        presets = await image_presets(client)
    return {model: sorted(names) for model, names in presets.items() if model in COVER_IMAGE_MODELS and isinstance(names, dict)}


@app.get(p("/api/settings"))
async def get_settings() -> dict[str, Any]:
    return {**music_settings(), "whisper_models": [{"id": key, "label": label} for key, label in pack.WHISPER_MODELS.items()],
            "cover_image_models": [{"id": key, "label": label} for key, label in COVER_IMAGE_MODELS.items()],
            "cover_sizes": {model: [{"id": key, "label": label} for key, label in sizes] for model, sizes in COVER_SIZES.items()},
            "default_cover_system_prompt": DEFAULT_COVER_PROMPT}


class MusicSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lyric_timing: bool
    whisper_model: str = pack.DEFAULT_WHISPER_MODEL
    song_cover: bool = False
    cover_system_prompt: str = Field(default="", max_length=20000)
    cover_agent: str = Field(default="", max_length=200)
    cover_reads: str = Field(default="both", pattern=r"^(both|direction|lyrics)$")
    cover_image_model: str = DEFAULT_COVER_IMAGE_MODEL
    # An Image studio preset (saved per image model) for the cover render; "" = the current settings.
    cover_preset: str = Field(default="", max_length=200)
    # "WxH" overrides the render size (over a preset's too); "" = the preset's size, or 1024 x 1024.
    cover_size: str = Field(default="", max_length=20)

    @model_validator(mode="after")
    def size_fits_model(self) -> "MusicSettings":
        if not self.cover_size.strip():
            self.cover_size = ""
            return self
        size = parse_cover_size(self.cover_size)
        if size is None:
            raise ValueError("Write the cover size as width × height, e.g. 1024x1024.")
        if problem := cover_size_problem(self.cover_image_model, *size):
            raise ValueError(problem)
        self.cover_size = f"{size[0]}x{size[1]}"
        return self

    @field_validator("whisper_model")
    @classmethod
    def known_model(cls, value: str) -> str:
        if value not in pack.WHISPER_MODELS:
            raise ValueError("Choose one of the listed Whisper models.")
        return value

    @field_validator("cover_image_model")
    @classmethod
    def known_image_model(cls, value: str) -> str:
        if value not in COVER_IMAGE_MODELS:
            raise ValueError("Choose one of the listed image models.")
        return value


@app.put(p("/api/settings"))
async def put_settings(body: MusicSettings) -> dict[str, Any]:
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    data = body.model_dump()
    if data["cover_system_prompt"].strip() in {"", DEFAULT_COVER_PROMPT}:
        data["cover_system_prompt"] = ""   # follows the shipped prompt
    temporary.write_text(json.dumps(data))
    temporary.replace(SETTINGS_PATH)
    return music_settings()


@app.post(p("/api/jobs/{job_id}/lyric-timing"), status_code=202)
async def request_lyric_timing(job_id: str) -> dict[str, Any]:
    if not lyric_timing_enabled():
        raise APIError(409, "lyric_timing_off", "Whisper lyric timing is turned off in Settings.")
    song = store.job(job_id)
    if not song or song["state"] != "complete" or not song.get("output_path") or not pack.has_sung_lines(song_lyrics(song)):
        raise APIError(404, "job_not_found", "That song has no lyrics to time.")
    return await queue_alignment(song)


@app.get(p("/api/jobs/{job_id}/lyric-timing"))
async def get_lyric_timing(job_id: str) -> Response:
    timings = (store.job(job_id) or {}).get("lyric_timings")
    if not timings:
        raise APIError(404, "not_found", "This song has no word timings.")
    return Response(timings, media_type="application/json")


# ---- YuE2 Studio (pytraveler/YuE2-ComfyUI): backend/pack.py ----
MAX_PACK_UPLOAD = 300 * 1024 * 1024


# Phones drop requests when the browser is left (to another app, the home screen): the page retries with the same
# Idempotency-Key and gets what the first try made instead of a second upload or a second queued read.
_idem: dict[str, asyncio.Future] = {}


async def idempotent(key: str | None, make: Any) -> Any:
    if not key or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
        return await make()
    if (earlier := _idem.get(key)) is not None:
        done = await asyncio.shield(earlier)
        if done is not None:
            return done() if callable(done) else done
    future = asyncio.get_running_loop().create_future()
    _idem[key] = future
    while len(_idem) > 2000:
        _idem.pop(next(iter(_idem)))
    try:
        out = await make()
    except BaseException:
        _idem.pop(key, None)
        future.set_result(None)
        raise
    # A queued job is answered with its current state; an upload with what it stored.
    job_id = out.get("id") if isinstance(out, dict) and "state" in out else None
    future.set_result((lambda: public_job(store.job(job_id) or {}) if store.job(job_id) else out) if job_id else out)
    return out


@app.post(p("/api/pack/upload"))
async def pack_upload(http_request: Request, file: UploadFile) -> dict[str, str]:
    return await idempotent(http_request.headers.get("Idempotency-Key"), lambda: _pack_upload(file))


async def _pack_upload(file: UploadFile) -> dict[str, str]:
    """A recording or MIDI file for a task: kept in ComfyUI's input folder under a fresh name."""
    original = Path(file.filename or "").name
    suffix = Path(original).suffix.lower()
    if suffix not in pack.AUDIO_SUFFIXES | pack.MIDI_SUFFIXES:
        raise APIError(422, "bad_file", "Upload an audio file (FLAC, WAV, MP3, Ogg, Opus, M4A) or a MIDI file.")
    data = await file.read(MAX_PACK_UPLOAD + 1)
    if len(data) > MAX_PACK_UPLOAD:
        raise APIError(413, "file_too_large", "That file is over 300 MB.")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(original).stem)[:80] or "upload"
    try:
        name = await comfy.upload_input(f"music_{uuid.uuid4().hex[:8]}_{stem}{suffix}", data)
    except ServiceError as exc:
        raise APIError(503, "comfy_unavailable", str(exc)) from exc
    return {"name": name, "label": original}


def _cover_root(job: dict[str, Any]) -> dict[str, Any] | None:
    """The cover request a song comes from: the cover itself, or the cover at the start of a chain of edits."""
    seen: set[str] = set()
    while job and job["id"] not in seen:
        seen.add(job["id"])
        request_data = json.loads(job.get("request_json") or "{}")
        if request_data.get("task") == "cover":
            return request_data
        if request_data.get("task") != "edit" or not request_data.get("source_job"):
            return None
        job = store.job(request_data["source_job"])
    return None


async def _edit_source(job_id: str) -> tuple[str, dict[str, Any] | None]:
    """The song an edit starts from, as a lossless copy in ComfyUI's input folder, and the cover it comes from
    (a cover is edited through its YuE2 take, which the edit then sings in the cover's voice again)."""
    source = store.job(job_id)
    if not source or source["state"] != "complete" or not source.get("output_path"):
        raise APIError(404, "job_not_found", "That song isn't available any more.")
    if not json.loads(source.get("request_json") or "{}").get("task"):
        raise APIError(422, "not_studio_song", "Only songs made with YuE2 Studio can be edited.")
    cover = _cover_root(source)
    relative = source["output_path"]
    if cover is not None:
        relative = json.loads(source.get("result_json") or "{}").get("take") or ""
        if not relative:
            raise APIError(422, "no_take", "This cover was made before covers kept an editable take. "
                                           "Make it again with Retry (same seed), then edit the new one.")
    path = (settings.output_dir / relative).resolve()
    if not path.is_relative_to(settings.output_dir.resolve()) or path.suffix.lower() != ".flac" or not path.is_file():
        raise APIError(404, "job_not_found", "That song's FLAC file is gone.")
    try:
        name = await comfy.upload_input(f"music_edit_{job_id[:12]}.flac", await asyncio.to_thread(path.read_bytes))
    except ServiceError as exc:
        raise APIError(503, "comfy_unavailable", str(exc)) from exc
    return name, cover


@app.post(p("/api/pack/run"), status_code=202)
async def pack_run(request: pack.PackRequest, http_request: Request) -> dict[str, Any]:
    return await idempotent(http_request.headers.get("Idempotency-Key"), lambda: _pack_run(request))


async def _pack_run(request: pack.PackRequest) -> dict[str, Any]:
    if request.remix_of and request.task in {"song", "cover"}:
        # Remix of a YuE2 Studio song or a cover: its own saved request and exact seed, with the page's new words.
        source, original = _remix_source(request.remix_of)
        fields = {**original, "seed": int(source["seed"]), "lyrics": request.lyrics, "remix_of": request.remix_of}
        if request.task == "song" and (score := json.loads(source.get("result_json") or "{}").get("score")):
            fields["score_abc"] = score
        request = pack.PackRequest.model_validate(fields)
    if problem := request.problem():
        raise APIError(422, "invalid_request", problem)
    if request.task == pack.PREVIEW_TASK:
        return await _voice_preview_run(request)
    comfy_state = await comfy.status()
    if not comfy_state["online"]:
        raise APIError(503, "comfy_unavailable", f"ComfyUI is offline at {settings.comfy_url}.")
    source, cover = await _edit_source(request.source_job) if request.task == "edit" else ("", None)
    workflow, seed, targets = pack.build(request, source, music_settings()["whisper_model"], cover)
    missing = await missing_files(settings.comfy_url, [workflow])
    if missing["files"] or missing["nodes"]:
        raise APIError(409, MISSING_CODE, missing_message(missing), {"missing": missing, "studio": "Music"})
    return await enqueue_job(workflow, seed, request.label(), request.lyrics, request.max_seconds, request.model_dump(), targets)


VOICE_PREVIEWS_KEPT = 6


def _prune_voice_previews() -> None:
    """Previews are throwaway: keep the newest few finished ones, delete the rest and their files."""
    finished = [item for item in store.jobs() if is_voice_preview(item) and item["state"] not in UNFINISHED_JOB_STATES]
    finished.sort(key=lambda item: item["created_at"], reverse=True)
    for item in finished[VOICE_PREVIEWS_KEPT:]:
        if item.get("output_path"):
            path = (settings.output_dir / item["output_path"]).resolve()
            if path.is_relative_to(settings.output_dir.resolve()) and path.name.startswith("MusicVoicePreview_"):
                path.unlink(missing_ok=True)
        store.delete_job(item["id"])


async def _voice_preview_run(request: pack.PackRequest) -> dict[str, Any]:
    """A custom voice heard on a stretch of one of your songs (source_job) or an uploaded clip (input_file)."""
    source = request.input_file
    if request.source_job:
        song = store.job(request.source_job)
        if not song or song["state"] != "complete" or not song.get("output_path") or is_voice_preview(song):
            raise APIError(404, "job_not_found", "That song isn't available any more.")
        if Path(song["output_path"]).suffix.lower() not in pack.AUDIO_SUFFIXES:
            raise APIError(422, "invalid_request", "That song has no audio file to preview on.")
        source = f"{song['output_path']} [output]"   # LoadAudio reads ComfyUI's output folder directly
    comfy_state = await comfy.status()
    if not comfy_state["online"]:
        raise APIError(503, "comfy_unavailable", f"ComfyUI is offline at {settings.comfy_url}.")
    workflow, seed, targets = pack.voice_preview(request, source)
    missing = await missing_files(settings.comfy_url, [workflow])
    if missing["files"] or missing["nodes"]:
        raise APIError(409, MISSING_CODE, missing_message(missing), {"missing": missing, "studio": "Music"})
    _prune_voice_previews()
    # Filed under its own VRAM peak: voice conversion needs far less than a YuE2 song.
    return await enqueue_job(workflow, seed, request.label(), "", request.preview_seconds, request.model_dump(), targets,
                             queue_model="RSP voice conversion")


# ---- RSP's custom voices: the library lives in the node pack (rsp/voices.py), reached through ComfyUI ----
MAX_VOICE_UPLOAD = 90 * 1024 * 1024   # ComfyUI's own upload limit is 100 MB
_VOICE_ID = re.compile(r"^[a-f0-9]{16}$")


async def _voices_call(method: str, path: str, **kwargs: Any) -> httpx.Response:
    try:
        async with httpx.AsyncClient(base_url=settings.comfy_url, timeout=120) as client:
            response = await client.request(method, "/rsp/voices" + path, **kwargs)
    except httpx.HTTPError as exc:
        raise APIError(503, "comfy_unavailable", f"ComfyUI is offline at {settings.comfy_url}.") from exc
    if response.status_code == 404 and not path:
        raise APIError(503, "rsp_missing", "ComfyUI doesn't have the RSP custom node: install it in Models -> Manager -> Core Backend -> Music covers, then restart ComfyUI.")
    if response.status_code >= 400:
        try:
            message = response.json().get("message")
        except ValueError:
            message = ""
        raise APIError(response.status_code, "voice_error", message or "ComfyUI refused that voice request.")
    return response


def _voice_id(voice_id: str) -> str:
    if not _VOICE_ID.match(voice_id):
        raise APIError(404, "voice_not_found", "That voice is gone.")
    return voice_id


# Renames go to RSP (voice.json). A ComfyUI still running an RSP from before its rename route answers 405; the new
# name is then kept here and shown instead, until a rename reaches RSP itself.
VOICE_NAMES_PATH = settings.data_dir / "voice-names.json"


def _voice_names() -> dict[str, str]:
    try:
        names = json.loads(VOICE_NAMES_PATH.read_text())
        return {str(k): str(v) for k, v in names.items()} if isinstance(names, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_voice_names(names: dict[str, str]) -> None:
    VOICE_NAMES_PATH.with_suffix(".tmp").write_text(json.dumps(names, ensure_ascii=False))
    VOICE_NAMES_PATH.with_suffix(".tmp").replace(VOICE_NAMES_PATH)


class VoiceRename(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@app.get(p("/api/voices"))
async def list_voices() -> dict[str, Any]:
    listing = (await _voices_call("GET", "")).json()
    if names := _voice_names():
        voices = [{**item, "name": names.get(str(item.get("id")), item.get("name"))} for item in listing.get("voices") or []]
        listing = {**listing, "voices": sorted(voices, key=lambda item: str(item.get("name", "")).lower())}
    return listing


@app.patch(p("/api/voices/{voice_id}"))
async def rename_voice(voice_id: str, body: VoiceRename) -> dict[str, Any]:
    voice_id = _voice_id(voice_id)
    name = " ".join(body.name.split())[:80]
    if not name:
        raise APIError(422, "invalid_name", "Give the voice a name.")
    names = _voice_names()
    try:
        item = (await _voices_call("PATCH", f"/{voice_id}", json={"name": name})).json()
        if names.pop(voice_id, None) is not None:
            _save_voice_names(names)
        return item
    except APIError as exc:
        if exc.status_code != 405:
            raise
    voices = (await _voices_call("GET", "")).json().get("voices") or []
    item = next((v for v in voices if v.get("id") == voice_id), None)
    if not item:
        raise APIError(404, "voice_not_found", "That voice is gone.")
    names[voice_id] = name
    _save_voice_names(names)
    return {**item, "name": name}


@app.post(p("/api/voices"))
async def add_voice(file: UploadFile, name: str = Form(default="", max_length=200)) -> dict[str, Any]:
    data = await file.read(MAX_VOICE_UPLOAD + 1)
    if len(data) > MAX_VOICE_UPLOAD:
        raise APIError(413, "file_too_large", "That file is over 90 MB. A few minutes of singing is plenty.")
    return (await _voices_call("POST", "", data={"name": name}, files={"file": (Path(file.filename or "voice").name, data)})).json()


# A voice from a YouTube video: yt-dlp fetches the audio (optionally only a stretch of it), then it's added like an upload.
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
voice_imports: dict[str, dict[str, Any]] = {}
voice_import_keys: dict[str, str] = {}


def _youtube_url(raw: str) -> str:
    """Accept only a YouTube video ID, then build our own URL for yt-dlp."""
    parsed = urlparse(raw.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"https", "http"} or parsed.port or parsed.username or parsed.password:
        raise APIError(422, "invalid_url", "Paste a YouTube video link.")
    video_id = ""
    if host in YOUTUBE_HOSTS:
        parts = parsed.path.strip("/").split("/")
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) == 2 and parts[0] in {"shorts", "live"}:
            video_id = parts[1]
    elif host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise APIError(422, "invalid_url", "Paste a link to one YouTube video.")
    return f"https://www.youtube.com/watch?v={video_id}"


class VoiceFromYouTube(BaseModel):
    url: str = Field(min_length=1, max_length=500)


class VoiceTrim(BaseModel):
    name: str = Field(default="", max_length=200)
    start: float = Field(default=0.0, ge=0)
    end: float | None = Field(default=None, gt=0)


VOICE_IMPORT_DIR = Path(tempfile.gettempdir()) / "voice-imports"


def _forget_voice_import(job_id: str) -> None:
    job = voice_imports.pop(job_id, None) or {}
    if job.get("task"):
        job["task"].cancel()
    shutil.rmtree(VOICE_IMPORT_DIR / job_id, ignore_errors=True)


async def _run(command: list[str], timeout: float) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, (out or b"").decode(errors="replace"), (err or b"").decode(errors="replace")


async def _voice_import_job(job_id: str, url: str) -> None:
    """Downloads the video's audio and waits ("ready") for a trim; the voice is added by /add."""
    job = voice_imports[job_id]
    work = VOICE_IMPORT_DIR / job_id
    work.mkdir(parents=True, exist_ok=True)
    try:
        try:
            code, out, err = await _run(["yt-dlp", "--no-config", "--no-playlist", "--no-progress", "--no-warnings",
                                         "--format", "bestaudio/best", "--extract-audio", "--audio-format", "best",
                                         "--match-filter", "duration <= 3600", "--max-filesize", "300M",
                                         "--socket-timeout", "20", "--retries", "2", "--fragment-retries", "2",
                                         "--print", "after_move:%(title)s", "--output", str(work / "source.%(ext)s"), url], 600)
        except asyncio.TimeoutError:
            raise APIError(504, "youtube_timeout", "YouTube took over 10 minutes. Try again.") from None
        files = [path for path in work.iterdir() if path.name.startswith("source.") and path.suffix.lower() != ".part"]
        if code or not files:
            reason = (err.strip().splitlines() or ["YouTube download failed."])[-1]
            if not files and not code:
                reason = "That video is over an hour long. Pick a shorter one."
            raise APIError(502, "youtube_failed", reason.removeprefix("ERROR: ")[:300])
        code, out_probe, _ = await _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(files[0])], 60)
        try:
            duration = float(out_probe.strip())
        except ValueError:
            raise APIError(502, "youtube_failed", "Couldn't read the downloaded audio.") from None
        lines = out.strip().splitlines()
        job.update(state="ready", stage="Ready to trim", source=str(files[0]), duration=round(duration, 2),
                   title=(lines[-1] if lines else "YouTube voice")[:80])
    except APIError as exc:
        job.update(state="failed", error=exc.message)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # never left "running"
        log.exception("YouTube voice %s failed", job_id[:8])
        job.update(state="failed", error=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        if job.get("state") != "ready":
            shutil.rmtree(work, ignore_errors=True)
        job.pop("task", None)


def _voice_import(job_id: str) -> dict[str, Any]:
    job = voice_imports.get(job_id) if re.fullmatch(r"[a-f0-9]{32}", job_id) else None
    if not job:
        raise APIError(404, "not_found", "That YouTube download isn't kept any more (Music restarted, or it was over 3 hours ago). Paste the link again.")
    return job


@app.post(p("/api/voices/youtube"), status_code=202)
async def voice_from_youtube(body: VoiceFromYouTube, http_request: Request) -> dict[str, Any]:
    key = http_request.headers.get("Idempotency-Key") or ""
    if key and (known := voice_import_keys.get(key)) in voice_imports:
        return {"id": known, **{k: v for k, v in voice_imports[known].items() if k in {"state", "stage"}}}
    url = _youtube_url(body.url)
    cutoff = time.monotonic() - 3 * 3600
    for old_id, old in list(voice_imports.items()):
        if old["created"] < cutoff:
            _forget_voice_import(old_id)
    for old_key, old_id in list(voice_import_keys.items()):
        if old_id not in voice_imports:
            voice_import_keys.pop(old_key, None)
    job_id = uuid.uuid4().hex
    voice_imports[job_id] = {"state": "running", "stage": "Downloading from YouTube", "created": time.monotonic()}
    if re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
        voice_import_keys[key] = job_id
    voice_imports[job_id]["task"] = asyncio.create_task(_voice_import_job(job_id, url))
    return {"id": job_id, "state": "running", "stage": voice_imports[job_id]["stage"]}


@app.get(p("/api/voices/youtube/{job_id}"))
async def voice_from_youtube_status(job_id: str, http_response: Response) -> dict[str, Any]:
    http_response.headers["Cache-Control"] = "no-store"
    job = _voice_import(job_id)
    return {key: value for key, value in job.items() if key in {"state", "stage", "error", "voice", "duration", "title"}}


@app.get(p("/api/voices/youtube/{job_id}/audio"))
async def voice_from_youtube_audio(job_id: str) -> FileResponse:
    job = _voice_import(job_id)
    if job.get("state") != "ready" or not os.path.isfile(job.get("source", "")):
        raise APIError(409, "not_ready", "That download isn't ready.")
    return FileResponse(job["source"], media_type=mimetypes.guess_type(job["source"])[0] or "audio/ogg")


@app.post(p("/api/voices/youtube/{job_id}/add"))
async def voice_from_youtube_add(job_id: str, body: VoiceTrim) -> dict[str, Any]:
    """Cuts the chosen stretch out of the download and adds it as a custom voice."""
    job = _voice_import(job_id)
    if job.get("state") == "done":
        return job["voice"]
    if job.get("state") != "ready" or job.get("adding"):
        raise APIError(409, "not_ready", "That download isn't ready to add.")
    duration = float(job["duration"])
    start, end = min(body.start, duration), min(body.end if body.end is not None else duration, duration)
    if end - start < 3:
        raise APIError(422, "too_short", "Keep at least 3 seconds (20-25 s or more of singing works best).")
    job["adding"] = True
    try:
        # FLAC keeps it exact; long stretches go to MP3 so they stay under the 90 MB upload limit.
        target = VOICE_IMPORT_DIR / job_id / ("voice.flac" if end - start <= 360 else "voice.mp3")
        codec = ["-c:a", "flac"] if target.suffix == ".flac" else ["-c:a", "libmp3lame", "-q:a", "0"]
        try:
            code, _, err = await _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                                       "-i", job["source"], "-vn", *codec, str(target)], 300)
        except asyncio.TimeoutError:
            raise APIError(504, "trim_timeout", "Cutting the audio took too long.") from None
        if code or not target.is_file():
            raise APIError(500, "trim_failed", (err.strip().splitlines() or ["Couldn't cut the audio."])[-1][:300])
        if target.stat().st_size > MAX_VOICE_UPLOAD:
            raise APIError(413, "file_too_large", "That stretch is over 90 MB. Take a shorter one.")
        name = " ".join(body.name.split())[:80] or job.get("title") or "YouTube voice"
        item = (await _voices_call("POST", "", data={"name": name}, files={"file": (target.name, target.read_bytes())})).json()
    finally:
        job.pop("adding", None)
    job.update(state="done", stage="Added", voice=item)
    shutil.rmtree(VOICE_IMPORT_DIR / job_id, ignore_errors=True)
    return item


@app.delete(p("/api/voices/youtube/{job_id}"))
async def voice_from_youtube_discard(job_id: str) -> dict[str, Any]:
    _forget_voice_import(job_id)
    return {"discarded": True}


@app.get(p("/api/voices/{voice_id}/audio"))
async def voice_audio(voice_id: str) -> Response:
    response = await _voices_call("GET", f"/{_voice_id(voice_id)}/audio")
    return Response(response.content, media_type=response.headers.get("content-type", "audio/flac"))


@app.delete(p("/api/voices/{voice_id}"))
async def delete_voice(voice_id: str) -> dict[str, Any]:
    result = (await _voices_call("DELETE", f"/{_voice_id(voice_id)}")).json()
    names = _voice_names()
    if names.pop(voice_id, None) is not None:
        _save_voice_names(names)
    return result


# ---- Enhance (like Video's): Hermes rewrites Music direction or Lyrics, through the framework's write-prompt ----
ENHANCE_SYSTEM = {
    "style": (
        "You improve the Music direction (the style text) of a song generator. Keep everything the user asked for: genre, "
        "language, mood, vocal character, instruments, tempo and any exact words. {format} Never name real "
        "artists or songs. If it is empty, write one that fits the lyrics. Return only the direction as the prompt."),
    "lyrics": (
        "You improve the lyrics of a song generator. Keep the user's meaning, story, language, names and any line in quotes. "
        "Make the lines singable with a steady meter and natural rhymes, and mark the structure with section tags in the "
        "format the MODEL GUIDE uses ([Verse], [Chorus], [Bridge] ...). If the lyrics are empty, write complete lyrics that "
        "fit the Music direction and length. If they are only [Instrumental], return them unchanged. Return only the "
        "lyrics, with line breaks, as the prompt."),
}
# How each model wants its Music direction written (its MODEL GUIDE has the details).
STYLE_FORMAT = {
    "yue2": ("Write it as the one concise, comma-separated line the MODEL GUIDE asks for: language, genre, vocal "
             "character, instruments, melody/feel, tempo. A description of the music only, no instructions to the model."),
    "minimax_music3": ("Write it as the MODEL GUIDE's caption: exactly three headed parts in order (Global Metadata, Vocal "
                       "Details, Arrangement as a section-by-section timeline), about 250-450 English words and under 3,500 "
                       "characters. Never quote or paraphrase lyric lines in it."),
}
KEEP_METER = (" This is a Remix: the melody is already written. Keep every section and every line in place, and give each "
              "line about the same number of syllables as the line it replaces, so the new words fit the same notes.")


class EnhanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(pattern=r"^(style|lyrics)$")
    style: str = Field(default="", max_length=4000)
    lyrics: str = Field(default="", max_length=20000)
    original_lyrics: str = Field(default="", max_length=20000)   # Remix: the words whose meter the new ones keep
    model: str = Field(default="yue2", max_length=40)
    duration: float = Field(default=0.0, ge=0.0, le=900.0)
    wish: str = Field(default="", max_length=2000)


@app.post(p("/api/enhance"))
async def enhance(body: EnhanceRequest) -> dict[str, Any]:
    """Enhance next to Music direction and Lyrics: one Hermes turn in the framework (it queues its own GPU turn)."""
    return {"text": await enhance_text(body)}


# The page starts Enhance as a job and polls it: leaving a phone's browser (or the page being closed) cuts a request
# that waits minutes for the GPU, but not the job, and the page picks its result up when it's back.
enhance_jobs: dict[str, dict[str, Any]] = {}
enhance_keys: dict[str, str] = {}


async def _enhance_job(job_id: str, body: EnhanceRequest) -> None:
    job = enhance_jobs[job_id]
    try:
        job.update(state="done", text=await enhance_text(body, live_id=job_id))
    except APIError as exc:
        job.update(state="failed", error=exc.message)
    except Exception as exc:  # never left "running"
        log.exception("Enhance %s failed", job_id[:8])
        job.update(state="failed", error=f"{type(exc).__name__}: {exc}"[:300])
    job.pop("task", None)


@app.post(p("/api/enhance/start"), status_code=202)
async def enhance_start(body: EnhanceRequest, http_request: Request) -> dict[str, Any]:
    key = http_request.headers.get("Idempotency-Key") or ""
    if key and (known := enhance_keys.get(key)) in enhance_jobs:
        return {"id": known, "state": enhance_jobs[known]["state"]}
    if body.field == "style" and not (body.style.strip() or body.lyrics.strip()):
        raise APIError(422, "nothing_to_enhance", "Write a Music direction or some lyrics first.")
    if body.field == "lyrics" and not (body.lyrics.strip() or body.style.strip()):
        raise APIError(422, "nothing_to_enhance", "Write some lyrics or a Music direction first.")
    cutoff = time.monotonic() - 3 * 3600
    for old_id, old in list(enhance_jobs.items()):
        if old["created"] < cutoff and old["state"] != "running":
            enhance_jobs.pop(old_id, None)
    for old_key, old_id in list(enhance_keys.items()):
        if old_id not in enhance_jobs:
            enhance_keys.pop(old_key, None)
    job_id = uuid.uuid4().hex
    enhance_jobs[job_id] = {"state": "running", "created": time.monotonic(), "field": body.field}
    if re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
        enhance_keys[key] = job_id
    enhance_jobs[job_id]["task"] = asyncio.create_task(_enhance_job(job_id, body))
    return {"id": job_id, "state": "running"}


@app.get(p("/api/enhance/{job_id}"))
async def enhance_status(job_id: str, http_response: Response) -> dict[str, Any]:
    http_response.headers["Cache-Control"] = "no-store"
    job = enhance_jobs.get(job_id)
    if not job:
        raise APIError(404, "not_found", "That Enhance isn't known any more (Music restarted). Run it again.")
    # What the model has streamed so far (the framework keeps it while it writes): shown live under the box.
    if job.get("live", {}).get("phase") != "done":
        base = os.getenv("BUNNY_FRAMEWORK_URL", "http://127.0.0.1:8700").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=4) as client:
                response = await client.get(f"{base}/v1/agentic/live/{job_id}", headers={"X-Queue-Internal-Secret": settings.queue_internal_secret})
            if response.status_code == 200:
                job["live"] = response.json()
        except httpx.HTTPError:
            pass
    return {key: value for key, value in job.items() if key in {"state", "text", "error", "field", "live"}}


async def enhance_text(body: EnhanceRequest, live_id: str = "") -> str:
    if body.field == "style" and not (body.style.strip() or body.lyrics.strip()):
        raise APIError(422, "nothing_to_enhance", "Write a Music direction or some lyrics first.")
    if body.field == "lyrics" and not (body.lyrics.strip() or body.style.strip()):
        raise APIError(422, "nothing_to_enhance", "Write some lyrics or a Music direction first.")
    remix = bool(body.original_lyrics.strip())
    model = {"yue2_pack": "yue2"}.get(body.model, body.model)
    system = ENHANCE_SYSTEM[body.field].replace("{format}", STYLE_FORMAT.get(model, STYLE_FORMAT["yue2"])) \
        + (KEEP_METER if remix and body.field == "lyrics" else "")
    parts = [f"Improve the {'Music direction' if body.field == 'style' else 'lyrics'} below.",
             f"MUSIC DIRECTION:\n{body.style.strip() or '(empty)'}", f"LYRICS:\n{body.lyrics.strip() or '(empty)'}"]
    if remix:
        parts.append(f"THE SONG'S ORIGINAL LYRICS (match their sections and syllables per line):\n{body.original_lyrics.strip()}")
    if body.duration:
        parts.append(f"Song length: about {round(body.duration)} seconds.")
    if body.wish.strip():
        parts.append(f"The user's wishes: {body.wish.strip()}")
    base = os.getenv("BUNNY_FRAMEWORK_URL", "http://127.0.0.1:8700").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=4 * 3600) as client:
            response = await client.post(f"{base}/v1/agentic/music/write-prompt", headers={"X-Queue-Internal-Secret": settings.queue_internal_secret},
                                         json={"brief": "\n\n".join(parts)[:12000], "settings": {"model": model}, "system": system,
                                               "purpose": f"enhance {'direction' if body.field == 'style' else 'lyrics'}", "live_id": live_id})
    except httpx.HTTPError as exc:
        raise APIError(503, "agentic_unavailable", f"Hermes is unavailable: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise APIError(502, "enhance_failed", f"Hermes couldn't enhance it: {response.text[:300]}")
    text = str(response.json().get("prompt") or "").strip()
    if not text:
        raise APIError(502, "enhance_failed", "Hermes returned nothing.")
    return text[:4000] if body.field == "style" else text[:20000]


class CoverDirection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=64)
    label: str = Field(default="", max_length=400)
    wish: str = Field(default="", max_length=4000)


@app.post(p("/api/cover/direction"))
async def cover_direction(body: CoverDirection) -> dict[str, Any]:
    """Agentic's Music direction, from a finished transcribe job with describe on. Queues its own GPU turn in the framework."""
    source = store.job(body.job_id)
    if not source or source["state"] != "complete" or not json.loads(source.get("result_json") or "{}").get("facts"):
        raise APIError(404, "job_not_found", "That song's details aren't available. Try again.")
    return {"style": await write_direction_text(source, body.label, body.wish)}


async def write_direction_text(source: dict[str, Any], label: str, wish: str) -> str:
    request_data = json.loads(source.get("request_json") or "{}")
    result = json.loads(source.get("result_json") or "{}")
    brief = pack.direction_brief(result, bool(request_data.get("recognize_lyrics") or request_data.get("whisper")), label, wish)
    base = os.getenv("BUNNY_FRAMEWORK_URL", "http://127.0.0.1:8700").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=4 * 3600) as client:
            response = await client.post(f"{base}/v1/agentic/music/write-prompt", headers={"X-Queue-Internal-Secret": settings.queue_internal_secret},
                                         json={"brief": brief, "settings": {"model": "yue2"}, "purpose": "music direction"})
    except httpx.HTTPError as exc:
        raise APIError(503, "agentic_unavailable", f"Agentic is unavailable: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise APIError(502, "agentic_failed", f"Agentic couldn't write it: {response.text[:300]}")
    return str(response.json()["prompt"])


direction_tasks: dict[str, asyncio.Task[None]] = {}


def _set_direction(job_id: str, **fields: Any) -> None:
    item = store.job(job_id)
    if item:
        store.update_job(job_id, result_json=json.dumps({**json.loads(item.get("result_json") or "{}"), **fields}))


async def _direction_task(job_id: str) -> None:
    """A cover's Music direction, written on the server as soon as its song has been read (see PackRequest.direction)."""
    try:
        source = store.job(job_id)
        request_data = json.loads((source or {}).get("request_json") or "{}")
        if not source or not json.loads(source.get("result_json") or "{}").get("facts"):
            _set_direction(job_id, direction_state="failed", direction_error="The song's details didn't come through.")
        else:
            text = await write_direction_text(source, str(request_data.get("direction_label") or ""), str(request_data.get("direction_wish") or ""))
            _set_direction(job_id, direction_state="done", direction=text, direction_error="")
    except asyncio.CancelledError:
        raise
    except APIError as exc:
        _set_direction(job_id, direction_state="failed", direction_error=exc.message)
    except Exception as exc:  # never leave it "writing" forever
        log.exception("Music direction for %s failed", job_id[:8])
        _set_direction(job_id, direction_state="failed", direction_error=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        direction_tasks.pop(job_id, None)
    await publish_jobs()


def start_direction(job_id: str) -> None:
    if job_id in direction_tasks:
        return
    _set_direction(job_id, direction_state="writing", direction_error="")
    direction_tasks[job_id] = asyncio.create_task(_direction_task(job_id))


def resume_directions() -> None:
    """After a restart: directions that were being written start again (the read itself is finished and kept)."""
    for item in store.jobs():
        if item["state"] == "complete" and json.loads(item.get("result_json") or "{}").get("direction_state") == "writing":
            direction_tasks.pop(item["id"], None)
            start_direction(item["id"])


async def queue_grant(job_id: str, entry_id: str, token: str) -> bool:
    """bunny-lock granted this job the GPU (over the client's stream). True = started."""
    # generate() registers the job right after its enqueue returns; the client
    # already waits for that, this only covers a slow save.
    for _ in range(100):
        item = store.job(job_id)
        if job_id in pending_submissions or (item and item["state"] != "waiting_for_gpu"):
            break
        await asyncio.sleep(0.02)
    item = store.job(job_id)
    pending = pending_submissions.pop(job_id, None)
    if not item or item["state"] != "waiting_for_gpu" or pending is None:
        return False
    store.update_job(job_id, state="preparing", lock_token=token)
    task = asyncio.create_task(submit_job(job_id, pending["workflow"], token, pending.get("targets")))
    submission_tasks.add(task)
    task.add_done_callback(submission_tasks.discard)
    await publish_jobs()
    return True


async def queue_cancel(job_id: str, entry_id: str) -> tuple[int, dict[str, Any]]:
    """The queue page's Cancel: the same as the card's Cancel button."""
    try:
        return 200, await cancel_job(job_id)
    except APIError as exc:
        return exc.status_code, {"error": exc.code, "message": exc.message}


@app.get(p("/api/jobs"))
async def jobs() -> list[dict[str, Any]]:
    return public_jobs(store.jobs())


@app.get(p("/api/jobs/{job_id}"))
async def job(job_id: str) -> dict[str, Any]:
    item = store.job(job_id)
    if not item:
        raise APIError(404, "job_not_found", "Generation not found.")
    return public_job(item)


@app.get(p("/api/jobs/{job_id}/stats"))
async def job_stats_route(job_id: str) -> dict[str, Any]:
    item = store.job(job_id)
    if not item:
        raise APIError(404, "job_not_found", "Generation not found.")
    live = live_job_timings(job_id) if item["state"] in UNFINISHED_JOB_STATES else None
    return job_stats(item, live)


@app.post(p("/api/jobs/{job_id}/retry"), status_code=202)
async def retry_job(job_id: str) -> dict[str, Any]:
    item = store.job(job_id)
    if not item:
        raise APIError(404, "job_not_found", "Generation not found.")
    if item["state"] in {"waiting_for_gpu", "preparing", "queued", "rendering", "cancelling"}:
        raise APIError(409, "job_active", "This generation is still active.")
    try:
        request_data = json.loads(item.get("request_json") or "{}")
        request_data["seed"] = int(item["seed"])
        if request_data.get("task"):
            return await _pack_run(pack.PackRequest.model_validate(request_data))
        original = GenerationRequest.model_validate(request_data)
    except (ValueError, TypeError, KeyError) as exc:
        raise APIError(409, "retry_unavailable", "The saved generation settings are unavailable.") from exc
    return await generate(original)


@app.post(p("/api/jobs/{job_id}/cancel"))
async def cancel_job(job_id: str) -> dict[str, Any]:
    item = store.job(job_id)
    if not item:
        raise APIError(404, "job_not_found", "Generation not found.")
    if item["state"] in {"complete", "failed"}:
        raise APIError(409, "job_finished", "This generation has already finished and can be removed instead.")
    if item["state"] in {"cancelling", "cancelled"}:
        return {"cancelled": item["state"] == "cancelled", "job": public_job(item)}

    previous_state = item["state"]
    if previous_state == "waiting_for_gpu":
        pending_submissions.pop(job_id, None)
        if item.get("queue_entry_id"):
            await lock.dequeue(item["queue_entry_id"])
        store.update_job(job_id, state="cancelled", completed_at=utc_now())
        log.info("Cancelled queued (not yet submitted) generation job %s", job_id[:8])
        await publish_jobs()
        return {"cancelled": True, "job": public_job(store.job(job_id))}
    store.update_job(job_id, state="cancelling")
    await publish_jobs()
    prompt_id = item.get("prompt_id")
    if not prompt_id:
        log.info("Marked preparing generation job %s for cancellation", job_id[:8])
        return {"cancelled": True, "job": public_job(store.job(job_id))}
    try:
        cancelled = await comfy.cancel(prompt_id)
    except ServiceError as exc:
        # ComfyUI unreachable: nothing will ever finish this job, so the cancel still ends it
        # and hands back the GPU slot (it used to revert to rendering and hold the lock forever).
        log.warning("Force-cancelling generation job %s: ComfyUI unreachable (%s)", job_id[:8], exc)
        cancelled = False
    if not cancelled:
        log.warning("Force-cancelling generation job %s: ComfyUI no longer has it queued or rendering", job_id[:8])
    store.update_job(job_id, state="cancelled", completed_at=utc_now())
    await finalize_job_lock(job_id, item.get("lock_token"))
    log.info("Cancelled generation job %s (%s)", job_id[:8], prompt_id[:8])
    await publish_jobs()
    return {"cancelled": True, "job": public_job(store.job(job_id))}


async def queue_adopt(job_id: str, entry_id: str, active: bool, token: str | None) -> bool:
    """After a restart: this entry was ours. A saved waiting job keeps its place
    (or starts now, if its turn came while the app was down)."""
    item = store.job(job_id)
    if not item or item["state"] != "waiting_for_gpu" or job_id not in pending_submissions:
        return False
    store.update_job(job_id, queue_entry_id=entry_id)
    return await queue_grant(job_id, entry_id, token) if active and token else True


lock.on_grant, lock.on_cancel = queue_grant, queue_cancel
lock.on_adopt = queue_adopt


@app.delete(p("/api/jobs/{job_id}"))
async def delete_job(job_id: str) -> dict[str, bool]:
    item = store.job(job_id)
    if not item:
        raise APIError(404, "job_not_found", "Generation not found.")
    if item["state"] in {"waiting_for_gpu", "preparing", "queued", "rendering", "cancelling"}:
        raise APIError(409, "job_active", "Cancel this generation before removing it from history.")
    store.delete_job(job_id)
    if task := cover_tasks.get(job_id):
        task.cancel()
    (COVER_DIR / f"{job_id}.png").unlink(missing_ok=True)
    cover_errors.pop(job_id, None)
    for other in store.jobs():
        if align_source(other) == job_id and other["state"] not in UNFINISHED_JOB_STATES:
            store.delete_job(other["id"])
    log.info("Removed generation job %s from Music history", job_id[:8])
    await publish_jobs()
    return {"deleted": True}


def job_output(job_id: str) -> tuple[dict[str, Any], Path]:
    item = store.job(job_id)
    if not item or not item.get("output_path"):
        raise APIError(404, "output_not_found", "This generation has no available output.")
    stored_path = Path(item["output_path"])
    path = (stored_path if stored_path.is_absolute() else settings.output_dir / stored_path).resolve()
    try:
        path.relative_to(settings.output_dir.resolve())
    except ValueError as exc:
        raise APIError(403, "invalid_output", "The output path is outside the configured output directory.") from exc
    if not path.is_file():
        raise APIError(404, "output_not_found", "The generated output file is missing.")
    return item, path


@app.get(p("/api/outputs/{job_id}/stream"))
async def stream_output(job_id: str) -> FileResponse:
    _, path = job_output(job_id)
    return FileResponse(
        path, media_type=mimetypes.guess_type(path.name)[0] or "audio/flac",
        headers={"Accept-Ranges": "bytes", "Content-Disposition": "inline"},
    )


@app.get(p("/api/outputs/{job_id}/download"))
async def download_output(job_id: str) -> FileResponse:
    _, path = job_output(job_id)
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@app.get(p("/api/events"))
async def event_stream() -> StreamingResponse:
    return StreamingResponse(events.stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Agentic sessions, Hermes settings, model guides and the Hermes terminal live in the
# internal framework (BunnyBytes/shared). The browser goes through this studio so the
# framework secret stays server-side; one route forwards the allowed paths.
_AGENTIC_PATH = re.compile(r"^(models|settings|guides|preview|rag|attachments|sessions(/[a-f0-9]{32}(/(cancel|pause|resume|plan|steer))?)?)$")


@app.api_route(p("/api/agentic/{path:path}"), methods=["GET", "POST", "PUT", "DELETE"])
async def agentic_proxy(path: str, request: Request):
    if not _AGENTIC_PATH.match(path):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    base = os.getenv("BUNNY_FRAMEWORK_URL", "http://127.0.0.1:8700").rstrip("/")
    url = base + ("/v1/agentic/models" if path == "models" else "/v1/agentic/music/" + path)
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=120 if path == "attachments" else 30) as client:
            response = await client.request(request.method, url, content=body or None,
                headers={"X-Queue-Internal-Secret": settings.queue_internal_secret, "Content-Type": "application/json"})
        return JSONResponse(response.json(), status_code=response.status_code)
    except (httpx.HTTPError, ValueError) as exc:
        return JSONResponse({"detail": f"Agentic framework unavailable: {type(exc).__name__}"}, status_code=503)


if __name__ == "__main__":
    uvicorn.run("app:app", host=settings.host, port=settings.port)
