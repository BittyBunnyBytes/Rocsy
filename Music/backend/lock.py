from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import Settings


log = logging.getLogger(__name__)

ENQUEUE_REQUEST_TIMEOUT = 10.0


class LockClient:
    """Client for the cross-app bunny-lock GPU queue coordinator. Its
    internal surface (/enqueue, /heartbeat, /release, /dequeue) is gated by a
    shared X-Queue-Internal-Secret header, always sent here.

    heartbeat()/release() stay best-effort (a coordinator hiccup must never
    block or crash an already-running generation). enqueue() is not: a failed
    enqueue means the job genuinely cannot be queued, so it returns None on
    failure instead of swallowing it.
    """

    def __init__(self, config: Settings):
        self.config = config
        self._headers = {"X-Queue-Internal-Secret": config.queue_internal_secret}

    async def enqueue(self, app: str, job_id: str, label: str, is_continuous: bool = False) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(base_url=self.config.lock_url, timeout=ENQUEUE_REQUEST_TIMEOUT) as client:
                response = await client.post(
                    "/enqueue",
                    json={"app": app, "job_id": job_id, "label": label, "is_continuous": is_continuous},
                    headers=self._headers,
                )
                if response.status_code >= 400:
                    log.warning("bunny-lock /enqueue returned HTTP %s", response.status_code)
                    return None
                return response.json()
        except httpx.HTTPError as exc:
            log.warning("bunny-lock /enqueue failed (%s)", exc)
            return None
        except Exception:
            log.exception("Unexpected error calling bunny-lock /enqueue")
            return None

    async def dequeue(self, entry_id: str) -> None:
        try:
            async with httpx.AsyncClient(base_url=self.config.lock_url, timeout=5) as client:
                await client.post(f"/dequeue/{entry_id}", headers=self._headers)
        except httpx.HTTPError as exc:
            log.warning("bunny-lock /dequeue failed: %s", exc)

    async def heartbeat(self, token: str | None) -> None:
        if not token:
            return
        try:
            async with httpx.AsyncClient(base_url=self.config.lock_url, timeout=5) as client:
                await client.post("/heartbeat", json={"token": token}, headers=self._headers)
        except httpx.HTTPError as exc:
            log.warning("bunny-lock heartbeat failed: %s", exc)

    async def release(self, token: str | None) -> None:
        if not token:
            return
        try:
            async with httpx.AsyncClient(base_url=self.config.lock_url, timeout=5) as client:
                await client.post("/release", json={"token": token}, headers=self._headers)
        except httpx.HTTPError as exc:
            log.warning("bunny-lock release failed: %s", exc)

    async def heartbeat_loop(self, token: str | None, stop_event: asyncio.Event) -> None:
        """Heartbeat every lock_heartbeat_interval seconds until stop_event is
        set. Runs as a background task for the lifetime of one job's active
        ComfyUI submission; caller is responsible for calling release() on
        terminal state."""
        if not token:
            return
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.config.lock_heartbeat_interval)
            except asyncio.TimeoutError:
                await self.heartbeat(token)
