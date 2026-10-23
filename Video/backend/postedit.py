"""Post-Edit: one ffmpeg pass that re-renders a finished video with timing,
frame, color, audio (music, voice-over with ducking), caption and text
overlay edits. See PostEditRequest in models.py for the knobs.

Everything lands on an "output timeline": the trimmed source played at
`speed` (optionally reversed), plus `hold_end` seconds of frozen last frame.
Music, voice clips, captions and text overlays are positioned on that
timeline, which is what the Post-Edit tab's preview shows.

ffmpeg runs with cwd = a scratch dir, so text overlays (textfile=) and the
captions file can be referenced by bare relative names - no filter escaping
of user text or paths.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import gpu
from .models import PostEditRequest

FONT_FILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
CRF = {"standard": 18, "high": 12, "near_lossless": 4}
SAMPLE_FORMAT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"


@dataclass(frozen=True)  # cached by probe(); never mutate
class SourceInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    video_duration: float = 0.0  # the video stream alone; can end before the audio (and the file)


@dataclass
class AudioClip:
    path: Path
    start: float
    volume: float
    srt: str | None
    captions: bool
    words: list[list] | None = None  # exact (start, end, word) timings, Edge TTS only


# ffprobe costs ~50 ms and the same finished files get probed again and again
# (frame grabs, downloads, the Settings tab's outro). Keyed on mtime + size, so
# a rewritten file is probed afresh; a failed/empty probe isn't cached.
_probe_cache: dict[tuple[str, int, int], SourceInfo] = {}
PROBE_CACHE_SIZE = 256


SILENCE_DB = -50       # quieter than this counts as silence (a soft fade-in is still sound)
SILENCE_SCAN = 120.0   # leading silence is looked for in the first two minutes only


async def leading_silence(path: Path) -> float:
    """Seconds of silence before the first sound in an audio file (0 when it starts
    with sound, is silent throughout, or can't be read). A hair is left before the
    first sound so its attack isn't clipped."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-hide_banner", "-t", f"{SILENCE_SCAN:g}", "-i", str(path), "-vn",
        "-af", f"silencedetect=noise={SILENCE_DB}dB:d=0.05", "-f", "null", "-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await process.communicate()
    if process.returncode:
        return 0.0
    text = err.decode(errors="replace")
    start = re.search(r"silence_start: (-?[0-9.]+)", text)
    if not start or float(start.group(1)) > 0.02:
        return 0.0
    end = re.search(r"silence_end: ([0-9.]+)", text)
    length = re.search(r"Duration: (\d+):(\d+):([0-9.]+)", text)
    scanned = min(SILENCE_SCAN, int(length.group(1)) * 3600 + int(length.group(2)) * 60 + float(length.group(3))) if length else SILENCE_SCAN
    if not end or float(end.group(1)) >= scanned - 0.05:  # silent for the whole scanned part: leave it alone
        return 0.0
    return max(0.0, float(end.group(1)) - 0.02)


async def probe(path: Path) -> SourceInfo:
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    if key is not None and key in _probe_cache:
        return _probe_cache[key]
    info = await _probe(path)
    if key is not None and info.duration > 0:
        if len(_probe_cache) >= PROBE_CACHE_SIZE:
            _probe_cache.pop(next(iter(_probe_cache)))
        _probe_cache[key] = info
    return info


def _tag_seconds(stream: dict) -> float:
    """Matroska (the Post-Edit pass-1 .mkv) has no stream duration, only a
    DURATION tag like "00:00:08.416000000"."""
    match = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", ((stream.get("tags") or {}).get("DURATION") or "").strip())
    return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3]) if match else 0.0


