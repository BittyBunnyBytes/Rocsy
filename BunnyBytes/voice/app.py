"""Rocsy Voice: local Qwen and dedicated host speech engines.

All model paths are local. A missing checkpoint is reported, never downloaded.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from array import array
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import reference
import settings as voice_settings

PREFIX = "/Voice"
DATA = Path(os.getenv("VOICE_DATA_DIR", "/data"))
MODEL = Path(os.getenv("QWEN_TTS_MODEL_DIR", "/models/qwen/Qwen3-TTS-12Hz-1.7B-Base"))
VOICES = Path(os.getenv("VOICE_VOICES_DIR", "/data/voices"))
WORKER = os.getenv("VOICE_WORKER_URL", "http://127.0.0.1:7860").rstrip("/")
WORKER_SECRET = os.getenv("VOICE_WORKER_SECRET") or os.getenv("QUEUE_INTERNAL_SECRET") or ""
PORT = int(os.getenv("VOICE_PORT", "8950"))
QUEUE = os.getenv("VOICE_QUEUE_URL", "http://127.0.0.1:8600").rstrip("/")
QUEUE_SECRET = (os.getenv("QUEUE_INTERNAL_SECRET") or os.getenv("KREA_QUEUE_INTERNAL_SECRET") or os.getenv("MUS_QUEUE_INTERNAL_SECRET") or "")
DATA.mkdir(parents=True, exist_ok=True)
(DATA / "outputs").mkdir(exist_ok=True)
VOICES.mkdir(parents=True, exist_ok=True)
REFS = DATA / "refs"  # uploaded recordings + cleaned clips for new characters (kept 6 h)
REFS.mkdir(exist_ok=True)

app = FastAPI(title="BunnyBytes Voice", docs_url=None, redoc_url=None, openapi_url=None)
app.mount(f"{PREFIX}/static", StaticFiles(directory=Path(__file__).parent / "static"), name="voice-static")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
model_lock = threading.Lock()
qwen_model = None
pending: dict[str, Generate] = {}
started: set[str] = set()


class Generate(BaseModel):
    text: str = Field(min_length=1)  # both Qwen and the dedicated engines split long scripts
    engine: str
    voice_id: str
    language: str = "English"
    settings: dict = Field(default_factory=dict)


def _qwen_installed() -> bool:
    return (MODEL / "config.json").is_file() and (MODEL / "model.safetensors").is_file()


def _voice_profiles() -> list[dict]:
    """Every Rocsy character with reference audio, for the Studio picker and
    the Character gallery (portrait / emoji, category, summary, transcript)."""
    result = []
    if not VOICES.is_dir():
        return result
    import json
    for path in sorted(VOICES.glob("*/profile.json")):
        try:
            profile = json.loads(path.read_text())
            audio = path.parent / "reference.wav"
            if not audio.is_file():
                continue
            transcript = str(profile.get("transcript") or profile.get("reference_text") or "").strip()
            portrait = _portrait_path(path.parent, profile)
            result.append({
                "id": path.parent.name, "name": str(profile.get("name") or path.parent.name),
                "category": str(profile.get("category") or "").strip()[:40],
                "summary": str(profile.get("summary") or "").strip()[:400],
                "icon": str(profile.get("icon") or "").strip()[:8],
                "portrait": f"{PREFIX}/api/voices/{path.parent.name}/portrait?v={int(portrait.stat().st_mtime)}"
                            if portrait else "",
                "reference": f"{PREFIX}/api/voices/{path.parent.name}/reference",
                "duration": float(profile.get("reference_duration") or 0) or None,
                "transcript": bool(transcript), "transcript_text": transcript[:600],
                "created": audio.stat().st_mtime,
            })
        except (OSError, ValueError, TypeError):
            continue
    return result


def _portrait_path(folder: Path, profile: dict) -> Path | None:
    name = str(profile.get("icon_file") or "")
    if not re.fullmatch(r"[\w.-]+\.(webp|png|jpe?g)", name):
        return None
    path = folder / name
    return path if path.is_file() else None


def _voice_dir(voice_id: str) -> Path:
    """A saved character's folder, only for ids the profile scan returns."""
    if not re.fullmatch(r"[\w-]+", voice_id) or voice_id not in {voice["id"] for voice in _voice_profiles()}:
        raise HTTPException(404, "Saved voice not found")
    return VOICES / voice_id


async def _worker_state() -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(f"{WORKER}/health", headers={"X-Voice-Worker-Secret": WORKER_SECRET})
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError):
        return None


