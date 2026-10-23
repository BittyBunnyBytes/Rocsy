"""Reference-voice cleanup for new characters, the same effects as BunnyVoice's
Voice Lab: pick a 1-15 s clip, remove music, suppress noise, normalize speech
and focus on voice frequencies.

Music removal runs on the GPU only (never a CPU fallback): the clip is written
to ComfyUI's input/VoiceRefs and ComfyUI's BunnyRemoveMusic node runs
~/AI/AudioSeparator/separate.py in "voices" mode (BandIt Plus DnR, speech only).
The caller holds the shared GPU queue token while that runs. Everything else
is ffmpeg audio filtering.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from pathlib import Path

import httpx

COMFY = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
SUPERVISOR = os.getenv("COMFY_SUPERVISOR_URL", "http://127.0.0.1:8083").rstrip("/")
COMFY_INPUT = Path(os.getenv("COMFY_VOICE_INPUT_DIR", "/comfy-input/VoiceRefs"))  # = ComfyUI input/VoiceRefs
COMFY_SEPARATED = Path(os.getenv("COMFY_SEPARATED_DIR", "/comfy-sep"))           # = ComfyUI output/postedit-sep
MAX_CLIP = 15.0
MIN_CLIP = 1.0
KEEP_SECONDS = 6 * 3600


class CleanupError(Exception):
    """A user-facing failure (bad file, separation failed)."""


async def _run(*args: str, timeout: float = 120) -> str:
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        raise CleanupError("Audio processing took too long")
    if process.returncode:
        raise CleanupError(err.decode(errors="replace").strip()[-300:] or "Audio processing failed")
    return out.decode(errors="replace")


async def probe_duration(path: Path) -> float:
    try:
        out = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=nw=1:nk=1", str(path), timeout=30)
        duration = float(out.strip())
    except (CleanupError, ValueError):
        raise CleanupError("That file has no audio that could be read")
    if duration < MIN_CLIP:
        raise CleanupError("The recording is shorter than one second")
    return duration


def prune(folder: Path) -> None:
    """Drop uploads and cleaned clips nobody saved within six hours."""
    cutoff = time.time() - KEEP_SECONDS
    for path in folder.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def clip_window(source_duration: float, start: float, length: float) -> tuple[float, float]:
    start = max(0.0, min(float(start), max(0.0, source_duration - MIN_CLIP)))
    length = min(MAX_CLIP, max(MIN_CLIP, float(length)), source_duration - start)
    return start, length


async def select_clip(source: Path, start: float, length: float, out: Path) -> None:
    """The chosen window as 44.1 kHz stereo float WAV (what the separator expects)."""
    await _run("ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(source),
               "-t", f"{length:.3f}", "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_f32le", str(out))


async def _comfy_online(client: httpx.AsyncClient) -> bool:
    try:
        return (await client.get(f"{COMFY}/system_stats", timeout=5)).status_code == 200
    except httpx.HTTPError:
        return False


async def separate_voice(clip: Path, on_stage=lambda stage: None) -> Path:
    """Speech-only stem of `clip` from the GPU separator, via ComfyUI. Returns
    a temp file in COMFY_SEPARATED; the caller deletes it."""
    name = f"voice-sep-{uuid.uuid4().hex}"
    COMFY_INPUT.mkdir(parents=True, exist_ok=True)
    staged = COMFY_INPUT / f"{name}.wav"
    output = COMFY_SEPARATED / f"{name}.wav"
    shutil.copyfile(clip, staged)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if not await _comfy_online(client):
                on_stage("Starting ComfyUI for music removal…")
                try:
                    await client.post(f"{SUPERVISOR}/start")  # wake only; never switches a running profile
                except httpx.HTTPError:
                    pass
                for _ in range(60):
                    await asyncio.sleep(2)
                    if await _comfy_online(client):
                        break
                else:
                    raise CleanupError("ComfyUI is offline, and music removal runs on the GPU through it")
            on_stage("Removing music on the GPU…")
            response = await client.post(f"{COMFY}/prompt", json={
                "client_id": f"voice-{name}",
                "prompt": {"1": {"class_type": "BunnyRemoveMusic", "inputs": {
                    "audio_file": f"VoiceRefs/{staged.name}", "mode": "voices", "output_name": name}}}})
            if response.status_code != 200:
                raise CleanupError(f"ComfyUI refused the music-removal job: {response.text[-300:]}")
            prompt_id = response.json()["prompt_id"]
            lost = finished_without_file = 0
            for _ in range(900):  # 30 min, mostly queued behind other GPU work
                await asyncio.sleep(2)
                try:
                    history = (await client.get(f"{COMFY}/history/{prompt_id}")).json().get(prompt_id)
                except (httpx.HTTPError, ValueError):
                    history = None
                status = (history or {}).get("status") or {}
                if status.get("status_str") == "error":
                    message = next((m[1].get("exception_message") for m in status.get("messages", [])
                                    if m[0] == "execution_error"), "")
                    raise CleanupError(f"Music removal failed on the GPU: {str(message)[-300:]}")
                if status.get("completed"):
                    if output.is_file():
                        return output
                    finished_without_file += 1
                    if finished_without_file >= 5:
                        raise CleanupError("Music removal finished but wrote no audio")
                elif not history:
                    try:
                        queue = (await client.get(f"{COMFY}/queue")).json()
                        ids = {item[1] for item in queue.get("queue_running", []) + queue.get("queue_pending", [])}
                    except (httpx.HTTPError, ValueError):
                        ids = set()
                    lost = 0 if prompt_id in ids else lost + 1
                    if lost >= 5:
                        raise CleanupError("ComfyUI lost the music-removal job (it likely restarted). Try again.")
            raise CleanupError("Music removal timed out")
    finally:
        staged.unlink(missing_ok=True)


async def finish(selected: Path, voice: Path | None, music: float, noise: float, normalize: bool,
                 focus: bool, out: Path) -> None:
    """Blend the voice stem with the original (music = 0..1, 1 = voice only),
    then the speech filters, to the 44.1 kHz mono 16-bit WAV cloning wants."""
    inputs = ["-i", str(selected)]
    chain: list[str] = []
    if voice is not None and music > 0:
        inputs += ["-i", str(voice)]
        if music >= 1:
            graph = "[1:a]anull[mix]"
        else:
            graph = (f"[0:a]volume={1 - music:.3f}[o];[1:a]volume={music:.3f}[v];"
                     "[o][v]amix=inputs=2:duration=first:normalize=0[mix]")
    else:
        graph = "[0:a]anull[mix]"
    if focus:
        chain += ["highpass=f=70", "lowpass=f=12000"]
    if noise > 0:
        chain.append(f"afftdn=nr={4 + noise * 36:.2f}:nf=-45:tn=1:gs=5")
    if normalize:
        chain.append("speechnorm=e=3:c=3:r=0.0005:f=0.0005")
    graph += f";[mix]{','.join(chain) or 'anull'}[out]"
    await _run("ffmpeg", "-nostdin", "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "[out]",
               "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(out))
