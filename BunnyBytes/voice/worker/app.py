"""Rocsy-owned host worker for the three local speech engines and Whisper.

The Docker Voice service owns jobs, saved voices and the GPU queue. This
loopback worker only runs the installed engine environments on the host.
"""
from __future__ import annotations

import hmac
import json
import os
import subprocess
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from worker.rocsy_voice.config_store import ROOT, get_voice, load_config
from worker.rocsy_voice.engines import ENGINE_NAMES, generate_script, model_file_statuses, unload_models

SECRET = os.getenv("VOICE_WORKER_SECRET") or os.getenv("QUEUE_INTERNAL_SECRET") or ""
app = FastAPI(title="Rocsy Voice worker", docs_url=None, redoc_url=None, openapi_url=None)


def authorize(x_voice_worker_secret: str = Header(default="")) -> None:
    if not SECRET or not hmac.compare_digest(x_voice_worker_secret, SECRET):
        raise HTTPException(403, "Forbidden")


class Generate(BaseModel):
    engine: str
    voice_id: str
    text: str = Field(min_length=1)
    settings: dict = Field(default_factory=dict)


@app.get("/health", dependencies=[Depends(authorize)])
def health():
    return {"ok": True, "engines": model_file_statuses(),
            "transcribe_ready": (ROOT / "models/legacy/WhisperLargeV3Turbo/large-v3-turbo.pt").is_file()}


@app.post("/generate", dependencies=[Depends(authorize)])
def generate(body: Generate):
    if body.engine not in ENGINE_NAMES:
        raise HTTPException(422, "Unknown engine")
    if not next((item for item in model_file_statuses() if item["id"] == body.engine), {}).get("ready"):
        raise HTTPException(503, f"{ENGINE_NAMES[body.engine]} is not installed")
    if not get_voice(body.voice_id):
        raise HTTPException(404, "Saved voice not found")
    try:
        config = load_config()
        for key, value in body.settings.items():
            if key in config["segments"]:
                config["segments"][key] = value
            elif key in config["engines"][body.engine]:
                config["engines"][body.engine][key] = value
        output = generate_script(body.text, body.engine, body.voice_id, "gpu", config, lambda *_: None)
        try:
            return Response(output.read_bytes(), media_type="audio/wav")
        finally:
            output.unlink(missing_ok=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(500, str(exc)[-1000:]) from exc


@app.post("/transcribe", dependencies=[Depends(authorize)])
def transcribe(audio: UploadFile = File(...)):
    checkpoint = ROOT / "models/legacy/WhisperLargeV3Turbo/large-v3-turbo.pt"
    if not checkpoint.is_file():
        raise HTTPException(503, "Whisper checkpoint is not installed")
    with tempfile.TemporaryDirectory(prefix="rocsy-voice-transcribe-") as folder:
        clip = Path(folder) / "reference.wav"
        with clip.open("wb") as target:
            while chunk := audio.file.read(1024 * 1024):
                if target.tell() + len(chunk) > 50 * 1024 * 1024:
                    raise HTTPException(413, "Reference audio must be under 50 MB")
                target.write(chunk)
        command = [str(ROOT / "models/legacy/IndexTTS2/.venv/bin/python"),
                   str(ROOT / "worker/rocsy_voice/transcribe_reference.py"), str(clip)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(504, "Transcription timed out") from exc
    if result.returncode:
        raise HTTPException(500, result.stderr[-500:] or "Transcription failed")
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise HTTPException(500, "Transcription returned no result") from exc


@app.post("/eject", dependencies=[Depends(authorize)])
def eject():
    return unload_models()