@app.get("/health")
@app.get(f"{PREFIX}/api/health")
async def health():
    return {"ok": True, "qwen_installed": _qwen_installed(),
            "worker_online": await _worker_state() is not None}


@app.get(f"{PREFIX}/api/showcase")
async def showcase():
    """Character portraits for the home page's Voice card backdrop."""
    return {"pictures": [voice["portrait"] for voice in _voice_profiles() if voice["portrait"]]}


@app.get(f"{PREFIX}/api/state")
async def state():
    worker = await _worker_state()
    worker_engines = {item["id"]: item for item in (worker or {}).get("engines", [])}
    installed = _qwen_installed()
    return {"engines": [
        {"id": "qwen3", "name": "Qwen3-TTS 1.7B Base", "ready": installed,
         "detail": "Voice cloning · 10 languages" if installed else "Model weights not installed"},
        *[{"id": key, "name": name, "ready": bool(worker_engines.get(key, {}).get("ready")),
           "detail": "Local Rocsy engine" if worker_engines.get(key, {}).get("ready")
           else ("Model files are missing" if worker else "Voice engine worker is offline")}
          for key, name in (("indextts2", "IndexTTS2"), ("fish_s2", "Fish Audio S2 Pro"),
                            ("fish_v1", "Fish Audio 1.5"))]],
        "voices": _voice_profiles(), "jobs": list(jobs.values())[-30:],
        "outputs": [{"id": path.stem, "audio": f"{PREFIX}/api/audio/{path.stem}",
                     "created": path.stat().st_mtime, **_output_meta(path)}
                    for path in sorted((DATA / "outputs").glob("*.wav"),
                                       key=lambda path: path.stat().st_mtime, reverse=True)[:60]
                    if re.fullmatch(r"[0-9a-f]{32}", path.stem)]}


@app.get(f"{PREFIX}/api/settings")
async def get_settings():
    return voice_settings.response()


@app.put(f"{PREFIX}/api/settings/{{engine}}")
async def save_settings(engine: str, changes: dict):
    try:
        return {"engine": engine, "values": voice_settings.save_engine(engine, changes)}
    except KeyError as exc:
        raise HTTPException(404, "Unknown voice engine") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete(f"{PREFIX}/api/settings/{{engine}}")
async def reset_settings(engine: str):
    try:
        return {"engine": engine, "values": voice_settings.reset_engine(engine)}
    except KeyError as exc:
        raise HTTPException(404, "Unknown voice engine") from exc


QWEN_CHUNK_CHARS = 400   # one Qwen pass per ~400 characters: stable voice, bounded VRAM
QWEN_CHUNK_MIN = 80
QWEN_CHUNK_GAP = 0.25    # seconds of silence between chunks


