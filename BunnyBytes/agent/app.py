"""Rocsy Agent: the Master Agent service (/Agent).

A session is a conversation with the Master Agent. Each turn is a Hermes run
(Hermes API /v1/runs) briefed with the master-agent skill, whose helper script
calls this service's tool endpoints to start and track jobs in Vid, Krea, Mus
and the Trainer. Those jobs go to the front of the shared GPU queue.

The agent never thinks while its jobs render: `wait` (or ending a turn with
jobs still running) ends the Hermes run, this service watches the jobs, and
the agent is resumed with the results once all of them have finished. With a
local model it holds the GPU while it thinks (ComfyUI's VRAM is freed first)
and hands it back, model unloaded, before its jobs run.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import urllib.request
import logging
import mimetypes
import os
import re
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import date as Date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import studios
import media_import
import guide_rag
import video_edit
from brief import DEFAULT_BRIEF, REQUIRED, SCRIPT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agent")

PREFIX = os.getenv("AGENT_MOUNT_PREFIX", "/Agent").rstrip("/")
DATA = Path(os.getenv("AGENT_DATA_DIR", "/data"))
HERMES_URL = os.getenv("HERMES_API_URL", "http://127.0.0.1:8642").rstrip("/")
HERMES_KEY = os.getenv("HERMES_API_KEY", "")
LLAMA_URL = os.getenv("LLAMA_URL", "http://127.0.0.1:8400").rstrip("/")
LLAMA_KEY = os.getenv("LLAMA_API_KEY", "")
COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
LOCK_URL = os.getenv("AGENT_QUEUE_URL", "http://127.0.0.1:8600").rstrip("/")
QUEUE_SECRET = os.getenv("QUEUE_INTERNAL_SECRET", "")
NOTIFY_URL = os.getenv("AGENT_NOTIFY_HUB_URL", "http://127.0.0.1:8080/internal/notify")
HOSTCTL_URL = os.getenv("HOSTCTL_URL", "http://127.0.0.1:8899").rstrip("/")
HOSTCTL_SECRET = os.getenv("HOSTCTL_SECRET", "")
# User attachments: written here, read by Hermes at HERMES_ATTACH_DIR (the same host folder, ~/.hermes/rocsy-attachments).
ATTACH_DIR = Path(os.getenv("AGENT_ATTACH_DIR", "/attachments"))
HERMES_ATTACH_DIR = os.getenv("AGENT_ATTACH_HERMES_DIR", "/opt/data/rocsy-attachments").rstrip("/")
PINNED_ROOT = Path(os.getenv("AGENT_PINNED_ROOT", "/pinned-uploads"))
PINNED_INDEX = Path(os.getenv("AGENT_PINNED_INDEX", "/pinned-cache-data/upload-cache.json"))
PINNED_GROUPS_INDEX = PINNED_INDEX.with_name("upload-cache-groups.json")
HERMES_PINNED_DIR = os.getenv("AGENT_PINNED_HERMES_DIR", "/opt/data/rocsy-pinned").rstrip("/")
# The agent's own workspace (downloads, drafts, scripts): ~/.hermes/rocsy-workspace, read-only here for `share`.
WORKSPACE_DIR = Path(os.getenv("AGENT_WORKSPACE_DIR", "/workspace"))
HERMES_WORKSPACE = os.getenv("AGENT_WORKSPACE_HERMES_DIR", "/opt/data/rocsy-workspace").rstrip("/")
# Connections → Accounts: <platform>.json logins (read only here) + scripts/<platform>_upload.* that Hermes writes.
ACCOUNTS_DIR = Path(os.getenv("AGENT_ACCOUNTS_DIR", "/accounts"))
HERMES_ACCOUNTS = os.getenv("AGENT_ACCOUNTS_HERMES_DIR", "/opt/data/rocsy-accounts").rstrip("/")
ACCOUNT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
PLAYWRIGHT = "/opt/data/hermes-agent/node_modules/playwright-core"
MAX_UPLOAD = 2 * 1024 ** 3      # per attachment; sent in 8 MB parts (gateway body limit 50 MB, Cloudflare 100 MB)
SELF_URL = f"http://127.0.0.1:{os.getenv('AGENT_PORT', '8900')}"
LOCAL_PROVIDERS = ("custom", "llama-cpp", "lmstudio", "ollama")
REASONING = ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"]
MAX_RESUMES = 8          # automatic "your jobs finished" turns per user message
MAX_EVENTS = 3000
BUSY = {"starting", "waiting_for_gpu", "thinking", "rendering"}

# What the Master Agent may do beyond the studios. Hermes' own tools (web, browser, files,
# terminal, memory, subagents, cron) are always loaded in an API run, so those rules reach it
# through the brief; the Rocsy tools (services, notify, share) are enforced here as well.
PERMISSIONS: list[dict[str, Any]] = [
    {"key": "web", "label": "Web and browser", "default": True,
     "on": "You may search the web, read pages and use your browser tools.",
     "off": "Do not use the web or the browser."},
    {"key": "files", "label": "Files", "default": "workspace", "choices": ["none", "read", "workspace", "full"],
     "none": "Do not read or write files on this machine (attachments excepted).",
     "read": "You may read files on this machine; do not create, change or delete any.",
     "workspace": "You may read files anywhere, and create, change or delete files only inside your workspace {workspace}.",
     "full": "You may read and write files anywhere you can reach. Back up a file before you change it, never delete the user's work, and say what you changed."},
    {"key": "terminal", "label": "Terminal", "default": True,
     "on": "You may run shell commands (downloads, conversions, scripts, inspecting things). Keep them short; long ones go in the background.",
     "off": "Run no shell command except the helper script."},
    {"key": "gpu", "label": "Use the GPU", "default": True,
     "on": "You may use the GPU yourself when a task needs it. Video: ffmpeg on the GPU's media engine, never the CPU "
           "(decode `-hwaccel vaapi -hwaccel_device /dev/dri/renderD128`; encode `-vaapi_device /dev/dri/renderD128 -vf "
           "format=nv12,hwupload -c:v h264_vaapi -qp 20`). Compute (torch/ROCm, already installed): first check VRAM with "
           "`cat /sys/class/drm/card1/device/mem_info_vram_used /sys/class/drm/card1/device/mem_info_vram_total` and keep at "
           "least 6 GB free (the desktop runs on this GPU and crashes when VRAM fills); free it when done. Never load "
           "image/video/music models yourself: generation goes through the studios and their GPU queue.",
     "off": "Don't use the GPU yourself: GPU work goes through the studios (postedit, montage, motion, captions)."},
    {"key": "memory", "label": "Memory and skills", "default": True,
     "on": "You may save what you learn about the user's taste to your memory and create or improve skills.",
     "off": "Do not write to your memory or change skills."},
    {"key": "delegate", "label": "Subagents", "default": True,
     "on": "You may hand side tasks (research, prompt drafts) to subagents.",
     "off": "Do not start subagents."},
    {"key": "schedule", "label": "Schedules", "default": False,
     "on": "You may create scheduled (cron) jobs when the user asks for something recurring.",
     "off": "Do not create scheduled (cron) jobs."},
    {"key": "services", "label": "Services", "default": False,
     "on": "You may start, stop and restart this machine's services with `$R service` (it refuses while they render).",
     "off": "You may look at services (`$R services`) but not start, stop or restart them."},
    {"key": "llama_optimize", "label": "Llama optimization", "default": True,
     "on": "You may benchmark and tune a llama-swap model when the user asks. First scan the hardware and research current llama.cpp guidance; the optimizer saves and tests the baseline and restores it on failure.",
     "off": "Do not benchmark or change llama-swap model parameters."},
    {"key": "rea_mods", "label": "REA mods on the real game", "default": False,
     "on": "You may install your REA mod builds into the user's game, uninstall them, and play-test them on the real game with `$R reamod` (the game starts on the user's desktop through Steam, runs for the test, is screenshotted 3 times (15 s in, half way, just before closing) and closed). Only single-player mods of games the user owns.",
     "off": "Installing a mod into a game, uninstalling it and real-game play tests are the user's (REA page); you may check `$R reamod status` and run REA test runs with `$R reamod test MOD [--why 'CHANGE -> EXPECTED LOG LINE']`."},
    {"key": "notify_user", "label": "Push notifications", "default": True,
     "on": "You may send the user push notifications with `$R notify`.",
     "off": "Do not send push notifications."},
]
PERM_DEFAULTS = {x["key"]: x["default"] for x in PERMISSIONS}
PERMISSION_GATED = {"REA"}      # job kinds this service runs itself, gated by a permission instead of a studio switch
APPROVAL_MODES = ("ask", "allow", "deny")
LOOP_GUARDS = ("off", "warn", "stop")    # hostctl copies this into Hermes' tool_loop_guardrails
# `$R images` engines: label, search page for the browser mode, and whether the service can read it headless
IMAGE_ENGINES = {
    "duckduckgo": {"label": "DuckDuckGo Images", "url": "https://duckduckgo.com/?ia=images&iax=images&q={q}", "headless": True},
    "bing+commons": {"label": "Bing + Wikimedia Commons", "url": "https://www.bing.com/images/search?q={q}", "headless": True, "private": False},
    "bing": {"label": "Bing Images", "url": "https://www.bing.com/images/search?q={q}", "headless": True, "private": False},
    "commons": {"label": "Wikimedia Commons", "url": "https://commons.wikimedia.org/w/index.php?search={q}&title=Special:MediaSearch&type=image", "headless": True},
    "google": {"label": "Google Images", "url": "https://www.google.com/search?tbm=isch&q={q}", "headless": False, "private": False},
    "brave": {"label": "Brave Images", "url": "https://search.brave.com/images?q={q}", "headless": False},
    "yandex": {"label": "Yandex Images", "url": "https://yandex.com/images/search?text={q}", "headless": False},
}
IMAGE_MODES = ("headless", "browser")    # headless: the service searches by itself; browser: the agent in the /browser Chromium
# Review mode (/review): what a studio call counts as. "plan" is the agent's `$R plan` overview.
REVIEW_KINDS = {"plan": "its plan", "video": "videos (incl. H3 Agentic)", "image": "images", "music": "music",
                "voice": "voice (speech and voice-overs)", "edit": "post-edits, captions and montages", "motion": "Motion renders",
                "train": "LoRA training"}
REVIEW_ALIASES = {"plans": "plan", "videos": "video", "vid": "video", "agent": "video", "agentic": "video", "images": "image",
                  "img": "image", "krea": "image", "song": "music", "songs": "music", "mus": "music", "voices": "voice",
                  "speech": "voice", "tts": "voice", "postedit": "edit", "post-edit": "edit", "captions": "edit",
                  "montage": "edit", "edits": "edit", "trainer": "train", "training": "train", "lora": "train"}
# Ask first (Settings → Agentic): the first use of each service in a session waits for the user's OK;
# once allowed, that service is cleared for the rest of the session (or for good with "Always").
ASK_SERVICES = {"Vid": "Video (incl. H3 Agentic, post-edits, captions, montages)", "Krea": "Image", "Mus": "Music",
                "Voice": "Voice", "Motion": "Motion renders", "Trainer": "LoRA training", "REA": "REA mod tests",
                "Services": "starting, stopping or restarting services", "Uploads": "logging in to your website accounts and posting there"}

DEFAULTS: dict[str, Any] = {
    "model": "", "provider": "", "reasoning": "default",
    "video_model": "minimax_h3", "voice_model": "qwen3", "image_model": "krea2", "music_model": "yue2",
    "services": {name: True for name in studios.SERVICES},
    "priority": True,            # its jobs jump to the front of the GPU queue
    "gpu_guard": True,           # local model: hold the GPU while thinking, hand it over (unloaded) for renders
    "cache_save": True,          # park the local model's conversation cache on disk before an unload, restore it after
    "keep_loaded_ask": 0,        # minutes the local model stays loaded after a turn that ends with a question (0 = off, -1 = until the GPU is wanted)
    "confirm_jobs": False,       # review mode (/review): each studio call is a proposal you approve or veto first
    "review_services": [],       # what review mode covers ([] = everything in REVIEW_KINDS)
    "ask_services": False,       # ask before the first use of each service in a session, then it is cleared
    "cleared_services": [],      # services the user allowed with "Always" (never asked again)
    "may_cancel_others": False,  # may cancel jobs it did not start
    "max_jobs": 0,               # GPU jobs per user message (0 = no limit)
    "vid_preset": "",            # base settings for its videos ("" = Vid defaults)
    "use_agentic": True,         # use a studio's own Agentic mode where it has one (Vid: H3 Agentic sessions)
    "brief": "",                 # "" = built-in brief
    "notify": True,
    "goal_max_turns": 10,        # turns a /goal may take before it pauses for you
    "permissions": dict(PERM_DEFAULTS),
    "command_approval": "ask",   # Hermes asks before a risky command: ask you on the page, allow, or deny
    "continuity": True,          # one Hermes session per Agent session: it keeps its own tool calls and notes
    "loop_guard": "off",         # repeated tool calls: off (nothing), warn (tell you, keep going), stop (Hermes halts, goal pauses)
    "limit_restarts": 0,         # Hermes' iteration limit ended a run: restart it this many times in a row (0 = off, -1 = no limit)
    "image_engine": "duckduckgo",     # `$R images` search engine (IMAGE_ENGINES)
    "image_mode": "headless",         # headless (the service fetches the results) or browser (the agent, in the visible /browser Chromium)
    # Context compression summarizes with the session's own model; hostctl copies this backup into
    # Hermes' auxiliary.compression.fallback_chain for when that model can't answer ("" = none).
    "compress_backup_model": "", "compress_backup_provider": "",
    "compression": {},           # Settings → Compression; filled from COMPRESS_PRESETS by Config.clean
}

# Settings → Compression. hostctl (hermeslink.sync_compression) writes these into Hermes:
# at/trim_at are % of the Agent model's window (compression.threshold + threshold_tokens /
# proactive_prune_tokens), keep_messages = protect_last_n, keep_user = min_tail_user_messages,
# trim_min_chars = proactive_prune_min_result_chars; summary (% of the summarized turns) and
# tool_rounds (tail tool results kept whole) are Hermes constants the rocsy plugin sets.
# Balanced = what ran before this tab (Hermes' 0.5 ratio was the binding trigger); keep_user >= 3
# because a review message was once summarized away and the model replayed its previous turn.
COMPRESS_FIELDS = {  # key: (min, max); trim_at 0 = no trimming
    "at": (25, 85), "keep_messages": (2, 60), "keep_user": (1, 8), "summary": (5, 40),
    "trim_at": (0, 80), "trim_min_chars": (500, 50000), "tool_rounds": (1, 12),
}
COMPRESS_PRESETS = {
    "light":    {"at": 70, "keep_messages": 30, "keep_user": 4, "summary": 30, "trim_at": 50, "trim_min_chars": 8000, "tool_rounds": 8},
    "balanced": {"at": 50, "keep_messages": 20, "keep_user": 3, "summary": 20, "trim_at": 30, "trim_min_chars": 4000, "tool_rounds": 6},
    "strong":   {"at": 40, "keep_messages": 10, "keep_user": 2, "summary": 15, "trim_at": 20, "trim_min_chars": 2000, "tool_rounds": 3},
    "maximum":  {"at": 30, "keep_messages": 6,  "keep_user": 1, "summary": 10, "trim_at": 15, "trim_min_chars": 1000, "tool_rounds": 2},
}


def clean_compression(values: Any) -> dict[str, Any]:
    """A named preset carries its own numbers; "custom" keeps the user's, clamped."""
    values = values if isinstance(values, dict) else {}
    preset = values.get("preset") if values.get("preset") in (*COMPRESS_PRESETS, "custom") else "balanced"
    if preset != "custom":
        return {"preset": preset, **COMPRESS_PRESETS[preset]}
    out: dict[str, Any] = {"preset": "custom"}
    for key, (lo, hi) in COMPRESS_FIELDS.items():
        try:
            out[key] = max(lo, min(hi, int(values.get(key))))
        except (TypeError, ValueError):
            out[key] = COMPRESS_PRESETS["balanced"][key]
    if 0 < out["trim_at"] < 10:
        out["trim_at"] = 10
    return out


def p(path: str) -> str:
    return PREFIX + path


def now() -> float:
    return time.time()


# ---------------- config ----------------

class Config:
    path = DATA / "config.json"

    _cached: tuple[Any, dict[str, Any]] | None = None

    def load(self) -> dict[str, Any]:
        # Called on every stream wake-up (10x a second while Hermes streams): parse only when
        # the file changed. Callers get their own copy, so mutating it never touches the cache.
        try:
            st = self.path.stat()
            key: Any = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if self._cached is None or self._cached[0] != key or key is None:
            try:
                saved = json.loads(self.path.read_text())
            except (OSError, ValueError):
                saved = {}
            Config._cached = (key, self.clean({**DEFAULTS, **saved}))
        return copy.deepcopy(self._cached[1])

    @staticmethod
    def clean(values: dict[str, Any]) -> dict[str, Any]:
        out = {**DEFAULTS}
        out["model"] = str(values.get("model") or "")[:200]
        out["provider"] = str(values.get("provider") or "")[:200]
        out["reasoning"] = values.get("reasoning") if values.get("reasoning") in REASONING else "default"
        for key, choices in (("video_model", studios.VIDEO_MODELS), ("voice_model", studios.VOICE_MODELS),
                             ("image_model", studios.KREA_MODELS), ("music_model", studios.MUSIC_MODELS)):
            out[key] = values.get(key) if values.get(key) in choices else DEFAULTS[key]
        services = values.get("services") if isinstance(values.get("services"), dict) else {}
        out["services"] = {name: bool(services.get(name, True)) for name in studios.SERVICES}
        for key in ("priority", "gpu_guard", "cache_save", "confirm_jobs", "ask_services", "may_cancel_others", "notify", "use_agentic", "continuity"):
            out[key] = bool(values.get(key, DEFAULTS[key]))
        try:
            out["goal_max_turns"] = max(1, min(100, int(values.get("goal_max_turns") or DEFAULTS["goal_max_turns"])))
        except (TypeError, ValueError):
            out["goal_max_turns"] = DEFAULTS["goal_max_turns"]
        try:
            out["max_jobs"] = max(0, int(values.get("max_jobs") or 0))     # 0 = no limit
        except (TypeError, ValueError):
            out["max_jobs"] = 0
        out["vid_preset"] = str(values.get("vid_preset") or "")[:120]
        kinds = values.get("review_services") if isinstance(values.get("review_services"), list) else []
        out["review_services"] = [k for k in REVIEW_KINDS if k in kinds]
        cleared = values.get("cleared_services") if isinstance(values.get("cleared_services"), list) else []
        out["cleared_services"] = [k for k in ASK_SERVICES if k in cleared]
        perms = values.get("permissions") if isinstance(values.get("permissions"), dict) else {}
        out["permissions"] = {}
        for x in PERMISSIONS:
            value = perms.get(x["key"], x["default"])
            out["permissions"][x["key"]] = (value if value in x["choices"] else x["default"]) if "choices" in x else bool(value)
        out["command_approval"] = values.get("command_approval") if values.get("command_approval") in APPROVAL_MODES else "ask"
        out["continuity"] = bool(values.get("continuity", True))
        out["loop_guard"] = values.get("loop_guard") if values.get("loop_guard") in LOOP_GUARDS else "off"
        try:
            out["keep_loaded_ask"] = max(-1, min(240, int(values.get("keep_loaded_ask") or 0)))
        except (TypeError, ValueError):
            out["keep_loaded_ask"] = 0
        try:
            out["limit_restarts"] = max(-1, min(100, int(values.get("limit_restarts") or 0)))
        except (TypeError, ValueError):
            out["limit_restarts"] = 0
        out["image_mode"] = values.get("image_mode") if values.get("image_mode") in IMAGE_MODES else "headless"
        engine = values.get("image_engine") if values.get("image_engine") in IMAGE_ENGINES else "duckduckgo"
        out["image_engine"] = engine if out["image_mode"] == "browser" or IMAGE_ENGINES[engine]["headless"] else "duckduckgo"
        out["compress_backup_model"] = str(values.get("compress_backup_model") or "")[:200]
        out["compress_backup_provider"] = str(values.get("compress_backup_provider") or "")[:200] if out["compress_backup_model"] else ""
        out["compression"] = clean_compression(values.get("compression"))
        brief = str(values.get("brief") or "").replace("\r\n", "\n")
        out["brief"] = "" if brief.strip() == DEFAULT_BRIEF.strip() else brief[:60000]
        return out

    def save(self, values: dict[str, Any]) -> dict[str, Any]:
        current = self.load()
        if isinstance(values.get("permissions"), dict):       # a partial permissions patch keeps the rest
            values = {**values, "permissions": {**current["permissions"], **values["permissions"]}}
        data = self.clean({**current, **values})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)
        return data


config = Config()


def is_local(cfg: dict[str, Any], s: dict[str, Any] | None = None) -> bool:
    """Whether the model this session thinks with runs on this machine's GPU."""
    override = (s or {}).get("model_override") or {}
    model, provider = (override["model"], override.get("provider") or "") if override.get("model") else (cfg["model"], cfg["provider"])
    if not model or not provider:
        provider = models_cache.get("data", {}).get("default", {}).get("provider") or "custom"
    return provider.split(":")[0] in LOCAL_PROVIDERS


# ---------------- sessions ----------------

class Sessions:
    def __init__(self) -> None:
        self.dir = DATA / "sessions"
        self.items: dict[str, dict[str, Any]] = {}
        self.dirty: set[str] = set()

    def load(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        for file in self.dir.glob("*.json"):
            with suppress(OSError, ValueError):
                item = json.loads(file.read_text())
                for j in item.get("jobs") or []:
                    if j.get("deferred") and not j.get("job_id") and j.get("state") not in studios.DONE:
                        j.update(state="failed", error="the Agent service restarted before it finished; run captions again")
                if compact_terminals(item.get("events") or []):
                    self.dirty.add(item["id"])   # rewritten smaller on the next flush
                if item.get("state") in BUSY:    # the service restarted mid-turn
                    item["state"] = "rendering" if unfinished(item) else "interrupted"
                    item["pending"] = None
                self.items[item["id"]] = item

    def save(self, sid: str) -> None:
        self.dirty.add(sid)
        changed(sid)

    def flush(self) -> None:
        for sid in list(self.dirty):
            self.dirty.discard(sid)
            item = self.items.get(sid)
            mirror_goal_if_changed(sid, item)
            if item is None:
                (self.dir / f"{sid}.json").unlink(missing_ok=True)
                continue
            tmp = self.dir / f"{sid}.tmp"
            with suppress(OSError):
                tmp.write_text(json.dumps(item))
                tmp.replace(self.dir / f"{sid}.json")


# One asyncio.Event per session: set (and replaced) on every change, so each open
# /events stream wakes at once instead of the page polling.
signals: dict[str, asyncio.Event] = {}


def signal(sid: str) -> asyncio.Event:
    if sid not in signals:
        signals[sid] = asyncio.Event()
    return signals[sid]


def changed(sid: str) -> None:
    old = signals.pop(sid, None)
    if old:
        old.set()
    list_signal_fire()


list_signals: list[asyncio.Event] = [asyncio.Event()]


def list_signal_fire() -> None:
    list_signals[0].set()
    list_signals[0] = asyncio.Event()


sessions = Sessions()


# The goal and its milestones also live in the REA mod/rewrite whose agent session this is (REA's
# Goals tab; a fresh session for that project sees what is already proven). REA finds the project by
# session id; sessions that belong to no REA project are simply not saved there.
goal_mirrored: dict[str, str] = {}


def mirror_goal_if_changed(sid: str, item: dict[str, Any] | None) -> None:
    goal = (item or {}).get("goal")
    mark = json.dumps(goal, sort_keys=True, default=str)
    if goal_mirrored.get(sid) == mark or (sid not in goal_mirrored and goal is None):
        goal_mirrored[sid] = mark
        return
    goal_mirrored[sid] = mark
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def post() -> None:
        with suppress(Exception):
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{REA_API}/goals/session/{sid}", headers={"X-REA": "1"}, json={"goal": goal})

    loop.create_task(post())
runners: dict[str, asyncio.Task] = {}
gpu_turns: dict[str, dict[str, Any]] = {}      # queue job id -> {"event", "token"}
gpu_entries: dict[str, Any] = {}                # queue entry id -> live GpuTurn or GpuSlot
models_cache: dict[str, Any] = {}
live_streams: dict[str, dict[str, str]] = {}  # transient text shown while Hermes streams
compress_live: dict[str, dict[str, Any]] = {}  # auto-compression progress (Hermes rocsy plugin + llama.cpp /slots)
compress_pollers: dict[str, asyncio.Task] = {}
prefill_live: dict[str, dict[str, Any]] = {}   # model loading / reading the prompt before the first token


def compact_terminal(events: list[dict[str, Any]], tool_id: Any) -> None:
    """A terminal (and H3 Agentic's mirrored log) re-sends its whole output on every update - one
    long command left 243 copies of 24 KB. Keep the first event as the panel's place in the
    timeline (text blanked) and drop the copies after it; the newest event carries the output."""
    first = True
    keep: list[dict[str, Any]] = []
    for e in events:
        if e.get("kind") == "terminal" and e.get("tool_id") == tool_id:
            if not first:
                continue
            first = False
            e["text"], e["error"] = "", ""
        keep.append(e)
    events[:] = keep


def compact_terminals(events: list[dict[str, Any]]) -> bool:
    """Compact every terminal in an old session at once (only the newest copy keeps its output)."""
    latest: dict[Any, int] = {}
    for e in events:
        if e.get("kind") == "terminal" and e.get("tool_id") is not None:
            latest[e["tool_id"]] = e["i"]
    seen: set[Any] = set()
    keep: list[dict[str, Any]] = []
    for e in events:
        tid = e.get("tool_id") if e.get("kind") == "terminal" else None
        if tid is not None and e["i"] != latest[tid]:
            if tid in seen:
                continue
            seen.add(tid)
            e["text"], e["error"] = "", ""
        elif tid is not None:
            seen.add(tid)
        keep.append(e)
    shrunk = len(keep) != len(events)
    events[:] = keep
    return shrunk


# Replies and thoughts are stored whole (a 4,000 cap cut multi-clip scripts mid-prompt).
MAX_EVENT_TEXT = 200_000


def ev(s: dict[str, Any], kind: str, text: str, **extra: Any) -> None:
    if kind == "terminal" and extra.get("tool_id") is not None:
        compact_terminal(s["events"], extra["tool_id"])
    s["events"].append({"i": s["next_event"], "t": now(), "kind": kind, "text": text, **extra})
    s["next_event"] += 1
    if len(s["events"]) > MAX_EVENTS:
        del s["events"][: len(s["events"]) - MAX_EVENTS]
    s["updated"] = now()
    sessions.save(s["id"])


def set_state(s: dict[str, Any], state: str, stage: str = "") -> None:
    s["state"], s["stage"], s["updated"] = state, stage, now()
    sessions.save(s["id"])


def unfinished(s: dict[str, Any]) -> list[dict[str, Any]]:
    return [j for j in s.get("jobs", []) if j["state"] not in studios.DONE]


# Jobs that never touch the GPU (REA test runs: the game on a software-rendered virtual display).
# While only these run, the session keeps its GPU turn and the model stays loaded: unloading it
# after every turn made each REA test cycle reload the 27B model and re-read its whole 40-80K
# context from scratch, and a compression right after resume paid that cold prefill too.
CPU_JOB_KINDS = {"reatest"}
held_turns: dict[str, "GpuTurn"] = {}
parked_turns: dict[str, "GpuTurn"] = {}     # the model kept loaded while a question waits for the user


async def park_turn(s: dict[str, Any], turn: "GpuTurn", minutes: int) -> None:
    """Keep the model loaded (and the GPU slot) while the user answers its question, so the reply needs no
    reload. Let go when anything else queues for the GPU, the model is gone, the session stops, or the time is up."""
    sid = s["id"]
    until = now() + minutes * 60 if minutes > 0 else None
    turn.parked_until = until                         # the countdown under the question (public "parked")
    changed(sid)
    try:
        while parked_turns.get(sid) is turn:
            await asyncio.sleep(4)
            if parked_turns.get(sid) is not turn:
                return                                # the reply took it over
            gone = sid not in sessions.items or s.get("stop")
            loaded = await llama_loaded()
            if gone or (until and now() >= until) or loaded == [] or await gpu_wanted_elsewhere(turn):
                break
    except asyncio.CancelledError:
        pass
    if parked_turns.get(sid) is turn:
        parked_turns.pop(sid, None)
        changed(sid)
        with suppress(Exception):
            await turn.release()


def cpu_only_wait(s: dict[str, Any]) -> bool:
    left = unfinished(s)
    return bool(left) and all(j.get("kind") in CPU_JOB_KINDS for j in left)


async def gpu_wanted_elsewhere(turn: "GpuTurn") -> bool:
    """Someone else is queued for the GPU (a render, another session): stop holding it."""
    try:
        entries = await studios.queue()
    except Exception:  # noqa: BLE001
        return True                                   # can't tell: behave as before
    return any(e.get("state") != "active" and e.get("entry_id") != turn.entry for e in entries)


def new_session(prompt: str) -> dict[str, Any]:
    sid = uuid.uuid4().hex
    title = re.sub(r"\s+", " ", prompt).strip()
    s = {"id": sid, "title": title[:80] + ("…" if len(title) > 80 else ""), "created": now(), "updated": now(),
         "state": "starting", "stage": "", "turns": [], "events": [], "next_event": 0, "jobs": [],
         "run_id": None, "pending": None, "resumes": 0, "jobs_this_message": 0, "summary": "", "error": "",
         "named": ""}          # "" = still the first words of the prompt; "agent" or "user" once named
    sessions.items[sid] = s
    return s


def clean_title(text: str) -> str:
    title = re.sub(r"\s+", " ", str(text or "")).strip().strip("\"'“”‘’`").rstrip(".:;,")
    return title[:80].rstrip() + ("…" if len(title) > 80 else "")


def set_title(s: dict[str, Any], title: str, by: str) -> None:
    s["title"], s["named"] = title, by
    sessions.save(s["id"])


def public(s: dict[str, Any], since: int = -1) -> dict[str, Any]:
    out = {k: v for k, v in s.items() if k not in ("events", "turns")}
    out["events"] = [e for e in s["events"] if e["i"] > since]
    out["live"] = live_streams.get(s["id"], {}).copy()
    out["compress"] = dict(compress_live[s["id"]]) if s["id"] in compress_live else None
    out["prefill"] = dict(prefill_live[s["id"]]) if s["id"] in prefill_live else None
    parked = parked_turns.get(s["id"])
    out["parked"] = {"until": getattr(parked, "parked_until", None)} if parked else None
    out["turns"] = s["turns"]
    out["busy"] = s["state"] in BUSY
    out["attachments"] = public_attachments(s.get("attachments"))
    out["boards"] = public_boards(s)
    out["queued"] = [{"text": q["text"][:200]} for q in s.get("queued") or []]
    out["local"] = bool(config.load()["gpu_guard"] and is_local(config.load(), s))
    return out


# ---------------- attachments ----------------
# Each upload is ATTACH_DIR/<id>/<name> + meta.json. It belongs to no session until a message
# carries it; then the session lists it as att1, att2, ... Unsent uploads are swept after a day.

ATTACH_ID = re.compile(r"[0-9a-f]{32}")
PINNED_ID = re.compile(r"pinned-([0-9a-f]{20})")


def attachment_kind(mime: str) -> str:
    head = mime.split("/")[0]
    return head if head in ("image", "video", "audio") else "file"


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w.() -]+", "_", Path(name or "file").name).strip(" .") or "file"
    return name[-120:]


def pinned_meta(aid: str) -> dict[str, Any] | None:
    """Resolve a pin to its original file; no chat upload or file copy is made."""
    match = PINNED_ID.fullmatch(aid or "")
    if not match:
        return None
    with suppress(OSError, ValueError, TypeError, AttributeError):
        entry = json.loads(PINNED_INDEX.read_text()).get(match.group(1), {})
        if entry.get("pinned") not in (True, "true"):
            return None
        relative = str(entry.get("path") or "")
        path_part = Path(relative)
        if not relative or path_part.is_absolute() or ".." in path_part.parts:
            return None
        if hashlib.sha256(relative.encode()).hexdigest()[:20] != match.group(1):
            return None
        root = PINNED_ROOT.resolve(strict=True)
        source = root / path_part
        path = source.resolve(strict=True)
        if not path.is_relative_to(root) or source.is_symlink() or not path.is_file():
            return None
        stat = path.stat()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return {"id": aid, "name": path.name, "mime": mime, "kind": attachment_kind(mime),
                "size": stat.st_size, "total": stat.st_size, "complete": True,
                "url": p(f"/api/attachments/{aid}"), "hermes_path": f"{HERMES_PINNED_DIR}/{relative}",
                "pinned_path": str(path), "source": f"pinned:{match.group(1)}", "note": entry.get("note", "")}
    return None


def rag_pinned_ids(text: str, explicit: list[str]) -> list[str]:
    """Find pinned files whose saved phrases occur in this user message."""
    if not text or text.startswith("/") or len(explicit) >= 20:
        return []
    try:
        pins = json.loads(PINNED_INDEX.read_text())
        groups = json.loads(PINNED_GROUPS_INDEX.read_text()) if PINNED_GROUPS_INDEX.exists() else {}
        if not isinstance(pins, dict) or not isinstance(groups, dict):
            return []
    except (OSError, ValueError):
        log.warning("Could not read pinned RAG terms", exc_info=True)
        return []

    folded = text.casefold()

    def matches(entry: dict) -> bool:
        terms = entry.get("rag_terms", [])
        if not isinstance(terms, list):
            return False
        for phrase in terms:
            if not isinstance(phrase, str) or not phrase.strip():
                continue
            pattern = r"(?<!\w)" + r"\s+".join(re.escape(word) for word in phrase.casefold().split()) + r"(?!\w)"
            if re.search(pattern, folded):
                return True
        return False

    chosen = set(explicit)
    found = []

    def add(file_id: str) -> None:
        aid = f"pinned-{file_id}"
        if aid not in chosen and len(explicit) + len(found) < 20 and pinned_meta(aid):
            chosen.add(aid)
            found.append(aid)

    for group in groups.values():
        if isinstance(group, dict) and matches(group):
            for file_id in group.get("files", []):
                if isinstance(file_id, str):
                    add(file_id)
    for file_id, entry in pins.items():
        if isinstance(entry, dict) and entry.get("pinned") in (True, "true") and matches(entry):
            add(file_id)
    return found


def pinned_path(a: dict[str, Any]) -> Path:
    if PINNED_ID.fullmatch(a.get("id", "")):
        fresh = pinned_meta(a["id"])
        if not fresh:
            raise studios.StudioError(f"Pinned file {a['name']} is no longer available")
        return Path(fresh["pinned_path"])
    return ATTACH_DIR / a["id"] / a["name"]


def attachment_meta(aid: str) -> dict[str, Any] | None:
    if PINNED_ID.fullmatch(aid or ""):
        return pinned_meta(aid)
    if not ATTACH_ID.fullmatch(aid or ""):
        return None
    with suppress(OSError, ValueError):
        return json.loads((ATTACH_DIR / aid / "meta.json").read_text())
    return None


