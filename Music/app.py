from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
import httpx
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend.comfy import ComfyService, ServiceError, output_from_history
from backend.config import BASE_DIR, settings
from backend.events import EventBroker
from backend.generation import GenerationError, MINIMAX_MODEL_FILES, build_workflow
from backend.lock import LockClient
from backend.models import GenerationRequest
from backend.store import Store, utc_now
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
lock = LockClient(settings)
events = EventBroker()
client_id = f"music-web-{uuid.uuid4().hex}"

submission_tasks: set[asyncio.Task[None]] = set()
heartbeat_tasks: dict[str, tuple[asyncio.Task[None], asyncio.Event]] = {}
# Jobs enqueued with bunny-lock but not yet given the go-ahead to submit to
# ComfyUI - populated in generate(), consumed (and popped) in the internal
# proceed endpoint once the coordinator calls back.
pending_submissions: dict[str, dict[str, Any]] = {}

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


def public_job(item: dict[str, Any]) -> dict[str, Any]:
    output = bool(item.get("output_path"))
    request = json.loads(item.get("request_json") or "{}")
    state = item["state"]
    error = item.get("error")
    if state == "complete" and not output:
        # Older versions could label an unfinished prompt complete simply
        # because an early ComfyUI history record lacked status fields.
        state = "failed"
        error = error or "No audio file was saved for this earlier generation. Please generate the track again."
    return {
        "id": item["id"],
        "prompt_id": item.get("prompt_id"),
        "style": item["style"],
        "model": request.get("model", "yue2"),
        # The submitted settings: Retry loads them back into the form.
        "request": request,
        "lyrics": item["lyrics"],
        "seed": int(item["seed"]),
        "duration": item["duration"],
        "state": state,
        "created_at": item["created_at"],
        "started_at": item.get("started_at"),
        "completed_at": item.get("completed_at"),
        "error": error,
        "progress": int(item.get("progress") or 0),
        "output_url": p(f"/api/outputs/{item['id']}/stream") if output else None,
        "output_files": [item.get("output_path")] if item.get("output_path") else [],
        "download_url": p(f"/api/outputs/{item['id']}/download") if output else None,
    }


async def publish_jobs() -> None:
    await events.publish("jobs", [public_job(item) for item in store.jobs()])


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


async def submit_job(job_id: str, workflow: dict[str, Any], lock_token: str | None) -> None:
    try:
        prompt_id = await comfy.submit(workflow, client_id)
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
            start_heartbeat(job_id, lock_token)
            log.info("Submitted generation job %s as prompt %s", job_id[:8], prompt_id[:8])
    except Exception as exc:
        store.update_job(job_id, state="failed", error=str(exc), completed_at=utc_now())
        await finalize_job_lock(job_id, lock_token)
        log.exception("Generation submission failed for %s", job_id[:8])
    await publish_jobs()


async def monitor_jobs() -> None:
    while True:
        try:
            comfy_state = await comfy.status()
            if not comfy_state["online"]:
                asyncio.create_task(comfy.ensure_supervisor_started())
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    removed = store.delete_unfinished_jobs()
    log.info("Music starting; workflow=%s; comfy_url=%s; removed_unfinished_jobs=%d", settings.workflow_path.name, settings.comfy_url, removed)
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


@app.post(p("/api/generate"), status_code=202)
async def generate(request: GenerationRequest) -> dict[str, Any]:
    comfy_state = await comfy.status()
    if not comfy_state["online"]:
        raise APIError(
            503, "comfy_unavailable",
            f"ComfyUI is offline at {settings.comfy_url}. Start ComfyUI or correct MUS_COMFY_URL, then try again.",
        )
    try:
        workflow, seed = build_workflow(settings, request)
    except GenerationError as exc:
        raise APIError(422 if exc.code == "workflow_incompatible" else 503, exc.code, str(exc)) from exc
    workflow_models.apply(request.model, workflow)
    # Checked against ComfyUI before queueing; the Music UI offers to link or download what's missing.
    missing = await missing_files(settings.comfy_url, [workflow])
    if missing["files"] or missing["nodes"]:
        raise APIError(409, MISSING_CODE, missing_message(missing), {"missing": missing, "studio": "Music"})

    job_id = uuid.uuid4().hex
    label = f"Music: {request.style[:60]}"
    entry = await lock.enqueue(app="Mus", job_id=job_id, label=label)
    if entry is None:
        raise APIError(503, "queue_unavailable", "Could not reach the GPU queue coordinator. Try again shortly.")
    item = {
        "id": job_id, "style": request.style, "lyrics": request.lyrics, "seed": seed, "duration": request.duration,
        "state": "waiting_for_gpu", "created_at": utc_now(), "request": request.model_dump(),
        "queue_entry_id": entry["entry_id"],
    }
    store.create_job(item)
    pending_submissions[job_id] = {"workflow": workflow}
    await publish_jobs()
    return public_job(store.job(job_id) or item)


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
    # Into an empty queue the coordinator grants the GPU while /api/generate is
    # still between its enqueue and saving the job. Wait for that instead of
    # handing the turn back (the job then sat in waiting_for_gpu forever).
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
    task = asyncio.create_task(submit_job(job_id, pending["workflow"], token))
    submission_tasks.add(task)
    task.add_done_callback(submission_tasks.discard)
    await publish_jobs()
    return {"accepted": True}


@app.get(p("/api/jobs"))
async def jobs() -> list[dict[str, Any]]:
    return [public_job(item) for item in store.jobs()]


@app.get(p("/api/jobs/{job_id}"))
async def job(job_id: str) -> dict[str, Any]:
    item = store.job(job_id)
    if not item:
        raise APIError(404, "job_not_found", "Generation not found.")
    return public_job(item)


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
    if item["state"] in {"waiting_for_gpu", "preparing", "queued", "rendering", "cancelling"}:
        raise APIError(409, "job_active", "Cancel this generation before removing it from history.")
    store.delete_job(job_id)
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
_AGENTIC_PATH = re.compile(r"^(models|settings|guides|preview|sessions(/[a-f0-9]{32}(/(cancel|plan|steer))?)?)$")


@app.api_route(p("/api/agentic/{path:path}"), methods=["GET", "POST", "PUT", "DELETE"])
async def agentic_proxy(path: str, request: Request):
    if not _AGENTIC_PATH.match(path):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    base = os.getenv("BUNNY_FRAMEWORK_URL", "http://127.0.0.1:8700").rstrip("/")
    url = base + ("/v1/agentic/models" if path == "models" else "/v1/agentic/music/" + path)
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(request.method, url, content=body or None,
                headers={"X-Queue-Internal-Secret": settings.queue_internal_secret, "Content-Type": "application/json"})
        return JSONResponse(response.json(), status_code=response.status_code)
    except (httpx.HTTPError, ValueError) as exc:
        return JSONResponse({"detail": f"Agentic framework unavailable: {type(exc).__name__}"}, status_code=503)


if __name__ == "__main__":
    uvicorn.run("app:app", host=settings.host, port=settings.port)