async def _probe(path: Path) -> SourceInfo:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate,duration:stream_tags=DURATION",
        "-of", "json", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    data = json.loads(stdout or b"{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    rate = video.get("r_frame_rate") or "24/1"
    numerator, _, denominator = rate.partition("/")
    try:
        fps = float(numerator) / float(denominator or 1)
    except (ValueError, ZeroDivisionError):
        fps = 24.0
    duration = float((data.get("format") or {}).get("duration") or 0)
    try:
        video_duration = float(video.get("duration") or 0) or _tag_seconds(video) or duration
    except ValueError:
        video_duration = duration
    return SourceInfo(
        duration=duration,
        width=int(video.get("width") or 0), height=int(video.get("height") or 0),
        fps=fps or 24.0, has_audio=any(s.get("codec_type") == "audio" for s in streams),
        video_duration=video_duration,
    )


async def measure_loudness(path: Path, start: float = 0.0, end: float | None = None) -> float | None:
    """Integrated loudness (LUFS) of a file's audio, optionally over start..end
    seconds; None when there's no audio or it's silent (-inf)."""
    window = ["-ss", f"{start:.3f}"] + (["-to", f"{end:.3f}"] if end is not None else [])
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats", *window, "-i", str(path), "-vn",
        "-af", "loudnorm=print_format=json", "-f", "null", "-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    match = re.search(r'"input_i"\s*:\s*"(-?[0-9.]+|-inf|inf)"', stderr.decode(errors="replace"))
    if not match or "inf" in match.group(1):
        return None
    return float(match.group(1))


# What a next video is leveled to when the finished edit is silent (the
# loudnorm target Normalize uses).
NORMALIZED_LUFS = -16.0


def next_video_gain(edited_lufs: float | None, next_lufs: float | None) -> float:
    """dB to apply to the faded-into video's audio so it plays as loud as the
    fully rendered edit before it (measured after pass 1, so music, voice-over,
    music removal and Normalize are all in it). Clamped to -30..+12 dB."""
    if next_lufs is None:
        return 0.0
    target = edited_lufs if edited_lufs is not None else NORMALIZED_LUFS
    return max(-30.0, min(12.0, target - next_lufs))


def output_duration(request: PostEditRequest, source: SourceInfo) -> tuple[float, float, float]:
    """(trim_start, trim_end, output length) with the trim clamped to the source."""
    start = min(request.trim_start, max(0.0, source.duration - 0.1))
    end = min(request.trim_end if request.trim_end is not None else source.duration, source.duration)
    end = max(end, start + 0.1)
    return start, end, (end - start) / request.speed + request.hold_end


def atempo_chain(speed: float) -> list[str]:
    parts = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    if abs(speed - 1.0) > 1e-3:
        parts.append(f"atempo={speed:.5f}")
    return parts


def _ass_color(hex_color: str) -> str:
    red, green, blue = hex_color[1:3], hex_color[3:5], hex_color[5:7]
    return f"&H00{blue}{green}{red}".upper()


_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def parse_srt(text: str) -> list[tuple[float, float, str]]:
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines = block.split("\n")
        for index, line in enumerate(lines):
            match = _SRT_TIME.search(line)
            if match:
                g = [int(value) for value in match.groups()]
                start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                body = " ".join(part.strip() for part in lines[index + 1:] if part.strip())
                if body:
                    cues.append((start, end, body))
                break
    return cues


def _srt_time(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def estimate_words(cues: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Word timings from sentence cues when the TTS gave none (TikTok, uploads):
    each word gets a share of its cue proportional to its length."""
    words = []
    for start, end, text in cues:
        tokens = text.split()
        weights = [len(token) + 2 for token in tokens]
        total, cursor = sum(weights) or 1, start
        for token, weight in zip(tokens, weights):
            span = (end - start) * weight / total
            words.append((cursor, cursor + span, token))
            cursor += span
    return words


def timeline_words(clips: list[AudioClip], length: float) -> list[tuple[float, float, str]]:
    """Every captioned voice clip's words, placed on the output timeline."""
    words = []
    for clip in clips:
        if not clip.captions:
            continue
        own = [tuple(word) for word in clip.words] if clip.words else estimate_words(parse_srt(clip.srt or ""))
        for start, end, text in own:
            start, end = start + clip.start, min(end + clip.start, length)
            if start < length and end > start:
                words.append((start, end, text))
    words.sort()
    return words


# Effects ---------------------------------------------------------------
LETTERBOX = {"2.39": 2.39, "2.00": 2.0, "1.85": 1.85}


def effect_filters(request: PostEditRequest, size: tuple[int, int], length: float, fps: float) -> list[str]:
    """Video filters for the Effects card, in order (after the look, before
    fps/hold/fades). Entries can hold `;` sub-chains (glow); the caller joins
    them with commas into the one [0:v] chain."""
    width, height = size
    out = []
    if request.tone_curve != "none":
        out.append(f"curves=preset={request.tone_curve}")
    if request.teal_orange > 0:
        a = request.teal_orange
        # Teal shadows, orange highlights (and a little warmth in the mids).
        out.append(f"colorbalance=rs={-0.25 * a:.3f}:gs={0.04 * a:.3f}:bs={0.25 * a:.3f}"
                   f":rm={0.06 * a:.3f}:bm={-0.06 * a:.3f}:rh={0.18 * a:.3f}:gh={0.04 * a:.3f}:bh={-0.2 * a:.3f}")
    if request.glow > 0:
        sigma = max(2.0, min(width, height) * (0.01 + 0.025 * request.glow))
        out.append(f"split[pe_glow_a][pe_glow_b];[pe_glow_b]gblur=sigma={sigma:.1f}[pe_glow_c];"
                   f"[pe_glow_a][pe_glow_c]blend=all_mode=screen:all_opacity={0.25 + 0.5 * request.glow:.3f}")
    if request.rgb_split > 0:
        shift = int(request.rgb_split)
        out.append(f"rgbashift=rh={-shift}:bh={shift}:edge=smear")
    if request.pixelate > 1:
        out.append(f"pixelize=width={request.pixelate}:height={request.pixelate}")
    if abs(request.slow_zoom) > 1e-3:
        # zoompan with d=1 = one output frame per input frame; z runs linearly over the output length.
        z = abs(request.slow_zoom)
        rate = fps or 24.0
        frames = max(1, round(length * rate))
        zoom = f"1+{z:.4f}*on/{frames}" if request.slow_zoom > 0 else f"1+{z:.4f}*(1-on/{frames})"
        out.append(f"zoompan=z='{zoom}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={rate:.3f}")
    if request.letterbox != "off":
        ratio = LETTERBOX[request.letterbox]
        bar = max(0, int((height - width / ratio) / 2))
        if bar > 0:
            out.append(f"drawbox=x=0:y=0:w=iw:h={bar}:color=black:t=fill,drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black:t=fill")
    return out


def effects_on(request: PostEditRequest) -> bool:
    return bool(request.tone_curve != "none" or request.teal_orange or request.glow or request.rgb_split
                or request.pixelate > 1 or abs(request.slow_zoom) > 1e-3 or request.letterbox != "off")


# Audio effects -----------------------------------------------------------
EQ_PRESETS = {
    "voice": ["highpass=f=90", "equalizer=f=250:t=q:w=1.2:g=-2", "equalizer=f=3200:t=q:w=1:g=4", "equalizer=f=9000:t=q:w=1:g=2"],
    "bass": ["bass=g=7:f=110"],
    "bright": ["treble=g=5:f=4500", "equalizer=f=10000:t=q:w=1:g=2"],
    "telephone": ["highpass=f=320", "lowpass=f=3300", "equalizer=f=1500:t=q:w=1:g=5"],
    "radio": ["highpass=f=180", "lowpass=f=5500", "equalizer=f=2200:t=q:w=1:g=4", "acompressor=threshold=0.1:ratio=6:attack=5:release=80"],
    "muffled": ["lowpass=f=700", "volume=1.5"],
}
ECHO_PRESETS = {
    "room": "aecho=0.8:0.6:35|55:0.25|0.18",
    "hall": "aecho=0.8:0.75:90|160|240:0.35|0.25|0.15",
    "echo": "aecho=0.8:0.85:320|640:0.4|0.2",
    "stadium": "aecho=0.8:0.8:180|380|620:0.45|0.3|0.2",
}
VOICE_FX = {
    "clear": EQ_PRESETS["voice"] + ["acompressor=threshold=0.08:ratio=3:attack=10:release=150:makeup=1.5"],
    "deep": ["rubberband=pitch=0.82:formant=preserved", "bass=g=3:f=120"],
    "high": ["rubberband=pitch=1.26:formant=preserved"],
    "radio": EQ_PRESETS["radio"],
    "telephone": EQ_PRESETS["telephone"],
    "echo": [ECHO_PRESETS["echo"]],
    "hall": [ECHO_PRESETS["hall"]],
    "robot": ["afftfilt=real='hypot(re,im)*cos(0)':imag='hypot(re,im)*sin(0)':win_size=512:overlap=0.75", "aecho=0.8:0.7:12:0.5"],
}
COMPRESS = {
    "light": "acompressor=threshold=0.18:ratio=2:attack=20:release=250:makeup=1.2",
    "punchy": "acompressor=threshold=0.1:ratio=4:attack=8:release=120:makeup=2",
    "broadcast": "acompressor=threshold=0.06:ratio=6:attack=5:release=200:makeup=2.6",
}


def pitch_filter(semitones: float) -> list[str]:
    if abs(semitones) < 0.05:
        return []
    return [f"rubberband=pitch={2 ** (semitones / 12):.5f}:formant=preserved"]


def original_fx(request: PostEditRequest) -> list[str]:
    out = pitch_filter(request.original_pitch) + EQ_PRESETS.get(request.original_eq, [])
    if request.original_echo != "off":
        out.append(ECHO_PRESETS[request.original_echo])
    return out


def voice_bus_fx(request: PostEditRequest) -> list[str]:
    out = list(VOICE_FX.get(request.voice_fx, []))
    if request.voice_deess:
        out.append("deesser=i=0.6:m=0.5:f=0.5")
    return out


def master_fx(request: PostEditRequest) -> list[str]:
    out = []
    if request.master_bass:
        out.append(f"bass=g={request.master_bass:.1f}:f=100")
    if request.master_treble:
        out.append(f"treble=g={request.master_treble:.1f}:f=3500")
    if request.master_compress != "off":
        out.append(COMPRESS[request.master_compress])
    if abs(request.stereo_width - 1) > 0.01:
        if request.stereo_width < 0.05:
            out.append("pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1")
        else:
            out.append(f"extrastereo=m={request.stereo_width:.2f}:c=1")
    return out


def video_untouched(request: PostEditRequest, source: SourceInfo, trim_start: float, trim_end: float) -> bool:
    """True when the edit changes nothing in the picture (captions/overlays are
    checked by the caller), so the video stream can be copied as is."""
    whole = trim_start < 0.01 and trim_end >= (source.video_duration or source.duration) - 0.05
    return (whole and abs(request.speed - 1) < 1e-3 and not request.reverse and not request.hold_end
            and not request.fade_in and not request.fade_out and not request.stabilize and not request.crop
            and not request.flip_h and not request.flip_v and not request.rotate and not (request.width and request.height)
            and not request.fps and not request.interpolate and not request.texts
            and (request.brightness, request.contrast, request.saturation, request.gamma, request.hue) == (0, 1, 1, 1, 0)
            and request.temperature == 6500 and not request.sharpen and not request.denoise and not request.grain
            and not request.vignette and not effects_on(request))


def output_size(request: PostEditRequest, source: SourceInfo) -> tuple[int, int]:
    """The rendered frame size (same arithmetic as the crop/scale filters)."""
    if request.width and request.height:
        return request.width, request.height
    width, height = source.width, source.height
    if request.crop:
        width = int(width * request.crop.w / 2) * 2
        height = int(height * request.crop.h / 2) * 2
    if request.rotate in (90, 270):
        width, height = height, width
    return max(2, width // 2 * 2), max(2, height // 2 * 2)


def _ass_time(seconds: float) -> str:
    centis = max(0, int(round(seconds * 100)))
    hours, rest = divmod(centis, 360_000)
    minutes, rest = divmod(rest, 6000)
    secs, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "∖").replace("{", "(").replace("}", ")").replace("\r", "").replace("\n", "\\N")


def _ass_inline_color(hex_color: str) -> str:
    return f"&H{hex_color[5:7]}{hex_color[3:5]}{hex_color[1:3]}&".upper()


def _tilt(index: int) -> float:
    """A small deterministic 'random' angle per caption chunk / text."""
    return round(((index * 7919) % 9 - 4) * 0.9, 1)


POP_IN = r"\fscx35\fscy35\t(0,90,\fscx118\fscy118)\t(90,170,\fscx100\fscy100)"


def pop_caption_events(request: PostEditRequest, words: list[tuple[float, float, str]], length: float) -> list[str]:
    """TikTok-style captions: `pop_words` words at a time (never across a
    sentence end or a pause), each group popping in, the spoken word colored."""
    groups: list[list[tuple[float, float, str]]] = []
    for index, word in enumerate(words):
        if not groups or len(groups[-1]) >= request.pop_words or re.search(r"[.!?…]$", groups[-1][-1][2]) \
                or word[0] - groups[-1][-1][1] > 0.5:
            groups.append([])
        groups[-1].append(word)

    def token(text: str) -> str:
        text = re.sub(r"[.,;:]+$", "", text) or text  # punchier without trailing periods/commas
        return _ass_text(text.upper() if request.pop_uppercase else text)

    highlight = _ass_inline_color(request.pop_highlight)
    base = _ass_inline_color(request.caption_color)
    events = []
    for number, group in enumerate(groups):
        next_start = groups[number + 1][0][0] if number + 1 < len(groups) else length
        group_end = min(next_start, group[-1][1] + 0.5, length)
        tilt = rf"\frz{_tilt(number)}" if request.pop_tilt else ""
        for position, word in enumerate(group):
            start = group[0][0] if position == 0 else word[0]
            end = group[position + 1][0] if position + 1 < len(group) else group_end
            if end - start < 0.01:
                continue
            text = " ".join(
                f"{{\\c{highlight}}}{token(w[2])}{{\\c{base}}}" if i == position else token(w[2])
                for i, w in enumerate(group)
            )
            tags = (POP_IN if position == 0 else "") + tilt
            events.append(f"Dialogue: 1,{_ass_time(start)},{_ass_time(end)},Pop,,0,0,0,,{{{tags}}}{text}")
    return events


def overlay_ass(request: PostEditRequest, clips: list[AudioClip], length: float, size: tuple[int, int]) -> str:
    """ASS script for the pop captions and pop text overlays (empty if neither)."""
    width, height = size
    styles, events = [], []
    if request.captions and request.caption_style == "pop":
        px = max(8, round(request.caption_size / 288 * height))
        align = {"bottom": 2, "middle": 5, "top": 8}[request.caption_position]
        margin = 0 if request.caption_position == "middle" else round(height * 0.14)
        styles.append(
            f"Style: Pop,DejaVu Sans,{px},{_ass_color(request.caption_color)},&H000000FF,&H00000000,&H64000000,"
            f"-1,0,0,0,100,100,0,0,1,{max(2, round(px * 0.13))},{max(1, round(px * 0.05))},{align},"
            f"{round(width * 0.06)},{round(width * 0.06)},{margin},1"
        )
        events += pop_caption_events(request, timeline_words(clips, length), length)
    for index, overlay in enumerate(request.texts):
        if not overlay.pop:
            continue
        end = overlay.end if overlay.end is not None and overlay.end > overlay.start else length
        row = {"top": 7, "middle": 4, "bottom": 1}[overlay.position]
        align = row + {"left": 0, "center": 1, "right": 2}[overlay.align]
        margin = {"top": round(height * 0.06), "middle": 0, "bottom": round(height * 0.08)}[overlay.position]
        border = (f"3,{max(6, overlay.size // 4)},0" if overlay.box
                  else f"1,{max(2, overlay.size // 16) if overlay.outline else 0},{max(1, overlay.size // 30)}")
        outline_color = "&H73000000" if overlay.box else "&H00000000"
        styles.append(
            f"Style: T{index},DejaVu Sans,{overlay.size},{_ass_color(overlay.color)},&H000000FF,{outline_color},&H64000000,"
            f"-1,0,0,0,100,100,0,0,{border},{align},{round(width * 0.05)},{round(width * 0.05)},{margin},1"
        )
        tilt = rf"\frz{_tilt(index + 3)}"
        events.append(
            f"Dialogue: 2,{_ass_time(overlay.start)},{_ass_time(end)},T{index},,0,0,0,,"
            f"{{{POP_IN}\\fad(0,120){tilt}}}{_ass_text(overlay.text)}"
        )
    if not events:
        return ""
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        *styles, "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events, "",
    ])


def combined_captions(clips: list[AudioClip], length: float) -> str:
    cues = []
    for clip in clips:
        if not (clip.captions and clip.srt):
            continue
        for start, end, text in parse_srt(clip.srt):
            start, end = start + clip.start, min(end + clip.start, length)
            if start < length and end > start:
                cues.append((start, end, text))
    cues.sort()
    # edge-tts sentence cues overlap by ~50ms; libass would stack the next
    # line above the previous one (and keep it raised), so end each cue where
    # the next one starts.
    for index in range(len(cues) - 1):
        start, end, text = cues[index]
        if end > cues[index + 1][0]:
            cues[index] = (start, max(start + 0.05, cues[index + 1][0] - 0.001), text)
    return "".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(cues, 1))


