from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from events import EventBroker
from store import QueueStore

# TTL choice: 90 seconds. Generation jobs are expected to be long-running (well
# over 90s), so the *holder* is responsible for renewing via /heartbeat every
# ~20-30s -- three missed heartbeats before the lock is considered abandoned
# and TTL-expires, which bounds how long a crashed backend can wedge the
# queue to ~90s instead of forever.
LOCK_TTL_SECONDS = float(os.getenv("LOCK_TTL_SECONDS", "90"))
REAP_INTERVAL_SECONDS = 5.0
PROCEED_REQUEST_TIMEOUT = 10.0
# Connection-refused retries for a proceed callback (the owning app restarting
# or briefly saturated). The request never reached the app, so a retry can
# never double-start a job; a timeout is NOT retried for exactly that reason.
PROCEED_CONNECT_RETRIES = (0.5, 1.0, 2.0, 4.0, 8.0)
CANCEL_FORWARD_TIMEOUT = 15.0
SUPERVISOR_RESTART_TIMEOUT = 60.0
COMFYUI_SUPERVISOR_URL = os.getenv("COMFYUI_SUPERVISOR_URL", "http://127.0.0.1:8083").rstrip("/")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bunny-lock")

BASE_DIR = Path(__file__).resolve().parent


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is required and must not be empty. Set it in the environment "
            f"(e.g. BunnyBytes/.env) before starting the lock/queue service."
        )
    return value


QUEUE_INTERNAL_SECRET = _required("QUEUE_INTERNAL_SECRET")

# The apps this coordinator hands GPU turns to. Base URLs already include
# each app's own mount prefix (e.g. "/Video") because every app in this stack is
# configured with that prefix even when reached directly on its own port, not
# just through the gateway -- see each service's *_MOUNT_PREFIX in
# docker-compose.yml.
APP_PREFIXES = {"Vid": "/Video", "Mus": "/Music", "Krea": "/Image", "Trainer": "/Trainer", "Agent": "/Agent", "Voice": "/Voice", "Framework": ""}
APP_BASE_URLS = {
    "Vid": os.getenv("VID_UPSTREAM", "http://127.0.0.1:8000").rstrip("/") + APP_PREFIXES["Vid"],
    "Mus": os.getenv("MUS_UPSTREAM", "http://127.0.0.1:8200").rstrip("/") + APP_PREFIXES["Mus"],
    "Krea": os.getenv("KREA_UPSTREAM", "http://127.0.0.1:8300").rstrip("/") + APP_PREFIXES["Krea"],
    "Trainer": os.getenv("TRAINER_UPSTREAM", "http://127.0.0.1:8500").rstrip("/") + APP_PREFIXES["Trainer"],
    "Agent": os.getenv("AGENT_UPSTREAM", "http://127.0.0.1:8900").rstrip("/") + APP_PREFIXES["Agent"],
    "Voice": os.getenv("VOICE_UPSTREAM", "http://127.0.0.1:8950").rstrip("/") + APP_PREFIXES["Voice"],
    "Framework": os.getenv("FRAMEWORK_UPSTREAM", "http://127.0.0.1:8700").rstrip("/"),
}
HERMES_API_URL = os.getenv("HERMES_API_URL", "http://127.0.0.1:8642").rstrip("/")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")
LLAMA_URL = os.getenv("LLAMA_URL", "http://127.0.0.1:8400").rstrip("/")
LLAMA_API_KEY = os.getenv("LLAMA_API_KEY", "")
HERMES_CHECK_INTERVAL_SECONDS = 10.0
# Apps whose GET /api/jobs/{job_id} reports a render's progress.
PROGRESS_APPS = {"Vid", "Krea", "Mus", "Trainer", "Voice"}
PROGRESS_INTERVAL_SECONDS = 2.0
PROGRESS_REQUEST_TIMEOUT = 3.0

PREFIX = os.getenv("QUEUE_MOUNT_PREFIX", "").rstrip("/")


def p(path: str) -> str:
    return f"{PREFIX}{path}"


