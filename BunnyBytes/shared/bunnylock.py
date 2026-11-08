"""Client for bunny-lock, Rocsy's shared GPU queue (protocol v2, see BunnyBytes/lock/PROTOCOL.md).

One copy of this file serves every app: docker-compose.yml mounts it read-only
into each container. An app makes one BunnyLock, starts it at startup, and gets
GPU turns in one of two ways:

* lease() - wait for the turn inside a block; the turn is released (or the
  queue entry dropped) when the block exits, even when it is cancelled:

      async with gpu.lease(job_id, "Master Agent: ...", priority=True, uses_llm=True,
                           llm_settle=True) as turn:
          ...                        # the GPU is ours

* enqueue() + on_grant - for job-based apps that queue now and start later:
  on_grant(job_id, entry_id, token) runs when it is the job's turn and returns
  True when the job started (False hands the turn straight back). The app
  calls release(token) when the GPU work is done.

The client keeps one stream open to the queue. Grants, prepare-early notices
and the queue page's Cancel arrive on it, and the stream closing tells the
queue this process is gone, so a crashed app frees the GPU at once instead of
after a lease timeout. Held turns are heartbeated automatically.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("bunnylock")

GrantHandler = Callable[[str, str, str], "bool | Awaitable[bool]"]
# Returns None when the job isn't the app's (the client then tries its own
# waiters), an HTTP status, or (status, body).
CancelHandler = Callable[[str, str], "Any | Awaitable[Any]"]
PrepareHandler = Callable[[str, str], "Any | Awaitable[Any]"]
# (job_id, entry_id, active, token) after an app restart: True keeps the entry.
AdoptHandler = Callable[[str, str, bool, "str | None"], "bool | Awaitable[bool]"]

HEARTBEAT_SECONDS = 20.0
REQUEST_TIMEOUT = 10.0
CONNECT_WAIT = 5.0          # enqueue waits this long for the stream on a fresh start
REGISTRATION_WAIT = 30.0    # a grant can beat its own enqueue response: wait for it
PROGRESS_GAP = 0.25         # at most 4 progress pushes per second per turn


class PendingStore(dict):
    """Jobs queued but not yet started, also kept on disk (one JSON file per job)
    so they survive an app restart. Values must be JSON; keys added to a value
    later (like a prepare-early future) stay in memory only."""

    def __init__(self, directory: Any):
        super().__init__()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        for path in self.directory.glob("*.json"):
            try:
                super().__setitem__(path.stem, json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                path.unlink(missing_ok=True)

    def __setitem__(self, job_id: str, value: dict[str, Any]) -> None:
        super().__setitem__(job_id, value)
        path = self.directory / f"{job_id}.json"
        try:
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(value), encoding="utf-8")
            temp.replace(path)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("Could not save queued job %s: %s", job_id, exc)

    def pop(self, job_id: str, *default: Any) -> Any:
        (self.directory / f"{job_id}.json").unlink(missing_ok=True)
        return super().pop(job_id, *default)

    def retain(self, keep: set[str]) -> None:
        for job_id in [key for key in self if key not in keep]:
            self.pop(job_id, None)


class LeaseCancelled(Exception):
    """The queue page cancelled this entry while it was still waiting."""


class LockUnavailable(Exception):
    """The queue could not be reached, so the job could not be queued."""


@dataclass
class Turn:
    token: str
    entry_id: str
    job_id: str
    # Apps of the jobs already on the GPU when this turn started (concurrent
    # jobs, Queue page setting). Empty = the GPU was ours alone: then it's safe
    # to free other apps' cached models / unload the LLM to make room.
    others: list[str] = field(default_factory=list)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass
class _Waiter:
    future: asyncio.Future
    entry_id: str | None = None


@dataclass
class BunnyLock:
    app: str
    url: str = field(default_factory=lambda: os.getenv("BUNNY_LOCK_URL", "http://127.0.0.1:8600"))
    secret: str = field(default_factory=lambda: os.getenv("QUEUE_INTERNAL_SECRET", ""))
    on_grant: GrantHandler | None = None
    on_cancel: CancelHandler | None = None
    on_prepare: PrepareHandler | None = None
    on_adopt: AdoptHandler | None = None
    heartbeat_seconds: float = HEARTBEAT_SECONDS

    def __post_init__(self) -> None:
        self.url = self.url.rstrip("/")
        self.instance = uuid.uuid4().hex
        self._headers = {"X-Queue-Internal-Secret": self.secret}
        self._client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task] = []
        self._connected: asyncio.Event | None = None
        self._held: set[str] = set()                   # tokens we hold (heartbeated)
        self._seen: set[str] = set()                   # grants already handled (re-sent on reconnect)
        self._adopted: set[str] = set()                # jobs kept from the previous process
        self._enqueuing: dict[str, asyncio.Event] = {}
        self._waiters: dict[str, _Waiter] = {}
        self._others: dict[str, list[str]] = {}       # token -> apps already on the GPU at grant time
        self._progress_at: dict[str, float] = {}
        self._progress_later: dict[str, tuple[Any, Any, Any, Any]] = {}
        self._background: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Open the stream and start heartbeating. Call once the event loop runs (startup)."""
        if self._tasks:
            return
        self._loop = asyncio.get_running_loop()
        self._client = httpx.AsyncClient(base_url=self.url, headers=self._headers, timeout=REQUEST_TIMEOUT)
        self._connected = asyncio.Event()
        self._tasks = [asyncio.create_task(self._stream_loop()), asyncio.create_task(self._heartbeat_loop())]

    async def stop(self) -> None:
        tasks = [*self._tasks, *self._background]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        self._background.clear()
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def connected(self) -> bool:
        return bool(self._connected and self._connected.is_set())

    def held(self, token: str | None) -> bool:
        return bool(token) and token in self._held

    def shared_with(self, token: str | None) -> list[str]:
        """Apps whose jobs were already on the GPU when this turn was granted
        (concurrent jobs). Non-empty: don't free VRAM or unload models they use."""
        return list(self._others.get(token or "", []))

    # ------------------------------------------------------------ requests

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise LockUnavailable("BunnyLock.start() was not called")
        return self._client

    async def _post(self, path: str, body: dict[str, Any], timeout: float = REQUEST_TIMEOUT) -> httpx.Response | None:
        try:
            return await self._http().post(path, json=body, timeout=timeout)
        except (httpx.HTTPError, LockUnavailable) as exc:
            log.warning("bunny-lock %s failed: %s", path, exc)
            return None

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def enqueue(self, job_id: str, label: str, *, is_continuous: bool = False, priority: bool = False,
                      group: str | None = None, uses_llm: bool | None = None, model: str | None = None,
                      config: str | None = None, vram_mb: int | None = None) -> dict[str, Any] | None:
        """Queue a job. Returns {"entry_id", "state", "position"}, or None when the
        queue can't be reached (the job cannot be queued; tell the user).
        uses_llm=False marks a job that never needs the LLM: right after an LLM
        turn the queue keeps the model unloaded while it renders.
        model / config (e.g. "1024x1024", "720p 121f") file the job's measured VRAM
        peak more precisely; vram_mb is the app's own guess (it can only raise the
        recorded peak). All optional: without them the peak is kept per app."""
        self.start()   # no-op once running
        if self._connected and not self._connected.is_set():
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._connected.wait(), CONNECT_WAIT)
        registered = self._enqueuing[job_id] = asyncio.Event()
        try:
            response = await self._post("/v2/enqueue", {
                "app": self.app, "instance": self.instance, "job_id": job_id, "label": label,
                "is_continuous": is_continuous, "priority": priority, "group": group, "uses_llm": uses_llm,
                "model": model, "config": config, "vram_mb": vram_mb,
            })
            if response is None or response.status_code >= 400:
                if response is not None:
                    log.warning("bunny-lock enqueue returned HTTP %s: %s", response.status_code, response.text[:200])
                return None
            entry = response.json()
            waiter = self._waiters.get(job_id)
            if waiter:
                waiter.entry_id = entry.get("entry_id")
            return entry
        finally:
            # Let a grant that raced this response through, once the caller
            # had a chance to register the job (the next loop iteration).
            asyncio.get_running_loop().call_soon(registered.set)
            self._enqueuing.pop(job_id, None)

    async def release(self, token: str | None, *, llm_settle: bool = False) -> bool:
        """Hand the GPU back. Shielded: a cancel landing here can't skip it.
        llm_settle=True after an LLM turn that just unloaded its model."""
        if not token:
            return False
        self._held.discard(token)
        self._others.pop(token, None)
        self._progress_at.pop(token, None)
        self._progress_later.pop(token, None)
        response = await asyncio.shield(self._post("/v2/release", {"token": token, "llm_settle": llm_settle}))
        return bool(response is not None and response.status_code == 200)

    async def dequeue(self, entry_id: str | None) -> None:
        if entry_id:
            await asyncio.shield(self._post("/v2/dequeue", {"entry_id": entry_id}))

    async def prioritize(self, entry_id: str | None) -> bool:
        if not entry_id:
            return False
        response = await self._post("/v2/prioritize", {"entry_id": entry_id})
        return bool(response is not None and response.status_code == 200 and response.json().get("prioritized"))

    async def shutdown_at(self, entry_id: str, shutdown_at: str | None) -> None:
        await self._post("/v2/shutdown_at", {"entry_id": entry_id, "shutdown_at": shutdown_at})

    def progress(self, token: str | None, job: "dict[str, Any] | Callable[[], dict[str, Any] | None] | None" = None, *,
                 percent: float | None = None,
                 stage: str | None = None, remaining_seconds: float | None = None) -> None:
        """Push render progress for the queue page (fire and forget, throttled).
        Pass the app's own job JSON (as GET /api/jobs/{id} returns it, or a function
        building it), or the numbers."""
        if not token or token not in self._held or self._client is None:
            return
        try:
            in_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            in_loop = False
        if not in_loop and self._loop is not None:   # called from a worker thread
            self._loop.call_soon_threadsafe(lambda: self.progress(token, job, percent=percent, stage=stage,
                                                                  remaining_seconds=remaining_seconds))
            return
        now = time.monotonic()
        wait = PROGRESS_GAP - (now - self._progress_at.get(token, 0.0))
        if wait > 0:
            # Too soon: keep only the newest value and send it when the gap is over,
            # so the last update before a pause is never lost.
            first = token not in self._progress_later
            self._progress_later[token] = (job, percent, stage, remaining_seconds)
            if first:
                self._loop.call_later(wait, self._progress_flush, token)
            return
        self._progress_at[token] = now
        body: dict[str, Any] = {"token": token}
        if callable(job):   # built only when a push is due
            job = job()
        if job is not None:
            body["job"] = {k: job.get(k) for k in ("progress", "stage", "started_at", "estimate", "timing") if k in job}
        else:
            body |= {"percent": percent, "stage": stage, "remaining_seconds": remaining_seconds}
        self._spawn(self._post("/v2/progress", body, timeout=3))

    def _progress_flush(self, token: str) -> None:
        pending = self._progress_later.pop(token, None)
        if pending is not None:
            job, percent, stage, remaining = pending
            self.progress(token, job, percent=percent, stage=stage, remaining_seconds=remaining)

    # Thread helpers (Voice runs generations in worker threads).
    def release_sync(self, token: str | None, *, llm_settle: bool = False) -> bool:
        if not token:
            return False
        self._held.discard(token)
        self._others.pop(token, None)
        self._progress_at.pop(token, None)
        self._progress_later.pop(token, None)
        try:
            response = httpx.post(f"{self.url}/v2/release", json={"token": token, "llm_settle": llm_settle},
                                  headers=self._headers, timeout=REQUEST_TIMEOUT)
            return response.status_code == 200
        except httpx.HTTPError as exc:
            log.warning("bunny-lock release failed: %s", exc)
            return False

    # ------------------------------------------------------------ waiting

    @asynccontextmanager
    async def lease(self, job_id: str, label: str, *, priority: bool = False, uses_llm: bool | None = None,
                    llm_settle: bool = False, group: str | None = None, is_continuous: bool = False,
                    on_wait: Callable[[dict[str, Any]], Any] | None = None, timeout: float | None = None,
                    model: str | None = None, config: str | None = None, vram_mb: int | None = None):
        """Wait for the GPU, hold it for the block, then hand it back.
        Raises LockUnavailable when the queue can't be reached, LeaseCancelled
        when the queue page cancelled the entry while it waited."""
        waiter = self._waiters[job_id] = _Waiter(asyncio.get_running_loop().create_future())
        entry: dict[str, Any] | None = None
        token: str | None = None
        try:
            entry = await self.enqueue(job_id, label, priority=priority, uses_llm=uses_llm, group=group,
                                       is_continuous=is_continuous, model=model, config=config, vram_mb=vram_mb)
            if entry is None:
                raise LockUnavailable("The GPU queue is not reachable.")
            waiter.entry_id = entry.get("entry_id")
            if not waiter.future.done() and on_wait is not None:
                await _maybe_await(on_wait(entry))
            token = await (asyncio.wait_for(asyncio.shield(waiter.future), timeout) if timeout else asyncio.shield(waiter.future))
            yield Turn(token=token, entry_id=entry.get("entry_id", ""), job_id=job_id, others=self.shared_with(token))
        finally:
            self._waiters.pop(job_id, None)
            # A grant can win the race with the cancellation of this waiter:
            # keep its token so the active entry is still handed back.
            if token is None and waiter.future.done() and not waiter.future.cancelled() and waiter.future.exception() is None:
                token = waiter.future.result()
            if not waiter.future.done():
                waiter.future.cancel()
            if token:
                await self.release(token, llm_settle=llm_settle)
            elif entry:
                await self.dequeue(entry.get("entry_id"))

    # ------------------------------------------------------------ stream

    async def _stream_loop(self) -> None:
        backoff = 0.25
        timeout = httpx.Timeout(REQUEST_TIMEOUT, read=10.0)   # the queue sends a comment every second
        while True:
            try:
                async with self._http().stream("GET", "/v2/stream", params={"app": self.app, "instance": self.instance},
                                               timeout=timeout) as response:
                    if response.status_code != 200:
                        raise httpx.HTTPStatusError(f"HTTP {response.status_code}", request=response.request, response=response)
                    event, data = None, []
                    async for line in response.aiter_lines():
                        if line == "":
                            if event:
                                self._dispatch(event, "\n".join(data))
                            event, data = None, []
                        elif line.startswith(":"):
                            continue
                        else:
                            name, _, value = line.partition(":")
                            value = value[1:] if value.startswith(" ") else value
                            if name == "event":
                                event = value
                            elif name == "data":
                                data.append(value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep reconnecting whatever happened
                if self.connected or backoff >= 2:
                    log.warning("bunny-lock stream lost (%s); reconnecting", exc)
            finally:
                if self._connected:
                    self._connected.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 2.0)

    def _dispatch(self, event: str, raw: str) -> None:
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            return
        if event == "hello":
            if self._connected:
                self._connected.set()
        elif event == "grant":
            self._others[data["token"]] = [str(a) for a in data.get("others") or []]
            self._spawn(self._handle_grant(data["entry_id"], data["job_id"], data["token"]))
        elif event == "prepare" and self.on_prepare is not None:
            self._spawn(self._handle_prepare(data["job_id"], data["entry_id"]))
        elif event == "adopt":
            self._spawn(self._handle_adopt(data["entry_id"], data["job_id"], bool(data.get("active")), data.get("token")))
        elif event == "cancel":
            self._spawn(self._handle_cancel(data["request_id"], data["job_id"], data["entry_id"]))

    async def _handle_grant(self, entry_id: str, job_id: str, token: str) -> None:
        if token in self._seen:       # re-sent after a reconnect
            if token in self._held:
                await self._post("/v2/ack", {"token": token})
            return
        self._seen.add(token)
        registering = self._enqueuing.get(job_id)
        if registering is not None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(registering.wait(), REGISTRATION_WAIT)
        await asyncio.sleep(0)        # the enqueuing caller registers its job first
        accepted = False
        try:
            waiter = self._waiters.get(job_id)
            if waiter is not None and not waiter.future.done():
                waiter.future.set_result(token)
                accepted = True
            elif self.on_grant is not None:
                accepted = bool(await _maybe_await(self.on_grant(job_id, entry_id, token)))
        except Exception:  # noqa: BLE001
            log.exception("GPU turn handler failed for %s", job_id)
        if accepted:
            self._held.add(token)
            await self._post("/v2/ack", {"token": token})
        else:
            self._others.pop(token, None)
            await self.release(token)

    async def _handle_adopt(self, entry_id: str, job_id: str, active: bool, token: str | None) -> None:
        """The app restarted: an entry of its previous process is on offer."""
        if token:
            self._seen.add(token)   # a re-sent grant for it must not start the job twice
        keep = False
        try:
            if self.on_adopt is not None:
                keep = bool(await _maybe_await(self.on_adopt(job_id, entry_id, active, token)))
        except Exception:  # noqa: BLE001
            log.exception("adopting %s failed", job_id)
        if keep:
            self._adopted.add(job_id)
            if active and token:
                self._held.add(token)
        await self._post("/v2/adopt", {"entry_id": entry_id, "keep": keep})
        if keep and active and token:
            await self._post("/v2/ack", {"token": token})

    async def requeue_lost(self, pending: dict[str, dict[str, Any]], on_entry: Callable[[str, dict[str, Any]], Any],
                           settle: float = 5.0) -> None:
        """After a restart: saved jobs the queue did not offer back (it was down or
        reset, or the app was away too long) are queued again, at the back.
        A saved job's "queue_model" / "queue_config" keep its VRAM peak key."""
        while self._connected is None or not self._connected.is_set():
            await asyncio.sleep(1)
        await asyncio.sleep(settle)
        for job_id in list(pending):
            item = pending.get(job_id)
            if item is None or job_id in self._adopted:
                continue
            while job_id in pending:
                entry = await self.enqueue(job_id, str(item.get("label") or job_id), group=item.get("group"), uses_llm=False,
                                           model=item.get("queue_model"), config=item.get("queue_config"))
                if entry:
                    on_entry(job_id, entry)
                    log.info("Queued %s again after the restart", job_id)
                    break
                await asyncio.sleep(10)

    async def _handle_prepare(self, job_id: str, entry_id: str) -> None:
        try:
            await _maybe_await(self.on_prepare(job_id, entry_id))
        except Exception:  # noqa: BLE001 - preparing is only a head start
            log.exception("prepare-early failed for %s", job_id)

    async def _handle_cancel(self, request_id: str, job_id: str, entry_id: str) -> None:
        status, body = 404, {"error": "not_found", "message": "This app no longer has that job."}
        try:
            result = await _maybe_await(self.on_cancel(job_id, entry_id)) if self.on_cancel else None
            if result is None:
                waiter = self._waiters.get(job_id)
                if waiter is not None and not waiter.future.done():
                    waiter.future.set_exception(LeaseCancelled(job_id))
                    status, body = 200, {"cancelled": True}
            elif isinstance(result, tuple):
                status, body = int(result[0]), result[1]
            elif isinstance(result, bool):
                status, body = (200, {"cancelled": True}) if result else (404, body)
            elif isinstance(result, int):
                status, body = result, {"status": result}
            elif isinstance(result, dict):
                status, body = 200, result
        except Exception as exc:  # noqa: BLE001
            log.exception("cancel handler failed for %s", job_id)
            status, body = 500, {"error": "cancel_failed", "message": str(exc)}
        await self._post("/v2/reply", {"request_id": request_id, "status": status, "body": body})

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            for token in list(self._held):
                response = await self._post("/v2/heartbeat", {"token": token, "instance": self.instance}, timeout=6)
                if response is not None and response.status_code == 404:
                    self._held.discard(token)   # the turn is gone (expired or released elsewhere)