def source_caption_clip(request: PostEditRequest, source_path: Path, source: SourceInfo,
                        cues: list[list], words: list[list]) -> AudioClip:
    """Map Whisper's source timestamps onto the trimmed, speed-adjusted output."""
    trim_start, trim_end, _ = output_duration(request, source)

    def map_items(items: list[list]) -> list[tuple[float, float, str]]:
        mapped = []
        for start, end, text in items:
            start, end = max(float(start), trim_start), min(float(end), trim_end)
            if end > start and text.strip():
                mapped.append(((start - trim_start) / request.speed,
                               (end - trim_start) / request.speed, text.strip()))
        return mapped

    mapped_cues = map_items(cues)
    mapped_words = map_items(words) if words else estimate_words(mapped_cues)
    srt = "".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{text}\n\n"
                  for i, (a, b, text) in enumerate(mapped_cues, 1))
    return AudioClip(source_path, 0, 0, srt, True,
                     [[a, b, text] for a, b, text in mapped_words])


def build(
    request: PostEditRequest, source_path: Path, source: SourceInfo, workdir: Path, output_path: Path,
    music: tuple[Path, float, float | None] | None, voices: list[AudioClip],
    original_audio: Path | None = None, intermediate: bool = False,
    caption_clips: list[AudioClip] | None = None,
) -> tuple[list[str], float]:
    """The ffmpeg argv (run it with cwd=workdir) and the output length in seconds.
    original_audio replaces the source's own soundtrack (the music-removed
    version from remove_music); it covers the whole untrimmed source."""
    trim_start, trim_end, length = output_duration(request, source)
    inputs: list[str] = [*gpu.decode_args(), "-i", str(source_path)]
    next_input = 1
    graph: list[str] = []

    # ---------------------------------------------------------------- video
    video = [f"trim=start={trim_start:.4f}:end={trim_end:.4f}", "setpts=PTS-STARTPTS"]
    if request.reverse:
        video.append("reverse")
    if abs(request.speed - 1) > 1e-3:
        video.append(f"setpts=PTS/{request.speed:.5f}")
    if request.stabilize:
        video.append("deshake")
    if request.crop:
        c = request.crop
        video.append(f"crop=w='floor(iw*{c.w:.5f}/2)*2':h='floor(ih*{c.h:.5f}/2)*2':x='iw*{c.x:.5f}':y='ih*{c.y:.5f}'")
    if request.flip_h:
        video.append("hflip")
    if request.flip_v:
        video.append("vflip")
    video += {90: ["transpose=1"], 180: ["hflip", "vflip"], 270: ["transpose=2"]}.get(request.rotate, [])
    if request.width and request.height:
        video.append(f"scale={request.width}:{request.height}:force_original_aspect_ratio=decrease:flags=lanczos")
        video.append(f"pad={request.width}:{request.height}:(ow-iw)/2:(oh-ih)/2")
    else:
        video.append("scale='trunc(iw/2)*2':'trunc(ih/2)*2'")
    video.append("setsar=1")
    if request.denoise > 0:
        d = request.denoise
        video.append(f"hqdn3d={d:.2f}:{d * 0.75:.2f}:{d * 1.5:.2f}:{d * 1.1:.2f}")
    if (request.brightness, request.contrast, request.saturation, request.gamma) != (0, 1, 1, 1):
        video.append(
            f"eq=brightness={request.brightness:.3f}:contrast={request.contrast:.3f}"
            f":saturation={request.saturation:.3f}:gamma={request.gamma:.3f}"
        )
    if request.hue:
        video.append(f"hue=h={request.hue:.1f}")
    if request.temperature != 6500:
        video.append(f"colortemperature=temperature={request.temperature}")
    if request.sharpen:
        video.append(f"unsharp=5:5:{request.sharpen:.2f}:5:5:0")
    if request.grain:
        video.append(f"noise=alls={request.grain}:allf=t")
    if request.vignette > 0:
        video.append(f"vignette=angle={0.2 + request.vignette * 1.2:.3f}")
    # The zoom runs over the moving part (the hold after it stays at the last zoom).
    video += effect_filters(request, output_size(request, source), max(0.1, length - request.hold_end), source.fps)
    if request.fps:
        if request.interpolate:
            video.append(f"minterpolate=fps={request.fps}:mi_mode=mci:mc_mode=aobmc:vsbmc=1")
        else:
            video.append(f"fps={request.fps}")
    elif abs(request.speed - 1) > 1e-3:
        video.append(f"fps={source.fps:.3f}")  # setpts alone would leave source_fps*speed
    elif request.hold_end > 0:
        # tpad only clones the last frame when an fps filter comes before it:
        # after trim/setpts alone, ffmpeg 7.1 ended the video at the source's
        # end and the hold was silently dropped (audio ran past the picture;
        # found 2026-10-01 on a 9.5 s "Fit voice" hold).
        video.append(f"fps={source.fps:.3f}")
    if request.hold_end > 0:
        video.append(f"tpad=stop_mode=clone:stop_duration={request.hold_end:.3f}")
    if request.fade_in > 0:
        video.append(f"fade=t=in:st=0:d={min(request.fade_in, length):.3f}:color={request.fade_color}")
    if request.fade_out > 0:
        fade = min(request.fade_out, length)
        video.append(f"fade=t=out:st={length - fade:.3f}:d={fade:.3f}:color={request.fade_color}")

    for index, overlay in enumerate(request.texts):
        if overlay.pop:
            continue  # rendered by the ASS pass below
        (workdir / f"text{index}.txt").write_text(overlay.text)
        start = overlay.start
        end = overlay.end if overlay.end is not None and overlay.end > start else length
        x = {"left": "w*0.05", "center": "(w-text_w)/2", "right": "w-text_w-w*0.05"}[overlay.align]
        y = {"top": "h*0.06", "middle": "(h-text_h)/2", "bottom": "h-text_h-h*0.08"}[overlay.position]
        options = [
            f"fontfile={FONT_FILE}", f"textfile=text{index}.txt", "expansion=none", f"fontsize={overlay.size}",
            f"fontcolor=0x{overlay.color[1:]}", f"x={x}", f"y={y}", f"enable='between(t,{start:.3f},{end:.3f})'",
        ]
        if overlay.outline:
            options += [f"borderw={max(2, overlay.size // 16)}", "bordercolor=black"]
        if overlay.box:
            options += ["box=1", "boxcolor=black@0.55", f"boxborderw={max(6, overlay.size // 4)}"]
        if overlay.fade:
            ramp = max(0.01, min(0.3, (end - start) / 3))  # a start at/after the end gave 0 -> divide by zero
            options.append(
                f"alpha='if(lt(t,{start + ramp:.3f}),(t-{start:.3f})/{ramp:.3f},if(gt(t,{end - ramp:.3f}),({end:.3f}-t)/{ramp:.3f},1))'"
            )
        video.append("drawtext=" + ":".join(options))

    caption_clips = voices if caption_clips is None else caption_clips
    captions = combined_captions(caption_clips, length) if request.captions and request.caption_style == "classic" else ""
    if captions:
        (workdir / "captions.srt").write_text(captions)
        alignment = {"bottom": 2, "middle": 5, "top": 8}[request.caption_position]
        style = (
            f"FontName=DejaVu Sans,Bold=1,FontSize={request.caption_size},PrimaryColour={_ass_color(request.caption_color)},"
            f"OutlineColour=&H00000000,BackColour=&H80000000,BorderStyle=1,Outline=2,Shadow=1,Alignment={alignment},MarginV=24"
        )
        video.append(f"subtitles=captions.srt:force_style='{style}'")
    script = overlay_ass(request, caption_clips, length, output_size(request, source))
    if script:
        (workdir / "overlays.ass").write_text(script)
        video.append("ass=overlays.ass")
    out_size = output_size(request, source)
    # Audio-only edits (music, voice-over, leveling, music removal) leave the
    # picture untouched: copy the video stream - instant, lossless, no encode.
    copy_video = not captions and not script and video_untouched(request, source, trim_start, trim_end)
    if copy_video:
        inputs[: len(gpu.decode_args())] = []  # nothing to decode
    else:
        video[-1] += gpu.upload(*out_size)  # GPU encode: frames go back to the GPU after the CPU-only filters
        graph.append("[0:v]" + ",".join(video) + "[vout]")

    # ---------------------------------------------------------------- audio
    original_label = "0:a"
    if original_audio is not None and source.has_audio:
        inputs += ["-i", str(original_audio)]
        original_label = f"{next_input}:a"
        next_input += 1
    if source.has_audio and not request.mute_original and request.original_volume > 0:
        original = [f"atrim=start={trim_start:.4f}:end={trim_end:.4f}", "asetpts=PTS-STARTPTS", SAMPLE_FORMAT]
        if request.reverse:
            original.append("areverse")
        original += atempo_chain(request.speed)
        if request.audio_denoise:
            original.append("afftdn=nf=-25")
        original += original_fx(request)
        original += [f"volume={request.original_volume:.3f}", "apad", f"atrim=duration={length:.4f}"]
        graph.append(f"[{original_label}]" + ",".join(original) + "[orig]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{length:.4f}", "-i", "anullsrc=r=48000:cl=stereo"]
        graph.append(f"[{next_input}:a]{SAMPLE_FORMAT}[orig]")
        next_input += 1
    bed = "orig"

    if music:
        music_path, music_trim_start, music_trim_end = music
        inputs += ["-ss", f"{music_trim_start:.3f}"]
        if music_trim_end is not None:
            inputs += ["-t", f"{max(0.1, music_trim_end - music_trim_start):.3f}"]
        inputs += ["-i", str(music_path)]
        span = max(0.1, length - request.music_start)
        chain = [SAMPLE_FORMAT]
        if request.music_loop:
            chain.append("aloop=loop=-1:size=2000000000")
        chain += ["asetpts=N/SR/TB", f"atrim=duration={span:.4f}"]
        if request.music_fade_in > 0:
            chain.append(f"afade=t=in:st=0:d={min(request.music_fade_in, span):.3f}")
        if request.music_fade_out > 0:
            fade = min(request.music_fade_out, span)
            chain.append(f"afade=t=out:st={span - fade:.3f}:d={fade:.3f}")
        chain.append(f"volume={request.music_volume:.3f}")
        if request.music_start > 0:
            chain.append(f"adelay={int(request.music_start * 1000)}:all=1")
        graph.append(f"[{next_input}:a]" + ",".join(chain) + "[music]")
        next_input += 1
        graph.append("[orig][music]amix=inputs=2:duration=first:normalize=0[bed]")
        bed = "bed"

    voice_labels = []
    for clip in voices:
        inputs += ["-i", str(clip.path)]
        chain = [SAMPLE_FORMAT, f"volume={clip.volume:.3f}"]
        if clip.start > 0:
            chain.append(f"adelay={int(clip.start * 1000)}:all=1")
        label = f"voice{len(voice_labels)}"
        graph.append(f"[{next_input}:a]" + ",".join(chain) + f"[{label}]")
        voice_labels.append(label)
        next_input += 1

    if voice_labels:
        # The voice bus is padded to the full length: sidechaincompress stops
        # when its key input ends, which cut the WHOLE soundtrack (music and
        # original audio too) off at the last voice clip's end.
        pad = ",".join([*voice_bus_fx(request), "apad", f"atrim=duration={length:.4f}"])
        if len(voice_labels) > 1:
            graph.append("".join(f"[{v}]" for v in voice_labels) + f"amix=inputs={len(voice_labels)}:duration=longest:normalize=0,{pad}[vo]")
        else:
            graph.append(f"[{voice_labels[0]}]{pad}[vo]")
        if request.duck and request.duck_strength > 0:
            ratio = 2 + 18 * request.duck_strength
            graph.append("[vo]asplit=2[vo_mix][vo_key]")
            graph.append(f"[{bed}][vo_key]sidechaincompress=threshold=0.015:ratio={ratio:.1f}:attack=15:release=400[ducked]")
            graph.append("[ducked][vo_mix]amix=inputs=2:duration=first:normalize=0[mixed]")
        else:
            graph.append(f"[{bed}][vo]amix=inputs=2:duration=first:normalize=0[mixed]")
        bed = "mixed"

    post = []
    if request.fade_in > 0:
        post.append(f"afade=t=in:st=0:d={min(request.fade_in, length):.3f}")
    if request.fade_out > 0:
        fade = min(request.fade_out, length)
        post.append(f"afade=t=out:st={length - fade:.3f}:d={fade:.3f}")
    post += master_fx(request)
    if request.normalize:
        post.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    post += ["alimiter=limit=0.97:level=disabled", "aresample=48000", f"atrim=duration={length:.4f}"]
    graph.append(f"[{bed}]" + ",".join(post) + "[aout]")

    video_codec = ["-c:v", "copy"] if copy_video else None
    if intermediate:
        # Pass 1 of a "fade into another video" edit: near-lossless video and
        # lossless audio, so the leveling measurement and the final encode in
        # append_args() work from the edit as rendered, not a lossy copy.
        encode = [*(video_codec or gpu.h264(CRF["near_lossless"], *out_size)), "-c:a", "flac"]
    else:
        encode = [*(video_codec or gpu.h264(CRF[request.quality], *out_size)),
                  "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    args = [
        "ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1", *([] if copy_video else gpu.init_args()), *inputs,
        "-filter_complex", ";".join(graph), "-map", "0:v:0" if copy_video else "[vout]", "-map", "[aout]",
        *encode, "-t", f"{length:.4f}", str(output_path),
    ]
    return args, length


