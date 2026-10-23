"""Rocsy Models: browse and download models and LoRAs from Civitai, Civitai Red
and Hugging Face into ComfyUI's models folder, see how well each file fits this
machine, and keep installed files up to date from the Manager (never automatic).

Served by the gateway at /Models/ behind the site login.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import contextlib
import copy
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import hardware
import integrations
import locations
import sources
import support
import comfysetup
import required
from comfysetup import Setup, hf_link
from downloads import Downloads, _same_identity, check_comfy_gguf, check_llama_gguf, llama_gguf_architecture
from library import BASE_PATTERNS, Library
from usage import Usage

PREFIX = "/Models"
HERE = Path(__file__).parent
DATA = Path(os.getenv("MODELHUB_DATA_DIR", "/data"))
ROOT = Path(os.getenv("MODELHUB_MODELS_DIR", "/comfy-models"))
LAYA_MODELS = Path(os.getenv("MODELHUB_LAYA_MODELS_DIR", "/laya-models"))
LAYA_HOST = Path(os.getenv("MODELHUB_LAYA_HOST_DIR") or LAYA_MODELS)   # shown as the location only
GATEWAY = os.getenv("MODELHUB_GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")
SECRET = os.getenv("QUEUE_INTERNAL_SECRET", "")
DATA.mkdir(parents=True, exist_ok=True)
(DATA / "thumbs").mkdir(exist_ok=True)
SETTINGS_PATH = DATA / "settings.json"
DEFAULT_SETTINGS = {"civitai_key": "", "hf_token": "", "auto_check": True, "concurrency": 2, "blur_nsfw": True}

app = FastAPI(title="Rocsy Models", docs_url=None, redoc_url=None, openapi_url=None)
app.mount(f"{PREFIX}/static", StaticFiles(directory=HERE / "static"), name="static")


def load_settings() -> dict:
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def tokens() -> dict:
    values = load_settings()
    return {"civitai": values["civitai_key"], "hf": values["hf_token"]}


def notify(event: str, title: str, body: str) -> None:
    if not SECRET:
        return

    async def send():
        with contextlib.suppress(httpx.HTTPError):
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{GATEWAY}/internal/notify", headers={"x-internal-secret": SECRET},
                                  json={"service": "Models", "event": event, "title": title, "body": body,
                                        "url": "/Models/#manager", "tag": f"models-{event}"})
    with contextlib.suppress(RuntimeError):
        asyncio.get_running_loop().create_task(send())


def retire_verified_update(job: dict) -> bool:
    """Remove the prior model only after every file in this update is verified."""
    batch = job.get("update_batch")
    if not batch:
        return False
    members = [entry for entry in downloads.jobs.values() if entry.get("update_batch") == batch]
    primary = next((entry for entry in members if entry.get("supersede")), None)
    if not primary or primary.get("retired") or primary.get("retirement_blocked"):
        return False
    if len(members) != primary.get("update_expected") or not all(
            entry.get("state") == "complete" and entry.get("postprocessed") for entry in members):
        return False

    def retain(reason: str) -> None:
        primary["retirement_blocked"] = reason
        primary["note"] = (primary.get("note", "") + f" · Previous version retained: {reason}").strip(" ·")[:400]

    if not all(entry.get("verified") and entry.get("postprocess_ok") for entry in members):
        retain("not every file had a matching source SHA-256 and successful registration")
        return False
    outputs = {entry.get("path") for entry in members}
    old_files = primary.get("supersede_files") or []
    if not old_files:
        retain("the original file identity was not recorded")
        return False
    targets = []
    for record in old_files:
        original = record["path"]
        swapped = next((entry.get("previous") for entry in members
                        if entry.get("previous") and entry.get("dest") and
                        str(Path(entry["dest"]) / entry.get("name", "")) == original), None)
        # A same-name replacement has a backup path; a renamed update leaves
        # the original in place until this group succeeds.
        rel = swapped or original
        path = ROOT / rel
        resolved = path.resolve()
        moved = locations.moved_target(path)   # moved to a storage location: the link + its file go
        if ((not str(resolved).startswith(str(ROOT.resolve()) + os.sep) and not moved) or (path.is_symlink() and not moved)
                or rel in outputs or not _same_identity(path, record["identity"])):
            retain(f"{original} changed or is no longer safe to remove")
            return False
        targets.append((record, path))
    try:
        for record, path in targets:
            original = record["path"]
            locations.remove(path)
            if original not in outputs:
                sidecar = (ROOT / original).with_name(Path(original).name + ".sha256")
                if record.get("sidecar_identity") and _same_identity(sidecar, record["sidecar_identity"]):
                    with contextlib.suppress(OSError):
                        sidecar.unlink()
                library.links.pop(original, None)
                library.hashes.pop(original, None)
    except OSError as exc:
        retain(f"could not remove the previous file ({exc.strerror or exc})")
        return False
    primary["retired"] = True
    primary["note"] = (primary.get("note", "") + " · Previous version removed after SHA-256 verification").strip(" ·")[:400]
    return True


async def download_done(job: dict) -> None:
    link = dict(job.get("link") or {})
    if link:
        library.link(job["path"], {**link, "sha256": job["sha256"], "linked_by": "downloaded here"})
    after = job.get("after") or {}
    job["postprocess_ok"] = True
    try:
        if after.get("type") == "gallery":
            job["note"] = await integrations.register_lora(job, after)
        elif after.get("type") == "llama" and after.get("role") == "model":
            with (ROOT / job["path"]).open("rb") as handle:
                llama_gguf_architecture(handle.read(4096))
            # Wait for the vision projector in the same group before adding the entry.
            for _ in range(720):
                siblings = [j for j in downloads.jobs.values() if j["group"] == job["group"] and j["id"] != job["id"]]
                if all(j["state"] == "complete" for j in siblings) or any(j["state"] in ("failed", "cancelled") for j in siblings):
                    break
                await asyncio.sleep(5)
            mmproj = next((j.get("path", "") for j in downloads.jobs.values()
                           if j["group"] == job["group"] and (j.get("after") or {}).get("role") == "mmproj"
                           and j["state"] == "complete"), "")
            model_id = await asyncio.to_thread(integrations.add_llama_model, after["name"], job["path"], mmproj,
                                               after.get("offload", False), after.get("source", ""),
                                               after.get("ngram_nvme", False))
            restarter.request(f"new model {model_id}")
            job["note"] = (f"Added to llama-swap as \"{model_id}\"; it shows up after llama-swap restarts, "
                           "which happens automatically once no model is loaded")
        elif Path(job["dest"]).parts[:1] and Path(job["dest"]).parts[0] in integrations.DIFFUSION_DIRS and not after.get("role"):
            job["note"] = await integrations.register_checkpoint(job, after.get("base") or link.get("base") or "")
    except Exception as exc:  # noqa: BLE001 - the file is downloaded either way
        job["postprocess_ok"] = False
        job["note"] = f"Downloaded, but {exc}"[:300]
    await asyncio.to_thread(library.scan)
    job["postprocessed"] = True
    if retire_verified_update(job):
        await asyncio.to_thread(library.scan)
    downloads.save()
    notify("downloaded", "Download finished", job["name"] + (f" · {job['note']}" if job.get("note") else ""))


restarter = integrations.LlamaRestarter()
downloads: Downloads
library = Library(DATA, ROOT, tokens)
usage = Usage(DATA, library)
comfy_setup = Setup(DATA, ROOT, tokens)
hw_cache: dict = {}
enum_cache: dict = {}


def scan_hardware() -> dict:
    hw_cache.clear()
    hw_cache.update(hardware.scan({"Models folder": ROOT}))
    hw_cache["scanned_at"] = time.time()
    return hw_cache


@app.on_event("startup")
async def startup() -> None:
    global downloads
    downloads = Downloads(DATA, ROOT, tokens, download_done)
    downloads.store_for = download_store
    downloads.concurrency = int(load_settings()["concurrency"])
    scan_hardware()
    asyncio.create_task(downloads.run())
    asyncio.create_task(asyncio.to_thread(library.scan))
    asyncio.create_task(daily_check())
    asyncio.create_task(restarter.loop())
    asyncio.create_task(usage.loop())
    asyncio.create_task(core_watch())
    asyncio.create_task(comfy_nodes_watch())
    asyncio.create_task(warm_searches())
    asyncio.create_task(locations.loop())


def download_store(dest: str, link: Path) -> str:
    """Settings -> Storage: an update of a moved file stays on its drive; new files go to the default."""
    moved = locations.moved_target(link)
    if moved is not None:
        loc = locations.location_of(moved)
        if loc and os.path.isdir(loc["path"]) and os.access(loc["path"], os.W_OK):
            return loc["path"]
    return locations.store_for(dest)


async def daily_check() -> None:
    """Checks only (never updates): once a day when enabled, plus at start-up if overdue."""
    await asyncio.sleep(60)
    while True:
        if load_settings()["auto_check"] and time.time() - library.last_check > 24 * 3600:
            await asyncio.to_thread(library.scan)
            unknown = [i["path"] for i in library.view() if i["status"] == "unknown" and i["sha256"]]
            if unknown:
                await library.identify(unknown, compute=False)
            before = {p for p, link in library.links.items() if (link.get("update") or {}).get("available")}
            await library.check_updates()
            after = [p for p, link in library.links.items() if (link.get("update") or {}).get("available")]
            new = [p for p in after if p not in before]
            if new:
                notify("updates", f"{len(new)} model update{'s' if len(new) != 1 else ''} available",
                       ", ".join(Path(p).name for p in new[:3]) + ("…" if len(new) > 3 else ""))
        await asyncio.sleep(1800)


# ---- pages ------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"ok": True}


@app.get(PREFIX)
async def slash():
    return RedirectResponse(f"{PREFIX}/")


@app.get(f"{PREFIX}/", response_class=HTMLResponse)
async def page():
    html = (HERE / "static" / "index.html").read_text()
    version = hashlib.sha256(b"".join((HERE / "static" / n).read_bytes() for n in ("app.js", "app.css"))).hexdigest()[:10]
    return HTMLResponse(html.replace("{{v}}", version), headers={"Cache-Control": "no-store"})


# ---- state, hardware, settings -----------------------------------------------

@app.get(f"{PREFIX}/api/state")
async def state():
    values = load_settings()
    return {"hardware": hw_cache, "downloads": downloads.list()[:60], "active": downloads.active_count(),
            "library_task": library.task, "last_check": library.last_check,
            "llama_restart": {"pending": restarter.pending, "reason": restarter.reason, "last": restarter.last},
            "updates": sum(1 for link in library.links.values() if isinstance(link, dict) and (link.get("update") or {}).get("available")),
            "node_updates": node_updates["count"],
            "settings": {"civitai_key": bool(values["civitai_key"]), "hf_token": bool(values["hf_token"]),
                         "auto_check": values["auto_check"], "concurrency": values["concurrency"],
                         "blur_nsfw": values["blur_nsfw"]}}


@app.post(f"{PREFIX}/api/hardware/scan")
async def hardware_scan():
    return await asyncio.to_thread(scan_hardware)


class SettingsBody(BaseModel):
    civitai_key: str | None = None
    hf_token: str | None = None
    auto_check: bool | None = None
    concurrency: int | None = None
    blur_nsfw: bool | None = None


@app.put(f"{PREFIX}/api/settings")
async def put_settings(body: SettingsBody):
    values = load_settings()
    for key, value in body.model_dump(exclude_none=True).items():
        if key in ("civitai_key", "hf_token"):
            value = value.strip()
            if value and not re.fullmatch(r"[\w\-.]{8,200}", value):
                raise HTTPException(422, "That doesn't look like a key")
        if key == "concurrency":
            value = max(1, min(4, value))
        values[key] = value
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(values))
    os.chmod(tmp, 0o600)
    os.replace(tmp, SETTINGS_PATH)
    downloads.concurrency = int(values["concurrency"])
    return await state()


# ---- browse ---------------------------------------------------------------------

async def civitai_bases(client: httpx.AsyncClient) -> list[str]:
    if not enum_cache or time.time() - enum_cache.get("at", 0) > 86400:
        try:
            response = await client.get("https://civitai.com/api/v1/enums", headers=sources.UA, timeout=20)
            enum_cache.update(bases=response.json().get("BaseModel") or [], at=time.time())
        except (httpx.HTTPError, ValueError):
            enum_cache.setdefault("bases", [])
    return enum_cache["bases"]


def _fit_variants(version: dict, category: str, kind: str) -> None:
    for variant in version["variants"]:
        role = variant.get("role", "")
        cat = "lora" if kind == "lora" or role == "LoRA" else ("encoder" if role or variant["dest"] in ("vae", "text_encoders") else category)
        size = variant["size"] + sum(f["size"] for f in variant.get("companions") or [])
        variant["fit"] = hardware.fit(size, cat, variant.get("file_label") or variant["files"][0]["name"],
                                      variant.get("format", ""), hw_cache, variant["quant"])
        variant["installed"] = _installed(variant)
    main = [v for v in version["variants"] if not v.get("role") and v["dest"] not in ("vae", "text_encoders")] or version["variants"]
    version["best"] = hardware.best_variant(main) if kind != "lora" else None


def _installed(variant: dict) -> str:
    for file in variant["files"]:
        path = ROOT / file["dest"] / Path(file["name"]).name
        if path.exists():
            return str(path.relative_to(ROOT))
        sha = file.get("sha256")
        if sha:
            match = next((rel for rel, h in library.hashes.items() if h.get("sha256") == sha), "")
            if match:
                return match
    return ""


@app.get(f"{PREFIX}/api/bases")
async def bases():
    """Base models for the LoRA filter: the ones installed here first, then every Civitai base."""
    async with httpx.AsyncClient() as client:
        everything = await civitai_bases(client)
    installed = library.installed_bases()
    return {"installed": [{**b, "also": sources.ALSO_WORKS_ON.get(b["base"], []),
                           "hf": bool(sources.HF_BASE_REPOS.get(b["base"]))} for b in installed],
            "all": [b for b in everything if b not in {i["base"] for i in installed}],
            "hf_mapped": sorted(sources.HF_BASE_REPOS)}


# Infinite scroll asks for the next page ahead of time and tabs/Back revisit pages, so recent
# pages are kept for a few minutes; one pooled client keeps the TLS connections warm.
log = logging.getLogger("modelhub")
SEARCH_TTL = 300
SEARCH_STALE = 3 * 86400  # older than TTL but younger than this: served at once, refreshed behind
search_refreshing: set[tuple] = set()
search_cache: dict[tuple, tuple[float, dict]] = {}
search_client: httpx.AsyncClient | None = None
# First pages people open (a tab, a base filter, a sort) are kept warm in the background and saved
# across restarts, so opening Models or LoRAs never waits on a cold Civitai/HF answer.
search_hot: dict[tuple, float] = {}  # key -> last asked (wall clock)
HOT_KEEP = 3 * 86400
HOT_MAX = 40
HOT_REFRESH = 1800
SEARCH_SNAPSHOT = DATA / "search_cache.json"
# Images-API fallbacks for cards without a preview: the page waits this long, the rest land later.
FALLBACK_WAIT = 1.2
fallback_tasks: set[asyncio.Task] = set()


def browse_client() -> httpx.AsyncClient:
    global search_client
    if search_client is None:
        search_client = httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
    return search_client


@app.get(f"{PREFIX}/api/search")
async def search(source: str = "civitai", kind: str = "model", category: str = "all", q: str = "",
                 sort: str = "downloads", cursor: str = "", gguf: bool = True, base: str = ""):
    base = base.strip()[:60]
    if source not in ("civitai", "civitai_red", "hf") or kind not in ("model", "lora"):
        raise HTTPException(422, "Unknown source or kind")
    if cursor and source == "hf" and not all(u.startswith(sources.HF + "/api/models?") for u in cursor.split("|")):
        raise HTTPException(422, "Bad cursor")
    key = (source, kind, category, q.strip()[:120], sort, cursor, gguf, base)
    if not cursor and not key[3]:
        search_hot[key] = time.time()
    hit = search_cache.get(key)
    age = time.monotonic() - hit[0] if hit else None
    if hit and age < SEARCH_TTL:
        return _mark_installed(hit[1])
    if hit and age < SEARCH_STALE:
        # Answer instantly with the older page and refresh it for next time.
        if key not in search_refreshing:
            search_refreshing.add(key)
            asyncio.create_task(_refresh_search(key))
        return _mark_installed(hit[1])
    return _mark_installed(await _fetch_search(key))


def _installed_index() -> dict[str, list[dict]]:
    """Files on disk by the model page they came from: "civitai:<model id>" (Civitai and Civitai Red
    share ids) or "hf:<repo>". Only linked files count; unidentified ones can't be matched."""
    index: dict[str, list[dict]] = {}
    for item in library.items:
        link = library.links.get(item["path"]) or {}
        if link.get("source", "").startswith("civitai") and link.get("model_id"):
            key = f"civitai:{link['model_id']}"
        elif link.get("source") == "hf" and link.get("repo"):
            key = f"hf:{link['repo'].lower()}"
        else:
            continue
        index.setdefault(key, []).append({"name": item["name"], "version_id": str(link.get("version_id") or "")})
    return index


