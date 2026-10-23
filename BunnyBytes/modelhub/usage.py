"""When each installed model file was last used.

The models drive is mounted noatime, so file access times say nothing. Instead:
- every ComfyUI output keeps the job graph it came from (PNG "prompt" text chunk,
  MP4/FLAC "prompt" metadata tag): any model/LoRA file named in it was used at
  the output's time (backfill + new outputs);
- ComfyUI's job history, checked every couple of minutes (covers jobs whose
  output went elsewhere, and failed runs that still loaded the models);
- llama-swap's loaded model, checked every minute (LLMs have no backfill:
  tracking starts when this first runs).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from pathlib import Path

import httpx

import integrations

COMFY = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
OUTPUT = Path(os.getenv("MODELHUB_COMFY_OUTPUT", "/comfy-output"))
MEDIA = (".png", ".mp4", ".webm", ".webp", ".flac", ".wav", ".mp3", ".mov", ".gif")
# Any quoted string that names a model file (optionally with JSON-escaped quotes).
FILE_NAME = re.compile(rb'\\?"([^"\\\n]{1,200}\.(?:safetensors|gguf|ckpt|pt|pth|sft|bin))\\?"', re.I)
HEAD = TAIL = 3 * 1024 * 1024
BACKFILL_DAYS = 60


def names_in_file(path: Path) -> set[str]:
    """Model file names mentioned in an output's embedded job graph."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size <= HEAD + TAIL:
                data = handle.read()
            else:  # the metadata sits at the start (PNG, FLAC, faststart MP4) or the end (MP4 moov)
                data = handle.read(HEAD)
                handle.seek(size - TAIL)
                data += handle.read(TAIL)
    except OSError:
        return set()
    if b"class_type" not in data:
        return set()
    return {m.decode("utf-8", "replace") for m in FILE_NAME.findall(data)}


def names_in_graph(graph) -> set[str]:
    return {m.decode("utf-8", "replace") for m in FILE_NAME.findall(json.dumps(graph).encode())}


KREA = os.getenv("MODELHUB_KREA_URL", "http://127.0.0.1:8300/Krea")
# Krea saves images without the job graph, so past Krea jobs are read from its job
# list. Model -> files from Krea's backend (models.py KREA_UNET_NAMES / ANIMA_VERSIONS,
# qwen.py, the Krea template). Only for backfill: new jobs come from ComfyUI's history.
KREA_FILES = {
    "krea2": ["krea2_turbo_int8_convrot.safetensors", "qwen3vl_4b_fp8_scaled.safetensors", "qwen_image_vae.safetensors"],
    "lustify": ["lustifyNSFWCheckpoint_v10Krea2_fp8.safetensors", "qwen3vl_4b_fp8_scaled.safetensors", "qwen_image_vae.safetensors"],
    "qwen_image_21": ["qwen_image_2.1_int8_convrot.safetensors", "qwen3vl_8b_int8_convrot.safetensors",
                      "qwen_image_2.1_vae_bf16.safetensors"],
}
ANIMA_FILES = {"base": "anima_baseV10.safetensors", "aesthetic": "anima_aestheticV11.safetensors",
               "turbo": "anima_turboV10.safetensors", "preview_29": "Anima-2.9B-preview-v1.safetensors",
               "v38": "Anima-3.8B-v1.1.safetensors"}