def append_args(request: PostEditRequest, edited_path: Path, edited: SourceInfo, next_path: Path, next_info: SourceInfo,
                gain_db: float, output_path: Path) -> tuple[list[str], float]:
    """Pass 2 of "fade into another video": the fully rendered edit (pass 1)
    joined to the next video with an xfade + acrossfade. The next video is
    fitted to the edit's frame size and frame rate (xfade needs both
    identical), plays unedited, and gets gain_db (next_video_gain: the level
    that matches the edit as rendered) as one static gain, so its own mix is
    kept. Returns the ffmpeg argv and the output length."""
    # Timed on the video streams: when a file's audio runs longer than its
    # picture, the format duration would put the xfade after the last frame
    # (no transition at all). Each audio is padded/trimmed to its video.
    length = edited.video_duration or edited.duration
    next_length = next_info.video_duration or next_info.duration
    width, height, fps = edited.width, edited.height, edited.fps or 24.0
    overlap = max(0.05, min(request.next_transition_duration, length - 0.05, next_length - 0.05))
    total = length + next_length - overlap
    inputs = [*gpu.decode_args(), "-i", str(edited_path), *gpu.decode_args(), "-i", str(next_path)]
    graph = [
        f"[0:v]fps={fps:.3f},settb=AVTB[vm]",
        f"[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:.3f},format=yuv420p,settb=AVTB[vn]",
    ]
    tail = atail = ""
    if request.next_fade_out > 0:
        fade = min(request.next_fade_out, next_length)
        tail = f",fade=t=out:st={total - fade:.3f}:d={fade:.3f}:color={request.fade_color}"
        atail = f",afade=t=out:st={total - fade:.3f}:d={fade:.3f}"
    graph.append(f"[vm][vn]xfade=transition={request.next_transition}:duration={overlap:.3f}:offset={length - overlap:.3f}{tail}{gpu.upload(width, height)}[vout]")
    if edited.has_audio:
        graph.append(f"[0:a]{SAMPLE_FORMAT},apad,atrim=duration={length:.4f}[am]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{length:.4f}", "-i", "anullsrc=r=48000:cl=stereo"]
        graph.append(f"[2:a]{SAMPLE_FORMAT}[am]")
    if next_info.has_audio:
        gain = f"volume={gain_db:.2f}dB," if abs(gain_db) >= 0.05 else ""
        graph.append(f"[1:a]{SAMPLE_FORMAT},{gain}apad,atrim=duration={next_length:.4f}[an]")
    else:
        silent = inputs.count("-i")  # index of the input added next
        inputs += ["-f", "lavfi", "-t", f"{next_length:.4f}", "-i", "anullsrc=r=48000:cl=stereo"]
        graph.append(f"[{silent}:a]{SAMPLE_FORMAT}[an]")
    graph.append(f"[am][an]acrossfade=d={overlap:.3f}:c1=tri:c2=tri{atail},"
                 f"alimiter=limit=0.97:level=disabled,aresample=48000,atrim=duration={total:.4f}[aout]")
    args = [
        "ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1", *gpu.init_args(), *inputs,
        "-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]",
        *gpu.h264(CRF[request.quality], width, height),
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{total:.4f}", str(output_path),
    ]
    return args, total


