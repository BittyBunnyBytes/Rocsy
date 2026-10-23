"""Download processing (the Settings tab): watermarks and metadata applied to
files as they are downloaded. Originals are never changed; processed copies
are cached under data/downloads keyed by the job and a hash of the settings.

Visual marks (text, logo, tiled) are ffmpeg filters; the tiled mark is a
full-frame RGBA PNG drawn with Pillow. Audio marks are a spoken tag (Edge TTS,
cached per text/voice) and an optional high-frequency marker tone. Metadata
is stripped with -map_metadata -1 plus bitexact flags (no encoder/creation
time), then the configured fields are written. An uploaded outro video can be
appended after the (watermarked) video: scaled to the same size and frame
rate, with its own sound or silence, optionally faded through black.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from . import gpu
from PIL.PngImagePlugin import PngInfo

STATE_KEY = "download_settings"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "top", "bottom", "center", "bounce", "jump")
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "apply_to_frames": True,
    "filename_pattern": "{model}_{date}_{id}",
    "text": {"enabled": False, "text": "© Video", "position": "bottom-right", "size": 4.0, "opacity": 0.7,
             "color": "#ffffff", "outline": True, "box": False, "margin": 3.0, "speed": 1.0, "interval": 3.0},
    "logo": {"enabled": False, "position": "top-right", "width": 12.0, "opacity": 0.8, "margin": 3.0, "speed": 1.0, "interval": 3.0},
    "tiled": {"enabled": False, "text": "H3 MACHINE", "opacity": 0.08, "size": 3.5, "angle": -30, "spacing": 2.2, "color": "#ffffff"},
    # source: "tts" = the phrase spoken by an Edge TTS voice, "file" = an uploaded sound
    # (data/watermark/audio-tag.<ext>). where: start / middle / end / random (seeded by the job).
    "audio_tag": {"enabled": False, "source": "tts", "text": "Made with Rocsy", "voice": "en-US-AriaNeural", "volume": 0.8, "where": "end"},
    "audio_marker": {"enabled": False, "frequency": 19000, "volume": 0.01},
    # fit: letterbox the outro into the frame; fill: crop it to fill the frame.
    "outro": {"enabled": False, "fit": "fit", "keep_audio": True, "volume": 1.0, "fade": 0.3},
    "metadata": {"strip": True, "title": "", "artist": "", "copyright": "", "comment": "Downloaded from Video", "description": ""},
}
LIMITS = {"text": 200, "field": 500, "pattern": 120}
TAG_WHERE = ("start", "middle", "end", "random")


def _merge(base: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in (saved or {}).items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            result[key] = {**base[key], **{k: v for k, v in value.items() if k in base[key]}}
        elif key in base:
            result[key] = value
    return result


def load(store: Any) -> dict[str, Any]:
    return _merge(DEFAULTS, store.get_state(STATE_KEY) or {})


def _num(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) else max(low, min(high, number))


def _color(value: Any, default: str) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) else default


def _text(value: Any, limit: int, default: str = "") -> str:
    return str(value)[:limit] if isinstance(value, str) else default


def update(store: Any, changes: dict[str, Any]) -> dict[str, Any]:
    """Merge and validate (clamp) the incoming settings, then save."""
    merged = _merge(load(store), changes)
    clean = json.loads(json.dumps(DEFAULTS))
    clean["enabled"] = bool(merged["enabled"])
    clean["apply_to_frames"] = bool(merged["apply_to_frames"])
    pattern = _text(merged["filename_pattern"], LIMITS["pattern"], DEFAULTS["filename_pattern"]).strip()
    clean["filename_pattern"] = pattern or DEFAULTS["filename_pattern"]
    t, c = merged["text"], clean["text"]
    c.update(enabled=bool(t["enabled"]), text=_text(t["text"], LIMITS["text"]), position=t["position"] if t["position"] in POSITIONS else "bottom-right",
             size=_num(t["size"], 1, 30, 4), opacity=_num(t["opacity"], 0.01, 1, 0.7), color=_color(t["color"], "#ffffff"),
             outline=bool(t["outline"]), box=bool(t["box"]), margin=_num(t["margin"], 0, 20, 3),
             speed=_num(t["speed"], 0.1, 5, 1), interval=_num(t["interval"], 0.5, 30, 3))
    lo, c = merged["logo"], clean["logo"]
    c.update(enabled=bool(lo["enabled"]), position=lo["position"] if lo["position"] in POSITIONS else "top-right",
             width=_num(lo["width"], 2, 80, 12), opacity=_num(lo["opacity"], 0.01, 1, 0.8), margin=_num(lo["margin"], 0, 20, 3),
             speed=_num(lo["speed"], 0.1, 5, 1), interval=_num(lo["interval"], 0.5, 30, 3))
    ti, c = merged["tiled"], clean["tiled"]
    c.update(enabled=bool(ti["enabled"]), text=_text(ti["text"], LIMITS["text"]), opacity=_num(ti["opacity"], 0.005, 1, 0.08),
             size=_num(ti["size"], 1, 20, 3.5), angle=_num(ti["angle"], -90, 90, -30), spacing=_num(ti["spacing"], 1.1, 6, 2.2),
             color=_color(ti["color"], "#ffffff"))
    au, c = merged["audio_tag"], clean["audio_tag"]
    c.update(enabled=bool(au["enabled"]), source=au["source"] if au["source"] in ("tts", "file") else "tts", text=_text(au["text"], LIMITS["text"]),
             voice=au["voice"] if isinstance(au["voice"], str) and re.fullmatch(r"[A-Za-z]{2,3}-[A-Za-z0-9-]+Neural", au["voice"]) else DEFAULTS["audio_tag"]["voice"],
             volume=_num(au["volume"], 0.05, 2, 0.8), where=au["where"] if au["where"] in TAG_WHERE else "end")
    am, c = merged["audio_marker"], clean["audio_marker"]
    c.update(enabled=bool(am["enabled"]), frequency=int(_num(am["frequency"], 15000, 21000, 19000)), volume=_num(am["volume"], 0.001, 0.1, 0.01))
    ou, c = merged["outro"], clean["outro"]
    c.update(enabled=bool(ou["enabled"]), fit=ou["fit"] if ou["fit"] in ("fit", "fill") else "fit",
             keep_audio=bool(ou["keep_audio"]), volume=_num(ou["volume"], 0, 2, 1), fade=_num(ou["fade"], 0, 2, 0.3))
    for key in DEFAULTS["metadata"]:
        clean["metadata"][key] = bool(merged["metadata"][key]) if key == "strip" else _text(merged["metadata"][key], LIMITS["field"])
    store.set_state(STATE_KEY, clean)
    return clean


def settings_hash(settings: dict[str, Any], logo: Path | None, outro: Path | None = None, tag_file: Path | None = None) -> str:
    payload = json.dumps(settings, sort_keys=True)
    if logo and logo.is_file():
        payload += f"|{logo.stat().st_mtime_ns}"
    if outro and outro.is_file():
        payload += f"|outro:{outro.stat().st_mtime_ns}"
    if tag_file and tag_file.is_file():
        payload += f"|tag:{tag_file.stat().st_mtime_ns}"
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def visual_enabled(settings: dict[str, Any], logo: Path | None) -> bool:
    return bool((settings["text"]["enabled"] and settings["text"]["text"].strip())
                or (settings["logo"]["enabled"] and logo and logo.is_file())
                or (settings["tiled"]["enabled"] and settings["tiled"]["text"].strip()))


def audio_enabled(settings: dict[str, Any]) -> bool:
    tag = settings["audio_tag"]
    return bool((tag["enabled"] and (tag["source"] == "file" or tag["text"].strip())) or settings["audio_marker"]["enabled"])


def tag_delay(where: str, duration: float, tag_duration: float, seed: str) -> float:
    """Seconds into the video where the audio tag starts. "random" is seeded by
    the job, so one video always gets the same spot (and cached downloads match)."""
    room = max(0.0, duration - tag_duration - 0.2)  # latest start that still fits
    if where == "start":
        return 0.0
    if where == "middle":
        return max(0.0, (duration - tag_duration) / 2)
    if where == "random":
        return random.Random(seed).uniform(0.0, room)
    return room


# ------------------------------------------------------------------ filename --

def filename(item: dict[str, Any], pattern: str, extension: str) -> str:
    created = item.get("completed_at") or item.get("created_at") or ""
    try:
        date = datetime.fromisoformat(created.replace("Z", "+00:00")).strftime("%Y%m%d-%H%M%S")
    except ValueError:
        date = "date"
    prompt = re.sub(r"[^A-Za-z0-9]+", "-", (item.get("prompt") or "").split(" · ")[0])[:40].strip("-").lower() or "video"
    values = {"id": item["id"][:8], "model": item.get("model") or "video", "date": date, "prompt": prompt,
              "duration": f"{item.get('duration') or 0:g}s", "size": f"{item.get('width') or 0}x{item.get('height') or 0}"}
    name = re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), pattern)
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .") or item["id"][:8]
    return f"{name[:150]}.{extension}"


# ------------------------------------------------------------------- visuals --

def _ffcolor(hex_color: str) -> str:
    return f"0x{hex_color[1:]}"


def _placement(position: str, margin_x: str, margin_y: str, speed: float, interval: float,
               width: str, height: str, item_w: str, item_h: str) -> tuple[str, str]:
    """x/y expressions (ffmpeg) for a mark of size item_w x item_h inside width x height."""
    free_x, free_y = f"({width}-{item_w})", f"({height}-{item_h})"
    if position == "bounce":  # DVD-logo style: never still, so it can't be cropped away cleanly
        return (f"abs(mod(t*{60 * speed:.3f},2*{free_x})-{free_x})", f"abs(mod(t*{43 * speed:.3f},2*{free_y})-{free_y})")
    if position == "jump":  # a new spot every `interval` seconds
        return (f"{free_x}*(0.05+0.9*mod(floor(t/{interval:.3f})*0.618034+0.13,1))",
                f"{free_y}*(0.05+0.9*mod(floor(t/{interval:.3f})*0.414214+0.71,1))")
    x = {"left": margin_x, "right": f"{free_x}-{margin_x}", "center": f"{free_x}/2"}
    y = {"top": margin_y, "bottom": f"{free_y}-{margin_y}", "middle": f"{free_y}/2"}
    horizontal = "left" if "left" in position else "right" if "right" in position else "center"
    vertical = "top" if position.startswith("top") else "bottom" if position.startswith("bottom") else "middle"
    return x[horizontal], y[vertical]


def tiled_png(settings: dict[str, Any], width: int, height: int, target: Path) -> Path:
    """Full-frame transparent PNG with the tiled text rotated across it."""
    tiled = settings["tiled"]
    size = max(8, int(height * tiled["size"] / 100))
    font = ImageFont.truetype(FONT, size)
    diagonal = int(math.hypot(width, height)) + size * 4
    canvas = Image.new("RGBA", (diagonal, diagonal), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    text = tiled["text"].strip()
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = right - left, bottom - top
    step_x, step_y = int(text_w + size * tiled["spacing"]), int(text_h * tiled["spacing"] * 1.6)
    red, green, blue = (int(tiled["color"][i:i + 2], 16) for i in (1, 3, 5))
    alpha = int(255 * tiled["opacity"])
    for row, y in enumerate(range(0, diagonal, max(8, step_y))):
        offset = (row % 2) * step_x // 2
        for x in range(-step_x, diagonal, max(8, step_x)):
            draw.text((x + offset, y), text, font=font, fill=(red, green, blue, alpha))
    rotated = canvas.rotate(tiled["angle"], resample=Image.BICUBIC)
    cx, cy = rotated.width // 2, rotated.height // 2
    rotated.crop((cx - width // 2, cy - height // 2, cx - width // 2 + width, cy - height // 2 + height)).save(target)
    return target


def visual_graph(settings: dict[str, Any], width: int, height: int, workdir: Path, logo: Path | None,
                 first_input: int, base: str = "0:v") -> tuple[list[str], list[str], str]:
    """(extra ffmpeg inputs, filter_complex parts, output label) applying the
    enabled visual marks to [base]. Needs cwd=workdir (textfile is relative)."""
    inputs: list[str] = []
    parts: list[str] = []
    label, index = base, first_input
    if settings["tiled"]["enabled"] and settings["tiled"]["text"].strip():
        tiled_png(settings, width, height, workdir / "tiled.png")
        inputs += ["-loop", "1", "-i", "tiled.png"]
        parts.append(f"[{label}][{index}:v]overlay=0:0:shortest=1:format=auto[vt]")
        label, index = "vt", index + 1
    if settings["logo"]["enabled"] and logo and logo.is_file():
        lo = settings["logo"]
        logo_w = max(8, int(width * lo["width"] / 100) // 2 * 2)
        inputs += ["-loop", "1", "-i", str(logo)]
        parts.append(f"[{index}:v]format=rgba,scale={logo_w}:-2,colorchannelmixer=aa={lo['opacity']:.3f}[logo]")
        mx, my = f"{width * lo['margin'] / 100:.1f}", f"{height * lo['margin'] / 100:.1f}"
        x, y = _placement(lo["position"], mx, my, lo["speed"], lo["interval"], "W", "H", "w", "h")
        parts.append(f"[{label}][logo]overlay=x='{x}':y='{y}':eval=frame:shortest=1:format=auto[vl]")
        label, index = "vl", index + 1
    if settings["text"]["enabled"] and settings["text"]["text"].strip():
        tx = settings["text"]
        (workdir / "watermark.txt").write_text(tx["text"])
        size = max(8, int(height * tx["size"] / 100))
        mx, my = f"{width * tx['margin'] / 100:.1f}", f"{height * tx['margin'] / 100:.1f}"
        x, y = _placement(tx["position"], mx, my, tx["speed"], tx["interval"], "w", "h", "text_w", "text_h")
        options = [f"fontfile={FONT}", "textfile=watermark.txt", "expansion=none", f"fontsize={size}",
                   f"fontcolor={_ffcolor(tx['color'])}@{tx['opacity']:.3f}", f"x='{x}'", f"y='{y}'"]
        if tx["outline"]:
            options += [f"borderw={max(1, size // 18)}", f"bordercolor=black@{tx['opacity'] * 0.8:.3f}"]
        if tx["box"]:
            options += ["box=1", f"boxcolor=black@{tx['opacity'] * 0.5:.3f}", f"boxborderw={max(4, size // 4)}"]
        parts.append(f"[{label}]drawtext={':'.join(options)}[vx]")
        label = "vx"
    return inputs, parts, label


def metadata_args(settings: dict[str, Any]) -> list[str]:
    meta = settings["metadata"]
    args: list[str] = []
    if meta["strip"]:
        args += ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact",
                 "-flags:v", "+bitexact", "-flags:a", "+bitexact"]
    for key in ("title", "artist", "copyright", "comment", "description"):
        if meta[key].strip():
            args += ["-metadata", f"{key}={meta[key].strip()}"]
    return args


# --------------------------------------------------------------------- video --

def video_args(settings: dict[str, Any], source: Path, info: dict[str, Any], workdir: Path, output: Path,
               logo: Path | None, tag_audio: Path | None, outro: tuple[Path, dict[str, Any]] | None = None) -> list[str]:
    """ffmpeg argv (run with cwd=workdir) producing the processed download.
    `outro` = (file, its probe info) when a video should be appended at the end."""
    width, height, duration = info["width"], info["height"], max(0.1, info["duration"])
    visual = visual_enabled(settings, logo)
    audio_marks = audio_enabled(settings) and (tag_audio is not None or settings["audio_marker"]["enabled"])
    # GPU decode/encode (backend/gpu.py); only the watermark filters run on the CPU.
    args = ["ffmpeg", "-y", "-v", "error", *gpu.init_args(), *gpu.decode_args(), "-i", str(source)]
    if outro is not None:
        return _outro_args(settings, info, workdir, output, logo, tag_audio, outro, visual, audio_marks, args)
    if not visual and not audio_marks:
        return args + ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", *metadata_args(settings),
                       "-movflags", "+faststart", str(output)]
    next_input = 1
    parts: list[str] = []
    video_label = "0:v"
    if visual:
        inputs, visual_parts, video_label = visual_graph(settings, width, height, workdir, logo, next_input)
        args += inputs
        parts += visual_parts
        next_input += inputs.count("-i")
    audio_label = "0:a" if info["has_audio"] else None
    if audio_marks:
        if audio_label is None:
            args += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            audio_label, next_input = f"{next_input}:a", next_input + 1
        fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
        parts.append(f"[{audio_label}]{fmt}[a0]")
        mixes = ["a0"]
        if tag_audio is not None:
            tag = settings["audio_tag"]
            args += ["-i", str(tag_audio)]
            delay = tag_delay(tag["where"], duration, info.get("tag_duration", 2.0), info.get("seed", ""))
            parts.append(f"[{next_input}:a]{fmt},volume={tag['volume']:.3f},adelay={int(delay * 1000)}:all=1[tag]")
            mixes.append("tag")
            next_input += 1
        if settings["audio_marker"]["enabled"]:
            marker = settings["audio_marker"]
            args += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", f"sine=frequency={marker['frequency']}:sample_rate=48000"]
            parts.append(f"[{next_input}:a]{fmt},volume={marker['volume']:.4f}[mark]")
            mixes.append("mark")
            next_input += 1
        parts.append("".join(f"[{m}]" for m in mixes) + f"amix=inputs={len(mixes)}:duration=first:normalize=0,alimiter=limit=0.97:level=disabled[aout]")
        audio_label = "aout"
    if visual:
        parts.append(f"[{video_label}]null{gpu.upload(width, height)}[venc]")
        video_label = "venc"
    args += ["-filter_complex", ";".join(parts)] if parts else []
    args += ["-map", f"[{video_label}]" if visual else "0:v:0"]
    if audio_label:
        args += ["-map", f"[{audio_label}]" if audio_marks else "0:a:0"]
    args += gpu.h264(16, width, height) if visual else ["-c:v", "copy"]
    if audio_label:
        args += ["-c:a", "aac", "-b:a", "192k"] if audio_marks else ["-c:a", "copy"]
    args += [*metadata_args(settings), "-movflags", "+faststart", "-t", f"{duration:.3f}", str(output)]
    return args


def _outro_args(settings: dict[str, Any], info: dict[str, Any], workdir: Path, output: Path, logo: Path | None,
                tag_audio: Path | None, outro: tuple[Path, dict[str, Any]], visual: bool, audio_marks: bool,
                args: list[str]) -> list[str]:
    """Same marks as video_args, then the outro joined after them (always re-encoded:
    both parts need one size, frame rate and audio format to be concatenated)."""
    width, height, duration = info["width"], info["height"], max(0.1, info["duration"])
    fps = info.get("fps") or 24
    path, outro_info = outro
    conf = settings["outro"]
    outro_duration = max(0.1, outro_info["duration"])
    fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    parts: list[str] = []
    next_input = 1
    video_label = "0:v"
    if visual:
        inputs, visual_parts, video_label = visual_graph(settings, width, height, workdir, logo, next_input)
        args += inputs
        parts += visual_parts
        next_input += inputs.count("-i")
    # Main audio: the video's own (plus any audio marks), else silence of its length.
    if info["has_audio"]:
        main_audio = "0:a"
    else:
        args += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        main_audio, next_input = f"{next_input}:a", next_input + 1
    parts.append(f"[{main_audio}]{fmt},atrim=0:{duration:.3f},apad=whole_dur={duration:.3f}[a0]")
    mixes = ["a0"]
    if tag_audio is not None:
        tag = settings["audio_tag"]
        args += ["-i", str(tag_audio)]
        delay = tag_delay(tag["where"], duration, info.get("tag_duration", 2.0), info.get("seed", ""))
        parts.append(f"[{next_input}:a]{fmt},volume={tag['volume']:.3f},adelay={int(delay * 1000)}:all=1[tag]")
        mixes.append("tag")
        next_input += 1
    if settings["audio_marker"]["enabled"]:
        marker = settings["audio_marker"]
        args += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", f"sine=frequency={marker['frequency']}:sample_rate=48000"]
        parts.append(f"[{next_input}:a]{fmt},volume={marker['volume']:.4f}[mark]")
        mixes.append("mark")
        next_input += 1
    if len(mixes) > 1:
        parts.append("".join(f"[{m}]" for m in mixes) + f"amix=inputs={len(mixes)}:duration=first:normalize=0,alimiter=limit=0.97:level=disabled[amain]")
    else:
        parts.append("[a0]anull[amain]")
    # Outro video: same size (fit = letterbox, fill = crop), frame rate and pixel format.
    args += [*gpu.decode_args(), "-i", str(path)]
    outro_input = next_input
    next_input += 1
    size = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
            if conf["fit"] == "fit" else f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}")
    fade = min(conf["fade"], duration / 2, outro_duration / 2)
    main_fade = f",fade=t=out:st={duration - fade:.3f}:d={fade:.3f}" if fade > 0 else ""
    outro_fade = f",fade=t=in:st=0:d={fade:.3f}" if fade > 0 else ""
    parts.append(f"[{video_label}]fps={fps:.4f},scale={width}:{height},setsar=1,format=yuv420p,trim=0:{duration:.3f},setpts=PTS-STARTPTS{main_fade}[v0]")
    parts.append(f"[{outro_input}:v]{size},setsar=1,fps={fps:.4f},format=yuv420p,setpts=PTS-STARTPTS{outro_fade}[v1]")
    if conf["keep_audio"] and outro_info.get("has_audio"):
        parts.append(f"[{outro_input}:a]{fmt},volume={conf['volume']:.3f},atrim=0:{outro_duration:.3f},apad=whole_dur={outro_duration:.3f}[a1]")
    else:
        args += ["-f", "lavfi", "-t", f"{outro_duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        parts.append(f"[{next_input}:a]{fmt}[a1]")
        next_input += 1
    parts.append("[v0][amain][v1][a1]concat=n=2:v=1:a=1[vcat][aout]")
    parts.append(f"[vcat]null{gpu.upload(width, height)}[vout]")
    args += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]",
             *gpu.h264(16, width, height), "-c:a", "aac", "-b:a", "192k",
             *metadata_args(settings), "-movflags", "+faststart", str(output)]
    return args


# --------------------------------------------------------------------- image --

def image_args(settings: dict[str, Any], source: Path, width: int, height: int, workdir: Path, output: Path,
               logo: Path | None, at: float | None = None) -> list[str]:
    """ffmpeg argv drawing the visual marks on one frame (a PNG, or a video at `at` seconds)."""
    args = ["ffmpeg", "-y", "-v", "error"]
    if at is not None:  # a video: decode on the GPU (PNG frames have nothing to decode there)
        args += [*gpu.init_args(), *gpu.decode_args(), "-ss", f"{at:.3f}"]
    args += ["-i", str(source)]
    # Moving marks use t: shift the frame's timestamp to `at` so the preview shows where it would be.
    shift = [f"[0:v]setpts=PTS-STARTPTS+{at:.3f}/TB[v0]"] if at else []
    inputs, parts, label = visual_graph(settings, width, height, workdir, logo, 1, "v0" if at else "0:v")
    args += inputs
    if parts:
        args += ["-filter_complex", ";".join(shift + parts), "-map", f"[{label}]"]
    return args + ["-frames:v", "1", "-update", "1", str(output)]


def finish_png(settings: dict[str, Any], path: Path) -> None:
    """Strip PNG text/EXIF chunks and write the configured message instead."""
    meta = settings["metadata"]
    with Image.open(path) as image:
        image.load()
        info = PngInfo()
        for key, name in (("title", "Title"), ("artist", "Author"), ("copyright", "Copyright"),
                          ("comment", "Comment"), ("description", "Description")):
            if meta[key].strip():
                info.add_text(name, meta[key].strip())
        clean = image.copy() if meta["strip"] else image
        clean.save(path, pnginfo=info)


async def run(args: list[str], cwd: Path) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(*args, cwd=str(cwd), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, stderr = await process.communicate()
    return process.returncode or 0, stderr.decode(errors="replace")[-600:]