def _iso(value: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


class Usage:
    def __init__(self, data_dir: Path, library):
        self.path = data_dir / "usage.json"
        self.library = library
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        self.last: dict[str, float] = data.get("last", {})       # models/ relative path -> unix time
        self.scanned: dict[str, float] = data.get("scanned", {})  # output file -> mtime already read
        self.seen_prompts: list[str] = data.get("seen_prompts", [])
        self.since: dict[str, float] = data.get("since", {})      # "comfy" / "llama" -> tracking start
        self.busy = False

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"last": self.last, "scanned": self.scanned, "since": self.since,
                                   "seen_prompts": self.seen_prompts[-3000:]}))
        os.replace(tmp, self.path)

    def _index(self) -> tuple[dict[str, str], dict[str, list[str]]]:
        """How graphs name files: relative to their folder (loras/X/y.safetensors -> X/y.safetensors) or bare."""
        by_rel, by_base = {}, {}
        for item in self.library.items:
            rel = item["path"].split("/", 1)[1] if "/" in item["path"] else item["path"]
            by_rel[rel.casefold()] = item["path"]
            by_base.setdefault(item["name"].casefold(), []).append(item["path"])
        return by_rel, by_base

    def mark(self, names: set[str], when: float) -> int:
        by_rel, by_base = self._index()
        hits = 0
        for name in names:
            key = name.replace("\\", "/").casefold()
            paths = [by_rel[key]] if key in by_rel else by_base.get(key.rsplit("/", 1)[-1], [])
            for path in paths:
                if when > self.last.get(path, 0):
                    self.last[path] = when
                hits += 1
        return hits

    def backfill_outputs(self) -> int:
        """Read job graphs from ComfyUI outputs not read yet (newest BACKFILL_DAYS)."""
        if not OUTPUT.is_dir():
            return 0
        cutoff = time.time() - BACKFILL_DAYS * 86400
        found = 0
        for dirpath, dirnames, filenames in os.walk(OUTPUT):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if not name.lower().endswith(MEDIA):
                    continue
                path = Path(dirpath) / name
                with contextlib.suppress(OSError):
                    mtime = path.stat().st_mtime
                    key = str(path.relative_to(OUTPUT))
                    if mtime < cutoff or self.scanned.get(key) == mtime:
                        continue
                    found += self.mark(names_in_file(path), mtime)
                    self.scanned[key] = mtime
        self.since.setdefault("comfy", time.time() - BACKFILL_DAYS * 86400)
        return found

    async def backfill_krea(self, client: httpx.AsyncClient) -> None:
        with contextlib.suppress(httpx.HTTPError, ValueError, KeyError):
            page = (await client.get(f"{KREA}/", timeout=10)).text
            match = re.search(r"data-lora-slots='([^']*)'", page)
            slots = json.loads(match.group(1).replace("&#34;", '"').replace("&quot;", '"')) if match else {}
            data = json.loads((await client.get(f"{KREA}/api/jobs", timeout=15)).text, strict=False)
            for job in data.get("jobs", data) if isinstance(data, dict) else data:
                when = _iso(job.get("completed_at") or job.get("started_at") or "")
                if not when or not job.get("started_at"):
                    continue
                request = job.get("request") or {}
                model = job.get("model") or request.get("model")
                names = set(KREA_FILES.get(model, []))
                if model == "anima":
                    version = request.get("anima_version") or request.get("version") or ""
                    names |= {ANIMA_FILES[version]} if version in ANIMA_FILES else set()
                family = slots.get("qwen" if model == "qwen_image_21" else "krea") or []
                for index, lora in enumerate(request.get("loras") or []):
                    if isinstance(lora, dict) and lora.get("enabled") and index < len(family):
                        names.add(family[index])
                self.mark(names, when)

    async def poll_comfy(self, client: httpx.AsyncClient) -> None:
        with contextlib.suppress(httpx.HTTPError, ValueError):
            history = (await client.get(f"{COMFY}/history", params={"max_items": "200"}, timeout=10)).json()
            seen = set(self.seen_prompts)
            for prompt_id, entry in history.items():
                if prompt_id in seen:
                    continue
                started = next((m[1].get("timestamp", 0) / 1000 for m in (entry.get("status") or {}).get("messages") or []
                                if m and m[0] == "execution_start"), time.time())
                self.mark(names_in_graph((entry.get("prompt") or [None, None, {}])[2]), started)
                self.seen_prompts.append(prompt_id)

    async def poll_llama(self) -> None:
        restarter = integrations.LlamaRestarter()
        loaded = await restarter.loaded()
        if loaded is None:
            return
        self.since.setdefault("llama", time.time())
        if not loaded:
            return
        now = time.time()
        for model in integrations.llama_models():
            if model["id"] in loaded:
                for host in (model["model"], model["mmproj"]):
                    prefix = integrations.MODELS_HOST_DIR + "/"
                    if host.startswith(prefix):
                        self.last[host[len(prefix):]] = now

    async def loop(self) -> None:
        await asyncio.sleep(20)
        tick = 0
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    if tick % 30 == 0:  # outputs + Krea's job list: at start, then every 30 min
                        self.busy = True
                        await asyncio.to_thread(self.backfill_outputs)
                        await self.backfill_krea(client)
                        self.busy = False
                    if tick % 2 == 0:
                        await self.poll_comfy(client)
                    await self.poll_llama()
                    self.save()
                except Exception:  # noqa: BLE001 - tracking must never stop the service
                    self.busy = False
                tick += 1
                await asyncio.sleep(60)

    def info(self, path: str, folder: str) -> dict:
        """last_used + whether tracking covers long enough to judge this file."""
        source = "llama" if folder == "llm" else "comfy"
        since = self.since.get(source)
        return {"last_used": self.last.get(path, 0), "tracked_since": since or 0}
