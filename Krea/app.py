from __future__ import annotations

import asyncio
import hmac
import contextlib
import copy
import io
import zipfile
import json
import logging
import mimetypes
import os
import re
import shutil
import time
from urllib.parse import urlsplit
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
import httpx
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend.comfy import ComfyHTTPError, ComfyService, ServiceError, output_from_history
from backend import custom_loras, gallery_hermes, lora_aliases, lora_catalog, lora_links, lora_previews
from backend.config import BASE_DIR, settings
from backend.events import EventBroker
from backend.generation import (
    DEFAULT_LORA_ENABLED, GenerationError, LORA_SLOTS, batch_seed, build_enhanced_workflows, build_workflows, display_size,
)
from backend.lock import LockClient
from backend.models import (
    ANIMA_VERSIONS, ANIMA_LORA_SLOTS, MOSS_LORA_SLOTS, MOSS_FAST_CHECKPOINT, MOSS_FAST_MODES, DEFAULT_QWEN_LORA_ENABLED, DEFAULT_REFERENCE_STRENGTHS, LORA_INFO, MAX_SIZE, QWEN_LORA_SLOTS, MODEL_ANIMA, MODEL_MOSS, MODEL_LUSTIFY, MODEL_QWEN_IMAGE_21, QWEN_DEFAULT_CFG, QWEN_DEFAULT_STEPS,
    QWEN_MAX_PIXELS, QWEN_MAX_SIDE, QWEN_REFERENCE_SLOT_COUNT, REFERENCE_SLOT_COUNT, GenerationRequest,
    PresetRenameRequest, PresetRequest, lora_family, lora_slots_for,
)
from backend.qwen import enhancer_text_from_history
from backend import qwen as qwen_graphs
from backend.models import KREA_STYLE_REFERENCE_LORA, QWEN_TEXT_ENCODERS, QWEN_VIGGLE_LORA
from backend.workflow_models import (LEARN_SKIP_INPUTS, MISSING_CODE, Slot, WorkflowModelRequest, WorkflowModels,
                                     missing_files, missing_message, slots_from_graph)
from backend import checkpoints
from backend.store import Store, utc_now


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("krea")

settings.prepare()
custom_loras.initialize()
lora_catalog.initialize()
store = Store(settings.data_dir / "krea.sqlite3")
comfy = ComfyService(settings)
lock = LockClient(settings)
events = EventBroker()


def _workflow_model_seeds() -> dict[str, dict[str, Any]]:
    """Advanced workflow card: the fixed model files of each Image workflow
    (more are learned from real graphs - see shared/workflow_models.py)."""
    try:
        template = slots_from_graph(json.loads(settings.workflow_path.read_text()), LEARN_SKIP_INPUTS)
    except (OSError, ValueError):
        template = []
    upscaler = Slot("UpscaleModelLoader", "model_name", qwen_graphs.UPSCALE_MODEL_NAME, "4x upscaler")
    enhancers = [Slot("CLIPLoader", "clip_name", qwen_graphs.PE_T2I_NAME, "Prompt enhancer (text)"),
                 Slot("CLIPLoader", "clip_name", qwen_graphs.PE_I2I_NAME, "Prompt enhancer (with references)")]
    return {
        "krea2": {"label": "Krea 2 (any Krea 2 checkpoint)", "slots": [
            *template, Slot("LoraLoaderModelOnly", "lora_name", KREA_STYLE_REFERENCE_LORA, "Style-reference LoRA"), *enhancers]},
        MODEL_QWEN_IMAGE_21: {"label": "Qwen Image 2.1", "slots": [
            Slot("CLIPLoader", "clip_name", QWEN_TEXT_ENCODERS["standard"], "Text encoder (standard)"),
            Slot("VAELoader", "vae_name", qwen_graphs.VAE_NAME, "VAE"), upscaler,
            Slot("LoraLoaderModelOnly", "lora_name", QWEN_VIGGLE_LORA, "Viggle speed LoRA"), *enhancers]},
        MODEL_ANIMA: {"label": "Anima", "slots": [
            Slot("CLIPLoader", "clip_name", "qwen_3_06b_base.safetensors", "Text encoder"),
            Slot("VAELoader", "vae_name", "qwen_image_vae.safetensors", "VAE"), upscaler]},
        MODEL_MOSS: {"label": "MOSS-Image", "slots": [
            Slot("MOSSImageCLIPLoader", "clip_name", settings.moss_clip_name, "MOSS text encoder"),
            Slot("VAELoader", "vae_name", settings.moss_vae_name, "MOSS VAE")]},
    }


workflow_models = WorkflowModels(settings.data_dir / "workflow-models.json", settings.comfy_url,
                                 _workflow_model_seeds(), aliases={MODEL_LUSTIFY: "krea2"})
client_id = f"krea-web-{uuid.uuid4().hex}"

submission_tasks: set[asyncio.Task[None]] = set()
heartbeat_tasks: dict[str, tuple[asyncio.Task[None], asyncio.Event]] = {}
# Jobs enqueued with bunny-lock but not yet given the go-ahead to submit to
# ComfyUI - populated in generate(), consumed (and popped) in the internal
# proceed endpoint once the coordinator calls back.
pending_submissions: dict[str, dict[str, Any]] = {}

PREFIX = settings.mount_prefix  # e.g. "" standalone, "/Image" behind the BunnyBytes gateway

MODEL_LABELS = {"krea2": "Krea 2", MODEL_LUSTIFY: "Lustify (Krea 2)", MODEL_QWEN_IMAGE_21: "Qwen Image 2.1", MODEL_ANIMA: "Anima", MODEL_MOSS: "MOSS-Image"}

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_UPLOAD_TYPES = {"image/png", "image/jpeg", "image/webp"}


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str, extra: dict[str, Any] | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}


def p(path: str) -> str:
    """Prefix a route/asset path with the configured mount prefix."""
    return f"{PREFIX}{path}"


