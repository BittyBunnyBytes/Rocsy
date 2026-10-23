"""Import one public YouTube or TikTok video into the Agent workspace."""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class MediaImportError(ValueError):
    pass


def platform(url: str) -> str:
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        raise MediaImportError("Give a public YouTube or TikTok video URL")
    if host in ("youtu.be", "youtube.com") or host.endswith(".youtube.com"):
        if host == "youtu.be":
            valid = bool(parsed.path.strip("/"))
        else:
            valid = ((parsed.path == "/watch" and bool(parse_qs(parsed.query).get("v")))
                     or bool(re.fullmatch(r"/(?:shorts|live|embed)/[^/]+/?", parsed.path)))
        if not valid:
            raise MediaImportError("Give a link to one YouTube video, Short, or live recording")
        return "youtube"
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        if host not in ("vm.tiktok.com", "vt.tiktok.com") and not (
            re.search(r"/video/\d+", parsed.path) or parsed.path.startswith("/t/")):
            raise MediaImportError("Give a link to one TikTok video")
        return "tiktok"
    raise MediaImportError("Only YouTube and TikTok video links are supported")


def seconds(value: str | int | float | None) -> float | None:
    if value in (None, ""):
        return None
    parts = str(value).strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise MediaImportError("Use seconds or HH:MM:SS for start and end")
    try:
        nums = [float(part) for part in parts]
    except ValueError as exc:
        raise MediaImportError("Use seconds or HH:MM:SS for start and end") from exc
    if any(not 0 <= number < 60 for number in nums[1:]) or any(number != number for number in nums):
        raise MediaImportError("Invalid start or end time")
    total = sum(number * 60 ** i for i, number in enumerate(reversed(nums)))
    if not 0 <= total <= 24 * 3600:
        raise MediaImportError("Start and end must be within 24 hours")
    return total


def crop_filter(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 4 or any(not part.isdigit() for part in parts):
        raise MediaImportError("Crop must be x:y:width:height in pixels")
    x, y, width, height = map(int, parts)
    if width < 2 or height < 2 or width % 2 or height % 2 or any(n > 8192 for n in (x, y, width, height)):
        raise MediaImportError("Crop width and height must be even pixel counts between 2 and 8192")
    return f"crop={width}:{height}:{x}:{y}"


def _run(args: list[str], timeout: int) -> str:
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise MediaImportError("Media download or conversion timed out") from exc
    if process.returncode:
        detail = (output or "").strip().splitlines()[-3:]
        raise MediaImportError("; ".join(detail)[-450:] or "Media download or conversion failed")
    return output or ""


def import_clip(url: str, workspace: Path, session_id: str, *, output_format: str = "mp4",
                start: str | float | None = None, end: str | float | None = None,
                crop: str = "", name: str = "") -> Path:
    site = platform(url)
    if output_format not in ("mp4", "mp3"):
        raise MediaImportError("Format must be mp4 or mp3")
    begin, finish = seconds(start), seconds(end)
    begin = begin or 0.0
    if finish is not None and finish <= begin:
        raise MediaImportError("End must be later than start")
    if finish is not None and finish - begin > 3600:
        raise MediaImportError("A clip may be at most one hour long")
    video_crop = crop_filter(crop)
    if output_format == "mp3" and video_crop:
        raise MediaImportError("Cropping applies to video; omit --crop for MP3")

    folder = workspace / session_id / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    task_id = uuid.uuid4().hex[:10]
    temp = folder / f"tmp-{task_id}"
    temp.mkdir()
    stem = re.sub(r"[^\w.-]+", "-", name.strip(), flags=re.UNICODE).strip(".-")[:80] or f"{site}-{task_id}"
    output = folder / f"{stem}.{output_format}"
    if output.exists():
        output = folder / f"{stem}-{task_id}.{output_format}"
    try:
        # TikTok's download_addr is commonly watermarked. Use a playback format only;
        # fail if the site does not expose one rather than silently choosing a marked copy.
        selector = ("best[format_note!*=?'watermarked'][format_id!*='download'][vcodec!=none]"
                    if site == "tiktok" else "bv*+ba/b")
        # The Manager-managed yt-dlp (Models -> Manager -> Core Backend) when installed, else the image's copy.
        managed = Path("/opt/rocsy-tools/yt-dlp/yt-dlp")
        command = ([sys.executable, str(managed)] if managed.is_file() else [sys.executable, "-m", "yt_dlp"]) + ["--ignore-config", "--no-playlist", "--no-progress",
                   "--no-warnings", "--max-filesize", "750M",
                   "--socket-timeout", "20", "--retries", "2", "--fragment-retries", "2",
                   "-f", selector, "--merge-output-format", "mp4", "-o", str(temp / "source.%(ext)s")]
        partial = site == "youtube" and finish is not None
        if partial:
            command += ["--download-sections", f"*{begin:g}-{finish:g}", "--force-keyframes-at-cuts"]
        try:
            _run(command + [url], 240)
        except MediaImportError as exc:
            if site == "tiktok" and "Requested format" in str(exc):
                raise MediaImportError("TikTok did not offer a non-watermarked playback format for this video") from exc
            raise
        candidates = [p for p in temp.glob("source.*") if p.is_file() and p.suffix.lower() in
                      (".mp4", ".mkv", ".webm", ".mov")]
        if not candidates:
            raise MediaImportError("The video could not be downloaded in a usable format")
        source = max(candidates, key=lambda p: p.stat().st_size)
        ffmpeg = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        if begin and not partial:
            ffmpeg += ["-ss", f"{begin:g}"]
        ffmpeg += ["-i", str(source)]
        if finish is not None:
            ffmpeg += ["-t", f"{finish - begin:g}"]
        if output_format == "mp3":
            ffmpeg += ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-q:a", "2"]
        else:
            ffmpeg += ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
                       "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                       "-movflags", "+faststart"]
            if video_crop:
                ffmpeg += ["-vf", video_crop]
        _run(ffmpeg + [str(output)], 240)
        if not output.is_file() or output.stat().st_size < 1024:
            raise MediaImportError("The converted file is empty")
        return output
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)
