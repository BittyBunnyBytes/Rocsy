"""Auto-tagging: caption every clip/image in a dataset with a llama-swap vision model.

A tag job takes the shared GPU queue (same lock as training), asks ComfyUI to free its VRAM,
pulls N evenly spaced, timestamped frames per clip on the GPU (VAAPI; 8 by default, 3-24), sends them to the vision model with the tagging
system prompt, cleans the reply into a caption and writes <clip>.txt. When the job ends the
model is unloaded from llama-swap so VRAM is free again for ComfyUI.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import base64
import json
import math
import os
import random
import re
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

import httpx
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import caption_guides as guides
import families

LLAMA_URL = os.getenv("LLAMA_URL", "http://127.0.0.1:8400").rstrip("/")
LLAMA_KEY = os.getenv("LLAMA_API_KEY", "")
LLAMA_CONFIG = Path(os.getenv("LLAMA_CONFIG", "/llama-config.yaml"))
LLAMA_OVERRIDE_DIR = Path(os.getenv("LLAMA_OVERRIDE_DIR", "/llama-overrides"))
STRATA_BRIDGE = "strata-swap-bridge"   # Strata entries: vision via its own encoder, not --mmproj
COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
VIDEO = {".mp4", ".mov", ".webm", ".mkv"}
IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VAAPI = ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi"]

# Rules that work for any LoRA: characters, people, objects, styles, motion, real or animated footage.
DEFAULT_SYSTEM_PROMPT = """You write training captions for a video/image LoRA (MiniMax H3 and similar models). Each request shows you frames from ONE short clip (in time order) or a single image. Reply with the caption only.

The caption teaches the model what the trigger word means. The trigger word stands for the concept the LoRA teaches (given below as "LoRA teaches", if known). Describe everything else that is visible clearly and literally, so the model learns that those things can change while the concept stays.

Write the caption like this:
1. Start with the trigger word, then a comma. Example: "mytrigger, ...". If there is no trigger word, just start with the subject.
2. Subject: who or what is in it. Name well-known characters, people or objects only if you are sure; otherwise describe them plainly (age group, build, hair, clothing, colours, species, material). If there are several subjects, say how many and tell them apart.
3. Action: compare the frames one after another (they are evenly spaced and timestamped) to work out the motion. Describe what happens in order, with plain, precise verbs: direction (toward/away from the camera, left to right, up, around), speed (slowly, quickly, suddenly), body parts that move (turns her head, raises both arms, kicks), repeated motions (nods twice, bounces in place) and how things change (the door swings open, the cup tips over). If nothing moves, say so ("stands still and looks around").
4. Setting: where it is, plus a few clear background objects, time of day and lighting if obvious (dim kitchen at night, sunny beach, neon-lit street, plain white studio background).
5. Camera and look, only when clear: close-up, wide shot, static camera, camera pans/tracks/zooms, handheld; live-action footage, 3D render, 2D animation, illustration.
6. If a style sentence is given, end with it exactly, word for word.

What to leave out (it belongs to the trigger word):
- If the LoRA teaches a STYLE or look, do not describe that style in detail; describe the content (who, what, where) instead.
- If it teaches a CHARACTER or PERSON, use the trigger word as their name and still describe what changes between clips: outfit, pose, expression, action, setting.
- If it teaches a MOTION or effect, describe the motion plainly and precisely; it is the main thing to get right.

Rules:
- One paragraph, present tense, 25 to 60 words before the style sentence.
- Describe only what you can see. Do not guess sounds, names, places or story.
- Never write "the image shows", "in this frame", "the video", "frames", "scene 1" or similar.
- No lists, no quotation marks, no emojis, no hashtags, no notes after the caption.
- Use the trigger word exactly once, at the very start.

Examples:
- LoRA teaches a style (trigger inkw4sh, style sentence "Loose ink-and-watercolor illustration with visible paper texture."):
  inkw4sh, a red fox with a white-tipped tail trots across a snowy field toward a wooden fence, pauses and looks back over its shoulder, slow pan following it. Loose ink-and-watercolor illustration with visible paper texture.
- LoRA teaches a person (trigger j4ne):
  j4ne, a woman with shoulder-length red hair in a green raincoat, walks along a wet city sidewalk at night under neon signs, glances over her shoulder and smiles, handheld camera tracking beside her."""

# Booru-tag captions for image LoRAs (Anima, SDXL/Pony/Illustrious, SD 1.5): trigger first
# (keep_tokens 1), then identity traits, then what changes.
DEFAULT_TAGS_PROMPT = """You tag images for training an image LoRA. Each request shows ONE image. Reply with a single line of comma-separated tags and nothing else.

The trigger word stands for the concept the LoRA teaches (given below as "LoRA teaches", if known). Tag everything that is visible, clearly and literally, so the model learns which things can change while the concept stays.

How to tag:
1. Start with the trigger word, then a comma. If there is no trigger word, start with the subject count.
2. Subject count and type: 1girl, 1boy, 1other, 2girls, solo, no humans, and species when not human (anthro, coyote, pony, robot).
3. Identity traits: hair colour, length and style, eye colour, skin or fur colour, body type, markings, accessories that are always worn.
4. Clothing, item by item, with colours (black tank top, denim shorts, barefoot).
5. Pose and action (standing, sitting, leaning on wall, holding cup, arms crossed), expression and gaze (smile, closed eyes, looking at viewer).
6. Framing and angle (portrait, upper body, cowboy shot, full body, from above, from side, close-up).
7. Setting and background (outdoors, bedroom, graveyard, white background, simple background), lighting if obvious.
8. Medium or look only if notable (3d, sketch, pixel art, monochrome, photo).
9. If a style sentence is given, add it as the last tag, word for word.

What to leave out (it belongs to the trigger word): if the LoRA teaches a STYLE, do not tag that style; tag the content. If it teaches a CHARACTER, keep the same identity tags in every image (copy them from the example captions when given) and still tag everything that changes: outfit, pose, expression, setting.

Rules:
- Booru tags: lowercase, short (1-4 words), spaces instead of underscores, singular nouns.
- Only what you can see. No guesses about names, story or sounds; name a known character or series only if you are certain.
- No sentences, no quality or score tags (masterpiece, best quality, score_9, highres), no hashtags, no numbering, no notes.
- Use the trigger word exactly once, at the very start. Each tag only once.
- About 20-45 tags."""

DEFAULT_USER_TEMPLATE = """Trigger word: {trigger}
LoRA teaches: {concept}
Style sentence to end with: {style}
{what}
{examples}{existing}Return only the caption."""


# How to pick training clips from longer videos, as rules for a vision model:
# scene-detect, skip intro/outro, then keep only windows where the subject is clearly on screen
# from first to last frame, with no hidden cut, and prefer variety.
DEFAULT_CUT_PROMPT = """You are a strict clip scout choosing short training clips for a video LoRA. Each request shows the frames of ONE candidate window cut from a longer video, in time order (Frame 1 = start ... last frame = end). Judge EACH frame on its own, then the window. Reply with JSON only.

For every frame answer: is the subject (given as "Looking for") clearly visible in THIS frame? It counts only if a large part of the subject is inside the picture and recognisable. It does NOT count if the frame shows an empty room, a doorway, scenery or objects without the subject, if the subject is only a tiny speck, mostly cut off at the edge, hidden, or blurred by motion.

Then answer: are all frames the same continuous shot (same place and same camera setup)? A different room, angle or location in any frame means false.