def job_request(item: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(item.get("request_json") or "{}")
    except json.JSONDecodeError:
        return {}


def job_prompt_ids(item: dict[str, Any]) -> list[str]:
    """Every ComfyUI prompt of the job (one per image; the enhancer pre-pass alone while it runs)."""
    ids = json.loads(item.get("prompt_ids") or "null")
    return ids if ids else ([item["prompt_id"]] if item.get("prompt_id") else [])


def job_outputs(item: dict[str, Any]) -> list[str | None]:
    """Output path per image (relative to the output dir); None = not finished,
    "" = finished without a file. Jobs from before multi-image only have output_path."""
    count = int(item.get("batch_count") or 1)
    outputs = json.loads(item.get("outputs_json") or "null")
    if outputs is None:
        outputs = [item.get("output_path")] + [None] * (count - 1)
    return (outputs + [None] * count)[:max(count, len(outputs))]


def output_version(stored: str) -> str:
    """Cache-busting token for an output file (its mtime), so the browser can
    keep images and thumbnails for good and still notice a replaced file."""
    path = Path(stored)
    path = path if path.is_absolute() else settings.output_dir / path
    try:
        return str(path.stat().st_mtime_ns // 1_000_000)
    except OSError:
        return "0"


def public_job(item: dict[str, Any]) -> dict[str, Any]:
    output = bool(item.get("output_path"))
    seed = int(item["seed"])
    model = item.get("model") or "krea2"
    version = job_request(item).get("anima_version", "aesthetic")
    model_label = f"Anima · {ANIMA_VERSIONS.get(version, ANIMA_VERSIONS['aesthetic'])[1]}" if model == MODEL_ANIMA else MODEL_LABELS.get(model, model)
    if model != MODEL_ANIMA and (checkpoint := job_request(item).get("checkpoint")):
        model_label += f" · {Path(checkpoint).name.removesuffix('.safetensors')}"
    images = [
        {"index": index, "seed": batch_seed(seed, index),
         "url": p(f"/api/outputs/{item['id']}/stream?index={index}&v={output_version(path)}"),
         "thumb_url": p(f"/api/outputs/{item['id']}/thumb?index={index}&v={output_version(path)}"),
         "download_url": p(f"/api/outputs/{item['id']}/download?index={index}")}
        for index, path in enumerate(job_outputs(item)) if path
    ]
    return {
        "id": item["id"],
        "prompt_id": item.get("prompt_id"),
        "model": item.get("model") or "krea2",
        "model_label": model_label,
        "stage": item.get("stage") or "render",
        "prompt": item["prompt"],
        "enhanced_prompt": item.get("enhanced_prompt"),
        "seed": int(item["seed"]),
        "width": item["width"],
        "height": item["height"],
        "enhance": bool(item["enhance"]),
        "state": item["state"],
        "created_at": item["created_at"],
        "started_at": item.get("started_at"),
        "completed_at": item.get("completed_at"),
        "error": item.get("error"),
        "progress": int(item.get("progress") or 0),
        "output_url": p(f"/api/outputs/{item['id']}/stream?v={output_version(item['output_path'])}") if output else None,
        "output_files": [path for path in job_outputs(item) if path],
        "thumb_url": p(f"/api/outputs/{item['id']}/thumb?v={output_version(item['output_path'])}") if output else None,
        "download_url": p(f"/api/outputs/{item['id']}/download") if output else None,
        # The submitted settings: the card's details dialog, "Use these settings" and Retry.
        "request": job_request(item),
        # Multi-image generations: every finished image so far, and a zip of them.
        "batch_count": int(item.get("batch_count") or 1),
        "images": images,
        "zip_url": p(f"/api/outputs/{item['id']}/zip") if len(images) > 1 else None,
    }


async def publish_jobs() -> None:
    await events.publish("jobs", [public_job(item) for item in store.visible_jobs()])


def start_heartbeat(job_id: str, token: str | None) -> None:
    if not token:
        return
    stop_event = asyncio.Event()
    task = asyncio.create_task(lock.heartbeat_loop(token, stop_event))
    heartbeat_tasks[job_id] = (task, stop_event)


async def stop_heartbeat(job_id: str) -> None:
    entry = heartbeat_tasks.pop(job_id, None)
    if not entry:
        return
    task, stop_event = entry
    stop_event.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def finalize_job_lock(job_id: str, token: str | None) -> None:
    """Stop this job's heartbeat and release its lock token. Called on every
    terminal state (complete/failed/cancelled) and on unhandled errors."""
    try:
        await stop_heartbeat(job_id)
    finally:
        # Shielded: a cancel arriving here must not skip the release (the slot
        # would sit idle until its TTL expired).
        await asyncio.shield(lock.release(token))


async def cancel_prompts(prompt_ids: list[str]) -> bool:
    """Best-effort cancel of ComfyUI prompts; True if any was still queued/running."""
    cancelled = False
    for prompt_id in prompt_ids:
        with contextlib.suppress(ServiceError):
            cancelled = await comfy.cancel(prompt_id) or cancelled
    return cancelled


async def submit_prompts(job_id: str, workflows: list[dict[str, Any]]) -> list[str]:
    """Queue every image's workflow in ComfyUI (they run one after another),
    recording the ids as they go; stops early if the job is being cancelled."""
    prompt_ids: list[str] = []
    model = (store.job(job_id) or {}).get("model")
    for workflow in workflows:
        workflow_models.apply(model, workflow)
        prompt_ids.append(await comfy.submit(workflow, client_id))
        store.update_job(job_id, prompt_id=prompt_ids[0], prompt_ids=json.dumps(prompt_ids))
        current = store.job(job_id)
        if current and current["state"] in {"cancelling", "cancelled"}:
            break
    return prompt_ids


async def submit_job(job_id: str, workflows: list[dict[str, Any]], lock_token: str | None) -> None:
    prompt_ids: list[str] = []
    try:
        # The fastest ComfyUI memory profile for this model (switches only while ComfyUI is idle).
        job = store.job(job_id)
        await comfy.ensure_profile("qwen" if job and job.get("model") == MODEL_QWEN_IMAGE_21 else "krea")
        store.update_job(job_id, started_at=utc_now())
        prompt_ids = await submit_prompts(job_id, workflows)
        job = store.job(job_id)
        if job and job["state"] == "cancelling":
            await cancel_prompts(prompt_ids)
            store.update_job(job_id, state="cancelled", completed_at=utc_now())
            await finalize_job_lock(job_id, lock_token)
            log.info("Cancelled generation job %s during submission", job_id[:8])
        else:
            state = "enhancing" if job and job.get("stage") == "enhance" else "queued"
            store.update_job(job_id, state=state, progress=0)
            start_heartbeat(job_id, lock_token)
            log.info("Submitted generation job %s as %d prompt(s), first %s", job_id[:8], len(prompt_ids), prompt_ids[0][:8])
    except Exception as exc:
        await cancel_prompts(prompt_ids)
        store.update_job(job_id, state="failed", error=str(exc), completed_at=utc_now())
        await finalize_job_lock(job_id, lock_token)
        log.exception("Generation submission failed for %s", job_id[:8])
    await publish_jobs()


async def advance_enhanced_job(job: dict[str, Any], history: dict[str, Any]) -> None:
    """The Qwen prompt-enhancer pre-pass finished: parse its answer and submit
    the image prompt under the same GPU lock token (the lock is only released
    once the image prompt reaches a terminal state)."""
    job_id = job["id"]
    try:
        request = GenerationRequest.model_validate(json.loads(job["request_json"]))
        workflows, final_prompt, size = build_enhanced_workflows(
            settings, request, int(job["seed"]), enhancer_text_from_history(history) or ""
        )
    except (GenerationError, ValueError) as exc:
        store.update_job(job_id, state="failed", error=f"Prompt enhancer: {exc}", completed_at=utc_now())
        await finalize_job_lock(job_id, job.get("lock_token"))
        return
    current = store.job(job_id)
    if not current or current["state"] in {"cancelling", "cancelled"}:
        store.update_job(job_id, state="cancelled", completed_at=utc_now())
        await finalize_job_lock(job_id, job.get("lock_token"))
        return
    size_changes = {"width": size[0], "height": size[1]} if size else {}
    store.update_job(job_id, stage="render", state="queued", progress=0, enhanced_prompt=final_prompt,
                     prompt_ids=json.dumps([]), outputs_json=None, **size_changes)
    try:
        prompt_ids = await submit_prompts(job_id, workflows)
    except ServiceError as exc:
        await cancel_prompts(json.loads((store.job(job_id) or {}).get("prompt_ids") or "[]"))
        store.update_job(job_id, state="failed", error=str(exc), completed_at=utc_now())
        await finalize_job_lock(job_id, job.get("lock_token"))
        return
    log.info("Job %s: prompt enhanced, submitted %d image prompt(s)", job_id[:8], len(prompt_ids))


def _history_error(history: dict[str, Any]) -> str | None:
    status = history.get("status", {})
    status_text = str(status.get("status_str", "success")).lower()
    if not (status_text in {"error", "failed"} or status.get("completed") is False):
        return None
    messages = status.get("messages") or []
    return str(messages[-1])[:1000] if messages else "ComfyUI reported a generation failure."


async def check_job(job: dict[str, Any], running_ids: set[str]) -> bool:
    """One monitor pass over a job's prompts; True if anything changed. Each
    finished image is recorded right away, so the card fills in while the rest
    render. A failed image fails the job (finished images stay) and cancels the
    images still queued."""
    prompt_ids = job_prompt_ids(job)
    if not prompt_ids:
        return False
    changed = False
    running_state = "enhancing" if job.get("stage") == "enhance" else "rendering"
    if any(prompt_id in running_ids for prompt_id in prompt_ids) and job["state"] not in {running_state, "cancelling"}:
        store.update_job(job["id"], state=running_state)
        changed = True
    if job.get("stage") == "enhance":
        try:
            history = await comfy.history(prompt_ids[0])
        except ServiceError:
            history = None
        if not history:
            return changed
        error = _history_error(history)
        if error:
            store.update_job(job["id"], state="failed", error=error, completed_at=utc_now())
            await finalize_job_lock(job["id"], job.get("lock_token"))
        else:
            await advance_enhanced_job(job, history)
        return True
    outputs = job_outputs(job)
    error = None
    for index, prompt_id in enumerate(prompt_ids):
        if index >= len(outputs) or outputs[index] is not None:
            continue
        try:
            history = await comfy.history(prompt_id)
        except ServiceError:
            history = None
        if not history:
            continue
        error = _history_error(history)
        if error:
            break
        output = output_from_history(history, settings.output_dir)
        outputs[index] = str(output.relative_to(settings.output_dir.resolve())) if output else ""
        changed = True
    first = next((path for path in outputs if path), None)
    done = sum(path is not None for path in outputs)
    if error:
        await cancel_prompts([pid for pid, path in zip(prompt_ids, outputs) if path is None])
        if len(outputs) > 1:
            error = f"Image {done + 1} of {len(outputs)}: {error}"
        store.update_job(job["id"], state="failed", error=error, completed_at=utc_now(), outputs_json=json.dumps(outputs), output_path=first)
        await finalize_job_lock(job["id"], job.get("lock_token"))
        return True
    if not changed:
        return False
    if done == len(outputs):
        store.update_job(job["id"], state="complete", progress=100, output_path=first, outputs_json=json.dumps(outputs), completed_at=utc_now())
        await finalize_job_lock(job["id"], job.get("lock_token"))
        log.info("Generation job %s completed (%d image(s))", job["id"][:8], len(outputs))
    else:
        store.update_job(job["id"], output_path=first, outputs_json=json.dumps(outputs),
                         progress=max(int(job.get("progress") or 0), int(done / len(outputs) * 100)))
    return True


_checkpoint_list: dict[str, list[str]] | None = None
_checkpoint_checked = 0.0
CHECKPOINT_POLL_SECONDS = 15


async def publish_checkpoints(force: bool = False) -> None:
    """Push the installed-model list to open pages when it changes (new downloads,
    files dropped into ComfyUI by hand, deletions)."""
    global _checkpoint_list, _checkpoint_checked
    _checkpoint_checked = time.monotonic()
    found = await checkpoints.installed(comfy)
    if force or found != _checkpoint_list:
        _checkpoint_list = found
        await events.publish("checkpoints", found)


async def monitor_jobs() -> None:
    while True:
        try:
            comfy_state = await comfy.status()
            if not comfy_state["online"]:
                asyncio.create_task(comfy.ensure_supervisor_started())
            running_ids = set(comfy_state.pop("running_ids", []))
            await events.publish("status", {"comfyui": comfy_state})
            if comfy_state["online"] and time.monotonic() - _checkpoint_checked >= CHECKPOINT_POLL_SECONDS:
                with contextlib.suppress(ServiceError):
                    await publish_checkpoints()
            changed = False
            for job in store.unfinished_jobs():
                if await check_job(job, running_ids):
                    changed = True
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
                if not job or job["state"] in {"cancelling", "cancelled", "complete", "failed"}:
                    continue
                if event_type == "progress" and job.get("stage") == "enhance":
                    continue
                if event_type == "progress":
                    maximum = float(data.get("max") or 0)
                    value = float(data.get("value") or 0)
                    step = value / maximum if maximum > 0 else 0
                    # Whole-job progress: finished images plus this image's step fraction.
                    images = job_outputs(job)
                    done = sum(path is not None for path in images)
                    reported = max(0, min(99, round((done + step) / max(1, len(images)) * 100)))
                    percentage = max(int(job.get("progress") or 0), reported)
                    store.update_job(job["id"], state="rendering", progress=percentage)
                    await publish_jobs()
                elif event_type in {"execution_error", "execution_interrupted"}:
                    images = job_outputs(job)
                    await cancel_prompts([pid for pid, path in zip(job_prompt_ids(job), images) if path is None and pid != prompt_id])
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    removed = store.delete_unfinished_jobs()
    log.info("Krea starting; workflow=%s; comfy_url=%s; removed_unfinished_jobs=%d", settings.workflow_path.name, settings.comfy_url, removed)
    monitor = asyncio.create_task(monitor_jobs())
    progress_monitor = asyncio.create_task(monitor_comfy_progress())
    try:
        yield
    finally:
        monitor.cancel()
        progress_monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor
        with contextlib.suppress(asyncio.CancelledError):
            await progress_monitor
        for job_id in tuple(heartbeat_tasks):
            await stop_heartbeat(job_id)
        for task in tuple(submission_tasks):
            task.cancel()
        if submission_tasks:
            await asyncio.gather(*tuple(submission_tasks), return_exceptions=True)
        removed = store.delete_unfinished_jobs()
        if removed:
            log.info("Removed %d unfinished generation job(s) during shutdown", removed)
        store.close()
        log.info("Krea stopped")


# docs_url/redoc_url/openapi_url disabled: defense-in-depth against the
# route/parameter map being exposed if ever reached directly (only the
# gateway is internet-facing in this stack).
app = FastAPI(title="Krea", version="1.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount(p("/static"), StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.middleware("http")
async def cache_gallery_previews(request: Request, call_next):
    response = await call_next(request)
    if (request.method == "GET" and response.status_code == 200
            and request.url.path.startswith(p("/static/lora-gallery/"))
            and Path(request.url.path).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".mp4"}):
        response.headers["Cache-Control"] = "public, max-age=604800"
    return response


def _asset_version() -> str:
    """Changes whenever a static file changes, so browsers (and Cloudflare) fetch
    the new app.js/app.css after a deploy instead of reusing a cached copy."""
    import hashlib
    digest = hashlib.sha256()
    for path in sorted((BASE_DIR / "static").rglob("*")):
        if path.is_file() and path.suffix in {".js", ".css", ".json"}:
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
    return templates.TemplateResponse(
        request, "index.html",
        {
            "app_version": ASSET_VERSION,
            "app_prefix": PREFIX,
            "lora_slots": {family: lora_slots_for(family) for family in ("krea", "qwen", "moss", "anima")},
            "lora_defaults": {"krea": [*DEFAULT_LORA_ENABLED, *([False] * (len(LORA_SLOTS) - len(DEFAULT_LORA_ENABLED)))],
                              "qwen": [*DEFAULT_QWEN_LORA_ENABLED, *([False] * (len(QWEN_LORA_SLOTS) - len(DEFAULT_QWEN_LORA_ENABLED)))],
                              "moss": [False] * len(MOSS_LORA_SLOTS), "anima": [False] * len(ANIMA_LORA_SLOTS)},
            "lora_info": {family: [LORA_INFO.get(name, {}) for name in lora_slots_for(family)]
                          for family in ("krea", "qwen", "moss", "anima")},
            "reference_slot_count": REFERENCE_SLOT_COUNT,
            "reference_defaults": DEFAULT_REFERENCE_STRENGTHS,
            "model_config": {
                "krea2": {"label": "Krea 2", "max_side": MAX_SIZE, "reference_slots": REFERENCE_SLOT_COUNT},
                MODEL_ANIMA: {"label": "Anima", "max_side": MAX_SIZE, "reference_slots": 0, "versions": {key: label for key, (_, label) in ANIMA_VERSIONS.items()}},
                MODEL_MOSS: {"label": "MOSS-Image", "max_side": MAX_SIZE, "reference_slots": 0, "steps": 50, "cfg": 3.0},
                MODEL_QWEN_IMAGE_21: {
                    "label": "Qwen Image 2.1", "max_side": QWEN_MAX_SIDE, "max_pixels": QWEN_MAX_PIXELS,
                    "reference_slots": QWEN_REFERENCE_SLOT_COUNT, "steps": QWEN_DEFAULT_STEPS, "cfg": QWEN_DEFAULT_CFG,
                },
            },
        },
    )


@app.get(p("/api/checkpoints"))
async def list_checkpoints() -> dict[str, list[str]]:
    try:
        return await checkpoints.installed(comfy)
    except ServiceError as exc:
        raise APIError(503, "checkpoints_unavailable", str(exc)) from exc


# Rocsy Models base-model names -> checkpoint family.
CHECKPOINT_BASES = {"qwen 2.1": "qwen_image_21", "krea 2": "krea2", "moss image": MODEL_MOSS}


@app.post(p("/api/internal/trainer/lora"))
async def register_trainer_lora(request: Request) -> dict[str, Any]:
    """The Trainer copied a finished LoRA into the custom LoRA folder: list it in
    the gallery under "Trainer made". Loopback + internal secret only."""
    secret = settings.queue_internal_secret
    if not secret or not hmac.compare_digest(request.headers.get("X-Queue-Internal-Secret", ""), secret):
        raise APIError(403, "forbidden", "Internal endpoint")
    body = await request.json()
    triggers = [word for word in body.get("triggers") or [] if isinstance(word, str)]
    try:
        item = await asyncio.to_thread(custom_loras.register, str(body.get("storage_name", "")), str(body.get("model", "")),
                                       str(body.get("label", "")), source="trainer",
                                       source_job_id=str(body.get("job_id", ""))[:64], triggers=triggers)
    except FileNotFoundError as exc:
        raise APIError(422, "lora_missing", "The LoRA file is not in the custom LoRA folder") from exc
    except ValueError as exc:
        raise APIError(422, "invalid_lora", str(exc)) from exc
    return {"item": item}


@app.post(p("/api/internal/checkpoints/register"))
async def register_downloaded_checkpoint(request: Request) -> dict[str, Any]:
    """Rocsy Models finished downloading a diffusion model: remember its base model
    (so files whose names don't say "Qwen Image 2.1" or "Krea 2" still list) and
    push the fresh list to open pages. Loopback + internal secret only."""
    secret = settings.queue_internal_secret
    if not secret or not hmac.compare_digest(request.headers.get("X-Queue-Internal-Secret", ""), secret):
        raise APIError(403, "forbidden", "Internal endpoint")
    body = await request.json()
    filename = str(body.get("filename", "")).replace("\\", "/").split("/")[-1]
    group = CHECKPOINT_BASES.get(str(body.get("base", "")).strip().casefold())
    if group and filename:
        await asyncio.to_thread(checkpoints.register, filename, group)
    try:
        await publish_checkpoints(force=True)
    except ServiceError as exc:
        return {"family": group, "listed": False, "warning": f"ComfyUI unreachable: {exc}"}
    listed = any(filename in names for names in (_checkpoint_list or {}).values())
    return {"family": group, "listed": listed}


@app.get(p("/api/health"))
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(p("/api/lora-gallery/shared"))
async def shared_lora_gallery() -> dict[str, Any]:
    url = os.getenv("BUNNY_FRAMEWORK_URL", "http://127.0.0.1:8700").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{url}/v1/lora-gallery/krea", headers={"X-Queue-Internal-Secret": settings.queue_internal_secret})
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        data = json.loads((BASE_DIR / "static" / "lora-gallery" / "index.json").read_text())
    for overlay in (custom_loras.gallery_index(), *(lora_catalog.index(family) for family in ("krea", "qwen", "moss", "anima"))):
        for filename, info in overlay.items():
            data[filename] = {**data.get(filename, {}), **info}
    return data


@app.get(p("/api/lora-gallery/custom"))
async def custom_lora_gallery() -> dict[str, Any]:
    return {"items": custom_loras.entries(), "index": custom_loras.gallery_index()}


@app.get(p("/api/lora-gallery/catalog/{model}"))
async def get_lora_catalog(model: str) -> dict[str, Any]:
    try:
        return {**lora_catalog.get(model), "index": lora_catalog.index(model)}
    except ValueError as exc:
        raise APIError(422, "invalid_model", str(exc)) from exc


@app.put(p("/api/lora-gallery/catalog/{model}"))
async def save_lora_catalog(model: str, request: Request) -> dict[str, Any]:
    try:
        return lora_catalog.save(model, await request.json())
    except (ValueError, TypeError) as exc:
        raise APIError(422, "invalid_catalog", str(exc)) from exc


@app.patch(p("/api/lora-gallery/catalog/{model}/item"))
async def edit_lora_catalog_item(model: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    try:
        return lora_catalog.edit(model, str(body.get("filename", "")), body)
    except (ValueError, TypeError) as exc:
        raise APIError(422, "invalid_lora", str(exc)) from exc


@app.post(p("/api/lora-gallery/upload"))
async def upload_custom_lora(model: str = Form(...), name: str = Form(...), file: UploadFile = File(...)) -> dict[str, Any]:
    if model not in {"krea", "qwen", "moss", "anima"} or not (file.filename or "").lower().endswith(".safetensors"):
        raise APIError(422, "invalid_lora", "Choose a Krea 2, Qwen-Image 2.1, MOSS-Image, or Anima .safetensors LoRA file")
    temporary = settings.data_dir / f"lora-upload-{uuid.uuid4().hex}.part"
    size = 0
    try:
        with temporary.open("wb") as stream:
            while chunk := await file.read(8 * 1024 * 1024):
                size += len(chunk)
                if size > custom_loras.MAX_LORA_BYTES:
                    raise APIError(413, "lora_too_large", "LoRA exceeds 6 GiB")
                stream.write(chunk)
        try:
            item = await asyncio.to_thread(custom_loras.install, temporary, model, name)
        except ValueError as exc:
            raise APIError(422, "invalid_lora", str(exc)) from exc
        return {"item": item}
    finally:
        temporary.unlink(missing_ok=True)
        await file.close()


_lora_chunk_locks: dict[str, asyncio.Lock] = {}


@app.post(p("/api/lora-gallery/chunk"))
async def upload_custom_lora_chunk(upload_id: str = Form(...), index: int = Form(...),
                                   total: int = Form(...), chunk: UploadFile = File(...)) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id) or not 1 <= total <= 768 or not 0 <= index < total:
        raise APIError(422, "invalid_chunk", "Invalid LoRA upload chunk")
    root = settings.data_dir / "custom-lora-chunks"
    if index == 0 and root.is_dir():
        for stale in root.iterdir():
            if stale.is_dir() and time.time() - stale.stat().st_mtime > 24 * 3600:
                shutil.rmtree(stale, ignore_errors=True)
    folder = root / upload_id
    folder.mkdir(parents=True, exist_ok=True)
    data = await chunk.read(8 * 1024 * 1024 + 1)
    await chunk.close()
    if not data or len(data) > 8 * 1024 * 1024:
        raise APIError(413, "invalid_chunk", "LoRA upload chunks must be 8 MiB or smaller")
    temporary = folder / f"{index:04d}.tmp"
    temporary.write_bytes(data)
    os.replace(temporary, folder / f"{index:04d}.part")
    return {"received": index}


@app.post(p("/api/lora-gallery/complete"))
async def finish_custom_lora_upload(request: Request) -> dict[str, Any]:
    data = await request.json()
    upload_id, total = str(data.get("upload_id", "")), data.get("total")
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id) or not isinstance(total, int) or not 1 <= total <= 768:
        raise APIError(422, "invalid_upload", "Invalid LoRA upload")
    if data.get("model") not in {"krea", "qwen", "moss", "anima"} or not str(data.get("name", "")).strip():
        raise APIError(422, "invalid_lora", "Choose a Krea 2, Qwen-Image 2.1, MOSS-Image, or Anima LoRA name")
    folder = settings.data_dir / "custom-lora-chunks" / upload_id
    lock = _lora_chunk_locks.setdefault(upload_id, asyncio.Lock())
    async with lock:
        parts = [folder / f"{index:04d}.part" for index in range(total)]
        if any(not part.is_file() for part in parts):
            raise APIError(409, "missing_chunks", "LoRA upload is incomplete")
        if sum(part.stat().st_size for part in parts) > custom_loras.MAX_LORA_BYTES:
            raise APIError(413, "lora_too_large", "LoRA exceeds 6 GiB")
        assembled = folder / "assembled.safetensors"
        try:
            def combine() -> None:
                with assembled.open("wb") as output:
                    for part in parts:
                        with part.open("rb") as source:
                            shutil.copyfileobj(source, output, 8 * 1024 * 1024)
            await asyncio.to_thread(combine)
            try:
                item = await asyncio.to_thread(custom_loras.install, assembled,
                    str(data.get("model", "")), str(data.get("name", "")))
            except ValueError as exc:
                raise APIError(422, "invalid_lora", str(exc)) from exc
            return {"item": item}
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            _lora_chunk_locks.pop(upload_id, None)


@app.patch(p("/api/lora-gallery/custom/{item_id}"))
async def update_custom_lora(item_id: str, request: Request) -> dict[str, Any]:
    try:
        item = custom_loras.update(item_id, await request.json())
        lora_catalog.initialize()
    except KeyError as exc:
        raise APIError(404, "lora_missing", "LoRA was not found") from exc
    except (ValueError, TypeError) as exc:
        raise APIError(422, "invalid_lora", str(exc)) from exc
    return {"item": item}


@app.delete(p("/api/lora-gallery/custom/{item_id}"))
async def delete_custom_lora(item_id: str) -> dict[str, Any]:
    try:
        item = await asyncio.to_thread(custom_loras.delete, item_id)
    except KeyError as exc:
        raise APIError(404, "lora_missing", "LoRA was not found") from exc
    return {"deleted": item["id"]}


@app.get(p("/api/lora-gallery/aliases/{model}"))
async def get_lora_aliases(model: str) -> list[dict[str, Any]]:
    try:
        return lora_aliases.get(model)
    except ValueError as exc:
        raise APIError(422, "invalid_model", str(exc)) from exc


@app.post(p("/api/lora-gallery/aliases/{model}"))
async def save_lora_aliases(model: str, request: Request) -> list[dict[str, Any]]:
    body = await request.json()
    try:
        return lora_aliases.save(model, str(body.get("filename", "")), str(body.get("target", "")), body.get("aliases", []))
    except (ValueError, TypeError) as exc:
        raise APIError(422, "invalid_alias", str(exc)) from exc


@app.delete(p("/api/lora-gallery/aliases/{model}"))
async def delete_lora_aliases(model: str, filename: str, target: str) -> list[dict[str, Any]]:
    try:
        return lora_aliases.delete(model, filename, target)
    except ValueError as exc:
        raise APIError(422, "invalid_alias", str(exc)) from exc


_lora_inspections: dict[str, dict[str, Any]] = {}
_lora_link_jobs: dict[str, dict[str, Any]] = {}
_lora_link_tasks: set[asyncio.Task[Any]] = set()
_lora_link_active: dict[str, asyncio.Task[Any]] = {}
_lora_link_limit = asyncio.Semaphore(2)


@app.post(p("/api/internal/lora-gallery/register"))
async def register_downloaded_lora(request: Request) -> dict[str, Any]:
    """Rocsy Models finished downloading a LoRA into this app's custom LoRA folder:
    add it to the gallery with the metadata it already has from Civitai / Hugging
    Face (name, triggers, strength, description, previews). Loopback + internal
    secret only; the file is never copied."""
    secret = settings.queue_internal_secret
    if not secret or not hmac.compare_digest(request.headers.get("X-Queue-Internal-Secret", ""), secret):
        raise APIError(403, "forbidden", "Internal endpoint")
    body = await request.json()
    model = str(body.get("model", ""))
    try:
        item = await asyncio.to_thread(custom_loras.register, str(body.get("storage_name", "")), model,
                                       str(body.get("label", "")), source_job_id=str(body.get("source_key", ""))[:200],
                                       triggers=[w for w in body.get("triggers") or [] if isinstance(w, str)])
        changes = {key: body[key] for key in ("description", "creator", "version", "base_model", "url", "strength_note")
                   if isinstance(body.get(key), str)}
        strength = body.get("default_strength")
        if isinstance(strength, (int, float)) and not isinstance(strength, bool) and 0 <= strength <= 2:
            changes["default_strength"] = float(strength)
        item = custom_loras.update(item["id"], changes)
    except ValueError as exc:
        raise APIError(422, "invalid_lora", str(exc)) from exc
    category = str(body.get("category") or "").strip()
    if category and category.casefold() != "uploaded":
        with contextlib.suppress(ValueError):
            lora_catalog.assign(model, item["filename"], category)
    warning = ""
    images = [entry for entry in body.get("previews") or [] if isinstance(entry, dict)]
    if images and not item.get("previews"):
        try:
            cached = await lora_previews.cache(item["id"], images)
            if cached:
                item = custom_loras.update(item["id"], {"previews": cached})
        except Exception:
            warning = "Added, but the preview media could not be saved"
    return {"item": item, "warning": warning}


@app.post(p("/api/lora-gallery/link/inspect"))
async def inspect_lora_link(request: Request) -> dict[str, Any]:
    body = await request.json()
    model, url, token = (str(body.get(key, "")).strip() for key in ("model", "url", "token"))
    if model not in {"krea", "qwen", "moss", "anima"}:
        raise APIError(422, "invalid_model", "Choose Krea 2, Qwen-Image 2.1, MOSS-Image, or Anima")
    try:
        result = await lora_links.inspect(url, model, token)
    except (ValueError, ServiceError, httpx.HTTPError) as exc:
        raise APIError(422, "invalid_link", str(exc)) from exc
    inspection_id = uuid.uuid4().hex
    _lora_inspections[inspection_id] = {"result": result, "model": model, "token": token,
                                       "created": asyncio.get_running_loop().time()}
    return {"id": inspection_id, "title": result["title"], "provider": result["provider"],
            "candidates": [{key: item.get(key) for key in ("id", "filename", "version", "base_model", "size_bytes")}
                           for item in result["candidates"]]}


async def _download_linked_lora(task_id: str, inspection: dict[str, Any], candidate: dict[str, Any] | None) -> None:
    task = _lora_link_jobs[task_id]
    result = inspection["result"]
    temporary = settings.data_dir / f"lora-link-{task_id}.part"
    installed_id = None
    try:
        async with _lora_link_limit:
            recommendation = None
            if candidate is None:
                task["stage"] = "Hermes is selecting a compatible LoRA"
                recommendation = await gallery_hermes.choose_link(inspection["model"], result)
                candidate = next(item for item in result["candidates"] if item["id"] == recommendation["candidate_id"])
            task["stage"] = "Downloading LoRA"
            def progress(amount: int, total: int | None) -> None:
                task.update(downloaded_bytes=amount, total_bytes=total or candidate.get("size_bytes") or 0)
            await lora_links.download(candidate, result["provider"], inspection["token"], temporary, progress)
            if task.get("cancel_requested"):
                raise custom_loras.InstallCancelled()
            task["stage"] = "Installing LoRA"
            source_key = "link:" + inspection["model"] + ":" + str(candidate.get("sha256") or candidate["download_url"])
            item = await asyncio.to_thread(custom_loras.install, temporary, inspection["model"],
                                           str(recommendation.get("name") or result["title"] or Path(candidate["filename"]).stem) if recommendation else result["title"] or Path(candidate["filename"]).stem,
                                           source="link", source_job_id=source_key,
                                           suggested_filename=candidate["filename"],
                                           triggers=[word for word in candidate.get("triggers", []) if isinstance(word, str)],
                                           cancel_check=lambda: bool(task.get("cancel_requested")))
            installed_id = item["id"]
            if task.get("cancel_requested"):
                raise custom_loras.InstallCancelled()
            changes = {"creator": result.get("creator", ""), "version": candidate.get("version", ""),
                       "base_model": candidate.get("base_model", ""), "description": result.get("context", "")[:10000],
                       "url": result.get("source_url", ""), "triggers": item.get("triggers", [])}
            if recommendation:
                changes["description"] = str(recommendation.get("description") or changes["description"])[:10000]
                changes["strength_note"] = str(recommendation.get("strength_note") or "")[:500]
                changes["triggers"] = [word for word in recommendation.get("triggers", []) if isinstance(word, str)][:12]
                strength = recommendation.get("recommended_strength")
                if isinstance(strength, (int, float)) and not isinstance(strength, bool) and 0 <= strength <= 2:
                    changes["default_strength"] = strength
            item = custom_loras.update(item["id"], changes)
            if recommendation and isinstance(recommendation.get("category"), str) and recommendation["category"].strip():
                category = recommendation["category"].strip()
                if category.casefold() != "uploaded":
                    lora_catalog.assign(inspection["model"], item["filename"], category)
            if result["provider"] == "civitai":
                try:
                    version_id = str(candidate["id"]).split(":", 1)[0]
                    version = next((entry for entry in result.get("versions", []) if str(entry.get("id")) == version_id), {})
                    previews = await lora_previews.cache(item["id"], version.get("images") or [], inspection["token"])
                    if previews:
                        item = custom_loras.update(item["id"], {"previews": previews})
                except Exception:
                    pass  # A preview failure does not discard a validated, installed LoRA.
            task.update(state="complete", stage="Added to gallery", item=item)
    except (asyncio.CancelledError, custom_loras.InstallCancelled):
        task.update(state="cancelled", stage="Download cancelled", error="")
    except Exception as exc:
        task.update(state="failed", stage="Failed", error=str(exc)[:500])
    finally:
        if installed_id and task["state"] != "complete":
            with contextlib.suppress(KeyError):
                await asyncio.to_thread(custom_loras.delete, installed_id)
        temporary.unlink(missing_ok=True)
        _lora_link_active.pop(task_id, None)


@app.post(p("/api/lora-gallery/link"), status_code=202)
async def start_lora_link(request: Request) -> dict[str, Any]:
    body = await request.json()
    inspection = _lora_inspections.pop(str(body.get("inspection_id", "")), None)
    if not inspection or asyncio.get_running_loop().time() - inspection["created"] > 1800:
        raise APIError(410, "link_expired", "Inspect the link again")
    auto = bool(body.get("auto", True))
    candidate = None if auto else next((item for item in inspection["result"]["candidates"]
                      if item["id"] == body.get("candidate_id")), None)
    if not auto and not candidate:
        raise APIError(422, "invalid_lora", "Select a listed LoRA file")
    task_id = uuid.uuid4().hex
    state = {"id": task_id, "state": "running", "stage": "Queued", "downloaded_bytes": 0,
             "total_bytes": candidate.get("size_bytes") or 0 if candidate else 0, "error": ""}
    _lora_link_jobs[task_id] = state
    worker = asyncio.create_task(_download_linked_lora(task_id, inspection, candidate))
    _lora_link_tasks.add(worker)
    _lora_link_active[task_id] = worker
    worker.add_done_callback(_lora_link_tasks.discard)
    return {"task": state}


@app.get(p("/api/lora-gallery/link/{task_id}"))
async def get_lora_link(task_id: str) -> dict[str, Any]:
    task = _lora_link_jobs.get(task_id)
    if not task:
        raise APIError(404, "link_missing", "LoRA download was not found")
    return {"task": task}


@app.post(p("/api/lora-gallery/link/{task_id}/cancel"))
async def cancel_lora_link(task_id: str) -> dict[str, Any]:
    task = _lora_link_jobs.get(task_id)
    if not task:
        raise APIError(404, "link_missing", "LoRA download was not found")
    if task["state"] == "running":
        task["cancel_requested"] = True
        worker = _lora_link_active.get(task_id)
        if worker and task.get("stage") != "Installing LoRA":
            worker.cancel()
    return {"task": task}


@app.get(p("/api/lora-gallery/media/{item_id}/{filename}"))
async def custom_lora_media(item_id: str, filename: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", item_id) or not re.fullmatch(r"[0-3]\.(?:jpg|mp4)", filename) or not custom_loras.get(item_id):
        raise APIError(404, "media_missing", "LoRA preview was not found")
    path = custom_loras.MEDIA_ROOT / item_id / filename
    if not path.is_file():
        raise APIError(404, "media_missing", "LoRA preview was not found")
    return FileResponse(path, media_type="video/mp4" if filename.endswith(".mp4") else "image/jpeg")


_gallery_tasks: dict[str, dict[str, Any]] = {}
_gallery_workers: set[asyncio.Task[Any]] = set()


async def _run_gallery_task(task_id: str, kind: str, model_or_id: str, url: str = "") -> None:
    task = _gallery_tasks[task_id]
    try:
        task["stage"] = "Hermes is reading LoRA details"
        if kind == "organize":
            index = await shared_lora_gallery()
            task["stage"] = "Hermes is organizing the gallery"
            task["proposal"] = await gallery_hermes.organize(model_or_id, index)
            task["stage"] = "Ready for review"
        else:
            task["stage"] = "Hermes is importing source details"
            task["item"] = await gallery_hermes.scrape(model_or_id, url)
            task["stage"] = "Imported"
        task["state"] = "complete"
    except Exception as exc:
        task.update(state="failed", stage="Failed", error=str(exc)[:500])


@app.post(p("/api/lora-gallery/catalog/{model}/organize"), status_code=202)
async def start_gallery_organize(model: str) -> dict[str, Any]:
    try:
        lora_catalog.get(model)
    except ValueError as exc:
        raise APIError(422, "invalid_model", str(exc)) from exc
    task_id = uuid.uuid4().hex
    task = {"id": task_id, "state": "running", "stage": "Queued", "error": ""}
    _gallery_tasks[task_id] = task
    worker = asyncio.create_task(_run_gallery_task(task_id, "organize", model))
    _gallery_workers.add(worker)
    worker.add_done_callback(_gallery_workers.discard)
    return {"task": task}


@app.post(p("/api/lora-gallery/custom/{item_id}/scrape"), status_code=202)
async def start_gallery_scrape(item_id: str, request: Request) -> dict[str, Any]:
    if not custom_loras.get(item_id):
        raise APIError(404, "lora_missing", "LoRA was not found")
    body = await request.json()
    url = str(body.get("url", "")).strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise APIError(422, "invalid_url", "Enter a public HTTP(S) source page URL")
    task_id = uuid.uuid4().hex
    task = {"id": task_id, "state": "running", "stage": "Queued", "error": ""}
    _gallery_tasks[task_id] = task
    worker = asyncio.create_task(_run_gallery_task(task_id, "scrape", item_id, url))
    _gallery_workers.add(worker)
    worker.add_done_callback(_gallery_workers.discard)
    return {"task": task}


@app.get(p("/api/lora-gallery/task/{task_id}"))
async def get_gallery_task(task_id: str) -> dict[str, Any]:
    task = _gallery_tasks.get(task_id)
    if not task:
        raise APIError(404, "task_missing", "Gallery task was not found")
    return {"task": task}


@app.post(p("/api/upload"))
async def upload_reference(file: UploadFile) -> dict[str, str]:
    if file.content_type not in ALLOWED_UPLOAD_TYPES:
        raise APIError(415, "unsupported_type", "Reference images must be PNG, JPEG, or WEBP.")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise APIError(413, "upload_too_large", "Reference images must be 20MB or smaller.")
    if not content:
        raise APIError(400, "empty_upload", "The uploaded file is empty.")
    try:
        stored_name = await comfy.upload_image(file.filename or "reference.png", content, file.content_type)
    except ServiceError as exc:
        raise APIError(502, "upload_failed", str(exc)) from exc
    return {"image": stored_name}


@app.get(p("/api/references/{name}/preview"))
async def reference_preview(name: str) -> Response:
    """Thumbnail source for an uploaded reference (it lives in ComfyUI's input folder)."""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise APIError(400, "invalid_name", "Invalid reference name.")
    try:
        content, content_type = await comfy.view_input(name)
    except ComfyHTTPError as exc:
        raise APIError(404 if exc.status_code == 404 else 502, "reference_unavailable", "That reference image is no longer available.") from exc
    except ServiceError as exc:
        raise APIError(502, "reference_unavailable", str(exc)) from exc
    return Response(content, media_type=content_type, headers={"Cache-Control": "private, max-age=86400"})


@app.post(p("/api/generate"), status_code=202)
async def generate(request: GenerationRequest) -> dict[str, Any]:
    if request.model == MODEL_MOSS and request.speed in MOSS_FAST_MODES:
        request = request.model_copy(update={"checkpoint": MOSS_FAST_CHECKPOINT, "negative_prompt": ""})
    return await create_generation(request)


async def create_generation(request: GenerationRequest, *, hidden: bool = False, under_lock: bool = False) -> dict[str, Any]:
    """hidden: a render made for another app (kept out of the gallery and home).
    under_lock: that app already holds the GPU queue slot, so skip the queue and submit now."""
    if request.loras:
        expanded_prompt = lora_aliases.expand(request.prompt, lora_family(request.model), request.loras)
        if expanded_prompt != request.prompt:
            request = GenerationRequest.model_validate({**request.model_dump(), "prompt": expanded_prompt})
    comfy_state = await comfy.status()
    if not comfy_state["online"]:
        raise APIError(
            503, "comfy_unavailable",
            f"ComfyUI is offline at {settings.comfy_url}. Start ComfyUI or correct KREA_COMFY_URL, then try again.",
        )
    if request.model != MODEL_ANIMA:
        try:
            checkpoint = checkpoints.resolve(request.model, request.checkpoint, await checkpoints.installed(comfy))
        except ValueError as exc:
            # The model's own default file isn't installed: build with it anyway, so the
            # missing-files check below can offer to fetch it with the VAE/encoders.
            checkpoint = request.checkpoint or checkpoints.DEFAULTS.get(request.model)
            if not checkpoint or checkpoint not in (checkpoints.DEFAULTS.get(request.model), MOSS_FAST_CHECKPOINT):
                raise APIError(422, "checkpoint_unavailable", str(exc)) from exc
        except ServiceError as exc:
            raise APIError(503, "checkpoints_unavailable", str(exc)) from exc
        request = request.model_copy(update={"checkpoint": checkpoint})
    try:
        workflows, seed, stage = build_workflows(settings, request)
    except GenerationError as exc:
        raise APIError(422 if exc.code == "workflow_incompatible" else 503, exc.code, str(exc)) from exc
    # The graphs as they'll be queued (with Advanced workflow overrides), checked before queueing.
    checked = [copy.deepcopy(workflow) for workflow in workflows]
    for workflow in checked:
        workflow_models.apply(request.model, workflow)
    missing = await missing_files(settings.comfy_url, checked)
    if missing["files"] or missing["nodes"]:
        raise APIError(409, MISSING_CODE, missing_message(missing), {"missing": missing, "studio": "Image"})

    width, height = display_size(request)
    job_id = uuid.uuid4().hex
    selected_model_label = (f"Anima · {ANIMA_VERSIONS[request.anima_version][1]}" if request.model == MODEL_ANIMA
                            else f"{MODEL_LABELS[request.model]} · {Path(request.checkpoint).stem}")
    label = f"{selected_model_label}{f' x{request.count}' if request.count > 1 else ''}: {request.prompt[:60]}"
    entry = None
    if not under_lock:
        entry = await lock.enqueue(app="Krea", job_id=job_id, label=label)
        if entry is None:
            raise APIError(503, "queue_unavailable", "Could not reach the GPU queue coordinator. Try again shortly.")
    item = {
        "id": job_id, "prompt": request.prompt, "seed": seed, "width": width, "height": height,
        "enhance": request.enhance, "state": "waiting_for_gpu" if entry else "preparing", "created_at": utc_now(),
        "request": request.model_dump(), "queue_entry_id": entry["entry_id"] if entry else None,
        "model": request.model, "stage": stage, "batch_count": request.count, "hidden": hidden,
    }
    store.create_job(item)
    if entry:
        pending_submissions[job_id] = {"workflows": workflows}
    else:
        # No token of its own: the caller's slot stays held (and is released) by the caller.
        task = asyncio.create_task(submit_job(job_id, workflows, None))
        submission_tasks.add(task)
        task.add_done_callback(submission_tasks.discard)
    if not hidden:
        await publish_jobs()
    return public_job(store.job(job_id) or item)


def _require_internal(http_request: Request) -> None:
    client_host = http_request.client.host if http_request.client else None
    secret = settings.queue_internal_secret
    if client_host not in ("127.0.0.1", "::1") or not secret or not hmac.compare_digest(
            http_request.headers.get("X-Queue-Internal-Secret", ""), secret):
        raise APIError(403, "forbidden", "Internal endpoint")


@app.post(p("/api/internal/generate"), status_code=202)
async def internal_generate(http_request: Request) -> dict[str, Any]:
    """Renders for other Rocsy services (e.g. the GPU tuning benchmark): body {"request": GenerationRequest, "under_lock": bool}.
    The job is hidden from the Image gallery; fetch it with /api/jobs/{id} and
    /api/outputs/{id}/download, then remove it (and its files) with DELETE /api/internal/jobs/{id}."""
    _require_internal(http_request)
    body = await http_request.json()
    try:
        request = GenerationRequest.model_validate(body.get("request") or {})
    except ValueError as exc:
        raise APIError(422, "invalid_request", str(exc)) from exc
    return await create_generation(request, hidden=True, under_lock=bool(body.get("under_lock")))


@app.delete(p("/api/internal/jobs/{job_id}"))
async def internal_delete_job(job_id: str, http_request: Request) -> dict[str, bool]:
    """Drop a hidden job and its output files once the caller has copied them."""
    _require_internal(http_request)
    item = store.job(job_id)
    if not item or not item.get("hidden"):
        raise APIError(404, "job_not_found", "Hidden generation not found.")
    if item["state"] in {"waiting_for_gpu", "preparing", "queued", "enhancing", "rendering", "cancelling"}:
        raise APIError(409, "job_active", "Cancel this generation before removing it.")
    root = settings.output_dir.resolve()
    for stored in job_outputs(item):
        if stored:
            path = (Path(stored) if Path(stored).is_absolute() else root / stored).resolve()
            if path.is_relative_to(root):
                try:
                    path.unlink(missing_ok=True)
                except OSError:     # the output mount is read-only here; the caller removes files on the host
                    pass
    store.delete_job(job_id)
    for thumb in THUMB_DIR.glob(f"{job_id}-*.webp"):
        thumb.unlink(missing_ok=True)
    return {"deleted": True}


@app.post(p("/api/internal/queue/proceed/{job_id}"))
async def queue_proceed(job_id: str, http_request: Request) -> dict[str, Any]:
    """Called by the bunny-lock queue coordinator when it's this job's turn.
    Loopback + shared-secret only - never routed through the gateway."""
    client_host = http_request.client.host if http_request.client else None
    if client_host not in ("127.0.0.1", "::1"):
        raise APIError(403, "forbidden", "This endpoint is only reachable from localhost.")
    if http_request.headers.get("X-Queue-Internal-Secret") != settings.queue_internal_secret:
        raise APIError(403, "forbidden", "Missing or invalid X-Queue-Internal-Secret.")
    body = await http_request.json()
    token = body.get("token")
    # The coordinator can grant the GPU while /api/generate is still setting this job up
    # (enqueue returns, then the job is saved and registered below it - H3 snapshots its
    # references in between). Wait for that instead of dropping the turn.
    for _ in range(300):
        item = store.job(job_id)
        if job_id in pending_submissions or (item and item["state"] != "waiting_for_gpu"):
            break
        await asyncio.sleep(0.1)
    item = store.job(job_id)
    pending = pending_submissions.pop(job_id, None)
    if not item or item["state"] != "waiting_for_gpu" or pending is None:
        if token:
            await lock.release(token)
        return {"accepted": False}
    store.update_job(job_id, state="preparing", lock_token=token)
    task = asyncio.create_task(submit_job(job_id, pending["workflows"], token))
    submission_tasks.add(task)
    task.add_done_callback(submission_tasks.discard)
    await publish_jobs()
    return {"accepted": True}


@app.post(p("/api/jobs/{job_id}/retry"), status_code=202)
async def retry_job(job_id: str) -> dict[str, Any]:
    """Queue the same settings again. A job that used a random seed gets a new one."""
    item = store.job(job_id)
    if not item:
        raise APIError(404, "job_not_found", "Generation not found.")
    try:
        request = GenerationRequest.model_validate(job_request(item))
    except ValueError as exc:
        raise APIError(422, "retry_unavailable", f"This generation's settings can't be reused: {exc}") from exc
    return await generate(request)


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


@app.get(p("/api/presets"))
async def presets() -> dict[str, dict[str, Any]]:
    """Every saved settings preset, {model: {name: settings}}, shared by all browsers."""
    return store.presets()


@app.put(p("/api/presets"))
async def save_preset(request: PresetRequest) -> dict[str, dict[str, Any]]:
    store.save_preset(request.model, request.name, request.settings)
    return store.presets()


@app.post(p("/api/presets/rename"))
async def rename_preset(request: PresetRenameRequest) -> dict[str, dict[str, Any]]:
    presets_ = store.presets().get(request.model, {})
    if request.name not in presets_:
        raise APIError(404, "preset_not_found", f'No preset named "{request.name}".')
    if request.new_name != request.name and request.new_name in presets_:
        raise APIError(409, "preset_exists", f'A preset named "{request.new_name}" already exists.')
    store.rename_preset(request.model, request.name, request.new_name)
    return store.presets()


@app.delete(p("/api/presets"))
async def delete_preset(model: str, name: str) -> dict[str, dict[str, Any]]:
    if not store.delete_preset(model, name):
        raise APIError(404, "preset_not_found", f'No preset named "{name}".')
    return store.presets()


@app.get(p("/api/jobs"))
async def jobs() -> list[dict[str, Any]]:
    return [public_job(item) for item in store.visible_jobs()]


@app.get(p("/api/jobs/{job_id}"))
async def job(job_id: str) -> dict[str, Any]:
    item = store.job(job_id)
    if not item:
        raise APIError(404, "job_not_found", "Generation not found.")
    return public_job(item)


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
    prompt_ids = job_prompt_ids(item)
    if not prompt_ids:
        log.info("Marked preparing generation job %s for cancellation", job_id[:8])
        return {"cancelled": True, "job": public_job(store.job(job_id))}
    # Every image not finished yet (the running one is interrupted, queued ones dropped);
    # finished images of a multi-image job stay on the card.
    outputs = job_outputs(item)
    pending = [pid for index, pid in enumerate(prompt_ids) if index >= len(outputs) or outputs[index] is None]
    prompt_id = pending[0] if pending else prompt_ids[0]
    try:
        cancelled = False
        for pending_id in pending:
            cancelled = await comfy.cancel(pending_id) or cancelled
    except ServiceError as exc:
        store.update_job(job_id, state=previous_state)
        await publish_jobs()
        raise APIError(502, "cancel_failed", str(exc)) from exc
    if not cancelled:
        store.update_job(job_id, state=previous_state)
        await publish_jobs()
        raise APIError(409, "cancel_too_late", "ComfyUI no longer reports this generation as queued or rendering.")
    store.update_job(job_id, state="cancelled", completed_at=utc_now())
    await finalize_job_lock(job_id, item.get("lock_token"))
    log.info("Cancelled generation job %s (%s)", job_id[:8], prompt_id[:8])
    await publish_jobs()
    return {"cancelled": True, "job": public_job(store.job(job_id))}


@app.delete(p("/api/jobs/{job_id}"))
async def delete_job(job_id: str) -> dict[str, bool]:
    item = store.job(job_id)
    if not item:
        raise APIError(404, "job_not_found", "Generation not found.")
    if item["state"] in {"waiting_for_gpu", "preparing", "queued", "enhancing", "rendering", "cancelling"}:
        raise APIError(409, "job_active", "Cancel this generation before removing it from history.")
    store.delete_job(job_id)
    for thumb in THUMB_DIR.glob(f"{job_id}-*.webp"):
        thumb.unlink(missing_ok=True)
    log.info("Removed generation job %s from Krea history", job_id[:8])
    await publish_jobs()
    return {"deleted": True}


def job_output(job_id: str, index: int = 0) -> tuple[dict[str, Any], Path]:
    """The job's image number `index` (0 = the first; single-image jobs only have that)."""
    item = store.job(job_id)
    outputs = job_outputs(item) if item else []
    if not item or index < 0 or index >= len(outputs) or not outputs[index]:
        raise APIError(404, "output_not_found", "This generation has no available output.")
    stored_path = Path(outputs[index])
    path = (stored_path if stored_path.is_absolute() else settings.output_dir / stored_path).resolve()
    try:
        path.relative_to(settings.output_dir.resolve())
    except ValueError as exc:
        raise APIError(403, "invalid_output", "The output path is outside the configured output directory.") from exc
    if not path.is_file():
        raise APIError(404, "output_not_found", "The generated output file is missing.")
    return item, path


# Versioned (?v=mtime) image URLs never change content, so the browser keeps them.
IMMUTABLE = "private, max-age=31536000, immutable"
THUMB_EDGE = 1024  # longest side; gallery cards are at most ~500 css px wide
THUMB_DIR = settings.data_dir / "thumbs"


def make_thumb(source: Path, target: Path) -> None:
    from PIL import Image
    with Image.open(source) as image:
        image.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".part")
        image.save(temporary, "WEBP", quality=82, method=4)  # keeps alpha (Qwen RGBA)
        temporary.replace(target)


@app.get(p("/api/outputs/{job_id}/stream"))
async def stream_output(job_id: str, index: int = 0, v: str = "") -> FileResponse:
    _, path = job_output(job_id, index)
    return FileResponse(
        path, media_type=mimetypes.guess_type(path.name)[0] or "image/png",
        headers={"Cache-Control": IMMUTABLE if v else "no-cache", "Content-Disposition": "inline"},
    )


@app.get(p("/api/outputs/{job_id}/thumb"))
async def thumb_output(job_id: str, index: int = 0, v: str = "") -> FileResponse:
    """A ~1024px WebP of the output for gallery cards (full PNGs are 1-1.5 MB),
    made once and kept in data/thumbs keyed by the file's mtime."""
    _, path = job_output(job_id, index)
    stat = path.stat()
    target = THUMB_DIR / f"{job_id}-{index}-{stat.st_mtime_ns}-{THUMB_EDGE}.webp"
    if not target.is_file():
        try:
            await asyncio.to_thread(make_thumb, path, target)
        except Exception as exc:  # unreadable/odd image: fall back to the original
            log.warning("Thumbnail failed for %s: %s", path.name, exc)
            return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "image/png",
                                headers={"Cache-Control": "no-cache"})
    return FileResponse(target, media_type="image/webp",
                        headers={"Cache-Control": IMMUTABLE if v else "no-cache"})


