"""Internal BunnyBytes capability API. Never expose this port through the gateway."""
from __future__ import annotations

import asyncio
import hmac
import os
import json
import shutil
import tempfile
import urllib.request
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from operations import MediaError, probe, transform
import agentic

SECRET = os.environ.get("QUEUE_INTERNAL_SECRET", "")
if not SECRET:
    raise RuntimeError("QUEUE_INTERNAL_SECRET is required")

MAX_UPLOAD_BYTES = int(os.environ.get("FRAMEWORK_MAX_UPLOAD_MB", "250")) * 1024 * 1024
MAX_PARALLEL = max(1, int(os.environ.get("FRAMEWORK_MAX_PARALLEL", "2")))
SLOTS = asyncio.Semaphore(MAX_PARALLEL)
app = FastAPI(title="BunnyBytes internal capabilities", version="1.0.0", docs_url=None, redoc_url=None)

CAPABILITIES = {
    "version": "1.0.0",
    "authentication": "X-Queue-Internal-Secret header",
    "base_url": "http://127.0.0.1:8700",
    "upload_limit_bytes": MAX_UPLOAD_BYTES,
    "services": {
        "queue": {"base_url": os.environ.get("LOCK_UPSTREAM", "http://127.0.0.1:8600"),
                  "purpose": "GPU job coordination", "entrypoints": ["/enqueue", "/heartbeat", "/release", "/dequeue/{entry_id}"]},
        "video": {"base_url": os.environ.get("VID_UPSTREAM", "http://127.0.0.1:8000") + "/Vid",
                  "purpose": "H3 video generation, specialized post-edit and reference tools", "entrypoints": ["/api/health", "/api/status"]},
        "music": {"base_url": os.environ.get("MUS_UPSTREAM", "http://127.0.0.1:8200") + "/Mus",
                  "purpose": "YuE2 and MiniMax Music 3 generation", "entrypoints": ["/api/health", "/api/generate", "/api/jobs"]},
        "voice": {"base_url": os.environ.get("VOICE_UPSTREAM", "http://127.0.0.1:8950") + "/Voice",
                  "purpose": "Text-to-speech generation", "entrypoints": ["/api/health", "/api/generate", "/api/jobs/{job_id}"]},
        "krea": {"base_url": os.environ.get("KREA_UPSTREAM", "http://127.0.0.1:8300") + "/Krea",
                 "purpose": "Krea image generation", "entrypoints": ["/api/health", "/api/generate", "/api/jobs"]},
        "connections": {"base_url": os.environ.get("CONNECTIONS_UPSTREAM", "http://127.0.0.1:8800") + "/Connections",
                        "purpose": "Telegram output watcher and Cloudflare tunnel status",
                        "entrypoints": ["/api/status", "/api/cloudflare", "/api/watcher/start", "/api/watcher/stop"]},
        "trainer": {"base_url": os.environ.get("TRAINER_UPSTREAM", "http://127.0.0.1:8500") + "/Trainer",
                    "purpose": "LoRA training and dataset-specific autotrim (optional Compose profile)",
                    "entrypoints": ["/api/health", "/api/jobs", "/api/datasets/{name}/autotrim-all"]},
    },
    "operations": {
        "lora.gallery": {"path": "/v1/lora-gallery/{studio}", "result": "Merged JSON catalog including installed and edited LoRAs", "studios": ["h3", "krea", "all"]},
        "lora.gallery_state": {"path": "/v1/lora-gallery/{studio}/state", "result": "Model catalogs, aliases and custom LoRAs", "studios": ["h3", "krea"]},
        "media.probe": {"path": "/v1/media/probe", "result": "JSON metadata", "fields": ["file"]},
        "media.trim": {"path": "/v1/media/trim", "result": "MP4 video or M4A audio", "fields": ["file", "start", "end"]},
        "media.crop": {"path": "/v1/media/crop", "result": "MP4 video or PNG image", "fields": ["file", "x", "y", "width", "height"],
                       "coordinates": "fractions of source width and height, 0 to 1"},
        "media.extract_audio": {"path": "/v1/media/extract-audio", "result": "M4A audio", "fields": ["file"]},
        "media.thumbnail": {"path": "/v1/media/thumbnail", "result": "JPEG image", "fields": ["file", "at"]},
        "agentic.sessions": {"path": "/v1/agentic/{studio}/sessions", "result": "Persistent planned, rendered and reviewed generation sessions", "studios": ["image", "music", "voice"],
                             "also": ["/v1/agentic/{studio}/settings", "/v1/agentic/{studio}/guides", "/v1/agentic/{studio}/preview",
                                      "/v1/agentic/{studio}/sessions/{id}/steer", "/v1/agentic/{studio}/sessions/{id}/feedback", "/v1/agentic/models"]},
    },
}