def public_attachments(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [{k: a.get(k) for k in ("id", "ref", "name", "mime", "kind", "size", "url")} for a in items or []]


def claim_attachments(s: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
    """Attach uploads to a session (att1, att2, ...); unknown ids are refused."""
    out = []
    have = {a["id"]: a for a in s.setdefault("attachments", [])}
    for aid in dict.fromkeys(ids):
        if aid in have:
            out.append(have[aid])
            continue
        meta = attachment_meta(aid)
        if not meta or not meta.get("complete", True):
            raise HTTPException(422, "An attachment is missing or didn't finish uploading; attach it again")
        meta.update(ref=f"att{len(s['attachments']) + 1}", session=s["id"], studio={})
        if ATTACH_ID.fullmatch(aid):
            (ATTACH_DIR / aid / "meta.json").write_text(json.dumps(meta))
        s["attachments"].append(meta)
        out.append(meta)
    if out:
        sessions.save(s["id"])
    return out


def drop_attachments(s: dict[str, Any]) -> None:
    import shutil
    for a in s.get("attachments") or []:
        if ATTACH_ID.fullmatch(a.get("id", "")):
            shutil.rmtree(ATTACH_DIR / a["id"], ignore_errors=True)


def sweep_uploads() -> None:
    """Uploads never sent with a message, older than a day."""
    import shutil
    with suppress(OSError):
        for folder in ATTACH_DIR.iterdir():
            meta = attachment_meta(folder.name)
            if meta is None or (not meta.get("session") and meta.get("t", 0) < now() - 86400):
                if ATTACH_ID.fullmatch(folder.name):
                    shutil.rmtree(folder, ignore_errors=True)


async def attachment_in(s: dict[str, Any], ref: str, service: str) -> dict[str, Any]:
    """The studio's copy of an attachment, uploaded on first use and remembered."""
    a = next((a for a in s.get("attachments") or [] if a["ref"] == ref.strip().lower()), None)
    if not a:
        known = ", ".join(x["ref"] for x in s.get("attachments") or []) or "none"
        raise studios.StudioError(f"no attachment {ref} (this session has: {known})")
    # Vid copies live in its private Agentic pool ("Vid-refs"; the old "Vid" key was the user's pool)
    # and H3 cleans them up after use, so a remembered copy is re-checked before it's reused.
    key = "Vid-refs" if service == "Vid" else service
    if cached := a.get("studio", {}).get(key):
        if service != "Vid" or cached.get("id") in await studios.vid_reference_ids():
            return cached
    if service == "Krea" and a["kind"] != "image":
        raise studios.StudioError(f"{ref} is a {a['kind']}; Image only takes images")
    result = await studios.upload(service, str(pinned_path(a)), a["name"], a["mime"])
    a.setdefault("studio", {})[key] = result
    with suppress(OSError):
        (ATTACH_DIR / a["id"] / "meta.json").write_text(json.dumps(a))
    sessions.save(s["id"])
    return result


def refs_arg(args: dict[str, Any]) -> list[str]:
    return [r for r in re.split(r"[,\s]+", str(args.get("ref") or "")) if r]


def own_job(s: dict[str, Any], ref: str) -> dict[str, Any] | None:
    return next((j for j in s["jobs"] if j["ref"] == ref.strip().lower()), None)


def finished_job(s: dict[str, Any], ref: str) -> dict[str, Any]:
    j = own_job(s, ref)
    if not j:
        raise studios.StudioError(f"no attachment or job {ref} (attachments: "
                                  f"{', '.join(a['ref'] for a in s.get('attachments') or []) or 'none'}; see jobs for job refs)")
    if j["state"] not in ("complete", "completed") or not j.get("outputs"):
        raise studios.StudioError(f"{ref} has no finished output yet ({j['state']}); wait for it first")
    return j


async def job_output_in_vid(s: dict[str, Any], j: dict[str, Any], role: str = studios.VID_REF_ROLE) -> dict[str, Any]:
    """A finished job's first output (Krea image, Mus song) as a Vid reference, uploaded once per role
    (again if Vid has since cleaned that copy up)."""
    cache = j.setdefault("in_vid", {})
    if role in cache and cache[role].get("id") in await studios.vid_reference_ids():
        return cache[role]
    out = j["outputs"][0]
    folder = DATA / "tmp"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{j['job_id']}-{uuid.uuid4().hex[:6]}"
    try:
        mime = await studios.fetch_output(out["url"], str(path))
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "audio/mpeg": ".mp3", "audio/wav": ".wav",
               "audio/x-wav": ".wav", "audio/flac": ".flac", "audio/mp4": ".m4a", "audio/ogg": ".ogg", "video/mp4": ".mp4"}.get(mime)
        if not ext:
            ext = {"image": ".png", "audio": ".mp3", "video": ".mp4"}.get(out["kind"], "")
            mime = {".png": "image/png", ".mp3": "audio/mpeg", ".mp4": "video/mp4"}.get(ext, mime)
        record = await studios.upload("Vid", str(path), f"{j['ref']}{ext}", mime, role)
    finally:
        path.unlink(missing_ok=True)
    cache[role] = record
    sessions.save(s["id"])
    return record


async def song_seconds(j: dict[str, Any]) -> float:
    if not (j.get("meta") or {}).get("duration"):
        with suppress(studios.StudioError):        # jobs that finished before durations were kept
            j["meta"] = {"duration": (await studios.job(j["service"], j["job_id"])).get("duration")}
    try:
        return float((j.get("meta") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


async def vid_refs(s: dict[str, Any], refs: list[str], prompt: str) -> tuple[list[str], str]:
    """Upload attachments and finished image jobs (image1) to Vid; {att1} / {image1} in the prompt
    becomes its H3 label (<Picture 3>). Song jobs (mus1) aren't H3 references: see soundtrack_note."""
    ids = []
    for ref in refs:
        if any(a["ref"] == ref.strip().lower() for a in s.get("attachments") or []):
            record = await attachment_in(s, ref, "Vid")
        else:
            j = finished_job(s, ref)
            if j["service"] == "Mus":
                continue
            if j["service"] != "Krea":
                raise studios.StudioError(f"{ref} is a {svc(j['service'])} job; only images (image1, ...) and attachments are "
                                          "references. Continue a video with --resume, join clips with montage")
            record = await job_output_in_vid(s, j)
        ids.append(record["id"])
        prompt = re.sub(r"\{\s*" + re.escape(ref) + r"\s*\}", record.get("token") or ref, prompt, flags=re.I)
    return ids, prompt


MUSIC_VIDEO = re.compile(r"music.?video|\bsong\b|soundtrack|\bmv\b|to the (music|beat|track)", re.I)


async def soundtrack_note(s: dict[str, Any], refs: list[str], request: str) -> str:
    """Tell Vid's Agentic planner how long the song is, so the clips add up to the whole track.
    Uses the song jobs passed with --ref, else the latest finished song when the request is a music video."""
    songs = [j for r in refs if (j := own_job(s, r)) and j["service"] == "Mus" and j["state"] in ("complete", "completed")]
    if not songs and MUSIC_VIDEO.search(request):
        songs = [j for j in s["jobs"] if j["service"] == "Mus" and j["state"] in ("complete", "completed")][-1:]
    notes = []
    for j in songs:
        seconds = await song_seconds(j)
        if not seconds:
            continue
        clips = max(1, round(seconds / 15 + 0.49))
        notes.append(f"SOUNDTRACK: the music video is cut to a {seconds:.0f} s song ({j['ref']}: \"{j['label'][:120]}\"). "
                     f"Plan clips that add up to about {seconds:.0f} s in total (for example {clips} clips of about "
                     f"{seconds / clips:.0f} s), with energy and cuts that follow the song's sections. The song is laid over "
                     "the finished clips afterwards and replaces their audio, so the clips need no music, singing or dialogue.")
    return "\n".join(notes)


# ---------------- GPU turn (local models) ----------------

async def lock_call(path: str, body: dict[str, Any], timeout: float = 10) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(LOCK_URL + path, json=body, headers={"X-Queue-Internal-Secret": QUEUE_SECRET})
        response.raise_for_status()
        return response.json()


class GpuTurn:
    """The shared GPU slot while a local model thinks. Front of the line; ComfyUI's VRAM
    is freed once it is ours; on release the model is unloaded first."""

    def __init__(self, s: dict[str, Any]):
        self.s = s
        self.key: str | None = None
        self.token: str | None = None
        self.entry: str | None = None
        self.heart: asyncio.Task | None = None

    async def acquire(self) -> None:
        key = f"{self.s['id']}-{self.s['next_event']}"
        self.key = key
        turn = gpu_turns[key] = {"event": asyncio.Event(), "token": None}
        try:
            entry = await lock_call("/enqueue", {"app": "Agent", "job_id": key, "label": f"Master Agent: {self.s['title'][:60]}",
                                                 "priority": True})
            self.entry = entry.get("entry_id")
            if self.entry:
                gpu_entries[self.entry] = self
            if not turn["event"].is_set():
                set_state(self.s, "waiting_for_gpu", "Waiting for the GPU (front of the queue)")
                ev(self.s, "system", "Waiting for the GPU so the model can load (front of the queue)")
            await turn["event"].wait()
            self.token = turn["token"]
        finally:
            # A proceed callback can win the race with cancellation of the waiter.
            # Retain its token so release can return the active queue entry.
            self.token = self.token or turn["token"]
            gpu_turns.pop(key, None)
        self.heart = asyncio.create_task(self._heartbeat())
        with suppress(Exception):
            async with httpx.AsyncClient(timeout=20) as client:
                await client.post(COMFY_URL + "/free", json={"unload_models": True, "free_memory": True})

    async def _heartbeat(self) -> None:
        while self.token:
            await asyncio.sleep(20)
            with suppress(Exception):
                await lock_call("/heartbeat", {"token": self.token}, timeout=6)

    async def release(self) -> None:
        if self.entry:
            gpu_entries.pop(self.entry, None)
        if self.heart:
            self.heart.cancel()
        # Only a turn that held the GPU unloads the model: a session stopped while it was still queued
        # used to unload (for up to SETTLE_LIMIT) the model another session was thinking with.
        try:
            if self.token:
                await save_llm_context(self.s)
                await unload_llm_settled(self.s)
        finally:
            # Shielded and in a finally: a stop that lands during the unload must
            # still hand the GPU back instead of leaving it to the TTL.
            token, self.token = self.token, None
            if token:
                with suppress(Exception):
                    await asyncio.shield(lock_call("/release", {"token": token}))
            elif self.entry:
                with suppress(Exception):
                    await lock_call(f"/dequeue/{self.entry}", {})


class GpuSlot:
    """The shared GPU slot for a job the Agent service runs itself (Motion renders): front of the
    line, ComfyUI's cached models freed and the local model unloaded before it starts."""

    def __init__(self, s: dict[str, Any], key: str, label: str, job_ref: str):
        self.s, self.key, self.label, self.job_ref = s, key, label, job_ref
        self.token: str | None = None
        self.entry: str | None = None
        self.heart: asyncio.Task | None = None

    async def acquire(self, on_wait=None) -> None:
        turn = gpu_turns[self.key] = {"event": asyncio.Event(), "token": None}
        try:
            entry = await lock_call("/enqueue", {"app": "Agent", "job_id": self.key, "label": self.label[:80],
                                                 "priority": config.load()["priority"]})
            self.entry = entry.get("entry_id")
            if self.entry:
                gpu_entries[self.entry] = self
            if not turn["event"].is_set() and on_wait:
                on_wait()
            await turn["event"].wait()
            self.token = turn["token"]
        finally:
            self.token = self.token or turn["token"]
            gpu_turns.pop(self.key, None)
        self.heart = asyncio.create_task(self._heartbeat())
        with suppress(Exception):
            async with httpx.AsyncClient(timeout=20) as client:
                await client.post(COMFY_URL + "/free", json={"unload_models": True, "free_memory": True})
        await unload_llm_settled(self.s)

    async def _heartbeat(self) -> None:
        while self.token:
            await asyncio.sleep(20)
            with suppress(Exception):
                await lock_call("/heartbeat", {"token": self.token}, timeout=6)

    async def release(self) -> None:
        if self.entry:
            gpu_entries.pop(self.entry, None)
        if self.heart:
            self.heart.cancel()
        token, self.token = self.token, None
        entry, self.entry = self.entry, None
        if token:
            with suppress(Exception):
                await lock_call("/release", {"token": token})
        elif entry:
            with suppress(Exception):
                await lock_call(f"/dequeue/{entry}", {})


def llama_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {LLAMA_KEY}"} if LLAMA_KEY else {}


async def unload_llm() -> None:
    with suppress(Exception):
        async with httpx.AsyncClient(timeout=30) as client:
            await client.get(LLAMA_URL + "/unload", headers=llama_headers())


async def llama_loaded() -> list[str] | None:
    """Models llama-swap has loaded right now; None when it can't be asked."""
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            response = await client.get(LLAMA_URL + "/running", headers=llama_headers())
            response.raise_for_status()
            return [str(m.get("model") or m.get("name") or "?") for m in response.json().get("running") or []]
    except Exception:  # noqa: BLE001
        return None


def slot_file(s: dict[str, Any]) -> str:
    return f"rocsy-{s['id']}.bin"


async def last_slot(client: httpx.AsyncClient, model: str) -> int:
    """The slot that served the latest request (llama.cpp with --parallel N has several; Strata one)."""
    with suppress(Exception):
        r = await client.get(f"{LLAMA_URL}/upstream/{model}/slots", headers=llama_headers())
        slots = [x for x in r.json() if isinstance(x, dict) and isinstance(x.get("id"), int)]
        if slots:
            return max(slots, key=lambda x: x.get("id_task") or -1)["id"]
    return 0


async def save_llm_context(s: dict[str, Any]) -> None:
    """Before the model is unloaded: park the conversation it holds on disk (llama-server's slot save, with
    --slot-save-path from llama-launch / hostctl's Strata command), so the next turn restores it instead of
    reading the whole prompt again."""
    s.pop("llm_saved", None)
    if not config.load()["cache_save"]:
        return
    for model in await llama_loaded() or []:
        with suppress(Exception):
            async with httpx.AsyncClient(timeout=60) as client:
                slot = await last_slot(client, model)
                r = await client.post(f"{LLAMA_URL}/upstream/{model}/slots/{slot}?action=save",
                                      json={"filename": slot_file(s)}, headers=llama_headers())
            if r.status_code == 200 and (n := int(r.json().get("n_saved") or 0)) > 0:
                s["llm_saved"] = {"model": model, "slot": slot, "tokens": n, "t": now()}
                return


async def restore_llm_context(s: dict[str, Any]) -> None:
    """After the GPU is ours again: load the model and give it back this session's parked conversation."""
    saved = s.get("llm_saved") or {}
    model = run_model(s, config.load())[0]
    if saved.get("model") != model or not config.load()["cache_save"]:
        return
    s.pop("llm_saved", None)                          # one use: a later turn's prompt has moved on
    set_state(s, "thinking", "Loading the model with this conversation's cache")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(360, connect=10)) as client:
            await client.get(f"{LLAMA_URL}/upstream/{model}/health", headers=llama_headers())
            r = await client.post(f"{LLAMA_URL}/upstream/{model}/slots/{int(saved.get('slot') or 0)}?action=restore",
                                  json={"filename": slot_file(s)}, headers=llama_headers())
        if r.status_code == 200:
            n = int(r.json().get("n_restored") or 0)
            ev(s, "system", f"Restored this conversation's cache in the model ({n:,} tokens it doesn't have to re-read)")
    except Exception:  # noqa: BLE001 - the run just reads its prompt again
        log.warning("restoring the model cache for %s failed", s["id"], exc_info=True)


QUIET_SECONDS = 8       # the model must stay unloaded this long before the GPU is handed over
SETTLE_LIMIT = 150      # give up waiting (and warn) after this long


async def unload_llm_settled(s: dict[str, Any] | None = None) -> bool:
    """Unload the model and keep it unloaded before the GPU goes to the renders. Hermes keeps
    calling the model after a run ends (its background memory/skill review, a late steer), which
    used to load it straight back next to a render: CUDA OOM (2026-10-08). True once it stayed
    unloaded for QUIET_SECONDS."""
    await unload_llm()
    start, quiet, reloads = now(), now(), 0
    while now() - start < SETTLE_LIMIT:
        await asyncio.sleep(2)
        loaded = await llama_loaded()
        if loaded is None:
            return False            # can't tell; the unload above is all we can do
        if not loaded:
            if now() - quiet >= QUIET_SECONDS:
                return True
            continue
        reloads += 1
        if s is not None and reloads == 1:
            ev(s, "system", f"Hermes loaded the model again after the turn ({', '.join(loaded)}; its background review); "
                            "unloading it before the GPU goes to the renders")
        await unload_llm()
        quiet = now()
    if s is not None:
        ev(s, "warn", f"The model kept reloading for {SETTLE_LIMIT} s after the turn; handing the GPU over anyway (renders may run out of VRAM)")
    return False


# ---------------- Hermes ----------------

async def check_model_choice(choice: "ModelChoice | None") -> dict[str, str] | None:
    """A per-session model or thinking override. None = no change,
    {} = back to the Agentic settings."""
    if choice is None:
        return None
    if choice.reasoning and choice.reasoning not in REASONING:
        raise HTTPException(422, f"Unknown thinking level {choice.reasoning}")
    if not choice.model:
        return {"reasoning": choice.reasoning} if choice.reasoning else {}
    if choice.provider.startswith("cli:"):
        # An external coding CLI on the host (Settings → Sub-agents); hostctl checks the CLI and model name.
        return {"model": choice.model, "provider": choice.provider, "reasoning": choice.reasoning}
    try:
        inventory = await hermes_models()
    except Exception:
        inventory = None  # Hermes unreachable: let the run report a bad model itself
    if inventory:
        known = {(pr["slug"], m) for pr in inventory["providers"] for m in pr["models"]}
        if (choice.provider, choice.model) not in known and not any(m == choice.model for _, m in known):
            raise HTTPException(422, f"Hermes has no model {choice.model}")
    return {"model": choice.model, "provider": choice.provider, "reasoning": choice.reasoning}


def set_model_choice(s: dict[str, Any], choice: dict[str, str] | None) -> None:
    if choice is None:
        return
    if choice:
        s["model_override"] = choice
    else:
        s.pop("model_override", None)


def run_model(s: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str, str]:
    """(model, provider, reasoning) for a run: the session's override, else the Agentic settings."""
    o = s.get("model_override") or {}
    if o.get("model"):
        return o["model"], o.get("provider") or "", o.get("reasoning") or cfg["reasoning"]
    return cfg["model"], cfg["provider"], o.get("reasoning") or cfg["reasoning"]


def hermes_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {HERMES_KEY}"} if HERMES_KEY else {}


async def hermes_models() -> dict[str, Any]:
    if models_cache.get("t", 0) > now() - 60:
        return models_cache["data"]
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(HERMES_URL + "/api/model/options", headers=hermes_headers())
        response.raise_for_status()
        data = response.json()
    providers = []
    for pr in data.get("providers", []):
        names = [m if isinstance(m, str) else (m.get("id") or m.get("name")) for m in pr.get("models") or []]
        names = [m for m in names if m]
        # moa is a meta-provider; OpenCode's free tier refuses use outside OpenCode (HTTP 403).
        if pr.get("authenticated") and names and pr.get("slug") != "moa" and not pr["slug"].startswith("opencode"):
            providers.append({"slug": pr["slug"], "name": pr.get("name") or pr["slug"], "models": names,
                              "local": pr["slug"].split(":")[0] in LOCAL_PROVIDERS, "current": bool(pr.get("is_current"))})
    providers.sort(key=lambda x: (not x["current"], not x["local"], x["name"].lower()))
    result = {"providers": providers, "default": {"model": data.get("model"), "provider": data.get("provider")}, "reasoning": REASONING}
    models_cache.update(t=now(), data=result)
    return result


def history_text(s: dict[str, Any], turns: bool = True) -> str:
    """Earlier turns of this session, for the next Hermes run (only the jobs when Hermes
    keeps the conversation itself)."""
    lines = []
    for turn in s["turns"][:-1] if turns else []:
        who = "USER" if turn["role"] == "user" else "YOU (summary)"
        lines.append(f"{who}: {turn['text'][:2000]}")
    if s["jobs"]:
        lines.append("YOUR JOBS SO FAR:")
        tests = [j for j in s["jobs"] if j.get("kind") in ("gametest", "reatest")]
        lines.extend(job_line(j, latest=not tests or j is tests[-1]) for j in s["jobs"][-30:])
    return ("\nEARLIER IN THIS SESSION:\n" + "\n".join(lines) + "\n") if lines else ""


def job_line(j: dict[str, Any], latest: bool = True) -> str:
    what = (f"{j['ref']} {svc(j['service'])}{' Agentic' if j.get('kind') == 'agentic' else ''}"
            f"{' (' + j['model'] + ')' if j.get('model') else ''} {j['state']}")
    if j.get("kind") == "agentic" and j.get("stage") and j["state"] not in studios.DONE:
        what += f" ({j['stage'][:120]})"
    if j.get("progress") is not None and j["state"] not in studios.DONE:
        what += f" {j['progress']}%"
    if j.get("error"):
        what += f" — error: {j['error'][:200]}"
    if j.get("kind") == "agentic" and j.get("outputs"):
        n = len(j["outputs"])
        retried = sum(1 for o in j.get("iterations") or [] if o.get("phase") == "full" and not o.get("selected"))
        what += (" — the finished video (its Post-Edit/Stitch)" if j.get("full_output") else
                 f" — {n} final clip(s) in story order" + (f", `fetch {j['ref']}` gives {j['ref']}-1..{j['ref']}-{n}" if n > 1 else "")
                 + (f" ({retried} replaced retry render(s) not included)" if retried else ""))
    elif j.get("outputs"):
        what += f" — {len(j['outputs'])} output(s)"
    if j.get("summary"):
        # A play test's diagnosis is long: in full for the latest one, older ones keep their first line.
        cap = 1500 if j.get("kind") not in ("gametest", "reatest") else 4500 if latest else 500
        what += f" — {j['summary'][:cap]}"
    if j.get("kind") == "postedit" and (j.get("meta") or {}).get("transcript"):
        what += f" — Whisper heard ({j['meta'].get('language') or '?'}): \"{' / '.join(j['meta']['transcript'])[:400]}\""
    return f"- {what}: {j['label'][:120]}"


# A broad goal ("a full multiplayer game mode with AI players, a shop, objectives...") made the local model jump
# between features and lose track after every compression. Goals are split into small milestones
# first; each turn works on the current one only.
MILESTONES_MIN, MILESTONES_MAX = 3, 12


def current_part(g: dict[str, Any]) -> dict[str, Any] | None:
    parts = g.get("parts") or []
    return parts[g.get("part", 0)] if parts and g.get("part", 0) < len(parts) else None


def parts_overview(g: dict[str, Any]) -> str:
    return "\n".join(f"  {i + 1}. [{'done' if p['status'] == 'done' else 'NOW' if i == g.get('part', 0) else 'todo'}] {p['text']}"
                     + (f" (proof: {p['evidence'][:160]})" if p.get("evidence") else "") for i, p in enumerate(g.get("parts") or []))


def goal_block(s: dict[str, Any]) -> str:
    g = s.get("goal")
    if not g or g["status"] != "active":
        return ""
    if g.get("until"):
        left = max(0, round((g["until"] - now()) / 60))
        return (f"\nSCHEDULED RUN (the user's schedule; about {left} min left, it ends at {g.get('until_text', 'the set time')} and "
                f"you are stopped then):\n{g['text']}\nWork in batches: start jobs, `$R wait`, end the turn; you are resumed "
                "until the time is up. No milestones and no `$R goal done` for this: just keep producing, varied, not repeats.\n")
    limit = f"of at most {g['max_turns']}" if g["max_turns"] else "with no turn limit"
    part = current_part(g)
    if not part:
        return (f"\nSTANDING GOAL (set by the user with /goal; turn {g['turns'] + 1} {limit}):\n{g['text']}\n"
                f"FIRST, before any other work: split this goal into {MILESTONES_MIN}-{MILESTONES_MAX} small milestones, in build order, each ONE "
                "testable outcome with how it is proven (e.g. a log line), and register them with "
                "`$R goal parts \"milestone 1 || milestone 2 || ...\"`. Then work only on milestone 1.\n")
    return (f"\nSTANDING GOAL (set by the user with /goal; turn {g['turns'] + 1} {limit}), split into milestones:\n"
            f"{parts_overview(g)}\n"
            f"CURRENT MILESTONE ({g['part'] + 1} of {len(g['parts'])}): {part['text']}\n"
            "Work ONLY on the current milestone. When it is proven (checked results, quote the evidence), run "
            "`$R goal done \"evidence\"`: that closes it and moves you to the next one. The whole goal is achieved after the "
            "last milestone. If a milestone turns out wrong or too big, re-split the remaining ones with `$R goal parts \"...\"` "
            f"(done milestones are kept).\nThe whole goal, for context only:\n{g['text'][:3000]}\n"
            "You are re-invoked automatically after each turn, so end each turn at a natural checkpoint and say what is left.\n")


def naming_block(s: dict[str, Any]) -> str:
    if s.get("named") or s.get("named_asks", 0) >= 2:
        return ""
    s["named_asks"] = s.get("named_asks", 0) + 1
    return ("\nSESSION NAME: this session is still called \"" + s["title"][:80] + "\" (the user's first words). Early in this "
            "turn, give it a short, specific name of 2-6 words that says what is being made, e.g. "
            "`$R title \"Neon rooftop chase\"`. No quotes, emoji or trailing punctuation inside the name.\n")


def browser_block(state: dict[str, Any] | None) -> str:
    if not state or not state.get("connected"):
        return ""
    return (f"\nLIVE BROWSER: the user connected your browser tools (browser_navigate, browser_snapshot, browser_click, ...) "
            f"to a Chromium window on their desktop ({state.get('browser') or 'Chromium'}, CDP {state.get('live')}, Hermes' "
            "debug profile). Use them when the request needs the web. The user can see that window: don't close tabs or "
            "navigate away from their pages unless the task needs it.\n")


def perm(cfg: dict[str, Any], key: str) -> Any:
    return cfg["permissions"].get(key, PERM_DEFAULTS[key])


def permissions_block(cfg: dict[str, Any]) -> str:
    lines = []
    for x in PERMISSIONS:
        value = perm(cfg, x["key"])
        text = x[value] if "choices" in x else x["on" if value else "off"]
        lines.append(f"- {x['label']}: {text.format(workspace=HERMES_WORKSPACE)}")
    lines.append(f"- Your workspace for downloads, drafts and scripts is {HERMES_WORKSPACE}; `$R share` a file there to show it to the user.")
    if perm(cfg, "web") and perm(cfg, "files") != "none":
        engine = IMAGE_ENGINES[cfg["image_engine"]]["label"]
        how = (f"searches {engine} by itself and lists numbered results with sizes and sites; then `$R grab imgN [--name NAME]`"
               if cfg["image_mode"] == "headless" else
               f"opens {engine} in the user's visible Chromium (the /browser window) and tells you to search there with your "
               "browser tools; open a big result to get its original image URL, then `$R grab URL [--name NAME]`")
        lines.append(f"- Reference pictures from the web: FIRST `$R images \"subject official art\"`, the user's chosen image search: "
                     f"it {how}. Search the whole web: don't add 'wiki' to the query and don't go to wikis first (their page "
                     "pictures are often tiny sprites/thumbnails). grab downloads, checks and attaches it in one step (att5 for "
                     "--ref) and also takes a direct image URL or a page that shows it. Don't curl images yourself, and never "
                     "build or guess URLs. Look at every grabbed picture with your vision tool before using it. If 3 grabs "
                     "fail, stop: go on without it, or `ask` the user for a picture.")
    if perm(cfg, "web") and perm(cfg, "terminal") and perm(cfg, "files") != "none":
        lines.append(f"- Video downloads: `yt-dlp` is on your PATH (kept up to date in the Manager) for any site it supports, e.g. "
                     f"`yt-dlp -P {HERMES_WORKSPACE} -f 'bv*[height<=1080]+ba/b' --merge-output-format mp4 --no-playlist URL` "
                     "(`-x --audio-format mp3` for audio, `--download-sections '*1:05-1:20'` for a part). A finished download "
                     "shows up in the user's chat by itself. For a YouTube/TikTok link the user sends, `$R import-media` (with "
                     "trim/crop) is simpler. If yt-dlp says a site broke, tell the user to update it in Models → Manager.")
    if perm(cfg, "files") != "none":
        lines.append("- Videos: whenever the user asks you to find/search/pick moments in a video, or to trim or crop one, show the work "
                     "on a video board in their chat: `$R scan start REF \"what to find\"`, then `scan look` + `scan mark` each window, "
                     "`scan pick` the best, `scan cut` the trims/crop (the same numbers you pass to postedit/import-media), `scan done`.")
    lines.append("- Never touch credentials, .env files or other people's sessions, and never send the user's files or data anywhere "
                 "they didn't ask for. Studio settings belong to the user: change them only when asked.")
    return "PERMISSIONS (set by the user in Settings → Agentic):\n" + "\n".join(lines) + "\n"


H3_SCRIPT_TURNS = 3      # the detected message and two follow-ups ("make it longer") keep the guide


def h3_script_block(s: dict[str, Any]) -> str:
    """When the user's message asks for a MiniMax H3 prompt ("script") or an H3 question, searched
    against the H3 prompting guides (guide_rag), hand the agent the matching guide sections."""
    text = next((t["text"] for t in reversed(s.get("turns") or []) if t["role"] == "user" and not t.get("review")), "")
    state = s.setdefault("h3_script", {})
    if state.get("msg") != s.get("msg"):          # a new user message: score it once
        wanted, score, why = guide_rag.h3_script_intent(text)
        if wanted:
            fresh = not state.get("left")
            state.update(left=H3_SCRIPT_TURNS, why=why, score=score, query=text[:2000])
            if fresh:
                ev(s, "system", f"H3 script request ({why}); giving the Master Agent the matching H3 prompting guide sections")
        elif state.get("left"):
            state.update(left=state["left"] - 1, query=(state.get("query", "") + "\n" + text)[-2000:])
        state["msg"] = s.get("msg")
    if not state.get("left"):
        return ""
    return ("\nH3 SCRIPT (found by searching the H3 prompting guides: " + str(state.get("why")) + "). The user wants a MiniMax H3 "
            "prompt written or fixed (or an H3 prompting question answered), not a render. Pick the mode from what they describe "
            "and attach: Text (T2VA, 3 fields), Keyframe (I2VA / FL2VA / L2VA: an instruction line, a blank line, the 3 fields) or "
            "Reference (ref2va, the six sections). Write it exactly in that official format in a fenced code block labelled h3 (```h3) they can paste "
            "into Video, then one line naming the mode and the references it expects. ONE H3 PROMPT IS AT MOST 15 SECONDS: "
            "when the asked length (or what the story needs) is longer, write several prompts, Clip 1, Clip 2... each 15 s or "
            "less and adding up to the requested time (40 s = 15 + 15 + 10), each in its own code block labelled with its clip "
            "number and length, timestamps restarting at 00:00 in each, the same characters/setting/style/voices throughout and "
            "each clip continuing from the end of the one before; finish with the total length. No length asked: one clip of "
            "up to 15 s, or more clips only when the story needs the time. Start no jobs unless they ask you to render "
            "it; then pass the finished prompt unchanged to `$R video` / `$R agentic`. More of the guide: `$R guide h3 \"what you "
            "need\"`.\nH3 GUIDE EXCERPTS (reference data):\n" + guide_rag.excerpts("h3", state.get("query") or text, 5, 9000) + "\n")


def attachments_block(s: dict[str, Any]) -> str:
    items = s.get("attachments") or []
    if not items:
        return ""
    lines = []
    pinned_ids = set()
    for a in items[-40:]:
        line = (f"- {a['ref']}: {a['kind']} \"{a['name']}\" ({a['size'] // 1024} KB) · "
                f"file {a['hermes_path']} · URL {SELF_URL}{p('/api/attachments/' + a['id'])}")
        match = PINNED_ID.fullmatch(a.get("id", ""))
        if match:
            pinned_ids.add(match.group(1))
            fresh = pinned_meta(a["id"])
            note = (fresh or a).get("note")
            if isinstance(note, str) and note.strip():
                line += "\n  User context: " + json.dumps(note[:2000], ensure_ascii=False)
        lines.append(line)
    if pinned_ids:
        with suppress(OSError, ValueError, TypeError, AttributeError):
            groups = json.loads(PINNED_GROUPS_INDEX.read_text())
            if isinstance(groups, dict):
                for group in groups.values():
                    if not isinstance(group, dict) or not isinstance(group.get("files"), list):
                        continue
                    note = group.get("note")
                    if isinstance(note, str) and note.strip() and pinned_ids.intersection(group["files"]):
                        name = str(group.get("name", "Group"))[:80]
                        lines.append(f"- Group {json.dumps(name, ensure_ascii=False)} context: "
                                     + json.dumps(note[:2000], ensure_ascii=False))
    return ("\nATTACHMENTS the user sent or matched from pinned RAG terms in this session "
            "(look at images with your vision tool on the file path; pass them to studios with --ref att1,att2; "
            "pass relevant file paths and context notes to subagents when delegating):\n" + "\n".join(lines) + "\n")


def build_input(s: dict[str, Any], request: str, browser: dict[str, Any] | None = None,
                split: bool = False) -> str | tuple[str, str]:
    """The run's text. split=True (a Hermes session that already holds this conversation)
    returns (instructions, input): the brief goes in as the run's system prompt, so it isn't
    stored again in the session every turn, and the input is only this turn's message."""
    cfg = config.load()
    allowed = [name for name, on in cfg["services"].items() if on]
    names = {"Vid": "Vid (video)", "Krea": "Image (still images; refs image1, image2...)", "Mus": "Mus (music)", "Trainer": "Trainer (LoRA training)",
             "Motion": "Motion (code-based video: Remotion/React)", "Voice": "Voice (speech with saved characters)"}
    if "Vid" not in allowed:
        video_note = ""
    elif cfg["use_agentic"] and cfg["video_model"] in studios.AGENTIC_VIDEO_MODELS:
        video_note = (("The video model is Wan 2.2: it has no sound and no Reference mode; a --ref image is used as the first "
                       "frame (image to video). " if cfg["video_model"] == "wan_2_2" else "")
                      + "Videos: use `$R agentic` for every video request. Vid's Agentic mode is better than single clips: it "
                      "decides how many clips and how long each is, chains continuations, uses the attachments, reviews the "
                      "renders and fixes them. Give it the user's whole idea (story, style, characters, dialogue, length) as the "
                      "request, plus anything extra as --guide. `$R video` is sent through Agentic mode too; add --plain 1 only "
                      "when the user asks for exactly one quick clip. `--resume REF` continues an earlier Agentic job's session.")
    else:
        video_note = ("Videos: `$R video` renders one clip of 5-15 s. Vid's Agentic mode supports MiniMax H3 and Wan 2.2; "
                      "use `$R agentic --model minimax_h3` (or wan_2_2) for a multi-clip video when the user wants that model.")
    marker = "\u2063REQUEST\u2063"
    values = {"session": s["id"], "script": SCRIPT, "services": ", ".join(names[n] for n in allowed) or "none (status only)",
              "request": marker if split else request, "history": history_text(s, turns=not split), "video_note": video_note,
              "permissions": permissions_block(cfg)}
    template = cfg["brief"] or DEFAULT_BRIEF
    if "{permissions}" not in template:
        template += "\n{permissions}"
    try:
        text = template.format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        ev(s, "warn", f"The Master Agent brief has a bad placeholder ({exc}); using the built-in brief")
        text = DEFAULT_BRIEF.format(**values)
    model_note = ("\nSTUDIO MODEL DEFAULTS (use these when the user does not name a model): "
                  + ", ".join(f"{kind}={cfg[f'{kind}_model']}" for kind in ("video", "voice", "image", "music"))
                  + ". An explicit user model choice overrides these defaults.\n")
    extra = model_note + review_block(cfg) + ask_block(s, cfg) + h3_script_block(s) + attachments_block(s) + browser_block(browser) + goal_block(s) + naming_block(s)
    if not split:
        return text + extra
    head, _, tail = text.partition(marker)
    head = re.sub(r"\n?USER REQUEST:\s*$", "\n", head.rstrip()) + "\n" + tail.strip()
    return head.strip() + "\n\nThe user's message for this turn is the user message; the conversation so far is this Hermes session.", request + "\n" + extra


# A turn that only thinks for this long (no tool call, no reply) is circling: after a compression the
# A long mod session reasoned ~10 min re-arguing one wrong theory. With loop protection on "stop"
# it is cut and restarted fresh like a Hermes loop halt.
THINK_STALL_SECONDS = 600


class RunFailed(RuntimeError):
    """A Hermes run that ended without finishing. `kind` says how the conversation can recover:
    "truncated" (the reply was cut off), "overflow" (the history no longer fits), "loop" (Hermes
    halted it for repeating failing tool calls), "limit" (it used up Hermes' iteration budget) or "" (other)."""

    def __init__(self, message: str, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind


OVERFLOW_EXITS = {"context_compression_exhausted", "context_compression_timeout", "compaction_handoff_not_actionable"}


def run_failure(data: dict[str, Any], status: str) -> RunFailed:
    """Hermes' failed/partial runs carry no `error` field, only turn_exit_reason and its
    explanation as the run output; this turns them into a readable, classified error."""
    reason = str(data.get("turn_exit_reason") or "")
    text = str(data.get("error") or data.get("output") or data.get("content") or "").strip()
    low = text.lower()
    if reason in OVERFLOW_EXITS or "grown too long" in low or "context size" in low:
        kind = "overflow"
    elif (reason == "guardrail_halt" or "keep retrying" in low or "repeated errors" in low
          or "repetition loop" in low or "repetition detected" in low):
        kind = "loop"
    elif reason.startswith(("max_iterations_reached", "budget_exhausted")) or "iteration budget exhausted" in low:
        kind = "limit"
    elif "output length limit" in low or "cut off before it finished" in low or "cut off by a network" in low:
        kind = "truncated"
    else:
        kind = ""
    detail = text or reason.replace("_", " ") or status
    return RunFailed(detail + (f" ({reason})" if reason and reason.replace("_", " ") not in detail else ""), kind)


async def drive(s: dict[str, Any], request: str) -> None:
    """One Hermes run for this session; returns when the run ends."""
    cfg = config.load()
    browser = await browser_state()
    cli_model, cli_provider, cli_reasoning = run_model(s, cfg)
    if cli_provider.startswith("cli:"):
        return await drive_cli(s, request, browser, cli_model, cli_provider[4:], cli_reasoning)
    if cfg["continuity"]:
        # One Hermes session per Agent session: Hermes keeps every earlier tool call and result
        # (and compresses them itself) instead of a summary in the brief.
        sid_h = s.setdefault("hermes_session", f"rocsy-{s['id']}" + (f"-{s['hermes_gen']}" if s.get("hermes_gen") else ""))
        if s.get("hermes_runs", 0):
            instructions, text = build_input(s, request, browser, split=True)
            body: dict[str, Any] = {"input": text, "instructions": instructions, "session_id": sid_h}
        else:
            body = {"input": build_input(s, request, browser), "session_id": sid_h}
    else:
        body = {"input": build_input(s, request, browser)}
    model, provider, reasoning = run_model(s, cfg)
    if model:
        body["model"] = model
        if provider:
            body["provider"] = provider
    if reasoning != "default":
        body["model_options"] = {"reasoning_effort": reasoning}
    if s.get("stop"):
        return                                    # stopped between runs: don't start another one
    set_state(s, "thinking", "Starting the Master Agent")
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=None)) as client:
        response = await client.post(HERMES_URL + "/v1/runs", headers=hermes_headers(), json=body)
        if response.status_code >= 300:
            raise RuntimeError(f"Hermes answered {response.status_code}: {response.text[:200]}")
        s["run_id"] = response.json()["run_id"]
        if s.get("stop"):
            # Stop came in while the run was being created, so /stop hit the previous (finished) run_id:
            # interrupt this one now, or it keeps the local model (Strata) generating (2026-10-17).
            with suppress(Exception):
                await client.post(f"{HERMES_URL}/v1/runs/{s['run_id']}/stop", headers=hermes_headers())
        if cfg["continuity"]:
            s["hermes_runs"] = s.get("hermes_runs", 0) + 1
        ev(s, "system", f"Master Agent run started · {model or 'Hermes default model'}"
           + (f" ({provider})" if provider else "") + f" · thinking {reasoning}"
           + (" · chosen for this session" if s.get("model_override") else ""))
        set_state(s, "thinking", "Thinking")
        say: list[str] = []
        reasoning: list[str] = []
        live = live_streams[s["id"]] = {"assistant": "", "reasoning": "", "rate": None}
        last_live_push = 0.0
        stamps: list[float] = []       # one per streamed delta (llama.cpp streams ~1 token each)

        def show_live(kind: str, text: str) -> None:
            nonlocal last_live_push
            # Thinking shows only its tail; a reply renders as markdown, so it needs its start (fences).
            live[kind] = text[-4000:] if kind == "reasoning" else text[:MAX_EVENT_TEXT]
            t = now()
            stamps.append(t)
            while stamps and t - stamps[0] > 3:
                del stamps[0]
            span = t - stamps[0] if len(stamps) > 1 else 0
            live["rate"] = round((len(stamps) - 1) / span, 1) if span >= 0.5 else live.get("rate")
            if now() - last_live_push >= 0.1:
                changed(s["id"])
                last_live_push = now()

        def flush_reasoning() -> str:
            text = "".join(reasoning).strip()
            reasoning.clear()
            live["reasoning"] = ""
            if text:
                ev(s, "think", text[:MAX_EVENT_TEXT])
            else:
                changed(s["id"])
            return text

        def flush(clean: str | None = None) -> None:
            # Keep one blank line between paragraphs (the page renders markdown), drop runs of them.
            text = clean or re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(say)).strip()
            say.clear()
            live["assistant"] = ""
            if text:
                ev(s, "assistant", text[:MAX_EVENT_TEXT])
            else:
                changed(s["id"])

        final = ""
        completed_text = ""
        terminal = False
        s["loop_warnings"], s["loop_cmd"] = 0, ""
        last_action = now()
        stall_guard = cfg.get("loop_guard") == "stop"
        async with client.stream("GET", f"{HERMES_URL}/v1/runs/{s['run_id']}/events", headers=hermes_headers()) as stream:
            lines = stream.aiter_lines()
            pending = asyncio.create_task(lines.__anext__())
            try:
                while not terminal:
                    arrived, _ = await asyncio.wait({pending}, timeout=30)
                    if not arrived:
                        status_response = await client.get(f"{HERMES_URL}/v1/runs/{s['run_id']}", headers=hermes_headers())
                        status_response.raise_for_status()
                        current = status_response.json()
                        status = current.get("status")
                        if status == "completed":
                            final = (current.get("output") or "").strip()
                            terminal = True
                        elif status in ("failed", "cancelled", "interrupted"):
                            if (s.get("stop") or s.get("optimizer_run")) and status in ("cancelled", "interrupted"):
                                terminal = True
                            else:
                                raise run_failure(current, status)
                        continue
                    try:
                        line = pending.result()
                    except StopAsyncIteration:
                        break
                    pending = asyncio.create_task(lines.__anext__())
                    if not line.startswith("data:"):
                        continue
                    with suppress(ValueError):
                        e = json.loads(line[5:].strip())
                        kind = e.get("event", "")
                        if kind not in ("reasoning.delta", "reasoning.available"):
                            last_action = now()           # tools, replies, compression progress: not a stall
                        if kind == "assistant.completed":
                            completed_text = e.get("content") or ""
                        elif kind == "message.delta":
                            last_action = now()
                            say.append(e.get("delta") or "")
                            show_live("assistant", "".join(say))
                        elif kind == "message.interim":
                            # Commentary beside tool calls that wasn't streamed as deltas (it was dropped before).
                            interim = (e.get("text") or "").strip()
                            if interim and not e.get("already_streamed"):
                                flush_reasoning()
                                flush()
                                ev(s, "assistant", interim[:MAX_EVENT_TEXT])
                        elif kind == "reasoning.delta":
                            reasoning.append(e.get("delta") or "")
                            show_live("reasoning", "".join(reasoning))
                            if stall_guard and now() - last_action > THINK_STALL_SECONDS:
                                flush_reasoning()
                                s["loop_cmd"] = f"(thinking for {THINK_STALL_SECONDS // 60} minutes without a tool call or reply)"
                                with suppress(Exception):
                                    await client.post(f"{HERMES_URL}/v1/runs/{s['run_id']}/stop", headers=hermes_headers())
                                raise RunFailed(f"The agent thought for over {THINK_STALL_SECONDS // 60} minutes without acting", "loop")
                        elif kind == "reasoning.available":
                            text = (e.get("text") or "").strip()
                            squash = lambda t: re.sub(r"\s+", "", t)
                            streamed = flush_reasoning() if reasoning else ""
                            if streamed and text and squash(streamed).startswith(squash(text)):
                                pass
                            elif text and say and squash(text) == squash("".join(say)):
                                flush(text)
                            elif text and say and squash("".join(say)).startswith(squash(text)):
                                pass    # a shortened preview of the reply, not a thought
                            elif text:
                                ev(s, "think", text[:MAX_EVENT_TEXT])
                        elif kind == "tool.started":
                            last_action = now()
                            flush_reasoning()
                            flush()
                            preview = re.sub(r"python3 \S*rocsy\.py \S+", "rocsy", e.get("preview") or "").strip()
                            if e.get("tool") not in ("terminal", "exec_command"):
                                ev(s, "tool", f"{e.get('tool')}  {preview[:400]}")
                        elif kind == "tool.completed" and e.get("error"):
                            if e.get("tool") not in ("terminal", "exec_command"):
                                ev(s, "warn", f"{e.get('tool')} failed after {e.get('duration', 0):.1f}s")
                        elif kind == "tool.completed" and e.get("screenshot_path"):
                            post_browser_screenshot(s, e)
                        elif kind == "terminal.started":
                            flush_reasoning()
                            flush()
                            ev(s, "terminal", "", tool_id=e.get("tool_id"), command=e.get("command") or "", status="running")
                        elif kind == "terminal.completed":
                            if "[Tool loop warning" in (e.get("output") or "") or "consecutive identical call" in (e.get("output") or ""):
                                s["loop_warnings"] = s.get("loop_warnings", 0) + 1
                                s["loop_cmd"] = (e.get("command") or "")[:300]
                                if s["loop_warnings"] == 3 and config.load()["loop_guard"] != "off":   # once per turn; never stops the model
                                    ev(s, "warn", "Possible loop: the agent keeps repeating the same tool call. "
                                                  "It is still running; Stop or message it if it doesn't recover.")
                                    await notify(s, "loop", "Master Agent may be looping", s["loop_cmd"][:180] or s["title"])
                            ev(s, "terminal", e.get("output") or "", tool_id=e.get("tool_id"),
                               command=e.get("command") or "", status="complete",
                               exit_code=e.get("exit_code"), error=e.get("error") or "")
                        elif kind == "approval.request":
                            flush_reasoning()
                            flush()
                            await command_approval(s, client, e)
                        elif kind.startswith("run."):
                            status = kind[4:]
                            if status not in ("completed", "failed", "cancelled", "interrupted"):
                                continue
                            flush_reasoning()
                            flush()
                            if e.get("pending_steer"):     # a steer that arrived as the run ended: send it next
                                queue_message(s, str(e["pending_steer"]), note="steer arrived as the turn ended")
                            if status == "completed":
                                final = (e.get("output") or "").strip()
                            elif status in ("cancelled", "interrupted") and (s.get("stop") or s.get("optimizer_run")):
                                pass                      # Stop or controller yield for benchmarking
                            elif status in ("failed", "cancelled", "interrupted"):
                                raise run_failure({**e, "output": e.get("output") or completed_text}, status)
                            terminal = True
                            break
            finally:
                pending.cancel()
        if not terminal:
            status_response = await client.get(f"{HERMES_URL}/v1/runs/{s['run_id']}", headers=hermes_headers())
            status_response.raise_for_status()
            current = status_response.json()
            status = current.get("status")
            if status == "completed":
                final = (current.get("output") or "").strip()
            elif status in ("failed", "cancelled", "interrupted"):
                if not ((s.get("stop") or s.get("optimizer_run")) and status in ("cancelled", "interrupted")):
                    raise run_failure(current, status)
            else:
                raise RuntimeError("Hermes event stream ended before the run finished")
        flush_reasoning()
        flush()
        if final:
            s["turns"].append({"role": "agent", "text": final[:6000], "t": now()})
            s["summary"] = final[:6000]
            sessions.save(s["id"])