@app.get(p("/api/outputs/{job_id}/download"))
async def download_output(job_id: str, index: int = 0) -> FileResponse:
    _, path = job_output(job_id, index)
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@app.get(p("/api/outputs/{job_id}/zip"))
async def download_zip(job_id: str) -> Response:
    """Every finished image of a multi-image generation in one zip (stored, PNGs are compressed already)."""
    item = store.job(job_id)
    if not item:
        raise APIError(404, "job_not_found", "Generation not found.")
    buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for index, stored in enumerate(job_outputs(item)):
            if not stored:
                continue
            _, path = job_output(job_id, index)
            archive.write(path, f"{index + 1:02d}_seed{batch_seed(int(item['seed']), index)}_{path.name}")
            count += 1
    if not count:
        raise APIError(404, "output_not_found", "This generation has no finished images yet.")
    name = f"{(item.get('model') or 'krea')}_{job_id[:8]}_{count}_images.zip"
    return Response(buffer.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get(p("/api/events"))
async def event_stream() -> StreamingResponse:
    return StreamingResponse(events.stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _lora_note_text(text: str) -> str:
    """Gallery text for the Agentic planner: plain words, no HTML (as H3's _lora_note_text)."""
    return " ".join(re.sub(r"<[^>]*>", " ", text).split())


def _lora_note_trigger(trigger: str) -> str:
    """A trigger as plain words; placement notes ("start with: ...", "x (at the start)") are dropped."""
    if re.search(r"start with|\bprompt with\b", trigger, re.I):
        return ""
    return _lora_note_text(re.sub(r"\s*\(.*\)\s*$", "", trigger)).strip(" ,.:;")


@app.get(p("/api/agentic-loras/{model}"))
async def agentic_lora_options(model: str) -> dict[str, Any]:
    """Installed LoRAs for Hermes' "Look for relevant LoRAs", built here like H3's LoRA gallery note:
    one compact row per slot (180-char description, up to 4 plain triggers, gallery strength).
    The framework fetches this at session start; the page no longer sends the list."""
    if model not in ("krea", "qwen", "moss", "anima"):
        raise APIError(422, "invalid_model", "model must be krea, qwen, moss, or anima")
    try:
        gallery = json.loads((BASE_DIR / "static" / "lora-gallery" / "index.json").read_text())
    except (OSError, ValueError):
        gallery = {}
    for overlay in (custom_loras.gallery_index(), lora_catalog.index(model)):
        for filename, info in overlay.items():
            gallery[filename] = {**gallery.get(filename, {}), **info}
    assignments = lora_catalog.get(model).get("assignments", {})
    rows = []
    for slot, filename in enumerate(lora_slots_for(model)):
        info = LORA_INFO.get(filename, {})
        if info.get("deleted"):
            continue
        extra = gallery.get(filename) or {}
        triggers = [t for t in map(_lora_note_trigger, info.get("triggers") or extra.get("trained_words") or []) if t][:4]
        rows.append({"slot": slot, "label": info.get("label") or extra.get("name") or filename.removesuffix(".safetensors"),
                     "group": assignments.get(filename) or info.get("group") or "Other", "triggers": triggers,
                     "strength": info.get("strength", 1.0),
                     "description": _lora_note_text(str(extra.get("description") or ""))[:180]})
    return {"model": model, "loras": rows}


# Agentic sessions, Hermes settings, model guides and the Hermes terminal live in the
# internal framework (BunnyBytes/shared). The browser goes through this studio so the
# framework secret stays server-side; one route forwards the allowed paths.
_AGENTIC_PATH = re.compile(r"^(models|settings|guides|preview|sessions(/[a-f0-9]{32}(/(cancel|plan|steer|feedback))?)?)$")


@app.api_route(p("/api/agentic/{path:path}"), methods=["GET", "POST", "PUT", "DELETE"])
async def agentic_proxy(path: str, request: Request):
    if not _AGENTIC_PATH.match(path):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    base = os.getenv("BUNNY_FRAMEWORK_URL", "http://127.0.0.1:8700").rstrip("/")
    url = base + ("/v1/agentic/models" if path == "models" else "/v1/agentic/image/" + path)
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(request.method, url, content=body or None,
                headers={"X-Queue-Internal-Secret": (os.getenv("QUEUE_INTERNAL_SECRET") or os.getenv("KREA_QUEUE_INTERNAL_SECRET") or os.getenv("MUS_QUEUE_INTERNAL_SECRET") or ""), "Content-Type": "application/json"})
        return JSONResponse(response.json(), status_code=response.status_code)
    except (httpx.HTTPError, ValueError) as exc:
        return JSONResponse({"detail": f"Agentic framework unavailable: {type(exc).__name__}"}, status_code=503)


if __name__ == "__main__":
    uvicorn.run("app:app", host=settings.host, port=settings.port)