GALLERY_FILES = {
    "h3": Path(os.environ.get("FRAMEWORK_H3_LORA_GALLERY", "/catalogs/h3/index.json")),
    "krea": Path(os.environ.get("FRAMEWORK_KREA_LORA_GALLERY", "/catalogs/krea/index.json")),
}


def _studio_gallery_overrides(studio: str) -> list[dict]:
    upstream = _studio_upstream(studio)
    overlays = []
    for path in ["/api/lora-gallery/custom", *(
        f"/api/lora-gallery/catalog/{model}" for model in
        (("minimax_h3", "ltx_2_5") if studio == "h3" else ("krea", "qwen")))]:
        try:
            with urllib.request.urlopen(upstream + path, timeout=2) as response:
                data = json.load(response)
            if isinstance(data, dict) and isinstance(data.get("index"), dict):
                overlays.append(data["index"])
        except (OSError, ValueError):
            continue
    return overlays


def _studio_upstream(studio: str) -> str:
    if studio == "h3":
        return os.environ.get("VID_UPSTREAM", "http://127.0.0.1:8000").rstrip("/") + "/Vid"
    if studio == "krea":
        return os.environ.get("KREA_UPSTREAM", "http://127.0.0.1:8300").rstrip("/") + "/Krea"
    raise HTTPException(status_code=404, detail="Unknown studio")


@app.middleware("http")
async def reject_unauthorized_before_upload(request: Request, call_next):
    # Multipart parsing happens before route dependencies. Reject callers before
    # FastAPI spools their upload to disk, even on this loopback-only port.
    if request.url.path.startswith("/v1/") and not hmac.compare_digest(
        request.headers.get("x-queue-internal-secret", ""), SECRET
    ):
        return JSONResponse({"detail": "Invalid internal secret"}, status_code=403)
    return await call_next(request)


def require_internal(x_queue_internal_secret: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_queue_internal_secret, SECRET):
        raise HTTPException(status_code=403, detail="Invalid internal secret")


@app.post("/api/internal/queue/proceed/{turn_id}", dependencies=[Depends(require_internal)])
async def proceed_hermes_turn(turn_id: str, request: Request):
    turn = agentic.TURNS.get(turn_id)
    if not turn:
        raise HTTPException(404, "Unknown Hermes turn")
    body = await request.json()
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise HTTPException(422, "Missing queue token")
    if not turn.done():
        turn.set_result(token)
    return {"ok": True}


@app.post("/v1/agentic/{studio}/sessions", dependencies=[Depends(require_internal)], status_code=202)
async def start_agentic(studio: str, body: agentic.AgenticRequest):
    if studio not in agentic.STUDIOS:
        raise HTTPException(404, "Unknown studio")
    if not body.request.strip():
        raise HTTPException(422, "Enter a request")
    # lora_options from older pages is ignored (the framework fetches the LoRA list from the studio).
    if len(json.dumps({k: v for k, v in body.settings.items() if k != "lora_options"})) > 12000:
        raise HTTPException(422, "Settings are too large")
    try:
        return agentic.summary(agentic.start(studio, body))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/v1/agentic/{studio}/sessions", dependencies=[Depends(require_internal)])
async def list_agentic(studio: str, limit: int = 50):
    if studio not in agentic.STUDIOS:
        raise HTTPException(404, "Unknown studio")
    rows = [agentic.summary(item) for item in reversed(list(agentic.SESSIONS.values())) if item["studio"] == studio]
    return rows[:limit] if limit > 0 else rows


def _studio(studio: str) -> None:
    if studio not in agentic.STUDIOS:
        raise HTTPException(404, "Unknown studio")


def _session(studio: str, session_id: str) -> dict:
    item = agentic.SESSIONS.get(session_id)
    if not item or item["studio"] != studio:
        raise HTTPException(404, "Session not found")
    return item


@app.get("/v1/agentic/{studio}/settings", dependencies=[Depends(require_internal)])
async def agentic_settings(studio: str):
    _studio(studio)
    return agentic.public_settings(studio)


