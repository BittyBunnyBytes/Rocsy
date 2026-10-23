"""Frame-exact video edits for the Master Agent (`$R edit`): trim by frame or time, keep every Nth
frame, speed, crop, fit to another video's frame, loop to a count or to another video's length, and
lay the soundtrack from any clip. Decodes and encodes on the GPU's media engine (VA-API) and scales
with scale_vaapi; there is no CPU encode fallback (Rocsy's GPU-over-CPU rule)."""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
from typing import Any

RENDER_NODE = "/dev/dri/renderD128"
MAX_SECONDS = 600


class EditError(Exception):
    pass


async def run(cmd: list[str], timeout: float) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise EditError(f"ffmpeg took longer than {timeout:.0f} s")
    if proc.returncode:
        raise EditError(err.decode(errors="replace").strip()[-400:] or f"{cmd[0]} exited {proc.returncode}")
    return out.decode(errors="replace")


def rate(text: str) -> float:
    num, _, den = str(text or "0").partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


async def probe(path: Path) -> dict[str, Any]:
    """width, height, fps, frames, duration and whether it has sound."""
    data = json.loads(await run(["ffprobe", "-v", "error", "-show_entries",
                                 "stream=codec_type,width,height,r_frame_rate,avg_frame_rate,nb_frames:format=duration",
                                 "-of", "json", str(path)], 60) or "{}")
    streams = data.get("streams") or []
    video = next((x for x in streams if x.get("codec_type") == "video"), None)
    duration = float((data.get("format") or {}).get("duration") or 0)
    info = {"duration": round(duration, 3), "audio": any(x.get("codec_type") == "audio" for x in streams)}
    if not video or not video.get("width"):
        return info | {"width": 0, "height": 0, "fps": 0.0, "frames": 0}
    fps = rate(video.get("avg_frame_rate")) or rate(video.get("r_frame_rate")) or 24.0
    frames = int(video.get("nb_frames") or 0) or round(duration * fps)
    return info | {"width": int(video["width"]), "height": int(video["height"]), "fps": round(fps, 3), "frames": frames}


async def packets(path: Path) -> int:
    out = await run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                     "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)], 60)
    return int(out.strip().split(",")[0] or 0)


def even(x: float) -> int:
    return max(2, int(x) // 2 * 2)


def cover(w: int, h: int, tw: int, th: int) -> tuple[int, int, int, int]:
    """The centred crop of a w x h frame with the tw x th aspect (scaled to fill, edges cut)."""
    cw, ch = (min(w, h * tw / th), h) if w / h > tw / th else (w, min(h, w * th / tw))
    cw, ch = even(cw), even(ch)
    return cw, ch, even((w - cw) / 2) if w > cw else 0, even((h - ch) / 2) if h > ch else 0


async def render(src: Path, out: Path, work: Path, *, frames: tuple[int, int] | None = None,
                 times: tuple[float, float | None] | None = None, every: int = 1, speed: float = 1.0,
                 fps: float | None = None, crop: tuple[int, int, int, int] | None = None,
                 size: tuple[int, int] | None = None, loop: int = 1, length: float | None = None,
                 audio: Path | None = None, audio_start: float = 0.0, timeout: float = 540) -> dict[str, Any]:
    """Two passes: 1) the edited cycle (trim, every Nth, speed, crop, fit) encoded once without
    B-frames; 2) that cycle stream-copied `loop` times (or to `length` seconds, frame-exact) with the
    soundtrack from `audio` starting at `audio_start`, padded with silence and cut to the video."""
    if not os.path.exists(RENDER_NODE):
        raise EditError("this machine has no GPU media engine (VA-API) for the encode; use postedit instead")
    info = await probe(src)
    if not info["width"]:
        raise EditError(f"{src.name} has no video stream")
    out_fps = fps or info["fps"]
    chain: list[str] = []
    if frames:
        a, b = frames
        if not 0 <= a < b:
            raise EditError("--frames is START-END with END after START (frame numbers from 0, END not included)")
        chain += [f"trim=start_frame={a}:end_frame={b}", "setpts=PTS-STARTPTS"]
    elif times:
        a, b = times
        chain += [f"trim=start={a:.3f}" + (f":end={b:.3f}" if b else ""), "setpts=PTS-STARTPTS"]
    if every > 1:                       # keep frames 0, N, 2N ... and play them back to back
        chain += [f"select='not(mod(n\\,{every}))'", f"setpts=N/({out_fps}*TB)"]
    if speed != 1:
        chain += [f"setpts=PTS/{speed}", f"fps={out_fps}"]
    elif fps and every <= 1:
        chain.append(f"fps={out_fps}")
    w, h = info["width"], info["height"]
    if crop:
        cw, ch, cx, cy = even(crop[2]), even(crop[3]), even(crop[0]) if crop[0] else 0, even(crop[1]) if crop[1] else 0
        if cx + cw > w or cy + ch > h:
            raise EditError(f"--crop {crop[0]}:{crop[1]}:{crop[2]}:{crop[3]} goes outside the {w}x{h} frame")
        chain.append(f"crop={cw}:{ch}:{cx}:{cy}")
        w, h = cw, ch
    if size:
        tw, th = even(size[0]), even(size[1])
        cw, ch, cx, cy = cover(w, h, tw, th)
        if (cw, ch) != (w, h):
            chain.append(f"crop={cw}:{ch}:{cx}:{cy}")
        chain += ["format=nv12", "hwupload", f"scale_vaapi=w={tw}:h={th}:mode=hq:format=nv12"]
    else:
        chain += ["format=nv12", "hwupload"]
    cycle = work / "cycle.mp4"
    head = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    await run(head + ["-init_hw_device", f"vaapi=va:{RENDER_NODE}", "-filter_hw_device", "va",
                      "-hwaccel", "vaapi", "-hwaccel_device", "va", "-i", str(src), "-an", "-vf", ",".join(chain),
                      "-r", f"{out_fps}", "-c:v", "h264_vaapi", "-qp", "16", "-bf", "0", str(cycle)], timeout)
    count = await packets(cycle)
    if not count:
        raise EditError("the edit left no frames (check --frames / --start / --every)")
    total = round(length * out_fps) if length else count * max(1, loop)
    if total / out_fps > MAX_SECONDS:
        raise EditError(f"the result would be {total / out_fps:.0f} s; edits are capped at {MAX_SECONDS} s")
    loops = math.ceil(total / count)
    cmd = head + (["-stream_loop", str(loops - 1)] if loops > 1 else []) + ["-i", str(cycle)]
    if audio:
        cmd += ["-ss", f"{audio_start:.3f}", "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                "-af", "apad", "-c:a", "aac", "-b:a", "192k", "-t", f"{total / out_fps:.3f}"]
    else:
        cmd += ["-map", "0:v:0"]
    await run(cmd + ["-c:v", "copy", "-frames:v", str(total), "-movflags", "+faststart", str(out)], timeout)
    cycle.unlink(missing_ok=True)
    return await probe(out) | {"cycle_frames": count, "loops": total / count}


async def muted_copy(src: Path, out: Path) -> None:
    await run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(src), "-map", "0:v:0", "-c", "copy",
               "-movflags", "+faststart", str(out)], 120)
