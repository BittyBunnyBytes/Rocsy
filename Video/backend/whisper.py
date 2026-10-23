"""Whisper transcription of a Post-Edit source before its music mix.

GPU only: the transcription runs as ComfyUI's BunnyTranscribe node (openai-whisper large-v3 on the GPU,
queued behind renders, loaded per run and freed). There is no CPU fallback; when the node or ComfyUI is
unavailable the transcription fails with that reason. app.py passes the runner in (it owns the ComfyUI
client); this module keeps the cache and turns the node's JSON into caption cues and word timings.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

MODEL_NAME = "openai-whisper-large-v3-gpu"
# Transcripts cached by the old CPU path are still valid text: keep using them.
CACHED_MODELS = {MODEL_NAME, "Systran/faster-whisper-large-v3"}
_transcription_lock = asyncio.Lock()

# (source video, name for the node's files) -> the node's JSON: language, segments (with words).
# (source video, name for the node's files, isolate the vocals first) -> the node's JSON.
GpuRunner = Callable[[Path, str, bool], Awaitable[dict[str, Any]]]

# Whisper writes what it hears that isn't words as markers: "♪ ... ♪", "🎵 Music Playing 🎵",
# "[Music]", "(upbeat music)", "*laughs*". Captions show only sung or spoken words.
_MARKS = re.compile(r"[\u2669-\u266f\U0001F3B5\U0001F3B6\U0001F3BC\U0001F3A4\U0001F3A7\U0001F3B8\U0001F3B9\U0001F3BA\U0001F3BB\U0001F941]")
_TAG = re.compile(r"\[[^\]]{0,60}\]|\([^)]{0,60}\)|\*[^*]{0,60}\*")
_NON_SPEECH = re.compile(r"(?i)^(?:(?:upbeat|soft|gentle|dramatic|background|intense|instrumental|tropical|electronic|"
                         r"cheerful|calm|sad|happy|light|loud|quiet|fast|slow|ominous|suspenseful|dance|pop|rock)\s+)*"
                         r"(?:music(?:\s+playing|\s+continues|\s+fades(?:\s+out)?|\s+ends)?|instrumental|applause|silence|"
                         r"no\s+speech|laughs?|laughter|singing|humming|vocali[sz]ing|cheering|outro|intro)$")
_EDGE = " \t-\u2013\u2014.,;:!?\"'"


def clean_text(text: str) -> str:
    """Words only: music symbols, [tags] / (tags) / *tags* and lines like "Music Playing" removed."""
    text = _TAG.sub(" ", _MARKS.sub(" ", str(text or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return "" if not text.strip(_EDGE) or _NON_SPEECH.match(text.strip(_EDGE)) else text


def clean(result: dict[str, Any]) -> dict[str, Any]:
    """A transcript with only real words: cleaned cues, and the words that fall inside them."""
    cues = []
    for start, end, text in result.get("cues") or []:
        if line := clean_text(text):
            cues.append([start, end, line])
    words, depth = [], 0
    for start, end, word in result.get("words") or []:
        token = _MARKS.sub("", str(word))
        opened, closed = token.count("[") + token.count("("), token.count("]") + token.count(")")
        inside = depth > 0 or opened > 0
        depth = max(0, depth + opened - closed)
        token = clean_text(token) if not inside else ""
        if token and any(a - 0.05 <= float(start) and float(end) <= b + 0.05 for a, b, _ in cues):
            words.append([start, end, token])
    return {**result, "cues": cues, "words": words}


def cache_path(data_dir: Path, job_id: str, vocals: bool = False) -> Path:
    return data_dir / "postedit-transcripts" / f"{job_id}{'-vocals' if vocals else ''}.json"


def cached(data_dir: Path, job_id: str, source: Path, vocals: bool = False) -> dict[str, Any] | None:
    path = cache_path(data_dir, job_id, vocals)
    try:
        value = json.loads(path.read_text())
        stat = source.stat()
        if value.get("source_size") == stat.st_size and value.get("source_mtime_ns") == stat.st_mtime_ns \
                and value.get("model") in CACHED_MODELS:
            return clean(value)          # older caches still hold the markers
    except (OSError, ValueError, TypeError):
        pass
    return None


def _shape(node: dict[str, Any]) -> dict[str, Any]:
    """The node's segments/words as Post-Edit's cues and words ([start, end, text] lists)."""
    cues: list[list[Any]] = []
    words: list[list[Any]] = []
    for segment in node.get("segments") or []:
        start, end = float(segment.get("start", 0)), float(segment.get("end", 0))
        line = str(segment.get("text") or "").strip()
        if not line or end <= start or segment.get("no_speech_prob", 0) >= 0.6:
            continue
        cues.append([round(start, 3), round(end, 3), line])
        timed = [w for w in segment.get("words") or []
                 if str(w.get("word") or "").strip() and float(w.get("end", 0)) > float(w.get("start", 0))]
        if timed:
            words.extend([round(float(w["start"]), 3), round(float(w["end"]), 3), str(w["word"]).strip()] for w in timed)
        else:
            # A node loaded before word timestamps were added: spread the words over the segment by length.
            tokens = line.split()
            total = sum(len(t) for t in tokens) or 1
            at = start
            for token in tokens:
                step = (end - start) * len(token) / total
                words.append([round(at, 3), round(at + step, 3), token])
                at += step
    return {"model": MODEL_NAME, "language": node.get("language"), "cues": cues, "words": words}


async def transcribe(data_dir: Path, job_id: str, source: Path, gpu: GpuRunner, vocals: bool = False) -> dict[str, Any]:
    """vocals=True splits the vocals from the music on the GPU first (song lyrics); cached separately."""
    result = cached(data_dir, job_id, source, vocals)
    if result is not None:
        return result
    async with _transcription_lock:
        result = cached(data_dir, job_id, source, vocals)
        if result is not None:
            return result
        result = clean(_shape(await gpu(source, f"postedit-tr-{job_id}{'-v' if vocals else ''}", vocals)))
        result["vocals_isolated"] = vocals
        stat = source.stat()
        result.update(source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns)
        path = cache_path(data_dir, job_id, vocals)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False))
        os.replace(temporary, path)
        return result