@app.put("/v1/agentic/{studio}/settings", dependencies=[Depends(require_internal)])
async def update_agentic_settings(studio: str, request: Request):
    _studio(studio)
    changes = await request.json()
    if not isinstance(changes, dict):
        raise HTTPException(422, "Settings must be a JSON object")
    try:
        return agentic.update_settings(studio, changes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/v1/agentic/{studio}/guides", dependencies=[Depends(require_internal)])
async def agentic_guides(studio: str):
    _studio(studio)
    return agentic.guides(studio)


@app.post("/v1/agentic/{studio}/preview", dependencies=[Depends(require_internal)])
async def agentic_preview(studio: str, body: agentic.PreviewRequest):
    _studio(studio)
    return await agentic.preview(studio, body)


@app.get("/v1/agentic/models", dependencies=[Depends(require_internal)])
async def agentic_models():
    try:
        async with agentic.httpx.AsyncClient(timeout=8) as client:
            response = await client.get(f"{agentic.LLAMA}/v1/models", headers={"Authorization": f"Bearer {agentic.KEY}"} if agentic.KEY else {})
            response.raise_for_status()
        return {"default": agentic.MODEL, "models": [item["id"] for item in response.json().get("data", []) if isinstance(item, dict) and isinstance(item.get("id"), str)]}
    except (agentic.httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "Llama model list is unavailable") from exc


@app.get("/v1/agentic/{studio}/sessions/{session_id}", dependencies=[Depends(require_internal)])
async def get_agentic(studio: str, session_id: str):
    item = agentic.SESSIONS.get(session_id)
    if not item or item["studio"] != studio:
        raise HTTPException(404, "Session not found")
    return item


@app.post("/v1/agentic/{studio}/sessions/{session_id}/cancel", dependencies=[Depends(require_internal)])
async def cancel_agentic(studio: str, session_id: str):
    item = _session(studio, session_id)
    await _voice_cancel_guard(item)
    agentic.cancel(item)
    return agentic.summary(item)


async def _voice_cancel_guard(item: dict) -> None:
    # Voice's TTS engines can't stop mid-render; only a queued voice job can be cancelled.
    if item["studio"] == "voice" and item.get("job_ids") and item.get("state") in agentic.ACTIVE:
        try:
            voice_job = await agentic.studio_call("voice", "GET", f"/api/jobs/{item['job_ids'][-1]}")
        except (ValueError, agentic.httpx.HTTPError):
            raise HTTPException(503, "Voice job status is unavailable") from None
        if voice_job.get("state") != "queued":
            raise HTTPException(409, "Running voice jobs cannot be stopped; wait for this render to finish")


@app.post("/v1/agentic/{studio}/sessions/{session_id}/steer", dependencies=[Depends(require_internal)])
async def steer_agentic(studio: str, session_id: str, body: agentic.Steer):
    item = _session(studio, session_id)
    if body.text.strip().lower() == "/stop":
        await _voice_cancel_guard(item)
    try:
        return agentic.steer(item, body.text)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.delete("/v1/agentic/{studio}/sessions/{session_id}", dependencies=[Depends(require_internal)])
async def delete_agentic(studio: str, session_id: str):
    item = _session(studio, session_id)
    try:
        agentic.delete(item)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.post("/v1/agentic/{studio}/sessions/{session_id}/plan", dependencies=[Depends(require_internal)])
async def decide_agentic_plan(studio: str, session_id: str, body: agentic.PlanDecision):
    item = agentic.SESSIONS.get(session_id)
    if not item or item["studio"] != studio:
        raise HTTPException(404, "Session not found")
    try:
        return agentic.summary(agentic.decide_plan(item, body))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/v1/agentic/{studio}/sessions/{session_id}/feedback", dependencies=[Depends(require_internal)])
async def agentic_output_feedback(studio: str, session_id: str, body: agentic.OutputFeedback):
    item = _session(studio, session_id)
    try:
        return agentic.summary(agentic.decide_output(item, body))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/v1/lora-gallery/{studio}/state", dependencies=[Depends(require_internal)])
async def lora_gallery_state(studio: str):
    """One discoverable view of both studios' editable gallery state."""
    upstream = _studio_upstream(studio)
    models = ("minimax_h3", "ltx_2_5") if studio == "h3" else ("krea", "qwen")
    def read(path: str) -> dict | list:
        with urllib.request.urlopen(upstream + path, timeout=5) as response:
            return json.load(response)
    try:
        catalogs = {model: await asyncio.to_thread(read, f"/api/lora-gallery/catalog/{model}") for model in models}
        custom = await asyncio.to_thread(read, "/api/lora-gallery/custom")
        aliases = (await asyncio.to_thread(read, "/api/trigger-aliases")) if studio == "h3" else {
            model: await asyncio.to_thread(read, f"/api/lora-gallery/aliases/{model}") for model in models}
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Studio gallery unavailable") from exc
    return {"studio": studio, "catalogs": catalogs, "custom": custom, "aliases": aliases}





@app.get("/v1/lora-gallery/{studio}", dependencies=[Depends(require_internal)])
async def lora_gallery(studio: str):
    """Shared LoRA metadata, including each studio's uploaded and edited entries."""
    if studio not in {*GALLERY_FILES, "all"}:
        raise HTTPException(status_code=404, detail="Unknown studio")
    catalogs = {}
    for name in ([studio] if studio != "all" else GALLERY_FILES):
        try:
            data = json.loads(GALLERY_FILES[name].read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=503, detail=f"{name} gallery unavailable") from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=503, detail=f"{name} gallery is invalid")
        for overlay in await asyncio.to_thread(_studio_gallery_overrides, name):
            for filename, info in overlay.items():
                if isinstance(info, dict):
                    data[filename] = {**data.get(filename, {}), **info}
        catalogs[name] = data
    return catalogs if studio == "all" else catalogs[studio]