def summary(request: PostEditRequest) -> str:
    """Short human label for the job card."""
    parts = []
    if request.trim_start or request.trim_end is not None:
        parts.append("trim")
    if abs(request.speed - 1) > 1e-3:
        parts.append(f"{request.speed:g}x")
    if request.reverse:
        parts.append("reverse")
    if request.crop:
        parts.append("crop")
    if request.brightness or request.contrast != 1 or request.saturation != 1 or request.gamma != 1 or request.hue or request.temperature != 6500:
        parts.append("color")
    if effects_on(request):
        parts.append("effects")
    if (request.original_eq != "off" or request.original_echo != "off" or abs(request.original_pitch) >= 0.05
            or request.voice_fx != "off" or request.voice_deess or request.master_bass or request.master_treble
            or request.master_compress != "off" or abs(request.stereo_width - 1) > 0.01):
        parts.append("audio fx")
    if request.remove_music != "off":
        parts.append("voices only" if request.remove_music == "voices" else "music removed")
    if request.next_job_id or request.next_reference_id:
        parts.append("fade into next video")
    if request.music_reference_id:
        parts.append("music")
    if request.voices:
        parts.append(f"{len(request.voices)} voice clip{'s' * (len(request.voices) > 1)}")
    if request.captions:
        parts.append(("Whisper " if request.caption_source == "whisper" else "") +
                     ("pop captions" if request.caption_style == "pop" else "captions"))
    if request.texts:
        parts.append(f"{len(request.texts)} text")
    return ", ".join(parts) or "re-encode"
