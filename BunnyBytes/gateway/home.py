"""Live data for the home page (/api/home): what each studio is doing, the
GPU queue, the loaded LLM and the latest outputs. Every source is fetched
in parallel on loopback with a short timeout; one that's down just shows as
offline."""
from __future__ import annotations

import asyncio
import io
import json
import re
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

import httpx

ACTIVE = {"waiting_for_gpu", "preparing", "queued", "rendering", "running", "finalizing", "cancelling", "uploading", "training"}
WAITING = {"waiting_for_gpu", "queued", "preparing"}


async def _get(client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None) -> Any:
    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return json.loads(response.content, strict=False)  # prompts can hold raw control chars
    except Exception:  # noqa: BLE001 - offline is a normal state here
        return None


# Krea outputs are full-size PNGs (~1-2 MB, no-cache): the home page shows a
# small cached WebP instead. Only Krea output URLs, Trainer dataset pictures
# (the Trainer card's backdrop / training slideshow) and Voice character
# portraits (the Voice card's backdrop) are accepted as sources.
_THUMB_SRC = re.compile(r"^/(?:Krea/api/outputs/[0-9a-f]{32}/stream\?index=\d+(?:&v=\d+)?"
                        r"|Trainer/api/datasets/[A-Za-z0-9][A-Za-z0-9_-]{0,79}/(?:media|thumb)/[^/?#]+\?v=\d+"
                        r"|Voice/api/voices/[\w-]{1,100}/portrait\?v=\d+)$")
_thumbs: OrderedDict[str, bytes] = OrderedDict()


def thumb_url(src: str) -> str:
    return f"/api/home/thumb?src={quote(src, safe='')}" if _THUMB_SRC.match(src) else src


def _shrink(raw: bytes, side: int = 640, quality: int = 78) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        image = image.convert("RGB")
        image.thumbnail((side, side))
        out = io.BytesIO()
        image.save(out, "WEBP", quality=quality, method=4)
    return out.getvalue()


async def thumbnail(upstreams: dict[str, str], src: str, lite: bool = False) -> bytes | None:
    """`lite` (mobile data saver) makes a smaller, lower-quality copy."""
    if not _THUMB_SRC.match(src):
        return None
    key = f"lite|{src}" if lite else src
    if key in _thumbs:
        _thumbs.move_to_end(key)
        return _thumbs[key]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{upstreams[src.split('/')[1]]}{src}")
            response.raise_for_status()
        data = await asyncio.to_thread(_shrink, response.content, *((360, 62) if lite else ()))
    except Exception:  # noqa: BLE001 - gone or not an image
        return None
    _thumbs[key] = data
    while len(_thumbs) > 96:  # room for a training slideshow
        _thumbs.popitem(last=False)
    return data


def _jobs(data: Any) -> list[dict[str, Any]] | None:
    if isinstance(data, dict):
        data = data.get("jobs")
    return data if isinstance(data, list) else None


def _short(text: Any, size: int = 90) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= size else text[:size - 1].rstrip() + "…"


def _activity(jobs: list[dict[str, Any]] | None, noun: str) -> dict[str, Any]:
    if jobs is None:
        return {"online": False, "active": 0, "waiting": 0, "text": "Offline"}
    running = [job for job in jobs if job.get("state") in ACTIVE - WAITING]
    waiting = [job for job in jobs if job.get("state") in WAITING]
    if running:
        progress = running[0].get("progress")
        text = f"{len(running)} {noun}" + (f" · {int(progress)}%" if isinstance(progress, (int, float)) and progress else "")
    elif waiting:
        text = f"{len(waiting)} waiting"
    else:
        text = "Idle"
    return {"online": True, "active": len(running), "waiting": len(waiting), "text": text,
            "current": _short(running[0].get("prompt") or running[0].get("style") or running[0].get("name")) if running else ""}


