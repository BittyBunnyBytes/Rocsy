from __future__ import annotations

import asyncio

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
        result = await self.request("POST", "/prompt", {"prompt": workflow, "client_id": client_id}, timeout=30)
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
                    "Krea will not issue a global interrupt."
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

    async def _supervisor_profile(self) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                return (await client.get(f"{self.config.comfyui_supervisor_url}/status")).json().get("profile")
        except (httpx.HTTPError, ValueError):
            return None

    async def ensure_profile(self, profile: str, timeout: float = 600.0, poll_interval: float = 3.0) -> None:
        """Bring the shared ComfyUI onto the memory profile this model is fastest on
        (see the supervisor's COMFY_ARGS_KREA / COMFY_ARGS_QWEN). The supervisor only
        switches while ComfyUI is idle and answers "busy" otherwise, so keep asking
        until it runs `profile` and answers again (same approach as Video)."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            state = await self.status()
            current = await self._supervisor_profile()
            if state["online"] and current in (None, profile):  # None: launched some other way - leave it be
                return
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(f"{self.config.comfyui_supervisor_url}/start", json={"profile": profile})
            except httpx.HTTPError:
                pass
            if loop.time() >= deadline:
                raise ServiceError(f"ComfyUI stayed busy on the {current or 'other'} profile, so it couldn't switch to the "
                                   f"{profile} profile. Try again once it's idle.")
            await asyncio.sleep(poll_interval)

    async def view_input(self, filename: str) -> tuple[bytes, str]:
        """An image in ComfyUI's input folder (a reference uploaded via upload_image)."""
        try:
            async with httpx.AsyncClient(base_url=self.config.comfy_url, timeout=20) as client:
                response = await client.get("/view", params={"filename": filename, "type": "input"})
                response.raise_for_status()
                return response.content, response.headers.get("content-type", "image/png")
        except httpx.HTTPStatusError as exc:
            raise ComfyHTTPError(exc.response.status_code, f"ComfyUI returned HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ServiceError(f"ComfyUI is unavailable: {exc}") from exc

    async def upload_image(self, filename: str, content: bytes, content_type: str) -> str:
        """Uploads a reference image into ComfyUI's own input directory (the
        only place its LoadImage combo can see files) and returns the stored
        filename to reference from a workflow's `image` input."""
        try:
            async with httpx.AsyncClient(base_url=self.config.comfy_url, timeout=30) as client:
                response = await client.post(
                    "/upload/image",
                    files={"image": (filename, content, content_type or "application/octet-stream")},
                    data={"type": "input", "overwrite": "false"},
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            raise ComfyHTTPError(exc.response.status_code, f"ComfyUI rejected the upload: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ServiceError(f"ComfyUI is unavailable: {exc}") from exc
        name = body.get("name")
        if not name:
            raise ServiceError("ComfyUI accepted the upload but returned no filename.")
        return str(name)

    async def free_models(self) -> None:
        try:
            await self.request("POST", "/free", {"unload_models": True, "free_memory": True}, timeout=15)
        except ServiceError:
            pass


def output_from_history(history: dict[str, Any], output_root: Path) -> Path | None:
    extensions = {".png", ".jpg", ".jpeg", ".webp"}
    for node in history.get("outputs", {}).values():
        for item in node.get("images", []) or []:
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