Also reject (keep=false) windows that are mainly screens, phone or computer UIs, charts, title cards, intros, logos, credits, text, hands or objects without the subject, dashcam footage, black or white frames, fades or transitions.

Reply with exactly this JSON and nothing else (one true/false per frame, in order):
{"frames": [true, false, ...], "same_shot": true or false, "keep": true or false, "score": 0-10, "subject": "who/what is visible, 2-6 words", "reason": "one short sentence"}
Score 9-10: subject large, sharp, clearly doing something in every frame. 6-8: subject clear in every frame but small or static. 0-5: anything weaker."""

DEFAULT_CUT_TEMPLATE = """Looking for: {concept}
Candidate window {index} of {count}: {start:.1f}s to {end:.1f}s of the video. {nframes} frames follow (Frame 1 to Frame {nframes}).
Return the JSON with exactly {nframes} values in "frames"."""


class _SafeDict(dict):
    """str.format_map that leaves unknown {placeholders} in place instead of failing."""
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class TagRequest(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    trigger: str = Field(default="", max_length=64)
    style: str = Field(default="", max_length=400)
    concept: str = Field(default="", max_length=300)
    mode: Literal["missing", "all"] = "missing"
    files: list[str] | None = None
    unload_after: bool = True
    source: Literal["trainer", "hermes"] = "trainer"
    agents: int = Field(default=1, ge=1, le=16)
    frames: int = Field(default=8, ge=3, le=24)   # frames sampled per video clip
    target: str = Field(default="auto", max_length=60)   # caption_guides id, Trainer family id, or "auto"
    caption_format: Literal["auto", "tags", "natural"] = "auto"   # auto = the target model's own format


class WorkBody(BaseModel):
    agent: str = Field(min_length=1, max_length=60)


class TagSettings(BaseModel):
    system_prompt: str = Field(min_length=20, max_length=20000)
    user_template: str = Field(min_length=10, max_length=5000)
    default_model: str = Field(default="", max_length=120)
    cut_prompt: str | None = Field(default=None, max_length=20000)
    cut_template: str | None = Field(default=None, max_length=5000)
    tags_prompt: str | None = Field(default=None, max_length=20000)
    guide_overrides: dict[str, str] | None = None   # guide id -> edited guidance ("" = back to the default)


class CutRequest(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    concept: str = Field(default="", max_length=300)
    count: int = Field(default=8, ge=1, le=50)
    seconds: float = Field(default=56 / 24, ge=0.9, le=15)
    variable: bool = False          # random length per clip between min_seconds and max_seconds
    min_seconds: float = Field(default=2.0, ge=0.9, le=15)
    max_seconds: float = Field(default=5.0, ge=0.9, le=15)
    h3: bool = True                 # variable lengths snap to H3 frame counts (17n+5 at 24 fps)
    staged: bool = False
    scene_mode: bool = False        # sample detected shots instead of a dense timeline scan
    agents: int = Field(default=0, ge=0, le=16)      # 0 = as many as the model has slots
    unload_after: bool = True
    source: Literal["trainer", "hermes"] = "trainer"



MAX_CUT_WINDOWS = 120


def plan_cut_windows(duration: float, cuts: list[float], lengths: list[float], seed: str,
                     scene_mode: bool = False) -> tuple[list[tuple[float, float]], float, float]:
    """Sample each detected shot, or scan densely across the full timeline."""
    lo, hi = (6.0, duration - 12.0) if duration > 60 else (0.0, duration)
    typical = sum(lengths) / len(lengths)
    stride = max(3.5, typical * 1.2)
    rng = random.Random(seed)
    bounds = [0.0, *sorted({cut for cut in cuts if 0 < cut < duration}), duration]
    windows: list[tuple[float, float]] = []
    for shot_start, shot_end in zip(bounds, bounds[1:]):
        start, end = max(shot_start, lo) + 0.2, min(shot_end, hi) - 0.2
        span = end - start
        fits = [length for length in lengths if length <= span + 1e-6]
        if not fits:
            continue
        count = (2 if span >= 3 * max(lengths) else 1) if scene_mode else max(1, math.ceil(span / stride))
        scene_fits = [length for length in fits if length <= span / count] or fits
        for index in range(count):
            length = rng.choice(scene_fits if scene_mode else fits)
            centre = start + span * (index + 0.5) / count
            window_start = max(start, min(centre - length / 2, end - length))
            windows.append((round(window_start, 3), round(window_start + length, 3)))
    limit = 60 if scene_mode else MAX_CUT_WINDOWS
    if len(windows) > limit:
        step = len(windows) / limit
        windows = [windows[int((index + 0.5) * step)] for index in range(limit)]
    return windows, lo, hi

def strata_vision_on(model_id: str) -> bool:
    """Strata's Vision parameter (Llama → Parameters): on unless <id>.args stores "--vision off"."""
    with suppress(OSError):
        for line in (LLAMA_OVERRIDE_DIR / f"{model_id}.args").read_text().splitlines():
            flag, _, value = line.strip().partition(" ")
            if flag == "--vision":
                return value.strip() != "off"
    return True