def _mark_installed(result: dict) -> dict:
    """Tags cards whose model already has files here, and what can run them (support.py). Done
    per response, outside the search cache, so a download or uninstall shows on the next page load."""
    index = _installed_index()
    items = []
    for item in result["items"]:
        key = f"hf:{item['id'].lower()}" if item["source"] == "hf" else f"civitai:{item['id']}"
        files = index.get(key)
        if files:
            versions = {f["version_id"] for f in files if f["version_id"]}
            latest = item.get("latest_version")
            item = {**item, "installed": {"files": sorted(f["name"] for f in files),
                                          # Civitai only: none of the files is the newest version on the page
                                          "older": bool(latest and versions and latest not in versions)}}
        # Per response too: a workflow saved in ComfyUI counts on the next page load.
        item = {**item, "supported": support.supported(item)}
        items.append(item)
    return {**result, "items": items}


async def _refresh_search(key: tuple) -> None:
    try:
        await _fetch_search(key)
    except Exception as exc:  # keep serving the stale page
        log.info("Background search refresh failed: %s", exc)
    finally:
        search_refreshing.discard(key)


async def warm_searches() -> None:
    """Keeps recently opened first pages fresh (light: each at most every 30 min) and saves them."""
    _load_search_snapshot()
    saved: set = set()
    await asyncio.sleep(20)
    while True:
        now = time.time()
        for key in [k for k, last in search_hot.items() if now - last > HOT_KEEP]:
            search_hot.pop(key, None)
        for key in sorted(search_hot, key=search_hot.get)[:-HOT_MAX]:
            search_hot.pop(key, None)
        due = [k for k in search_hot if k not in search_refreshing
               and (k not in search_cache or time.monotonic() - search_cache[k][0] > HOT_REFRESH)]
        for key in due:
            search_refreshing.add(key)
            await _refresh_search(key)
        current = {(k, search_cache[k][0]) for k in search_hot if k in search_cache}
        if current != saved:  # a page was opened or refreshed since the last save
            await asyncio.to_thread(_write_search_snapshot, _search_snapshot())
            saved = current
        await asyncio.sleep(60)


def _search_snapshot() -> str:
    """Serialized on the event loop: late previews patch cached cards there."""
    now_wall, now_mono = time.time(), time.monotonic()
    return json.dumps([{"key": list(k), "asked": search_hot[k], "at": now_wall - (now_mono - search_cache[k][0]),
                        "result": search_cache[k][1]} for k in search_hot if k in search_cache])


def _write_search_snapshot(text: str) -> None:
    tmp = SEARCH_SNAPSHOT.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(SEARCH_SNAPSHOT)


def _load_search_snapshot() -> None:
    try:
        pages = json.loads(SEARCH_SNAPSHOT.read_text())
    except (OSError, ValueError):
        return
    now_wall, now_mono = time.time(), time.monotonic()
    for page in pages if isinstance(pages, list) else []:
        with contextlib.suppress(KeyError, TypeError, ValueError):
            key = tuple(page["key"])
            if len(key) == 8 and now_wall - page["at"] < SEARCH_STALE and key not in search_cache:
                search_cache[key] = (now_mono - (now_wall - page["at"]), page["result"])
                search_hot[key] = float(page["asked"])


async def _image_fallbacks(source: str, items: list[dict]) -> None:
    """Cards Civitai listed without any image: ask the images API, but only hold the page for
    FALLBACK_WAIT. Late answers patch the cached page (and are remembered per model)."""
    missing = [item for item in items if not item["preview"]]
    if not missing:
        return
    tasks = {asyncio.create_task(sources.civitai_image_fallback(browse_client(), source, dict(item), tokens()["civitai"])): item
             for item in missing}
    _, pending = await asyncio.wait(tasks, timeout=FALLBACK_WAIT)
    for task, item in tasks.items():
        if task in pending:
            fallback_tasks.add(task)
            task.add_done_callback(lambda t, item=item: (fallback_tasks.discard(t), _late_preview(source, item)))
        else:
            sources.apply_image_fallback(source, item)


def _late_preview(source: str, item: dict) -> None:
    if sources.apply_image_fallback(source, item) and item["preview"]:
        item["poster"] = poster_url(item["preview"], item.get("preview_kind", ""))
        item["preview"] = thumb_url(item["preview"])


def _recommended_quant(files: list[dict] | None, category: str, kind: str) -> dict | None:
    """Card badge: the quant that runs best here (best_variant logic), or the smallest
    one when nothing runs (shown red). None when there is nothing to say."""
    if kind == "lora" or not files:
        return None
    picks = []
    for f in files:
        if f.get("role"):
            continue  # draft models, VAEs, encoders: extras, not the model
        quant = f.get("quant") or hardware.quant_of(f["name"], f.get("precision", ""))
        fit = hardware.fit(f["size"], category, f["name"], f.get("format") or f.get("precision", ""), hw_cache, quant)
        picks.append({"key": str(len(picks)), "size": f["size"], "quant": quant, "fit": fit,
                      "tag": f.get("quant_tag") or quant["label"] or "Full repo"})
    if not picks:
        return None
    best_key = hardware.best_variant(picks)
    if best_key is not None:
        best = picks[int(best_key)]
    else:
        blocked = [p for p in picks if p["fit"]["tier"] == "no"]
        if not blocked:
            return None  # sizes unknown: keep the plain fit badge
        best = min(blocked, key=lambda p: p["size"] or 1 << 62)
    return {"tag": best["tag"], "tier": best["fit"]["tier"], "tight": bool(best["fit"].get("tight")),
            "size": best["size"], "label": best["fit"]["label"], "note": best["fit"].get("note", "")}


async def _fill_hf_quants(items: list[dict]) -> None:
    """Recommended-quant data for Hugging Face cards: the list API has no file sizes,
    so fetch each repo's file list (one light call, side by side; the page is cached)."""
    sem = asyncio.Semaphore(8)

    async def fill(item: dict) -> None:
        async with sem:
            try:
                item["quants"] = await sources.hf_quants(browse_client(), item["id"], tokens()["hf"],
                                                         item["category"], item["kind"])
            except httpx.HTTPError:
                pass  # gated or unreachable: the card keeps its plain fit badge

    await asyncio.gather(*(fill(item) for item in items if item["kind"] == "model"))