async def drive_cli(s: dict[str, Any], request: str, browser: Any, model: str, cli: str, reasoning: str) -> None:
    """One turn through an external coding CLI on the host (Settings → Sub-agents): bunny-hostctl runs
    it in the Hermes workspace with the whole brief (the CLI keeps no conversation of its own) and
    streams its replies, thoughts and commands back as this session's events."""
    if s.get("stop"):
        return
    if not HOSTCTL_SECRET:
        raise RuntimeError("the host helper isn't configured for the Agent service (HOSTCTL_SECRET)")
    body = {"cli": cli, "model": "" if model == "default" else model, "effort": "" if reasoning == "default" else reasoning,
            "access": "agent", "cwd": HERMES_WORKSPACE, "hermes_paths": True, "prompt": build_input(s, request, browser)}
    set_state(s, "thinking", "Starting the Master Agent")
    say: list[str] = []
    commands: dict[str, str] = {}
    final = ""

    def flush() -> None:
        text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(say)).strip()
        say.clear()
        live_streams.get(s["id"], {})["assistant"] = ""
        if text:
            ev(s, "assistant", text[:MAX_EVENT_TEXT])

    live = live_streams[s["id"]] = {"assistant": "", "reasoning": "", "rate": None}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=None)) as client:
            async with client.stream("POST", HOSTCTL_URL + "/clis/run", json=body, headers={"X-Hostctl-Secret": HOSTCTL_SECRET}) as stream:
                if stream.status_code >= 300:
                    detail = (await stream.aread()).decode(errors="replace")
                    with suppress(ValueError):
                        detail = json.loads(detail).get("error") or detail
                    raise RuntimeError(f"Could not start the CLI: {str(detail)[:300]}")
                async for line in stream.aiter_lines():
                    if not line.strip():
                        continue
                    with suppress(ValueError):
                        e = json.loads(line)
                        kind = e.get("type")
                        if kind == "started":
                            s["cli_run"] = e.get("run")
                            ev(s, "system", f"Master Agent run started · {model or 'default'} ({cli} CLI) · thinking {reasoning}"
                               + (" · chosen for this session" if s.get("model_override") else ""))
                            set_state(s, "thinking", "Thinking")
                        elif kind == "text":
                            say.append(e.get("delta") or "")
                            live["assistant"] = "".join(say)[:MAX_EVENT_TEXT]
                            changed(s["id"])
                        elif kind == "thinking":
                            flush()
                            ev(s, "think", str(e.get("delta") or "")[:MAX_EVENT_TEXT])
                        elif kind == "tool":
                            flush()
                            ev(s, "tool", f"{e.get('name')}  {str(e.get('detail') or '')[:400]}")
                        elif kind == "terminal_start":
                            flush()
                            commands[str(e.get("id"))] = str(e.get("command") or "")
                            ev(s, "terminal", "", tool_id=e.get("id"), command=commands[str(e.get("id"))], status="running")
                        elif kind == "tool_result" and str(e.get("id")) in commands:
                            ev(s, "terminal", e.get("output") or "", tool_id=e.get("id"), command=commands[str(e.get("id"))],
                               status="complete", exit_code=e.get("exit_code"), error="error" if e.get("error") else "")
                        elif kind == "warn":
                            ev(s, "warn", str(e.get("message") or ""))
                        elif kind == "done":
                            final = str(e.get("text") or "").strip()
                            say[:] = [final] if final else say
                        elif kind == "error":
                            if not s.get("stop"):
                                raise RunFailed(str(e.get("message") or "The CLI failed"))
    finally:
        s.pop("cli_run", None)
    flush()
    if final:
        s["turns"].append({"role": "agent", "text": final[:6000], "t": now()})
        s["summary"] = final[:6000]
        sessions.save(s["id"])


async def conversation(sid: str, request: str) -> None:
    """Run turns until the agent is done: a run, then (if it left jobs running) wait for
    them with the GPU handed over, then resume it with the results."""
    s = sessions.items[sid]
    cfg = config.load()
    guard = cfg["gpu_guard"] and is_local(cfg, s)
    cut_offs = fresh_starts = halts = limits = 0   # recoveries since the last turn that finished cleanly
    try:
        while True:
            if s.get("stop"):
                break
            turn = held_turns.pop(sid, None) or parked_turns.pop(sid, None) or (GpuTurn(s) if guard else None)
            turn_started = now()
            recover = ""
            turn_ok = False
            try:
                restore = bool(turn and not turn.token)
                if restore:
                    await turn.acquire()
                try:
                    watcher = asyncio.create_task(watch_media(s, turn_started))
                    gauge = asyncio.create_task(prefill_watch(s))     # before the restore: it loads the model (bar + load timing)
                    try:
                        if restore:
                            await restore_llm_context(s)
                        await drive(s, with_queued(s, request))
                    finally:
                        watcher.cancel()
                        gauge.cancel()
                    turn_ok = True
                    cut_offs = fresh_starts = halts = limits = 0
                except RunFailed as failure:
                    # A cut-off reply or an overflowing history isn't the end of the work: carry on
                    # by itself (bounded, so a model that keeps failing still stops).
                    if s.get("stop") or not failure.kind:
                        raise
                    if failure.kind == "truncated" and cut_offs < 3:
                        cut_offs += 1
                        recover = "truncated"
                    elif failure.kind == "overflow" and fresh_starts < 1:
                        fresh_starts += 1
                        recover = "overflow"
                    elif failure.kind == "loop" and halts < LOOP_RESTARTS:
                        halts += 1
                        recover = "loop"
                    elif failure.kind == "limit" and (cap := config.load()["limit_restarts"]) and (cap < 0 or limits < cap):
                        limits += 1
                        recover = "limit"
                    else:
                        if failure.kind == "loop":
                            pause_goal(s, "it kept repeating the same failing steps")
                        raise
            finally:
                if turn and turn_ok and not s.get("stop") and cpu_only_wait(s) and not await gpu_wanted_elsewhere(turn):
                    held_turns[sid] = turn                # CPU-only jobs: keep the model warm for the next turn
                    ev(s, "system", "Keeping the model loaded while the CPU-only job runs (no GPU needed); "
                                    "it is unloaded at once if anything else queues for the GPU")
                elif (turn and turn_ok and not s.get("stop") and s.get("asked") and not unfinished(s)
                      and (keep := config.load()["keep_loaded_ask"]) and not await gpu_wanted_elsewhere(turn)):
                    parked_turns[sid] = turn              # its question waits for the user: the reply needs no reload
                    ev(s, "system", "Keeping the model loaded while you answer" + (f" (up to {keep} min)" if keep > 0 else "")
                                    + "; it is unloaded at once if anything else queues for the GPU")
                    asyncio.create_task(park_turn(s, turn, keep))
                elif turn:
                    await turn.release()
                start_deferred(s)                         # the GPU is free of the model now (or the job needs none)
                with suppress(Exception):
                    post_new_media(s, turn_started)
            if s.get("stop"):
                break
            if recover == "truncated":
                ev(s, "warn", f"The model's reply was cut off mid-action; continuing automatically ({cut_offs}/3)")
                request = ("Your previous reply was cut off before it finished, so its last action did NOT run. Continue "
                           "the work from where you were. Keep each tool call small: write big files in several parts "
                           "(write_file the first part, then patch/append the rest), keep shell commands short, and "
                           "think briefly before acting.")
                continue
            if recover == "loop":
                # Restart it in a fresh Hermes conversation: in the old one the looping turns stay in the
                # history and the model copies them again (seen in a long mod session).
                fresh_conversation(s)
                ev(s, "warn", f"Hermes stopped the agent for looping; restarting it in a fresh conversation with a summary "
                              f"of this session ({halts}/{LOOP_RESTARTS})")
                await notify(s, "loop", "Master Agent was looping: restarted", (s.get("loop_cmd") or s["title"])[:180])
                request = loop_nudge(s, halted=True) + ("\n\nThe latest request was:\n"
                           + next((t["text"] for t in reversed(s["turns"]) if t["role"] == "user"), request))
                continue
            if recover == "limit":
                cap = config.load()["limit_restarts"]
                ev(s, "warn", f"The agent reached Hermes' iteration limit; restarting it automatically ({limits}"
                              + (f"/{cap})" if cap > 0 else ", no limit)"))
                request = ("Your previous run reached Hermes' iteration limit (too many tool calls in one turn) and was "
                           "ended, so you are being restarted. Re-check where the work stands with short commands, then "
                           "continue from there. Don't redo finished steps.\n\nThe latest request was:\n"
                           + next((t["text"] for t in reversed(s["turns"]) if t["role"] == "user"), request))
                continue
            if recover == "overflow":
                s.pop("hermes_session", None)
                s.update(hermes_runs=0, hermes_gen=s.get("hermes_gen", 0) + 1)
                ev(s, "warn", "The conversation outgrew the model's context; continuing in a fresh Hermes conversation "
                              "with a summary of this session")
                request = ("Your previous Hermes conversation grew too long and was reset; this session's history above "
                           "is a summary of it. Re-check the current state with short commands (don't re-read big "
                           "files or dump long listings), then continue the work.\n\nThe latest request was:\n"
                           + next((t["text"] for t in reversed(s["turns"]) if t["role"] == "user"), request))
                continue
            if s.get("optimizer_run"):
                result = await watch_optimizer(s)
                if s.get("stop"):
                    break
                if s["resumes"] >= MAX_RESUMES:
                    ev(s, "warn", f"Stopped after {MAX_RESUMES} automatic turns for this message.")
                    break
                s["resumes"] += 1
                request = ("The llama.cpp optimization has finished. Its result is:\n"
                           + json.dumps(result, indent=2)[:12000]
                           + "\n\nReview the measured scores, decide whether another distinct batch is useful, "
                             "and report the result to the user. Do not repeat tested settings.")
                continue
            if s.get("queued") and not unfinished(s):      # a message (or late steer) came in during the turn
                request = "Continue the conversation."
                continue
            if s.get("asked") and not unfinished(s):
                break                                      # it asked the user something: their reply starts the next turn
            if review_pending(s) and not unfinished(s):
                s["wait_requested"] = False
                break                                      # proposals wait for the user: their decisions start the next turn
            if not unfinished(s) and not s.get("wait_requested"):
                follow = goal_next(s)
                if not follow:
                    break
                request = follow
                continue
            s["wait_requested"] = False
            await watch_jobs(s, interrupt=not guard)
            if s.get("stop"):
                break
            if s.get("queued") and unfinished(s):
                # A cloud model answers at once while the jobs keep rendering (no VRAM at stake).
                request = ("Your jobs so far:\n" + "\n".join(job_line(j) for j in s["jobs"] if j.get("msg") == s.get("msg"))
                           + "\n\nSome are still rendering; don't start duplicates of them. The original request was:\n"
                           + next((t["text"] for t in reversed(s["turns"]) if t["role"] == "user" and not t.get("queued") and not t.get("review")), ""))
                ev(s, "system", "Answering your message while the jobs keep rendering")
                continue
            if s["resumes"] >= MAX_RESUMES:
                ev(s, "warn", f"Stopped after {MAX_RESUMES} automatic turns for this message.")
                break
            s["resumes"] += 1
            request = ("Your jobs have finished. Results:\n" + "\n".join(job_line(j) for j in s["jobs"] if j.get("msg") == s.get("msg"))
                       + "\n\nCheck them (`$R jobs`), retry a failed one once if the error says how, start any further jobs the "
                         "request still needs, or end with your short summary for the user.\n\nThe original request was:\n"
                       + next((t["text"] for t in reversed(s["turns"]) if t["role"] == "user" and not t.get("review")), "")
                       + ("\n\nStill waiting for the user's review (don't re-propose or start these): "
                          + ", ".join(f"{x['id']} {x['title']}" for x in review_pending(s)) if review_pending(s) else ""))
            ev(s, "system", "All jobs finished; resuming the Master Agent with the results")
        if s.get("stop"):
            set_state(s, "stopped", "Stopped")
            ev(s, "warn", "Stopped")
        elif s.get("asked"):
            set_state(s, "done", "Waiting for your answer")
            await notify(s, "done", "The Master Agent has a question", s["asked"][:180])
        elif waiting := review_pending(s):
            set_state(s, "done", "Waiting for your review")
            await notify(s, "done", "The Master Agent wants your OK", ", ".join(f"{x['id']} {x['title']}" for x in waiting)[:180])
        else:
            set_state(s, "done", "Done")
            ev(s, "ok", "Done")
            await notify(s, "done", "Master Agent finished", s["summary"][:180] or s["title"])
    except asyncio.CancelledError:
        set_state(s, "stopped", "Stopped")
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("session %s failed", sid)
        s["error"] = str(exc)[:500]
        set_state(s, "failed", "Failed")
        ev(s, "error", f"Failed: {str(exc)[:500]}")
        await notify(s, "failed", "Master Agent failed", str(exc)[:180])
    finally:
        if held := held_turns.pop(sid, None):
            with suppress(Exception):
                await held.release()
        # A message sent while this turn was wrapping up (state already "done") started its own
        # runner: leave that one's run, stream and registration alone.
        if runners.get(sid) in (None, asyncio.current_task()):
            live_streams.pop(sid, None)
            if (c := compress_live.get(sid)) and c["phase"] not in ("done", "skipped"):
                compress_clear(sid)                   # the run ended mid-compression (failed or stopped)
            s["pending"] = None
            s["run_id"] = None
            runners.pop(sid, None)
            # Messages queued after the loop's last look (it broke for a question, a review, the
            # resume cap or a failure) would sit "queued" forever: read them now. After a Stop they
            # wait and go out with the next message, unless one was sent after the Stop (while the
            # run was still winding down): that one is meant to be read, so continue with it.
            after_stop = any(q["t"] > (s.get("stop_t") or float("inf")) for q in s.get("queued") or [])
            if s.get("queued") and (not s.get("stop") or after_stop) and sid in sessions.items:
                s.update(stop=False, resumes=0, asked="", wait_requested=False, error="")
                set_state(s, "starting", "Reading your queued message")
                ev(s, "system", "Reading the message you sent while it was busy")
                runners[sid] = asyncio.create_task(conversation(sid, "Continue the conversation."))
        changed(sid)
        sessions.save(sid)


def queue_message(s: dict[str, Any], text: str, note: str = "", attachments: list[dict[str, Any]] | None = None,
                  event: int | None = None) -> None:
    """A user message for the next turn (the agent is busy rendering, waiting for the GPU, or a
    steer missed the end of a run). `event` = its "queued" bubble, marked sent on delivery."""
    s.setdefault("queued", []).append({"text": text, "note": note, "attachments": [a["ref"] for a in attachments or []],
                                       "t": now(), "event": event})
    sessions.save(s["id"])