store = QueueStore(BASE_DIR / "data" / "queue.sqlite3")
events = EventBroker()
hermes_card: dict[str, Any] | None = None
# Render progress of the active entry, read from its own app's job API:
# entry_id -> {"percent", "remaining_seconds", "stage"}.
progress_by_entry: dict[str, dict[str, Any]] = {}
no_progress_entries: set[str] = set()   # the app doesn't know the job (Hermes turns etc.): stop asking


def _public_entries() -> list[dict[str, Any]]:
    entries = store.public_list()
    for entry in entries:
        entry["progress"] = progress_by_entry.get(entry["entry_id"])
    if not hermes_card:
        return entries
    # The Master Agent's turns are Hermes runs, so both cards show up together:
    # fold the Hermes card into the Agent entry (the active one if any).
    agents = [e for e in entries if e.get("app") == "Agent"]
    if not agents:
        return [hermes_card] + entries
    target = next((e for e in agents if e.get("state") == "active"), agents[0])
    target["hermes"] = {"runs": hermes_card.get("runs", 1), "loaded_model": hermes_card.get("loaded_model")}
    return entries


async def _check_hermes_card() -> dict[str, Any] | None:
    """Show remote API work only while Hermes is running and Llama has a model loaded.

    This is a display card, never a GPU lock entry: remote Hermes runs do not
    acquire or release the shared scheduler's token.
    """
    if not HERMES_API_KEY or not LLAMA_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            hermes_response, llama_response = await asyncio.gather(
                client.get(f"{HERMES_API_URL}/health/detailed", headers={"Authorization": f"Bearer {HERMES_API_KEY}"}),
                client.get(f"{LLAMA_URL}/running", headers={"Authorization": f"Bearer {LLAMA_API_KEY}"}),
            )
        hermes_response.raise_for_status()
        llama_response.raise_for_status()
        checks = hermes_response.json().get("readiness", {}).get("checks", {})
        active_runs = checks.get("background_queues", {}).get("active_api_runs", 0)
        running = llama_response.json().get("running", [])
        if not isinstance(active_runs, int) or active_runs < 1 or not isinstance(running, list):
            return None
        loaded = [item.get("model") for item in running if isinstance(item, dict) and item.get("model")]
        if not loaded:
            return None
        return {
            "entry_id": "hermes-remote-active", "app": "Hermes", "job_id": "remote-agent",
            "label": "Remote agent" + (f" · {active_runs} runs" if active_runs > 1 else ""),
            "state": "active", "is_continuous": True, "is_locked": False,
            "is_informational": True, "loaded_model": loaded[0], "shutdown_at": None,
            "position": None, "runs": active_runs,
        }
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return None


async def _hermes_card_loop() -> None:
    global hermes_card
    while True:
        try:
            next_card = await _check_hermes_card()
            if next_card != hermes_card:
                hermes_card = next_card
                await _publish_queue()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Hermes queue status check failed")
        await asyncio.sleep(HERMES_CHECK_INTERVAL_SECONDS)


def _seconds_since(value: Any) -> float | None:
    try:
        started = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())


def _job_progress(app_name: str, job: dict[str, Any], became_active_at: str | None) -> dict[str, Any] | None:
    """Normalise each app's job JSON to a percent + time left. Apps that give an
    estimate (Video's per-settings render average, the Trainer's epoch clock)
    use it; otherwise time left is extrapolated from the progress so far."""
    remaining = None
    stage = job.get("stage") if isinstance(job.get("stage"), str) else None
    raw = job.get("progress")
    if app_name == "Trainer" and isinstance(raw, dict):
        percent = float(raw.get("overall") or 0) * 100
        stage = raw.get("phase_label") or stage
        timing = job.get("timing") if isinstance(job.get("timing"), dict) else {}
        remaining = timing.get("remaining_seconds", raw.get("eta_seconds"))
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
        percent = float(raw)
    else:
        return None
    percent = max(0.0, min(100.0, percent))
    elapsed = _seconds_since(job.get("started_at") or became_active_at)
    estimate = job.get("estimate") if isinstance(job.get("estimate"), dict) else {}
    if remaining is None and estimate.get("seconds") and elapsed is not None and elapsed < estimate["seconds"]:
        remaining = estimate["seconds"] - elapsed
    if remaining is None and elapsed is not None and 3 <= percent < 100:
        remaining = elapsed * (100 - percent) / percent
    return {
        "percent": round(percent, 1),
        "remaining_seconds": round(remaining) if isinstance(remaining, (int, float)) else None,
        "stage": stage[:60] if stage else None,
    }