async def _fetch_search(key: tuple) -> dict:
    source, kind, category, q, sort, cursor, gguf, base = key
    try:
        client = browse_client()
        if source == "hf":
            result = await sources.hf_search(client, kind, category, q, sort, cursor,
                                             tokens()["hf"], gguf, base)
        else:
            result = await sources.civitai_search(client, source, kind, category, q, sort, cursor,
                                                  await civitai_bases(client), tokens()["civitai"], base)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"{'Hugging Face' if source == 'hf' else 'Civitai'} answered {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"{'Hugging Face' if source == 'hf' else 'Civitai'} is unreachable") from exc
    if source == "hf":
        await _fill_hf_quants(result["items"])
    else:
        await _image_fallbacks(source, result["items"])
    for item in result["items"]:
        if item["preview"]:
            item["poster"] = poster_url(item["preview"], item.get("preview_kind", ""))
            item["preview"] = thumb_url(item["preview"])
        if item.get("logo"):
            item["logo"] = f"{PREFIX}/api/logo/{item['logo']}"
        primary = item.pop("primary", None)
        if primary and primary.get("size"):
            cat = "lora" if item["kind"] == "lora" else item["category"]
            item["fit"] = hardware.fit(primary["size"], cat, primary["name"], primary["precision"], hw_cache)
            item["size"] = primary["size"]
        rec = _recommended_quant(item.pop("quants", None), item["category"], item["kind"])
        if rec:
            item["rec"] = rec
    now = time.monotonic()
    for old in [k for k, (t, _) in search_cache.items() if now - t >= SEARCH_STALE][:200]:
        search_cache.pop(old, None)
    if len(search_cache) >= 200:
        search_cache.pop(next(iter(search_cache)))
    search_cache[key] = (now, result)
    return result


async def fetch_detail(source: str, model_id: str, kind: str = "") -> dict:
    client = browse_client()  # pooled: no new TLS handshake per model page
    if source == "hf":
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", model_id):
            raise HTTPException(422, "Bad repo id")
        detail = await sources.hf_model(client, model_id, tokens()["hf"], kind)
    else:
        if not model_id.isdigit():
            raise HTTPException(422, "Bad model id")
        detail = await sources.civitai_model(client, source, model_id, tokens()["civitai"])
    return detail


access_cache: dict[str, tuple[float, str]] = {}


async def civitai_access(source: str, version_id: str, fresh: bool = False) -> str:
    """Can this account download a paywalled (early access) version? Asks the download URL itself
    without following it: a redirect to the file = bought/unlocked, 403 "Early Access" = locked.
    "no_key" without a Civitai API key (purchases are tied to the account), "unknown" on errors."""
    if not tokens()["civitai"]:
        return "no_key"
    hit = access_cache.get(version_id)
    if hit and not fresh and time.monotonic() - hit[0] < 120:
        return hit[1]
    url = f"{sources.CIVITAI_HOSTS.get(source, sources.CIVITAI_HOSTS['civitai'])}/api/download/models/{int(version_id)}"
    headers = {"User-Agent": "Rocsy-ModelHub/1.0", "Authorization": f"Bearer {tokens()['civitai']}"}
    try:
        async with browse_client().stream("GET", url, headers=headers, follow_redirects=False, timeout=20) as response:
            if response.is_redirect or response.status_code == 200:
                result = "yes"
            elif response.status_code == 403 and "Early Access" in (await response.aread()).decode(errors="replace"):
                result = "locked"
            elif response.status_code == 401:
                result = "no_key"
            else:
                result = "unknown"
    except httpx.HTTPError:
        result = "unknown"
    access_cache[version_id] = (time.monotonic(), result)
    return result


@app.get(f"{PREFIX}/api/civitai-access")
async def civitai_access_check(version_id: str, source: str = "civitai"):
    """"I bought it: check again" on a locked version."""
    if not version_id.isdigit() or source not in ("civitai", "civitai_red"):
        raise HTTPException(422, "Bad version")
    return {"access": await civitai_access(source, version_id, fresh=True)}


# Model pages are opened (and prefetched on hover) again and again while browsing: keep the
# upstream answer a few minutes. Updates/links call fetch_detail directly and stay fresh.
DETAIL_TTL = 300
detail_cache: dict[tuple, tuple[float, dict]] = {}


@app.get(f"{PREFIX}/api/model")
async def model(source: str, id: str, kind: str = ""):
    key = (source, id, kind)
    hit = detail_cache.get(key)
    try:
        if hit and time.monotonic() - hit[0] < DETAIL_TTL:
            detail = copy.deepcopy(hit[1])
        else:
            raw = await fetch_detail(source, id, kind)
            now = time.monotonic()
            for old in [k for k, (t, _) in detail_cache.items() if now - t >= DETAIL_TTL]:
                detail_cache.pop(old, None)
            detail_cache[key] = (now, raw)
            detail = copy.deepcopy(raw)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise HTTPException(502, "Gated or private: add a token in Manager -> Settings" if status in (401, 403)
                            else f"Upstream answered {status}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Upstream unreachable") from exc
    for version in detail["versions"]:
        _fit_variants(version, version["category"], detail["kind"])
        for variant in version["variants"]:
            target = integration_target(detail, version, variant)
            variant["target"] = target and {k: target[k] for k in ("type", "label", "dest")}
        if version.get("preview"):
            version["poster"] = poster_url(version["preview"], version.get("preview_kind", ""))
            version["full"] = thumb_url(version["preview"], wide=True)
            version["preview"] = thumb_url(version["preview"])
        # The page's preview gallery: same-origin (the site CSP blocks other hosts), a small rendition
        # for the strip, a big one for the main view, a still poster for Civitai videos.
        version["images"] = [{"url": thumb_url(i["url"]), "full": thumb_url(i["url"], wide=True),
                              "poster": poster_url(i["url"], i.get("type") or "image"),
                              "type": i.get("type") or "image", "nsfwLevel": i.get("nsfwLevel") or 1}
                             for i in version.get("images") or [] if urlparse(i.get("url") or "").hostname in THUMB_HOSTS]
    if detail.get("preview"):
        detail["preview"] = thumb_url(detail["preview"])
    if source != "hf":  # paywalled versions: does this Civitai account already have them?
        locked = [v for v in detail["versions"] if v.get("paywall")][:6]
        for version, access in zip(locked, await asyncio.gather(*(civitai_access(source, v["id"]) for v in locked))):
            version["access"] = access
    return detail


# ---- downloads --------------------------------------------------------------------

class DownloadBody(BaseModel):
    source: str
    model_id: str
    version_id: str
    variant: str
    dest: str = ""
    companions: bool = True
    replace: str = ""  # installed file (relative path) this download updates in place
    schedule: bool = False  # Civitai early access: queue it to start when it becomes free


def _clean_dest(dest: str) -> str:
    dest = dest.strip().strip("/")
    if not dest or ".." in dest.split("/") or not re.fullmatch(r"[\w .+/-]{1,160}", dest):
        raise HTTPException(422, "Choose a destination folder")
    return dest


# models/ folders whose ComfyUI loaders take GGUFs (ComfyUI-GGUF) -> the kind check_comfy_gguf checks.
COMFY_GGUF_KINDS = {"diffusion_models": "diffusion", "unet": "diffusion", "text_encoders": "text", "clip": "text"}


_WAN_HIGH = {"high", "highnoise", "hn", "h", "hi"}
_WAN_LOW = {"low", "lownoise", "ln", "l", "lo"}


def wan_expert(name: str) -> tuple[str, str]:
    """('high'|'low'|'', the name without its expert marker) - Video's custom_loras.wan_expert rule."""
    stem = re.sub(r"\.[a-z0-9]+$", "", name.lower())
    tokens = re.split(r"[^a-z0-9]+", stem)
    high = bool(set(tokens) & _WAN_HIGH) or "high_noise" in stem
    low = bool(set(tokens) & _WAN_LOW) or "low_noise" in stem
    rest = " ".join(t for t in tokens if t not in _WAN_HIGH | _WAN_LOW | {"noise"})
    return ("high" if high and not low else "low" if low and not high else ""), rest


def wan_partner(detail: dict, version: dict, variant: dict) -> tuple[dict, dict] | None:
    """The other half of a Wan 2.2 high/low-noise LoRA pair: (version, variant), from the same version's files
    first, else a sibling version named like this one (authors often publish "v1 High" and "v1 Low")."""
    expert, rest = wan_expert(variant["files"][0]["name"])
    if not expert:
        expert, rest = wan_expert(version.get("name") or "")
        if not expert:
            return None
        same_version = []
    else:
        same_version = [(version, v) for v in version.get("variants") or [] if v is not variant
                        and wan_expert(v["files"][0]["name"])[0] not in ("", expert)]
    if len(same_version) == 1:
        return same_version[0]
    _, version_rest = wan_expert(version.get("name") or "")
    siblings = [(other, (other.get("variants") or [None])[0]) for other in detail.get("versions") or []
                if other is not version and other.get("base") == version.get("base")
                and wan_expert(other.get("name") or "") == ({"high": "low", "low": "high"}[expert], version_rest)]
    return siblings[0] if len(siblings) == 1 and siblings[0][1] else None


def integration_target(detail: dict, version: dict, variant: dict) -> dict | None:
    """Where this file goes after downloading: a studio LoRA gallery or llama-swap."""
    lower = variant["files"][0]["name"].lower()
    if detail["kind"] == "lora" and lower.endswith(".safetensors"):
        gallery = integrations.gallery_for(sources.base_name(version.get("base") or ""))
        if gallery:
            return {"type": "gallery", "label": gallery["label"], "dest": gallery["dest"], "gallery": gallery}
    if variant.get("format") == "GGUF" and variant["dest"].startswith("llm/") and not variant.get("role"):
        return {"type": "llama", "label": "llama-swap", "dest": variant["dest"]}
    return None


@app.post(f"{PREFIX}/api/download")
async def download(body: DownloadBody):
    return await queue_download(body)


