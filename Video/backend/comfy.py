from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
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
    """ComfyUI HTTP client with opt-in ownership of a locally launched process."""

    def __init__(self, config: Settings):
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.starting = False
        # Sticky across calls that don't know which model is in flight (the
        # background monitor_services() health-check loop's blind wake-up
        # nudge) - set the instant an explicit profile request starts (see
        # ensure_online()), not after it finishes, so a nudge that lands
        # during an in-flight profile-switch restart's brief offline window
        # doesn't race it back onto the wrong profile.
        self._last_profile: str | None = None

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
            queue = await self.request("GET", "/queue", timeout=8)
            return {
                "online": True,
                "starting": self.starting,
                "managed": self.config.comfy_mode == "local",
                "rendering": len(queue.get("queue_running", [])),
                "waiting": len(queue.get("queue_pending", [])),
                "running_ids": [item[1] for item in queue.get("queue_running", []) if len(item) > 1],
                "pending_ids": [item[1] for item in queue.get("queue_pending", []) if len(item) > 1],
            }
        except ServiceError:
            return {"online": False, "starting": self.starting, "managed": self.config.comfy_mode == "local", "rendering": 0, "waiting": 0, "running_ids": [], "pending_ids": []}

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
                    "Video will not issue a global interrupt."
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

    async def start(self) -> None:
        if self.config.comfy_mode != "local":
            raise ServiceError("ComfyUI is configured as an external service and cannot be started by Video.")
        # A separately launched ComfyUI Desktop instance may already own the
        # configured endpoint. Reuse it without claiming process ownership.
        if (await self.status())["online"]:
            log.info("ComfyUI is already available at %s", self.config.comfy_url)
            return
        if self.process and self.process.returncode is None:
            return
        if not self.config.comfy_command.strip():
            raise ServiceError("H3_COMFY_COMMAND is required in local service mode.")
        self.starting = True
        log_path = self.config.log_dir / "comfyui.log"
        output = log_path.open("ab")
        try:
            self.process = await asyncio.create_subprocess_exec(
                "bash", "-lc", self.config.comfy_command,
                stdout=output, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            )
            for _ in range(90):
                await asyncio.sleep(2)
                if self.process.returncode is not None:
                    raise ServiceError(f"Managed ComfyUI exited with code {self.process.returncode}.")
                if (await self.status())["online"]:
                    log.info("Managed ComfyUI is ready")
                    return
            raise ServiceError("Managed ComfyUI did not become ready in three minutes.")
        finally:
            output.close()
            self.starting = False

    async def ensure_supervisor_started(self, profile: str | None = None, wake_only: bool = False) -> None:
        """Best-effort: ask the host comfyui-supervisor to start ComfyUI when
        this app runs against it as an external service - see
        BunnyBytes/supervisors/comfyui_supervisor.py. Idempotent
        and safe to call repeatedly; swallows all errors, since this is a
        background self-heal nudge that callers must never block on. No-op in
        "local" mode, where this app already owns starting ComfyUI itself.

        `profile` selects a memory strategy the supervisor launches ComfyUI
        with - "h3" for MiniMax H3/SeedHunter's ~27GB Qwen3-VL-32B text
        encoder (doesn't fit the default profile's single-buffer VRAM
        staging on a 24GB card), vs. the default profile tuned for LTX 2.5.
        If the supervisor is already running a different profile, this
        restarts it onto the requested one - see supervisor's start()."""
        if self.config.comfy_mode == "local":
            return
        # wake_only: a bare "make sure something is up" nudge (the background
        # self-heal) - never the sticky profile, which the supervisor would
        # treat as a switch request and restart a ComfyUI another app is using.
        profile = None if wake_only else profile or self._last_profile
        if profile:
            self._last_profile = profile
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{self.config.comfyui_supervisor_url}/start",
                    **({"json": {"profile": profile}} if profile else {}),
                )
        except httpx.HTTPError:
            pass

    async def restart_if_idle(self) -> bool:
        """Restart the shared ComfyUI through the supervisor, on whatever
        profile it's running, but only while its queue is empty - it's shared
        with other apps, so never kill anyone else's render. Returns whether
        a restart was started. No-op in "local" mode."""
        if self.config.comfy_mode == "local":
            return False
        state = await self.status()
        if not state["online"] or state["rendering"] or state["waiting"]:
            return False
        profile = await self._supervisor_profile() or self._last_profile
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                await client.post(f"{self.config.comfyui_supervisor_url}/stop")
        except httpx.HTTPError:
            return False
        await self.ensure_supervisor_started(profile)
        return True

    async def comfy_pid(self) -> int | None:
        """The running ComfyUI's process id per the supervisor - it changes
        on every restart. None when unknown (local mode, supervisor down)."""
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(f"{self.config.comfyui_supervisor_url}/status")
                response.raise_for_status()
                return response.json().get("pid")
        except httpx.HTTPError:
            return None

    async def _supervisor_profile(self) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.config.comfyui_supervisor_url}/status")
                response.raise_for_status()
                return response.json().get("profile")
        except httpx.HTTPError:
            return None

    async def ensure_online(self, profile: str | None = None, timeout: float = 90.0, poll_interval: float = 2.0,
                            switch: bool = True) -> dict[str, Any]:
        """What the Generate button actually needs: not just a passive status
        read, but an active attempt to bring ComfyUI up - on the right memory
        profile for `profile`'s model family - before giving up. In "external"
        mode (this app's own container has no GPU and can't launch ComfyUI
        itself - see ensure_supervisor_started's docstring), a single offline
        status() used to fail the request immediately even though the
        supervisor could have started it right then; the background
        monitor_services() poll would eventually notice and nudge it, but a
        request made before that poll ticks (or before boot finishes) still
        lost. Also checks the supervisor's currently-running profile even
        when ComfyUI is already online - a mismatch (e.g. the previous
        request needed the LTX profile, this one needs "h3") needs its own
        restart, not just a wake-up nudge. In "local" mode this delegates to
        start(), which already owns launching ComfyUI with a fixed profile."""
        if self.config.comfy_mode == "local":
            state = await self.status()
            if state["online"]:
                return state
            try:
                await self.start()
            except ServiceError:
                pass
            return await self.status()
        if profile:
            # Set before any await below, so a concurrent background nudge
            # (see ensure_supervisor_started's sticky _last_profile) can't
            # land mid-restart and default onto the wrong profile.
            self._last_profile = profile
        state = await self.status()
        # switch=False (Generate, before this app holds the GPU lock): only
        # wake ComfyUI up - with this profile if it is down, since then nobody
        # is using it - but never restart a running one; submit_job() makes
        # the switch once the job owns the GPU (see ensure_profile).
        if state["online"] and (not switch or not profile or (await self._supervisor_profile()) in (None, profile)):
            return state
        await self.ensure_supervisor_started(profile)
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)
            state = await self.status()
            if state["online"]:
                return state
        return state

    async def ensure_profile(self, profile: str | None, timeout: float = 600.0, poll_interval: float = 3.0) -> None:
        """Bring ComfyUI onto `profile` before submitting. The supervisor only
        switches profiles while ComfyUI is idle (it is shared - a switch kills
        whatever runs), answering "busy" otherwise, so keep asking until it
        has switched and come back up. Raises ServiceError on timeout."""
        if not profile or self.config.comfy_mode == "local":
            return
        self._last_profile = profile
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            state = await self.status()
            current = await self._supervisor_profile()
            if state["online"] and current in (None, profile):  # None: launched some other way - leave it be
                return
            await self.ensure_supervisor_started(profile)
            if asyncio.get_event_loop().time() >= deadline:
                raise ServiceError(
                    f"ComfyUI stayed busy on the {current or 'other'} memory profile for {max(1, round(timeout / 60))} minute(s), so it "
                    f"couldn't switch to the {profile} profile this model needs. Try again once it's idle.")
            await asyncio.sleep(poll_interval)

    async def stop_managed(self) -> None:
        if not self.process or self.process.returncode is not None:
            return
        os.killpg(self.process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()
        self.process = None


def output_from_history(history: dict[str, Any], output_root: Path, node_id: str | None = None) -> Path | None:
    """Resolve one output video from a ComfyUI /history entry. When `node_id` is
    given, only that node's outputs are scanned (used by H3 SeedHunter to read
    each of the 3 distinct candidate-preview combine nodes individually, instead
    of "first match anywhere" - the default, unscoped behavior other models use)."""
    extensions = {".mp4", ".webm", ".mov", ".mkv", ".gif"}
    outputs = history.get("outputs", {})
    if node_id is not None:
        if node_id not in outputs:
            return None
        nodes = {node_id: outputs[node_id]}
    else:
        nodes = outputs
    for node in nodes.values():
        for collection in ("gifs", "videos", "images"):
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