def vision_models() -> list[dict[str, Any]]:
    """llama-swap models that can see images: an --mmproj, or a Strata model with Vision on."""
    try:
        cfg = yaml.safe_load(LLAMA_CONFIG.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    out = []
    for key, m in (cfg.get("models") or {}).items():
        cmd = str((m or {}).get("cmd", ""))
        if "--mmproj" in cmd:
            par = re.search(r"--parallel\s+(\d+)", cmd)
            out.append({"id": key, "name": str((m or {}).get("name", key)), "slots": int(par.group(1)) if par else 1})
        elif STRATA_BRIDGE in cmd and strata_vision_on(key):
            out.append({"id": key, "name": str((m or {}).get("name", key)), "slots": 1})
    return out


def caption_path(media: Path) -> Path:
    """Exact <stem>.txt, else a case-insensitive match (as Fizgig matches), else the exact path."""
    exact = media.with_suffix(".txt")
    if exact.is_file():
        return exact
    key = media.stem.casefold()
    with suppress(OSError):
        for p in media.parent.iterdir():
            if p.is_file() and p.suffix.lower() == ".txt" and p.stem.casefold() == key:
                return p
    return exact


def clean_caption(text: str, trigger: str, style: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^(caption|answer)\s*:\s*", "", text, flags=re.I).strip()
    text = text.strip()
    # Drop quotes that wrap the whole reply, but keep quoted visible text (Qwen / MOSS guides).
    if len(text) > 1 and text[0] in "\"“" and text[-1] in "\"”" and not re.search(r"[\"“”]", text[1:-1]):
        text = text[1:-1].strip()
    text = " ".join(text.split())  # one paragraph
    if trigger:
        body = re.sub(rf"^\s*{re.escape(trigger)}\s*[,:;-]?\s*", "", text, flags=re.I)
        body = re.sub(rf"(?<![\w]){re.escape(trigger)}(?![\w])\s*,?\s*", "", body, flags=re.I).strip()
        text = f"{trigger}, {body}" if body else trigger
    if style:
        s = style.strip()
        if s.lower() not in text.lower():
            text = text.rstrip() + ("" if text.rstrip().endswith((".", "!", "?")) else ".") + " " + s
    return text.strip()


def build_router(prefix: str, dataset_root: Path, data_dir: Path,
                 queue_request: Callable[..., Awaitable[httpx.Response]], give_back: Callable[[Path], None]):
    router = APIRouter(prefix=prefix)
    settings_file = data_dir / "tagging.json"
    jobs: dict[str, dict[str, Any]] = {}
    tasks: dict[str, asyncio.Task] = {}

    def load_settings() -> dict[str, Any]:
        base = {"system_prompt": DEFAULT_SYSTEM_PROMPT, "user_template": DEFAULT_USER_TEMPLATE, "default_model": "qwen-9b",
                "cut_prompt": DEFAULT_CUT_PROMPT, "cut_template": DEFAULT_CUT_TEMPLATE,
                "tags_prompt": DEFAULT_TAGS_PROMPT, "guide_overrides": {}}
        with suppress(OSError, ValueError):
            base.update({k: v for k, v in json.loads(settings_file.read_text(encoding="utf-8")).items() if v})
        return base

    def frames_b64(path: Path, duration: float, count: int = 8) -> list[tuple[float, str]]:
        """(timestamp, data URL) pairs. Videos: `count` frames evenly spaced from 4% to 96% of the clip,
        so the model can follow motion; more frames are sent smaller to keep the prompt size steady."""
        if path.suffix.lower() in IMAGE:
            # Images: scaled on the GPU to at most 1280 px (never up) and sent as JPEG; the original
            # bytes are the fallback for JPEG/PNG/WebP if VAAPI can't take the file.
            cmd = ["ffmpeg", "-loglevel", "error", "-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-filter_hw_device", "va",
                   "-i", str(path), "-frames:v", "1",
                   "-vf", "format=nv12,hwupload,scale_vaapi=w=min(iw\\,1280):h=min(ih\\,1280):force_original_aspect_ratio=decrease,hwdownload,format=nv12",
                   "-f", "image2", "-c:v", "mjpeg", "-q:v", "3", "pipe:1"]
            with suppress(Exception):
                proc = subprocess.run(cmd, capture_output=True, timeout=60)
                if proc.returncode == 0 and proc.stdout:
                    return [(0.0, "data:image/jpeg;base64," + base64.b64encode(proc.stdout).decode())]
            mime = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp"}.get(path.suffix.lower())
            return [(0.0, f"data:image/{mime};base64," + base64.b64encode(path.read_bytes()).decode())] if mime else []
        count = max(1, count) if duration else 1
        times = [duration * (0.04 + 0.92 * i / (count - 1)) for i in range(count)] if count > 1 else [duration * 0.5]
        width = 640 if count <= 8 else 512 if count <= 16 else 448

        def grab(t: float) -> tuple[float, str] | None:
            cmd = ["ffmpeg", "-loglevel", "error", *VAAPI, "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1",
                   "-vf", f"scale_vaapi=w={width}:h=-2,hwdownload,format=nv12", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"]
            proc = subprocess.run(cmd, capture_output=True, timeout=60)
            if proc.returncode == 0 and proc.stdout:
                return (t, "data:image/jpeg;base64," + base64.b64encode(proc.stdout).decode())
            return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            return [f for f in pool.map(grab, times) if f]

    def duration_of(path: Path) -> float:
        with suppress(Exception):
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                                 capture_output=True, text=True, timeout=20).stdout
            return float(out.strip())
        return 0.0

    def ev(job: dict[str, Any], kind: str, text: str, **extra: Any) -> None:
        """Session log shown in the Hermes-style terminal pane (kinds: user, system, tool, assistant, ok, warn, error)."""
        job["log"].append({"t": time.time(), "kind": kind, "text": text, **extra})
        if len(job["log"]) > 4000:
            del job["log"][:500]
            job["log_offset"] = job.get("log_offset", 0) + 500

    # ---- swarm: many agents on one job, claim-based so they never caption the same clip ----
    LEASE_SECONDS = 300

    def _expire(job: dict[str, Any]) -> None:
        now = time.time()
        for name, c in list(job["claims"].items()):
            if c["until"] < now:
                job["claims"].pop(name)
                job["status"][name] = "pending"
                ev(job, "warn", f"{c['agent']} lost its claim on {name} (lease expired); back in the pool", agent=c["agent"], file=name)

    def _pick(job: dict[str, Any], agent: str) -> str | None:
        """Choose the next clip for `agent` so agents rarely meet:
        - keep walking forward through your own stretch while the next clip is free;
        - otherwise jump into the largest run of untouched clips: its top if nobody is working
          (the first agent starts at the top of the list), else its middle, the spot furthest
          from every other agent's position."""
        order, status = job["queue"], job["status"]
        pos = job["agents"][agent].get("pos")
        if pos is not None and pos < len(order) and status[order[pos]] == "pending":
            return order[pos]
        runs, start = [], None
        for i in range(len(order) + 1):
            free = i < len(order) and status[order[i]] == "pending"
            if free and start is None:
                start = i
            elif not free and start is not None:
                runs.append((start, i)); start = None
        if not runs:
            return None
        start, end = max(runs, key=lambda r: r[1] - r[0])
        others_busy = any(st.get("current") for a, st in job["agents"].items() if a != agent)
        return order[start + (end - start) // 2 if others_busy else start]

    async def claim(job: dict[str, Any], agent: str) -> str | None:
        async with job["lock"]:
            _expire(job)
            if job.get("cancel"):
                return None
            name = _pick(job, agent)
            if name is None:
                return None
            job["status"][name] = "claimed"
            job["claims"][name] = {"agent": agent, "until": time.time() + LEASE_SECONDS}
            st = job["agents"][agent]
            pos = st.get("pos")
            if pos is None or pos >= len(job["queue"]) or job["queue"][pos] != name:
                st["jumps"] = st.get("jumps", 0) + 1
            st["pos"] = job["queue"].index(name) + 1
            st["current"] = name
            job["current"] = name
            return name

    async def release(job: dict[str, Any], agent: str, name: str, ok: bool) -> None:
        async with job["lock"]:
            job["claims"].pop(name, None)
            job["status"][name] = "done" if ok else "failed"
            st = job["agents"][agent]
            st["current"] = None
            st["done" if ok else "failed"] += 1
            job["processed"] = sum(1 for v in job["status"].values() if v in ("done", "failed"))

    def join(job: dict[str, Any], agent: str, source: str) -> None:
        if agent not in job["agents"]:
            job["agents"][agent] = {"source": source, "done": 0, "failed": 0, "current": None, "pos": None, "jumps": 0,
                                    "color": len(job["agents"])}
            ev(job, "system", f"{agent} joined the swarm ({source})", agent=agent)

    def system_prompt(job: dict[str, Any], settings: dict[str, Any]) -> str:
        """Tags or sentences prompt + the target model's guidance (the edited one if saved)."""
        base = settings["tags_prompt"] if job.get("format") == guides.TAGS else settings["system_prompt"]
        gid = job.get("guide")
        text = (settings.get("guide_overrides") or {}).get(gid) or guides.BY_ID.get(gid, {}).get("text", "")
        if not text:
            return base
        return (f"{base}\n\nTarget model: {guides.BY_ID[gid]['label']}. How this model wants its captions "
                f"(where this differs from the rules above, follow this):\n{text}")

    def clean(job: dict[str, Any], reply: str, trigger: str, style: str) -> str:
        if job.get("format") == guides.TAGS:
            return guides.clean_tags(reply, trigger, style, limit=30 if job.get("guide") == "sd15" else 60)
        return clean_caption(reply, trigger, style)

    def examples_text(job: dict[str, Any], name: str) -> str:
        """Tags format: up to 2 captions already in the dataset, so identity tags and order stay consistent."""
        picks = [c for f, c in job.get("examples", []) if f != name][:2]
        if not picks:
            return ""
        return ("Captions already in this dataset (match their tag style, order and identity tags; tag THIS image's own content):\n"
                + "\n".join(f"- {c}" for c in picks) + "\n")

    async def caption_one(job: dict[str, Any], agent: str, name: str, client: httpx.AsyncClient,
                          settings: dict[str, Any], headers: dict[str, str]) -> str:
        path = dataset_root / job["dataset"] / name
        if not path.is_file():
            raise RuntimeError("file is gone")
        dur = await asyncio.to_thread(duration_of, path) if path.suffix.lower() in VIDEO else 0.0
        t0 = time.time()
        frames = await asyncio.to_thread(frames_b64, path, dur, int(job.get("frames") or 8))
        images = [u for _, u in frames]
        if not images:
            raise RuntimeError("could not read frames")
        ev(job, "tool", f"vaapi.frames  {name}  {len(images)} frame(s)" + (f" of {dur:.2f}s" if dur else ""),
           icon="🎞", file=name, agent=agent, ms=int((time.time() - t0) * 1000))
        cap_file = caption_path(path)
        existing = cap_file.read_text(encoding="utf-8").strip() if cap_file.is_file() else ""
        if path.suffix.lower() not in VIDEO:
            what = "This is one still image."
        elif len(images) == 1:
            what = f"This image is the middle frame of one {dur:.1f}-second video clip."
        else:
            step = (frames[-1][0] - frames[0][0]) / (len(frames) - 1)
            what = (f"These {len(images)} images are frames from one {dur:.1f}-second video clip, in time order, about {step:.2f}s apart; "
                    "each is labelled with its timestamp. Compare them one after another to work out the motion: "
                    "what moves, in which direction, how fast, and in what order things happen. Describe the motion, not the frames.")
        user_text = settings["user_template"].format_map(_SafeDict(
            trigger=job["trigger"] or "(none)", style=job["style"] or "(none)", what=what,
            concept=job.get("concept") or "(not specified)",
            existing=(f"The current caption (improve it, keep anything that is still correct): {existing}\n" if existing else ""),
            examples=examples_text(job, name), file=name))
        if "{examples}" not in settings["user_template"] and (ex := examples_text(job, name)):
            user_text = ex + user_text   # custom templates saved before {examples} existed
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        if len(frames) > 1:
            for k, (t, u) in enumerate(frames):
                content += [{"type": "text", "text": f"Frame {k + 1} · {t:.2f}s:"}, {"type": "image_url", "image_url": {"url": u}}]
        else:
            content += [{"type": "image_url", "image_url": {"url": u}} for u in images]
        # Thinking models (Gemma 4, Qwen 3.x) otherwise spend the whole budget
        # in reasoning_content and return an empty caption.
        body = {"model": job["model"], "temperature": 0.3, "max_tokens": 600,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "system", "content": system_prompt(job, settings)},
                             {"role": "user", "content": content}]}
        reply = ""
        for attempt in range(2):
            t0 = time.time()
            ev(job, "tool", f"llama.chat  {job['model']}  [{len(images)} image(s) + prompt]" + ("  (retry)" if attempt else ""),
               icon="🧠", file=name, agent=agent)
            r = await client.post(LLAMA_URL + "/v1/chat/completions", json=body, headers=headers)
            if r.status_code != 200:
                raise RuntimeError(f"llama HTTP {r.status_code}: {r.text[:200]}")
            data_r = r.json()
            reply = data_r["choices"][0]["message"].get("content") or ""
            usage = data_r.get("usage") or {}
            ev(job, "system", f"{time.time() - t0:.1f}s · {usage.get('prompt_tokens', '?')} in / {usage.get('completion_tokens', '?')} out tokens",
               file=name, agent=agent)
            if len(clean(job, reply, "", "")) >= 15:
                break
            ev(job, "warn", "empty reply, retrying with a bigger budget", file=name, agent=agent)
            body["max_tokens"] = 1500
        if len(clean(job, reply, job["trigger"], "")) < len(job["trigger"]) + 15:
            raise RuntimeError(f"the model returned no caption: {reply[:120]!r}")
        caption = clean(job, reply, job["trigger"], job["style"])
        ev(job, "assistant", caption, file=name, agent=agent)
        cap_file.write_text(caption + "\n", encoding="utf-8")
        give_back(cap_file)
        ev(job, "ok", f"saved {cap_file.name}", file=name, agent=agent)
        return caption

    async def work_one(job: dict[str, Any], agent: str, client: httpx.AsyncClient,
                       settings: dict[str, Any], headers: dict[str, str]) -> dict[str, Any] | None:
        """Claim one clip for `agent`, caption it, release it. None when nothing is left."""
        name = await claim(job, agent)
        if name is None:
            return None
        job["stage"] = "Captioning"
        try:
            caption = await caption_one(job, agent, name, client, settings, headers)
            job["done"] += 1
            job["results"].append({"file": name, "caption": caption, "agent": agent})
            await release(job, agent, name, True)
            return {"file": name, "caption": caption}
        except Exception as exc:
            job["failed"] += 1
            job["errors"].append({"file": name, "error": str(exc)[:300], "agent": agent})
            ev(job, "error", f"{name}: {str(exc)[:300]}", file=name, agent=agent)
            await release(job, agent, name, False)
            return {"file": name, "error": str(exc)[:300]}

    async def run(job_id: str, token: str) -> None:
        job = jobs[job_id]
        ev(job, "system", f"GPU queue slot acquired after {time.time() - job['created']:.0f}s")
        stop = asyncio.Event()

        async def heartbeat():
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=20)
                except asyncio.TimeoutError:
                    with suppress(Exception):
                        await queue_request("/heartbeat", {"token": token}, timeout=6)
        hb = asyncio.create_task(heartbeat())
        settings = load_settings()
        headers = {"Authorization": f"Bearer {LLAMA_KEY}"} if LLAMA_KEY else {}
        job["settings"], job["headers"] = settings, headers
        try:
            job.update(state="preparing", stage="Freeing ComfyUI VRAM")
            ev(job, "tool", "comfyui.free  unload_models=true free_memory=true", icon="🧹")
            async with httpx.AsyncClient(timeout=20) as c:
                try:
                    fr = await c.post(COMFY_URL + "/free", json={"unload_models": True, "free_memory": True})
                    ev(job, "ok", f"ComfyUI VRAM released (HTTP {fr.status_code})")
                except Exception as exc:
                    ev(job, "warn", f"ComfyUI not reachable ({type(exc).__name__}); continuing")
            job.update(state="running", stage=f"Loading {job['model']} (first caption is slow)")
            n = job["swarm"]
            ev(job, "system", f"llama-swap → {job['model']} · swarm of {n} agent(s)"
               + (f" · model has {job['slots']} parallel slot(s)" if job.get("slots") else ""))
            async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as client:
                async def worker(agent: str, delay: float):
                    await asyncio.sleep(delay)      # stagger starts so agents don't hit the loader at once
                    while not job.get("cancel"):
                        try:
                            if await work_one(job, agent, client, settings, headers) is None:
                                break
                        except Exception as exc:  # one agent's bug must never take the whole swarm down
                            ev(job, "error", f"{agent} stopped: {type(exc).__name__}: {str(exc)[:200]}", agent=agent)
                            async with job["lock"]:
                                for n, c in list(job["claims"].items()):
                                    if c["agent"] == agent:
                                        job["claims"].pop(n); job["status"][n] = "pending"
                            break
                    ev(job, "system", f"{agent} finished ({job['agents'][agent]['done']} captioned)", agent=agent)
                names = [f"agent-{i + 1}" for i in range(n)]
                for a in names:
                    join(job, a, "trainer")
                # The first agent warms the model up alone; the rest join once it answered.
                first = asyncio.create_task(worker(names[0], 0))
                while len(names) > 1 and not job["done"] and not job["failed"] and not first.done() and not job.get("cancel"):
                    await asyncio.sleep(0.5)
                others = [asyncio.create_task(worker(a, i * 0.4)) for i, a in enumerate(names[1:])]
                await asyncio.gather(first, *others)
                # External agents (Hermes sessions etc.) may still hold claims: wait for them.
                while job["claims"] and not job.get("cancel"):
                    async with job["lock"]:
                        _expire(job)
                    await asyncio.sleep(1)
            job.update(state="cancelled" if job.get("cancel") else "complete", stage="Cancelled" if job.get("cancel") else "Complete", current=None)
            per = ", ".join(f"{a} {st['done']}" for a, st in job["agents"].items())
            ev(job, "warn" if job.get("cancel") else "ok",
               f"{'Cancelled' if job.get('cancel') else 'Done'}: {job['done']} captioned, {job['failed']} failed in {time.time() - job['created']:.0f}s ({per})")
        except Exception as exc:
            job.update(state="failed", stage="Failed", error=str(exc)[:400])
            ev(job, "error", f"job failed: {str(exc)[:300]}")
        finally:
            stop.set(); hb.cancel()
            if job.get("unload_after"):
                with suppress(Exception):
                    async with httpx.AsyncClient(timeout=30) as c:
                        await c.get(LLAMA_URL + "/unload", headers=headers)
                    ev(job, "tool", "llama.unload  (VRAM freed)", icon="⏏")
            with suppress(Exception):
                await queue_request("/release", {"token": token}, timeout=6)

    # ---- auto-cut: find good training clips in a long video (scene detect + vision scout) ----
    async def scene_cuts(path: Path, job: dict[str, Any]) -> list[float]:
        """GPU scene detection; killed straight away if the job is cancelled."""
        cmd = ["ffmpeg", "-hide_banner", *VAAPI, "-i", str(path), "-an",
               "-vf", "scale_vaapi=w=320:h=180,hwdownload,format=nv12,scdet=threshold=12", "-f", "null", "-"]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        cuts: list[float] = []
        assert proc.stderr is not None
        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stderr.readline(), timeout=0.5)
                except asyncio.TimeoutError:
                    line = None
                if job.get("cancel"):
                    proc.kill()
                    raise asyncio.CancelledError
                if line == b"":
                    break
                if line:
                    m = re.search(rb"lavfi\.scd\.time: ([0-9.]+)", line)
                    if m:
                        cuts.append(float(m.group(1)))
        finally:
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
            await proc.wait()
        return cuts

    def window_frames(path: Path, start: float, end: float) -> list[str]:
        out = []
        span = end - start
        for t in (start + 0.08, start + span * 0.25, start + span * 0.5, start + span * 0.75, end - 0.08):
            cmd = ["ffmpeg", "-loglevel", "error", *VAAPI, "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1",
                   "-vf", "scale_vaapi=w=448:h=-2,hwdownload,format=nv12", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"]
            proc = subprocess.run(cmd, capture_output=True, timeout=60)
            if proc.returncode == 0 and proc.stdout:
                out.append("data:image/jpeg;base64," + base64.b64encode(proc.stdout).decode())
        return out

    def parse_scout(text: str) -> dict[str, Any]:
        text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise ValueError(f"no JSON in reply: {text[:100]!r}")
        d = json.loads(m.group(0))
        keep = d.get("keep")
        keep = keep if isinstance(keep, bool) else str(keep).lower() in ("true", "yes", "1")
        try:
            score = float(d.get("score", 0))
        except (TypeError, ValueError):
            score = 0.0
        frames = d.get("frames")
        frames = [bool(f) if isinstance(f, bool) else str(f).lower() in ("true", "yes", "1") for f in frames] if isinstance(frames, list) else []
        same = d.get("same_shot", True)
        same = same if isinstance(same, bool) else str(same).lower() in ("true", "yes", "1")
        why = str(d.get("reason", ""))[:200]
        # The rule is enforced here, not left to the model's own "keep": the subject must be
        # visible in EVERY frame and the window must be one continuous shot.
        if frames and not all(frames):
            keep, why = False, f"subject missing in frame(s) {', '.join(str(i + 1) for i, f in enumerate(frames) if not f)}" + (f"; {why}" if why else "")
        if not same:
            keep, why = False, "not one continuous shot" + (f"; {why}" if why else "")
        return {"keep": keep, "score": max(0.0, min(10.0, score)) if keep else 0.0, "frames": frames,
                "subject": str(d.get("subject", ""))[:80], "reason": why}

    async def run_cut(job_id: str, token: str) -> None:
        job = jobs[job_id]
        ev(job, "system", f"GPU queue slot acquired after {time.time() - job['created']:.0f}s")
        stop = asyncio.Event()

        async def heartbeat():
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=20)
                except asyncio.TimeoutError:
                    with suppress(Exception):
                        await queue_request("/heartbeat", {"token": token}, timeout=6)
        hb = asyncio.create_task(heartbeat())
        settings = load_settings()
        headers = {"Authorization": f"Bearer {LLAMA_KEY}"} if LLAMA_KEY else {}
        path: Path = job["path"]
        lengths = job["lengths"]            # candidate clip lengths (one value unless variable)
        L = sum(lengths) / len(lengths)     # typical length, for spacing
        try:
            scene_mode = job["scene_mode"]
            job.update(state="preparing", stage="Finding shots" if scene_mode else "Planning timeline scan")
            dur = await asyncio.to_thread(duration_of, path)
            t0 = time.time()
            cuts = await scene_cuts(path, job) if scene_mode else []
            if job.get("cancel"):
                raise asyncio.CancelledError
            if not path.is_file():
                raise RuntimeError("the video was removed while Hermes was scouting it")
            job["shots"] = [round(c, 3) for c in cuts]
            job["duration"] = dur
            ev(job, "tool", (f"vaapi.scdet  {path.name}  {len(cuts) + 1} shots in {dur:.1f}s of video"
                             if scene_mode else f"timeline.scan  {path.name}  {dur:.1f}s of video"), icon="🎬",
               ms=int((time.time() - t0) * 1000))
            windows, lo, hi = plan_cut_windows(dur, cuts, lengths, job_id, scene_mode=scene_mode)
            job["total"] = len(windows)
            job["plan"] = windows
            ev(job, "system", f"{len(windows)} candidate window(s) of "
               + (f"{L:.2f}s" if len(lengths) == 1 else f"random length {min(lengths):.2f}–{max(lengths):.2f}s")
               + (f" (skipping the first {lo:.0f}s and last {dur - hi:.0f}s: intro/outro)" if lo else ""))
            if not windows:
                raise RuntimeError("no shot is long enough for a clip of that length")
            ev(job, "tool", "comfyui.free  unload_models=true free_memory=true", icon="🧹")
            async with httpx.AsyncClient(timeout=20) as c:
                with suppress(Exception):
                    await c.post(COMFY_URL + "/free", json={"unload_models": True, "free_memory": True})
            job.update(state="running", stage=f"Scouting with {job['model']}")
            n_agents = job["swarm"]
            ev(job, "system", f"llama-swap → {job['model']} · {n_agents} scout(s) in parallel")
            queue_w = list(enumerate(windows))
            results: list[dict[str, Any]] = []

            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
                async def scout(agent: str, delay: float):
                    await asyncio.sleep(delay)
                    join(job, agent, "trainer")
                    while queue_w and not job.get("cancel"):
                        i, (a, b) = queue_w.pop(0)
                        job["agents"][agent]["current"] = f"{a:.1f}s"
                        job["agents"][agent]["window"] = [a, b]
                        try:
                            imgs = await asyncio.to_thread(window_frames, path, a, b)
                            if len(imgs) < 3:
                                raise RuntimeError("could not read frames")
                            text = settings["cut_template"].format_map(_SafeDict(
                                concept=job["concept"] or "the main subject of the video", index=i + 1, count=len(windows), start=a, end=b,
                                nframes=len(imgs)))
                            content = [{"type": "text", "text": text}]
                            for k, u in enumerate(imgs):
                                content += [{"type": "text", "text": f"Frame {k + 1}:"}, {"type": "image_url", "image_url": {"url": u}}]
                            body = {"model": job["model"], "temperature": 0.1, "max_tokens": 300,
                                    "chat_template_kwargs": {"enable_thinking": False},
                                    "messages": [{"role": "system", "content": settings["cut_prompt"]},
                                                 {"role": "user", "content": content}]}
                            t1 = time.time()
                            ev(job, "tool", f"llama.scout  {a:.1f}–{b:.1f}s  [{len(imgs)} frames]", icon="🔎", agent=agent)
                            r = await client.post(LLAMA_URL + "/v1/chat/completions", json=body, headers=headers)
                            if r.status_code != 200:
                                raise RuntimeError(f"llama HTTP {r.status_code}: {r.text[:150]}")
                            verdict = parse_scout(r.json()["choices"][0]["message"].get("content") or "")
                            results.append({"start": a, "end": b, **verdict})
                            job["windows"] = sorted(results, key=lambda x: x["start"])
                            ev(job, "ok" if verdict["keep"] else "warn",
                               f"{a:.1f}s {'keep' if verdict['keep'] else 'skip'} {verdict['score']:.0f}/10 "
                               + "".join("■" if f else "□" for f in verdict["frames"])
                               + f" · {verdict['subject']} — {verdict['reason']}"
                               + f"  ({time.time() - t1:.1f}s)", agent=agent)
                            job["done"] += 1
                            job["agents"][agent]["done"] += 1
                        except Exception as exc:
                            job["failed"] += 1
                            ev(job, "error", f"{a:.1f}s: {str(exc)[:200]}", agent=agent)
                        job["processed"] += 1
                        job["agents"][agent]["current"] = None
                        job["agents"][agent]["window"] = None
                names = [f"agent-{k + 1}" for k in range(n_agents)]
                first = asyncio.create_task(scout(names[0], 0))
                while len(names) > 1 and not job["done"] and not job["failed"] and not first.done() and not job.get("cancel"):
                    await asyncio.sleep(0.5)
                await asyncio.gather(first, *[asyncio.create_task(scout(a, k * 0.3)) for k, a in enumerate(names[1:])])

            # Pick the best kept windows, spread out in time (no near-duplicates). Each pick is
            # verified first: its first and last frame are checked ONE AT A TIME with a plain
            # yes/no question (small vision models judge single images far more reliably).
            kept = sorted((r for r in results if r["keep"] and r["score"] >= 5), key=lambda r: (-r["score"], r["start"]))
            picks: list[dict[str, Any]] = []
            gap = max(2 * L, dur / (job["count"] * 4) if dur else 0)
            concept = job["concept"] or "the main subject of the video"

            async def edge_visible(client: httpx.AsyncClient, t: float) -> bool:
                cmd = ["ffmpeg", "-loglevel", "error", *VAAPI, "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1",
                       "-vf", "scale_vaapi=w=448:h=-2,hwdownload,format=nv12", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"]
                proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=60)
                if proc.returncode or not proc.stdout:
                    return False
                body = {"model": job["model"], "temperature": 0, "max_tokens": 5, "chat_template_kwargs": {"enable_thinking": False},
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": f"Does this image show: {concept}? Answer yes if it is visible at all, even small or partly off the edge. Answer no only if the image shows just an empty room, scenery, objects or text without it. Answer only yes or no."},
                            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(proc.stdout).decode()}}]}]}
                r = await client.post(LLAMA_URL + "/v1/chat/completions", json=body, headers=headers)
                return r.status_code == 200 and (r.json()["choices"][0]["message"].get("content") or "").strip().lower().startswith("y")

            async def verify(r: dict[str, Any]) -> bool:
                async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
                    first, last = await asyncio.gather(edge_visible(client, r["start"] + 0.04), edge_visible(client, r["end"] - 0.06))
                ok = first and last
                ev(job, "tool", f"verify  {r['start']:.1f}s  first frame {'✓' if first else '✗'} · last frame {'✓' if last else '✗'}",
                   icon="🔍" if ok else "✋")
                return ok

            if kept and not job.get("cancel"):
                job["stage"] = "Verifying picks"
            for spacing in (gap, L):   # second round tops up if the spacing was too strict
                for r in kept:
                    if len(picks) >= job["count"] or job.get("cancel"):
                        break
                    if r.get("rejected") or r in picks or not all(abs(r["start"] - p["start"]) >= spacing for p in picks):
                        continue
                    r["verifying"] = True
                    job["verifying"] = [r["start"], r["end"]]
                    if await verify(r):
                        picks.append(r)
                        job["segments"] = sorted(picks, key=lambda x: x["start"])   # live: picks appear as they pass
                    else:
                        r["rejected"] = True
            picks.sort(key=lambda r: r["start"])
            job["segments"] = picks
            job["verifying"] = None
            job.update(state="cancelled" if job.get("cancel") else "complete", stage="Cancelled" if job.get("cancel") else "Complete", current=None)
            ev(job, "ok", f"Picked {len(picks)} clip(s) from {len(kept)} good window(s) out of {len(windows)}: "
               + ", ".join(f"{p['start']:.1f}s ({p['score']:.0f})" for p in picks))
        except asyncio.CancelledError:
            job.update(state="cancelled", stage="Cancelled", current=None)
            ev(job, "warn", "auto-cut cancelled")
        except Exception as exc:
            job.update(state="failed", stage="Failed", error=str(exc)[:400])
            ev(job, "error", f"auto-cut failed: {str(exc)[:300]}")
        finally:
            stop.set(); hb.cancel()
            if job.get("unload_after"):
                with suppress(Exception):
                    async with httpx.AsyncClient(timeout=30) as c:
                        await c.get(LLAMA_URL + "/unload", headers=headers)
                    ev(job, "tool", "llama.unload  (VRAM freed)", icon="⏏")
            with suppress(Exception):
                await queue_request("/release", {"token": token}, timeout=6)

    @router.post("/api/datasets/{name}/autocut/{filename}", status_code=202)
    async def autocut(name: str, filename: str, body: CutRequest) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name) or not (dataset_root / name).is_dir():
            raise HTTPException(404, "Dataset not found.")
        folder = (dataset_root / name / ".incoming") if body.staged else (dataset_root / name)
        path = (folder / filename).resolve()
        if path.parent != folder.resolve() or not path.is_file() or path.suffix.lower() not in VIDEO:
            raise HTTPException(404, "Video not found.")
        models = {m["id"]: m for m in vision_models()}
        if body.model not in models:
            raise HTTPException(422, "Pick a vision model (one with an image projector).")
        busy = next((j for j in jobs.values() if j.get("kind") == "cut" and j["dataset"] == name and j.get("file") == filename
                     and j.get("staged") == body.staged and j["state"] in {"waiting_for_gpu", "preparing", "running"}), None)
        if busy:
            raise HTTPException(409, f"Hermes is already scouting this video (job {busy['id']}); cancel it or wait for it.")
        slots = models[body.model].get("slots", 1)
        if body.variable:
            lo, hi = sorted((body.min_seconds, body.max_seconds))
            if body.h3:
                lengths = [f / 24 for f in range(5, 125, 17) if lo - 1e-6 <= f / 24 <= hi + 1e-6]
            else:
                lengths = [float(x) for x in range(int(round(lo)), int(round(hi)) + 1)]
            if not lengths:
                raise HTTPException(422, "No clip length fits that range.")
        else:
            lengths = [body.seconds]
        job_id = "tag" + os.urandom(14).hex()[:29]
        jobs[job_id] = {"id": job_id, "kind": "cut", "dataset": name, "file": filename, "path": path, "model": body.model,
                        "concept": body.concept.strip(), "count": body.count, "seconds": body.seconds, "lengths": lengths,
                        "staged": body.staged, "scene_mode": body.scene_mode,
                        "unload_after": body.unload_after, "trigger": "", "style": "", "mode": "cut",
                        "total": 0, "processed": 0, "done": 0, "failed": 0, "skipped": 0, "errors": [], "results": [],
                        "current": None, "state": "waiting_for_gpu", "stage": "Waiting for the GPU queue", "error": None,
                        "created": time.time(), "log": [], "log_offset": 0, "source": body.source,
                        "swarm": max(1, min(body.agents or slots, 16)), "slots": slots, "agents": {}, "claims": {},
                        "status": {}, "lock": asyncio.Lock(), "segments": None}
        j = jobs[job_id]
        ev(j, "user", f"auto-cut {filename} ({'scene cuts' if body.scene_mode else 'full timeline'}) — find {body.count} clip(s) of "
           + (f"{body.seconds:.2f}s" if len(lengths) == 1 else f"random {min(lengths):.2f}–{max(lengths):.2f}s (" + "/".join(f"{x * 24:.0f}f" if body.h3 else f"{x:g}s" for x in lengths) + ")")
           + (f", looking for: {j['concept']}" if j["concept"] else "") + f", model {body.model}")
        ev(j, "system", "waiting for the shared GPU queue (renders and training go first)")
        try:
            r = await queue_request("/enqueue", {"app": "Trainer", "job_id": job_id, "label": f"Auto-cut {filename[:40]}"})
            r.raise_for_status()
            j["queue_entry_id"] = r.json().get("entry_id")
        except Exception as exc:
            j.update(state="failed", stage="Queue error", error=str(exc)[:300])
            raise HTTPException(503, "Could not reach the shared GPU queue.") from exc
        return public(j)

    def proceed(job_id: str, token: str) -> bool:
        job = jobs.get(job_id)
        if not job or job["state"] != "waiting_for_gpu":
            return False
        tasks[job_id] = asyncio.create_task((run_cut if job.get("kind") == "cut" else run)(job_id, token))
        return True

    @router.get("/api/tagging/models")
    async def models() -> dict[str, Any]:
        return {"models": vision_models(), "default": load_settings().get("default_model")}

    @router.get("/api/tagging/settings")
    async def get_settings() -> dict[str, Any]:
        s = load_settings()
        return {**s, "defaults": {"system_prompt": DEFAULT_SYSTEM_PROMPT, "user_template": DEFAULT_USER_TEMPLATE,
                                  "cut_prompt": DEFAULT_CUT_PROMPT, "cut_template": DEFAULT_CUT_TEMPLATE,
                                  "tags_prompt": DEFAULT_TAGS_PROMPT},
                "guides": [{"id": g["id"], "label": g["label"], "group": g["group"], "format": g["format"], "families": g["families"],
                            "text": (s.get("guide_overrides") or {}).get(g["id"]) or g["text"], "default_text": g["text"]}
                           for g in guides.GUIDES],
                # The target picker lists the models the Trainer can train; each maps to its caption guide.
                "targets": [{"id": f.id, "title": f.title, "group": f.group, "media": sorted(f.media),
                             "guide": guides.for_family(f.id) or ("h3" if f.group == "Video" else "natural")}
                            for f in families.all_families()]}

    @router.put("/api/tagging/settings")
    async def put_settings(body: TagSettings) -> dict[str, Any]:
        for key in ("{trigger}", "{style}", "{what}"):
            if key not in body.user_template:
                raise HTTPException(422, f"The request template must contain {key}.")
        data = body.model_dump()
        if not data.get("cut_prompt"):
            data.pop("cut_prompt", None)
        if not data.get("cut_template"):
            data.pop("cut_template", None)
        if not data.get("tags_prompt"):
            data.pop("tags_prompt", None)
        old = {}
        with suppress(OSError, ValueError):
            old = json.loads(settings_file.read_text(encoding="utf-8"))
        overrides = data.pop("guide_overrides", None)
        if overrides is not None:   # merge per guide; an empty or default text removes the override
            merged = dict(old.get("guide_overrides") or {})
            for gid, text in overrides.items():
                if gid not in guides.BY_ID:
                    raise HTTPException(422, f"Unknown model guide {gid!r}.")
                if not text.strip() or text.strip() == guides.BY_ID[gid]["text"]:
                    merged.pop(gid, None)
                else:
                    merged[gid] = text.strip()[:5000]
            data["guide_overrides"] = merged
        settings_file.write_text(json.dumps({**old, **data}, indent=2), encoding="utf-8")
        return load_settings()

    @router.post("/api/datasets/{name}/autotag", status_code=202)
    async def autotag(name: str, body: TagRequest) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name) or not (dataset_root / name).is_dir():
            raise HTTPException(404, "Dataset not found.")
        if body.model not in {m["id"] for m in vision_models()}:
            raise HTTPException(422, "Pick a vision model (one with an image projector) from the list.")
        if any(j["dataset"] == name and j.get("kind") != "cut" and j["state"] in {"waiting_for_gpu", "preparing", "running"} for j in jobs.values()):
            raise HTTPException(409, "This dataset is already being tagged.")
        ds = dataset_root / name
        media = sorted(p for p in ds.iterdir() if p.is_file() and p.suffix.lower() in VIDEO | IMAGE)
        if body.files:
            wanted = set(body.files)
            media = [p for p in media if p.name in wanted]
        if body.mode == "missing":
            media = [p for p in media if not caption_path(p).is_file()]
        if not media:
            raise HTTPException(422, "Nothing to tag: every clip already has a caption (choose 'all' to redo them).")
        slots = next((m.get("slots", 1) for m in vision_models() if m["id"] == body.model), 1)
        swarm = max(1, min(body.agents, 16, len(media)))
        all_media = [p for p in ds.iterdir() if p.is_file() and p.suffix.lower() in VIDEO | IMAGE]
        captioned = []
        for p in sorted(all_media):
            cap = caption_path(p)
            if cap.is_file():
                with suppress(OSError, UnicodeDecodeError):
                    if text := cap.read_text(encoding="utf-8").strip():
                        captioned.append((p.name, " ".join(text.split())[:1200]))
        has_video = any(p.suffix.lower() in VIDEO for p in all_media)
        guide = guides.resolve(body.target, has_video, [c for _, c in captioned])
        fmt = guide["format"] if body.caption_format == "auto" else body.caption_format
        # Example captions only make sense when they are already in the format being written.
        examples = (random.Random(name).sample(captioned, min(4, len(captioned)))
                    if fmt == guides.TAGS and guides.looks_like_tags([c for _, c in captioned]) else [])
        job_id = "tag" + os.urandom(14).hex()[:29]
        jobs[job_id] = {"id": job_id, "dataset": name, "model": body.model, "trigger": body.trigger.strip(),
                        "concept": body.concept.strip(),
                        "style": body.style.strip(), "mode": body.mode, "unload_after": body.unload_after,
                        "queue": [p.name for p in media], "total": len(media), "processed": 0, "done": 0,
                        "failed": 0, "skipped": 0, "errors": [], "results": [], "current": None,
                        "state": "waiting_for_gpu", "stage": "Waiting for the GPU queue", "error": None,
                        "created": time.time(), "log": [], "log_offset": 0,
                        "source": body.source or "trainer",
                        "frames": body.frames, "guide": guide["id"], "guide_label": guide["label"], "format": fmt,
                        "examples": examples,
                        "swarm": swarm, "slots": slots, "agents": {}, "claims": {},
                        "status": {p.name: "pending" for p in media}, "lock": asyncio.Lock()}
        j = jobs[job_id]
        ev(j, "user", f"tag dataset {name} — {len(media)} item(s), trigger \"{j['trigger'] or '-'}\""
                    + (f", teaches: {j['concept']}" if j["concept"] else "") + f", {body.mode} captions, model {body.model}" + (f", {body.frames} frames/clip" if has_video else "")
                    + f", target {guide['label']} ({'booru tags' if fmt == guides.TAGS else 'sentences'})"
                    + (f", {len(examples)} example caption(s) for style" if examples else "") + (f", swarm of {swarm} agents" if swarm > 1 else ""))
        ev(j, "system", "waiting for the shared GPU queue (renders and training go first)")
        try:
            r = await queue_request("/enqueue", {"app": "Trainer", "job_id": job_id, "label": f"Tagging {name} ({len(media)})"})
            r.raise_for_status()
            jobs[job_id]["queue_entry_id"] = r.json().get("entry_id")
        except Exception as exc:
            jobs[job_id].update(state="failed", stage="Queue error", error=str(exc)[:300])
            raise HTTPException(503, "Could not reach the shared GPU queue.") from exc
        return public(jobs[job_id])

    def public(job: dict[str, Any]) -> dict[str, Any]:
        return ({k: v for k, v in job.items() if k not in {"queue", "cancel", "log", "lock", "settings", "headers", "status", "path", "examples"}}
                | {"results": job["results"][-50:], "log_len": job.get("log_offset", 0) + len(job["log"]),
                   "tagged": {r["file"]: r.get("agent") for r in job["results"]
                              if job.get("status", {}).get(r["file"]) == "done"}})

    HERMES_LOG = Path(os.getenv("HERMES_LOG", "/hermes-logs/agent.log"))
    NOISE = re.compile(r"mem_trim|hermes_cli\.plugins|plugin_capabilities|tools_config|check_fn|gateway housekeeping|curator")

    def hermes_lines(since: float) -> list[dict[str, Any]]:
        """Recent non-housekeeping lines from Hermes' own agent.log (newer than `since`)."""
        if not HERMES_LOG.is_file():
            return []
        try:
            with HERMES_LOG.open("rb") as fh:
                fh.seek(max(0, HERMES_LOG.stat().st_size - 200_000))
                lines = fh.read().decode("utf-8", errors="replace").splitlines()[1:]
        except OSError:
            return []
        out = []
        for line in lines:
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d+) (\w+) (\S+?): (.*)", line)
            if not m or NOISE.search(line):
                continue
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) + int(m.group(2)) / 1000
            if ts < since:
                continue
            level = m.group(3)
            out.append({"t": ts, "kind": {"ERROR": "error", "WARNING": "warn"}.get(level, "hermes"),
                        "text": f"{m.group(4)}: {m.group(5)[:400]}", "hermes": True})
        return out[-200:]

    @router.get("/api/tag-jobs/{job_id}/log")
    async def job_log(job_id: str, after: int = 0) -> dict[str, Any]:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Unknown tagging job.")
        off = job.get("log_offset", 0)
        start = max(0, after - off)
        hermes = await asyncio.to_thread(hermes_lines, job["created"] - 5)
        return {"job": public(job), "lines": job["log"][start:], "next": off + len(job["log"]), "hermes": hermes}

    @router.get("/api/tag-jobs")
    async def list_jobs(dataset: str = "") -> dict[str, Any]:
        return {"jobs": [public(j) for j in jobs.values() if not dataset or j["dataset"] == dataset][-20:]}

    @router.get("/api/tag-jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        if job_id not in jobs:
            raise HTTPException(404, "Unknown tagging job.")
        return public(jobs[job_id])

    @router.post("/api/tag-jobs/{job_id}/work")
    async def work(job_id: str, body: WorkBody) -> dict[str, Any]:
        """An outside agent (e.g. another Hermes session) joins the swarm and does ONE clip:
        it is claimed for that agent with the same no-collision picking, captioned with the
        job's vision model, and released. Call repeatedly until {"done": true}."""
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Unknown tagging job.")
        if job["state"] != "running" or "settings" not in job:
            return {"done": job["state"] in {"complete", "failed", "cancelled"}, "waiting": job["state"] in {"waiting_for_gpu", "preparing"}, "state": job["state"]}
        agent = re.sub(r"[^A-Za-z0-9_.-]+", "-", body.agent)[:40] or "external"
        join(job, agent, "external")
        async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as client:
            result = await work_one(job, agent, client, job["settings"], job["headers"])
        if result is None:
            return {"done": True, "state": job["state"]}
        return {"done": False, **result, "processed": job["processed"], "total": job["total"]}

    @router.post("/api/tag-jobs/{job_id}/cancel")
    async def cancel(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Unknown tagging job.")
        if job["state"] == "waiting_for_gpu":
            with suppress(Exception):
                await queue_request(f"/dequeue/{job.get('queue_entry_id')}")
            job.update(state="cancelled", stage="Cancelled")
        else:
            job["cancel"] = True
            job["stage"] = "Stopping after the current clip"
        return public(job)

    router.swarm_helpers = {"claim": claim, "release": release, "join": join}  # for tests

    def active_jobs() -> list[dict[str, Any]]:
        """Running/queued tag + auto-cut jobs (for the dataset/trim guards)."""
        return [{"dataset": j["dataset"], "kind": j.get("kind", "tag"), "file": j.get("file"), "staged": j.get("staged", False), "id": j["id"]}
                for j in jobs.values() if j["state"] in {"waiting_for_gpu", "preparing", "running"}]
    router.active_jobs = active_jobs
    return router, proceed