async def _progress_loop() -> None:
    """Every couple of seconds, ask the active entry's app how far its job is,
    and send changes as a small "progress" event (not the whole queue)."""
    async with httpx.AsyncClient(timeout=PROGRESS_REQUEST_TIMEOUT) as client:
        while True:
            try:
                active = next((e for e in store.list_all() if e.state == "active"), None)
                current = {active.entry_id} if active else set()
                stale = set(progress_by_entry) - current
                for entry_id in stale:
                    progress_by_entry.pop(entry_id, None)
                no_progress_entries.intersection_update(current)
                changed = bool(stale)
                if active and active.app in PROGRESS_APPS and active.entry_id not in no_progress_entries:
                    response = await client.get(f"{APP_BASE_URLS[active.app]}/api/jobs/{active.job_id}")
                    if response.status_code == 404:
                        no_progress_entries.add(active.entry_id)
                    elif response.status_code == 200:
                        value = _job_progress(active.app, response.json(), active.became_active_at)
                        if value != progress_by_entry.get(active.entry_id):
                            if value is None:
                                progress_by_entry.pop(active.entry_id, None)
                            else:
                                progress_by_entry[active.entry_id] = value
                            changed = True
                if changed:
                    await events.publish("progress", {"progress": dict(progress_by_entry)})
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, ValueError):
                pass   # the app is busy or restarting: keep the last known value
            except Exception:
                log.exception("Queue progress check failed")
            await asyncio.sleep(PROGRESS_INTERVAL_SECONDS)


class EnqueueRequest(BaseModel):
    app: str
    job_id: str
    label: str
    is_continuous: bool = False
    priority: bool = False   # front of the line (the Master Agent); locked entries keep their slots
    group: str | None = Field(default=None, max_length=60)   # entries sharing a group show as one card on the queue page


class HeartbeatRequest(BaseModel):
    token: str


class ReleaseRequest(BaseModel):
    token: str


class ShutdownAtRequest(BaseModel):
    shutdown_at: str | None = None


class ReorderRequest(BaseModel):
    entry_ids: list[str]


class LockRequest(BaseModel):
    is_locked: bool


def _require_internal(request: Request, x_queue_internal_secret: str | None) -> None:
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="This endpoint is only reachable from localhost.")
    if x_queue_internal_secret != QUEUE_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Missing or invalid X-Queue-Internal-Secret.")


async def _publish_queue() -> None:
    await events.publish("queue", {"entries": _public_entries()})


async def _release_and_promote(token: str | None, why: str) -> None:
    """Free an active slot nobody will ever use and hand it to the next job,
    instead of leaving it to expire (a full TTL plus a reaper tick of dead GPU
    time). Idempotent: a token that is no longer the active one is a no-op."""
    if not token:
        return
    released, promoted = store.release(token, LOCK_TTL_SECONDS)
    if released:
        log.info("Freed the GPU slot (%s)", why)
    if promoted:
        log.info("Queue entry %s (%s/%s) promoted after %s", promoted.entry_id[:8], promoted.app, promoted.job_id[:8], why)
        asyncio.create_task(_fire_proceed(promoted))
    if released or promoted:
        await _publish_queue()


