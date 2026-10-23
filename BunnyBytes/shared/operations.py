"""Reusable media operations. No web framework or service-specific paths."""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

MAX_SECONDS = 3600.0
PROCESS_TIMEOUT = 600


class MediaError(ValueError):
    pass


def _run(args: list[str], timeout: int = PROCESS_TIMEOUT) -> bytes:
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise MediaError("Media processing timed out") from exc
    except OSError as exc:
        raise MediaError(f"Media tool unavailable: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()[-800:]
        raise MediaError(detail or "Media processing failed")
    return result.stdout


def probe(source: Path) -> dict:
    raw = _run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size:stream=codec_type,codec_name,width,height,duration",
        "-of", "json", str(source),
    ], timeout=30)
    try:
        data = json.loads(raw)
        streams = data.get("streams") or []
        duration = float((data.get("format") or {}).get("duration") or 0)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("invalid duration")
        return {
            "duration": duration,
            "size": int((data.get("format") or {}).get("size") or source.stat().st_size),
            "streams": [{key: stream[key] for key in ("codec_type", "codec_name", "width", "height", "duration") if key in stream}
                        for stream in streams],
        }
    except (ValueError, TypeError, KeyError) as exc:
        raise MediaError("Could not read media metadata") from exc


def _stream(info: dict, kind: str) -> dict | None:
    return next((stream for stream in info["streams"] if stream.get("codec_type") == kind), None)


def _window(start: float, end: float, duration: float) -> tuple[float, float]:
    if not all(math.isfinite(value) for value in (start, end)):
        raise MediaError("Trim times must be finite numbers")
    if start < 0 or end <= start or end - start > MAX_SECONDS:
        raise MediaError(f"Trim must select between 0 and {MAX_SECONDS:g} seconds")
    if duration > 0 and end > duration + 0.05:
        raise MediaError("Trim end exceeds the source duration")
    return start, end


def _crop_rect(x: float, y: float, width: float, height: float, source_width: int, source_height: int) -> tuple[int, int, int, int]:
    if not all(math.isfinite(value) for value in (x, y, width, height)):
        raise MediaError("Crop values must be finite numbers")
    if min(x, y) < 0 or width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
        raise MediaError("Crop must fit inside the frame; use fractions from 0 to 1")
    left = round(x * source_width)
    top = round(y * source_height)
    right = min(source_width, round((x + width) * source_width))
    bottom = min(source_height, round((y + height) * source_height))
    if right <= left or bottom <= top:
        raise MediaError("Crop is smaller than one pixel")
    return left, top, right - left, bottom - top


def transform(source: Path, output: Path, operation: str, *, start: float = 0, end: float = 0,
              x: float = 0, y: float = 0, width: float = 1, height: float = 1, at: float = 0) -> None:
    """Run a named operation and write only to the caller's scratch output path."""
    info = probe(source)
    video = _stream(info, "video")
    audio = _stream(info, "audio")
    base = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
    if operation == "trim":
        _window(start, end, info["duration"])
        if video:
            args = [*base, "-ss", f"{start:.6f}", "-i", str(source), "-t", f"{end-start:.6f}",
                    "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-movflags", "+faststart", str(output)]
        elif audio:
            args = [*base, "-ss", f"{start:.6f}", "-i", str(source), "-t", f"{end-start:.6f}",
                    "-vn", "-c:a", "aac", str(output)]
        else:
            raise MediaError("Trim needs audio or video")
    elif operation == "crop":
        if not video:
            raise MediaError("Crop needs an image or video")
        rect = _crop_rect(x, y, width, height, int(video.get("width") or 0), int(video.get("height") or 0))
        left, top, crop_width, crop_height = rect
        # yuv420p requires even dimensions; scale after crop for video only.
        image = not info["duration"]
        vf = f"crop={crop_width}:{crop_height}:{left}:{top}"
        if image:
            args = [*base, "-i", str(source), "-vf", vf, "-frames:v", "1", str(output)]
        else:
            vf += ",scale=ceil(iw/2)*2:ceil(ih/2)*2"
            args = [*base, "-i", str(source), "-vf", vf, "-map", "0:v:0", "-map", "0:a:0?",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(output)]
    elif operation == "extract-audio":
        if not audio:
            raise MediaError("Source has no audio stream")
        args = [*base, "-i", str(source), "-vn", "-c:a", "aac", str(output)]
    elif operation == "thumbnail":
        if not video:
            raise MediaError("Thumbnail needs an image or video")
        if not math.isfinite(at) or at < 0 or (info["duration"] > 0 and at >= info["duration"]):
            raise MediaError("Thumbnail time must be inside the video")
        args = [*base, "-ss", f"{at:.6f}", "-i", str(source), "-frames:v", "1", "-q:v", "3", str(output)]
    else:
        raise MediaError(f"Unknown operation: {operation}")
    _run(args)
    if not output.is_file() or output.stat().st_size == 0:
        raise MediaError("Media processing produced no output")