def mark_sent(s: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """The queued bubbles of delivered messages lose their "queued" tag (stored copy + live pages)."""
    ids = {item.get("event") for item in items} - {None}
    for e in s["events"]:
        if e["i"] in ids and e.get("queued"):
            e["queued"] = False
            e["sent"] = now()
    if ids:
        ev(s, "sent", "", refs=sorted(ids))


def with_queued(s: dict[str, Any], request: str) -> str:
    """The turn's prompt plus any messages the user sent since the last turn started."""
    queued, s["queued"] = s.get("queued") or [], []
    if not queued:
        return request
    for item in queued:
        s["turns"].append({"role": "user", "text": item["text"], "t": item["t"], "queued": True})
    mark_sent(s, queued)
    lines = "\n".join(f"- {item['text']}" + (f" (attached: {', '.join(item['attachments'])})" if item.get("attachments") else "")
                      + (f" [{item['note']}]" if item.get("note") else "") for item in queued)
    return (f"NEW MESSAGE{'S' if len(queued) > 1 else ''} FROM THE USER (newest instructions; they override older ones):\n"
            f"{lines}\n\n{request}")


def goal_next(s: dict[str, Any]) -> str | None:
    """After a finished turn: the next goal turn's prompt, or None (no goal / achieved / out of turns)."""
    g = s.get("goal")
    if not g or g["status"] != "active" or s.get("stop"):
        return None
    g["turns"] += 1
    if g.get("until"):                        # a scheduled run "for 2 hours": the clock ends it, not the agent
        left = g["until"] - now()
        if left <= 60:
            g["status"] = "done"
            ev(s, "ok", "The scheduled time is up")
            return None
        g["achieved"] = False
        s["resumes"] = 0
        ev(s, "system", f"Scheduled run: about {round(left / 60)} min left; continuing")
        return (f"SCHEDULED RUN, about {round(left / 60)} minutes left (it ends at {g.get('until_text', 'the set time')}; "
                "you are stopped then). Keep working on it: check `$R jobs`, then start the next batch. Don't finish early "
                "and don't repeat what you already made; vary it.\n\nTask: " + g["text"])
    if g.get("achieved"):
        g["status"] = "done"
        ev(s, "ok", f"Goal achieved: {g['text'][:200]}" + (f" — {g['evidence'][:300]}" if g.get("evidence") else ""))
        return None
    if g["max_turns"] and g["turns"] >= g["max_turns"]:
        g["status"] = "paused"
        ev(s, "warn", f"Goal paused after {g['turns']} turns (its budget). /goal resume to keep going, "
                      "/goal budget N to allow more turns, /goal clear to drop it.")
        return None
    guard = config.load()["loop_guard"]
    if s.get("loop_warnings", 0) >= 3 and guard != "off":
        # warn: event + notification during the turn and a change-approach nudge; stop also pauses the goal.
        g["stuck"] = g.get("stuck", 0) + 1
        if guard == "stop" and g["stuck"] >= 2 + LOOP_RESTARTS:
            pause_goal(s, f"it was still stuck repeating failing steps after {LOOP_RESTARTS} fresh restarts")
            return None
        if guard == "stop" and g["stuck"] >= 2:
            fresh_conversation(s)
            ev(s, "warn", "The agent kept repeating failing steps; restarting it in a fresh conversation with a summary of this session")
    else:
        g["stuck"] = 0
    s["resumes"] = 0
    ev(s, "system", f"Goal not reached yet; continuing (turn {g['turns'] + 1}" + (f" of {g['max_turns']})" if g["max_turns"] else ", no turn limit)"))
    part = current_part(g)
    if not part:
        return ((loop_nudge(s) + "\n\n" if g["stuck"] else "")
                + "Continue with the standing goal. You have not split it into milestones yet: do that first with "
                "`$R goal parts \"milestone 1 || milestone 2 || ...\"`, then work on milestone 1.\n\nGoal: " + g["text"])
    return ((loop_nudge(s) + "\n\n" if g["stuck"] else "")
            + f"Continue with the current milestone ({g['part'] + 1} of {len(g['parts'])}): {part['text']}\n"
            "Check what is done (`$R jobs`), do the next step, and run `$R goal done \"evidence\"` once this milestone is proven.")


LOOP_RESTARTS = 3   # fresh-conversation restarts for a loop before the goal pauses


def fresh_conversation(s: dict[str, Any]) -> None:
    """The next turn starts a new Hermes conversation with a summary of this session (like /fresh)."""
    s.pop("hermes_session", None)
    s.update(hermes_runs=0, hermes_gen=s.get("hermes_gen", 0) + 1, loop_warnings=0)


def pause_goal(s: dict[str, Any], why: str) -> None:
    g = s.get("goal")
    if g and g.get("status") == "active":
        g["status"] = "paused"
        ev(s, "warn", f"Goal paused: {why}. Tell the agent what to try instead, then /goal resume.")


def loop_nudge(s: dict[str, Any], halted: bool = False) -> str:
    """Prompt for a turn after the agent got stuck repeating failing tool calls."""
    cmd = s.get("loop_cmd") or ""
    return (("Hermes stopped your last turn because you repeated failing tool calls. " if halted else
             "In your last turn you got stuck repeating failing tool calls. ")
            + (f"The repeated command was:\n{cmd}\n" if cmd else "")
            + "Do NOT run it again. Read its error, write down in one or two lines what you have already tried and "
              "why it failed, then take a genuinely different approach (another tool, another source of information, "
              "or a smaller step). If you're blocked on something only the user can provide, use `$R ask` instead of "
              "retrying.")



async def watch_jobs(s: dict[str, Any], interrupt: bool = False) -> None:
    """Wait for this session's jobs. `interrupt` (a cloud model, no VRAM at stake): a new user
    message ends the wait at once so the agent can answer while the jobs keep rendering."""
    left = unfinished(s)
    set_state(s, "rendering", f"Rendering {len(left)} job(s); the Master Agent resumes when they finish")
    ev(s, "system", f"Waiting for {len(left)} job(s). The GPU is theirs; the Master Agent resumes when they finish.")
    tick = 0
    while unfinished(s) and not s.get("stop"):
        if s.get("queued") and (interrupt or s.get("cancel_for_message")):
            if not interrupt:      # the user stopped the jobs to talk: wait until they are really gone
                await refresh_jobs(s)
                if unfinished(s):
                    await asyncio.sleep(2)
                    continue
            break
        await refresh_jobs(s)
        tick += 1
        if tick % 3 == 0 and not interrupt:
            await vram_watchdog(s)
        held = held_turns.get(s["id"])
        if held and (not cpu_only_wait(s) or await gpu_wanted_elsewhere(held)):
            held_turns.pop(s["id"], None)
            ev(s, "system", "Something else needs the GPU: unloading the model until the job finishes")
            await held.release()
        await asyncio.sleep(4)
    s["cancel_for_message"] = False


async def watch_optimizer(s: dict[str, Any]) -> dict[str, Any]:
    """Wait without a Hermes turn or GPU reservation while llama-bench runs."""
    run_id = s["optimizer_run"]
    set_state(s, "rendering", "Benchmarking the model; live results appear below")
    ev(s, "system", "The Master Agent has unloaded its model and will resume after benchmarking.")
    failures = 0
    while not s.get("stop"):
        try:
            result = await hostctl("GET", f"/llama-optimize/status/{run_id}", timeout=12)
            failures = 0
        except RuntimeError as exc:
            failures += 1
            if failures == 15:
                ev(s, "warn", f"Waiting for the host helper to return before checking benchmark status: {exc}")
            await asyncio.sleep(4)
            continue
        if result.get("state") in ("complete", "failed", "reverted"):
            break
        await asyncio.sleep(4)
    else:
        return {"id": run_id, "state": "stopped"}
    s["optimizer_run"] = None
    s["wait_requested"] = False
    sessions.save(s["id"])
    return result


async def yield_for_optimizer(s: dict[str, Any], hermes_run: str, optimizer_run: str) -> None:
    """End a lingering thinking turn so the benchmark can get the GPU."""
    await asyncio.sleep(5)
    if s.get("optimizer_run") != optimizer_run or s.get("run_id") != hermes_run or s.get("state") != "thinking":
        return
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            await client.post(f"{HERMES_URL}/v1/runs/{hermes_run}/stop", headers=hermes_headers())
    except httpx.HTTPError:
        ev(s, "warn", "Could not end the thinking turn automatically; end it to release the GPU for benchmarking.")


async def vram_watchdog(s: dict[str, Any]) -> None:
    """While one of this session's plain renders holds the GPU, a loaded chat model can only be a
    straggling Hermes call from the agent's last turn: unload it before the render runs out of VRAM.
    Agentic jobs are left alone (their own planner uses the model under its own GPU turn)."""
    mine = {(j["service"], j["job_id"]) for j in unfinished(s) if j.get("kind") != "agentic"}
    if not mine or any(j.get("kind") == "agentic" for j in unfinished(s)):
        return
    try:
        holder = next((e for e in await studios.queue() if e.get("state") == "active"), None)
    except studios.StudioError:
        return
    if not holder or (holder.get("app"), str(holder.get("job_id") or "")) not in mine:
        return
    loaded = await llama_loaded()
    if loaded:
        ev(s, "warn", f"The chat model ({', '.join(loaded)}) was loaded while {holder.get('app')} rendered your job; unloading it")
        await unload_llm()


async def refresh_jobs(s: dict[str, Any]) -> None:
    for j in unfinished(s):
        if not j.get("job_id") or j.get("kind") == "motion":   # deferred / run by this service (run_postedit, run_motion)
            continue
        if j.get("kind") == "agentic":
            await refresh_agentic(s, j)
            continue
        try:
            item = await studios.job(j["service"], j["job_id"])
        except studios.StudioError as exc:
            if str(exc).startswith("404"):
                j.update(state="failed", error="the studio no longer has this job")
                ev(s, "warn", f"{j['ref']} disappeared from {j['service']}")
            continue
        state = str(item.get("state") or j["state"])
        j["progress"] = studios.progress(item)
        j["stage"] = str(item.get("stage") or "")[:120]
        if state != j["state"]:
            j["state"] = state
            if state in ("complete", "completed"):
                j["outputs"] = studios.outputs(j["service"], item)
                if item.get("duration"):
                    j["meta"] = {**(j.get("meta") or {}), "duration": item["duration"]}
                ev(s, "job", f"{j['ref']} finished", job=j["ref"])
            elif state in studios.DONE:
                j["error"] = str(item.get("error") or state)[:500]
                ev(s, "warn", f"{j['ref']} {state}: {j['error']}", job=j["ref"])
        sessions.save(s["id"])


async def refresh_agentic(s: dict[str, Any], j: dict[str, Any]) -> None:
    try:
        item = await studios.agentic_job(j["session_id"], j["job_id"])
    except studios.StudioError as exc:
        if str(exc).startswith("404"):
            j.update(state="failed", error="Vid no longer has this Agentic session")
            ev(s, "warn", f"{j['ref']} disappeared from Vid", job=j["ref"])
            sessions.save(s["id"])
        return
    state = item["state"]
    j["progress"], j["stage"] = item["progress"], item["stage"]
    # Mirror Vid's Agentic log in the existing Master Agent terminal card. The
    # revision changes for streaming updates; length/last entry catches normal
    # log lines that are appended without a revision bump.
    agentic_log = item.get("log") or []
    last_log = agentic_log[-1] if agentic_log else {}
    log_marker = (state, item.get("error") or "", item.get("log_revision"), len(agentic_log),
                  str(last_log.get("text") or "")[-240:])
    if log_marker != j.get("agentic_log_marker") or not j.get("agentic_terminal_sent"):
        j["agentic_log_marker"] = log_marker
        j["agentic_terminal_sent"] = True
        lines = []
        for entry in agentic_log:
            text = str(entry.get("text") or "").rstrip()
            if text:
                kind = str(entry.get("kind") or "log").upper()
                lines.append(f"[{kind}] {text}")
        terminal_text = "\n".join(lines)[-24000:] or j.get("stage") or "Starting Video Agentic…"
        terminal_status = ("failed" if state in studios.DONE and state not in ("complete", "completed")
                           else "complete" if state in ("complete", "completed") else "running")
        ev(s, "terminal", terminal_text,
           tool_id=f"h3-agentic:{j['job_id']}",
           command=f"Video Agentic · {j.get('label') or j['ref']}",
           phase=state,
           status=terminal_status,
           error=str(item.get("error") or "") if terminal_status == "failed" else "")
    outs = studios.agentic_outputs(item)
    if [o["url"] for o in outs] != [o["url"] for o in j.get("outputs") or []]:
        j["outputs"] = outs
    iterations = studios.agentic_iterations(item)
    if [o.get("url") for o in iterations] != [o.get("url") for o in j.get("iterations") or []]:
        j["iterations"] = iterations
    j["full_output"] = bool(item.get("full_output"))
    if state == "awaiting_approval" and not j.get("asked"):
        j["asked"] = True       # the user's call, never the agent's: approve or exclude the previews in Vid
        ev(s, "approval", f"{j['ref']}: the staging previews are waiting for your approval in Vid", job=j["ref"])
        await notify(s, "approval", "Agentic previews need your approval", j["label"][:160])
    elif state != "awaiting_approval":
        j["asked"] = False
        j.pop("approval_submitted", None)
    if state != j["state"]:
        j["state"] = state
        if state in ("complete", "completed"):
            ev(s, "job", f"{j['ref']} finished ({len(outs)} clip{'s' if len(outs) != 1 else ''})", job=j["ref"])
        elif state in studios.DONE:
            j["error"] = str(item.get("error") or state)[:500]
            ev(s, "warn", f"{j['ref']} {state}: {j['error']}", job=j["ref"])
    sessions.save(s["id"])


async def backfill_agentic_clips() -> None:
    """Finished Agentic cards saved while their iterations held only the staging previews (no
    "phase"): fetch them once more so they show the final clips and full-render retries too."""
    for s in list(sessions.items.values()):
        changed = False
        for j in s.get("jobs", []):
            if (j.get("kind") != "agentic" or j.get("state") not in studios.DONE or not j.get("job_id")
                    or all("phase" in item for item in j.get("iterations") or [{"phase": ""}])):
                continue
            try:
                item = await studios.agentic_job(j["session_id"], j["job_id"])
            except studios.StudioError:
                continue
            outs, iterations = studios.agentic_outputs(item), studios.agentic_iterations(item)
            if not iterations:          # Vid already dropped these renders: keep what the card had
                continue
            j["iterations"] = iterations
            j["outputs"] = outs or j.get("outputs") or []
            changed = True
        if changed:
            sessions.save(s["id"])


async def cancel_job_of(j: dict[str, Any]) -> None:
    if not j.get("job_id"):
        task = deferred_tasks.pop(f"{j.get('sid')}:{j['ref']}", None)
        if task:
            task.cancel()
        j.update(state="cancelled", error="cancelled before it started")
        return
    if j.get("kind") == "agentic":
        await studios.agentic_cancel(j["job_id"])
    else:
        await studios.cancel(j["service"], j["job_id"])


async def notify(s: dict[str, Any], event: str, title: str, body: str) -> None:
    if not config.load()["notify"] or not QUEUE_SECRET:
        return
    with suppress(Exception):
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(NOTIFY_URL, headers={"x-internal-secret": QUEUE_SECRET},
                              json={"service": "Agent", "event": event, "title": title, "body": body,
                                    "url": f"/Agent/?s={s['id']}", "tag": f"agent-{s['id']}"})


# ---------------- deferred jobs (Whisper captions) ----------------
# Post-Edit renders (and the Whisper for captions) use the GPU: never while a local model is
# loaded, so they start after the turn has handed the GPU back, like every other job renders.

deferred_tasks: dict[str, asyncio.Task] = {}


def start_deferred(s: dict[str, Any]) -> None:
    for j in s["jobs"]:
        if j.get("deferred") and not j.get("launched") and j["state"] not in studios.DONE:
            j["launched"] = True
            runner = {"motion": run_motion, "gametest": run_gametest, "reatest": run_reatest}.get(j.get("kind"), run_postedit)
            deferred_tasks[f"{s['id']}:{j['ref']}"] = asyncio.create_task(runner(s, j))


async def run_postedit(s: dict[str, Any], j: dict[str, Any]) -> None:
    body = j["deferred"]
    source = body["source_job_id"]
    try:
        if not (body.get("captions") and body.get("caption_source") == "whisper"):
            item = await studios.postedit(body)
            j.update(job_id=str(item.get("id") or ""), state=str(item.get("state") or "rendering"), stage="Post-Edit rendering")
            ev(s, "job", f"{j['ref']}: Post-Edit is rendering", job=j["ref"])
            return
        j.update(state="transcribing", stage="Splitting the vocals and running Whisper on the GPU (queued behind renders)")
        sessions.save(s["id"])
        status = await studios.postedit_transcribe(source)
        for _ in range(900):                        # 30 min, like Vid's own limit
            if status.get("state") in ("ready", "failed"):
                break
            await asyncio.sleep(2)
            status = await studios.postedit_transcript(source)
        if status.get("state") != "ready":
            raise studios.StudioError(status.get("error") or "Whisper didn't finish in 30 minutes")
        words = len(status.get("words") or [])
        if not words:
            raise studios.StudioError("Whisper heard no sung or spoken words, even with the vocals split from the music "
                                      "(an instrumental?). Use --text overlays or TTS --voiceover for words instead")
        j.update(stage=f"Whisper heard {words} words ({status.get('language') or '?'}); burning in the captions")
        sessions.save(s["id"])
        item = await studios.postedit(body)
        j.update(job_id=str(item.get("id") or ""), state=str(item.get("state") or "rendering"),
                 meta={"words": words, "language": status.get("language"), "transcript": [c[2] for c in status.get("cues") or []][:200]})
        ev(s, "job", f"{j['ref']}: Whisper heard {words} words; Post-Edit is burning in the captions", job=j["ref"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        j.update(state="failed", error=str(exc)[:500])
        ev(s, "warn", f"{j['ref']} failed: {str(exc)[:300]}", job=j["ref"])
    finally:
        deferred_tasks.pop(f"{s['id']}:{j['ref']}", None)
        sessions.save(s["id"])


def hermes_path(host_path: str) -> str:
    """A path hostctl reports under the host's rocsy-workspace, as Hermes (and share_file) see it."""
    tail = str(host_path).split("/rocsy-workspace/", 1)
    return f"{HERMES_WORKSPACE}/{tail[1]}" if len(tail) == 2 else str(host_path)


async def run_gametest(s: dict[str, Any], j: dict[str, Any]) -> None:
    """A REA mod play test on the real game (hostctl starts it through Steam on the desktop). The game
    shares the GPU with the desktop and the local LLM: it runs in the GPU slot with the model unloaded."""
    body = j["deferred"]
    slot = GpuSlot(s, f"{s['id']}-{j['ref']}-game", f"Master Agent play test {j['ref']}: {j['label'][:50]}", j["ref"])
    try:
        j.update(state="waiting_for_gpu", stage="Waiting for the GPU")
        sessions.save(s["id"])
        await slot.acquire(lambda: j.update(stage="Waiting for the GPU (front of the queue)"))
        j.update(state="rendering", stage=f"Comparing clean and modded game runs ({body['seconds']} s each when available)")
        sessions.save(s["id"])
        r = await hostctl("POST", f"/rea/mods/{body['mod']}/play", {k: body.get(k) for k in ("seconds", "screenshots", "uninstall", "install", "user_files")},
                          timeout=2 * body["seconds"] + 480)
        if r.get("error"):
            raise studios.StudioError(r["error"])
        pictures = []
        for shot in r.get("screenshots") or []:
            with suppress(ValueError, OSError):
                pictures.append(share_file(s, hermes_path(shot)))
        errs = r.get("errors") or []
        added = r.get("new_errors") or []
        shared = r.get("baseline_errors") or []
        baseline = r.get("baseline") or {}
        if not r.get("started"):
            summary = r.get("note") or "the game didn't start"
        else:
            if r.get("comparison_status") == "compared":
                comparison = (f"; {len(added)} error type(s) only in the modded run, {len(shared)} also in the clean game"
                              + (": " + " | ".join(added[:8]) if added else ""))
            elif r.get("comparison_status") == "inconclusive":
                comparison = f"; comparison inconclusive: {r.get('comparison_note', 'the runs did not reach comparable game states')}"
            else:
                comparison = (f"; {len(errs)} game error line(s) unclassified because the clean baseline is unavailable"
                              + (f" ({baseline.get('reason')})" if baseline.get("reason") else ""))
            summary = (f"ran {r.get('ran_seconds')} s" + (" then the game EXITED BY ITSELF" if r.get("exited_early") else ", then was closed")
                       + comparison
                       + f"; logs: {', '.join(hermes_path(x['copy']) for x in r.get('logs') or []) or 'none found'}"
                       + (f"; user:// files: {'consumed by the game' if (r.get('user_files') or {}).get('consumed') else 'NOT consumed (the mod never read them)'}" if r.get("user_files") else "")
                       + (f"; screenshots: {', '.join(a['ref'] for a in pictures)} (look at them with your vision tool ONE AT A TIME, "
                          "never in parallel, with a short specific question; if a call errors or times out, retry it once, then "
                          "carry on with the log evidence and say which screenshot you couldn't check: "
                          + ", ".join(a['hermes_path'] for a in pictures) + ")" if pictures else ""))
            diag = r.get("diagnosis") or {}
            if diag.get("text"):
                summary += (f"\nDIAGNOSIS (facts from the log; full report {hermes_path(diag['file'])}):\n{diag['text']}"
                            if diag.get("file") else f"\nDIAGNOSIS: {diag['text']}")
        j.update(state="complete" if r.get("started") else "failed", progress=100, stage="", summary=summary,
                 error="" if r.get("started") else summary, meta={"playtest": r.get("folder"), "installed": r.get("installed")})
        ev(s, "job", f"{j['ref']} play test: {summary[:600]}", job=j["ref"], attachments=public_attachments(pictures) if pictures else None)
    except asyncio.CancelledError:
        j.update(state="cancelled", error="cancelled")
        raise
    except Exception as exc:  # noqa: BLE001
        j.update(state="failed", error=str(exc)[:800])
        ev(s, "warn", f"{j['ref']} play test failed: {str(exc)[:400]}", job=j["ref"])
    finally:
        await slot.release()
        deferred_tasks.pop(f"{s['id']}:{j['ref']}", None)
        sessions.save(s["id"])


async def run_reatest(s: dict[str, Any], j: dict[str, Any]) -> None:
    """A REA test run (build, syntax check, the game on REA's virtual display, no GPU) with three
    screenshots like a play test: 15 s in, half way, just before the end. They're shown in chat."""
    body = j["deferred"]
    mod = body["mod"]
    try:
        j.update(state="rendering", stage=f"REA test run: build, syntax check, {body['timeout']} s game run with screenshots")
        sessions.save(s["id"])
        async with httpx.AsyncClient(timeout=60) as client:
            r = (await client.post(f"{REA_API}/mods/{mod}/testrun", headers={"X-REA": "1"},
                                   json={"by": "agent", "timeout": body["timeout"], "screenshots": True, "why": body.get("why", "")}))
            run = r.json()
            if r.status_code >= 400:
                raise studios.StudioError(run.get("detail") or f"REA answered HTTP {r.status_code}")
            # Baseline (first time per game version) + build + the modded run; fail closed after an hour.
            for _ in range(1200):
                if run.get("state") != "running":
                    break
                await asyncio.sleep(3)
                with suppress(httpx.HTTPError, ValueError):
                    run = (await client.get(f"{REA_API}/mods/{mod}/runs/{run['id']}")).json()
                    j.update(stage=f"REA test run {run.get('id')}: {run.get('phase') or 'running'}")
            else:
                raise studios.StudioError(f"test run {run.get('id')} still running after an hour")
        folder = f"{HERMES_WORKSPACE}/rea-mods/{mod}/runs/{run['id']}"
        pictures = []
        for shot in [x for x in run.get("shots") or [] if not x.get("base")][:3]:
            with suppress(ValueError, OSError):
                pictures.append(share_file(s, f"{folder}/shots/{shot['file']}", f"test-{run['id']}-{shot['file']}"))
        labels = [x["label"] for x in run.get("shots") or [] if not x.get("base")][:3]
        summary = (f"test run {run['id']} {run.get('state')}: {run.get('summary') or ''}"
                   + (f"; errors: {' | '.join(str(e) for e in (run.get('errors') or [])[:8])}" if run.get("errors") else "")
                   + f"; READ {folder}/digest.md FIRST (verdict, [PROOF] lines, the mod's own log lines, new errors with stack and IL, "
                     "screenshots: small enough to read whole); full logs there too (game.log, player.log, run.log, build.log: "
                     "grep them, never read_file them whole)"
                   + (f"; screenshots ({', '.join(labels)}): {', '.join(a['ref'] for a in pictures)} (look at them with your vision "
                      "tool ONE AT A TIME, never in parallel, with a short specific question; if a call errors or times out, retry it "
                      "once, then carry on with the log evidence and say which screenshot you couldn't check: "
                      + ", ".join(a['hermes_path'] for a in pictures) + ")" if pictures else
                      "; no screenshots (this engine's test run has no display run, or the game showed nothing)"))
        j.update(state="complete", progress=100, stage="", summary=summary, meta={"testrun": run.get("id"), "state": run.get("state")})
        ev(s, "job", f"{j['ref']} {summary[:600]}", job=j["ref"], attachments=public_attachments(pictures) if pictures else None)
    except asyncio.CancelledError:
        j.update(state="cancelled", error="cancelled")
        raise
    except Exception as exc:  # noqa: BLE001
        j.update(state="failed", error=str(exc)[:800])
        ev(s, "warn", f"{j['ref']} test run failed: {str(exc)[:400]}", job=j["ref"])
    finally:
        deferred_tasks.pop(f"{s['id']}:{j['ref']}", None)
        sessions.save(s["id"])


async def run_motion(s: dict[str, Any], j: dict[str, Any]) -> None:
    """A Motion (Remotion) render in the GPU slot, then imported into Vid as a finished video job."""
    body = j["deferred"]
    slot = GpuSlot(s, f"{s['id']}-{j['ref']}-motion", f"Master Agent Motion {j['ref']}: {j['label'][:50]}", j["ref"])
    motion_id = ""
    try:
        j.update(state="waiting_for_gpu", stage="Waiting for the GPU")
        sessions.save(s["id"])
        await slot.acquire(lambda: j.update(stage="Waiting for the GPU (front of the queue)"))
        item = await studios.motion("/api/jobs", body, timeout=60)
        motion_id = item["id"]
        j.update(motion_id=motion_id, state="rendering", stage="Starting")
        while True:
            await asyncio.sleep(2)
            item = await studios.motion(f"/api/jobs/{motion_id}", method="GET", timeout=30)
            if item.get("progress") != j.get("progress") or item.get("stage") != j.get("stage"):
                j.update(progress=item.get("progress"), stage=str(item.get("stage") or "")[:120])
                sessions.save(s["id"])
            if item["state"] in ("complete", "failed", "cancelled"):
                break
        if item["state"] != "complete":
            raise studios.StudioError(item.get("error") or item["state"])
        await slot.release()                                   # importing needs no GPU slot
        j.update(stage="Adding it to Vid", progress=99)
        folder = DATA / "tmp"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"motion-{motion_id}.mp4"
        try:
            await studios.motion_download(motion_id, str(path))
            vid = await studios.vid_import(str(path), j["label"])
        finally:
            path.unlink(missing_ok=True)
        j.update(state="complete", job_id=str(vid.get("id") or ""), progress=100, stage="", outputs=studios.outputs("Vid", vid),
                 meta={"duration": item.get("duration"), "width": item.get("width"), "height": item.get("height"), "fps": item.get("fps")})
        ev(s, "job", f"{j['ref']} finished ({item.get('width')}x{item.get('height')}, {item.get('duration')} s)", job=j["ref"])
    except asyncio.CancelledError:
        if motion_id:
            with suppress(Exception):
                await studios.motion(f"/api/jobs/{motion_id}/cancel", {}, timeout=20)
        j.update(state="cancelled", error="cancelled")
        raise
    except Exception as exc:  # noqa: BLE001
        j.update(state="failed", error=str(exc)[:800])
        ev(s, "warn", f"{j['ref']} failed: {str(exc)[:400]}", job=j["ref"])
    finally:
        await slot.release()
        deferred_tasks.pop(f"{s['id']}:{j['ref']}", None)
        sessions.save(s["id"])


def start(s: dict[str, Any], text: str, shown: str | None = None, attachments: list[dict[str, Any]] | None = None) -> None:
    if attachments:
        text += "\n(The user attached: " + ", ".join(f"{a['ref']} {a['kind']} \"{a['name']}\"" for a in attachments) + ")"
    if lapsed := review_pending(s):
        for x in lapsed:
            x.update(status="dropped", reported=True)
        shown = shown if shown is not None else text.split("\n(The user attached: ")[0]
        text += (f"\n(Your proposals {', '.join(x['id'] for x in lapsed)} were still waiting for review; the user sent this "
                 "instead, so they are NOT approved. Propose again only what this message still needs.)")
    s["turns"].append({"role": "user", "text": text, "t": now()})
    s.update(stop=False, resumes=0, jobs_this_message=0, error="", wait_requested=False, optimizer_run=None,
             asked="", msg=s.get("msg", 0) + 1, denied=[])
    if shown is not False:
        ev(s, "user", shown or text.split("\n(The user attached: ")[0], **({"attachments": public_attachments(attachments)} if attachments else {}))
    runners[s["id"]] = asyncio.create_task(conversation(s["id"], text))


# ---------------- slash commands ----------------

COMMANDS = [
    {"cmd": "/goal", "args": "<objective>", "help": "Set a standing goal; the Master Agent keeps working on it turn after turn until it's achieved"},
    {"cmd": "/goal status", "args": "", "help": "Show the goal and how many turns it has used"},
    {"cmd": "/goal pause", "args": "", "help": "Pause the goal (the current turn finishes)"},
    {"cmd": "/goal resume", "args": "", "help": "Resume a paused goal"},
    {"cmd": "/goal budget", "args": "<turns>", "help": "Turns the goal may take before it pauses (infinite = no limit)"},
    {"cmd": "/goal infinite", "args": "<objective>", "help": "Set a goal with no turn limit: it keeps working until achieved, /goal pause or /stop"},
    {"cmd": "/goal reopen", "args": "[N]", "help": "Send a milestone closed on wrong evidence back to todo (default: the last one closed)"},
    {"cmd": "/goal clear", "args": "", "help": "Drop the goal"},
    {"cmd": "/browser connect", "args": "[url]", "help": "Give the agent's browser tools a live Chromium window (opens one on the desktop if needed)"},
    {"cmd": "/browser status", "args": "", "help": "Is a live browser connected?"},
    {"cmd": "/browser disconnect", "args": "", "help": "Back to Hermes' default headless browser"},
    {"cmd": "/permissions", "args": "", "help": "What the Master Agent may do (change it in Settings → Agentic)"},
    {"cmd": "/review", "args": "on|off|<what>", "help": "Check before it uses a service: its plan and each prompt wait here for your OK or veto (e.g. /review music image)"},
    {"cmd": "/askfirst", "args": "on|off|status|forget", "help": "Ask before it first uses each service; once you allow one it is cleared (forget = drop the clearances)"},
    {"cmd": "/approvals", "args": "ask|allow|deny", "help": "When Hermes flags a risky command: ask you here, allow it, or deny it"},
    {"cmd": "/retry", "args": "", "help": "Send your last message again"},
    {"cmd": "/fresh", "args": "", "help": "Start a new Hermes conversation for this session (keeps the page, jobs and attachments)"},
    {"cmd": "/schedule", "args": "<time> [daily|weekdays|weekends|tomorrow|YYYY-MM-DD] [for 2h] <request>",
     "help": "Send the Master Agent a request at a set time, e.g. /schedule 3am for 2h make images of foxes"},
    {"cmd": "/schedule list", "args": "", "help": "Your schedules (also in Settings → Agentic)"},
    {"cmd": "/schedule cancel", "args": "<N>", "help": "Delete schedule N from /schedule list"},
    {"cmd": "/rename", "args": "<name>", "help": "Rename this session (/rename auto lets the agent name it)"},
    {"cmd": "/jobs", "args": "", "help": "This session's jobs"},
    {"cmd": "/queue", "args": "", "help": "The shared GPU queue"},
    {"cmd": "/status", "args": "", "help": "What every studio is doing"},
    {"cmd": "/model", "args": "", "help": "Which model the Master Agent thinks with"},
    {"cmd": "/stop", "args": "", "help": "Stop this session"},
    {"cmd": "/new", "args": "[request]", "help": "Start a new session"},
    {"cmd": "/think", "args": "", "help": "Show or hide thinking and tool calls"},
    {"cmd": "/help", "args": "", "help": "List these commands"},
]

browser_cache: dict[str, Any] = {}


async def hostctl(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 15) -> dict[str, Any]:
    if not HOSTCTL_SECRET:
        raise RuntimeError("the host helper isn't configured for the Agent service (HOSTCTL_SECRET)")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, HOSTCTL_URL + path, json=body, headers={"X-Hostctl-Secret": HOSTCTL_SECRET})
    except httpx.HTTPError as exc:
        raise RuntimeError("the host helper (bunny-hostctl) isn't reachable") from exc
    data = response.json() if response.content else {}
    if response.status_code >= 400:
        raise RuntimeError(data.get("error") or f"host helper error {response.status_code}")
    return data


async def browser_state(fresh: bool = False) -> dict[str, Any] | None:
    if not fresh and browser_cache.get("t", 0) > now() - 30:
        return browser_cache["data"]
    try:
        data = await hostctl("GET", "/browser")
    except RuntimeError:
        data = None
    browser_cache.update(t=now(), data=data)
    return data


def browser_line(state: dict[str, Any]) -> str:
    if state.get("connected"):
        return f"🌐 Live browser connected: {state.get('browser') or 'Chromium'} at {state['live']}. Hermes' browser tools drive it."
    if state.get("configured"):
        return f"🌐 Hermes points at {state['configured']}, but nothing answers there. /browser connect to open one."
    return "🌐 No live browser connected; the agent uses Hermes' default headless browser. /browser connect to give it a visible one."


UNLIMITED = ("infinite", "unlimited", "forever", "0", "∞")


def goal_line(g: dict[str, Any] | None) -> str:
    if not g:
        return "No goal set. /goal <objective> sets one."
    icon = {"active": "⊙", "paused": "⏸", "done": "✓"}.get(g["status"], "·")
    turns = f"{g['turns']} of {g['max_turns']} turns" if g["max_turns"] else f"{g['turns']} turns, no limit"
    out = f"{icon} Goal ({g['status']}, {turns}): {g['text']}"
    if g.get("parts"):
        out += f"\nMilestones ({sum(p['status'] == 'done' for p in g['parts'])} of {len(g['parts'])} done):\n{parts_overview(g)}"
    if g.get("evidence"):
        out += f"\nEvidence: {g['evidence']}"
    return out


async def run_command(s: dict[str, Any], text: str) -> str | None:
    """Apply a slash command to a session. Logs its output; returns a prompt when the
    command should start (or continue) a turn."""
    raw = text.strip()
    head, _, rest = raw.partition(" ")
    head, rest = head.lower(), rest.strip()
    say = lambda out: ev(s, "command", out)
    cfg = config.load()

    if head in ("/help", "/?"):
        say("Commands:\n" + "\n".join(f"{c['cmd']} {c['args']}".strip().ljust(28) + f"  {c['help']}" for c in COMMANDS))
        return None
    if head == "/schedule":
        items = load_schedules()
        verb, _, arg = rest.partition(" ")
        if verb.lower() in ("", "list", "status"):
            say("\n".join(f"{n}. {'⏰' if x.get('enabled') else '⏸'} {schedule_text(x)} ({x['tz']}): {x['prompt'][:120]}"
                          for n, x in enumerate(items, 1)) or "No schedules. /schedule 3am for 2h make images of ... adds one.")
            return None
        if verb.lower() in ("cancel", "delete", "remove", "off"):
            try:
                x = items[int(arg) - 1]
            except (ValueError, IndexError):
                say("Usage: /schedule cancel N (N from /schedule list)")
                return None
            save_schedules([y for y in items if y["id"] != x["id"]])
            say(f"Deleted the schedule {schedule_text(x)}: {x['prompt'][:120]}")
            return None
        words = rest.split()
        when = words.pop(0) if words else ""
        if words and re.fullmatch(r"[ap]\.?m\.?", words[0].lower()):      # "3 am"
            when += words.pop(0)
        repeat, day = "once", ""
        if words and words[0].lower() in ("daily", "weekdays", "weekends", "tomorrow", "today") or (words and re.fullmatch(r"\d{4}-\d{2}-\d{2}", words[0])):
            word = words.pop(0).lower()
            if word in ("daily", "weekdays", "weekends"):
                repeat = word
            elif word == "tomorrow":
                day = (datetime.now(zone(s.get("tz", ""))).date() + timedelta(days=1)).isoformat()
            elif word != "today":
                day = word
        hours, request = 0.0, " ".join(words)
        if m := re.match(r"for\s+(\d+(?:\.\d+)?)\s*(h|hrs?|hours?|m|mins?|minutes?)\b\s*(.*)", request, re.I | re.S):
            hours = float(m.group(1)) / (60 if m.group(2).lower().startswith("m") else 1)
            request = m.group(3)
        try:
            x = parse_schedule(request, when, repeat, s.get("tz", ""), day, hours, s.get("model_override") or None)
        except ValueError as exc:
            say(f"{exc}.\nUsage: /schedule 3am [daily|weekdays|weekends|tomorrow|YYYY-MM-DD] [for 2h] <request>")
            return None
        save_schedules(items + [x])
        first = datetime.fromtimestamp(x["next"], zone(x["tz"])).strftime("%a %d %b %H:%M")
        say(f"⏰ Scheduled {schedule_text(x)} ({x['tz']}), first run {first}: {x['prompt'][:200]}\n"
            "It starts a new session then. /schedule list or Settings → Agentic shows your schedules.")
        return None
    if head == "/goal":
        verb, _, arg = rest.partition(" ")
        verb = verb.lower()
        g = s.get("goal")
        if verb in ("", "status", "show"):
            say(goal_line(g))
            return None
        if verb == "pause":
            if not g or g["status"] != "active":
                say("No active goal to pause.")
                return None
            g["status"] = "paused"
            say(f"⏸ Goal paused: {g['text']}")
            return None
        if verb == "resume":
            if not g or g["status"] == "active":
                say("No paused goal to resume." if not g else "The goal is already active.")
                return None
            g.update(status="active", achieved=False)
            if g["max_turns"] and g["turns"] >= g["max_turns"]:
                g["max_turns"] = g["turns"] + cfg["goal_max_turns"]
            say(f"▶ Goal resumed ({g['turns']} of {g['max_turns']} turns): {g['text']}")
            return "Resume work on the standing goal. Check what is already done (`$R jobs`) and continue.\n\nGoal: " + g["text"]
        if verb == "reopen":
            # A milestone closed on wrong evidence goes back to todo and becomes current again.
            parts = (g or {}).get("parts") or []
            done = [i for i, p in enumerate(parts) if p["status"] == "done"]
            try:
                n = int(arg) - 1 if arg.strip() else (done[-1] if done else -1)
            except ValueError:
                n = -1
            if not g or n < 0 or n >= len(parts) or parts[n]["status"] != "done":
                say("Usage: /goal reopen [N] (a done milestone; default: the last one closed)")
                return None
            parts[n].update(status="todo", evidence="")
            g["part"] = min(g.get("part", 0), n)
            g.update(achieved=False, status="active")
            say(f"↺ Milestone {n + 1} reopened: {parts[n]['text']}")
            return None
        if verb in ("clear", "stop", "done"):
            s["goal"] = None
            say("✓ Goal cleared." if g else "No goal set.")
            return None
        if verb == "budget":
            try:
                # 0 / infinite / unlimited = no turn limit (it runs until achieved, /goal pause or /stop).
                turns = 0 if arg.strip().lower() in UNLIMITED else max(1, min(100, int(arg)))
            except ValueError:
                say("Usage: /goal budget <turns> (1-100, or infinite)")
                return None
            if not g:
                say("No goal set.")
                return None
            g["max_turns"] = turns
            say(f"Goal budget: {turns or 'no limit on'} turns ({g['turns']} used).")
            return None
        objective, budget = rest, cfg["goal_max_turns"]
        if verb in UNLIMITED:
            # /goal infinite <objective>: keep going until it's achieved (or /goal pause, /stop).
            objective, budget = arg.strip(), 0
            if not objective:
                if not g:
                    say("Usage: /goal infinite <objective>")
                    return None
                g.update(max_turns=0, status="active", achieved=False)
                say(f"⊙ Goal set to no turn limit ({g['turns']} used): {g['text']}")
                return "Resume work on the standing goal. Check what is already done (`$R jobs`) and continue.\n\nGoal: " + g["text"]
        s["goal"] = {"text": objective[:12000], "status": "active", "turns": 0, "max_turns": budget,
                     "achieved": False, "evidence": "", "set_at": now()}
        say(f"⊙ Goal set ({f'{budget}-turn budget' if budget else 'no turn limit'}): {objective[:600]}\nAfter each turn the Master Agent either marks it "
            "achieved or keeps going. /goal status, /goal pause, /goal resume, /goal clear.")
        return ("Start working on the standing goal below. First split it into small milestones with "
                "`$R goal parts \"milestone 1 || milestone 2 || ...\"` (each one testable outcome, in build order), then do the first one."
                "\n\nGoal: " + objective)
    if head == "/browser":
        verb, _, arg = rest.partition(" ")
        verb = verb.lower() or "status"
        try:
            if verb == "status":
                say(browser_line(await browser_state(fresh=True) or {}))
            elif verb == "connect":
                say("🌐 Connecting a live browser…")
                state = await hostctl("POST", "/browser/connect", {"url": arg.strip()}, timeout=60)
                browser_cache.update(t=now(), data=state)
                say(("Opened a Chromium window on the desktop (Hermes' debug profile). " if state.get("launched") else "")
                    + browser_line(state))
            elif verb == "disconnect":
                state = await hostctl("POST", "/browser/disconnect", {}, timeout=60)
                browser_cache.update(t=now(), data=state)
                say("🌐 Disconnected. Hermes' browser tools are back to their default headless browser. "
                    "The browser window stays open; close it whenever you like.")
            else:
                say("Usage: /browser connect [url] | status | disconnect")
        except RuntimeError as exc:
            ev(s, "error", f"/browser {verb}: {exc}")
        return None
    if head == "/permissions":
        say(permissions_block(cfg).strip() + f"\nCommand approvals: {cfg['command_approval']} · Hermes session memory: "
            + ("on" if cfg["continuity"] else "off") + ". Change them in Settings → Agentic.")
        return None
    if head in ("/review", "/check"):
        words = [w.lower() for w in re.split(r"[\s,]+", rest) if w]
        if not words or words == ["status"]:
            say(review_line(cfg))
            return None
        if words[0] in ("off", "no", "false", "0"):
            say(review_line(config.save({"confirm_jobs": False})))
            return None
        words = [w for w in words if w not in ("on", "yes", "true", "1", "all", "everything")]
        kinds = [REVIEW_ALIASES.get(w, w) for w in words]
        unknown = [w for w, k in zip(words, kinds) if k not in REVIEW_KINDS]
        if unknown:
            say(f"Unknown: {', '.join(unknown)}. Usage: /review on | off | status, or what to check: "
                + ", ".join(REVIEW_KINDS) + " (e.g. /review music image)")
            return None
        say(review_line(config.save({"confirm_jobs": True, "review_services": [k for k in REVIEW_KINDS if k in kinds]})))
        return None
    if head == "/askfirst":
        word = rest.lower().strip()
        if word in ("on", "yes", "true", "1"):
            cfg = config.save({"ask_services": True})
        elif word in ("off", "no", "false", "0"):
            cfg = config.save({"ask_services": False})
        elif word in ("forget", "reset"):
            cfg = config.save({"cleared_services": []})
            s["cleared"] = []
            sessions.save(s["id"])
        elif word not in ("", "status"):
            say("Usage: /askfirst on | off | status | forget")
            return None
        say(ask_line(s, cfg))
        return None
    if head == "/approvals":
        mode = rest.lower()
        if mode not in APPROVAL_MODES:
            say(f"Command approvals: {cfg['command_approval']}. Usage: /approvals ask|allow|deny")
            return None
        config.save({"command_approval": mode})
        say({"ask": "Risky commands now wait for you here.", "allow": "Risky commands are now allowed without asking "
             "(Hermes' hard blocks still apply).", "deny": "Risky commands are now denied."}[mode])
        return None
    if head == "/retry":
        last = next((t for t in reversed(s["turns"]) if t["role"] == "user" and not t.get("queued")), None)
        if not last:
            say("Nothing to retry yet.")
            return None
        if s["state"] in BUSY:
            say("The Master Agent is still working; /stop first.")
            return None
        say("Sending your last message again.")
        return "The user asks you to try their last request again (do it differently if it went wrong):\n" + last["text"]
    if head == "/fresh":
        s.pop("hermes_session", None)
        s.update(hermes_runs=0, hermes_gen=s.get("hermes_gen", 0) + 1)
        say("The next turn starts a new Hermes conversation; it gets a summary of this session instead of the full history.")
        return None
    if head == "/rename":
        if rest.lower() == "auto":
            s.update(named="", named_asks=0)
            say("The Master Agent names this session on its next turn.")
            return None
        title = clean_title(rest)
        if not title:
            say("Usage: /rename <name>  ·  /rename auto lets the agent name it")
            return None
        set_title(s, title, "user")
        say(f"Renamed to \u201c{title}\u201d.")
        return None
    if head == "/jobs":
        await refresh_jobs(s)
        say("\n".join(job_line(j) for j in s["jobs"]) or "No jobs in this session yet.")
        return None
    if head == "/queue":
        try:
            entries = await studios.queue()
            say("\n".join(f"{e.get('position') or 'GPU'} · {e.get('app')} · {e.get('state')} · {str(e.get('label') or '')[:80]}"
                          for e in entries) or "The GPU queue is empty.")
        except studios.StudioError as exc:
            ev(s, "error", f"/queue: {exc}")
        return None
    if head == "/status":
        say(json.dumps(await studios.status(allowed()), indent=1))
        return None
    if head == "/model":
        model, provider, reasoning = run_model(s, cfg)
        say(f"Model: {model or 'Hermes default'}" + (f" ({provider})" if provider else "") + f" · thinking {reasoning}."
            + (" Chosen for this session by the app that started it." if s.get("model_override") else " Change it in Settings → Agentic."))
        return None
    say(f"Unknown command {head}. /help lists them.")
    return None


async def command(s: dict[str, Any], text: str) -> None:
    """A slash command typed into a session: log it, run it, start a turn if it asks for one."""
    ev(s, "user", text, command=True)
    if text.strip().lower().split(" ")[0] == "/stop":
        await stop(s["id"])
        return
    prompt = await run_command(s, text)
    if prompt and s["state"] not in BUSY:
        start(s, prompt, shown=False)
    elif prompt:
        ev(s, "system", "The goal takes over when the current turn ends.")


# ---------------- app ----------------

async def flusher() -> None:
    while True:
        await asyncio.sleep(1)
        sessions.flush()


@asynccontextmanager
async def lifespan(_: FastAPI):
    sessions.load()
    with suppress(OSError):
        ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    sweep_uploads()
    task = asyncio.create_task(flusher())
    relay = asyncio.create_task(rea_outbox())
    timer = asyncio.create_task(scheduler())
    asyncio.create_task(backfill_agentic_clips())
    for s in sessions.items.values():          # restarted while its jobs rendered: keep watching them
        if s["state"] == "rendering":
            runners[s["id"]] = asyncio.create_task(resume_after_restart(s["id"]))
    try:
        yield
    finally:
        task.cancel()
        relay.cancel()
        timer.cancel()
        sessions.flush()


REA_API = os.getenv("REA_WEB_URL", "http://127.0.0.1:8976/REA/api").rstrip("/")


async def rea_outbox() -> None:
    """REA queues agent requests its server makes on its own (a failed mod test run asking the agent
    to fix it) because it can't reach this service from its network. Deliver them: a message to the
    session they name, or a new session; then tell REA which session took it."""
    def poll() -> list[dict[str, Any]]:
        # urllib, not httpx: a poll every few seconds shouldn't fill the log with request lines.
        with urllib.request.urlopen(f"{REA_API}/outbox", timeout=10) as r:
            return json.loads(r.read()).get("items", [])

    while True:
        await asyncio.sleep(8)
        try:
            items = await asyncio.to_thread(poll)
            if not items:
                continue
            async with httpx.AsyncClient(timeout=30) as client:
                for item in items[:5]:
                    sid, text, model = str(item.get("session") or ""), str(item.get("text") or ""), item.get("model") or None
                    if not text:
                        continue
                    resp = None
                    if sid in sessions.items:
                        resp = await client.post(f"{SELF_URL}{p(f'/api/sessions/{sid}/message')}",
                                                 json={"text": text, "on_busy": "queue", "model": model})
                    if resp is None or resp.status_code == 404:
                        resp = await client.post(f"{SELF_URL}{p('/api/sessions')}", json={"prompt": text, "model": model})
                        sid = resp.json().get("id", "") if resp.status_code < 300 else ""
                    if resp.status_code < 300 and sid:
                        await client.post(f"{REA_API}/outbox/{item['id']}/done", json={"session": sid}, headers={"X-REA": "1"})
                    else:
                        log.warning("REA outbox %s: %s %s", item.get("id"), resp.status_code, resp.text[:200])
        except asyncio.CancelledError:
            raise
        except Exception:  # REA down or restarting: try again next round
            pass


# ---------------- schedules ----------------
# "At 03:00 make images for two hours": a prompt sent to a NEW session at a set time (once or
# daily, in the user's own timezone). With a duration it runs as a goal until that time is up,
# then the session is stopped (jobs already started finish on their own).

SCHEDULES = DATA / "schedules.json"
REPEATS = ("once", "daily", "weekdays", "weekends")


def load_schedules() -> list[dict[str, Any]]:
    with suppress(OSError, ValueError):
        items = json.loads(SCHEDULES.read_text())
        if isinstance(items, list):
            return items
    return []


def save_schedules(items: list[dict[str, Any]]) -> None:
    tmp = SCHEDULES.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2))
    tmp.replace(SCHEDULES)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def next_run(x: dict[str, Any], after: float) -> float | None:
    """The next start time (epoch seconds) after `after`, or None when a one-off has passed."""
    tz = zone(x.get("tz", ""))
    hour, minute = (int(v) for v in x["time"].split(":"))
    if x["repeat"] == "once":
        day = Date.fromisoformat(x["date"])
        at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).timestamp()
        return at if at > after else None
    day = datetime.fromtimestamp(after, tz).date()
    for _ in range(9):
        at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).timestamp()
        weekday = day.weekday()
        fits = (x["repeat"] == "daily" or (x["repeat"] == "weekdays" and weekday < 5)
                or (x["repeat"] == "weekends" and weekday >= 5))
        if at > after and fits:
            return at
        day += timedelta(days=1)
    return None


def schedule_text(x: dict[str, Any]) -> str:
    when = {"once": f"on {x.get('date')}", "daily": "every day", "weekdays": "on weekdays", "weekends": "on weekends"}[x["repeat"]]
    span = f" for {x['hours']:g} h" if x.get("hours") else ""
    return f"{x['time']} {when}{span}"


def public_schedule(x: dict[str, Any]) -> dict[str, Any]:
    return {**x, "summary": schedule_text(x)}


def parse_schedule(prompt: str, time_: str, repeat: str, tz: str, day: str = "", hours: float = 0,
                   model: dict[str, Any] | None = None) -> dict[str, Any]:
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("Write what the Master Agent should do")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?", time_.strip().lower()) or re.fullmatch(r"(\d{1,2})()\s*([ap]\.?m\.?)", time_.strip().lower())
    if not m:
        raise ValueError("Give the time as HH:MM (24 h) or like 3am / 3:30pm")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        if not 1 <= hour <= 12:
            raise ValueError("With am/pm the hour is 1-12")
        hour = hour % 12 + (12 if m.group(3).startswith("p") else 0)
    if hour > 23 or minute > 59:
        raise ValueError("That isn't a time of day")
    if repeat not in REPEATS:
        raise ValueError(f"Repeat is one of {', '.join(REPEATS)}")
    if not 0 <= hours <= 72:
        raise ValueError("Run for 0-72 hours (0 = until the request is done)")
    x = {"id": uuid.uuid4().hex[:10], "prompt": prompt[:12000], "time": f"{hour:02d}:{minute:02d}", "repeat": repeat,
         "tz": zone(tz).key, "hours": round(float(hours), 2), "enabled": True, "created": now(), "last_run": None,
         "last_session": None, "model": model or None}
    tz_ = zone(x["tz"])
    if repeat == "once":
        today = datetime.now(tz_).date()
        try:
            x["date"] = Date.fromisoformat(day).isoformat() if day else today.isoformat()
        except ValueError as exc:
            raise ValueError("Give the date as YYYY-MM-DD") from exc
        if not day and next_run(x, now()) is None:          # "3am" said at 10pm: tomorrow
            x["date"] = (today + timedelta(days=1)).isoformat()
    x["next"] = next_run(x, now())
    if x["next"] is None:
        raise ValueError("That time has already passed")
    return x


async def run_schedule(x: dict[str, Any]) -> str:
    """Start a scheduled prompt in a new session; returns the session id."""
    s = new_session("⏰ " + x["prompt"])
    if x.get("model"):
        with suppress(HTTPException, Exception):
            set_model_choice(s, await check_model_choice(ModelChoice(**x["model"])))
    ev(s, "system", f"Started by your schedule ({schedule_text(x)})")
    prompt = x["prompt"]
    if x.get("hours"):
        until = now() + x["hours"] * 3600
        until_text = datetime.fromtimestamp(until, zone(x["tz"])).strftime("%H:%M")
        s["goal"] = {"text": prompt, "status": "active", "turns": 0, "max_turns": 0, "achieved": False, "evidence": "",
                     "set_at": now(), "until": until, "until_text": until_text, "schedule": x["id"]}
        prompt = (f"SCHEDULED RUN: the user scheduled this to run for {x['hours']:g} hours, until {until_text}. Keep working on "
                  "it in batches the whole time (you are resumed after each batch and stopped at the end); don't finish early.\n\n"
                  + prompt)
    start(s, prompt, shown=x["prompt"])
    return s["id"]


async def scheduler() -> None:
    """Fire due schedules (a start missed by over 10 minutes, e.g. the service was down, is skipped)
    and stop timed runs whose time is up."""
    while True:
        await asyncio.sleep(20)
        try:
            items, dirty, t = load_schedules(), False, now()
            for x in items:
                if not x.get("enabled") or not x.get("next") or x["next"] > t:
                    continue
                if t - x["next"] <= 600:
                    x["last_session"] = await run_schedule(x)
                    x["last_run"] = t
                else:
                    log.warning("schedule %s missed its %s start", x["id"], x["time"])
                    x["missed"] = x["next"]
                x["next"] = next_run(x, t)
                x["enabled"] = x["next"] is not None
                dirty = True
            if dirty:
                save_schedules(items)
            for sid, s in list(sessions.items.items()):
                g = s.get("goal") or {}
                if g.get("until") and g.get("status") == "active" and t >= g["until"]:
                    g["status"] = "done"
                    ev(s, "ok", "The scheduled time is up; stopping (jobs already started finish on their own)")
                    if s["state"] in BUSY or sid in runners:
                        await stop(sid)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("scheduler round failed")


async def resume_after_restart(sid: str) -> None:
    s = sessions.items[sid]
    ev(s, "system", "The Agent service restarted; still watching this session's jobs")
    await watch_jobs(s)
    runners.pop(sid, None)
    if not s.get("stop"):
        start(s, "(The Agent service restarted while your jobs rendered.) Your jobs have finished; check them with `$R jobs` "
                 "and finish the original request.")


app = FastAPI(title="Rocsy Agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
STATIC = Path(__file__).parent / "static"
app.mount(p("/static"), StaticFiles(directory=STATIC), name="static")


def asset_v() -> str:
    return str(int(max(f.stat().st_mtime for f in STATIC.iterdir())))


def session_list(limit: int = 100) -> list[dict[str, Any]]:
    items = sorted(sessions.items.values(), key=lambda s: s["updated"], reverse=True)
    items = items[:limit] if limit > 0 else items
    return [{"id": s["id"], "title": s["title"], "named": s.get("named", ""), "state": s["state"], "stage": s.get("stage", ""),
             "updated": s["updated"], "jobs": len(s["jobs"]), "busy": s["state"] in BUSY} for s in items]


@app.middleware("http")
async def cache_assets(request: Request, call_next):
    response = await call_next(request)
    # Static files are linked with ?v=<mtime>: a change gets a new URL, so browsers may keep them.
    if request.url.path.startswith(p("/static/")) and "v=" in request.url.query and response.status_code == 200:
        response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(p("/"))
@app.get(p(""))
async def index(s: str = ""):
    # The list and the open session ride along in the page, so the first paint needs no fetch.
    initial = {"sessions": session_list(), "session": public(sessions.items[s]) if s in sessions.items else None,
               "commands": COMMANDS}
    data = json.dumps(initial).replace("<", "\\u003c")
    html = (STATIC / "index.html").read_text().replace("{{v}}", asset_v()).replace("{{initial}}", data)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


class ModelChoice(BaseModel):
    """Optional per-session model and thinking choice for apps such as REA."""
    model: str = Field(default="", max_length=200)
    provider: str = Field(default="", max_length=120)
    reasoning: str = Field(default="", max_length=20)


class SessionBody(BaseModel):
    prompt: str = Field(default="", max_length=20000)
    attachments: list[str] = Field(default_factory=list, max_length=20)
    model: ModelChoice | None = None
    tz: str = Field(default="", max_length=64)       # the page's timezone, for /schedule times


class ScheduleBody(BaseModel):
    prompt: str = Field(max_length=12000)
    time: str = Field(max_length=12)
    repeat: str = Field(default="once", max_length=10)
    date: str = Field(default="", max_length=10)
    hours: float = Field(default=0, ge=0, le=72)
    tz: str = Field(default="", max_length=64)
    model: ModelChoice | None = None


class ScheduleEdit(BaseModel):
    enabled: bool


class MessageBody(BaseModel):
    tz: str = Field(default="", max_length=64)
    text: str = Field(default="", max_length=20000)
    attachments: list[str] = Field(default_factory=list, max_length=20)
    # While the agent's jobs render: "ask" (409 with what is rendering), "cancel" (stop them and
    # send now) or "queue" (send when they finish).
    on_busy: str = Field(default="ask", pattern="^(ask|cancel|queue)$")
    # Switch this session's model from this message on (see ModelChoice); None = keep it.
    model: ModelChoice | None = None


class ApproveBody(BaseModel):
    approve: bool = False
    choice: str = Field(default="", pattern="^(|once|session|deny)$")   # a command approval


class ReviewBody(BaseModel):
    id: str = Field(pattern=r"^(\*|p\d{1,4})$")          # a proposal, or * for every waiting one
    decision: str = Field(pattern="^(approve|veto)$")
    always: bool = False                                  # Ask first: clear the service for every session
    fields: dict[str, str] = Field(default_factory=dict)  # the user's edits to its prompt fields
    note: str = Field(default="", max_length=2000)


class AgenticDecisionBody(BaseModel):
    action: str = Field(pattern="^(accept|exclude)$")
    notes: str = Field(default="", max_length=4000)


def json_only(request: Request) -> None:
    # A cross-site form can't send JSON without a CORS preflight.
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415, "JSON only")