async def _fire_proceed(entry) -> None:
    """Tell the owning app it may now submit to ComfyUI.

    Connection failures are retried (the request never arrived, so it can't
    double-start anything). If the app can't be reached at all, or answers
    404 (it doesn't know the job -- it will never heartbeat or release it),
    the slot is freed straight away for the next job. A timeout is ambiguous
    (the app may have started the job), so that one is left to the TTL."""
    base_url = APP_BASE_URLS.get(entry.app)
    if not base_url:
        log.warning("Cannot fire proceed callback: unknown app %r", entry.app)
        await _release_and_promote(entry.token, f"unknown app {entry.app!r}")
        return
    url = f"{base_url}/api/internal/queue/proceed/{entry.job_id}"
    try:
        async with httpx.AsyncClient(timeout=PROCEED_REQUEST_TIMEOUT) as client:
            for attempt, delay in enumerate((*PROCEED_CONNECT_RETRIES, None), start=1):
                try:
                    response = await client.post(
                        url, json={"entry_id": entry.entry_id, "token": entry.token},
                        headers={"X-Queue-Internal-Secret": QUEUE_INTERNAL_SECRET},
                    )
                    break
                except httpx.ConnectError as exc:
                    if delay is None:
                        log.warning("Proceed callback to %s failed %d times (%s)", url, attempt, exc)
                        await _release_and_promote(entry.token, f"{entry.app} unreachable")
                        return
                    await asyncio.sleep(delay)
            if response.status_code == 404:
                log.warning("Proceed callback to %s returned HTTP 404", url)
                await _release_and_promote(entry.token, f"{entry.app} did not know job {entry.job_id[:8]}")
            elif response.status_code >= 400:
                log.warning("Proceed callback to %s returned HTTP %s", url, response.status_code)
    except httpx.HTTPError as exc:
        log.warning("Proceed callback to %s failed (%s)", url, exc)
    except Exception:
        log.exception("Unexpected error firing proceed callback to %s", url)