async def _snapshot(upstreams: dict[str, str], llama_key: str, hidden: set[str] = frozenset(),
                    art_exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=3.0) as client:
        vid, krea, mus, trainer, queue, llama, connections, agent, voice, voice_show, models, rea = await asyncio.gather(
            _get(client, f"{upstreams['Vid']}/Vid/api/jobs"),
            _get(client, f"{upstreams['Krea']}/Krea/api/jobs"),
            _get(client, f"{upstreams['Mus']}/Mus/api/jobs"),
            _get(client, f"{upstreams['Trainer']}/Trainer/api/jobs"),
            _get(client, f"{upstreams['Queue']}/Queue/api/queue"),
            _get(client, f"{upstreams['Llama']}/running", {"authorization": f"Bearer {llama_key}"}),
            _get(client, f"{upstreams['Connections']}/Connections/api/status"),
            _get(client, f"{upstreams['Agent']}/Agent/api/sessions"),
            _get(client, f"{upstreams['Voice']}/Voice/api/health"),
            _get(client, f"{upstreams['Voice']}/Voice/api/showcase"),
            _get(client, f"{upstreams['Models']}/Models/api/state"),
            _get(client, f"{upstreams['REA']}/REA/api/health"),
        )
    vid_jobs, krea_jobs, mus_jobs, trainer_jobs = _jobs(vid), _jobs(krea), _jobs(mus), _jobs(trainer)
    studios = {
        "Vid": _activity(vid_jobs, "rendering"),
        "Krea": _activity(krea_jobs, "generating"),
        "Mus": _activity(mus_jobs, "composing"),
        "Trainer": _activity(trainer_jobs, "training"),
    }
    # Trainer card backdrop: the dataset being trained as a slideshow, otherwise
    # random pictures from random datasets (the page keeps one per visit).
    # Datasets excluded in Settings -> Modules never show, not even while training.
    training = next((job for job in trainer_jobs or [] if job.get("state") in ACTIVE - WAITING
                     and job.get("dataset") and job["dataset"] not in art_exclude), None)
    if trainer_jobs is not None and "Trainer" not in hidden:
        query = f"?dataset={quote(training['dataset'], safe='')}" if training else f"?exclude={quote(','.join(sorted(art_exclude)), safe='')}"
        async with httpx.AsyncClient(timeout=3.0) as client:
            show = await _get(client, f"{upstreams['Trainer']}/Trainer/api/showcase{query}")
        pictures = [thumb_url(src) for src in (show or {}).get("pictures") or []
                    if isinstance(src, str) and src.split("/")[4:5] and src.split("/")[4] not in art_exclude]
        studios["Trainer"]["art"] = {"dataset": training["dataset"] if training else None, "slideshow": bool(training),
                                     "pictures": [src for src in pictures if src.startswith("/api/home/thumb")]}
    # Music card backdrop: the newest finished song, drawn as a waveform by the page.
    song = next((job for job in mus_jobs or [] if job.get("state") == "complete" and job.get("output_url")), None)
    if song:
        studios["Mus"]["art"] = {"id": song.get("id"), "audio": song["output_url"]}
    studios["Voice"] = {"online": voice is not None, "active": 0, "waiting": 0,
                         "text": "Offline" if voice is None else
                         "Qwen ready" if voice.get("qwen_installed") else "Choose a model"}
    # Voice card backdrop: a random character portrait (the page keeps one per visit).
    portraits = [thumb_url(src) for src in (voice_show or {}).get("pictures") or [] if isinstance(src, str)]
    studios["Voice"]["art"] = {"pictures": [src for src in portraits if src.startswith("/api/home/thumb")]}
    if isinstance(models, dict):
        active, updates = int(models.get("active") or 0), int(models.get("updates") or 0)
        text = " · ".join(x for x in (f"{active} downloading" if active else "",
                                      f"{updates} update{'s' if updates != 1 else ''} available" if updates else "") if x) or "Up to date"
        studios["Models"] = {"online": True, "active": active, "waiting": 0, "text": text, "updates": updates,
                             "href": "/Models/#updates" if updates else "/Models/#installed"}
    else:
        studios["Models"] = {"online": False, "active": 0, "waiting": 0, "text": "Offline", "updates": 0, "href": "/Models/"}
    if isinstance(rea, dict):
        analysing, open_ = int(rea.get("analyzing") or 0), int(rea.get("sessions") or 0)
        studios["REA"] = {"online": True, "active": analysing, "waiting": 0,
                          "text": f"{analysing} analysing" if analysing else f"{open_} open" if open_ else f"Ready · {rea.get('version') or ''}".rstrip(" ·")}
    else:
        studios["REA"] = {"online": False, "active": 0, "waiting": 0, "text": "Offline"}
    entries = queue.get("entries") if isinstance(queue, dict) else None
    if entries is None:
        studios["Queue"] = {"online": False, "text": "Offline", "active": 0, "waiting": 0}
    else:
        holder = next((entry for entry in entries if entry.get("state") == "active"), None)
        waiting = sum(entry.get("state") == "waiting" for entry in entries)
        studios["Queue"] = {"online": True, "active": int(bool(holder)), "waiting": waiting,
                            "text": (f"{holder.get('app')} on GPU" if holder else "GPU free") + (f" · {waiting} waiting" if waiting else ""),
                            "current": _short(holder.get("label")) if holder else "",
                            "href": "/Queue/"}
    models = [str(item.get("model") or "") for item in (llama or {}).get("running", [])] if isinstance(llama, dict) else None
    studios["Llama"] = {"online": models is not None, "active": len(models or []), "waiting": 0,
                        "text": "Offline" if models is None else (", ".join(models)[:60] + " loaded" if models else "No model loaded"),
                        # llama-swap's page for the loaded model (its UI routes by hash)
                        "href": f"/Llama/ui/#/models/{quote(models[0], safe='')}" if models else "/Llama/ui/"}

    if isinstance(connections, dict):
        running, enabled = bool(connections.get("running")), bool(connections.get("enabled"))
        text = ("Not set up" if not connections.get("configured") else "Telegram bot running" if running
                else "Paused" if not enabled else "Stopped" + (f" · {_short(connections.get('last_error'), 40)}" if connections.get("last_error") else ""))
        studios["Connections"] = {"online": True, "active": 0, "waiting": 0, "text": text,
                                  "ok": running, "problem": enabled and not running}
    else:
        studios["Connections"] = {"online": False, "active": 0, "waiting": 0, "text": "Offline"}

    sessions = agent.get("sessions") if isinstance(agent, dict) else None
    if sessions is None:
        studios["Agent"] = {"online": False, "active": 0, "waiting": 0, "text": "Offline"}
    else:
        busy = [item for item in sessions if item.get("busy")]
        studios["Agent"] = {"online": True, "active": len(busy), "waiting": 0,
                            "text": (f"Working on {len(busy)} session{'s' if len(busy) > 1 else ''}" if busy else "Ready for a request"),
                            "current": _short(busy[0].get("title")) if busy else "",
                            "href": f"/Agent/?s={quote(str(busy[0]['id']), safe='')}" if busy and busy[0].get("id") else "/Agent/"}

    recent: list[dict[str, Any]] = []
    for job in (vid_jobs or [])[:40]:
        if job.get("state") == "complete" and job.get("poster_url"):
            recent.append({"studio": "Vid", "kind": "video", "thumb": job["poster_url"], "url": f"/Vid/?job={job['id']}",
                           "title": _short(job.get("prompt"), 70), "meta": f"{job.get('model_label', '')} · {job.get('mode_label', '')}".strip(" ·"),
                           "at": job.get("completed_at") or ""})
    krea_done = [job for job in (krea_jobs or []) if job.get("state") == "complete" and job.get("images")]
    for job in krea_done[:2]:  # full-size PNGs: keep the home page light
        recent.append({"studio": "Krea", "kind": "image", "thumb": thumb_url(job["images"][0]["url"]), "url": "/Krea/",
                       "title": _short(job.get("prompt"), 70), "meta": job.get("model_label", ""), "at": job.get("completed_at") or ""})
    recent = [item for item in recent if item["studio"] not in hidden]  # Settings -> Modules
    recent.sort(key=lambda item: item["at"], reverse=True)
    # Each Create card's backdrop: that studio's newest output, even if older than the strip.
    latest = {studio: next((item["thumb"] for item in recent if item["studio"] == studio), None) for studio in ("Vid", "Krea")}

    busy = [name for name, info in studios.items() if info.get("active") and name not in ("Queue", "Llama") and name not in hidden]
    return {"studios": studios, "recent": recent[:8], "latest": latest, "busy": busy, "hidden": sorted(hidden),
            "code_activity": [] if "Code" in hidden else await _code_activity()}