async def save_upload(file: UploadFile, directory: Path) -> Path:
    source = directory / "source"
    total = 0
    try:
        with source.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Upload exceeds configured size limit")
                target.write(chunk)
    finally:
        await file.close()
    if not total:
        raise HTTPException(status_code=422, detail="Empty upload")
    return source


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/capabilities", dependencies=[Depends(require_internal)])
async def capabilities():
    return CAPABILITIES


@app.post("/v1/media/probe", dependencies=[Depends(require_internal)])
async def probe_route(file: UploadFile = File(...)):
    async with SLOTS:
        with tempfile.TemporaryDirectory(prefix="bunny-media-") as scratch:
            source = await save_upload(file, Path(scratch))
            try:
                return await asyncio.to_thread(probe, source)
            except MediaError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc


async def process(file: UploadFile, operation: str, extension: str, content_type: str, **options):
    # FileResponse sends after the endpoint returns, so cleanup belongs to its background task.
    directory = Path(tempfile.mkdtemp(prefix="bunny-media-"))
    try:
        async with SLOTS:
            source = await save_upload(file, directory)
            output = directory / f"result{extension}"
            await asyncio.to_thread(transform, source, output, operation, **options)
        return FileResponse(output, media_type=content_type, filename=output.name,
                            background=BackgroundTask(shutil.rmtree, directory, ignore_errors=True))
    except MediaError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


@app.post("/v1/media/trim", dependencies=[Depends(require_internal)])
async def trim_route(file: UploadFile = File(...), start: float = Form(...), end: float = Form(...)):
    # Audio-only input is returned as M4A; video input as MP4. The container
    # format is selected after probing, before invoking ffmpeg.
    async with SLOTS:
        directory = Path(tempfile.mkdtemp(prefix="bunny-media-"))
        try:
            source = await save_upload(file, directory)
            info = await asyncio.to_thread(probe, source)
            has_video = any(stream.get("codec_type") == "video" for stream in info["streams"])
            extension = ".mp4" if has_video else ".m4a"
            output = directory / f"result{extension}"
            await asyncio.to_thread(transform, source, output, "trim", start=start, end=end)
            return FileResponse(output, media_type="video/mp4" if has_video else "audio/mp4",
                                filename=output.name,
                                background=BackgroundTask(shutil.rmtree, directory, ignore_errors=True))
        except MediaError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise


@app.post("/v1/media/crop", dependencies=[Depends(require_internal)])
async def crop_route(file: UploadFile = File(...), x: float = Form(...), y: float = Form(...),
                     width: float = Form(...), height: float = Form(...)):
    async with SLOTS:
        directory = Path(tempfile.mkdtemp(prefix="bunny-media-"))
        try:
            source = await save_upload(file, directory)
            info = await asyncio.to_thread(probe, source)
            is_image = info["duration"] == 0
            output = directory / ("result.png" if is_image else "result.mp4")
            await asyncio.to_thread(transform, source, output, "crop", x=x, y=y, width=width, height=height)
            return FileResponse(output, media_type="image/png" if is_image else "video/mp4",
                                filename=output.name,
                                background=BackgroundTask(shutil.rmtree, directory, ignore_errors=True))
        except MediaError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise


@app.post("/v1/media/extract-audio", dependencies=[Depends(require_internal)])
async def extract_audio_route(file: UploadFile = File(...)):
    return await process(file, "extract-audio", ".m4a", "audio/mp4")


@app.post("/v1/media/thumbnail", dependencies=[Depends(require_internal)])
async def thumbnail_route(file: UploadFile = File(...), at: float = Form(0)):
    return await process(file, "thumbnail", ".jpg", "image/jpeg", at=at)
