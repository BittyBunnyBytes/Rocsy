from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator
from typing import Any


def _finite(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def _dumps(data: Any) -> str:
    """JSON a browser can parse: an inf/NaN (json.dumps writes Infinity/NaN)
    would make the tab's JSON.parse throw and drop the update."""
    try:
        return json.dumps(data, allow_nan=False, default=str)
    except ValueError:
        return json.dumps(_finite(data), default=str)


class EventBroker:
    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    async def publish(self, event: str, data: Any) -> None:
        message = {"event": event, "data": data}
        for queue in tuple(self.subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    async def stream(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
        self.subscribers.add(queue)
        try:
            # Right away, not after the first 15 s ping: Cloudflare holds the
            # response headers until the first body byte, so the tab's "open"
            # (and its catch-up re-fetch after a reconnect) waited 15 s.
            yield "event: ping\ndata: {}\n\n"
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"event: {message['event']}\ndata: {_dumps(message['data'])}\n\n"
                except asyncio.TimeoutError:
                    # A named event (not a ": comment") so tabs can tell a live
                    # stream from one that died silently and reconnect only then.
                    yield "event: ping\ndata: {}\n\n"
        finally:
            self.subscribers.discard(queue)