async def _reap_loop() -> None:
    while True:
        try:
            promoted = store.reap_and_promote(LOCK_TTL_SECONDS)
            if promoted:
                log.info("Reaper promoted queue entry %s (%s/%s) after prior holder expired", promoted.entry_id[:8], promoted.app, promoted.job_id[:8])
                asyncio.create_task(_fire_proceed(promoted))
                await _publish_queue()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Queue reaper tick failed")
        await asyncio.sleep(REAP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    reaper = asyncio.create_task(_reap_loop())
    hermes_monitor = asyncio.create_task(_hermes_card_loop())
    progress_monitor = asyncio.create_task(_progress_loop())
    try:
        yield
    finally:
        reaper.cancel()
        hermes_monitor.cancel()
        progress_monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await progress_monitor
        with contextlib.suppress(asyncio.CancelledError):
            await reaper
        with contextlib.suppress(asyncio.CancelledError):
            await hermes_monitor
        store.close()


# docs_url/redoc_url/openapi_url disabled: defense-in-depth against the
# route/parameter map being exposed if ever reached directly (only the
# gateway is internet-facing in this stack).
app = FastAPI(title="bunny-lock", version="2.5.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount(p("/static"), StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ---------------------------------------------------------------------------
# Internal surface: loopback + X-Queue-Internal-Secret only, never routed
# through the gateway. Used exclusively by the GPU apps' own backend code.
# ---------------------------------------------------------------------------

@app.post("/enqueue")
async def enqueue(request: EnqueueRequest, http_request: Request, x_queue_internal_secret: str | None = Header(default=None)) -> JSONResponse:
    _require_internal(http_request, x_queue_internal_secret)
    entry, promoted, position = store.enqueue(
        request.app, request.job_id, request.label, request.is_continuous, LOCK_TTL_SECONDS, priority=request.priority, group=request.group,
    )
    if promoted:
        log.info("Queue entry %s (%s/%s) active immediately", entry.entry_id[:8], entry.app, entry.job_id[:8])
        asyncio.create_task(_fire_proceed(entry))
    else:
        log.info("Queue entry %s (%s/%s) waiting%s", entry.entry_id[:8], entry.app, entry.job_id[:8], " (priority)" if request.priority else "")
    await _publish_queue()
    return JSONResponse({"entry_id": entry.entry_id, "state": entry.state, "position": position}, status_code=200)


@app.post("/heartbeat")
async def heartbeat(request: HeartbeatRequest, http_request: Request, x_queue_internal_secret: str | None = Header(default=None)) -> JSONResponse:
    _require_internal(http_request, x_queue_internal_secret)
    entry = store.heartbeat(request.token, LOCK_TTL_SECONDS)
    if entry is None:
        return JSONResponse({"error": "not_found", "message": "No active queue entry matches that token."}, status_code=404)
    return JSONResponse({"token": entry.token, "expires_at": entry.expires_at}, status_code=200)


@app.post("/release")
async def release(request: ReleaseRequest, http_request: Request, x_queue_internal_secret: str | None = Header(default=None)) -> JSONResponse:
    _require_internal(http_request, x_queue_internal_secret)
    released, promoted = store.release(request.token, LOCK_TTL_SECONDS)
    if not released:
        return JSONResponse(
            {"error": "not_found", "message": "That token does not match the currently active queue entry."},
            status_code=404,
        )
    if promoted:
        log.info("Queue entry %s (%s/%s) promoted after release", promoted.entry_id[:8], promoted.app, promoted.job_id[:8])
        asyncio.create_task(_fire_proceed(promoted))
    await _publish_queue()
    return JSONResponse({"released": True}, status_code=200)


@app.post("/dequeue/{entry_id}")
async def dequeue(entry_id: str, http_request: Request, x_queue_internal_secret: str | None = Header(default=None)) -> JSONResponse:
    _require_internal(http_request, x_queue_internal_secret)
    dequeued, reason = store.dequeue(entry_id)
    if dequeued and reason is None:
        await _publish_queue()
    return JSONResponse({"dequeued": dequeued, "reason": reason}, status_code=200)


@app.post("/prioritize/{entry_id}")
async def prioritize(entry_id: str, http_request: Request, x_queue_internal_secret: str | None = Header(default=None)) -> JSONResponse:
    """Internal: the Master Agent moves a job it started to the front of the waiting line."""
    _require_internal(http_request, x_queue_internal_secret)
    moved = store.prioritize(entry_id)
    if moved:
        await _publish_queue()
    return JSONResponse({"prioritized": moved})


@app.patch("/queue/{entry_id}")
async def update_shutdown_at(entry_id: str, request: ShutdownAtRequest, http_request: Request, x_queue_internal_secret: str | None = Header(default=None)) -> JSONResponse:
    _require_internal(http_request, x_queue_internal_secret)
    ok = store.set_shutdown_at(entry_id, request.shutdown_at)
    if not ok:
        return JSONResponse({"error": "not_found", "message": "No queue entry with that id."}, status_code=404)
    await _publish_queue()
    return JSONResponse({"updated": True}, status_code=200)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Browser-facing surface: routed through the gateway at /Queue/..., gated by
# the gateway's existing session cookie (the gateway checks auth before
# proxying here, same as it does for the other four apps -- this service does
# not re-verify the cookie itself, matching how those apps behave too).
# ---------------------------------------------------------------------------

@app.get(p("/"))
async def index(request: Request):
    return templates.TemplateResponse(request, "queue.html", {"mount_prefix": PREFIX, "app_version": app.version})


@app.get(p("/api/queue"))
async def list_queue() -> dict[str, Any]:
    return {"entries": _public_entries()}


@app.get(p("/api/queue/events"))
async def queue_events() -> StreamingResponse:
    # The current queue goes out first, so a (re)connecting page is never stale.
    return StreamingResponse(events.stream(("queue", {"entries": _public_entries()})), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post(p("/api/comfyui/restart"))
async def restart_comfyui() -> JSONResponse:
    """Ask the host supervisor to restart ComfyUI and its own systemd unit."""
    try:
        async with httpx.AsyncClient(timeout=SUPERVISOR_RESTART_TIMEOUT) as client:
            response = await client.post(f"{COMFYUI_SUPERVISOR_URL}/restart")
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("ComfyUI restart request failed: %s", exc)
        return JSONResponse({"message": "Could not reach the ComfyUI supervisor."}, status_code=502)
    if response.status_code != 200 or result.get("status") != "restarting":
        return JSONResponse(
            {"message": result.get("message", "ComfyUI restart failed.")},
            status_code=409 if response.status_code == 409 else 502,
        )
    return JSONResponse({"message": "ComfyUI restarted; supervisor restart requested."})


@app.post(p("/api/queue/reorder"))
async def reorder_queue(request: ReorderRequest) -> JSONResponse:
    reason = store.reorder_waiting(request.entry_ids)
    if reason:
        message = "The queue changed. Refresh and try again." if reason == "queue_changed" else "A locked job cannot change position."
        return JSONResponse({"error": reason, "message": message}, status_code=409)
    await _publish_queue()
    return JSONResponse({"entries": store.public_list()})


@app.post(p("/api/queue/{entry_id}/lock"))
async def lock_queue_entry(entry_id: str, request: LockRequest) -> JSONResponse:
    if not store.set_locked(entry_id, request.is_locked):
        return JSONResponse({"error": "not_found", "message": "No queue entry with that id."}, status_code=404)
    await _publish_queue()
    return JSONResponse({"entry_id": entry_id, "is_locked": request.is_locked})


async def _remove_orphan(entry_id: str, entry: Any, reason: str) -> dict[str, Any]:
    """Drop an entry its app will never release (it doesn't know the job, or it is down)."""
    current = store.get(entry_id)
    if current is None:
        return {"cancelled": True, "removed": "already_gone"}
    if current.state == "waiting":
        store.dequeue(entry_id)
        await _publish_queue()
    else:
        await _release_and_promote(current.token, f"cancelled {reason} {current.app}/{current.job_id[:8]}")
    log.info("Removed queue entry %s (%s/%s): %s", entry_id[:8], entry.app, entry.job_id[:8],
             "its app is not running" if reason == "app_down" else "its app no longer knows the job")
    return {"cancelled": True, "removed": reason}


@app.post(p("/api/queue/{entry_id}/cancel"))
async def cancel_from_queue(entry_id: str) -> JSONResponse:
    entry = store.get(entry_id)
    if entry is None:
        return JSONResponse({"error": "not_found", "message": "No queue entry with that id."}, status_code=404)
    base_url = APP_BASE_URLS.get(entry.app)
    if not base_url:
        return JSONResponse({"error": "unknown_app", "message": f"Unknown app {entry.app!r}."}, status_code=500)
    # Video's queue entries include Hermes turns (prompt enhancement, repair,
    # frames, LoRA import...) that /api/jobs/<id>/cancel 404s for, so Video
    # gets the same internal cancel route as the Master Agent.
    internal = entry.app in ("Agent", "Vid")
    headers = {"X-Queue-Internal-Secret": QUEUE_INTERNAL_SECRET} if internal else None
    if internal:
        url = f"{base_url}/api/internal/queue/cancel/{entry.job_id}"
    else:
        url = f"{base_url}/api/jobs/{entry.job_id}/cancel"
    try:
        async with httpx.AsyncClient(timeout=CANCEL_FORWARD_TIMEOUT) as client:
            response = await client.post(
                url,
                json={"entry_id": entry.entry_id} if internal else None,
                headers=headers,
            )
    except httpx.ConnectError as exc:
        # The app is down (container stopped or crashed): nothing will ever release
        # this entry, so the user's cancel removes it here, as for an orphan below.
        log.warning("Cancel forward to %s failed (%s): %s is not running, removing its entry", url, exc, entry.app)
        return JSONResponse(await _remove_orphan(entry_id, entry, "app_down"))
    except httpx.HTTPError as exc:
        log.warning("Cancel forward to %s failed (%s)", url, exc)
        return JSONResponse({"error": "forward_failed", "message": f"{entry.app} did not answer the cancel request ({exc})."}, status_code=502)
    # Normally the owning app's own cancel/stop handler calls /dequeue or
    # /release. When the app says it doesn't know the job (404), nothing will
    # ever release this entry, so the user's cancel removes it here instead.
    if response.status_code == 404:
        return JSONResponse(await _remove_orphan(entry_id, entry, "orphan"))
    try:
        body = response.json()
    except ValueError:
        body = {"status": response.status_code}
    return JSONResponse(body, status_code=response.status_code)


if __name__ == "__main__":
    # 127.0.0.1 only, per network_mode: host -- the browser-facing routes are
    # only reachable via bunny-gateway proxying over loopback, and the
    # internal routes are additionally guarded by QUEUE_INTERNAL_SECRET.
    uvicorn.run("app:app", host="127.0.0.1", port=int(os.getenv("LOCK_PORT", "8600")))