def _split_text(text: str, max_chars: int = QWEN_CHUNK_CHARS, min_chars: int = QWEN_CHUNK_MIN) -> list[str]:
    """Split a script at sentence/line boundaries (long sentences at words) without
    dropping text. Same packing as BunnyVoice's engines._split_text."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    units = [part.strip() for part in re.split(r"(?<=[.!?…])\s+|(?<=[。！？])|\n+", text) if part.strip()]
    pieces: list[str] = []
    for unit in units:
        if len(unit) <= max_chars:
            pieces.append(unit)
            continue
        words = unit.split()
        if len(words) == 1:  # no spaces (e.g. CJK): hard-cut
            pieces.extend(unit[i:i + max_chars] for i in range(0, len(unit), max_chars))
            continue
        chunk = ""
        for word in words:
            candidate = f"{chunk} {word}".strip()
            if chunk and len(candidate) > max_chars:
                pieces.append(chunk)
                chunk = word
            else:
                chunk = candidate
        if chunk:
            pieces.append(chunk)
    join = lambda left, right: left + ("" if left[-1] in "。！？" else " ") + right
    packed: list[str] = []
    for piece in pieces:
        candidate = join(packed[-1], piece) if packed else piece
        if packed and len(candidate) <= max_chars:
            packed[-1] = candidate
        else:
            packed.append(piece)
    if len(packed) > 1 and len(packed[-1]) < min_chars:
        combined = join(packed[-2], packed[-1])
        if len(combined) <= int(max_chars * 1.25):
            packed[-2:] = [combined]
    return packed or [text]


def _save_meta(job_id: str, payload: Generate):
    """What the Generations card shows for a finished file (script, character, engine)."""
    import json
    meta = {"text": payload.text, "voice_id": payload.voice_id, "engine": payload.engine,
            "language": payload.language, "settings": payload.settings}
    (DATA / "outputs" / f"{job_id}.json").write_text(json.dumps(meta))


def _output_meta(path: Path) -> dict:
    import json
    try:
        return json.loads(path.with_suffix(".json").read_text())
    except (OSError, ValueError):
        return {}


def _set_job(job_id: str, **fields):
    with jobs_lock:
        jobs[job_id].update(fields)


def _run_qwen(job_id: str, payload: Generate):
    global qwen_model
    try:
        import json
        import numpy as np
        import soundfile as sf
        import torch
        from qwen_tts import Qwen3TTSModel

        profile_dir = VOICES / payload.voice_id
        if profile_dir.name != payload.voice_id or not (profile_dir / "reference.wav").is_file():
            raise ValueError("Saved voice reference is missing")
        profile = json.loads((profile_dir / "profile.json").read_text())
        ref_text = str(profile.get("transcript") or profile.get("reference_text") or "").strip()
        with model_lock:
            if qwen_model is None:
                if not _qwen_installed():
                    raise RuntimeError("Qwen3-TTS weights are not installed")
                kwargs = {"device_map": "cuda:0" if torch.cuda.is_available() else "cpu",
                          "dtype": torch.bfloat16 if torch.cuda.is_available() else torch.float32,
                          "attn_implementation": "sdpa"}
                qwen_model = Qwen3TTSModel.from_pretrained(str(MODEL), **kwargs)
            chunks = _split_text(payload.text, max_chars=payload.settings.get("chunk_chars", QWEN_CHUNK_CHARS))
            _set_job(job_id, state="running", chunk=0, chunks=len(chunks))
            # The reference is encoded once and reused for every chunk.
            prompt = qwen_model.create_voice_clone_prompt(
                ref_audio=str(profile_dir / "reference.wav"), ref_text=ref_text or None,
                x_vector_only_mode=not ref_text)
            parts, sample_rate = [], 24000
            for index, chunk in enumerate(chunks, 1):
                _set_job(job_id, chunk=index)
                sample_settings = {key: payload.settings[key] for key in
                                   ("temperature", "top_p", "top_k", "repetition_penalty")
                                   if key in payload.settings}
                waveforms, sample_rate = qwen_model.generate_voice_clone(
                    text=chunk, language=payload.language, voice_clone_prompt=prompt, **sample_settings)
                if parts:
                    gap = payload.settings.get("chunk_gap_ms", int(QWEN_CHUNK_GAP * 1000)) / 1000
                    parts.append(np.zeros(int(sample_rate * gap), dtype=np.asarray(waveforms[0]).dtype))
                parts.append(np.asarray(waveforms[0]).reshape(-1))
            output = DATA / "outputs" / f"{job_id}.wav"
            sf.write(output, np.concatenate(parts), sample_rate)
            _save_meta(job_id, payload)
        _set_job(job_id, state="complete", audio=f"{PREFIX}/api/audio/{job_id}")
    except Exception as exc:
        _set_job(job_id, state="failed", error=str(exc)[:500])


async def _run_worker(job_id: str, payload: Generate):
    try:
        # The worker executes inside the shared GPU queue slot owned by this job.
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=1800 + len(payload.text))) as client:
            response = await client.post(f"{WORKER}/generate", json={
                "text": payload.text, "engine": payload.engine, "voice_id": payload.voice_id,
                "settings": payload.settings},
                headers={"X-Voice-Worker-Secret": WORKER_SECRET})
            response.raise_for_status()
        (DATA / "outputs" / f"{job_id}.wav").write_bytes(response.content)
        _save_meta(job_id, payload)
        _set_job(job_id, state="complete", audio=f"{PREFIX}/api/audio/{job_id}")
    except Exception as exc:
        detail = exc.response.text[-500:] if isinstance(exc, httpx.HTTPStatusError) else str(exc)[:500]
        _set_job(job_id, state="failed", error=detail)


def _queue_post(path: str, body: dict):
    with httpx.Client(timeout=10) as client:
        response = client.post(f"{QUEUE}{path}", json=body,
                               headers={"X-Queue-Internal-Secret": QUEUE_SECRET})
        response.raise_for_status()
        return response.json()


def _heartbeat(token: str, stop: threading.Event):
    while not stop.wait(20):
        try:
            _queue_post("/heartbeat", {"token": token})
        except httpx.HTTPError:
            pass


def _run_qwen_queued(job_id: str, payload: Generate, token: str):
    global qwen_model
    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(token, stop), daemon=True).start()
    try:
        _run_qwen(job_id, payload)
    finally:
        stop.set()
        with model_lock:
            qwen_model = None
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
        try:
            _queue_post("/release", {"token": token})
        except httpx.HTTPError:
            pass


async def _run_worker_queued(job_id: str, payload: Generate, token: str):
    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(token, stop), daemon=True).start()
    try:
        await _run_worker(job_id, payload)
    finally:
        stop.set()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                await client.post(f"{WORKER}/eject", headers={"X-Voice-Worker-Secret": WORKER_SECRET})
        except httpx.HTTPError:
            pass
        try:
            await asyncio.to_thread(_queue_post, "/release", {"token": token})
        except httpx.HTTPError:
            pass


# Reference cleanup / transcription steps waiting for their GPU queue turn.
gpu_waiters: dict[str, asyncio.Future] = {}


class GpuSlot:
    """Hold the shared GPU queue for one reference step (music removal or
    transcription): enqueue, wait for the proceed callback, heartbeat, release."""

    def __init__(self, job_id: str, label: str):
        self.job_id, self.label, self.token, self.beat = job_id, label, None, None

    async def __aenter__(self):
        waiter = asyncio.get_running_loop().create_future()
        gpu_waiters[self.job_id] = waiter
        try:
            await asyncio.to_thread(_queue_post, "/enqueue", {"app": "Voice", "job_id": self.job_id, "label": self.label})
        except httpx.HTTPError as exc:
            gpu_waiters.pop(self.job_id, None)
            raise reference.CleanupError("The GPU queue is unavailable") from exc
        try:
            self.token = await waiter
        finally:
            gpu_waiters.pop(self.job_id, None)
        self.beat = asyncio.create_task(self._heartbeat())
        return self

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(20)
            try:
                await asyncio.to_thread(_queue_post, "/heartbeat", {"token": self.token})
            except httpx.HTTPError:
                pass

    async def __aexit__(self, *exc):
        if self.beat:
            self.beat.cancel()
        if self.token:
            try:
                await asyncio.to_thread(_queue_post, "/release", {"token": self.token})
            except httpx.HTTPError:
                pass


@app.post(f"{PREFIX}/api/internal/queue/proceed/{{job_id}}")
async def proceed(job_id: str, request: Request):
    if request.headers.get("X-Queue-Internal-Secret") != QUEUE_SECRET:
        raise HTTPException(403, "Forbidden")
    body = await request.json()
    token = body.get("token")
    waiter = gpu_waiters.get(job_id)
    if waiter is not None:
        if token and not waiter.done():
            waiter.set_result(token)
        return {"ok": True}
    with jobs_lock:
        payload = pending.get(job_id)
        if not payload or not token:
            raise HTTPException(404, "Unknown queued job")
        if job_id in started:
            return {"ok": True}
        started.add(job_id)
        jobs[job_id]["state"] = "running"
    if payload.engine == "qwen3":
        threading.Thread(target=_run_qwen_queued, args=(job_id, payload, token), daemon=True).start()
    else:
        asyncio.create_task(_run_worker_queued(job_id, payload, token))
    return {"ok": True}


# ---- new characters: upload -> clean (clip + effects) -> transcribe -> save ----

def _ref_path(ref_id: str, suffix: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", ref_id):
        raise HTTPException(404, "Recording not found")
    return REFS / f"{ref_id}{suffix}"


def _upload_source(upload_id: str) -> Path:
    path = next(iter(REFS.glob(f"{_ref_path(upload_id, '').name}.src*")), None)
    if not path:
        raise HTTPException(404, "That upload expired - choose the file again")
    return path


def _youtube_url(raw: str) -> str:
    """Accept only a YouTube video ID, then build our own URL for yt-dlp."""
    parsed = urlparse(raw.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.port or parsed.username or parsed.password:
        raise HTTPException(422, "Enter an HTTPS YouTube video link")
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        parts = parsed.path.strip("/").split("/")
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) == 2 and parts[0] in {"shorts", "live"}:
            video_id = parts[1]
        else:
            video_id = ""
    elif host == "youtu.be":
        video_id = parsed.path.strip("/")
    else:
        video_id = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(422, "Enter a link to one YouTube video")
    return f"https://www.youtube.com/watch?v={video_id}"


class YouTubeRequest(BaseModel):
    url: str


async def _run_youtube_import(job_id: str, url: str):
    template = str(REFS / f"{job_id}.src.%(ext)s")
    command = ["yt-dlp", "--no-config", "--no-playlist", "--format", "bestaudio/best",
               "--extract-audio", "--audio-format", "mp3", "--audio-quality", "2",
               "--max-filesize", "200M", "--match-filter", "duration <= 1800",
               "--socket-timeout", "20", "--retries", "2", "--fragment-retries", "2",
               "--print", "after_move:%(title)s",
               "--output", template, url]
    try:
        _set_job(job_id, state="running", stage="Downloading YouTube audio…")
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=360)
        if result.returncode:
            raise ValueError((result.stderr or result.stdout).strip().splitlines()[-1][:300]
                             if (result.stderr or result.stdout).strip() else "YouTube download failed")
        source = _upload_source(job_id)
        if source.stat().st_size > 200 * 1024 * 1024:
            raise ValueError("Recording exceeds 200 MB")
        duration = await reference.probe_duration(source)
        if not 1 <= duration <= 1800:
            raise ValueError("Choose a video between 1 second and 30 minutes")
        title = (result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "YouTube audio")[:100]
        _set_job(job_id, state="complete", stage="Ready", upload_id=job_id,
                 duration=round(duration, 3), name=title)
    except (subprocess.TimeoutExpired, OSError, ValueError, reference.CleanupError) as exc:
        for path in REFS.glob(f"{job_id}.src*"):
            path.unlink(missing_ok=True)
        _set_job(job_id, state="failed", stage="Failed", error=str(exc)[:300] or "YouTube download failed")


@app.post(f"{PREFIX}/api/reference/youtube")
async def reference_youtube(body: YouTubeRequest):
    url = _youtube_url(body.url)
    reference.prune(REFS)
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"id": job_id, "engine": "reference", "kind": "import", "state": "queued",
                        "stage": "Starting…", "created": time.time()}
    asyncio.create_task(_run_youtube_import(job_id, url))
    return jobs[job_id]


@app.get(f"{PREFIX}/api/reference/{{upload_id}}/source")
async def reference_source(upload_id: str):
    source = _upload_source(upload_id)
    return FileResponse(source, media_type="audio/mpeg" if source.suffix == ".mp3" else None,
                        headers={"Cache-Control": "private, no-store"})


@app.get(f"{PREFIX}/api/reference/{{upload_id}}/waveform")
async def reference_waveform(upload_id: str):
    source = _upload_source(upload_id)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(source),
               "-map", "0:a:0", "-ac", "1", "-ar", "400", "-f", "s16le", "pipe:1"]
    try:
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True, timeout=40)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise HTTPException(422, "Could not read the audio waveform") from exc
    if result.returncode:
        raise HTTPException(422, "Could not read the audio waveform")
    samples = array("h")
    samples.frombytes(result.stdout[:len(result.stdout) // 2 * 2])
    count = 1200
    step = max(1, (len(samples) + count - 1) // count)
    peaks = [max((abs(value) for value in samples[i:i + step]), default=0) for i in range(0, len(samples), step)]
    top = max(peaks, default=0) or 1
    return {"peaks": [round(value / top, 3) for value in peaks]}


@app.post(f"{PREFIX}/api/reference/upload")
async def reference_upload(audio: UploadFile = File(...)):
    reference.prune(REFS)
    upload_id = uuid.uuid4().hex
    suffix = re.sub(r"[^a-z0-9]", "", Path(audio.filename or "").suffix.lower())[:5]
    path = REFS / f"{upload_id}.src{'.' + suffix if suffix else ''}"
    size = 0
    with path.open("wb") as handle:
        while chunk := await audio.read(1024 * 1024):
            size += len(chunk)
            if size > 200 * 1024 * 1024:
                handle.close()
                path.unlink(missing_ok=True)
                raise HTTPException(413, "Recordings must be under 200 MB")
            handle.write(chunk)
    try:
        duration = await reference.probe_duration(path)
    except reference.CleanupError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc)) from exc
    return {"id": upload_id, "duration": round(duration, 3), "name": audio.filename or ""}


class CleanRequest(BaseModel):
    start: float = Field(0, ge=0)
    length: float = Field(15, ge=1, le=15)
    remove_music: float = Field(100, ge=0, le=100)  # % of the voice-only stem in the mix
    noise: float = Field(0, ge=0, le=100)
    normalize: bool = True
    focus: bool = True


async def _run_clean(job_id: str, source: Path, body: CleanRequest):
    selected = REFS / f"{job_id}.sel.wav"
    voice = None
    try:
        duration = await reference.probe_duration(source)
        start, length = reference.clip_window(duration, body.start, body.length)
        _set_job(job_id, state="running", stage="Cutting the clip…")
        await reference.select_clip(source, start, length, selected)
        if body.remove_music > 0:
            _set_job(job_id, state="queued", stage="Waiting for the GPU…")
            async with GpuSlot(job_id, "Voice: remove music"):
                _set_job(job_id, state="running", stage="Removing music on the GPU…")
                voice = await reference.separate_voice(selected, lambda stage: _set_job(job_id, stage=stage))
        _set_job(job_id, stage="Cleaning up the voice…")
        await reference.finish(selected, voice, body.remove_music / 100, body.noise / 100, body.normalize,
                               body.focus, _ref_path(job_id, ".wav"))
        _set_job(job_id, state="complete", stage="Ready", duration=round(length, 3), start=round(start, 3),
                 audio=f"{PREFIX}/api/reference/{job_id}/audio")
    except Exception as exc:  # noqa: BLE001 - reported on the job
        _set_job(job_id, state="failed", stage="Failed", error=str(exc)[:500] or "Cleanup failed")
    finally:
        selected.unlink(missing_ok=True)
        if voice:
            voice.unlink(missing_ok=True)


@app.post(f"{PREFIX}/api/reference/{{upload_id}}/clean")
async def reference_clean(upload_id: str, body: CleanRequest):
    source = _upload_source(upload_id)
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"id": job_id, "engine": "reference", "kind": "reference", "state": "queued",
                        "stage": "Starting…", "created": time.time()}
    asyncio.create_task(_run_clean(job_id, source, body))
    return jobs[job_id]


async def _run_transcribe(job_id: str, clip: Path):
    try:
        worker = await _worker_state()
        if not worker or not worker.get("transcribe_ready"):
            raise reference.CleanupError("Rocsy's local transcription worker is unavailable")
        _set_job(job_id, state="queued", stage="Waiting for the GPU…")
        async with GpuSlot(job_id, "Voice: transcribe"):
            _set_job(job_id, state="running", stage="Transcribing with Whisper…")
            async with httpx.AsyncClient(timeout=900) as client:
                response = await client.post(f"{WORKER}/transcribe",
                    headers={"X-Voice-Worker-Secret": WORKER_SECRET},
                    files={"audio": ("reference.wav", clip.read_bytes(), "audio/wav")})
        if response.status_code != 200:
            raise reference.CleanupError(f"Transcription failed: {response.text[-300:]}")
        text = str(response.json().get("text") or "").strip()
        _set_job(job_id, state="complete", stage="Ready", text=text)
    except Exception as exc:  # noqa: BLE001
        _set_job(job_id, state="failed", stage="Failed", error=str(exc)[:500] or "Transcription failed")


@app.post(f"{PREFIX}/api/reference/{{clip_id}}/transcribe")
async def reference_transcribe(clip_id: str):
    clip = _ref_path(clip_id, ".wav")
    if not clip.is_file():
        raise HTTPException(404, "Build the cleaned clip first")
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"id": job_id, "engine": "transcribe", "kind": "transcribe", "state": "queued",
                        "stage": "Starting…", "created": time.time()}
    asyncio.create_task(_run_transcribe(job_id, clip))
    return jobs[job_id]


@app.get(f"{PREFIX}/api/reference/{{clip_id}}/audio")
async def reference_audio(clip_id: str):
    clip = _ref_path(clip_id, ".wav")
    if not clip.is_file():
        raise HTTPException(404, "Clip not found")
    return FileResponse(clip, media_type="audio/wav", headers={"Cache-Control": "private, no-store"})


@app.post(f"{PREFIX}/api/reference/{{clip_id}}/save")
async def reference_save(clip_id: str, name: str = Form(...), transcript: str = Form(""),
                         category: str = Form("Uncategorized"), summary: str = Form(""), icon: str = Form("🎙️"),
                         icon_image: UploadFile | None = File(None)):
    """Save the cleaned clip as a Rocsy character."""
    clip = _ref_path(clip_id, ".wav")
    if not clip.is_file():
        raise HTTPException(404, "The cleaned clip expired - build it again")
    if not name.strip():
        raise HTTPException(422, "Enter a character name")
    duration = await reference.probe_duration(clip)
    result = await _save_character(clip, name, transcript, category, summary, icon, duration, icon_image)
    clip.unlink(missing_ok=True)
    return {"voice": result}


async def _save_character(clip: Path, name: str, transcript: str, category: str,
                          summary: str, icon: str, duration: float,
                          icon_image: UploadFile | None = None) -> dict:
    name = name.strip()
    if not name:
        raise HTTPException(422, "Enter a character name")
    slug = re.sub(r"[^\w-]+", "-", name.lower()).strip("-")[:60] or "voice"
    voice_id = f"{slug}-{uuid.uuid4().hex[:8]}"
    folder = VOICES / voice_id
    folder.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copyfile(clip, folder / "reference.wav")
        icon_file = ""
        if icon_image and icon_image.filename:
            suffix = Path(icon_image.filename).suffix.lower()
            if suffix not in {".webp", ".png", ".jpg", ".jpeg"}:
                raise HTTPException(422, "Portrait must be a WebP, PNG or JPEG image")
            image = await icon_image.read(6 * 1024 * 1024 + 1)
            if len(image) > 6 * 1024 * 1024:
                raise HTTPException(413, "Portrait must be under 6 MB")
            if image:
                icon_file = f"icon{suffix}"
                (folder / icon_file).write_bytes(image)
        profile = {"id": voice_id, "name": name, "transcript": transcript.strip(),
                   "category": category.strip() or "Uncategorized", "summary": summary.strip()[:500],
                   "icon": icon.strip()[:12] or "🎙️", "icon_file": icon_file,
                   "audio_file": "reference.wav", "reference_duration": round(duration, 3)}
        (folder / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
        return profile
    except BaseException:
        shutil.rmtree(folder)
        raise


@app.post(f"{PREFIX}/api/voices")
async def add_voice(name: str = Form(...), transcript: str = Form(""),
                    audio: UploadFile = File(...)):
    if not name.strip():
        raise HTTPException(422, "Enter a voice name")
    body = await audio.read(50 * 1024 * 1024 + 1)
    if len(body) > 50 * 1024 * 1024:
        raise HTTPException(413, "Reference audio must be under 50 MB")
    source = REFS / f"{uuid.uuid4().hex}{Path(audio.filename or '').suffix[:10]}"
    clean = REFS / f"{uuid.uuid4().hex}.wav"
    try:
        source.write_bytes(body)
        duration = await reference.probe_duration(source)
        length = min(15.0, duration)
        await reference.select_clip(source, 0, length, clean)
        return await _save_character(clean, name, transcript, "Uncategorized", "", "🎙️", length)
    finally:
        source.unlink(missing_ok=True)
        clean.unlink(missing_ok=True)


@app.get(f"{PREFIX}/api/voices/{{voice_id}}/portrait")
async def voice_portrait(voice_id: str):
    import json
    folder = _voice_dir(voice_id)
    portrait = _portrait_path(folder, json.loads((folder / "profile.json").read_text()))
    if not portrait:
        raise HTTPException(404, "No portrait")
    media = {"webp": "image/webp", "png": "image/png"}.get(portrait.suffix[1:].lower(), "image/jpeg")
    # ?v= is the file's mtime, so a cached copy can be kept.
    return FileResponse(portrait, media_type=media, headers={"Cache-Control": "private, max-age=604800"})


@app.get(f"{PREFIX}/api/voices/{{voice_id}}/reference")
async def voice_reference(voice_id: str):
    return FileResponse(_voice_dir(voice_id) / "reference.wav", media_type="audio/wav",
                        headers={"Cache-Control": "private, max-age=3600"})


@app.delete(f"{PREFIX}/api/voices/{{voice_id}}")
async def remove_voice(voice_id: str):
    folder = _voice_dir(voice_id)
    shutil.rmtree(folder)
    return {"deleted": True, "id": voice_id}


@app.post(f"{PREFIX}/api/generate")
async def generate(payload: Generate):
    if payload.engine not in {"qwen3", "indextts2", "fish_s2", "fish_v1"}:
        raise HTTPException(422, "Unknown engine")
    if payload.voice_id not in {voice["id"] for voice in _voice_profiles()}:
        raise HTTPException(422, "Choose a saved voice with reference audio")
    if payload.engine == "qwen3" and not _qwen_installed():
        raise HTTPException(503, "Qwen3-TTS weights are not installed")
    if payload.engine != "qwen3":
        worker = await _worker_state()
        if not worker:
            raise HTTPException(503, "Rocsy Voice engine worker is offline")
        if not next((item for item in worker.get("engines", []) if item["id"] == payload.engine), {}).get("ready"):
            raise HTTPException(503, "Selected engine model files are missing")
    # Capture settings now; a queued job must not change when the form is edited.
    payload.settings = voice_settings.get_engine(payload.engine)
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"id": job_id, "engine": payload.engine, "state": "queued", "created": time.time(),
                    "text": payload.text, "voice_id": payload.voice_id, "language": payload.language,
                    "settings": payload.settings}
    pending[job_id] = payload
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"{QUEUE}/enqueue", json={
                "app": "Voice", "job_id": job_id, "label": payload.engine},
                headers={"X-Queue-Internal-Secret": QUEUE_SECRET})
            response.raise_for_status()
            jobs[job_id]["queue_entry_id"] = response.json()["entry_id"]
    except httpx.HTTPError as exc:
        pending.pop(job_id, None)
        jobs.pop(job_id, None)
        raise HTTPException(503, "GPU queue unavailable") from exc
    return jobs[job_id]


@app.get(f"{PREFIX}/api/jobs/{{job_id}}")
async def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[job_id]


@app.post(f"{PREFIX}/api/jobs/{{job_id}}/cancel")
async def cancel_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["state"] != "queued" or job_id in started:
            raise HTTPException(409, "Only queued voice jobs can be cancelled")
        entry_id = job.get("queue_entry_id")
        job["state"] = "cancelled"
        pending.pop(job_id, None)
    if entry_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{QUEUE}/dequeue/{entry_id}",
                                  headers={"X-Queue-Internal-Secret": QUEUE_SECRET})
        except httpx.HTTPError:
            pass  # proceed sees no pending payload and cannot start the cancelled job
    return job


@app.get(f"{PREFIX}/api/audio/{{job_id}}")
async def audio(job_id: str):
    output = DATA / "outputs" / f"{job_id}.wav"
    if not re.fullmatch(r"[0-9a-f]{32}", job_id) or not output.is_file():
        raise HTTPException(404, "Audio not found")
    return FileResponse(output, media_type="audio/wav", filename=f"voice-{job_id[:8]}.wav",
                        content_disposition_type="inline")


@app.delete(f"{PREFIX}/api/audio/{{job_id}}")
async def delete_audio(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(404, "Audio not found")
    output = DATA / "outputs" / f"{job_id}.wav"
    with jobs_lock:
        job = jobs.get(job_id)
        if job and job["state"] in {"queued", "running"}:
            raise HTTPException(409, "Audio is still being generated")
        try:
            output.unlink()
        except FileNotFoundError as exc:
            raise HTTPException(404, "Audio not found") from exc
        output.with_suffix(".json").unlink(missing_ok=True)
        if job:
            job.pop("audio", None)
    return {"deleted": True, "id": job_id}


@app.get(PREFIX)
async def slash():
    return RedirectResponse(f"{PREFIX}/")


@app.get(f"{PREFIX}/", response_class=HTMLResponse)
async def page():
    return HTMLResponse((Path(__file__).parent / "index.html").read_text())

# Agentic sessions, Hermes settings, model guides and the Hermes terminal live in the
# internal framework (BunnyBytes/shared). The browser goes through this studio so the
# framework secret stays server-side; one route forwards the allowed paths.
_AGENTIC_PATH = re.compile(r"^(models|settings|guides|preview|sessions(/[a-f0-9]{32}(/(cancel|plan|steer))?)?)$")


@app.api_route(f"{PREFIX}/api/agentic/{{path:path}}", methods=["GET", "POST", "PUT", "DELETE"])
async def agentic_proxy(path: str, request: Request):
    if not _AGENTIC_PATH.match(path):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    base = os.getenv("BUNNY_FRAMEWORK_URL", "http://127.0.0.1:8700").rstrip("/")
    url = base + ("/v1/agentic/models" if path == "models" else "/v1/agentic/voice/" + path)
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(request.method, url, content=body or None,
                headers={"X-Queue-Internal-Secret": (os.getenv("QUEUE_INTERNAL_SECRET") or os.getenv("KREA_QUEUE_INTERNAL_SECRET") or os.getenv("MUS_QUEUE_INTERNAL_SECRET") or ""), "Content-Type": "application/json"})
        return JSONResponse(response.json(), status_code=response.status_code)
    except (httpx.HTTPError, ValueError) as exc:
        return JSONResponse({"detail": f"Agentic framework unavailable: {type(exc).__name__}"}, status_code=503)

