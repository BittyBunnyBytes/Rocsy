from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings


log = logging.getLogger(__name__)

LOCK_REQUEST_TIMEOUT = 5.0
ENQUEUE_REQUEST_TIMEOUT = 10.0


class LockClient:
    """HTTP client for bunny-lock, the BunnyBytes cross-app GPU queue
    coordinator (127.0.0.1-only). Its internal surface (/enqueue, /heartbeat,
    /release, /dequeue) is gated by a shared X-Queue-Internal-Secret header,
    always sent here.

    heartbeat()/release() remain best-effort (a coordinator hiccup must never
    crash or block an already-running generation). enqueue()/dequeue() are not
    best-effort in the same way -- a failed enqueue means the job genuinely
    cannot be queued and the caller must surface that, so they return None on
    failure instead of silently swallowing it.
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
            async with httpx.AsyncClient(base_url=self.config.lock_url, timeout=LOCK_REQUEST_TIMEOUT) as client:
                response = await client.post(f"/dequeue/{entry_id}", headers=self._headers)
                if response.status_code >= 400:
                    log.warning("bunny-lock /dequeue returned HTTP %s", response.status_code)
        except httpx.HTTPError as exc:
            log.warning("bunny-lock /dequeue failed (%s); continuing without it", exc)
        except Exception:
            log.exception("Unexpected error calling bunny-lock /dequeue")

    async def _call(self, path: str, token: str) -> None:
        try:
            async with httpx.AsyncClient(base_url=self.config.lock_url, timeout=LOCK_REQUEST_TIMEOUT) as client:
                response = await client.post(path, json={"token": token}, headers=self._headers)
                if response.status_code >= 400:
                    log.warning("bunny-lock %s returned HTTP %s", path, response.status_code)
        except httpx.HTTPError as exc:
            log.warning("bunny-lock %s failed (%s); continuing without it", path, exc)
        except Exception:
            log.exception("Unexpected error calling bunny-lock %s", path)

    async def heartbeat(self, token: str) -> None:
        await self._call("/heartbeat", token)

    async def release(self, token: str) -> None:
        await self._call("/release", token)