async def queue_download(body: DownloadBody, *, supersede: str = ""):
    # Re-read the listing server-side: URLs and hashes never come from the browser.
    detail = await fetch_detail(body.source, body.model_id)
    version = next((v for v in detail["versions"] if v["id"] == body.version_id), None)
    if body.source == "hf" and not version:
        version = detail["versions"][0]
    variant = next((v for v in (version or {}).get("variants", []) if v["key"] == body.variant), None)
    if not variant:
        raise HTTPException(404, "That file is no longer listed")
    not_before = 0.0
    paywall = version.get("paywall") if body.source != "hf" else None
    if paywall and await civitai_access(body.source, version["id"], fresh=True) != "yes":
        if not body.schedule:
            raise HTTPException(409, "Early access on Civitai: unlock it with Buzz on its page, or choose Download when free"
                                if not paywall["permanent"] else "Paid on Civitai: buy it with Buzz on its page first")
        if paywall["permanent"]:
            raise HTTPException(409, "This version stays paid on Civitai, so it can't be scheduled")
        not_before = datetime.fromisoformat(paywall["ends"].replace("Z", "+00:00")).timestamp() + 300
    if version.get("category") == "llm" and variant.get("format") == "GGUF" and not variant.get("role"):
        fit = hardware.fit(variant["size"], "llm", variant["files"][0]["name"], "GGUF", hw_cache)
        if fit["tier"] == "no":
            raise HTTPException(422, fit.get("note") or "This model cannot run on this machine")
        primary = variant["files"][0]
        headers = {"User-Agent": "Rocsy-ModelHub/1.0"}
        if body.source == "hf" and tokens().get("hf"):
            headers["Authorization"] = f"Bearer {tokens()['hf']}"
        try:
            await check_llama_gguf(primary["url"], headers)
        except (ValueError, httpx.HTTPError) as exc:
            raise HTTPException(422, f"Cannot queue this llama.cpp model: {exc}") from exc
    files = variant["files"] + (variant.get("companions") or [] if body.companions else [])
    wan_pair = None
    group = f"{body.source}:{body.model_id}:{version['id']}:{variant['key']}"
    update_batch = uuid.uuid4().hex if supersede else ""
    replace_name = ""
    supersede_files = []
    if supersede:
        old_path = ROOT / supersede
        old = old_path.resolve()
        moved = locations.moved_target(old_path)
        if ((not str(old).startswith(str(ROOT.resolve()) + os.sep) and not moved) or not old.is_file()
                or (old_path.is_symlink() and not moved)):
            raise HTTPException(404, "The installed file to update is missing")
        old_parts = [old_path]
        shard = re.search(r"-00001-of-(\d{5})\.(gguf|safetensors)$", old_path.name)
        if shard:
            if int(shard.group(1)) > 512:
                raise HTTPException(422, "This model has too many shards for automatic replacement")
            old_parts += [old_path.with_name(old_path.name.replace("-00001-of-", f"-{n:05d}-of-"))
                          for n in range(2, int(shard.group(1)) + 1)]
        for old_part in old_parts:
            if not old_part.is_file() or (old_part.is_symlink() and not locations.moved_target(old_part)):
                continue
            stat = old_part.stat()
            record = {"path": str(old_part.relative_to(ROOT)),
                      "identity": {field: getattr(stat, field)
                                   for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")}}
            sidecar = old_part.with_name(old_part.name + ".sha256")
            if sidecar.is_file() and not sidecar.is_symlink():
                sidecar_stat = sidecar.stat()
                record["sidecar_identity"] = {field: getattr(sidecar_stat, field)
                                              for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")}
            supersede_files.append(record)
    if body.replace:
        old = (ROOT / body.replace).resolve()
        if ((not str(old).startswith(str(ROOT.resolve()) + os.sep) and not locations.moved_target(ROOT / body.replace))
                or not old.is_file()):
            raise HTTPException(404, "The installed file to update is missing")
        replace_name = Path(body.replace).name
    target = None if body.replace else integration_target(detail, version, variant)
    if target and body.dest and _clean_dest(body.dest) != target["dest"]:
        target = None  # the user chose another folder: plain download
    after_for = {}
    if target and target["type"] == "gallery":
        gallery = target["gallery"]
        strength, note = integrations.recommended_strength(detail.get("description", ""))
        page = detail["url"] + (f"?modelVersionId={version['id']}" if body.source != "hf" else "")
        label = detail["name"] if len(detail["versions"]) == 1 or body.source == "hf" else f"{detail['name']} ({version['name']})"
        after_for[0] = {"type": "gallery", "gallery": gallery, "label": label[:100], "triggers": version.get("triggers") or [],
                        "description": detail.get("description", "")[:10000], "creator": detail.get("author", ""),
                        "version": version.get("name", ""), "base_model": sources.base_name(version.get("base") or ""),
                        "url": page, "default_strength": strength,
                        "strength_note": note or "Default starting strength; adjust after testing",
                        "category": integrations.pick_category(await integrations.categories(gallery), detail.get("tags") or [],
                                                               bool(detail.get("nsfw") or version.get("nsfw"))),
                        "previews": version.get("images") or []}
        if gallery["model"] == "wan_2_2" and body.companions and (wan_pair := wan_partner(detail, version, variant)):
            # Video joins the two halves into one gallery LoRA (same download group = same source key).
            # It reads the expert from the file name, so a marker only the version name carries goes on the file.
            def marked(file: dict, owner: dict) -> dict:
                expert = wan_expert(owner.get("name") or "")[0]
                if wan_expert(file["name"])[0] or not expert:
                    return file
                stem, dot, ext = file["name"].rpartition(".")
                return {**file, "name": f"{stem}_{expert}_noise.{ext}" if dot else f"{file['name']}_{expert}_noise"}
            files = [marked(files[0], version), *files[1:], marked(wan_pair[1]["files"][0], wan_pair[0])]
            after_for[len(files) - 1] = after_for[0]
    elif target and target["type"] == "llama":
        quant = variant.get("quant", {}).get("label") or ""
        name = f"{detail['name'].removesuffix('-GGUF').removesuffix('-gguf')} {quant}".strip()
        fit_tier = variant.get("fit", {}).get("tier")
        after_for[0] = {"type": "llama", "role": "model", "name": name, "source": detail["url"],
                        "offload": fit_tier in ("offload", "multi", "nvme")
                        or hardware.fit(variant["size"], "llm", variant["files"][0]["name"], "GGUF", hw_cache)["tier"] in ("offload", "multi", "nvme"),
                        "ngram_nvme": fit_tier == "nvme"}
        for index in range(len(variant["files"]), len(files)):
            after_for[index] = {"type": "llama", "role": "mmproj"}
    if 0 not in after_for and version.get("base"):
        after_for[0] = {"type": "checkpoint", "base": sources.base_name(version["base"])}  # Image's model list
    if version.get("category") != "llm":
        # ComfyUI GGUFs (diffusion models, text encoders): only architectures ComfyUI-GGUF loads
        for file in files:
            dest = _clean_dest(body.dest) if body.dest and not body.replace else file["dest"]
            if body.replace:
                dest = str(Path(body.replace).parent)
            kind = COMFY_GGUF_KINDS.get(Path(dest).parts[0] if Path(dest).parts else "")
            if not kind or not file["name"].lower().endswith(".gguf"):
                continue
            headers = {"User-Agent": "Rocsy-ModelHub/1.0"}
            if body.source == "hf" and tokens().get("hf"):
                headers["Authorization"] = f"Bearer {tokens()['hf']}"
            try:
                await check_comfy_gguf(file["url"], headers, kind)
            except (ValueError, httpx.HTTPError) as exc:
                raise HTTPException(422, f"Cannot use {Path(file['name']).name} in ComfyUI: {exc}") from exc
    jobs = []
    try:
        for index, file in enumerate(files):
            primary = index == 0
            dest = _clean_dest(body.dest) if body.dest and not body.replace else file["dest"]
            if target:
                dest = target["dest"]
            if body.replace:
                dest = str(Path(body.replace).parent)
            name = replace_name if body.replace and primary and len(variant["files"]) == 1 else file["name"]
            if target and target["type"] == "gallery" and (after_for.get(index) or {}).get("type") == "gallery":
                name = integrations.storage_name(name, ROOT / dest)  # the studios' file-name rules
            if body.source == "hf":
                link = {"source": "hf", "repo": body.model_id, "file": file["name"], "revision": detail.get("revision", ""),
                        "name": body.model_id}
            else:
                link = {"source": body.source, "model_id": body.model_id, "version_id": version["id"],
                        "version_name": version["name"], "name": detail["name"], "base": version["base"],
                        "nsfw": body.source == "civitai_red" and detail.get("nsfw", False)}
            planned_path = str(Path(dest) / Path(name).name)
            matched_old = next((entry for entry in supersede_files if entry["path"] == planned_path), None)
            replacing_same_path = bool(body.replace and matched_old)
            jobs.append(downloads.add(url=file["url"], dest=dest, name=Path(name).name, size=file["size"],
                                      sha256=file.get("sha256", ""), source=body.source, group=group, link=link,
                                      replace=replacing_same_path, after=after_for.get(index),
                                      not_before=not_before, update_batch=update_batch,
                                      update_expected=len(files), supersede=supersede if primary else "",
                                      supersede_identity=matched_old["identity"] if matched_old else None,
                                      supersede_files=supersede_files if primary else None))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"jobs": jobs, "target": target and {k: target[k] for k in ("type", "label", "dest")}}


@app.post(f"{PREFIX}/api/downloads/{{job_id}}/{{action}}")
async def download_action(job_id: str, action: str):
    if job_id not in downloads.jobs or action not in ("pause", "resume", "cancel", "retry", "clear", "start"):
        raise HTTPException(404, "Unknown download")
    return downloads.action(job_id, action)


@app.get(f"{PREFIX}/api/dests")
async def dests():
    folders = {str(p.relative_to(ROOT)) for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")}
    if (ROOT / "llm").is_dir():
        folders |= {f"llm/{p.name}" for p in (ROOT / "llm").iterdir() if p.is_dir()}
    return {"folders": sorted(folders)}


# ---- core backend (llama.cpp + RDNA Boost builds, run on the host by hostctl/corebackend.py) ----

async def hostctl_extras(method: str, path: str = "") -> dict:
    if not integrations.HOSTCTL_SECRET:
        raise HTTPException(503, "The host helper (hostctl) isn't configured")
    try:
        async with httpx.AsyncClient(timeout=100) as client:
            response = await client.request(method, f"{integrations.HOSTCTL}/core/extras{path}",
                                            headers={"X-Hostctl-Secret": integrations.HOSTCTL_SECRET})
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"The host helper isn't reachable ({type(exc).__name__})") from exc
    data = response.json() if response.content else {}
    if response.status_code >= 300:
        raise HTTPException(response.status_code, data.get("error") or f"Host helper error {response.status_code}")
    return data


@app.get(f"{PREFIX}/api/core/extras")
async def core_extras_view():
    return await hostctl_extras("GET")


@app.get(f"{PREFIX}/api/hermes/addons")
async def hermes_addons_view():
    return await hostctl_extras("GET", "/hermes-addons")


@app.get(f"{PREFIX}/api/hermes/addons/search/{{kind}}")
async def hermes_addons_search(kind: str, q: str = ""):
    if kind not in {"skills", "plugins"} or len(q) > 120:
        raise HTTPException(400, "Invalid add-on search")
    return await hostctl_extras("GET", f"/hermes-addons/search/{kind}?q={quote(q, safe='')}")


@app.get(f"{PREFIX}/api/hermes/addons/catalog/{{kind}}")
async def hermes_addons_catalog(kind: str, q: str = ""):
    if kind not in {"skills", "plugins"} or len(q) > 120:
        raise HTTPException(400, "Invalid add-on catalog request")
    return await hostctl_extras("GET", f"/hermes-addons/catalog/{kind}?q={quote(q, safe='')}")


@app.get(f"{PREFIX}/api/hermes/addons/log")
async def hermes_addons_log():
    return await hostctl_extras("GET", "/hermes-addons/log")


@app.post(f"{PREFIX}/api/hermes/addons/install/{{kind}}/{{identifier:path}}")
async def hermes_addons_install(kind: str, identifier: str):
    if kind not in {"skills", "plugins"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~:/?#=&%+@-]{0,499}", identifier):
        raise HTTPException(400, "Invalid add-on identifier")
    return await hostctl_extras("POST", f"/hermes-addons/install/{kind}/{quote(identifier, safe='')}")


@app.get(f"{PREFIX}/api/core/extras/log/{{job}}")
async def core_extras_log(job: str):
    if job not in {"comfy-download", "comfy-update", "comfy-supervisor", "trainer-install", "flash-install", "hermes-download", "hermes-update", "personal-hermes-update", "motion-install", "rea-install", "rea-update", "rea-godot-download", "rea-godotsteam-download", "rea-love2d-download", "rea-tic80-download", "rea-godotpcktool-download", "ytdlp-install"}:
        raise HTTPException(404, "Unknown job")
    return await hostctl_extras("GET", f"/log/{job}")


@app.post(f"{PREFIX}/api/core/extras/check")
async def core_extras_check():
    return await hostctl_extras("POST", "/check")


@app.post(f"{PREFIX}/api/core/extras/{{job}}")
async def core_extras_action(job: str):
    if job not in {"comfy-download", "comfy-update", "comfy-supervisor", "trainer-install", "flash-install", "hermes-download", "hermes-update", "personal-hermes-update", "motion-install", "rea-install", "rea-update", "rea-godotsteam-download", "rea-godotsteam-uninstall", "rea-godotsteam-cache-uninstall", "rea-love2d-download", "rea-love2d-uninstall", "rea-tic80-download", "rea-tic80-uninstall", "rea-godotpcktool-download", "rea-godotpcktool-uninstall", "ytdlp-install"}:
        raise HTTPException(404, "Unknown job")
    return await hostctl_extras("POST", f"/{job}")


@app.post(f"{PREFIX}/api/core/extras/{{job}}/{{version}}")
async def core_extras_version_action(job: str, version: str):
    if job not in {"rea-godot-download", "rea-godot-uninstall", "rea-godot-cache-uninstall"} or not re.fullmatch(r"(?:3|4)\.\d+(?:\.\d+)?", version):
        raise HTTPException(404, "Unknown engine or version")
    return await hostctl_extras("POST", f"/{job}/{version}")


async def hostctl_swap(method: str, path: str = "", body: dict | None = None) -> dict:
    if not integrations.HOSTCTL_SECRET:
        raise HTTPException(503, "The host helper (hostctl) isn't configured")
    try:
        async with httpx.AsyncClient(timeout=900 if path == "/activate" else 60) as client:
            response = await client.request(method, f"{integrations.HOSTCTL}/core/swap{path}", json=body,
                                            headers={"X-Hostctl-Secret": integrations.HOSTCTL_SECRET})
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"The host helper isn't reachable ({type(exc).__name__})") from exc
    data = response.json() if response.content else {}
    if response.status_code >= 300:
        raise HTTPException(response.status_code, data.get("error") or f"Host helper error {response.status_code}")
    return data


@app.get(f"{PREFIX}/api/core/swap")
async def core_swap_view():
    return await hostctl_swap("GET")


@app.get(f"{PREFIX}/api/core/swap/log/{{build_id}}")
async def core_swap_log(build_id: str):
    if not re.fullmatch(r"v[0-9]+-rocsy-[0-9a-f]{8}", build_id):
        raise HTTPException(404, "Unknown build")
    return await hostctl_swap("GET", f"/log/{build_id}")


@app.post(f"{PREFIX}/api/core/swap/{{action}}")
async def core_swap_action(action: str, body: dict):
    if action not in {"fetch", "build", "activate", "cancel", "delete"}:
        raise HTTPException(404, "Unknown action")
    return await hostctl_swap("POST", f"/{action}", body)

@app.get(f"{PREFIX}/api/llama-benchmark/install/status")
async def core_benchmark_install_status():
    return await hostctl_core("GET", "/install/status", base="/llama-benchmark")


@app.post(f"{PREFIX}/api/llama-benchmark/{{action}}")
async def core_benchmark_manage(action: str):
    if action not in ("install", "uninstall"):
        raise HTTPException(404, "Unknown benchmark action")
    return await hostctl_core("POST", f"/{action}", {}, base="/llama-benchmark")


@app.get(f"{PREFIX}/api/core/voice")
async def core_voice_view():
    return await hostctl_core("GET", "", base="/core/voice")


@app.get(f"{PREFIX}/api/core/voice/{{engine}}/log")
async def core_voice_log(engine: str):
    if engine not in {"qwen3", "qwen3_design", "indextts2", "fish_s2", "fish_drama3"}:
        raise HTTPException(404, "Unknown voice engine")
    return await hostctl_core("GET", f"/{engine}/log", base="/core/voice")


@app.post(f"{PREFIX}/api/core/voice/{{engine}}/install")
async def core_voice_install(engine: str):
    if engine not in {"qwen3", "qwen3_design", "indextts2", "fish_s2", "fish_drama3"}:
        raise HTTPException(404, "Unknown voice engine")
    return await hostctl_core("POST", f"/{engine}/install", {}, base="/core/voice")


@app.get(f"{PREFIX}/api/core/voice/indextts2/deepspeed/log")
async def core_voice_deepspeed_log():
    return await hostctl_core("GET", "/indextts2/deepspeed/log", base="/core/voice")


@app.post(f"{PREFIX}/api/core/voice/indextts2/deepspeed")
async def core_voice_deepspeed_install():
    return await hostctl_core("POST", "/indextts2/deepspeed", {}, base="/core/voice")


@app.get(f"{PREFIX}/api/core/voice/qwen3/flash/log")
async def core_voice_flash_log():
    return await hostctl_core("GET", "/qwen3/flash/log", base="/core/voice")


@app.post(f"{PREFIX}/api/core/voice/qwen3/flash")
async def core_voice_flash_install():
    return await hostctl_core("POST", "/qwen3/flash", {}, base="/core/voice")


@app.get(f"{PREFIX}/api/core/engines")
async def core_engines_view():
    return await hostctl_core("GET", "", base="/core/engines")


@app.get(f"{PREFIX}/api/core/engines/log")
async def core_engines_log():
    return await hostctl_core("GET", "/log", base="/core/engines")


@app.post(f"{PREFIX}/api/core/engines/{{action}}")
async def core_engines_action(action: str, body: dict):
    if action not in {"strata-source", "strata-install", "strata-start", "strata-stop", "rocmfpx-build", "rocmfpx-cancel", "hipblaslt-tune",
                      "strata-ngram", "strata-ngram-cancel"}:
        raise HTTPException(404, "Unknown action")
    return await hostctl_core("POST", f"/{action}", body, base="/core/engines")


CORE_ACTIONS = {"fetch", "build", "cancel", "activate", "delete"}
core_seen: dict[str, str] = {}  # build id -> last phase, for the "build finished" notification


async def hostctl_core(method: str, path: str, body: dict | None = None, base: str = "/core/llamacpp") -> dict:
    if not integrations.HOSTCTL_SECRET:
        raise HTTPException(503, "The host helper (hostctl) isn't configured")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request(method, f"{integrations.HOSTCTL}{base}{path}", json=body,
                                            headers={"X-Hostctl-Secret": integrations.HOSTCTL_SECRET})
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"The host helper isn't reachable ({type(exc).__name__})") from exc
    data = response.json() if response.content else {}
    if response.status_code >= 300:
        raise HTTPException(response.status_code, data.get("error") or f"Host helper error {response.status_code}")
    return data


def core_notify(builds: list[dict]) -> None:
    for build in builds:
        phase, before = build.get("phase"), core_seen.get(build.get("id", ""))
        core_seen[build.get("id", "")] = phase
        if before and before not in ("ready", "failed", "cancelled") and phase in ("ready", "failed"):
            name = f"{build.get('release')} + RDNA Boost {build.get('boost_short')}"
            notify("core-build", "llama.cpp build ready" if phase == "ready" else "llama.cpp build failed",
                   name + (" - choose Use this build in Manager -> Core Backend" if phase == "ready" else f": {build.get('error', '')}"))


async def core_watch() -> None:
    """Polls the host only while a build runs, so a finished build is announced even with no page open."""
    while True:
        await asyncio.sleep(30)
        if not any(p not in ("ready", "failed", "cancelled") for p in core_seen.values()):
            continue
        with contextlib.suppress(HTTPException):
            core_notify((await hostctl_core("GET", ""))["builds"])


@app.get(f"{PREFIX}/api/core/llamacpp")
async def core_view():
    data = await hostctl_core("GET", "")
    core_notify(data.get("builds", []))
    return data


@app.get(f"{PREFIX}/api/core/llamacpp/log/{{build_id}}")
async def core_log(build_id: str):
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", build_id):
        raise HTTPException(404, "Unknown build")
    return await hostctl_core("GET", f"/log/{build_id}")


class RuntimeBody(BaseModel):
    kind: str = ""      # rocm | vulkan
    version: str = ""   # a version, or "system" for the default actions


RUNTIME_ACTIONS = {"refresh", "install", "radv-update", "cancel", "delete", "default"}


@app.get(f"{PREFIX}/api/core/runtime")
async def core_runtime_view():
    return await hostctl_core("GET", "", base="/core/runtime")


@app.get(f"{PREFIX}/api/core/runtime/log")
async def core_runtime_log():
    return await hostctl_core("GET", "/log", base="/core/runtime")


@app.post(f"{PREFIX}/api/core/runtime/{{action}}")
async def core_runtime_action(action: str, body: RuntimeBody):
    if action not in RUNTIME_ACTIONS:
        raise HTTPException(404, "Unknown action")
    return await hostctl_core("POST", f"/{action}", body.model_dump(), base="/core/runtime")


class CoreBody(BaseModel):
    release: str = ""
    boost: str = ""
    id: str = ""
    stock: bool = False   # plain upstream llama.cpp, never converted to RDNA Boost
    force: bool = False   # delete even though launch scripts reference the build


@app.post(f"{PREFIX}/api/core/llamacpp/{{action}}")
async def core_action(action: str, body: CoreBody):
    if action not in CORE_ACTIONS:
        raise HTTPException(404, "Unknown action")
    data = await hostctl_core("POST", f"/{action}", body.model_dump())
    if action == "build" and data.get("build"):
        core_seen[data["build"]["id"]] = data["build"].get("phase", "queued")
    return data


# ---- library / manager -------------------------------------------------------------

# ---- Core Backend → ComfyUI: missing default models, workflow models and custom nodes (comfysetup.py) ----

async def comfy_host() -> dict:
    """custom_nodes folders + node install job from the host, or {"error"} when hostctl is down."""
    try:
        return await hostctl_core("GET", "", base="/core/comfynodes")
    except HTTPException as exc:
        return {"error": exc.detail, "packs": None, "job": {}}


@app.get(f"{PREFIX}/api/comfy-setup")
async def comfy_setup_view(fresh: bool = False):
    host = await comfy_host()
    if fresh or not comfy_setup.last:
        await comfy_setup.scan(host.get("packs"))
    return {**comfy_setup.last, "host": host}


class SetupBody(BaseModel):
    keys: list[str] = []


@app.post(f"{PREFIX}/api/comfy-setup/download")
async def comfy_setup_download(body: SetupBody):
    # Only entries from the last scan: the URL and folder never come from the browser.
    entries = {item["key"]: item for item in comfy_setup.last.get("defaults", []) + comfy_setup.last.get("models", [])}
    picked = [entries[key] for key in dict.fromkeys(body.keys) if key in entries and entries[key].get("url")]
    if not picked:
        raise HTTPException(404, "Scan again: none of these files are listed as missing any more")
    jobs, errors = [], []
    for item in picked:
        link = hf_link(item["url"])
        try:
            jobs.append(downloads.add(url=item["url"], dest=item["dest"], name=item["name"], size=item.get("size") or 0,
                                      sha256=item.get("sha256", ""), source="hf" if link else "comfy-setup",
                                      group=f"comfy-setup:{item['key']}", link=link,
                                      after={"role": "comfy-setup"}))  # no studio registration
        except ValueError as exc:
            errors.append(f"{item['name']}: {exc}")
    return {"jobs": jobs, "errors": errors}


@app.post(f"{PREFIX}/api/comfy-setup/nodes")
async def comfy_setup_nodes(body: SetupBody):
    entries = {item["key"]: item for item in comfy_setup.last.get("nodes", [])}
    repos = [entries[key]["url"] for key in dict.fromkeys(body.keys) if key in entries]
    if not repos:
        raise HTTPException(404, "Scan again: none of these node packs are listed as missing any more")
    return await hostctl_core("POST", "/install", {"repos": repos}, base="/core/comfynodes")


@app.get(f"{PREFIX}/api/comfy-setup/log")
async def comfy_setup_log():
    return await hostctl_core("GET", "/log", base="/core/comfynodes")


@app.post(f"{PREFIX}/api/comfy-setup/restart")
async def comfy_setup_restart():
    return await hostctl_core("POST", "/restart", {}, base="/core/comfynodes")


# ---- Studios → Generate → missing model files dialog (required.py, shared/ui/missing-files.js) ----

class RequiredBody(BaseModel):
    files: list[dict] = []


async def _required_rows(files: list[dict]) -> list[dict]:
    async with httpx.AsyncClient(headers={"User-Agent": "Rocsy-ModelHub/1.0"}) as client:
        manager = await comfy_setup._manager_lists(client)
        by_file: dict[str, dict] = {}
        for entry in (manager.get("model-list.json") or {}).get("models") or []:
            by_file.setdefault(str(entry.get("filename", "")).lower(), entry)
        sources_ = [required.source(item["name"], library.links, by_file) for item in files]
        heads = await asyncio.gather(*(comfy_setup._head(client, src["url"]) if src.get("url") else asyncio.sleep(0, {})
                                       for src in sources_))
    try:
        found = (await hostctl_core("POST", "/find", {"names": [i["name"] for i in files]}, base="/models")).get("found") or {}
    except HTTPException as exc:
        log.warning("Disk search for missing files failed: %s", exc.detail)
        found = None
    rows = []
    for item, src, head in zip(files, sources_, heads):
        hits = (found or {}).get(item["name"].rsplit("/", 1)[-1]) or []
        row = required.plan(item, src, head or {}, hits, integrations.MODELS_HOST_DIR)
        row["searched"] = found is not None
        rows.append((row, src))
    return rows


@app.post(f"{PREFIX}/api/required-files")
async def required_files(body: RequiredBody):
    """What can be done about each missing file: link a copy found on disk, download it, or search."""
    files = required.clean(body.files)
    if not files:
        raise HTTPException(400, "No model files named")
    return {"files": [row for row, _ in await _required_rows(files)]}


class RequiredFetch(BaseModel):
    files: list[dict] = []  # [{name, folder, use: "link" | "download", path?}]


@app.post(f"{PREFIX}/api/required-files/fetch")
async def required_files_fetch(body: RequiredFetch):
    picks = {str(i.get("name", "")).replace("\\", "/").strip("/"): i for i in body.files if isinstance(i, dict)}
    files = required.clean(body.files)
    rows = {row["name"]: (row, src) for row, src in await _required_rows(files)}
    linked, jobs, errors = [], [], []
    for name, (row, src) in rows.items():
        pick = picks.get(name) or {}
        if not row["folder"]:
            errors.append(f"{name}: no ComfyUI folder is known for it")
            continue
        if pick.get("use") == "link":
            path = str(pick.get("path") or "")
            if path not in {hit["path"] for hit in row["found"]}:
                errors.append(f"{name}: that copy wasn't found on disk any more")
                continue
            try:
                done = await hostctl_core("POST", "/link", {"path": path, "folder": row["folder"], "name": name},
                                          base="/models")
                linked.append({"name": name, **done})
            except HTTPException as exc:
                errors.append(f"{name}: {exc.detail}")
        elif pick.get("use") == "download":
            if not src.get("url") or not row["download"]:
                errors.append(f"{name}: no download source is known")
                continue
            dest, filename = required.dest_parts(name, row["folder"])
            link = hf_link(src["url"])
            try:
                jobs.append(downloads.add(url=src["url"], dest=dest, name=filename, size=row["download"]["size"] or 0,
                                          sha256=src.get("sha256", ""), source="hf" if link else "required-files",
                                          group=f"required:{name.lower()}", link=link,
                                          after={"role": "required-files"}))  # no studio registration
            except ValueError as exc:
                errors.append(f"{name}: {exc}")
    if linked:
        await asyncio.to_thread(library.scan)
    return {"linked": linked, "jobs": [{"id": j["id"], "name": j["name"]} for j in jobs], "errors": errors}


@app.get(f"{PREFIX}/api/required-files/jobs")
async def required_files_jobs(ids: str = ""):
    wanted = [i for i in ids.split(",") if i][:32]
    return {"jobs": [{key: job.get(key) for key in ("id", "name", "state", "bytes", "size", "speed", "error", "retry")}
                     for job_id in wanted if (job := downloads.jobs.get(job_id))]}


# ---- Core Backend → ComfyUI → Custom nodes: browse/install any pack, detect and apply updates ----

NODE_CHECK_EVERY = 12 * 3600
node_updates = {"count": 0, "notified": set()}


def _remember_node_updates(updates: dict) -> list[str]:
    """Keep the badge count; return packs that are newly behind since the last notification."""
    behind = [p["dir"] for p in updates.get("packs", []) if p.get("status") == "behind"]
    node_updates["count"] = len(behind)
    new = [d for d in behind if d not in node_updates["notified"]]
    node_updates["notified"] = set(behind)
    return new


async def comfy_nodes_watch() -> None:
    """Checks only (never updates): every 12 h when update checks are on, like the model checks."""
    await asyncio.sleep(90)
    first = True
    while True:
        with contextlib.suppress(HTTPException):
            host = await hostctl_core("GET", "", base="/core/comfynodes")
            updates = host.get("updates") or {}
            if load_settings()["auto_check"] and time.time() - (updates.get("checked_at") or 0) > NODE_CHECK_EVERY \
                    and not (host.get("job") or {}).get("running"):
                updates = await hostctl_core("POST", "/check", {}, base="/core/comfynodes")
            new = _remember_node_updates(updates)
            if new and not first:
                notify("node-updates", f"{len(new)} ComfyUI node pack update{'s' if len(new) != 1 else ''} available",
                       ", ".join(new[:3]) + ("…" if len(new) > 3 else ""))
            first = False
        await asyncio.sleep(1800)


def _installed_keys(packs: list | None) -> set[str]:
    return {comfysetup.repo_key(p.get("remote", "")) for p in packs or []} - {""}


@app.get(f"{PREFIX}/api/comfy-nodes")
async def comfy_nodes_view():
    host = await comfy_host()
    if host.get("updates"):
        _remember_node_updates(host["updates"])
    # Titles/authors from ComfyUI-Manager's list, by repo.
    catalog = {item["key"]: item for item in await comfy_setup.catalog()}
    status = {item["dir"]: item for item in (host.get("updates") or {}).get("packs", [])}
    packs = []
    for pack in host.get("packs") or []:
        info = catalog.get(comfysetup.repo_key(pack.get("remote", ""))) or {}
        packs.append({**pack, **status.get(pack["dir"], {}), "title": info.get("title", ""),
                      "author": info.get("author", ""), "description": info.get("description", "")})
    return {"packs": packs, "job": host.get("job") or {}, "error": host.get("error"),
            "checked_at": (host.get("updates") or {}).get("checked_at")}


@app.get(f"{PREFIX}/api/comfy-nodes/catalog")
async def comfy_nodes_catalog(q: str = "", limit: int = 40):
    words = [w for w in re.split(r"\s+", q.lower().strip()) if w][:6]
    if not words:
        return {"results": [], "total": 0}
    installed = _installed_keys((await comfy_host()).get("packs"))
    scored = []
    for item in await comfy_setup.catalog():
        title, rest = item["title"].lower(), f"{item['key']} {item['author']} {item['description']}".lower()
        if all(w in title or w in rest for w in words):
            score = sum(3 if w in title else 1 for w in words) + (2 if title.startswith(words[0]) else 0)
            scored.append((-score, item["title"].lower(), item))
    scored.sort(key=lambda row: row[:2])
    results = [{**item, "installed": item["key"] in installed} for _, _, item in scored[:max(1, min(limit, 100))]]
    return {"results": results, "total": len(scored)}


class NodeReposBody(BaseModel):
    repos: list[str] = []


class NodeDirsBody(BaseModel):
    dirs: list[str] = []


@app.post(f"{PREFIX}/api/comfy-nodes/install")
async def comfy_nodes_install(body: NodeReposBody):
    repos = [r.strip() for r in body.repos if comfysetup.repo_key(r)]
    if not repos or len(repos) != len(body.repos):
        raise HTTPException(400, "Use github.com repository links, e.g. https://github.com/owner/ComfyUI-Pack")
    return await hostctl_core("POST", "/install", {"repos": repos}, base="/core/comfynodes")


@app.post(f"{PREFIX}/api/comfy-nodes/check")
async def comfy_nodes_check():
    updates = await hostctl_core("POST", "/check", {}, base="/core/comfynodes")
    _remember_node_updates(updates)
    return {"available": updates.get("available", 0), "checked_at": updates.get("checked_at")}


@app.post(f"{PREFIX}/api/comfy-nodes/update")
async def comfy_nodes_update(body: NodeDirsBody):
    return await hostctl_core("POST", "/update", {"dirs": body.dirs}, base="/core/comfynodes")


@app.get(f"{PREFIX}/api/library")
async def library_view():
    return {"items": library.view(), "task": library.task, "last_check": library.last_check}


@app.post(f"{PREFIX}/api/library/scan")
async def library_scan():
    await asyncio.to_thread(library.scan)
    return await library_view()


class PathsBody(BaseModel):
    paths: list[str] | None = None
    compute: bool = False


@app.post(f"{PREFIX}/api/library/identify")
async def library_identify(body: PathsBody):
    known = {i["path"] for i in library.items}
    paths = [p for p in (body.paths or [i["path"] for i in library.view() if i["status"] in ("unknown", "not_found")]) if p in known]
    asyncio.create_task(library.identify(paths, body.compute))
    return {"started": len(paths)}


class TraceBody(BaseModel):
    links: dict[str, dict]


@app.post(f"{PREFIX}/api/internal/library/traces")
async def library_traces(body: TraceBody, request: Request):
    """Links found from traces on this machine (download history, studio catalogs, how a file was
    made), applied in one go. Loopback only, never through the tunnel; each link is checked."""
    if request.headers.get("cf-connecting-ip") or (request.client and request.client.host not in ("127.0.0.1", "::1")):
        raise HTTPException(403, "forbidden")
    known = {i["path"] for i in library.items}
    applied = []
    for rel, link in body.links.items():
        if rel not in known or link.get("source") not in ("hf", "civitai", "local"):
            continue
        if link["source"] == "hf" and not (link.get("repo") and link.get("file")):
            continue
        if link["source"] == "civitai" and not (link.get("model_id") and link.get("version_id")):
            continue
        keep = {k: v for k, v in link.items() if k in ("source", "repo", "file", "name", "revision", "model_id", "version_id",
                "version_name", "base", "nsfw", "linked_by", "local_changes", "origin", "note", "derived_from")}
        library.link(rel, keep)
        applied.append(rel)
    asyncio.create_task(library.check_updates(applied))
    return {"applied": len(applied)}


@app.post(f"{PREFIX}/api/library/check")
async def library_check(body: PathsBody):
    asyncio.create_task(library.check_updates(body.paths))
    return {"started": True}


class LinkBody(BaseModel):
    path: str
    url: str


@app.post(f"{PREFIX}/api/library/link")
async def library_link(body: LinkBody):
    if body.path not in {i["path"] for i in library.items}:
        raise HTTPException(404, "Unknown file")
    parsed = urlparse(body.url.strip())
    host = (parsed.hostname or "").removeprefix("www.")
    if host in ("civitai.com", "civitai.red"):
        match = re.search(r"/models/(\d+)", parsed.path)
        if not match:
            raise HTTPException(422, "Paste a civitai.com/models/<id> link")
        detail = await fetch_detail("civitai", match.group(1))
        version_id = (parse_qs(parsed.query).get("modelVersionId") or [""])[0] or detail["versions"][0]["id"]
        version = next((v for v in detail["versions"] if v["id"] == version_id), detail["versions"][0])
        link = {"source": "civitai", "model_id": detail["id"], "version_id": version["id"], "version_name": version["name"],
                "name": detail["name"], "base": version["base"], "nsfw": host == "civitai.red", "linked_by": "you"}
    elif host == "huggingface.co":
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2:
            raise HTTPException(422, "Paste a huggingface.co/<user>/<repo> link")
        repo = "/".join(parts[:2])
        file = "/".join(parts[4:]) if len(parts) > 4 and parts[2] in ("blob", "resolve") else Path(body.path).name
        link = {"source": "hf", "repo": repo, "file": file, "name": repo, "revision": "", "linked_by": "you"}
    else:
        raise HTTPException(422, "Paste a Civitai or Hugging Face link")
    library.link(body.path, link)
    asyncio.create_task(library.check_updates([body.path]))
    return {"link": link}


class UpdateBody(BaseModel):
    path: str
    keep_name: bool = True
    variant: str = ""


@app.post(f"{PREFIX}/api/library/update")
async def library_update(body: UpdateBody):
    """Apply one update (the user clicked Update): download the newest version."""
    link = library.links.get(body.path)
    update = (link or {}).get("update") or {}
    if not link or not update.get("available"):
        raise HTTPException(409, "No update to apply - check again")
    if link["source"] == "hf":
        detail = await fetch_detail("hf", link["repo"])
        version = detail["versions"][0]
        variant = next((v for v in version["variants"] for f in v["files"] if f["name"] == link["file"]), None)
        if not variant:
            raise HTTPException(404, "The file is no longer in the repo")
        model_id, source = link["repo"], "hf"
    else:
        source = "civitai_red" if link.get("nsfw") else "civitai"
        detail = await fetch_detail(source, link["model_id"])
        version = next((v for v in detail["versions"] if v["id"] == update["version_id"]), None)
        if not version:
            raise HTTPException(404, "That version is no longer listed")
        variant = next((v for v in version["variants"] if v["key"] == body.variant), None) or \
            next((v for v in version["variants"] if v["primary"]), None) or version["variants"][0]
        model_id = link["model_id"]
    return await queue_download(DownloadBody(source=source, model_id=model_id, version_id=version["id"], variant=variant["key"],
                                             replace=body.path if body.keep_name else "", companions=False,
                                             dest="" if body.keep_name else str(Path(body.path).parent)),
                                supersede=body.path)


@app.delete(f"{PREFIX}/api/library/old")
async def library_remove_old(path: str):
    """Delete the previous version an in-place update kept as <name>.old."""
    link = ROOT / path
    old = link.resolve()
    if path.endswith(".old") and ".." not in Path(path).parts and locations.moved_target(link):
        locations.remove(link)                   # a moved file's backup: the link and its file
        return {"deleted": path}
    if not path.endswith(".old") or not str(old).startswith(str(ROOT.resolve()) + os.sep) or not old.is_file():
        raise HTTPException(404, "No previous version there")
    old.unlink()
    return {"deleted": path}


@app.get(f"{PREFIX}/api/library/olds")
async def library_olds():
    found = []
    for folder in {i["folder"] for i in library.items}:
        for path in (ROOT / folder).rglob("*.old"):
            with contextlib.suppress(OSError):
                found.append({"path": str(path.relative_to(ROOT)), "size": path.stat().st_size})
    return {"olds": found}


# ---- installed / uninstall ------------------------------------------------------------

SUPPORT = {"text_encoders": "Text encoders & VAEs", "vae": "Text encoders & VAEs", "clip_vision": "Text encoders & VAEs",
           "audio_encoders": "Text encoders & VAEs", "vae_approx": "Text encoders & VAEs"}
GROUP_ORDER = ["LLMs", "Decision & classification models", "Video models", "Image models", "Audio models", "LoRAs", "Text encoders & VAEs",
               "Upscalers & other", "Other models"]


def _base_of(item: dict) -> str:
    link = library.links.get(item["path"]) or {}
    manual = link.get("base_manual") or ""
    base = manual or (link.get("base") if link.get("base") not in (None, "", "Other") else "")
    from library import _guess_base
    return base or _guess_base(item["name"])


@app.get(f"{PREFIX}/api/installed")
async def installed(unused_days: int = 7):
    await locations.refresh()
    lora_uses = await integrations.lora_usage()
    now, window = time.time(), max(1, min(365, unused_days)) * 86400
    llama = integrations.llama_models()
    rows = []
    for item in library.view():
        folder, base = item["folder"], _base_of(item)
        uses = []
        if folder == "loras":
            group = "LoRAs"
            uses = [{"label": u["label"], "kind": "gallery", "managed": bool(u.get("custom_id"))} for u in lora_uses.get(item["path"], [])]
            sub = (lora_uses.get(item["path"]) or [{}])[0].get("label") or (f"{base} (no studio gallery)" if base else "Not in a studio gallery")
        elif folder == "llm":
            group = "LLMs"
            host = integrations.host_path(item["path"])
            uses = [{"label": f"llama-swap: {m['name']} ({m['id']})", "kind": "llama", "managed": m["managed"]}
                    for m in llama if host in (m["model"], m["mmproj"])]
            sub = "Vision projector" if "mmproj" in item["name"].lower() else item["path"].split("/")[1]
        elif folder == "classifiers":
            group, sub = "Decision & classification models", item["path"].split("/")[1] if len(item["path"].split("/")) > 2 else "Other classifiers"
        elif folder in SUPPORT:
            group, sub = SUPPORT[folder], folder.replace("_", " ")
        elif folder in ("diffusion_models", "checkpoints", "unet", "diffusers"):
            category = sources.civitai_category(base, "") if base else ""
            group = {"video": "Video models", "image": "Image models", "audio": "Audio models"}.get(category, "Other models")
            sub = base or folder.replace("_", " ")
        else:
            group, sub = "Upscalers & other", folder.replace("_", " ")
        seen = usage.info(item["path"], folder)
        link = item.get("link") or {}
        rows.append({"path": item["path"], "name": item["name"], "size": item["size"], "group": group, "sub": sub,
                     "base": base, "base_manual": link.get("base_manual", ""),
                     "uses": uses, "status": item["status"], "source": link.get("source", ""),
                     "added": item["mtime"], **seen, "stored": locations.describe(item["path"]),
                     "offline": bool(item.get("offline")),
                     # Unused for UNUSED_DAYS: not used in that window, not newly added, and tracked long enough to know.
                     "unused": (now - seen["last_used"] > window and now - item["mtime"] > window
                                and bool(seen["tracked_since"]) and now - seen["tracked_since"] >= window)})
    # Laya lives outside ComfyUI's model tree. Show its installed checkpoints as
    # read-only entries so they are visible without pretending llama-swap loads them.
    for subdir, label in (("", "English"), ("multilingual", "Multilingual"), ("typed-decisions", "Typed decisions")):
        checkpoint = LAYA_MODELS / subdir / "model.safetensors"
        try:
            if not checkpoint.is_file() or checkpoint.is_symlink():
                continue
            size = checkpoint.stat().st_size
        except OSError:
            continue
        rows.append({"path": f"laya/{subdir or 'english'}/model.safetensors", "name": f"Laya · {label}",
                     "location": str(LAYA_HOST / subdir / "model.safetensors"), "size": size, "group": "Decision & classification models",
                     "sub": "Laya", "base": "", "base_manual": "", "uses": [], "status": "", "source": "local",
                     "added": checkpoint.stat().st_mtime, "last_used": 0, "tracked_since": 0,
                     "usage_note": "Installed locally", "unused": False, "removable": False})
    groups = []
    for name in GROUP_ORDER:
        members = sorted((r for r in rows if r["group"] == name), key=lambda r: (r["sub"].casefold(), r["name"].casefold()))
        if members:
            groups.append({"name": name, "count": len(members), "size": sum(r["size"] for r in members), "items": members})
    return {"groups": groups, "free": hw_cache.get("disks", {}).get("Models folder", {}).get("free", 0),
            "llama_restart": {"pending": restarter.pending}, "unused_days": window // 86400,
            "tracking": {"comfy": usage.since.get("comfy", 0), "llama": usage.since.get("llama", 0), "scanning": usage.busy}}


# ---- storage locations (Settings -> Storage; moves run on the host: hostctl/storage.py) ----

class MoveBody(BaseModel):
    path: str
    to: str


@app.get(f"{PREFIX}/api/storage")
async def storage_view():
    data = await hostctl_core("GET", "", base="/storage")
    locations.snapshot["jobs"] = data.get("jobs", [])
    return data


@app.post(f"{PREFIX}/api/storage/move")
async def storage_move(body: MoveBody):
    """Move an installed file (and its shards) to another storage location; models/ keeps a link to it."""
    item = next((i for i in library.items if i["path"] == body.path), None)
    if not item:
        raise HTTPException(404, "Not an installed file")
    if any(job["state"] in ("queued", "downloading", "verifying") and job.get("path") == body.path for job in downloads.jobs.values()):
        raise HTTPException(409, "It is still downloading")
    job = await hostctl_core("POST", "/move", {"path": body.path, "to": body.to}, base="/storage")
    asyncio.create_task(locations.refresh())
    return job


@app.post(f"{PREFIX}/api/storage/move/{{job_id}}/cancel")
async def storage_move_cancel(job_id: str):
    return await hostctl_core("POST", "/move/cancel", {"id": job_id}, base="/storage")


class UninstallBody(BaseModel):
    path: str
    force: bool = False


SIDECARS = (".sha256", ".old")
STEM_SIDECARS = (".metadata.json", ".civitai.info", ".preview.png", ".preview.jpg", ".preview.webp", ".json")


@app.post(f"{PREFIX}/api/uninstall")
async def uninstall(body: UninstallBody):
    item = next((i for i in library.items if i["path"] == body.path), None)
    if not item:
        raise HTTPException(404, "Not an installed file")
    if any(job["state"] in ("queued", "downloading", "verifying") and job.get("path") == body.path for job in downloads.jobs.values()):
        raise HTTPException(409, "It is still downloading")
    path = ROOT / item["path"]
    notes, removed = [], []
    freed = 0
    moved = locations.moved_target(path)
    if item["folder"] == "loras":
        uses = (await integrations.lora_usage()).get(item["path"], [])
        built_in = [u["label"] for u in uses if not u.get("custom_id")]
        if built_in and not body.force:
            raise HTTPException(409, f"Built into the {', '.join(built_in)} list: its picker slot will stop working. "
                                     "Uninstall anyway?")
        for use in uses:
            if use.get("custom_id"):
                await integrations.delete_custom_lora(use["studio"], use["custom_id"])  # removes file, entry, previews
                notes.append(f"removed from the {use['label']}")
                if not path.exists() and not freed:
                    freed, _ = item["size"], removed.append(item["path"])
                if moved is not None and not path.is_symlink() and moved.is_file():
                    moved.unlink()               # the studio removed the link; its file in the location goes too
    if item["folder"] == "llm":
        host = integrations.host_path(item["path"])
        refs = [m for m in integrations.llama_models() if host in (m["model"], m["mmproj"])]
        hand_written = [m["id"] for m in refs if not m["managed"]]
        if hand_written and not body.force:
            raise HTTPException(409, f"llama-swap entry {', '.join(hand_written)} uses this file and was written by hand, "
                                     "so it stays in config.yaml (and fails if selected). Uninstall anyway?")
        for model in refs:
            try:
                await integrations.delete_llama_hipblaslt_tuning(model["id"])
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                raise HTTPException(409, f"Remove {model['id']}'s tuning data before uninstalling: {exc}") from exc
        for model in refs:
            if model["managed"] and await asyncio.to_thread(integrations.remove_llama_model, model["id"]):
                notes.append(f"llama-swap entry {model['id']} removed")
                restarter.request(f"removed {model['id']}")
    # The file itself (+ shards, + sidecars the LoRA Manager / we wrote).
    targets = [path]
    shard = re.search(r"-00001-of-(\d{5})\.(gguf|safetensors)$", path.name)
    if shard:
        targets += [path.with_name(path.name.replace("-00001-of-", f"-{n:05d}-of-")) for n in range(2, int(shard.group(1)) + 1)]
    for target in list(targets):
        targets += [target.with_name(target.name + suffix) for suffix in SIDECARS]
        targets += [target.with_name(target.stem + suffix) for suffix in STEM_SIDECARS]
    for target in targets:
        with contextlib.suppress(OSError):
            if target.is_file() or target.is_symlink():
                if target.is_symlink() and not target.exists():
                    notes.append(f"{target.name}'s drive is offline: its file there stays")
                freed += locations.remove(target)
                removed.append(str(target.relative_to(ROOT)))
    # An LLM folder left with only its vision projector goes too.
    if item["folder"] == "llm" and path.parent != ROOT / "llm":
        rest = [p for p in path.parent.glob("*.gguf")]
        if rest and all("mmproj" in p.name.lower() for p in rest):
            projector_refs = [m for m in integrations.llama_models()
                              if any(integrations.host_path(str(p.relative_to(ROOT))) == m["mmproj"] for p in rest)]
            if not projector_refs:
                for leftover in rest:
                    freed += leftover.stat().st_size
                    leftover.unlink()
                    removed.append(str(leftover.relative_to(ROOT)))
        with contextlib.suppress(OSError):
            path.parent.rmdir()  # only if now empty
    library.links.pop(item["path"], None)
    await asyncio.to_thread(library.scan)
    return {"removed": removed, "freed": freed, "notes": notes}


# ---- manual base association ---------------------------------------------------

class RebaseBody(BaseModel):
    path: str
    base: str = ""  # "" clears a manual association (back to the link / filename guess)


# Bases a user may associate by hand: everything the app already knows by name.
KNOWN_BASES = (set(sources.VIDEO_BASES) | set(sources.AUDIO_BASES) | set(sources.THREE_D_BASES)
               | set(sources.HF_BASE_REPOS) | {base for _pattern, base in BASE_PATTERNS})
MOVEABLE_MODEL_FOLDERS = ("checkpoints", "diffusion_models")


def _move_model_file(item: dict, dest: str) -> str:
    """Move a model file (plus shards and sidecars) to another folder under models/."""
    old = ROOT / item["path"]
    targets = [old]
    shard = re.search(r"-00001-of-(\d{5})\.(gguf|safetensors)$", old.name)
    if shard:
        targets += [old.with_name(old.name.replace("-00001-of-", f"-{n:05d}-of-")) for n in range(2, int(shard.group(1)) + 1)]
    for target in list(targets):
        targets += [target.with_name(target.name + suffix) for suffix in SIDECARS]
        targets += [target.with_name(target.stem + suffix) for suffix in STEM_SIDECARS]
    folder = ROOT / dest
    clash = next((t.name for t in targets if (folder / t.name).exists()), None)
    if clash:
        raise HTTPException(409, f"models/{dest}/{clash} already exists")
    folder.mkdir(parents=True, exist_ok=True)
    for target in targets:
        with contextlib.suppress(OSError):
            if target.is_file():
                target.replace(folder / target.name)
    return f"{dest}/{old.name}"


@app.post(f"{PREFIX}/api/installed/base")
async def set_base(body: RebaseBody):
    """Associate an installed file with a base model by hand (e.g. an "Other models" file that
    is really a MiniMax H3 checkpoint). Model files also move to the folder that base belongs
    in (checkpoints <-> diffusion_models), which is what the studios scan."""
    item = next((i for i in library.items if i["path"] == body.path), None)
    if not item:
        raise HTTPException(404, "Not an installed file")
    if any(job["state"] in ("queued", "downloading", "verifying") and job.get("path") == body.path for job in downloads.jobs.values()):
        raise HTTPException(409, "It is still downloading")
    base = " ".join(body.base.split())[:60]
    if base:
        canonical = {b.casefold(): b for b in KNOWN_BASES | {b["base"] for b in library.installed_bases()}}.get(base.casefold())
        if canonical is None:
            raise HTTPException(422, f"Unknown base model: {base}")
        base = canonical
    # Move first: a conflict there fails the whole request without recording anything.
    moved = ""
    if base and item["folder"] in MOVEABLE_MODEL_FOLDERS:
        category = sources.civitai_category(base, "")
        dest = sources.suggest_dest("model", category, "Checkpoint", base, item["name"])
        if dest != item["folder"]:
            moved = _move_model_file(item, dest)
    link = dict(library.links.get(item["path"]) or {"source": ""})
    if base:
        link["base_manual"] = base
    else:
        link.pop("base_manual", None)
    if link.get("source") or link.get("base_manual") or link.get("not_found"):
        library.links[item["path"]] = link
    else:
        library.links.pop(item["path"], None)
    if moved:
        library.links[moved] = library.links.pop(item["path"])
        if item["path"] in library.hashes:
            library.hashes[moved] = library.hashes.pop(item["path"])
        if item["path"] in usage.last:
            usage.last[moved] = usage.last.pop(item["path"])
            usage.save()
    library.save()
    await asyncio.to_thread(library.scan)
    notes = []
    final_path = moved or item["path"]
    if base and Path(final_path).parts[0] in integrations.DIFFUSION_DIRS:
        try:  # Image keeps a filename -> family registry for its checkpoint picker
            notes.append(await integrations.register_checkpoint({"name": Path(final_path).name}, base))
        except Exception as exc:  # noqa: BLE001 - the association itself already succeeded
            notes.append(f"Image didn't list it: {str(exc)[:120]}")
    if moved and base == "MiniMax H3":
        with contextlib.suppress(httpx.HTTPError):  # Video re-reads diffusion_models on demand
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{integrations.VID}/api/checkpoints/rescan")
            notes.append("Video re-scanned its checkpoint folder")
    return {"path": final_path, "base": base, "moved": bool(moved), "notes": [n for n in notes if n]}


# ---- thumbnails (Civitai images go through here: the site CSP is same-origin) ---------

THUMB_HOSTS = ("image.civitai.com", "imagecache.civitai.com", "image-b2.civitai.com", "huggingface.co",
               "cdn-avatars.huggingface.co", "cdn-uploads.huggingface.co", "github.com")
LOGOS = DATA / "logos.json"      # HF org -> avatar URL


@app.get(f"{PREFIX}/api/hermes/catalog-image")
async def hermes_catalog_image(u: str):
    """Fetch pinned plugin art through GitHub's API, which is faster here than the raw CDN."""
    parsed = urlparse(u)
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/([0-9a-f]{40})/(.+\.(png|jpe?g|webp|gif))",
                         parsed.path, re.I)
    if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com" or not match:
        raise HTTPException(404, "Not a pinned catalog image")
    owner, repo, revision, rel, ext = match.groups()
    if ".." in rel.split("/") or len(rel) > 240:
        raise HTTPException(404, "Invalid catalog image path")
    cache = DATA / "hermes-icons"
    cache.mkdir(exist_ok=True)
    target = cache / hashlib.sha256(u.encode()).hexdigest()
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif"}[ext.lower()]
    if target.is_file():
        return FileResponse(target, media_type=mime, headers=THUMB_HEADERS)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(rel, safe='/')}?ref={revision}"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(api_url, headers={"Accept": "application/vnd.github+json", **sources.UA})
            response.raise_for_status()
            payload = response.json()
        if payload.get("encoding") != "base64" or int(payload.get("size") or 0) > 2 * 1024**2:
            raise ValueError("Catalog image is too large")
        content = base64.b64decode("".join(payload["content"].split()), validate=True)
        if len(content) > 2 * 1024**2 or not (
                content.startswith(b"\x89PNG\r\n\x1a\n") or content.startswith(b"\xff\xd8\xff") or
                content.startswith(b"RIFF") and content[8:12] == b"WEBP" or
                content.startswith((b"GIF87a", b"GIF89a"))):
            raise ValueError("Catalog image data is invalid")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(404, "Catalog image unavailable") from exc
    target.write_bytes(content)
    return FileResponse(target, media_type=mime, headers=THUMB_HEADERS)


