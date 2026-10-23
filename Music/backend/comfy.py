from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
import websockets

from .config import Settings


log = logging.getLogger(__name__)


class ServiceError(RuntimeError):
    pass


class ComfyHTTPError(ServiceError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class ComfyService:
    """ComfyUI HTTP + websocket client. ComfyUI itself is always external here
    (started/managed on the host independently of this app)."""

    def __init__(self, config: Settings):
        self.config = config

    async def request(self, method: str, route: str, payload: dict[str, Any] | None = None, timeout: float = 20) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(base_url=self.config.comfy_url, timeout=timeout) as client:
                response = await client.request(method, route, json=payload)
                response.raise_for_status()
                return response.json() if response.content else {}
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                body = exc.response.json()
                detail = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body.get("error", "")
                errors = body.get("node_errors", {})
                if errors:
                    node, report = next(iter(errors.items()))
                    first = (report.get("errors") or [{}])[0]
                    reason = first.get("details") or first.get("message")
                    if reason:
                        detail = f"{detail} (node {node}: {reason})"
            except Exception:
                detail = exc.response.text[:500]
            raise ComfyHTTPError(
                exc.response.status_code,
                f"ComfyUI returned HTTP {exc.response.status_code}{': ' + str(detail) if detail else ''}",
            ) from exc
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ServiceError(f"ComfyUI is unavailable: {exc}") from exc

    async def status(self) -> dict[str, Any]:
        try:
            queue = await self.request("GET", "/queue", timeout=3)
            return {
                "online": True,
                "rendering": len(queue.get("queue_running", [])),
                "waiting": len(queue.get("queue_pending", [])),
                "running_ids": [item[1] for item in queue.get("queue_running", []) if len(item) > 1],
            }
        except ServiceError:
            return {"online": False, "rendering": 0, "waiting": 0, "running_ids": []}

    async def submit(self, workflow: dict[str, Any], client_id: str) -> str:
        # YuE2's SaveAudioAdvanced is a schema-based ComfyNode output. Ask
        # ComfyUI to execute it explicitly so API validation includes the
        # complete music graph instead of accepting only unrelated legacy
        # output nodes (which used to finish in ~2 seconds without audio).
        result = await self.request(
            "POST", "/prompt",
            {"prompt": workflow, "client_id": client_id, "partial_execution_targets": ["10"]},
            timeout=30,
        )
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise ServiceError("ComfyUI accepted no prompt ID.")
        return str(prompt_id)

    async def history(self, prompt_id: str) -> dict[str, Any] | None:
        result = await self.request("GET", f"/history/{prompt_id}", timeout=10)
        return result.get(prompt_id)

    async def cancel(self, prompt_id: str) -> bool:
        """Cancel one prompt without interrupting unrelated ComfyUI work."""
        try:
            result = await self.request("POST", f"/api/jobs/{prompt_id}/cancel", timeout=15)
            return bool(result.get("cancelled"))
        except ComfyHTTPError as exc:
            if exc.status_code != 404:
                raise

        # Compatibility path for ComfyUI versions without the targeted jobs API.
        queue = await self.request("GET", "/queue", timeout=10)
        pending_ids = {item[1] for item in queue.get("queue_pending", []) if len(item) > 1}
        running_ids = {item[1] for item in queue.get("queue_running", []) if len(item) > 1}
        if prompt_id in pending_ids:
            await self.request("POST", "/queue", {"delete": [prompt_id]}, timeout=15)
            return True
        if prompt_id in running_ids:
            if running_ids != {prompt_id}:
                raise ServiceError(
                    "This ComfyUI version lacks targeted cancellation and is rendering unrelated jobs; "
                    "Music will not issue a global interrupt."
                )
            await self.request("POST", "/interrupt", {"prompt_id": prompt_id}, timeout=15)
            return True
        return False

    async def events(self, client_id: str):
        """Yield ComfyUI websocket events for prompts submitted by this client."""
        parsed = urlparse(self.config.comfy_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        base_path = parsed.path.rstrip("/")
        url = urlunparse((scheme, parsed.netloc, f"{base_path}/ws", "", urlencode({"clientId": client_id}), ""))
        async with websockets.connect(url, open_timeout=5, ping_interval=20, ping_timeout=20) as socket:
            log.info("Connected to ComfyUI progress stream")
            async for message in socket:
                if not isinstance(message, str):
                    continue
                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event

    async def free_models(self) -> None:
        try:
            await self.request("POST", "/free", {"unload_models": True, "free_memory": True}, timeout=15)
        except ServiceError:
            pass

    async def ensure_supervisor_started(self) -> None:
        """Best-effort: ask the host comfyui-supervisor to start ComfyUI when
        it's unreachable - see
        BunnyBytes/supervisors/comfyui_supervisor.py. Idempotent
        and safe to call repeatedly; swallows all errors, since this is a
        background self-heal nudge that callers must never block on."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{self.config.comfyui_supervisor_url}/start")
        except httpx.HTTPError:
            pass


def output_from_history(history: dict[str, Any], output_root: Path) -> Path | None:
    # ComfyUI's SaveAudioAdvanced node writes .flac/.wav/.mp3 (configured via
    # its "format" input) rather than the image/video extensions Video
    # looks for.
    extensions = {".flac", ".wav", ".mp3", ".opus"}
    for node in history.get("outputs", {}).values():
        for collection in ("audio", "images"):
            for item in node.get(collection, []) or []:
                filename = Path(str(item.get("filename", ""))).name
                if Path(filename).suffix.lower() not in extensions:
                    continue
                subfolder = Path(str(item.get("subfolder", "")))
                if subfolder.is_absolute() or ".." in subfolder.parts:
                    continue
                candidate = (output_root / subfolder / filename).resolve()
                try:
                    candidate.relative_to(output_root.resolve())
                except ValueError:
                    continue
                if candidate.is_file():
                    return candidate
    return None