# Vibe Coding card heatmap: walking every project is too slow for each poll, so
# keep the counts for a few minutes. Hourly buckets let the page group by local day.
CODE_ACTIVITY_TTL = 300.0
_code_activity_cache: tuple[float, list[list[int]]] | None = None


async def _code_activity() -> list[list[int]]:
    global _code_activity_cache
    if _code_activity_cache and time.monotonic() - _code_activity_cache[0] < CODE_ACTIVITY_TTL:
        return _code_activity_cache[1]
    import code_history
    try:
        hours = await asyncio.to_thread(code_history.activity)
    except Exception:  # noqa: BLE001 - a missing workspace just means an empty grid
        hours = {}
    data = sorted([hour, count] for hour, count in hours.items())
    _code_activity_cache = (time.monotonic(), data)
    return data


# The home page renders with a snapshot and then polls /api/home, so cache it
# briefly: the page load and its first poll share one round of upstream calls.
# Concurrent callers share the fetch in flight, and it is shielded: the page
# load stops waiting after 1 s, but the fetch finishes and serves the poll.
_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
_inflight: dict[tuple, asyncio.Task] = {}


async def snapshot(upstreams: dict[str, str], llama_key: str, hidden: set[str] = frozenset(), max_age: float = 2.0,
                   art_exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    key = (tuple(sorted(hidden)), tuple(sorted(art_exclude)))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    task = _inflight.get(key)
    if task is None:
        async def fetch() -> dict[str, Any]:
            try:
                data = await _snapshot(upstreams, llama_key, hidden, art_exclude)
                _cache.clear()
                _cache[key] = (time.monotonic(), data)
                return data
            finally:
                _inflight.pop(key, None)
        task = _inflight[key] = asyncio.create_task(fetch())
    return await asyncio.shield(task)