@app.get(f"{PREFIX}/api/logo/{{org}}")
async def logo(org: str):
    """A lab's Hugging Face avatar (Qwen, mistralai, ...) for its models' cards; cached like thumbs."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", org):
        raise HTTPException(404, "Not an organization")
    try:
        known = json.loads(LOGOS.read_text())
    except (OSError, ValueError):
        known = {}
    if org not in known:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(f"{sources.HF}/api/organizations/{org}/avatar", headers=sources.UA)
                if response.status_code == 404:        # a person, not an organization: remembered as "no logo"
                    known[org] = ""
                else:
                    response.raise_for_status()
                    known[org] = response.json().get("avatarUrl") or ""
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(404, "Logo unavailable") from exc
        LOGOS.write_text(json.dumps(known))
    if not known[org]:
        raise HTTPException(404, "No logo")
    return await thumb(known[org])


def thumb_url(url: str, still: bool = False, wide: bool = False) -> str:
    return f"{PREFIX}/api/thumb?u={quote(url, safe='')}" + ("&still=1" if still else "") + ("&wide=1" if wide else "")


def poster_url(url: str, kind: str) -> str:
    """A still first frame for a Civitai video preview (~90 KB vs ~1.2 MB): cards show it until the video plays."""
    host = urlparse(url).hostname or ""
    return thumb_url(url, still=True) if kind == "video" and host.endswith("civitai.com") else ""


THUMB_CACHE_BYTES = 2 * 1024**3       # pruned oldest-first past this
THUMB_MAX_CACHED = 64 * 1024**2       # bigger previews (some HF READMEs embed 100 MB+ GIFs) stream through uncached
THUMB_HEADERS = {"Cache-Control": "private, max-age=604800, immutable"}  # the key is the source URL
thumb_client: httpx.AsyncClient | None = None
avatar_client: httpx.AsyncClient | None = None
thumbs_inflight: set[str] = set()
thumbs_pruned_at = 0.0


def _thumb_client() -> httpx.AsyncClient:
    global thumb_client
    if thumb_client is None:  # pooled: a page of cards reuses a few TLS connections instead of one handshake per image
        thumb_client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(20, read=60),
                                         limits=httpx.Limits(max_connections=24, max_keepalive_connections=12))
    return thumb_client


def _avatar_client() -> httpx.AsyncClient:
    global avatar_client
    if avatar_client is None:
        avatar_client = httpx.AsyncClient(follow_redirects=True, timeout=8,
                                          limits=httpx.Limits(max_connections=12, max_keepalive_connections=6))
    return avatar_client


def _prune_thumbs() -> None:
    files = []
    for path in (DATA / "thumbs").iterdir():
        if path.suffix in (".type", ".part"):
            continue
        with contextlib.suppress(OSError):
            stat = path.stat()
            files.append((stat.st_mtime, stat.st_size, path))
    total = sum(size for _, size, _ in files)
    for _, size, path in sorted(files):
        if total <= THUMB_CACHE_BYTES:
            break
        path.unlink(missing_ok=True)
        path.with_suffix(".type").unlink(missing_ok=True)
        total -= size


@app.get(f"{PREFIX}/api/thumb")
async def thumb(u: str, request: Request = None, still: bool = False, wide: bool = False):
    parsed = urlparse(u)
    if parsed.scheme != "https" or parsed.hostname not in THUMB_HOSTS:
        raise HTTPException(404, "Not an allowed image host")
    if parsed.hostname == "github.com" and not re.fullmatch(r"/[A-Za-z0-9-]+\.png", parsed.path):
        raise HTTPException(404, "Not a GitHub avatar")
    # Ask Civitai's image CDN for a small rendition instead of the original (still=1: first frame as a JPEG).
    # wide=1: the model page's main gallery view.
    size = ("anim=false," if still else "") + ("width=1080" if wide else "width=450")
    url = re.sub(r"/(width=\d+|original=true)[^/]*/", f"/{size}/", u) if "/width=" in u or "/original=" in u else u
    key = hashlib.sha256(url.encode()).hexdigest()[:32]
    cached = DATA / "thumbs" / key
    if cached.exists():
        kind = (cached.with_suffix(".type").read_text() if cached.with_suffix(".type").exists() else "image/jpeg")
        return FileResponse(cached, media_type=kind, headers=THUMB_HEADERS)

    # Miss: stream it to the browser while writing the cache, so the first bytes show up right away.
    # A second request for the same file meanwhile (videos ask for ranges) is proxied straight through.
    first = key not in thumbs_inflight
    if first:
        thumbs_inflight.add(key)  # claimed before any await, so a parallel request can't also write the .part
    headers = {**sources.UA, "Accept-Encoding": "identity"}  # bytes are passed through as-is
    if not first and request is not None and request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    client = _avatar_client() if parsed.hostname == "github.com" else _thumb_client()
    try:
        response = await client.send(client.build_request("GET", url, headers=headers), stream=True)
        try:
            response.raise_for_status()
        except httpx.HTTPError:
            await response.aclose()
            raise
    except httpx.HTTPError as exc:
        if first:
            thumbs_inflight.discard(key)
        raise HTTPException(404, "Image unavailable") from exc
    kind = response.headers.get("content-type", "image/jpeg").split(";")[0]
    if parsed.hostname == "github.com" and kind not in {
            "image/png", "image/jpeg", "image/webp", "image/gif"}:
        await response.aclose()
        thumbs_inflight.discard(key)
        raise HTTPException(404, "Not a supported icon")
    length = int(response.headers.get("content-length") or 0)
    cache_it = first and response.status_code == 200 and length <= THUMB_MAX_CACHED
    if first and not cache_it:
        thumbs_inflight.discard(key)
    if not kind.startswith(("image/", "video/")):
        await response.aclose()
        thumbs_inflight.discard(key)
        raise HTTPException(404, "Not an image")
    out = {"content-type": kind, **THUMB_HEADERS}
    for name in ("content-length", "content-range", "accept-ranges"):
        if name in response.headers:
            out[name] = response.headers[name]

    async def body():
        part = cached.with_suffix(".part")
        handle = part.open("wb") if cache_it else None
        written, complete = 0, False
        try:
            async for chunk in response.aiter_raw():
                if handle:
                    handle.write(chunk)
                    written += len(chunk)
                    if written > THUMB_MAX_CACHED:  # no content-length and it turned out huge
                        handle.close(); handle = None; part.unlink(missing_ok=True)
                yield chunk
            complete = True
        finally:
            await response.aclose()
            if handle:
                handle.close()
                if complete:
                    cached.with_suffix(".type").write_text(kind)
                    part.replace(cached)
                else:
                    part.unlink(missing_ok=True)
            if cache_it:
                thumbs_inflight.discard(key)
            if complete and handle is not None and time.monotonic() - thumbs_pruned_at > 120:
                globals()["thumbs_pruned_at"] = time.monotonic()
                await asyncio.to_thread(_prune_thumbs)

    return StreamingResponse(body(), status_code=response.status_code, headers=out)