@app.get(p("/api/config"))
async def get_config() -> dict[str, Any]:
    cfg = config.load()
    return {**cfg, "default_brief": DEFAULT_BRIEF, "brief_required": REQUIRED, "services_list": list(studios.SERVICES),
            "permissions_list": [{k: x[k] for k in ("key", "label", "default", "choices") if k in x}
                                 | {"note": x.get("on") or x.get("workspace"), "notes": {c: x[c].format(workspace=HERMES_WORKSPACE)
                                    for c in x.get("choices", [])}} for x in PERMISSIONS],
            "approval_modes": list(APPROVAL_MODES), "workspace": HERMES_WORKSPACE, "ask_services_list": ASK_SERVICES,
            "image_engines": [{"id": k, "label": v["label"], "headless": v["headless"], "private": v.get("private", True)}
                              for k, v in IMAGE_ENGINES.items()],
            "compression_presets": COMPRESS_PRESETS, "compression_fields": COMPRESS_FIELDS}


@app.put(p("/api/config"))
async def put_config(request: Request) -> dict[str, Any]:
    json_only(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected an object")
    brief = body.get("brief")
    if isinstance(brief, str) and brief.strip():
        missing = [r for r in REQUIRED if r not in brief]
        if missing:
            raise HTTPException(422, "The Master Agent brief must contain: " + ", ".join(missing))
    config.save(body)
    return await get_config()


@app.get(p("/api/options"))
async def get_options() -> dict[str, Any]:
    out: dict[str, Any] = {"reasoning": REASONING,
                           "video_models": list(studios.VIDEO_MODELS), "voice_models": list(studios.VOICE_MODELS),
                           "image_models": list(studios.KREA_MODELS), "music_models": list(studios.MUSIC_MODELS)}
    try:
        out["hermes"] = await hermes_models()
    except Exception as exc:  # noqa: BLE001
        out["hermes"] = {"providers": [], "default": {}, "error": f"Hermes is not reachable ({type(exc).__name__})"}
    try:
        out["clis"] = (await hostctl("GET", "/clis"))["clis"]
    except Exception:  # noqa: BLE001 - no host helper: no CLIs to offer
        out["clis"] = []
    try:
        out["vid_presets"] = sorted(await studios.vid_presets())
    except studios.StudioError:
        out["vid_presets"] = []
    return out


@app.get(p("/api/sessions"))
async def list_sessions(limit: int = 100) -> dict[str, Any]:
    """limit=0 lists every session (Settings → Cache/Storage log cleanup)."""
    return {"sessions": session_list(limit)}


@app.get(p("/api/sessions/summary"))
async def sessions_summary(ids: str = "") -> dict[str, Any]:
    """A few sessions at a glance for other apps (REA's Sessions tab): state, model, Hermes runs,
    jobs and the latest message, without the whole event history."""
    cfg = config.load()
    out = []
    for sid in [x.strip() for x in ids.split(",") if x.strip()][:100]:
        s = sessions.items.get(sid)
        if not s:
            out.append({"id": sid, "missing": True})
            continue
        model, provider, reasoning = run_model(s, cfg)
        said = next((e for e in reversed(s["events"]) if e["kind"] in ("assistant", "say")), None)
        asked = next((e for e in reversed(s["events"]) if e["kind"] == "user"), None)
        out.append({"id": sid, "title": s["title"], "state": s["state"], "stage": s.get("stage", ""), "busy": s["state"] in BUSY,
                    "created": s.get("created"), "updated": s.get("updated"), "jobs": len(s.get("jobs", [])),
                    "hermes_session": s.get("hermes_session", ""), "hermes_runs": s.get("hermes_runs", 0),
                    "model": model or (models_cache.get("data", {}).get("default", {}).get("model") or "Hermes default"),
                    "provider": provider, "reasoning": reasoning, "chosen": bool(s.get("model_override")),
                    "last_user": (asked or {}).get("text", "")[:400], "last_agent": (said or {}).get("text", "")[:600],
                    "error": s.get("error") or ""})
    return {"sessions": out}


def sse(data: Any, event: str = "") -> str:
    return (f"event: {event}\n" if event else "") + f"data: {json.dumps(data)}\n\n"


# ---------------- auto-compression progress ----------------
# Hermes compresses with one non-streamed summary call, so its own events can't say how far along it
# is. The rocsy plugin reports the phases (start, summary dispatched + Hermes' summary token target,
# done/skipped); while the summary runs on the local llama.cpp, its /slots show the prompt tokens
# read so far and the tokens written. Progress = both phases weighted by their expected time.

SUMMARY_THINK = 1.25      # the model thinks before it writes: expect ~25% more tokens than the target


def compress_event(s: dict[str, Any], args: dict[str, Any]) -> None:
    sid, phase = s["id"], str(args.get("phase") or "")
    c = compress_live.get(sid)
    if phase == "start" or c is None:
        c = compress_live[sid] = {"phase": "preparing", "pct": 0, "t0": now(), "tokens": args.get("tokens"),
                                  "messages": args.get("messages")}
    if phase == "summary":
        c.update(phase="summary", t_call=now(), model=str(args.get("model") or "")[:200],
                 prompt_est=int(int(args.get("prompt_chars") or 0) / 3.5) or None, budget=args.get("budget"))
    elif phase in ("done", "skipped"):
        c.update(phase=phase, pct=100, eta=None, t_end=now(), after=args.get("tokens"), after_messages=args.get("messages"))
        asyncio.get_running_loop().call_later(4, compress_clear, sid, c)
    changed(sid)
    if c["phase"] in ("preparing", "summary") and not (compress_pollers.get(sid) and not compress_pollers[sid].done()):
        compress_pollers[sid] = asyncio.create_task(compress_poll(sid))


def compress_clear(sid: str, c: dict[str, Any] | None = None) -> None:
    if c is None or compress_live.get(sid) is c:
        compress_live.pop(sid, None)
        changed(sid)


async def summary_slot(client: httpx.AsyncClient, model: str, prompt_est: int | None) -> dict[str, Any] | None:
    """The llama.cpp slot writing the summary. Only asks a model llama-swap already runs: /upstream
    would load one that isn't, and that is VRAM the summary never asked for."""
    running = (await client.get(LLAMA_URL + "/running", headers=llama_headers())).json().get("running") or []
    if not any(r.get("model") == model and r.get("state") == "ready" for r in running):
        return None
    slots = (await client.get(f"{LLAMA_URL}/upstream/{model}/slots", headers=llama_headers())).json()
    busy = [x for x in slots if isinstance(x, dict) and x.get("is_processing")] if isinstance(slots, list) else []
    if not busy:
        return None
    return min(busy, key=lambda x: abs((x.get("n_prompt_tokens") or 0) - (prompt_est or 0)))


async def compress_poll(sid: str) -> None:
    """Once a second while compressing: how much of the prompt is read and of the summary written."""
    rates: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            while (c := compress_live.get(sid)) and c["phase"] in ("preparing", "summary"):
                await asyncio.sleep(1)
                s = sessions.items.get(sid)
                if s is None or now() - c["t0"] > 3600:
                    compress_clear(sid)
                    return
                if c["phase"] != "summary":
                    continue
                u = s.get("usage") or {}
                slot = None
                with suppress(Exception):
                    slot = await summary_slot(client, c.get("model") or run_model(s, config.load())[0], c.get("prompt_est"))
                if (c := compress_live.get(sid)) is None or c["phase"] != "summary":
                    return
                target = int((c.get("budget") or 2000) * SUMMARY_THINK)
                if slot:
                    total = int(slot.get("n_prompt_tokens") or c.get("prompt_est") or 0)
                    tok = slot.get("next_token") or [{}]
                    decoded = int((tok[0] if isinstance(tok, list) and tok else tok or {}).get("n_decoded") or 0)
                    read = total if decoded else min(total, int(slot.get("n_prompt_tokens_processed") or 0)
                                                     + int(slot.get("n_prompt_tokens_cache") or 0))
                    t = now()
                    for key, value in (("read", read), ("wrote", decoded)):
                        last = rates.get(key)
                        if last and last[1] and value > last[1] and t > last[0]:   # from 0 = a partial second
                            rate = (value - last[1]) / (t - last[0])
                            rates[key + "_rate"] = rate if key + "_rate" not in rates else rates[key + "_rate"] * 0.6 + rate * 0.4
                        rates[key] = (t, value)
                    read_rate = rates.get("read_rate") or u.get("prefill_tps") or 500
                    write_rate = rates.get("wrote_rate") or u.get("tps") or 20
                    target = max(target, int(decoded * 1.05))  # writing past Hermes' target: still not done
                    read_t, write_t = total / read_rate, target / write_rate
                    done_t = min(read, total) / read_rate + min(decoded, target) / write_rate
                    left = max(0, total - read) / read_rate + max(0, target - decoded) / write_rate
                    c.update(source="slots", stage="writing" if decoded else "reading", read=read, total=total,
                             wrote=decoded, target=target, eta=round(left) or None,
                             pct=round(min(99, 2 + 97 * done_t / max(1e-6, read_t + write_t)), 1))
                else:
                    # Cloud model (or the slot is between calls): estimate from the last measured speeds.
                    total = c.get("prompt_est") or c.get("tokens") or 0
                    est = total / (u.get("prefill_tps") or 500) + target / (u.get("tps") or 20)
                    spent = now() - c.get("t_call", c["t0"])
                    c.update(source="estimate", stage="estimating", target=target,
                             pct=round(min(95, 2 + 93 * spent / max(1.0, est)), 1), eta=round(max(0, est - spent)) or None)
                changed(sid)
    finally:
        compress_pollers.pop(sid, None)


# ---------------- prompt reading progress ----------------
# Before a local model writes its first token it has to (load and) read the whole prompt: a fresh
# session, a resumed one after the model was unloaded, and every call right after a compression
# (the summary changes the start of the prompt, so nothing is cached). That can take minutes on a
# big context; the page shows it as a bar with the time left. llama.cpp reports it in /slots,
# Strata in /v1/status; both through llama-swap, and only for a model it already runs.

async def model_progress(client: httpx.AsyncClient, model: str, rates: dict[str, Any]) -> dict[str, Any] | None:
    running = (await client.get(LLAMA_URL + "/running", headers=llama_headers())).json().get("running") or []
    entry = next((r for r in running if r.get("model") == model), None)
    if entry is None:
        return None
    if entry.get("state") in ("starting", "loading"):
        return {"stage": "loading"}
    if entry.get("state") != "ready":
        return None
    t = now()
    if model.startswith("strata"):
        r = await client.get(f"{LLAMA_URL}/upstream/{model}/v1/status", headers=llama_headers())
        live = (r.json() or {}).get("live") or {}
        if live.get("state") != "reading":
            return None
        read, total = int(live.get("prompt_read") or 0), int(live.get("prompt_total") or live.get("prompt_tokens") or 0)
        rate = live.get("prefill_tok_s_mean")
    else:
        slots = (await client.get(f"{LLAMA_URL}/upstream/{model}/slots", headers=llama_headers())).json()
        busy = [x for x in slots if isinstance(x, dict) and x.get("is_processing")] if isinstance(slots, list) else []
        reading = []
        for x in busy:
            tok = x.get("next_token") or [{}]
            if not int((tok[0] if isinstance(tok, list) and tok else tok or {}).get("n_decoded") or 0):
                reading.append(x)
        if not reading:
            return None
        x = max(reading, key=lambda x: int(x.get("n_prompt_tokens") or 0))
        total = int(x.get("n_prompt_tokens") or 0)
        read = min(total, int(x.get("n_prompt_tokens_processed") or 0) + int(x.get("n_prompt_tokens_cache") or 0))
        rate = None
    if total <= 0:
        return None
    last = rates.get("last")
    if last and last[2] == total and read > last[1] and t > last[0]:
        now_rate = (read - last[1]) / (t - last[0])
        rates["rate"] = now_rate if not rates.get("rate") else rates["rate"] * 0.6 + now_rate * 0.4
    elif not last or last[2] != total:
        rates.pop("rate", None)                      # a new prompt
    rates["last"] = (t, read, total)
    rate = rates.get("rate") or rate
    eta = round((total - read) / rate) if rate and read < total else None
    return {"stage": "reading", "read": read, "total": total, "rate": round(rate) if rate else None, "eta": eta,
            "pct": round(100 * read / total, 1)}


# llama-swap only says "starting" while a model loads, with no progress. The time to load is
# learned instead: the last LOAD_SAMPLES loads of each model, averaged (one or two still count).
LOAD_TIMES = DATA / "load_times.json"
LOAD_SAMPLES = 5


def load_times() -> dict[str, list[float]]:
    try:
        data = json.loads(LOAD_TIMES.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_estimate(model: str) -> dict[str, Any] | None:
    samples = [x for x in load_times().get(model) or [] if isinstance(x, (int, float))]
    return {"avg": round(sum(samples) / len(samples), 1), "n": len(samples)} if samples else None


def record_load_time(model: str, seconds: float) -> None:
    data = load_times()
    data[model] = ([x for x in data.get(model) or [] if isinstance(x, (int, float))] + [round(seconds, 1)])[-LOAD_SAMPLES:]
    with suppress(OSError):
        tmp = LOAD_TIMES.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(LOAD_TIMES)


async def model_state(client: httpx.AsyncClient, model: str) -> str:
    running = (await client.get(LLAMA_URL + "/running", headers=llama_headers())).json().get("running") or []
    return str(next((r.get("state") for r in running if r.get("model") == model), "") or "")


async def prefill_watch(s: dict[str, Any]) -> None:
    """Once a second during a run: the model loading or reading the prompt, for the page's bar."""
    sid, rates, since = s["id"], {}, None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            while True:
                await asyncio.sleep(1)
                cfg = config.load()
                info = None
                model = run_model(s, cfg)[0]
                if is_local(cfg, s) and sid not in compress_live:     # the compression bar covers its own call
                    with suppress(Exception):
                        info = await model_progress(client, model, rates)
                was = prefill_live.get(sid) or {}
                if was.get("stage") == "loading" and (info or {}).get("stage") != "loading" and since:
                    with suppress(Exception):              # loaded (not failed or stopped): remember how long it took
                        if await model_state(client, was.get("model") or model) == "ready":
                            record_load_time(was.get("model") or model, now() - since)
                if info:
                    if since is None or info["stage"] != was.get("stage"):
                        since = now()
                    info["t0"] = since
                    if info["stage"] == "loading":
                        info["model"] = model
                        info["estimate"] = load_estimate(model)
                    if prefill_live.get(sid) != info:
                        prefill_live[sid] = info
                        changed(sid)
                elif prefill_live.pop(sid, None) is not None:
                    since = None
                    changed(sid)
    finally:
        if prefill_live.pop(sid, None) is not None:
            changed(sid)


@app.get(p("/api/sessions/{sid}/events"))
async def session_events(sid: str, request: Request, since: int = -1):
    """Server-sent events for one open session: new timeline events plus state/jobs
    whenever anything changes, and the sessions list when any session changes."""
    async def stream():
        last, sent, sent_list = since, None, None
        while True:
            wake = signal(sid)                 # take it before reading, so no change slips past
            wake_list = list_signals[0]
            s = sessions.items.get(sid)
            if s is None:
                yield sse({}, "gone")
                return
            snap = public(s, last)
            if snap["events"]:
                last = snap["events"][-1]["i"]
            key = json.dumps([snap["state"], snap.get("stage"), snap["jobs"], snap.get("pending"), snap["summary"], snap["title"], snap["live"], snap.get("usage"), snap.get("compress"), snap.get("prefill"), snap.get("parked"), snap.get("boards")], default=str)
            if snap["events"] or key != sent:
                sent = key
                yield sse(snap)
            listing = session_list()
            list_key = json.dumps([[i["id"], i["state"], i["jobs"], i["title"], i["named"]] for i in listing])
            if list_key != sent_list:
                sent_list = list_key
                yield sse(listing, "sessions")
            if await request.is_disconnected():
                return
            waiters = [asyncio.ensure_future(wake.wait()), asyncio.ensure_future(wake_list.wait())]
            done, pending = await asyncio.wait(waiters, timeout=15, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if not done:
                yield ": ping\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get(p("/api/schedules"))
async def list_schedules() -> dict[str, Any]:
    return {"items": [public_schedule(x) for x in load_schedules()], "repeats": list(REPEATS)}


@app.post(p("/api/schedules"), status_code=201)
async def add_schedule(body: ScheduleBody, request: Request) -> dict[str, Any]:
    json_only(request)
    model = await check_model_choice(body.model) if body.model else None
    try:
        x = parse_schedule(body.prompt, body.time, body.repeat, body.tz, body.date, body.hours, model or None)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    save_schedules(load_schedules() + [x])
    return public_schedule(x)


@app.patch(p("/api/schedules/{xid}"))
async def edit_schedule(xid: str, body: ScheduleEdit, request: Request) -> dict[str, Any]:
    json_only(request)
    items = load_schedules()
    x = next((y for y in items if y["id"] == xid), None)
    if not x:
        raise HTTPException(404, "No such schedule")
    x["next"] = next_run(x, now()) if body.enabled else x.get("next")
    if body.enabled and x["next"] is None:
        raise HTTPException(409, "That one-off time has passed; add a new schedule")
    x["enabled"] = body.enabled
    save_schedules(items)
    return public_schedule(x)


@app.delete(p("/api/schedules/{xid}"))
async def delete_schedule(xid: str) -> dict[str, Any]:
    items = load_schedules()
    if not any(y["id"] == xid for y in items):
        raise HTTPException(404, "No such schedule")
    save_schedules([y for y in items if y["id"] != xid])
    return {"deleted": xid}


@app.post(p("/api/sessions"), status_code=201)
async def create_session(body: SessionBody, request: Request) -> dict[str, Any]:
    json_only(request)
    text = body.prompt.strip()
    if not text and not body.attachments:
        raise HTTPException(422, "Write a message or attach something")
    head = text.split(" ")[0].lower()
    if head == "/new":
        text = text[4:].strip() or "New session"
    choice = await check_model_choice(body.model)
    s = new_session(text or "Attachment: " + ", ".join((attachment_meta(a) or {}).get("name", "file") for a in body.attachments))
    set_model_choice(s, choice)
    if body.tz:
        s["tz"] = body.tz
    matched = rag_pinned_ids(text, body.attachments)
    attachment_ids = body.attachments + matched
    if attachment_ids and not text.startswith("/"):
        start(s, text or "(see the attachments)", attachments=claim_attachments(s, attachment_ids))
        return public(s)
    if text.startswith("/"):
        if head == "/goal" and text[5:].strip():
            title = s["title"][6:]
            s["title"] = "Goal: " + (title.split(" ", 1)[1] if title.split(" ", 1)[0].lower() in UNLIMITED and " " in title else title)
        set_state(s, "done", "")
        await command(s, text)
    else:
        start(s, text)
    return public(s)


INLINE_TYPES = re.compile(r"^(image/(png|jpeg|gif|webp|avif)|video/(mp4|webm|quicktime)|audio/[\w.+-]+)$")


async def _receive(request: Request, path: Path, room: int) -> int:
    size = 0
    with open(path, "ab") as handle:
        async for chunk in request.stream():
            size += len(chunk)
            if size > room:
                raise HTTPException(413, f"Attachments can be up to {MAX_UPLOAD // 1024 // 1024} MB")
            handle.write(chunk)
    return size


def _upload_guard(request: Request) -> None:
    # A custom header can't be sent cross-site without a CORS preflight, which this service never answers.
    if request.headers.get("x-agent-upload") != "1":
        raise HTTPException(403, "Upload from the Agent page")


@app.post(p("/api/uploads"), status_code=201)
async def upload(request: Request) -> dict[str, Any]:
    """Start an attachment: the raw body is its first part (the page sends 8 MB parts, under the
    gateway's body limit); X-Upload-Total is the whole size, X-Filename its name."""
    _upload_guard(request)
    name = safe_name(request.headers.get("x-filename", "file"))
    mime = (request.headers.get("content-type") or "application/octet-stream").split(";")[0].strip().lower()[:100]
    try:
        total = int(request.headers.get("x-upload-total") or 0)
    except ValueError:
        total = 0
    if not 0 < total <= MAX_UPLOAD:
        raise HTTPException(413 if total else 400, f"Attachments can be 1 byte to {MAX_UPLOAD // 1024 // 1024} MB")
    aid = uuid.uuid4().hex
    folder = ATTACH_DIR / aid
    folder.mkdir(parents=True, exist_ok=True)
    try:
        size = await _receive(request, folder / name, total)
    except BaseException:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        raise
    meta = {"id": aid, "name": name, "mime": mime, "kind": attachment_kind(mime), "size": size, "total": total,
            "complete": size == total, "t": now(), "url": p(f"/api/attachments/{aid}"),
            "hermes_path": f"{HERMES_ATTACH_DIR}/{aid}/{name}", "session": None}
    (folder / "meta.json").write_text(json.dumps(meta))
    return {**public_attachments([meta])[0], "received": size, "complete": meta["complete"]}


@app.post(p("/api/uploads/{aid}/append"))
async def upload_part(aid: str, request: Request) -> dict[str, Any]:
    _upload_guard(request)
    meta = attachment_meta(aid)
    if not meta or meta.get("complete"):
        raise HTTPException(404, "No unfinished upload with that id")
    if request.headers.get("x-upload-offset") != str(meta["size"]):
        raise HTTPException(409, f"Expected the part at byte {meta['size']}")
    added = await _receive(request, ATTACH_DIR / aid / meta["name"], meta["total"] - meta["size"])
    meta["size"] += added
    meta["complete"] = meta["size"] == meta["total"]
    (ATTACH_DIR / aid / "meta.json").write_text(json.dumps(meta))
    return {**public_attachments([meta])[0], "received": meta["size"], "complete": meta["complete"]}


@app.delete(p("/api/uploads/{aid}"))
async def drop_upload(aid: str) -> dict[str, Any]:
    meta = attachment_meta(aid)
    if not meta or meta.get("session"):
        raise HTTPException(404, "No such unsent upload")
    import shutil
    shutil.rmtree(ATTACH_DIR / aid, ignore_errors=True)
    return {"deleted": aid}


@app.get(p("/api/pinned"))
async def pinned_files() -> dict[str, Any]:
    """Expose only currently pinned cache entries to the @ picker."""
    with suppress(OSError, ValueError, TypeError, AttributeError):
        index = json.loads(PINNED_INDEX.read_text())
        files = [meta for key in index if (meta := pinned_meta(f"pinned-{key}"))]
        files.sort(key=lambda item: Path(item["pinned_path"]).stat().st_mtime, reverse=True)
        groups = []
        with suppress(OSError, ValueError, TypeError, AttributeError):
            group_index = json.loads(PINNED_GROUPS_INDEX.read_text())
            active = {item["id"][7:] for item in files}
            groups = [{"id": key, "name": group["name"],
                       "files": [file_id for file_id in group.get("files", []) if file_id in active],
                       "rag_terms": group.get("rag_terms", []), "note": group.get("note", "")}
                      for key, group in group_index.items() if isinstance(group, dict)
                      and isinstance(group.get("name"), str)]
            groups.sort(key=lambda group: group["name"].casefold())
        return {"groups": groups, "files": [{key: item[key] for key in ("id", "name", "mime", "kind", "size", "url")}
                          | {"alias": index[item["id"][7:]].get("alias", ""),
                             "rag_terms": index[item["id"][7:]].get("rag_terms", []),
                             "note": index[item["id"][7:]].get("note", "")}
                          for item in files]}
    return {"files": [], "groups": []}


@app.get(p("/api/attachments/{aid}"))
async def get_attachment(aid: str):
    meta = attachment_meta(aid)
    if not meta or not meta.get("complete", True):
        raise HTTPException(404, "No such attachment")
    try:
        path = pinned_path(meta)
    except studios.StudioError:
        raise HTTPException(404, "No such attachment") from None
    if not path.is_file():
        raise HTTPException(404, "No such attachment")
    inline = bool(INLINE_TYPES.match(meta["mime"]))
    # Anything that isn't plain media downloads instead of rendering on this origin (no HTML/SVG here).
    return FileResponse(path, media_type=meta["mime"] if inline else "application/octet-stream",
                        filename=meta["name"], content_disposition_type="inline" if inline else "attachment",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400",
                                 "Content-Security-Policy": "default-src 'none'; sandbox"})


@app.get(p("/api/commands"))
async def list_commands() -> dict[str, Any]:
    return {"commands": COMMANDS}


@app.get(p("/api/sessions/{sid}"))
async def get_session(sid: str, since: int = -1) -> dict[str, Any]:
    s = sessions.items.get(sid)
    if not s:
        raise HTTPException(404, "No such session")
    return public(s, since)


class RenameBody(BaseModel):
    title: str = Field(default="", max_length=400)


@app.patch(p("/api/sessions/{sid}"))
async def rename_session(sid: str, body: RenameBody, request: Request) -> dict[str, Any]:
    """Rename a session. An empty title hands naming back to the agent (on its next turn)."""
    json_only(request)
    s = sessions.items.get(sid)
    if not s:
        raise HTTPException(404, "No such session")
    title = clean_title(body.title)
    if title:
        set_title(s, title, "user")
    else:
        s.update(named="", named_asks=0)
        sessions.save(sid)
    return {"id": sid, "title": s["title"], "named": s["named"]}


def remove_session(sid: str) -> None:
    drop_attachments(sessions.items.pop(sid))
    sessions.save(sid)


@app.delete(p("/api/sessions/{sid}"))
async def delete_session(sid: str) -> dict[str, Any]:
    s = sessions.items.get(sid)
    if not s:
        raise HTTPException(404, "No such session")
    if s["state"] in BUSY:
        raise HTTPException(409, "Stop this session first")
    remove_session(sid)
    return {"deleted": sid}


class DeleteBody(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=500)


@app.post(p("/api/sessions/delete"))
async def delete_sessions(body: DeleteBody, request: Request) -> dict[str, Any]:
    """Delete several sessions; busy ones are skipped (stop them first)."""
    json_only(request)
    deleted, busy = [], []
    for sid in dict.fromkeys(body.ids):
        s = sessions.items.get(sid)
        if not s:
            continue
        if s["state"] in BUSY:
            busy.append(sid)
            continue
        remove_session(sid)
        deleted.append(sid)
    return {"deleted": deleted, "busy": busy}


@app.post(p("/api/sessions/{sid}/message"))
async def message(sid: str, body: MessageBody, request: Request) -> dict[str, Any]:
    json_only(request)
    s = sessions.items.get(sid)
    if not s:
        raise HTTPException(404, "No such session")
    text = body.text.strip()
    if not text and not body.attachments:
        raise HTTPException(422, "Write a message or attach something")
    set_model_choice(s, await check_model_choice(body.model))
    if body.tz:
        s["tz"] = body.tz
    if text.startswith("/") and not body.attachments:
        await command(s, text)
        return public(s)
    attachment_ids = body.attachments + rag_pinned_ids(text, body.attachments)
    attachments = claim_attachments(s, attachment_ids) if attachment_ids else []
    text = text or "(see the attachments)"
    shown = {"attachments": public_attachments(attachments)} if attachments else {}
    if s["state"] == "thinking" and s.get("run_id") and not s.get("pending"):
        steer = text + (("\n(The user attached: " + ", ".join(f"{a['ref']} {a['kind']} \"{a['name']}\"" for a in attachments)
                         + "; see ATTACHMENTS with `$R attachments`)") if attachments else "")
        with suppress(httpx.HTTPError):
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(f"{HERMES_URL}/v1/runs/{s['run_id']}/steer", headers=hermes_headers(), json={"input": steer})
            if response.status_code < 300:
                s["turns"].append({"role": "user", "text": steer, "t": now()})
                ev(s, "user", text, steer=True, **shown)
                return public(s)
    if s["state"] in BUSY and s["id"] not in runners:     # nothing is driving it any more (it crashed): start afresh
        set_state(s, "interrupted", "")
    if s["state"] in BUSY:
        left = unfinished(s)
        local = config.load()["gpu_guard"] and is_local(config.load(), s)
        if s["state"] == "rendering" and left and local and body.on_busy == "ask":
            # Loading the model now would sit next to the renders in VRAM: let the user choose.
            return JSONResponse(status_code=409, content={"detail": {
                "code": "jobs_rendering", "local": True,
                "message": f"The Master Agent's {len(left)} job{'s are' if len(left) > 1 else ' is'} using the GPU. Its model "
                           "can't load to read your message until they finish without running out of VRAM.",
                "jobs": [{"ref": j["ref"], "service": j["service"], "label": j["label"][:140], "state": j["state"],
                          "progress": j.get("progress"), "agentic": j.get("kind") == "agentic"} for j in left]}})
        note = ""
        if body.on_busy == "cancel" and left:
            stopped = []
            for j in left:
                with suppress(studios.StudioError):
                    await cancel_job_of(j)
                    stopped.append(j["ref"])
            s["cancel_for_message"] = True
            note = f"they stopped {', '.join(stopped)} to send this; don't restart those unless they ask"
            ev(s, "warn", f"You stopped {', '.join(stopped) or 'the jobs'} to steer the conversation")
        ev(s, "user", text, queued=True, **shown)
        queue_message(s, text, note=note, attachments=attachments, event=s["next_event"] - 1)
        if body.on_busy == "queue" or not note:
            ev(s, "system", "Your message goes to the Master Agent " + ("as soon as it can read it" if not left or not local
                            else "when its jobs finish"))
        return public(s)
    start(s, text, attachments=attachments)
    return public(s)


@app.post(p("/api/sessions/{sid}/stop"))
async def stop(sid: str) -> dict[str, Any]:
    s = sessions.items.get(sid)
    if not s:
        raise HTTPException(404, "No such session")
    s.update(stop=True, stop_t=now())
    if s.get("cli_run"):
        with suppress(Exception):
            await hostctl("POST", "/clis/stop", {"run": s["cli_run"]})
    if s.get("run_id"):
        with suppress(Exception):
            async with httpx.AsyncClient(timeout=20) as client:
                await client.post(f"{HERMES_URL}/v1/runs/{s['run_id']}/stop", headers=hermes_headers())
    if s.get("pending"):
        s["pending"]["answer"] = "deny" if s["pending"].get("kind") == "command" else False
    task = runners.get(sid)
    if task and s["state"] in ("waiting_for_gpu", "starting"):
        task.cancel()
    ev(s, "warn", "Stop requested (jobs already started keep running; cancel them from their card)")
    return public(s)


@app.post(p("/api/internal/stop-model"))
async def stop_model(request: Request) -> dict[str, Any]:
    """hostctl, when the user stops a local engine (Strata): stop the sessions running on it too,
    or their next request makes llama-swap start the engine again."""
    supplied = request.headers.get("x-hostctl-secret", "")
    if not HOSTCTL_SECRET or not hmac.compare_digest(supplied, HOSTCTL_SECRET):
        raise HTTPException(403, "Forbidden")
    engine = str((await request.json()).get("engine") or "").lower()
    if not engine:
        raise HTTPException(422, "engine is required")
    cfg, stopped = config.load(), []
    for sid, s in list(sessions.items.items()):
        if (s["state"] in BUSY or sid in runners) and not s.get("stop") and engine in run_model(s, cfg)[0].lower():
            ev(s, "warn", f"{engine.capitalize()} was stopped, so this session is stopping too")
            await stop(sid)
            stopped.append(sid)
    return {"stopped": stopped}


@app.post(p("/api/sessions/{sid}/approve"))
async def approve(sid: str, body: ApproveBody, request: Request) -> dict[str, Any]:
    json_only(request)
    s = sessions.items.get(sid)
    if not s or not s.get("pending"):
        raise HTTPException(404, "Nothing is waiting for approval")
    if s["pending"].get("kind") == "command":
        s["pending"]["answer"] = body.choice or ("once" if body.approve else "deny")
    else:
        s["pending"]["answer"] = body.approve
    return public(s)


@app.post(p("/api/sessions/{sid}/review"))
async def review(sid: str, body: ReviewBody, request: Request) -> dict[str, Any]:
    """Approve (optionally edited) or veto a proposal; once none wait, the agent gets the decisions."""
    json_only(request)
    s = sessions.items.get(sid)
    targets = [x for x in review_pending(s or {}) if body.id in ("*", x["id"])]
    if not targets:
        raise HTTPException(404, "Nothing is waiting for your review")
    approve = body.decision == "approve"
    for x in targets:
        if approve and body.id != "*":
            for f in x["fields"]:
                value = body.fields.get(f["key"])
                if value is None or value.strip() == str(x["args"].get(f["key"]) or "").strip():
                    continue
                if f["key"] in REVIEW_JSON and value.strip():
                    try:
                        json.loads(value)
                    except ValueError:
                        raise HTTPException(422, f"{f['label']} must stay valid JSON")
                if not value.strip() and f is x["fields"][0]:
                    raise HTTPException(422, f"{f['label']} can't be empty; veto it instead")
                x["args"][f["key"]] = value.strip()
                x.setdefault("edited", []).append(f["key"])
        x.update(status="approved" if approve else "vetoed", note=body.note.strip(), decided=now())
        if x.get("clear"):
            service = x["clear"]
            if approve:
                clear_service(s, service)
                if body.always and service not in (cfg := config.load())["cleared_services"]:
                    config.save({"cleared_services": cfg["cleared_services"] + [service]})
                x["always"] = body.always
            elif service not in (denied := s.setdefault("denied", [])):
                denied.append(service)
            ev(s, "ok" if approve else "warn", f"{x['id']} {ASK_SERVICES[service]}: "
               + (("allowed for every session" if body.always else "allowed for this session") if approve else "not allowed")
               + (f" ({x['note'][:200]})" if x["note"] else ""), proposal=x["id"])
            continue
        ev(s, "ok" if approve else "warn", f"{x['id']} {x['title']}: {'approved' if approve else 'vetoed'}"
           + (" with your edits" if x.get("edited") else "") + (f" ({x['note'][:200]})" if x["note"] else ""), proposal=x["id"])
    review_deliver(s)
    sessions.save(sid)
    return public(s)


@app.post(p("/api/sessions/{sid}/jobs/{ref}/cancel"))
async def cancel_job(sid: str, ref: str) -> dict[str, Any]:
    s = sessions.items.get(sid)
    j = next((j for j in (s or {}).get("jobs", []) if j["ref"] == ref), None)
    if not j:
        raise HTTPException(404, "No such job")
    try:
        await cancel_job_of(j)
    except studios.StudioError as exc:
        raise HTTPException(502, str(exc)) from exc
    ev(s, "warn", f"You cancelled {ref}")
    return public(s)


@app.post(p("/api/sessions/{sid}/jobs/{ref}/agentic-approval"))
async def agentic_approval(sid: str, ref: str, body: AgenticDecisionBody, request: Request) -> dict[str, Any]:
    json_only(request)
    s = sessions.items.get(sid)
    j = next((j for j in (s or {}).get("jobs", []) if j["ref"] == ref), None)
    if not j or j.get("kind") != "agentic":
        raise HTTPException(404, "No Agentic job found")
    if j.get("state") != "awaiting_approval":
        raise HTTPException(409, "This Agentic job is not waiting for approval")
    try:
        await studios.agentic_stage_decision(j["session_id"], j["job_id"], body.action, body.notes.strip())
    except studios.StudioError as exc:
        raise HTTPException(502, str(exc)) from exc
    j["approval_submitted"] = True
    ev(s, "system", f"H3 staging previews {('accepted' if body.action == 'accept' else 'excluded')}" +
       (f" with note: {body.notes.strip()}" if body.notes.strip() else ""), job=ref)
    sessions.save(sid)
    return public(s)


@app.post(p("/api/internal/queue/proceed/{job_id}"))
async def proceed(job_id: str, request: Request) -> dict[str, Any]:
    if request.headers.get("x-queue-internal-secret") != QUEUE_SECRET or not QUEUE_SECRET:
        raise HTTPException(403, "forbidden")
    body = await request.json()
    turn = gpu_turns.get(job_id)
    if turn is None:          # nobody waits for it any more: hand the slot straight back
        with suppress(Exception):
            await lock_call("/release", {"token": body.get("token")})
        return {"ok": False}
    turn["token"] = body.get("token")
    turn["event"].set()
    return {"ok": True}


@app.post(p("/api/internal/queue/cancel/{job_id}"))
async def cancel_queue_entry(job_id: str, request: Request) -> dict[str, Any]:
    if request.headers.get("x-queue-internal-secret") != QUEUE_SECRET or not QUEUE_SECRET:
        raise HTTPException(403, "forbidden")
    entry_id = str((await request.json()).get("entry_id") or "")
    owner = gpu_entries.get(entry_id)
    if owner is None or owner.key != job_id:
        raise HTTPException(404, "No active Master Agent queue entry")
    if isinstance(owner, GpuTurn):
        await stop(owner.s["id"])
    else:
        task = deferred_tasks.get(f"{owner.s['id']}:{owner.job_ref}")
        if not task or task.done():
            raise HTTPException(404, "No active Master Agent job")
        task.cancel()
        ev(owner.s, "warn", f"You cancelled {owner.job_ref} from the GPU queue")
    return {"ok": True, "entry_id": entry_id}


# ---------------- tools (the master-agent skill's helper script) ----------------

def tool_session(sid: str) -> dict[str, Any]:
    s = sessions.items.get(sid)
    if not s:
        raise HTTPException(404, "Unknown session id; copy it from your brief.")
    return s


def allowed() -> set[str]:
    return {name for name, on in config.load()["services"].items() if on}


async def ask_user(s: dict[str, Any], what: str, kind: str = "job", choices: list[str] | None = None) -> Any:
    """Wait (up to 30 min) for the user's answer on the Agent page. A job approval answers
    True/False; a command approval answers one of its choices ("once", "session", "deny")."""
    s["pending"] = {"what": what, "kind": kind, "choices": choices or [], "answer": None, "t": now()}
    previous = (s["state"], s.get("stage", ""))
    set_state(s, "thinking", "Waiting for your approval")
    ev(s, "approval", f"Approve this job? {what}" if kind == "job" else f"Allow this command? {what}")
    try:
        for _ in range(1800):
            if s["pending"]["answer"] is not None or s.get("stop"):
                break
            await asyncio.sleep(1)
        answer = s["pending"]["answer"]
    finally:
        s["pending"] = None
        set_state(s, *previous)
    if kind == "job":
        answer = bool(answer)
        ev(s, "ok" if answer else "warn", "Approved" if answer else "Declined")
        return answer
    answer = answer if answer in (choices or []) else "deny"
    ev(s, "warn" if answer == "deny" else "ok", {"once": "Allowed once", "session": "Allowed for this turn", "deny": "Denied"}[answer])
    return answer


async def command_approval(s: dict[str, Any], client: httpx.AsyncClient, e: dict[str, Any]) -> None:
    """Hermes paused on a command its approval rules flag as risky (Settings → Agentic decides)."""
    what = str(e.get("command") or e.get("description") or "a command")[:600]
    offered = [c for c in (e.get("choices") or ["once", "session", "deny"]) if c in ("once", "session", "deny")] or ["once", "deny"]
    if "deny" not in offered:
        offered.append("deny")
    mode = config.load()["command_approval"]
    if mode == "allow" and not e.get("smart_denied"):
        choice = "once"
        ev(s, "warn", f"Hermes flagged a command; allowed by your Settings: {what[:200]}")
    elif mode == "deny":
        choice = "deny"
        ev(s, "warn", f"Hermes flagged a command; denied by your Settings: {what[:200]}")
    else:
        choice = await ask_user(s, what, kind="command", choices=offered)
    body = {"choice": choice}
    if e.get("request_id"):
        body["request_id"] = e["request_id"]
    with suppress(Exception):
        await client.post(f"{HERMES_URL}/v1/runs/{s['run_id']}/approval", headers=hermes_headers(), json=body)


# ---------------- review mode (/review) ----------------
# With review on, a studio call doesn't run: it becomes a proposal (p1, p2, ...) the user sees
# on the page with its full prompt, can edit, and approves or vetoes. The agent ends its turn;
# the decisions come back as its next message and it runs an approved one with --approved pN
# (the approved args replace whatever it sends, so the user's edits are what runs).

REVIEW_FIELDS = {
    "plan": [("text", "Plan")],
    "video": [("prompt", "Prompt")],
    "agentic": [("prompt", "Request"), ("guide", "Guide notes")],
    "image": [("prompt", "Prompt")],
    "music": [("style", "Style"), ("lyrics", "Lyrics")],
    "voice": [("text", "Speech")],
    "postedit": [("voiceover", "Voice-over (JSON)"), ("text", "Text overlays (JSON)"), ("settings", "Settings (JSON)")],
    "captions": [],
    "montage": [],
    "edit": [],
    "train": [],
    "motion": [("props", "Props (JSON)")],
}
REVIEW_JSON = {"voiceover", "text", "settings", "props"}
REVIEW_TITLES = {"plan": "Plan", "video": "Video", "agentic": "Video (H3 Agentic)", "image": "Image", "music": "Music",
                 "voice": "Voice", "postedit": "Post-Edit", "captions": "Captions", "montage": "Montage", "edit": "Frame edit", "train": "LoRA training",
                 "motion": "Motion render"}


def review_kinds(name: str, args: dict[str, Any]) -> set[str]:
    if name in ("video", "agentic"):
        return {"video"}
    if name in ("plan", "image", "music", "voice", "train"):
        return {name}
    if name in ("postedit", "captions"):
        return {"edit", "voice"} if args.get("voiceover") else {"edit"}
    if name in ("montage", "edit"):
        return {"edit"}
    if name == "motion" and args.get("action") == "render":
        return {"motion"}
    return set()


def review_covers(cfg: dict[str, Any], kinds: set[str]) -> bool:
    return bool(kinds) and cfg["confirm_jobs"] and (not cfg["review_services"] or bool(kinds & set(cfg["review_services"])))


def review_line(cfg: dict[str, Any]) -> str:
    if not cfg["confirm_jobs"]:
        return "Review mode is off: the Master Agent uses the studios without asking. /review on to check its prompts first."
    covered = cfg["review_services"] or list(REVIEW_KINDS)
    return ("Review mode is on for " + ", ".join(REVIEW_KINDS[k] for k in covered) + ". Each of those waits on this page "
            "with its full prompt until you approve, edit or veto it. /review off turns it off.")


def review_block(cfg: dict[str, Any]) -> str:
    if not cfg["confirm_jobs"]:
        return ""
    covered = cfg["review_services"] or list(REVIEW_KINDS)
    plan = "plan" in covered
    return ("\nREVIEW MODE IS ON: the user checks your work before you use a service. It covers "
            + ", ".join(REVIEW_KINDS[k] for k in covered) + ". A covered command starts nothing: it is shown to the user "
            "as a PROPOSAL (p1, p2, ...) with its full prompt. "
            + ("First run `$R plan \"your plan in a few lines: which studios, in what order, and why\"`. " if plan else
               "Start with `$R plan \"your plan in a few lines\"` so the user sees it. ")
            + "Then propose every job the request needs with its finished prompt (later steps too, with the refs they will use, "
            "e.g. --ref image1), then END YOUR TURN with no further text. The user approves, edits or vetoes each one and the "
            "decisions arrive as your next message. Run an approved job by repeating its command with `--approved pN`: the "
            "version the user approved runs, including their edits. A vetoed job is final: never re-propose it or work around "
            "it with another service. Only propose services the request needs (no song for a plain video, for example) "
            "unless you say why in the plan.\n")


def review_pending(s: dict[str, Any]) -> list[dict[str, Any]]:
    return [x for x in s.get("proposals") or [] if x["status"] == "pending"]


def review_sig(name: str, args: dict[str, Any]) -> str:
    family = "video" if name in ("video", "agentic") else name
    return json.dumps([family, {k: v for k, v in sorted(args.items()) if k != "approved"}], default=str)


def review_gate(s: dict[str, Any], name: str, args: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """None: go ahead (args may now be the user-approved version). A string: the tool's answer."""
    token = str(args.pop("approved", "") or "").strip().lower()
    kinds = review_kinds(name, args)
    if name == "plan":
        text = str(args.get("text") or "").strip()[:4000]
        if not text:
            return "error: give the plan, e.g. plan \"1) image of ... 2) H3 Agentic video from it\""
        if not review_covers(cfg, kinds):
            ev(s, "say", f"Plan: {text}")
            return "Shown to the user."
    elif not review_covers(cfg, kinds) and not token:
        return None
    proposals = s.setdefault("proposals", [])
    if token:
        x = next((x for x in proposals if x["id"] == token), None)
        family = lambda n: "video" if n in ("video", "agentic") else n
        if not x or family(x["tool"]) != family(name):
            return f"error: {token} isn't an approved {name} proposal. Propose it (run the command without --approved)."
        if x["status"] == "approved":
            x.update(status="used", used=now())
            args.clear()
            args.update(x["args"])
            if service := ask_service(name, args):
                clear_service(s, service)
            sessions.save(s["id"])
            return None
        return {"used": f"{token} already ran. Propose it again if another run is needed.",
                "vetoed": f"The user vetoed {token}. Don't start it or a substitute for it.",
                "dropped": f"{token} was never approved (the user moved on). Don't start it.",
                "pending": f"{token} is still waiting for the user. End your turn now."}.get(x["status"], f"error: {token} can't run")
    sig = review_sig(name, args)
    for x in proposals:
        if x.get("sig") == sig and x["status"] == "vetoed":
            return f"The user already vetoed this exact proposal ({x['id']}). Don't start it or a substitute for it."
        if x.get("sig") == sig and x["status"] == "pending":
            return f"That is already proposed as {x['id']} and waiting for the user. End your turn now."
    pid = f"p{max((int(x['id'][1:]) for x in proposals), default=0) + 1}"
    fields = [{"key": k, "label": label} for i, (k, label) in enumerate(REVIEW_FIELDS.get(name, []))
              if i == 0 or str(args.get(k) or "").strip()]
    shown = {f["key"] for f in fields} | {"action"}
    defaults = {"video": "video_model", "agentic": "video_model", "image": "image_model", "music": "music_model", "voice": "voice_model"}
    details = [f"{k.replace('_', '-')} {v}" for k, v in args.items() if k not in shown and v not in (None, "", False)]
    if name in defaults and not args.get("model"):
        details.insert(0, f"model {cfg[defaults[name]]} (your default)")
    title = REVIEW_TITLES.get(name, name) + (f" {args['name']}" if name == "motion" and args.get("name") else "")
    x = {"id": pid, "tool": name, "title": title, "kinds": sorted(kinds), "args": dict(args), "fields": fields,
         "details": " · ".join(details)[:600], "sig": sig, "status": "pending", "note": "", "t": now(), "msg": s.get("msg")}
    proposals.append(x)
    head = str(args.get(fields[0]["key"]) if fields else "") or x["details"]
    ev(s, "review", f"Proposed {pid} · {title}: {head[:300]}", proposal=pid)
    return (f"Proposed as {pid} for the user's review (review mode is on): nothing has started. Propose the rest of your plan the "
            f"same way, then END YOUR TURN with no further text. Their decisions arrive as your next message; run it then with the "
            f"same command plus --approved {pid}.")


def review_message(s: dict[str, Any], decided: list[dict[str, Any]]) -> str:
    lines = []
    for x in decided:
        note = f" Their note: \"{x['note']}\"." if x.get("note") else ""
        if x["tool"] == "plan":
            lines.append(f"- {x['id']} your plan: " + ("APPROVED." if x["status"] == "approved" else
                         "VETOED. Rethink it: propose a different plan (or ask what they want).") + note)
            x["status"] = "used" if x["status"] == "approved" else x["status"]
            continue
        if x.get("clear") and x["status"] == "vetoed":
            lines.append(f"- {x['id']} use {ASK_SERVICES[x['clear']]}: NOT ALLOWED. Don't use it (or a substitute for it) until "
                         "the user's next message; tell them what you would have used it for." + note)
            continue
        if x.get("clear"):
            lines.append(f"- {x['id']} use {ASK_SERVICES[x['clear']]}: ALLOWED"
                         + (" from now on (every session)" if x.get("always") else " for the rest of this session")
                         + f". Run the waiting call with --approved {x['id']}; later {ASK_SERVICES[x['clear']]} calls need no OK." + note)
            continue
        if x["status"] == "vetoed":
            lines.append(f"- {x['id']} {x['title']}: VETOED. Don't start it or a substitute for it unless the user asks." + note)
            continue
        edits = "".join(f" The user edited the {k}; it now reads: {str(x['args'].get(k))[:3000]!r}." for k in x.get("edited") or [])
        lines.append(f"- {x['id']} {x['title']}: APPROVED.{edits}{note} Run it with your {x['tool']} command plus "
                     f"--approved {x['id']} (when it needs earlier results, after they finish).")
    return ("REVIEW DECISIONS from the user:\n" + "\n".join(lines) + "\n\nAn approved job runs as approved (you can't change it with "
            "--approved; anything new or changed is a new proposal). Continue the request.")


def review_deliver(s: dict[str, Any]) -> None:
    """Every proposal is decided: send the decisions to the agent (now, or when its turn ends)."""
    decided = [x for x in s.get("proposals") or [] if x["status"] in ("approved", "vetoed") and not x.get("reported")]
    if not decided or review_pending(s):
        return
    for x in decided:
        x["reported"] = True
    text = review_message(s, decided)
    if s["id"] in runners:
        queue_message(s, text, note="review decisions")
        return
    s["turns"].append({"role": "user", "text": text, "t": now(), "review": True})
    s.update(stop=False, asked="", resumes=0, error="", wait_requested=False)
    ev(s, "system", "Your decisions went to the Master Agent")
    runners[s["id"]] = asyncio.create_task(conversation(s["id"], text))


def ask_service(name: str, args: dict[str, Any]) -> str:
    """The service a tool call uses, for Ask first ("" = needs no OK: looking, planning, helpers)."""
    if name in ("video", "agentic", "montage", "postedit", "captions", "edit"):
        return "Vid"
    if name == "motion":
        return "Motion" if args.get("action") == "render" else ""
    if name == "reamod":
        return "REA" if args.get("action", "status") != "status" else ""
    if name == "service":
        return "Services"
    if name in ("upload", "scout"):
        return "Uploads"
    return {"image": "Krea", "music": "Mus", "voice": "Voice", "train": "Trainer"}.get(name, "")


def service_cleared(s: dict[str, Any], cfg: dict[str, Any], service: str) -> bool:
    return service in cfg["cleared_services"] or service in (s.get("cleared") or [])


def clear_service(s: dict[str, Any], service: str) -> None:
    if service not in (cleared := s.setdefault("cleared", [])):
        cleared.append(service)


def ask_line(s: dict[str, Any], cfg: dict[str, Any]) -> str:
    if not cfg["ask_services"]:
        return "Ask first is off: the Master Agent uses its services without asking. /askfirst on to be asked once per service."
    ok = [ASK_SERVICES[k] for k in ASK_SERVICES if service_cleared(s, cfg, k)]
    return ("Ask first is on: the first time the Master Agent uses a service it waits here for your OK, then that service is "
            "cleared. Cleared: " + (", ".join(ok) if ok else "nothing yet") + ". /askfirst forget drops the clearances.")


def ask_block(s: dict[str, Any], cfg: dict[str, Any]) -> str:
    if not cfg["ask_services"]:
        return ""
    ok = [ASK_SERVICES[k] for k in ASK_SERVICES if service_cleared(s, cfg, k)]
    no = [ASK_SERVICES[k] for k in s.get("denied") or []]
    return ("\nASK FIRST IS ON: the first call to a service the user hasn't cleared starts nothing; it asks the user for their "
            "OK and you END YOUR TURN with no further text. Once they allow it, run that call again with `--approved pN` and use "
            "the service freely afterwards. Ask for every service the request needs in the same turn (one call each). Cleared: "
            + (", ".join(ok) if ok else "none yet") + "." + (f" NOT allowed until the user's next message: {', '.join(no)}." if no else "")
            + "\n")


def ask_gate(s: dict[str, Any], name: str, args: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Ask first: None = go ahead; a string = the tool's answer (the call became a request for the user's OK)."""
    service = ask_service(name, args)
    if not cfg["ask_services"] or not service or service_cleared(s, cfg, service):
        return None
    label = ASK_SERVICES[service]
    if service in (s.get("denied") or []):
        return f"The user didn't allow {label}. Don't use it or a substitute for it until their next message."
    proposals = s.setdefault("proposals", [])
    if waiting := next((x for x in proposals if x.get("clear") == service and x["status"] == "pending"), None):
        return (f"{label} is already waiting for the user's OK ({waiting['id']}). End your turn now; once it is allowed, "
                "make this call again.")
    pid = f"p{max((int(x['id'][1:]) for x in proposals), default=0) + 1}"
    head = next((str(args.get(k)) for k in ("prompt", "style", "text", "id", "name") if str(args.get(k) or "").strip()), "")
    details = " · ".join(f"{k.replace('_', '-')} {v}" for k, v in args.items() if v not in (None, "", False))[:600]
    title = REVIEW_TITLES.get(name, name) + (f" {args['action']}" if name in ("service", "reamod") and args.get("action") else "")
    x = {"id": pid, "tool": name, "title": title, "clear": service, "service_label": label, "kinds": [], "args": dict(args),
         "fields": [], "details": details, "sig": review_sig(name, args), "status": "pending", "note": "", "t": now(),
         "msg": s.get("msg")}
    proposals.append(x)
    ev(s, "review", f"Asks to use {label} ({pid}){': ' + head[:300] if head else ''}", proposal=pid)
    return (f"{label} isn't cleared yet (Ask first is on): nothing has started; the user is asked for their OK as {pid}. Ask for "
            f"any other service the request needs the same way, then END YOUR TURN with no further text. If they allow it, run "
            f"this call again with --approved {pid}.")


def svc(service: str) -> str:
    return studios.SERVICE_NAMES.get(service, service)


REF_PREFIX = {"Krea": "image"}   # image1, image2 (older sessions keep their krea1 refs)


async def submit(s: dict[str, Any], service: str, label: str, call, kind: str = "", model: str = "") -> str:
    cfg = config.load()
    if service not in allowed() and service not in PERMISSION_GATED:
        return f"error: {service} is switched off for the Master Agent in Settings → Agentic."
    if cfg["max_jobs"] and s["jobs_this_message"] >= cfg["max_jobs"]:
        return f"error: job limit reached ({cfg['max_jobs']} per message, Settings → Agentic)."
    try:
        item = await call()
    except studios.StudioError as exc:
        ev(s, "warn", f"{service} refused the job: {exc}")
        return f"error: {service} refused the job: {exc}"
    job_id = str(item.get("id") or item.get("job_id") or "")
    s["jobs_this_message"] += 1
    prefix = REF_PREFIX.get(service, service.lower())
    taken = [int(m.group(1)) for j in s["jobs"] if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", j["ref"]))]
    ref = f"{prefix}{max(taken, default=0) + 1}"
    model = studios.MODEL_LABELS.get(model, model)
    entry = item.get("queue_entry_id")
    j = {"ref": ref, "service": service, "job_id": job_id, "label": label[:300], "state": str(item.get("state") or "queued"),
         "progress": None, "stage": "", "outputs": [], "error": "", "msg": s.get("msg"), "t": now(), "queue_entry_id": entry}
    if kind == "agentic":
        j.update(kind="agentic", session_id=str(item.get("session_id") or job_id))
    if model:
        j["model"] = model
    s["jobs"].append(j)
    if cfg["priority"] and entry:
        with suppress(Exception):
            await lock_call(f"/prioritize/{entry}", {})
    using = f" with {model}" if model else ""
    ev(s, "job", f"Started {ref} in {svc(service)}{' Agentic mode' if kind == 'agentic' else ''}{using}: {label[:200]}", job=ref)
    return (f"started {ref} ({svc(service)} job {job_id[:8]}{', model ' + model if model else ''}), "
            f"{'front of the GPU queue' if cfg['priority'] else 'queued'}.")


# Post-Edit fields the agent sets with --settings; job/reference ids come from refs instead.
POSTEDIT_BLOCKED = {"source_job_id", "music_reference_id", "next_job_id", "next_reference_id", "voices", "editor"}


async def audio_in_vid(s: dict[str, Any], ref: str, role: str) -> dict[str, Any]:
    """An audio attachment or a finished Mus job as a Vid reference (uploaded once per role)."""
    ref = ref.strip().lower()
    a = next((a for a in s.get("attachments") or [] if a["ref"] == ref), None)
    if a:
        if a["kind"] != "audio":
            raise studios.StudioError(f"{ref} is a {a['kind']}, not audio")
        record = a.setdefault("studio", {}).get(f"Vid-{role}") or await studios.upload(
            "Vid", str(pinned_path(a)), a["name"], a["mime"], role)
        a["studio"][f"Vid-{role}"] = record
        return record
    j = finished_job(s, ref)
    if j["service"] != "Mus":
        raise studios.StudioError(f"{ref} is a {j['service']} job, not a song")
    return await job_output_in_vid(s, j, role)


def vid_output_id(s: dict[str, Any], ref: str, index: int | None = None) -> str:
    j = finished_job(s, ref)
    if j["service"] not in ("Vid", "Motion"):
        raise studios.StudioError(f"{ref} is a {j['service']} job; Post-Edit works on videos (vid1, motion1, ...)")
    ids = [m.group(1) for o in j["outputs"] if o["kind"] == "video" and (m := re.search(r"/api/outputs/([0-9a-f]{32})/", o["url"]))]
    if not ids:
        raise studios.StudioError(f"{ref} has no finished video")
    if index is not None and not 1 <= index <= len(ids):
        raise studios.StudioError(f"{ref} has {len(ids)} video(s); --index 1..{len(ids)}")
    return ids[(index or len(ids)) - 1]


def json_arg(args: dict[str, Any], key: str, kind: type) -> Any:
    raw = args.get(key)
    if raw in (None, ""):
        return kind()
    value = raw if isinstance(raw, kind) else json.loads(raw)
    if not isinstance(value, kind):
        raise ValueError(f"--{key} must be a JSON {kind.__name__}")
    return value


async def postedit_tool(s: dict[str, Any], name: str, args: dict[str, Any], num) -> str:
    """Vid Post-Edit on a finished video: trims, speed, look, fades, music, TTS voice-overs, text
    overlays, burned-in captions (Whisper on the GPU), fade into another video. Starts after the turn."""
    ref = str(args.get("ref") or "").strip().lower()
    try:
        settings = json_arg(args, "settings", dict)
        texts = json_arg(args, "text", list)
        voiceovers = json_arg(args, "voiceover", list)
    except ValueError as exc:
        return f"error: {exc}"
    blocked = sorted(set(settings) & POSTEDIT_BLOCKED)
    if blocked:
        return f"error: set {', '.join(blocked)} with --music / --next / --voiceover and refs, not --settings"
    try:
        # Vid takes the crop as 0-1 fractions; pixels (the form scan cut / import-media accept) got a 422 after the turn.
        if isinstance(settings.get("crop"), dict):
            c = settings["crop"]
            box = ":".join(str(c.get(k, d)) for k, d in (("x", 0), ("y", 0), ("w", 1), ("h", 1)))
            frame = {"width": 1, "height": 1}
            if max(float(v) for v in box.split(":")) > 1:
                frame = await probe_video((await board_source(s, ref))[0])
            settings["crop"] = crop_fraction(frame, box)
        body: dict[str, Any] = {"quality": "high", **settings, "source_job_id": vid_output_id(s, ref, num("index", int))}
        if texts:
            body["texts"] = texts
        style = str(args.get("captions") or args.get("style") or ("pop" if name == "captions" else "")).lower()
        if style:
            if style not in ("pop", "classic"):
                return "error: captions style is pop or classic"
            position = str(args.get("position") or settings.get("caption_position") or "bottom")
            color = str(args.get("color") or settings.get("caption_color") or "#ffffff")
            if position not in ("bottom", "middle", "top") or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                return "error: --position bottom|middle|top, --color like #ffffff"
            body.update(captions=True, caption_style=style, caption_position=position, caption_color=color,
                        caption_source="tts" if voiceovers else "whisper",
                        caption_size=max(8, min(80, num("size", int) or settings.get("caption_size") or (30 if style == "pop" else 22))),
                        pop_words=max(1, min(6, num("words", int) or settings.get("pop_words") or 2)))
        if music := str(args.get("music") or "").strip():
            body["music_reference_id"] = (await audio_in_vid(s, music, "postedit-music"))["id"]
            body.setdefault("music_volume", 1.0 if body.get("mute_original") else 0.3)
        if nxt := str(args.get("next") or "").strip():
            body["next_job_id"] = vid_output_id(s, nxt)
        voices = []
        for line in voiceovers[:20]:
            if not isinstance(line, dict) or not str(line.get("text") or "").strip():
                return "error: --voiceover is a JSON list like [{\"text\": \"...\", \"voice\": \"en-US-AriaNeural\", \"start\": 0}]"
            clip = await studios.tts(str(line["text"])[:5000], str(line.get("voice") or "en-US-AriaNeural"),
                                     str(line.get("provider") or "edge"), int(line.get("rate") or 0), int(line.get("pitch") or 0))
            voices.append({"reference_id": clip["id"], "start": float(line.get("start") or 0),
                           "volume": float(line.get("volume") or 1.0), "captions": bool(line.get("captions", True))})
        if voices:
            body["voices"] = voices
    except (studios.StudioError, TypeError, ValueError) as exc:
        return f"error: {exc}"
    whisper = body.get("captions") and body.get("caption_source") == "whisper"
    parts = [k for k in ("trim_start", "trim_end", "crop", "speed", "fade_in", "fade_out", "saturation", "contrast", "remove_music")
             if k in settings] + (["music"] if args.get("music") else []) + ([f"{len(voices)} voice-over(s)"] if voices else []) \
        + ([f"{len(texts)} text(s)"] if texts else []) + ([f"{body['caption_style']} captions"] if body.get("captions") else []) \
        + (["fade into " + str(args.get("next"))] if args.get("next") else [])
    label = f"Post-Edit of {ref}: " + (", ".join(parts) or "re-encode")
    text = await submit(s, "Vid", label, lambda: asyncio.sleep(0, {"id": "", "state": "waiting"}), kind="postedit")
    if not text.startswith("started"):
        return text
    s["jobs"][-1].update(deferred=body, kind="postedit", sid=s["id"], stage="Starts when your turn ends")
    postedit_board(s, ref, settings)
    return (text.split(" (")[0] + f": {label}." + (" Whisper (GPU) transcribes the audio first." if whisper else "")
            + " It starts when your turn ends: run wait, then end your turn. The result is a new video job.")


async def edit_source(s: dict[str, Any], ref: str) -> Path:
    """A local file for `edit`: an attachment (video or audio), a workspace path or a finished job's output."""
    ref = ref.strip()
    a = next((a for a in s.get("attachments") or [] if a["ref"] == ref.lower()), None)
    if a:
        if a["kind"] not in ("video", "audio"):
            raise ValueError(f"{a['ref']} is {a['kind']}, not a video or audio")
        return pinned_path(a)
    if "/" in ref or "." in ref:
        return (await board_source(s, ref))[0]
    j = finished_job(s, ref)
    o = next((o for o in j["outputs"] if o.get("kind") in ("video", "audio") and o.get("url")), None)
    if not o:
        raise ValueError(f"{ref} has no video or audio output")
    local = WORKSPACE_DIR / s["id"] / "frames" / "_sources" / f"{j['ref']}.{'mp4' if o['kind'] == 'video' else 'audio'}"
    if not local.is_file():
        local.parent.mkdir(parents=True, exist_ok=True)
        await studios.fetch_output(o["url"], str(local))
    return local


async def edit_tool(s: dict[str, Any], args: dict[str, Any], cfg: dict[str, Any], num) -> str:
    """Frame-exact edits on the GPU's media engine, run right away: trim by frame, keep every Nth frame,
    speed, crop, fit to another video, loop to a count or another video's length, any clip's soundtrack.
    The result is imported into Vid as a finished video job (plus a muted twin with --muted both)."""
    if "Vid" not in allowed():
        return "error: Vid is switched off for the Master Agent in Settings → Agentic."
    if perm(cfg, "files") == "none":
        return "error: the Files permission is off (Settings → Agentic)"
    ref = str(args.get("ref") or "").strip()
    if not ref:
        return "error: give the video to edit, e.g. edit vid3 --every 3 --fit att1 --length att1 --audio att1"
    try:
        src = await edit_source(s, ref)
        info = await video_edit.probe(src)
        if not info["width"]:
            return f"error: {ref} is not a video"
        frames = times = size = crop = length = None
        fps, notes = num("fps"), []
        if span := str(args.get("frames") or "").replace(" ", ""):
            a, _, b = span.partition("-")
            frames = (int(a), int(b) if b else info["frames"])
            notes.append(f"frames {frames[0]}-{frames[1]}")
        elif args.get("start") or args.get("end"):
            times = (parse_time(args.get("start") or "0"), parse_time(args["end"]) if args.get("end") else None)
            notes.append(f"{times[0]:g}-{times[1] if times[1] is not None else 'end'} s")
        every = max(1, int(num("every", int) or 1))
        speed = num("speed") or 1.0
        if not 0.1 <= speed <= 10:
            return "error: --speed is 0.1-10"
        if every > 1:
            notes.append(f"every {every}{ {2: 'nd', 3: 'rd'}.get(every, 'th') } frame")
        if speed != 1:
            notes.append(f"{speed:g}x speed")
        if box := str(args.get("crop") or ""):
            crop = tuple(int(float(x)) for x in box.split(":"))
            if len(crop) != 4:
                return "error: --crop is x:y:w:h in pixels"
            notes.append(f"crop {box}")
        if fit := str(args.get("fit") or "").strip():
            if m := re.fullmatch(r"(\d+)x(\d+)", fit):
                size = (int(m.group(1)), int(m.group(2)))
            else:
                target = await video_edit.probe(await edit_source(s, fit))
                size, fps = (target["width"], target["height"]), fps or target["fps"]
            if not all(64 <= x <= 4096 for x in size):
                return "error: --fit is WxH (64-4096) or a video ref"
            notes.append(f"fit {size[0]}x{size[1]}")
        if fps and not 1 <= fps <= 120:
            return "error: --fps is 1-120"
        out_fps = fps or info["fps"]
        if span := str(args.get("length") or "").strip():
            if re.fullmatch(r"[\d.]+f", span):
                length = int(span[:-1]) / out_fps
            elif re.fullmatch(r"[\d.:]+", span):
                length = parse_time(span)
            else:
                target = await video_edit.probe(await edit_source(s, span))
                length = target["frames"] / (target["fps"] or out_fps) if target["width"] else target["duration"]
            notes.append(f"length {length:g} s")
        loop = max(1, min(500, int(num("loop", int) or 1)))
        if loop > 1 and not length:
            notes.append(f"looped {loop}x")
        sound = str(args.get("audio") or "").strip()
        timed = every > 1 or speed != 1 or loop > 1 or length
        sound = sound or ("none" if timed else "keep")
        audio = None
        if sound.lower() != "none":
            audio = src if sound.lower() == "keep" else await edit_source(s, sound)
            if not (await video_edit.probe(audio))["audio"]:
                if sound.lower() == "keep":
                    audio = None
                else:
                    return f"error: {sound} has no sound"
        audio_start = parse_time(args.get("audio_start") or "0")
        if sound.lower() == "keep" and audio and not args.get("audio_start"):
            audio_start = frames[0] / info["fps"] if frames else (times[0] if times else 0.0)
        if audio is not None:
            notes.append("sound of " + (ref if sound.lower() == "keep" else sound)
                         + (f" from {audio_start:g} s" if audio_start else ""))
        muted = str(args.get("muted") or "").lower()
        if muted not in ("", "both", "only"):
            return "error: --muted is both or only"
        if muted == "only":
            audio = None
    except (studios.StudioError, ValueError, video_edit.EditError) as exc:
        return f"error: {exc}"
    label = f"Edit of {ref}: " + (", ".join(notes) or "re-encode")
    work = DATA / "tmp" / f"edit-{uuid.uuid4().hex[:12]}"
    work.mkdir(parents=True, exist_ok=True)
    out = work / "edit.mp4"
    try:
        result = await video_edit.render(src, out, work, frames=frames, times=times, every=every, speed=speed, fps=fps,
                                         crop=crop, size=size, loop=loop, length=length, audio=audio, audio_start=audio_start)
        files = [(out, label + (" (muted)" if muted == "only" or audio is None else ""))]
        if muted == "both" and audio is not None:
            await video_edit.muted_copy(out, work / "muted.mp4")
            files.append((work / "muted.mp4", label + " (muted)"))
        lines = []
        for path, name_ in files:
            vid = await studios.vid_import(str(path), name_)
            text = await submit(s, "Vid", name_, lambda: asyncio.sleep(0, {"id": vid.get("id"), "state": "complete"}))
            if not text.startswith("started"):
                return text
            j = s["jobs"][-1]
            j.update(kind="edit", outputs=studios.outputs("Vid", vid), stage="",
                     meta={k: result[k] for k in ("duration", "width", "height", "fps", "frames")})
            ev(s, "job", f"{j['ref']} finished", job=j["ref"])
            lines.append(f"{j['ref']}: {name_}")
        sessions.save(s["id"])
    except (studios.StudioError, video_edit.EditError) as exc:
        return f"error: {exc}"
    finally:
        for f in work.iterdir():
            f.unlink(missing_ok=True)
        work.rmdir()
    return ("\n".join(lines) + f"\nDone on the GPU: {result['width']}x{result['height']}, {result['fps']:g} fps, {result['frames']} frames "
            f"({result['duration']:g} s); one cycle is {result['cycle_frames']} frames, played {result['loops']:g}x, "
            f"{'with' if result['audio'] else 'no'} sound. They are finished video jobs in the chat: fetch one to check it, "
            "or postedit / captions / montage it.")


async def motion_tool(s: dict[str, Any], args: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Rocsy Motion: Remotion (React) projects in the agent's workspace, rendered on the GPU."""
    if "Motion" not in allowed():
        return "error: Motion is switched off for the Master Agent in Settings → Agentic."
    if perm(cfg, "files") in ("none", "read"):
        return "error: Motion needs the Files permission to write your project (Settings → Agentic)"
    action, name = str(args.get("action") or ""), str(args.get("name") or "").strip()
    if action == "list":
        root = WORKSPACE_DIR / s["id"] / "motion"
        names = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
        return "\n".join(f"{n}: {HERMES_WORKSPACE}/{s['id']}/motion/{n}" for n in names) or "No Motion projects yet: motion new NAME"
    if not name:
        return "error: give the project name, e.g. motion new banana-intro"
    project = name if name.startswith(HERMES_WORKSPACE + "/") else f"{HERMES_WORKSPACE}/{s['id']}/motion/{name}"
    try:
        extra = {k: args[k] for k in ("props",) if args.get(k)}
        if action == "new":
            r = await studios.motion("/api/projects", {"session": s["id"], "name": name}, timeout=30)
            return (("The project already exists" if r["existed"] else "Created") + f" at {r['path']} (files: {', '.join(r['files'])}). "
                    f"Read {r['path']}/AGENT.md first, then write src/Root.tsx and your components with your file tools. "
                    f"Then motion check {name}, motion still {name} --frame N to look at frames, and motion render {name}.")
        if action == "check":
            r = await studios.motion("/api/compositions", {"project": project, **extra}, timeout=300)
            return "Compiles. Compositions:\n" + "\n".join(f"- {c['id']}: {c['width']}x{c['height']} @ {c['fps']} fps, {c['frames']} frames "
                                                          f"({c['seconds']} s)" for c in r["compositions"])
        if action == "still":
            r = await studios.motion("/api/still", {"project": project, "composition": args.get("composition") or "Main",
                                                    "frame": int(args.get("frame") or 0), **extra}, timeout=300)
            with suppress(ValueError, OSError):
                a = share_file(s, r["path"], f"{name}-frame{r['frame']}.png")
                ev(s, "say", f"Motion preview · {name} · frame {r['frame']} of {r['of']}", attachments=public_attachments([a]))
            return (f"Frame {r['frame']} of {r['of']} ({r['width']}x{r['height']}, {r['fps']} fps) is at {r['path']} (also shown "
                    "to the user in the chat). Look at it with your vision tool.")
        if action == "assets":
            refs = refs_arg({"ref": args.get("ref")})
            if not refs:
                return "error: give the jobs or attachments to copy in, e.g. motion assets NAME --ref vid3,mus1,image1"
            public = WORKSPACE_DIR / s["id"] / "motion" / name / "public"
            if not public.parent.is_dir():
                return f"error: no project {name}; motion new {name} first"
            public.mkdir(exist_ok=True)
            names = []
            for ref in refs:
                a = next((a for a in s.get("attachments") or [] if a["ref"] == ref.lower()), None)
                if a:
                    import shutil
                    target = public / f"{a['ref']}{Path(a['name']).suffix.lower()}"
                    shutil.copyfile(pinned_path(a), target)
                    names.append(target.name)
                    continue
                j = finished_job(s, ref)
                outs = [o for o in j["outputs"] if o.get("url")]
                for index, o in enumerate(outs):
                    stem = f"{j['ref']}{'-' + str(index + 1) if len(outs) > 1 else ''}"
                    tmp = public / (stem + ".part")
                    mime = await studios.fetch_output(o["url"], str(tmp))
                    ext = {"video/mp4": ".mp4", "video/webm": ".webm", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                           "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac"}.get(
                               mime, {"video": ".mp4", "image": ".png", "audio": ".mp3"}.get(o["kind"], ".bin"))
                    tmp.replace(public / (stem + ext))
                    names.append(stem + ext)
            word_notes = []
            for ref in refs:
                j = own_job(s, ref)
                body_ = (j or {}).get("deferred") or {}
                if j and j.get("kind") == "postedit" and body_.get("captions") and body_.get("caption_source") == "whisper":
                    with suppress(studios.StudioError, KeyError, TypeError):
                        tr = await studios.postedit_transcript(body_["source_job_id"], body_.get("caption_vocals", True))
                        words = [[round(float(a), 3), round(float(b), 3), str(w)] for a, b, w in tr.get("words") or []]
                        module = public.parent / "src" / f"words_{j['ref']}.ts"
                        module.write_text(f"// Whisper word timings of {j['ref']} (seconds on its timeline): [start, end, word]\n"
                                          f"export const words: [number, number, string][] = {json.dumps(words)};\n")
                        word_notes.append(f"- src/words_{j['ref']}.ts: {len(words)} sung/spoken words with timings "
                                          f"(import {{ words }} from \"./words_{j['ref']}\")")
            info = await studios.motion("/api/probe", {"project": project, "files": names}, timeout=120)
            lines = [f"- staticFile(\"{x['name']}\"): {x.get('kind')}" + (f", {x['seconds']} s" if x.get("seconds") else "")
                     + (f", {x['width']}x{x['height']}" if x.get("width") else "") + (f" @ {x['fps']} fps" if x.get("fps") else "")
                     for x in info["files"]]
            return ("Copied into the project's public/ folder:\n" + "\n".join(lines + word_notes) + "\nUse them with <OffthreadVideo src={staticFile(...)} />, "
                    "<Img>, <Audio>. To draw over a whole video, make the composition exactly as long as it "
                    "(durationInFrames = seconds x fps) and the same size, with the video as the bottom layer (see AGENT.md).")
        if action == "render":
            composition = str(args.get("composition") or "Main")
            check = await studios.motion("/api/compositions", {"project": project, **extra}, timeout=300)
            comp = next((c for c in check["compositions"] if c["id"] == composition), None)
            if not comp:
                return f"error: no composition {composition}; the project has " + ", ".join(c["id"] for c in check["compositions"])
            label = str(args.get("label") or f"{name} ({composition})")
            body = {"project": project, "composition": composition, "label": label, **extra}
            text = await submit(s, "Motion", label, lambda: asyncio.sleep(0, {"id": "", "state": "waiting"}), kind="motion")
            if not text.startswith("started"):
                return text
            s["jobs"][-1].update(deferred=body, kind="motion", sid=s["id"], stage="Starts when your turn ends")
            return (text.split(" (")[0] + f": {comp['width']}x{comp['height']} @ {comp['fps']} fps, {comp['seconds']} s. It renders on the "
                    "GPU when your turn ends (run wait, then end your turn) and becomes a video job you can postedit, caption or montage.")
    except studios.StudioError as exc:
        return f"error: {exc}"
    return "error: motion new|assets|check|still|render|list"


# Services the agent may never start/stop: itself, Hermes, its model, the helper, and the user's way in.
AGENT_HANDS_OFF = {"bunny-agent", "hermes-agent", "hermes-dashboard", "llama", "hostctl", "bunny-gateway",
                   "bunny-connections", "bunny-cloudflared", "visual-ssh-desktop", "comfyui-legacy"}


MEDIA_EXT = {".mp4", ".mov", ".webm", ".mkv", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp3", ".wav", ".m4a", ".flac", ".ogg"}


media_sizes: dict[str, dict[str, int]] = {}    # session -> file -> size at the last live check


async def watch_media(s: dict[str, Any], since: float) -> None:
    """While a run is going: post new media (a finished download, a converted clip) as soon as the
    file stops growing, instead of when the turn ends."""
    try:
        while True:
            await asyncio.sleep(3)
            with suppress(Exception):
                post_new_media(s, since, live=True)
    finally:
        media_sizes.pop(s["id"], None)


def post_new_media(s: dict[str, Any], since: float, live: bool = False) -> None:
    """Media the agent made with its own tools this turn (ffmpeg, downloads, scripts) goes into the
    chat by itself, so nothing it made stays hidden in the workspace. Skips its fetched copies,
    Motion project assets and stills (posted when made), and anything already shared.
    live: only files whose size held since the last check and untouched for 3 s (still downloading otherwise)."""
    done = s.setdefault("auto_shared", {})
    fetched = set(s.get("fetched") or [])
    # Stills it pulled out of its own jobs to look at (fvid163.jpg, grid_vid16.jpg, frame5.jpg) are
    # inspection, not results: posting them took att numbers, so real refs ended up as att100+.
    refs = {j["ref"].lower() for j in s.get("jobs") or [] if j.get("ref")}
    def inspection(stem: str) -> bool:
        if re.search(r"frame|grid|contact|sheet|thumb|probe|check|inspect", stem, re.I):
            return True
        for word, digits in re.findall(r"([a-z]+?)(\d+)", stem.lower()):   # fvid163 = vid16, frame 3
            if any(f"{word[i:]}{digits[:n]}" in refs for i in range(len(word)) for n in range(1, len(digits) + 1)):
                return True
        return False
    shared = {a.get("source") for a in s.get("attachments") or [] if a.get("source")}
    found = []
    roots = [(WORKSPACE_DIR, False), (WORKSPACE_DIR / s["id"], True)]
    for root, deep in roots:
        if not root.is_dir():
            continue
        for path in (root.rglob("*") if deep else root.iterdir()):
            rel = path.relative_to(WORKSPACE_DIR).parts
            if (not path.is_file() or path.suffix.lower() not in MEDIA_EXT
                    or any(p in ("public", "out", "node_modules", "frames") or p.startswith(".") for p in rel[:-1])):
                continue
            with suppress(OSError):
                st = path.stat()
                key = str(path)
                if st.st_mtime < since - 2 or st.st_size < 1024 or key in fetched or key in shared or done.get(key) == st.st_mtime:
                    continue
                if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and inspection(path.stem):
                    continue
                if live:
                    seen = media_sizes.setdefault(s["id"], {})
                    steady = seen.get(key) == st.st_size and st.st_mtime < now() - 3
                    seen[key] = st.st_size
                    if not steady or path.name.startswith("tmp") or any(p.startswith("tmp-") for p in rel[:-1]):
                        continue
                found.append((st.st_mtime, path))
    posted = []
    for mtime, path in sorted(found)[-6:]:
        with suppress(ValueError, OSError):
            a = share_file(s, HERMES_WORKSPACE + "/" + str(path.relative_to(WORKSPACE_DIR)))
            s["auto_shared"][str(path)] = mtime
            posted.append(a)
    if posted:
        ev(s, "say", "Here's what I made" + (f" ({len(posted)} files)" if len(posted) > 1 else "") + ":",
           attachments=public_attachments(posted))


# ---------------- website accounts (Connections → Websites) ----------------
# Any site with a login. A scouting run (`$R scout ID`, or the first upload) logs in, works out how to
# do what the account is for, and writes sites/<id>.json (what the site needs) + scripts/<id>_upload.cjs.

GUIDE_SCHEMA = ('{"summary": "one or two lines", "login": {"how": "...", "two_factor": "no/sometimes/always", "captcha": "..."}, '
                '"fields": [{"name": "title", "required": true, "notes": "max 300 chars"}], '
                '"files": {"types": ["mp4", "jpg"], "max_size": "...", "max_length": "...", "aspect": "...", "notes": "..."}, '
                '"limits": "rate limits, account age/karma/verification rules", "steps": ["Open ...", "Click ..."], '
                '"warnings": ["..."]}')


def account_ids() -> list[str]:
    return sorted(p.stem for p in ACCOUNTS_DIR.glob("*.json") if ACCOUNT_ID.fullmatch(p.stem)) if ACCOUNTS_DIR.is_dir() else []


def load_account(account_id: str) -> dict[str, Any] | None:
    if not ACCOUNT_ID.fullmatch(account_id or ""):
        return None
    with suppress(OSError, ValueError):
        data = json.loads((ACCOUNTS_DIR / f"{account_id}.json").read_text())
        if isinstance(data, dict) and data.get("username") and data.get("password"):
            return data
    return None


def site_guide(account_id: str) -> dict[str, Any] | None:
    with suppress(OSError, ValueError):
        data = json.loads((ACCOUNTS_DIR / "sites" / f"{account_id}.json").read_text())
        return data if isinstance(data, dict) else None
    return None


def account_script(account_id: str) -> Path | None:
    return next(iter(sorted((ACCOUNTS_DIR / "scripts").glob(f"{account_id}_upload.*"))), None)


def guide_fields(guide: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [f for f in (guide or {}).get("fields") or [] if isinstance(f, dict) and f.get("name")]


def guide_line(guide: dict[str, Any] | None) -> str:
    if not guide:
        return "not scouted yet"
    fields = ", ".join(f"{f['name']}{'*' if f.get('required') else ''}" for f in guide_fields(guide))
    files = guide.get("files")
    files = "; ".join(f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in files.items() if v) if isinstance(files, dict) else str(files or "")
    return " · ".join(x for x in [str(guide.get("summary") or "")[:300], f"fields {fields}" if fields else "",
                                  f"files {files[:300]}" if files else "", f"limits {str(guide.get('limits'))[:300]}" if guide.get("limits") else ""] if x)


def accounts_text() -> str:
    lines = []
    for account_id in account_ids():
        a = load_account(account_id)
        if not a:
            continue
        script = account_script(account_id)
        lines.append(f"{account_id}: {a.get('name') or account_id} ({a.get('url')}) as {a['username']}"
                     + (f" · for: {a['purpose']}" if a.get("purpose") else "")
                     + f" · {'automated (' + script.name + ')' if script else 'scouted, no script yet' if site_guide(account_id) else 'not scouted'}"
                     + f"\n  {guide_line(site_guide(account_id))}")
    return ("\n".join(lines) + "\n(* = required field)") if lines else (
        "No website accounts are connected. The user adds them in Connections → Websites (/Connections/); never ask for a password in the chat.")


async def account_browser(s: dict[str, Any], account: dict[str, Any], what: str) -> tuple[str, str]:
    """(cdp url or "" for headless, how-to note), opening the visible browser for a headed account."""
    name = account.get("name") or "the site"
    if account.get("browser", "headed") == "headless":
        account_id = account.get("_id", "site")
        return "", (f"The account is set to headless: a script launches its own browser with a persistent profile in "
                    f"{HERMES_ACCOUNTS}/profiles/{account_id} (`chromium.launchPersistentContext(dir, {{ headless: true }})`) so the "
                    "login is kept; your own browser tools don't share that profile.")
    state = await browser_state(fresh=True) or {}
    if not state.get("connected"):
        ev(s, "say", f"🌐 Opening the live browser for {what} on {name}…")
        try:
            state = await hostctl("POST", "/browser/connect", {"url": ""}, timeout=60)
            browser_cache.update(t=now(), data=state)
        except RuntimeError as exc:
            raise studios.StudioError(f"couldn't open the live (headed) browser ({exc}). Tell the user; they can run /browser "
                                      f"connect, or set {name} to Headless in Connections → Websites.") from exc
    cdp = str(state.get("live") or "")
    return cdp, (f"Use the live headed browser (the visible Chromium on the user's desktop, CDP {cdp}): your browser tools drive "
                 "it, a script attaches with `chromium.connectOverCDP(CDP)`, and its profile keeps the login.")


def account_secrets(account_id: str) -> str:
    return (f"The login is in {HERMES_ACCOUNTS}/{account_id}.json (JSON: username, password). Read it ONLY inside a script. Never "
            "print, echo, cat or say the password, and never type it with your browser tools (their arguments are shown in the chat).")


def login_step(account: dict[str, Any], account_id: str, headed: bool) -> str:
    return (f"Log in as {account['username']}: write a short Node script that reads {HERMES_ACCOUNTS}/{account_id}.json, attaches to "
            f"the browser and fills the login form (Playwright: `const {{ chromium }} = require(\"{PLAYWRIGHT}\")`). Skip it if the "
            "page already shows you logged in. If a captcha or a 2FA code appears, `$R notify` the user"
            + (" (they can solve it in the visible window)" if headed else "") + " and `$R ask` them.")


def script_step(account_id: str) -> str:
    return (f"Write {HERMES_ACCOUNTS}/scripts/{account_id}_upload.cjs (CommonJS, Playwright from {PLAYWRIGHT}) that does the whole "
            "post from your notes: args --file PATH, --title/--caption/--tags, --field name=value (repeatable, for the site's own "
            "fields), [--cdp URL] and --dry-run (fill everything, stop before the final submit). It logs in only when needed, "
            "checks the site's requirements first, waits for the post to finish, prints one JSON line {\"ok\": true, \"url\": "
            "\"...\"} and exits non-zero with the reason on failure. Prefer stable selectors (roles, labels, data-testid) over "
            "CSS classes.")


def guide_step(account_id: str) -> str:
    return (f"Write what the site needs to {HERMES_ACCOUNTS}/sites/{account_id}.json, exactly this shape (plain JSON, fill what "
            f"you found, leave out what doesn't apply): {GUIDE_SCHEMA}. `fields` lists EVERY input the post form has, with "
            "required true/false; the user sees this on the Connections page and later uploads are checked against it.")


async def scout_tool(s: dict[str, Any], args: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Work a website out without posting: login, how to do what the account is for, the guide and the script."""
    account_id = str(args.get("site") or "").lower()
    account = load_account(account_id)
    if not account:
        return f"error: no website account {account_id or '(none given)'}.\n{accounts_text()}"
    if not perm(cfg, "web"):
        return "error: the Web and browser permission is off (Settings → Agentic)"
    account["_id"] = account_id
    try:
        cdp, browser_note = await account_browser(s, account, "scouting")
    except studios.StudioError as exc:
        return f"error: {exc}"
    old = site_guide(account_id)
    ev(s, "say", f"Scouting {account.get('name') or account_id}: logging in and working out what it needs (nothing gets posted)")
    return (f"SCOUTING {account.get('name') or account_id} ({account.get('url')}). Don't post, submit, buy, follow, message or "
            f"change anything on the site. {browser_note} {account_secrets(account_id)}\n"
            f"What the account is for: {account.get('purpose') or 'posting the media the user makes (videos and images)'}\n"
            + (f"An earlier guide exists (update it): {guide_line(old)}\n" if old else "")
            + f"1. {login_step(account, account_id, bool(cdp))}\n"
            "2. Find how to do what the account is for: navigate there with your browser tools, look at every page on the way "
            "(screenshots + your vision tool), and open the post/upload form. Note every field and which are required, the "
            "allowed file types, size, length and aspect, character limits, and any rules (account age, karma, verification, "
            "rate limits, community or upload rules, consent dialogs). Read the site's own help pages if the form doesn't say.\n"
            f"3. {guide_step(account_id)}\n"
            f"4. {script_step(account_id)} Test it with --dry-run only if that leaves nothing behind on the site (no drafts, "
            "no uploaded file); otherwise say it is untested.\n"
            "5. `$R say` a short summary of what the site needs, then end with it.")


async def upload_tool(s: dict[str, Any], args: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Post a file to a connected website account. No guide yet = scouting + posting by hand + guide +
    script; a guide but no script = follow it and write the script; a script = run it (repair if needed).
    Required fields from the guide are checked before anything opens."""
    account_id = str(args.get("site") or args.get("platform") or "").lower()
    account = load_account(account_id)
    if not account:
        return f"error: no website account {account_id or '(none given)'}.\n{accounts_text()}"
    if not perm(cfg, "web"):
        return "error: the Web and browser permission is off (Settings → Agentic)"
    account["_id"] = account_id
    label = account.get("name") or account_id
    ref = str(args.get("file") or "").strip()
    a = next((a for a in s.get("attachments") or [] if a["ref"] == ref.lower()), None)
    if a:
        path = a["hermes_path"]
    elif ref.startswith(HERMES_WORKSPACE + "/"):
        if not (WORKSPACE_DIR / ref[len(HERMES_WORKSPACE) + 1:]).is_file():
            return f"error: {ref} doesn't exist"
        path = ref
    else:
        try:
            files = await fetch_job_files(s, finished_job(s, ref))
        except studios.StudioError as exc:
            return f"error: {exc}"
        media = [f for f in files if f[0] in ("video", "image")]
        if not media:
            return f"error: {ref} has no video or image to upload"
        path = media[0][1]
    post = {k: str(args.get(k) or "").strip() for k in ("title", "caption", "tags")}
    raw_fields = args.get("field") or []
    fields = {}
    for item in raw_fields if isinstance(raw_fields, list) else [raw_fields]:
        key, sep, value = str(item).partition("=")
        if not sep or not key.strip():
            return f"error: --field takes name=value (got {item!r})"
        fields[key.strip()] = value.strip()
    guide = site_guide(account_id)
    given = {k.lower() for k, v in {**post, **fields}.items() if v} | {"file", "media", "video", "image"}
    missing = [f for f in guide_fields(guide) if f.get("required") and str(f["name"]).lower() not in given]
    if missing:
        return (f"error: {label} needs these before posting: "
                + "; ".join(f"{f['name']}" + (f" ({f['notes']})" if f.get("notes") else "") for f in missing)
                + ". Pass them (--title/--caption/--tags or --field name=value); ask the user if you can't pick sensible values.")
    try:
        cdp, browser_note = await account_browser(s, account, "the upload")
    except studios.StudioError as exc:
        return f"error: {exc}"
    script = account_script(account_id)
    flags = " ".join([f'--{k} "{v}"' for k, v in post.items() if v] + [f'--field "{k}={v}"' for k, v in fields.items()])
    want = ", ".join([f"{k} \"{v}\"" for k, v in post.items() if v] + [f"{k} \"{v}\"" for k, v in fields.items()]) or "no text fields"
    s.setdefault("uploads", []).append({"site": account_id, "file": path, **post, "fields": fields, "t": now(),
                                        "mode": "script" if script else "guided" if guide else "scouting"})
    sessions.save(s["id"])
    secrets = account_secrets(account_id)
    if script:
        ev(s, "say", f"Uploading to {label} with its saved script ({script.name})")
        return (f"{label} upload (automated run): run the saved script\n"
                f"    node {HERMES_ACCOUNTS}/scripts/{script.name} --file \"{path}\" {flags}" + (f" --cdp {cdp}" if cdp else "") + "\n"
                "It prints a JSON line {\"ok\": true, \"url\": ...} when the post is up. If it fails (the site changed, a new "
                "dialog, logged out), look at the page, finish this upload by hand, then FIX the script and the guide "
                f"({HERMES_ACCOUNTS}/sites/{account_id}.json) so the next run works. {browser_note} {secrets} "
                "When the post is up, `$R say` its link.")
    if guide:
        ev(s, "say", f"Uploading to {label} by its scouted guide, then writing the upload script")
        return (f"{label} upload (guided run: the site is scouted but has no script yet). {browser_note} {secrets}\n"
                f"What it needs: {guide_line(guide)}\nSteps found while scouting: "
                + " → ".join(str(x) for x in (guide.get("steps") or [])[:20]) + "\n"
                f"1. {login_step(account, account_id, bool(cdp))}\n"
                f"2. Post {path} by hand following those steps, with {want}. Fix the guide where the site differs.\n"
                f"3. {script_step(account_id)}\nWhen the post is up, `$R say` its link.")
    ev(s, "say", f"First {label} upload: a scouting run. The agent logs in, works out the site and posts by hand, then writes its guide and script")
    return (f"{label} upload: SCOUTING RUN (the site has never been worked out). {browser_note} {secrets}\n"
            f"1. {login_step(account, account_id, bool(cdp))}\n"
            f"2. Find the post/upload form ({account.get('url')}) and post by hand with your browser tools: {path}, with {want}. "
            "Note every page, field (required or not), file rule, limit and selector on the way. If the site requires something "
            "you weren't given, `$R ask` the user rather than guessing.\n"
            f"3. {guide_step(account_id)}\n4. {script_step(account_id)}\n"
            "Later uploads to this site use the script. When the post is up, `$R say` its link.")


async def fetch_job_files(s: dict[str, Any], j: dict[str, Any], failed=None) -> list[tuple[str, str]]:
    """Copy a finished job's outputs into the session's workspace folder: [(kind, Hermes path)]."""
    folder = WORKSPACE_DIR / s["id"]
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for index, o in enumerate(o for o in j["outputs"] if o.get("url")):
        ext = {"video": ".mp4", "image": ".png", "audio": ".mp3"}.get(o["kind"], ".bin")
        name_ = f"{j['ref']}{'-' + str(index + 1) if len(j['outputs']) > 1 else ''}"
        try:
            mime = await studios.fetch_output(o["url"], str(folder / (name_ + ext)))
        except studios.StudioError as exc:
            if failed:
                failed(exc)
            continue
        real = {"image/jpeg": ".jpg", "image/webp": ".webp", "audio/wav": ".wav", "audio/x-wav": ".wav",
                "audio/flac": ".flac", "video/webm": ".webm"}.get(mime)
        if real and real != ext:
            (folder / (name_ + ext)).rename(folder / (name_ + real))
            ext = real
        s.setdefault("fetched", []).append(str(folder / (name_ + ext)))
        out.append((o["kind"], f"{HERMES_WORKSPACE}/{s['id']}/{name_}{ext}"))
    return out


def post_browser_screenshot(s: dict[str, Any], event: dict[str, Any]) -> None:
    """Show a browser page image from Hermes without trusting a tool-result path."""
    path = str(event.get("screenshot_path") or "")
    prefix = HERMES_WORKSPACE + "/.browser-screenshots/"
    if event.get("tool") not in ("browser_navigate", "browser_click", "browser_back") or not path.startswith(prefix):
        return
    if not re.fullmatch(r"page-[0-9a-f]{32}\.png", path[len(prefix):]):
        return
    try:
        attachment = share_file(s, path)
    except (OSError, ValueError) as exc:
        log.warning("Could not share browser screenshot: %s", exc)
        return
    ev(s, "say", "Browser page screenshot", attachments=public_attachments([attachment]))


def sniff_media(head: bytes) -> tuple[str, str] | None:
    """(mime, extension) from a file's first bytes, so a name can't make an error page look like a picture."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav", ".wav"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif", ".avif"
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return "image/heic", ".heic"
        if brand.startswith(b"M4A"):
            return "audio/mp4", ".m4a"
        return ("video/quicktime", ".mov") if brand == b"qt  " else ("video/mp4", ".mp4")
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return ("video/webm", ".webm") if b"webm" in head[:64] else ("video/x-matroska", ".mkv")
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg", ".mp3"
    if head.startswith(b"fLaC"):
        return "audio/flac", ".flac"
    if head.startswith(b"OggS"):
        return "audio/ogg", ".ogg"
    return None


def textual(head: bytes) -> bool:
    """Looks like text (an HTML/error page), not a binary media file."""
    if not head or b"\0" in head:
        return False
    return sum(32 <= b < 127 or b in b"\t\r\n" for b in head) >= 0.9 * len(head)


def not_media_error(name: str, head: bytes) -> str:
    preview = re.sub(r"\s+", " ", head[:200].decode("utf-8", "replace")).strip()[:80]
    return (f"{name} is not really a picture, video or song: it is text/HTML starting {preview!r}. "
            "The download failed (an error, 404 or bot-check page). Use `$R grab URL`, which checks this for you.")


def image_size(data: bytes, mime: str) -> tuple[int, int] | None:
    """Width/height of PNG, GIF, WebP and JPEG from their headers (no Pillow in this image)."""
    import struct
    with suppress(Exception):
        if mime == "image/png":
            return struct.unpack(">II", data[16:24])
        if mime == "image/gif":
            return struct.unpack("<HH", data[6:10])
        if mime == "image/webp":
            kind = data[12:16]
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if kind == b"VP8X":
                return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
        if mime == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker == 0xFF:
                    i += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def share_file(s: dict[str, Any], path: str, name: str = "") -> dict[str, Any]:
    """Copy a file from the agent's workspace (or an attachment) into this session's attachments.
    The type comes from the file's bytes, not its name: an extension-less --name still makes an
    image, and an HTML error page saved as .png is refused."""
    import mimetypes
    import shutil
    path = path.strip()
    if path and not path.startswith("/"):            # relative paths are the agent's workspace
        path = f"{HERMES_WORKSPACE}/{path.removeprefix('./')}"
    roots = [(HERMES_WORKSPACE, WORKSPACE_DIR), (HERMES_ATTACH_DIR, ATTACH_DIR)]
    local = None
    for hermes_root, here in roots:
        if path == hermes_root or path.startswith(hermes_root + "/"):
            candidate = (here / path[len(hermes_root):].lstrip("/")).resolve()
            if candidate.is_relative_to(here.resolve()):
                local = candidate
            break
    if local is None:
        raise ValueError(f"share files from your workspace {HERMES_WORKSPACE} (copy it there first)")
    if not local.is_file():
        raise ValueError(f"no file at {path}")
    size = local.stat().st_size
    if not 0 < size <= MAX_UPLOAD:
        raise ValueError(f"files can be 1 byte to {MAX_UPLOAD // 1024 // 1024} MB")
    if len(s.get("attachments") or []) >= 200:
        raise ValueError("this session already holds 200 attachments")
    name = safe_name(name or local.name)
    with local.open("rb") as fh:
        head = fh.read(512)
    if sniffed := sniff_media(head):
        mime, ext = sniffed
        suffix = Path(name).suffix.lower()
        if suffix != ext and not (ext == ".jpg" and suffix in (".jpeg", ".jfif")):
            name = (name[: -len(suffix)] if suffix in MEDIA_EXT | {".avif", ".heic", ".jfif"} else name) + ext
    else:
        mime = mimetypes.guess_type(name)[0] or mimetypes.guess_type(local.name)[0] or "application/octet-stream"
        if attachment_kind(mime) != "file" and textual(head):
            raise ValueError(not_media_error(local.name, head))
    aid = uuid.uuid4().hex
    (ATTACH_DIR / aid).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(local, ATTACH_DIR / aid / name)
    meta = {"id": aid, "name": name, "mime": mime, "kind": attachment_kind(mime), "size": size, "total": size, "complete": True,
            "t": now(), "url": p(f"/api/attachments/{aid}"), "hermes_path": f"{HERMES_ATTACH_DIR}/{aid}/{name}",
            "session": None, "by": "agent", "source": str(local)}
    (ATTACH_DIR / aid / "meta.json").write_text(json.dumps(meta))
    return claim_attachments(s, [aid])[0]


GRAB_MAX = 100 * 1024 ** 2
GRAB_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9"}
GRAB_ACCEPT = "image/avif,image/webp,image/png,image/jpeg,image/*,video/*,audio/*;q=0.9,text/html;q=0.8,*/*;q=0.5"


async def grab_public(url: str) -> None:
    """Only public internet hosts: this service sits on the host network next to the studios and hostctl."""
    import ipaddress
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an http(s) URL: {url[:200]}")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or None)
    except OSError:
        raise ValueError(f"can't resolve {parts.hostname}") from None
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise ValueError(f"{parts.hostname} is not a public internet host")


async def grab_get(client: httpx.AsyncClient, url: str, referer: str = "") -> tuple[int, str, bytes]:
    """GET with redirects followed by hand (each hop checked by grab_public); body capped at GRAB_MAX."""
    from urllib.parse import urljoin, urlsplit
    for _ in range(6):
        await grab_public(url)
        origin = "{0.scheme}://{0.netloc}/".format(urlsplit(url))
        headers = {"Accept": GRAB_ACCEPT, "Referer": referer or origin}
        async with client.stream("GET", url, headers=headers) as r:
            if r.is_redirect and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > GRAB_MAX:
                    raise ValueError(f"bigger than {GRAB_MAX // 1024 ** 2} MB")
            return r.status_code, str(r.url), bytes(body)
    raise ValueError("too many redirects")


def page_image_urls(body: bytes, base: str) -> list[str]:
    """The page's own main picture: og:image / twitter:image / image_src."""
    import html
    from urllib.parse import urljoin
    text = body[:500_000].decode("utf-8", "replace")
    found = []
    for tag in re.findall(r"<(?:meta|link)\b[^>]*>", text, re.I):
        key = re.search(r"\b(?:property|name|rel)\s*=\s*[\"']([^\"']+)", tag, re.I)
        val = re.search(r"\b(?:content|href)\s*=\s*[\"']([^\"']+)", tag, re.I)
        if key and val and key.group(1).lower() in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "image_src"):
            found.append(urljoin(base, html.unescape(val.group(1))))
    return found


async def mediawiki_image_urls(client: httpx.AsyncClient, url: str) -> list[str]:
    """Wiki pages (Fandom, Wikipedia, other wikis...) through their API, which works when the page itself
    is behind a bot check: an article's lead image, or a File: page's original."""
    from urllib.parse import unquote, urlsplit
    parts = urlsplit(url)
    m = re.fullmatch(r"/(?:wiki|w)/([^?#]+)", parts.path)
    if not m:
        return []
    title = unquote(m.group(1)).replace("_", " ")
    is_file = re.match(r"(?i)(file|image):", title) is not None
    params = {"action": "query", "titles": title, "format": "json", "redirects": "1"}
    params.update({"prop": "imageinfo", "iiprop": "url"} if is_file else {"prop": "pageimages", "piprop": "original"})
    for api in ("/api.php", "/w/api.php"):
        with suppress(Exception):
            await grab_public(f"{parts.scheme}://{parts.netloc}{api}")
            r = await client.get(f"{parts.scheme}://{parts.netloc}{api}", params=params, headers={"Accept": "application/json"})
            pages = (r.json().get("query") or {}).get("pages") or {}
            urls = [(pg.get("imageinfo") or [{}])[0].get("url") if is_file else (pg.get("original") or {}).get("source")
                    for pg in pages.values()]
            if urls := [u for u in urls if u]:
                return urls
    return []


async def grab_media(url: str) -> tuple[str, bytes, str, str, str]:
    """Download a picture/video/song from a direct URL or from the page that shows it.
    Returns (final_url, bytes, mime, ext, how). Refuses HTTP errors, bot-check and error pages and
    anything that isn't real media, saying why, so the agent stops guessing URLs."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False, headers=GRAB_HEADERS) as client:
        status, final, body = await grab_get(client, url)
        if status < 400 and (sniffed := sniff_media(body[:64])):
            return final, body, *sniffed, ""
        is_page = textual(body[:512])
        challenge = status in (403, 429, 503) and re.search(rb"Just a moment|cf-chl|captcha|Attention Required", body[:20_000], re.I)
        candidates = page_image_urls(body, final) if status < 400 and is_page else []
        candidates += await mediawiki_image_urls(client, final)
        for cand in list(dict.fromkeys(candidates))[:4]:
            with suppress(ValueError, httpx.HTTPError):
                st2, final2, body2 = await grab_get(client, cand, referer=final)
                if st2 < 400 and (sniffed := sniff_media(body2[:64])):
                    return final2, body2, *sniffed, f"the main picture of {final}"
    if challenge:
        raise ValueError(f"HTTP {status}: {final} is behind a bot check (Cloudflare). Try another site, or open it in your browser tool "
                         "and take a direct image URL from it")
    if status >= 400:
        hint = (" - that URL doesn't exist. Never build or guess image URLs: take them from a page, a search result or the "
                "page URL itself (grab accepts pages)") if status in (404, 410) else ""
        raise ValueError(f"HTTP {status} for {final}{hint}")
    if is_page:
        raise ValueError(f"{final} is a web page with no main picture to take (no og:image). Pass a direct image URL, "
                         "or use `$R images \"...\"` and grab a result")
    raise ValueError(f"{final} is not a picture, video or song (it starts {body[:16]!r})")


async def bing_images(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    """Bing Images' first page (no API key): each tile's m="{json}" has the original + page URL, its link the size."""
    import html
    from urllib.parse import parse_qs, urlsplit
    r = await client.get("https://www.bing.com/images/search", params={"q": query, "form": "HDRSC2", "first": "1"})
    r.raise_for_status()
    out = []
    for tag in re.findall(r'<a\b[^>]*\bclass="iusc"[^>]*>', r.text):
        m, href = re.search(r'\bm="([^"]+)"', tag), re.search(r'\bhref="([^"]+)"', tag)
        with suppress(ValueError, AttributeError):
            d = json.loads(html.unescape(m.group(1)))
            q = parse_qs(urlsplit(html.unescape(href.group(1))).query) if href else {}
            if d.get("murl", "").startswith("http"):
                out.append({"url": d["murl"], "page": d.get("purl", ""), "title": d.get("t", ""),
                            "w": int((q.get("expw") or [0])[0]), "h": int((q.get("exph") or [0])[0]),
                            "alt": (q.get("cdnurl") or [""])[0], "from": "bing"})
    return out


async def duckduckgo_images(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    """DuckDuckGo Images (no API key): the search page's vqd token, then its i.js JSON (needs the XHR headers)."""
    r = await client.get("https://duckduckgo.com/", params={"q": query})
    m = re.search(r"vqd=[\"']?([\d-]+)", r.text)
    if not m:
        raise ValueError("DuckDuckGo gave no search token")
    r = await client.get("https://duckduckgo.com/i.js", params={"o": "json", "q": query, "l": "wt-wt", "vqd": m.group(1), "p": "1", "f": ",,,,,"},
                         headers={"Referer": "https://duckduckgo.com/", "Accept": "application/json, text/javascript, */*; q=0.01",
                                  "X-Requested-With": "XMLHttpRequest", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                                  "Sec-Fetch-Site": "same-origin"})
    if r.status_code != 200 or "results" not in r.text[:2000] + r.text[-2000:]:
        raise ValueError(f"DuckDuckGo refused the search (HTTP {r.status_code}, bot check)")
    return [{"url": x["image"], "page": x.get("url", ""), "title": x.get("title", ""), "w": int(x.get("width") or 0),
             "h": int(x.get("height") or 0), "alt": "", "from": "duckduckgo"}
            for x in r.json().get("results", []) if str(x.get("image", "")).startswith("http")]


async def commons_images(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    """Wikimedia Commons file search: free photos (real people, places), as 1600px renders (originals can be huge)."""
    r = await client.get("https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "generator": "search", "gsrsearch": query, "gsrnamespace": "6", "gsrlimit": "8",
        "prop": "imageinfo", "iiprop": "url|size|mime", "iiurlwidth": "1600", "format": "json"})
    pages = sorted(((r.json().get("query") or {}).get("pages") or {}).values(), key=lambda pg: pg.get("index", 0))
    out = []
    for pg in pages:
        ii = (pg.get("imageinfo") or [{}])[0]
        if ii.get("mime") in ("image/jpeg", "image/png", "image/webp"):
            scale = min(1.0, 1600 / max(ii.get("width") or 1, 1))
            out.append({"url": ii.get("thumburl") or ii.get("url"), "page": ii.get("descriptionurl", ""),
                        "title": pg.get("title", "").removeprefix("File:"), "w": round((ii.get("width") or 0) * scale),
                        "h": round((ii.get("height") or 0) * scale), "alt": ii.get("url", ""), "from": "commons"})
    return out


async def image_search(query: str, n: int = 10, min_side: int = 400, engine_id: str = "duckduckgo") -> list[dict[str, Any]]:
    """`$R images` headless: real image search across the web, big enough to be a reference."""
    from urllib.parse import urlsplit
    found: list[dict[str, Any]] = []
    errors = []
    engines = {"duckduckgo": (duckduckgo_images,), "bing": (bing_images,), "commons": (commons_images,),
               "bing+commons": (bing_images, commons_images)}.get(engine_id, (duckduckgo_images,))
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), follow_redirects=True, headers=GRAB_HEADERS) as client:
        for engine in engines:
            try:
                found += await engine(client, query)
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{engine.__name__}: {type(exc).__name__}")
    seen, out = set(), []
    for x in found:
        key = x["url"].split("?")[0]
        if key in seen or (x["w"] and x["h"] and min(x["w"], x["h"]) < min_side):
            continue
        seen.add(key)
        x["site"] = (urlsplit(x["page"] or x["url"]).hostname or "").removeprefix("www.")
        out.append(x)
    if not out and errors:
        raise ValueError("image search failed (" + ", ".join(errors) + ")")
    return out[:n]


async def grab_file(s: dict[str, Any], url: str, name: str = "") -> tuple[dict[str, Any], str]:
    """`$R grab`: download, check, save in workspace/<session>/grabs and share as an attachment."""
    from urllib.parse import unquote, urlsplit
    url, fallback = url.strip(), ""
    if m := re.fullmatch(r"(?i)img(\d+)", url):            # a result of the last `$R images` search
        hits = s.get("image_results") or []
        if not 1 <= int(m.group(1)) <= len(hits):
            raise ValueError(f"no {url} in the last image search ({len(hits)} results); run `$R images \"...\"` first")
        url, fallback = hits[int(m.group(1)) - 1]["url"], hits[int(m.group(1)) - 1].get("alt", "")
    if not re.match(r"(?i)https?://", url):
        raise ValueError("grab takes an http(s) URL or imgN from `$R images`")
    try:
        try:
            final, data, mime, ext, how = await grab_media(url)
        except (ValueError, httpx.HTTPError):
            if not fallback:
                raise
            final, data, mime, ext, how = await grab_media(fallback)   # hotlink-blocked: Bing's cached copy / the original
            how = f"a copy of {url} (the site refused the download)"
    except httpx.HTTPError as exc:
        raise ValueError(f"download failed: {type(exc).__name__}: {exc}"[:300]) from None
    path = urlsplit(final).path.split("/revision/")[0]     # Fandom: .../Name.png/revision/latest
    stem = Path(safe_name(name or unquote(Path(path).name) or "grab")).stem[:80] or "grab"
    folder = WORKSPACE_DIR / s["id"] / "grabs"
    folder.mkdir(parents=True, exist_ok=True)
    dest, n = folder / f"{stem}{ext}", 2
    while dest.exists():
        dest, n = folder / f"{stem}-{n}{ext}", n + 1
    dest.write_bytes(data)
    a = share_file(s, f"{HERMES_WORKSPACE}/{dest.relative_to(WORKSPACE_DIR)}", f"{name}{ext}" if name else dest.name)
    size = image_size(data, mime) if mime.startswith("image/") else None
    facts = [a["kind"], f"{len(data) // 1024} KB"] + ([f"{size[0]}x{size[1]}"] if size else [])
    text = (f"Grabbed {final} as {a['ref']} ({', '.join(facts)})" + (f", {how}" if how else "") + ". "
            f"Path for your tools: {a['hermes_path']}. ")
    if size and min(size) < 256:
        text += "It is SMALL: likely a thumbnail, icon, sprite or placeholder; look for a bigger one if it's for a reference. "
    if a["kind"] == "image":
        text += (f"LOOK at it with your vision tool before you use it: check it really shows the subject (not a placeholder, "
                 f"logo or the wrong person). Then pass it to a studio with --ref {a['ref']}.")
    return a, text



# ---------------- video boards (`$R scan`) ----------------
# A video the agent searches or cuts, drawn in the chat like the Trainer's auto-cut: the windows it
# looked at (looking now / keep with a score / skip), its picks, the planned trims and a crop box.
# Frames come from the GPU's media engine (VA-API) into workspace/<sid>/frames/<board>/, where the
# agent's vision tool reads them too (post_new_media skips "frames" folders).
RENDER_NODE = "/dev/dri/renderD128"
vaapi_frames: bool | None = None       # None = not tried yet; False = no usable media engine here
background_tasks: set[asyncio.Task] = set()   # fire-and-forget board work (kept referenced until done)


def parse_time(text: str) -> float:
    parts = str(text).strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"bad time {text!r}")
    return sum(float(x) * 60 ** i for i, x in enumerate(reversed(parts)))


def parse_spans(text: str, duration: float) -> list[list[float]]:
    """"12.5-15,1:02-1:05" -> [[12.5, 15.0], [62.0, 65.0]], clamped to the video."""
    out = []
    for part in str(text or "").replace(" ", "").split(","):
        if not part:
            continue
        a, sep, b = part.partition("-")
        if not sep:
            raise ValueError(f"give spans as START-END (seconds or m:ss), not {part!r}")
        start, end = max(0.0, parse_time(a)), parse_time(b)
        if duration and start >= duration:
            raise ValueError(f"{part} starts after the video ends ({duration:g}s)")
        end = min(end, duration) if duration else end
        if end <= start:
            raise ValueError(f"{part}: the end must be after the start")
        out.append([round(start, 2), round(end, 2)])
    if not out:
        raise ValueError("give at least one span, e.g. 12.5-15")
    return out


def board_of(s: dict[str, Any], bid: str) -> dict[str, Any]:
    b = next((b for b in s.get("boards") or [] if b["id"] == str(bid).strip().lower()), None)
    if not b:
        have = ", ".join(b["id"] for b in s.get("boards") or []) or "none"
        raise ValueError(f"no board {bid} (boards: {have}); start one with `$R scan start REF \"what to find\"`")
    return b


async def probe_video(path: Path) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
        "-of", "json", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    data = json.loads(out or b"{}")
    stream = (data.get("streams") or [{}])[0]
    duration = float((data.get("format") or {}).get("duration") or 0)
    if not stream.get("width") or duration <= 0:
        raise ValueError(f"{path.name} is not a playable video ({err.decode(errors='replace').strip()[:160] or 'no video stream'})")
    return {"duration": round(duration, 3), "width": int(stream["width"]), "height": int(stream["height"])}


async def video_frame(src: Path, t: float, dest: Path, width: int, height: int) -> None:
    """One frame at t as JPEG, decoded and scaled on the GPU's media engine; the CPU only when this
    machine has no usable one (the same rule as the studios' gpu.py)."""
    global vaapi_frames
    w = min(960, width)
    h = max(2, round(height * w / width / 2) * 2)
    w -= w % 2
    head = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    tail = ["-frames:v", "1", "-q:v", "4", str(dest)]
    if vaapi_frames is not False and os.path.exists(RENDER_NODE):
        cmd = head + ["-hwaccel", "vaapi", "-hwaccel_device", RENDER_NODE, "-hwaccel_output_format", "vaapi",
                      "-ss", f"{t:.3f}", "-i", str(src), "-vf", f"scale_vaapi=w={w}:h={h}:format=nv12,hwdownload,format=nv12"] + tail
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), 60)
        if proc.returncode == 0 and dest.is_file() and dest.stat().st_size:
            vaapi_frames = True
            return
        if vaapi_frames:                 # the media engine works: this file/time is the problem
            raise ValueError(f"no frame at {t:.2f}s: {err.decode(errors='replace').strip()[-200:]}")
        log.warning("VA-API frame decode unavailable, using the CPU: %s", err.decode(errors="replace").strip()[-300:])
        vaapi_frames = False
    proc = await asyncio.create_subprocess_exec(*(head + ["-ss", f"{t:.3f}", "-i", str(src), "-vf", f"scale={w}:{h}"] + tail),
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(proc.communicate(), 60)
    if proc.returncode or not dest.is_file():
        raise ValueError(f"no frame at {t:.2f}s: {err.decode(errors='replace').strip()[-200:]}")


async def board_source(s: dict[str, Any], ref: str) -> tuple[Path, str, str, str]:
    """(local file, page URL, name, ref) of a video: attN, a finished job (vid3, motion1) or a workspace path."""
    ref = ref.strip()
    a = next((a for a in s.get("attachments") or [] if a["ref"] == ref.lower()), None)
    if not a and ("/" in ref or "." in ref):
        a = share_file(s, ref)            # a file in the workspace becomes an attachment the page can play
    if a:
        if a["kind"] != "video":
            raise ValueError(f"{a['ref']} is {a['kind']}, not a video")
        return pinned_path(a), a["url"], a["name"], a["ref"]
    j = finished_job(s, ref)
    o = next((o for o in j["outputs"] if o.get("kind") == "video" and o.get("url")), None)
    if not o:
        raise ValueError(f"{ref} has no video output")
    local = WORKSPACE_DIR / s["id"] / "frames" / "_sources" / f"{j['ref']}.mp4"
    if not local.is_file():
        local.parent.mkdir(parents=True, exist_ok=True)
        await studios.fetch_output(o["url"], str(local))
    return local, o["url"], j.get("label", j["ref"])[:80], j["ref"]


async def new_board(s: dict[str, Any], ref: str, goal: str, mode: str, every: float | None = None) -> dict[str, Any]:
    local, url, name, ref = await board_source(s, ref)
    info = await probe_video(local)
    boards = s.setdefault("boards", [])
    bid = f"scan{max([int(b['id'][4:]) for b in boards] or [0]) + 1}"
    duration = info["duration"]
    step = every or max(2.0, min(10.0, round(duration / 30 * 2) / 2))
    plan, t = [], 0.0
    while t < duration - 0.25 and len(plan) < 80:
        plan.append([round(t, 2), round(min(duration, t + step), 2)])
        t += step
    b = {"id": bid, "ref": ref, "name": name, "url": url, "local": str(local), **info, "goal": goal[:300], "mode": mode,
         "state": "running", "plan": plan if mode == "search" else [], "verdicts": [], "scanning": None, "picks": [],
         "trims": [], "crop": None, "thumbs": {}, "summary": "", "created": now(), "updated": now()}
    boards.append(b)
    icon = "🔎 Searching" if mode == "search" else "✂ Cutting"
    ev(s, "board", f"{icon} {ref} · {name}" + (f": {goal[:200]}" if goal else ""), board=bid)
    return b


def board_touch(s: dict[str, Any], b: dict[str, Any]) -> None:
    b["updated"] = now()
    sessions.save(s["id"])


def public_boards(s: dict[str, Any]) -> list[dict[str, Any]]:
    return [{k: v for k, v in b.items() if k != "local"} for b in s.get("boards") or []]


def crop_fraction(b: dict[str, Any], text: str) -> dict[str, float]:
    """x:y:w:h in pixels (import-media's form) or 0-1 fractions (Post-Edit's) -> fractions of the frame."""
    try:
        x, y, w, h = (float(v) for v in str(text).replace(",", ":").split(":"))
    except ValueError:
        raise ValueError("--crop is x:y:w:h, in pixels or 0-1 fractions of the frame") from None
    if max(x, y, w, h) > 1:
        x, w = x / b["width"], w / b["width"]
        y, h = y / b["height"], h / b["height"]
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > 1.001 or y + h > 1.001:
        raise ValueError("the crop box must lie inside the frame")
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


async def scan_tool(s: dict[str, Any], args: dict[str, Any]) -> str:
    action = str(args.get("action") or "").lower()
    if action == "list":
        return "\n".join(f"{b['id']}: {b['ref']} {b['mode']} {b['state']} · {len(b['verdicts'])}/{len(b['plan'])} windows · "
                         f"{len(b['picks'])} picks · {len(b['trims'])} trims" for b in s.get("boards") or []) or "no boards yet"
    if action == "start":
        mode = "cut" if str(args.get("mode") or "") == "cut" else "search"
        every = float(args["every"]) if args.get("every") not in (None, "") else None
        if every is not None and not 0.5 <= every <= 120:
            raise ValueError("--every is 0.5-120 seconds")
        b = await new_board(s, str(args.get("ref") or ""), str(args.get("goal") or ""), mode, every)
        plan = ", ".join(f"{a:g}-{z:g}" for a, z in b["plan"][:80])
        return (f"Board {b['id']}: {b['ref']} ({b['duration']:.1f}s, {b['width']}x{b['height']}) is on the user's page with its player. "
                + (f"Windows to check: {plan}.\nFor each: `$R scan look {b['id']} START-END` (gives you frames to look at with "
                   f"vision, and shows the user where you are), then `$R scan mark {b['id']} START-END keep --score 1-10 \"why\"` "
                   f"or `... skip \"why\"`. `$R scan pick {b['id']} START-END` the best ones, `$R scan cut {b['id']} "
                   f"START-END[,...] [--crop x:y:w:h]` to show planned trims/crop, `$R scan done {b['id']} \"summary\"` at the end."
                   if mode == "search" else
                   f"Show the planned trims/crop with `$R scan cut {b['id']} START-END[,START-END] [--crop x:y:w:h]` (pixels of "
                   f"the {b['width']}x{b['height']} frame), look at frames with `$R scan look {b['id']} START-END`, and "
                   f"`$R scan done {b['id']}` when the cut is made."))
    b = board_of(s, str(args.get("board") or ""))
    if action == "look":
        (start, end), = parse_spans(str(args.get("span") or ""), b["duration"])[:1]
        n = max(1, min(4, int(args.get("frames") or 3)))
        folder = WORKSPACE_DIR / s["id"] / "frames" / b["id"]
        folder.mkdir(parents=True, exist_ok=True)
        b["scanning"] = [start, end]
        board_touch(s, b)
        paths = []
        for i in range(n):
            t = start + (end - start) * (i + 0.5) / n
            dest = folder / f"t{t:09.3f}.jpg"
            if not dest.is_file():
                await video_frame(Path(b["local"]), t, dest, b["width"], b["height"])
            paths.append((t, dest))
        b["thumbs"][f"{start:.2f}"] = paths[len(paths) // 2][1].name
        board_touch(s, b)
        lines = "\n".join(f"{t:.2f}s: {HERMES_WORKSPACE}/{d.relative_to(WORKSPACE_DIR)}" for t, d in paths)
        return (f"Frames of {b['ref']} {start:g}-{end:g}s (the user sees you looking here):\n{lines}\n"
                f"Look at them with your vision tool (one at a time), then `$R scan mark {b['id']} {start:g}-{end:g} keep|skip ...`.")
    if action == "mark":
        (start, end), = parse_spans(str(args.get("span") or ""), b["duration"])[:1]
        verdict = str(args.get("verdict") or "").lower()
        if verdict not in ("keep", "skip"):
            raise ValueError("mark takes keep or skip")
        score = max(0, min(10, int(float(args.get("score") or (7 if verdict == "keep" else 0)))))
        b["verdicts"] = [v for v in b["verdicts"] if abs(v["start"] - start) > 0.01]
        b["verdicts"].append({"start": start, "end": end, "keep": verdict == "keep", "score": score,
                              "reason": str(args.get("reason") or "")[:300]})
        b["verdicts"].sort(key=lambda v: v["start"])
        if not any(abs(w[0] - start) < 0.01 for w in b["plan"]):
            b["plan"] = sorted(b["plan"] + [[start, end]])
        if b["scanning"] and abs(b["scanning"][0] - start) < 0.01:
            b["scanning"] = None
        board_touch(s, b)
        left = sum(1 for w in b["plan"] if not any(abs(v["start"] - w[0]) < 0.01 for v in b["verdicts"]))
        return f"Marked {start:g}-{end:g}s {verdict}" + (f" {score}/10" if verdict == "keep" else "") + f". {left} window(s) left."
    if action == "pick":
        for start, end in parse_spans(str(args.get("span") or ""), b["duration"]):
            if not any(abs(p[0] - start) < 0.01 for p in b["picks"]):
                b["picks"].append([start, end])
            if not any(abs(w[0] - start) < 0.01 for w in b["plan"]):
                b["plan"] = sorted(b["plan"] + [[start, end]])
        b["picks"].sort()
        board_touch(s, b)
        return f"Picked {len(b['picks'])} span(s) on {b['id']}: " + ", ".join(f"{a:g}-{z:g}" for a, z in b["picks"])
    if action == "cut":
        if args.get("span"):
            b["trims"] = parse_spans(str(args["span"]), b["duration"])
        if args.get("crop"):
            b["crop"] = crop_fraction(b, str(args["crop"]))
        board_touch(s, b)
        crop = b["crop"]
        return (f"Shown on {b['id']}: " + (", ".join(f"trim {i + 1} {a:g}-{z:g}s" for i, (a, z) in enumerate(b["trims"])) or "no trims")
                + (f"; crop x={crop['x']:.3f} y={crop['y']:.3f} w={crop['w']:.3f} h={crop['h']:.3f} of the frame "
                   f"({round(crop['w'] * b['width'])}x{round(crop['h'] * b['height'])} px)" if crop else "")
                + ". Now make the cut (postedit / import-media) with the same numbers.")
    if action == "done":
        b.update(state="done", scanning=None, summary=str(args.get("summary") or "")[:500])
        board_touch(s, b)
        return f"{b['id']} marked done."
    raise ValueError("scan start|look|mark|pick|cut|done|list")


def postedit_board(s: dict[str, Any], ref: str, settings: dict[str, Any]) -> None:
    """A Post-Edit that trims or crops shows the cut on its source video, like `$R scan cut`."""
    if not any(k in settings for k in ("trim_start", "trim_end", "crop")):
        return

    async def show() -> None:
        with suppress(Exception):
            b = await new_board(s, ref, "Post-Edit cut", "cut")
            start = float(settings.get("trim_start") or 0)
            end = float(settings["trim_end"]) if settings.get("trim_end") is not None else b["duration"]
            b["trims"] = [[round(start, 2), round(min(end, b["duration"]), 2)]]
            if isinstance(settings.get("crop"), dict):
                c = settings["crop"]
                b["crop"] = crop_fraction(b, f"{c.get('x', 0)}:{c.get('y', 0)}:{c.get('w', 1)}:{c.get('h', 1)}")
            b["state"] = "done"
            board_touch(s, b)
    task = asyncio.create_task(show())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


@app.get(p("/api/sessions/{sid}/boards/{bid}/frames/{name}"))
async def board_frame(sid: str, bid: str, name: str) -> FileResponse:
    if not (re.fullmatch(r"[0-9a-f]{32}", sid) and re.fullmatch(r"scan\d{1,4}", bid) and re.fullmatch(r"t[\d.]{4,12}\.jpg", name)):
        raise HTTPException(404, "No such frame")
    path = WORKSPACE_DIR / sid / "frames" / bid / name
    if not path.is_file():
        raise HTTPException(404, "No such frame")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.post(p("/api/tool/{sid}/{name}"))
async def tool(sid: str, name: str, request: Request) -> dict[str, Any]:
    s = tool_session(sid)
    args = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    args = args if isinstance(args, dict) else {}
    cfg = config.load()

    def num(key: str, kind=float):
        value = args.get(key)
        if value in (None, ""):
            return None
        try:
            return kind(value)
        except (TypeError, ValueError):
            raise HTTPException(422, f"--{key} must be a number")

    if name == "compress":            # the Hermes rocsy plugin: auto-compression phases
        compress_event(s, args)
        return {"text": "ok"}
    if name == "usage":               # the Hermes rocsy plugin, after every model call
        s["usage"] = {k: args.get(k) for k in ("prompt_tokens", "output_tokens", "cache_read_tokens", "context_length",
                                               "compress_at", "tps", "prefill_tps", "ttfb", "model")} | {"t": now()}
        sessions.save(s["id"])
        return {"text": "ok"}
    if name == "plan" or review_kinds(name, args) or args.get("approved"):
        if (gate := review_gate(s, name, args, cfg)) is not None:
            return {"text": gate}
    if (gate := ask_gate(s, name, args, cfg)) is not None:
        return {"text": gate}
    if name == "status":
        return {"text": json.dumps(await studios.status(allowed()), indent=1)}
    if name == "options":
        out = await studios.options(allowed())
        out["default_video_preset"] = cfg["vid_preset"] or "(Vid defaults)"
        out["default_models"] = {kind: cfg[f"{kind}_model"] for kind in ("video", "voice", "image", "music")}
        return {"text": json.dumps(out, indent=1)}
    if name == "say":
        ev(s, "say", str(args.get("text") or "")[:MAX_EVENT_TEXT])   # was cut at 600 while the agent was told "shown"
        return {"text": "shown to the user"}
    if name == "benchmark":
        ev(s, "benchmark", str(args.get("text") or "")[:600], run=str(args.get("run") or "")[:120])
        return {"text": "benchmark line shown to the user"}
    if name == "title":
        title = clean_title(args.get("title"))
        if not title:
            return {"text": "error: give the name, e.g. title \"Neon rooftop chase\""}
        if s.get("named") == "user":
            return {"text": f"The user named this session \"{s['title']}\" themselves; keep their name."}
        set_title(s, title, "agent")
        return {"text": f"Session named \"{title}\"."}
    if name == "attachments":
        items = s.get("attachments") or []
        return {"text": "\n".join(f"{a['ref']}: {a['kind']} \"{a['name']}\" ({a['size'] // 1024} KB) · file {a['hermes_path']}"
                                  + (" · in Vid as " + a["studio"]["Vid-refs"].get("token", "") if a.get("studio", {}).get("Vid-refs") else "")
                                  for a in items) or "The user hasn't attached anything in this session."}
    if name in ("video", "agentic"):
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return {"text": "error: give the request / prompt"}
        selected_refs = refs_arg(args)
        try:
            ids, prompt = await vid_refs(s, selected_refs, prompt)
            guide = (await vid_refs(s, selected_refs, str(args.get("guide") or "")))[1]
        except studios.StudioError as exc:
            return {"text": f"error: {exc}"}
        model = str(args.get("model") or cfg["video_model"])
        if name == "video" and model in studios.AGENTIC_VIDEO_MODELS and cfg["use_agentic"] and args.get("plain") != "1" and "Vid" in allowed():
            name = "agentic"            # Settings → Agentic: Vid's own Agentic mode handles videos
        if name == "agentic":
            pictures = [f"{r} is {(own_job(s, r) or {}).get('in_vid', {}).get(studios.VID_REF_ROLE, {}).get('token') or r}"
                        for r in selected_refs if (own_job(s, r) or {}).get("service") == "Krea"]
            if pictures and model == "wan_2_2":
                guide = (guide + "\n" if guide else "") + ("The first image the user supplied is the first frame of the "
                         "video (Wan image to video): start the first clip from it.")
            elif pictures:
                guide = (guide + "\n" if guide else "") + ("REFERENCE IMAGES made for this video: " + "; ".join(pictures)
                         + ". For every NEW clip showing a character established by one of these pictures, use Reference mode "
                           "and select that picture again, including after scene changes. Name the character and its picture "
                           "together in subject_definitions. Include the picture in refs= for each such clip.")
            if note := await soundtrack_note(s, selected_refs, prompt + " " + guide):
                guide = (guide + "\n" if guide else "") + note
            resume = None
            if args.get("resume"):
                prior = next((j for j in s["jobs"] if j["ref"] == args["resume"] and j.get("kind") == "agentic"), None)
                if not prior:
                    return {"text": f"error: {args['resume']} isn't one of your Agentic jobs"}
                resume = prior["session_id"]
            mode = str(args.get("mode") or "text")
            text = await submit(s, "Vid", prompt, lambda: studios.agentic_video(
                prompt, guide, num("duration"), args.get("aspect") or None, mode, str(args.get("preset") or ""),
                cfg["vid_preset"], ids, resume, model), kind="agentic", model=model)
            return {"text": text + (" H3 Agentic mode plans the clips itself (how many, how long, continuations, reviews); "
                                    "if its staging approval is on, the user approves the previews in Vid, never you."
                                    if text.startswith("started") else "")}
        return {"text": await submit(s, "Vid", prompt, lambda: studios.video(
            prompt, num("duration"), args.get("aspect") or None, args.get("mode") or "", str(args.get("preset") or ""),
            cfg["vid_preset"], model, ids), model=model)}
    if name == "image":
        prompt = str(args.get("prompt") or "").strip()
        try:
            references = []
            ref_names = refs_arg(args)
            if ref_names == ["all"]:
                # Trial web grabs are candidates, not user-supplied references.
                ref_names = [a["ref"] for a in s.get("attachments") or []
                             if a["kind"] == "image" and "/grabs/" not in str(a.get("source") or "")]
                if not ref_names:
                    return {"text": "error: this session has no supplied images for --ref all; select a downloaded image explicitly with --ref attN"}
            elif "all" in ref_names:
                return {"text": "error: use --ref all alone, or list specific refs such as --ref att1,att2"}
            for index, ref in enumerate(ref_names, 1):
                record = await attachment_in(s, ref, "Krea")
                references.append({"enabled": True, "image": record["image"], "strength": 1.0})
                prompt = re.sub(r"\{\s*" + re.escape(ref) + r"\s*\}", f"reference image {index}", prompt, flags=re.I)
        except studios.StudioError as exc:
            return {"text": f"error: {exc}"}
        image_model = str(args.get("model") or cfg["image_model"])
        text = await submit(s, "Krea", prompt, lambda: studios.image(
            prompt, image_model, num("width", int), num("height", int), num("count", int),
            references, bool(args.get("enhance"))), model=image_model)
        if text.startswith("started"):
            text += f" References: {', '.join(ref_names) if ref_names else 'none'}. Prompt enhancer: {'on' if args.get('enhance') else 'off'}."
        return {"text": text}
    if name == "voice":
        speech = str(args.get("text") or "").strip()
        voice_id = str(args.get("voice") or "").strip()
        if not speech or not voice_id:
            return {"text": "error: give speech text and a saved voice ID (see options)"}
        voice_model = str(args.get("model") or cfg["voice_model"])
        return {"text": await submit(s, "Voice", speech, lambda: studios.voice(
            speech, voice_id, voice_model, str(args.get("language") or "English")), model=voice_model)}
    if name == "music":
        style = str(args.get("style") or "").strip()
        music_model = str(args.get("model") or cfg["music_model"])
        return {"text": await submit(s, "Mus", style, lambda: studios.music(
            style, args.get("lyrics") or None, num("duration"), music_model), model=music_model)}
    if name == "train":
        dataset, lora = str(args.get("dataset") or ""), str(args.get("name") or "")
        return {"text": await submit(s, "Trainer", f"train {lora} on {dataset}", lambda: studios.train(
            dataset, lora, args.get("trigger") or None, args.get("model") or None),
            model=f"LoRA for {args['model']}" if args.get("model") else "")}
    if name == "montage":
        try:
            clip_ids = []
            for ref in refs_arg({"ref": args.get("clips")}):
                j = finished_job(s, ref)
                if j["service"] not in ("Vid", "Motion"):
                    raise studios.StudioError(f"{ref} is a {j['service']} job; montage clips are videos (vid1, motion1, ...)")
                clip_ids += [m.group(1) for o in j["outputs"] if o["kind"] == "video"
                             and (m := re.search(r"/api/outputs/([0-9a-f]{32})/", o["url"]))]
            if not clip_ids:
                return {"text": "error: give the video jobs to join, e.g. montage vid1 (or vid1,vid2)"}
            music_id, music_label = None, ""
            if music := str(args.get("music") or "").strip().lower():
                if any(a["ref"] == music for a in s.get("attachments") or []):
                    a = next(a for a in s["attachments"] if a["ref"] == music)
                    if a["kind"] != "audio":
                        raise studios.StudioError(f"{music} is a {a['kind']}, not audio")
                    record = a.setdefault("studio", {}).get("Vid-music") or await studios.upload(
                        "Vid", str(pinned_path(a)), a["name"], a["mime"], "stitch-music")
                    a["studio"]["Vid-music"] = record
                else:
                    j = finished_job(s, music)
                    if j["service"] != "Mus":
                        raise studios.StudioError(f"{music} is a {j['service']} job, not a song")
                    record = await job_output_in_vid(s, j, "stitch-music")
                music_id, music_label = record["id"], f" + {music}"
        except studios.StudioError as exc:
            return {"text": f"error: {exc}"}
        clip_volume = num("clip_audio")
        label = f"Montage of {args.get('clips')}{music_label}"
        text = await submit(s, "Vid", label, lambda: studios.stitch(
            clip_ids, music_id, min(1.0, max(0.0, num("music_volume") or 1.0)),
            min(1.0, max(0.0, 0.0 if clip_volume is None else clip_volume)), str(args.get("transition") or "cut")))
        return {"text": text + (f" Vid joins {len(clip_ids)} clip(s) in order on the GPU" + (", the song replaces their audio and fades out "
                                "at the end" if music_id else "") + ". Run wait, then end your turn." if text.startswith("started") else "")}
    if name == "jobs":
        await refresh_jobs(s)
        return {"text": "\n".join(job_line(j) for j in s["jobs"]) or "You have not started any jobs yet."}
    if name == "wait":
        await refresh_jobs(s)
        left = unfinished(s)
        if s.get("optimizer_run"):
            s["wait_requested"] = True
            return {"text": "OK. End this turn now. The Master Agent will unload its model, release the GPU, "
                            "show live benchmark output, and resume when optimization finishes. Do not poll or sleep."}
        if not left:
            return {"text": "Nothing is running; all your jobs have finished. Check them with jobs."}
        s["wait_requested"] = True
        return {"text": f"OK. {len(left)} job(s) will render once you end your turn. End it now with one short line for the user; "
                        "you will be resumed with the results when they finish. Do not call wait again."}
    if name == "cancel":
        ref = str(args.get("ref") or "")
        j = next((j for j in s["jobs"] if j["ref"] == ref), None)
        if not j and ":" in ref and cfg["may_cancel_others"]:      # SERVICE:JOB_ID, a job someone else started
            service, _, job_id = ref.partition(":")
            if service not in allowed():
                return {"text": f"error: {service} is switched off for the Master Agent"}
            try:
                await studios.cancel(service, job_id)
            except studios.StudioError as exc:
                return {"text": f"error: {exc}"}
            ev(s, "warn", f"Master Agent cancelled {service} job {job_id[:8]} (not its own)")
            return {"text": f"cancel requested for {service} job {job_id}"}
        if not j:
            return {"text": f"error: no job {ref}; see jobs" + ("" if cfg["may_cancel_others"] else " (you may only cancel your own jobs)")}
        try:
            await cancel_job_of(j)
        except studios.StudioError as exc:
            return {"text": f"error: {exc}"}
        ev(s, "warn", f"Master Agent cancelled {ref}")
        return {"text": f"cancel requested for {ref}"}
    if name == "goal":
        g = s.get("goal")
        if args.get("action") == "done":
            if not g or g["status"] != "active":
                return {"text": "There is no active goal."}
            evidence = str(args.get("evidence") or "")[:1000]
            part = current_part(g)
            if part and g["part"] + 1 < len(g["parts"]):
                part.update(status="done", evidence=evidence)
                g["part"] += 1
                ev(s, "ok", f"Milestone {g['part']} of {len(g['parts'])} done: {part['text'][:160]}" + (f" — {evidence[:200]}" if evidence else ""))
                return {"text": f"Milestone {g['part']} done. Next milestone ({g['part'] + 1} of {len(g['parts'])}): "
                                f"{g['parts'][g['part']]['text']}\nWork on it now, or end the turn at a checkpoint."}
            if part:
                part.update(status="done", evidence=evidence)
            elif g.get("parts") is None:
                return {"text": "error: split the goal into milestones first: `$R goal parts \"milestone 1 || milestone 2 || ...\"`, "
                                "then prove them one by one."}
            g.update(achieved=True, evidence=evidence)
            ev(s, "ok", "The Master Agent marked the goal achieved" + (f": {g['evidence'][:300]}" if g["evidence"] else ""))
            return {"text": "Goal marked achieved. Finish your turn with a short summary for the user."}
        if args.get("action") == "parts":
            if not g or g["status"] != "active":
                return {"text": "There is no active goal."}
            # Strip list markers ("1.", "2)", "-", "*") only: "6v6 teams" keeps its digits.
            texts = [re.sub(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+", "", t).strip() for t in re.split(r"\|\||\n", str(args.get("evidence") or "")) if t.strip()]
            texts = [t[:400] for t in texts if t]
            if not MILESTONES_MIN <= len(texts) <= MILESTONES_MAX:
                return {"text": f"error: give {MILESTONES_MIN}-{MILESTONES_MAX} milestones separated by ||, each one testable outcome."}
            done = [p for p in g.get("parts") or [] if p["status"] == "done"]
            g["parts"] = done + [{"text": t, "status": "todo", "evidence": ""} for t in texts]
            g["part"] = len(done)
            ev(s, "system", f"Goal split into {len(g['parts'])} milestones:\n{parts_overview(g)}")
            return {"text": f"Registered. Current milestone ({g['part'] + 1} of {len(g['parts'])}): {g['parts'][g['part']]['text']}\n"
                            "Work only on it; `$R goal done \"evidence\"` when it is proven."}
        return {"text": goal_line(g)}
    if name == "upload":
        return {"text": await upload_tool(s, args, cfg)}
    if name == "scout":
        return {"text": await scout_tool(s, args, cfg)}
    if name == "accounts":
        return {"text": accounts_text()}
    if name == "guide":
        topic = str(args.get("topic") or "h3")
        if topic not in ("h3",):
            return {"text": "error: guides: h3 (MiniMax H3 prompting)"}
        query = str(args.get("query") or "").strip()
        if not query:
            return {"text": "error: say what you need, e.g. guide h3 \"reference mode retention markers\""}
        found = guide_rag.excerpts(topic, query, max(1, min(8, int(num("k", int) or 4))), budget=12000)
        return {"text": found or "Nothing in the guide matches that; try other words."}
    if name == "fetch":
        refs = refs_arg({"ref": args.get("ref")})
        if not refs:
            return {"text": "error: give job refs or attachments, e.g. fetch vid2,mus1"}
        if perm(cfg, "files") == "none":
            return {"text": "error: the Files permission is off (Settings → Agentic)"}
        lines = []
        for ref in refs:
            a = next((a for a in s.get("attachments") or [] if a["ref"] == ref.lower()), None)
            if a:
                lines.append(f"{ref}: {a['kind']} {a['hermes_path']}")
                continue
            try:
                j = finished_job(s, ref)
            except studios.StudioError as exc:
                lines.append(f"{ref}: error: {exc}")
                continue
            for kind, path in await fetch_job_files(s, j, lambda exc: lines.append(f"{ref}: error: {exc}")):
                lines.append(f"{ref}: {kind} {path}")
        return {"text": "\n".join(lines) + "\n\nThese are copies in your workspace: inspect them freely (ffprobe for length and streams; "
                        "to SEE a video, grab frames with `ffmpeg -v error -ss 5 -i FILE -frames:v 1 /opt/data/rocsy-workspace/"
                        f"{s['id']}/frames/frame5.jpg` (frames/ stays out of the chat) and look at them with your vision tool). For captions on a video use `$R captions REF`: "
                        "it runs Whisper on the GPU; never install or run Whisper yourself."}
    if name == "motion":
        return {"text": await motion_tool(s, args, cfg)}
    if name in ("captions", "postedit"):
        return {"text": await postedit_tool(s, name, args, num)}
    if name == "edit":
        return {"text": await edit_tool(s, args, cfg, num)}
    if name == "ask":
        question = str(args.get("question") or "").strip()[:1500]
        if not question:
            return {"text": "error: give the question"}
        choices = [c.strip()[:120] for c in re.split(r"[;|\n]", str(args.get("choices") or "")) if c.strip()][:6]
        s["asked"] = question
        ev(s, "ask", question, choices=choices)
        return {"text": "Shown to the user" + (f" with {len(choices)} choices" if choices else "") + ". End your turn now with no "
                        "further text; their answer arrives as the next message." + (" Your jobs keep rendering meanwhile." if unfinished(s) else "")}
    if name == "reamod":
        action, mod = str(args.get("action") or "status"), str(args.get("mod") or "")
        if not re.fullmatch(r"[0-9a-f]{12}", mod):
            return {"text": "error: give the REA mod id (12 hex characters, from GET http://127.0.0.1:8976/REA/api/mods)"}
        if action == "status":
            return {"text": json.dumps(await hostctl("GET", f"/rea/mods/{mod}/status"), indent=1)}
        if action == "test":   # REA's own sandbox, not the user's game: no permission needed
            timeout = max(15, min(600, int(num("seconds", int) or 60)))
            why = str(args.get("why") or "").strip()[:600]
            label = f"test run of REA mod {mod} ({timeout} s)"
            text = await submit(s, "REA", label, lambda: asyncio.sleep(0, {"id": "", "state": "waiting"}), kind="reatest")
            if s["jobs"] and s["jobs"][-1]["label"] == label and not s["jobs"][-1].get("deferred"):
                s["jobs"][-1].update(deferred={"mod": mod, "timeout": timeout, "why": why}, kind="reatest", sid=s["id"], stage="Starts when your turn ends")
                sessions.save(s["id"])
            return {"text": text + "\nThe test run (build, syntax check, the modded game on REA's virtual display) starts when your turn "
                                   "ends: run `$R wait`, then END YOUR TURN. You are resumed with its verdict, new errors, log paths and 3 "
                                   "screenshots (15 s in, half way, just before the end; also shown to the user in chat)."}
        if not perm(cfg, "rea_mods"):
            return {"text": "error: the 'REA mods on the real game' permission is off (Settings → Agentic → Permissions): "
                            "the user installs from the REA page"}
        if action in ("install", "uninstall"):
            r = await hostctl("POST", f"/rea/mods/{mod}/{action}", {}, timeout=300)
            if not r.get("error"):
                ev(s, "say", f"REA mod {mod}: {action}ed" + (f" ({r['message']})" if r.get("message") else ""))
            return {"text": json.dumps(r, indent=1)[:4000]}
        if action != "play":
            return {"text": "error: action is status, test, install, uninstall or play"}
        seconds = max(15, min(600, int(num("seconds", int) or 90)))
        user_files = args.get("user_files") or []
        if isinstance(user_files, str):
            user_files = [user_files]
        if not isinstance(user_files, list) or len(user_files) > 4 or not all(re.fullmatch(r"[\w.-]{1,64}", str(n)) and not str(n).startswith(".") for n in user_files):
            return {"text": "error: --user-file takes up to 4 plain file names (the mod's user:// autostart flag)"}
        body = {"mod": mod, "seconds": seconds, "screenshots": 3,  # 15 s in, half way, just before closing (shown in chat)
                "uninstall": bool(args.get("uninstall")), "install": args.get("install") is not False, "user_files": [str(n) for n in user_files]}
        label = f"play test of REA mod {mod} ({seconds} s)"
        text = await submit(s, "REA", label, lambda: asyncio.sleep(0, {"id": "", "state": "waiting"}), kind="gametest")
        if s["jobs"] and s["jobs"][-1]["label"] == label and not s["jobs"][-1].get("deferred"):
            s["jobs"][-1].update(deferred=body, kind="gametest", sid=s["id"], stage="Starts when your turn ends")
            sessions.save(s["id"])
        return {"text": text + "\nThe clean and modded game runs start when your turn ends (you hold the GPU while you think): run `$R wait`, then END "
                               "YOUR TURN. You are resumed with new errors vs baseline, shared errors, log paths and 3 screenshots (also shown to the user in chat)."}
    if name == "share":
        if not perm(cfg, "files") or perm(cfg, "files") == "none":
            return {"text": "error: the Files permission is off (Settings → Agentic)"}
        try:
            a = share_file(s, str(args.get("path") or ""), str(args.get("name") or ""))
        except ValueError as exc:
            return {"text": f"error: {exc}"}
        ev(s, "say", f"Shared {a['name']}" + (f": {str(args.get('caption'))[:300]}" if args.get("caption") else ""),
           attachments=public_attachments([a]))
        return {"text": f"Shared as {a['ref']} ({a['kind']}, {a['size'] // 1024} KB). The user sees it on the page; pass it to a "
                        f"studio with --ref {a['ref']}."}
    if name == "scan":
        if perm(cfg, "files") == "none":
            return {"text": "error: the Files permission is off (Settings → Agentic)"}
        try:
            return {"text": await scan_tool(s, args)}
        except (ValueError, studios.StudioError, asyncio.TimeoutError) as exc:
            return {"text": f"error: {exc}"}
    if name == "import_media":
        # A link the user supplied is not a YouTube search. This works even if web
        # search/browser access is disabled in Agentic settings.
        if perm(cfg, "files") == "none":
            return {"text": "error: Files access is off (Settings → Agentic)"}
        try:
            path = await asyncio.to_thread(
                media_import.import_clip, str(args.get("url") or ""), WORKSPACE_DIR, s["id"],
                output_format=str(args.get("format") or "mp4").lower(), start=args.get("start"),
                end=args.get("end"), crop=str(args.get("crop") or ""), name=str(args.get("name") or ""))
            a = share_file(s, f"{HERMES_WORKSPACE}/{path.relative_to(WORKSPACE_DIR)}")
        except (media_import.MediaImportError, ValueError) as exc:
            return {"text": f"error: {exc}"}
        ev(s, "say", f"Imported {a['name']} from the link", attachments=public_attachments([a]))
        return {"text": f"Imported as {a['ref']} ({a['kind']}, {a['size'] // 1024} KB). "
                        f"The file is in your workspace at {a['hermes_path']}; use --ref {a['ref']} only if needed."}
    if name == "grab":
        if not perm(cfg, "web"):
            return {"text": "error: the Web permission is off (Settings → Agentic)"}
        if perm(cfg, "files") == "none":
            return {"text": "error: the Files permission is off (Settings → Agentic)"}
        try:
            a, text = await grab_file(s, str(args.get("url") or ""), str(args.get("name") or ""))
        except ValueError as exc:
            return {"text": f"error: {exc}"}
        ev(s, "say", f"Grabbed {a['name']}" + (f": {str(args.get('caption'))[:300]}" if args.get("caption") else ""),
           attachments=public_attachments([a]))
        return {"text": text}
    if name == "images":
        if not perm(cfg, "web"):
            return {"text": "error: the Web permission is off (Settings → Agentic)"}
        query = str(args.get("query") or "").strip()[:300]
        if not query:
            return {"text": "error: give a search query"}
        min_side = int(num("min", int) or 400)
        engine = IMAGE_ENGINES[cfg["image_engine"]]
        if cfg["image_mode"] == "browser":
            from urllib.parse import quote_plus
            state = await browser_state(fresh=True) or {}
            if not state.get("connected"):
                ev(s, "say", "🌐 Opening the live browser for the image search…")
                try:
                    state = await hostctl("POST", "/browser/connect", {"url": ""}, timeout=60)
                    browser_cache.update(t=now(), data=state)
                except RuntimeError as exc:
                    return {"text": f"error: couldn't connect the live browser for the image search ({exc}). Tell the user; "
                                    "they can run /browser connect or switch Image search to headless in Settings → Agentic."}
            url = engine["url"].format(q=quote_plus(query))
            return {"text": f"Image search is set to the live browser ({engine['label']}): {browser_line(state)}\n"
                            f"Open {url} with your browser tools (browser_navigate), look at the results, click a big clean one "
                            "of the subject alone (official art or real photos over fan art unless asked) and take its ORIGINAL "
                            f"image URL (the full-size view's 'Visit'/'View image' link, at least {min_side}px), then "
                            "`$R grab URL --name NAME` and look at it with your vision tool. Never guess image URLs."}
        try:
            hits = await image_search(query, max(1, min(30, int(num("n", int) or 10))), min_side, cfg["image_engine"])
        except ValueError as exc:
            return {"text": f"error: {exc}"}
        s["image_results"] = hits
        sessions.save(s["id"])
        if not hits:
            return {"text": f"No pictures of at least {min_side}px for \"{query}\". Try other words (drop 'wiki'; add "
                            "'official art', 'portrait', 'photo', 'render' or 'screenshot') or a lower --min."}
        lines = [f"img{i}: {x['w']}x{x['h']} · {x['site']} · {x['title'][:90]}\n      {x['url'][:200]}"
                 for i, x in enumerate(hits, 1)]
        return {"text": f"Image results for \"{query}\" from {engine['label']} (sizes are the originals):\n" + "\n".join(lines) +
                        "\nPick a big, clean one showing the subject alone (official art or real photos over fan art unless "
                        "asked), then `$R grab imgN --name NAME` and look at it with your vision tool."}
    if name == "notify":
        if not perm(cfg, "notify_user"):
            return {"text": "error: push notifications are off for you (Settings → Agentic → Permissions)"}
        title = str(args.get("title") or "").strip()[:120]
        if not title:
            return {"text": "error: give a title"}
        if not QUEUE_SECRET:
            return {"text": "error: notifications aren't configured for the Agent service"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(NOTIFY_URL, headers={"x-internal-secret": QUEUE_SECRET},
                                      json={"service": "Agent", "event": "message", "title": title,
                                            "body": str(args.get("body") or "")[:400], "url": f"/Agent/?s={s['id']}",
                                            "tag": f"agent-{s['id']}-msg"})
            sent = r.json().get("sent") if r.status_code < 300 else None
        except (httpx.HTTPError, ValueError):
            sent = None
        ev(s, "say", f"🔔 {title}")
        return {"text": "notification sent" if sent else "notification queued (no device may be subscribed to Agent messages)"}
    if name == "services":
        try:
            items = (await hostctl("GET", "/items", timeout=30)).get("items", [])
        except RuntimeError as exc:
            return {"text": f"error: {exc}"}
        return {"text": "\n".join(f"{i['id']}: {i['name']} · {i.get('state') or ('running' if i.get('running') else 'stopped')}"
                                  + ("" if i["id"] not in AGENT_HANDS_OFF else " (not yours to control)") for i in items)
                        + ("" if perm(cfg, "services") else "\n(You may look but not control them: the Services permission is off.)")}
    if name == "llama-bench":
        if not perm(cfg, "llama_optimize"):
            return {"text": "error: Llama benchmarking is disabled in Settings → Agentic"}
        action = str(args.get("action") or "")
        model = str(args.get("model") or "")
        try:
            if action == "models":
                reply = await hostctl("GET", "/llama-optimize/models", timeout=30)
            elif action == "status" and model:
                reply = {"install": await hostctl("GET", f"/llama-benchmark/install/status/{model}", timeout=30),
                         "benchmark": await hostctl("GET", f"/llama-benchmark/status/{model}", timeout=30)}
            elif action == "start" and model:
                installed = await hostctl("GET", f"/llama-benchmark/install/status/{model}", timeout=30)
                if not installed.get("installed"):
                    return {"text": "error: install llama-bench for this model in llama-swap → Benchmarking first"}
                reply = await hostctl("POST", "/llama-benchmark/start", {"model": model}, timeout=30)
            else:
                return {"text": "error: choose models, status MODEL, or start MODEL"}
        except RuntimeError as exc:
            return {"text": f"error: {exc}"}
        return {"text": json.dumps(reply, indent=2)}
    if name == "llama-optimize":
        action = str(args.get("action") or "")
        if not perm(cfg, "llama_optimize"):
            return {"text": "error: Llama optimization is disabled in Settings → Agentic"}
        if action == "models":
            return {"text": json.dumps(await hostctl("GET", "/llama-optimize/models", timeout=30), indent=2)}
        if action == "status":
            return {"text": json.dumps(await hostctl("GET", "/llama-optimize/status", timeout=30), indent=2)}
        if action == "start":
            if not perm(cfg, "web"):
                return {"text": "error: Web permission is off; research is required before optimization"}
            model = str(args.get("model") or "")
            sources = args.get("sources") or []
            trials = args.get("trials") or []
            if not isinstance(sources, list):
                return {"text": "error: provide research source URLs"}
            try:
                reply = await hostctl("POST", "/llama-optimize/start",
                                      {"model": model, "session": sid, "sources": sources, "trials": trials}, timeout=30)
            except RuntimeError as exc:
                return {"text": f"error: {exc}"}
            s["optimizer_run"] = reply["id"]
            s["wait_requested"] = True
            if s.get("run_id"):
                asyncio.create_task(yield_for_optimizer(s, s["run_id"], reply["id"]))
            ev(s, "system", f"Started llama.cpp optimization for {model}; baseline is saved before benchmarking")
            return {"text": json.dumps(reply, indent=2) + "\nRun `$R wait` and END YOUR TURN now. "
                    "Do not use terminal sleep or poll status. The model unloads and the benchmark gets the GPU only after your turn ends."}
        return {"text": "error: choose models, start, or status"}
    if name == "service":
        item, action = str(args.get("id") or "").strip(), str(args.get("action") or "").strip()
        if not perm(cfg, "services"):
            return {"text": "error: the Services permission is off (Settings → Agentic); tell the user what you would restart and why"}
        if action not in ("start", "stop", "restart"):
            return {"text": "error: action is start, stop or restart"}
        if item in AGENT_HANDS_OFF:
            return {"text": f"error: {item} runs you, your model or the user's access; you can't {action} it"}
        try:
            reply = await hostctl("POST", f"/items/{item}/{action}", {}, timeout=180)
        except RuntimeError as exc:
            return {"text": f"error: {exc}"}
        ev(s, "warn", f"Master Agent: {action} {item} · {reply.get('message', '')[:200]}")
        return {"text": reply.get("message") or "done"}
    if name == "permissions":
        return {"text": permissions_block(cfg) + f"Command approvals: {cfg['command_approval']}. Studios: "
                        + ", ".join(sorted(allowed())) + f". GPU jobs per message: {cfg['max_jobs'] or 'no limit'}."}
    if name == "queue":
        entries = await studios.queue()
        return {"text": "\n".join(f"{e.get('position') or 'GPU'} · {e.get('app')} · {e.get('state')} · {str(e.get('label') or '')[:80]}"
                                  for e in entries) or "The GPU queue is empty."}
    raise HTTPException(404, f"Unknown tool {name}")
