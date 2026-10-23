from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import signal
from typing import Any

import httpx

from .comfy import ComfyService, ServiceError
from .config import Settings
from .generation import reference_category, reference_token
from .models import EnhanceRequest, MODEL_LABELS, MODE_LABELS


log = logging.getLogger(__name__)


class QwenService:
    """OpenAI-compatible Qwen client; only processes launched here are stoppable."""

    def __init__(self, config: Settings, comfy: ComfyService):
        self.config = config
        self.comfy = comfy
        self.process: asyncio.subprocess.Process | None = None
        self.starting = False

    async def _request(self, method: str, route: str, payload: dict[str, Any] | None = None, timeout: float = 20) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(base_url=self.config.qwen_url, timeout=timeout) as client:
                response = await client.request(method, route, json=payload)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            raise ServiceError(f"Qwen is unavailable: {exc}") from exc

    async def status(self) -> dict[str, Any]:
        try:
            models = await self._request("GET", "/models", timeout=3)
            online = bool(models.get("data"))
        except ServiceError:
            online = False
        return {"online": online, "starting": self.starting, "managed": self.config.qwen_mode == "local"}

    async def start(self) -> None:
        if (await self.status())["online"]:
            return
        if self.config.qwen_mode != "local":
            raise ServiceError("Qwen is configured as an external service and cannot be started by H3 Machine.")
        if not self.config.qwen_command_path.is_file():
            raise ServiceError("The configured Qwen command file was not found.")
        comfy_status = await self.comfy.status()
        if comfy_status["rendering"] or comfy_status["waiting"]:
            raise ServiceError("An H3 render is active or queued. Wait for the video queue before starting Qwen.")
        await self.comfy.free_models()
        self.starting = True
        output = (self.config.log_dir / "qwen.log").open("ab")
        env = os.environ.copy()
        env.setdefault("QWEN38_PORT", "8081")
        env.setdefault("QWEN38_CTX_SIZE", "65536")
        try:
            self.process = await asyncio.create_subprocess_exec(
                "bash", "-lc", self.config.qwen_command_path.read_text(), env=env,
                stdout=output, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            )
            for _ in range(90):
                await asyncio.sleep(2)
                if self.process.returncode is not None:
                    raise ServiceError(f"Managed Qwen exited with code {self.process.returncode}.")
                if (await self.status())["online"]:
                    log.info("Managed Qwen is ready")
                    return
            raise ServiceError("Qwen did not become ready in three minutes.")
        finally:
            output.close()
            self.starting = False

    async def stop_for_render(self) -> None:
        if self.config.qwen_mode != "local" or not self.process or self.process.returncode is not None:
            return
        os.killpg(self.process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()
        self.process = None

    def reference_content(self, request: EnhanceRequest, references: list[dict[str, Any]], frames: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        if request.mode == "fl2v":
            items = [("@image1 / @First", frame) for frame in frames[:1]] + [("@image2 / @Last", frame) for frame in frames[1:2]]
        elif request.mode == "ref2v":
            # Qwen is a vision-language model - only images can actually be
            # shown to it. Video/audio references still get a text-only label
            # below so it knows their <Video N>/<Audio N> tokens exist and
            # doesn't invent new ones for them.
            items = [
                (reference_token(ref) or f"@{ref['alias']}", ref)
                for ref in references if reference_category(ref) == "image"
            ]
        else:
            items = []
        content: list[dict[str, Any]] = []
        labels: list[str] = []
        for alias, ref in items:
            path = self.config.upload_dir / ref["stored_name"]
            if not path.is_file():
                continue
            mime = mimetypes.guess_type(path.name)[0] or ref["mime_type"]
            encoded = base64.b64encode(path.read_bytes()).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
            labels.append(f"{alias} is reference image {len(labels) + 1}")
        if request.mode == "ref2v":
            for ref in references:
                if reference_category(ref) == "image":
                    continue
                token = reference_token(ref)
                if token:
                    kind = "video" if reference_category(ref) == "video" else "audio"
                    labels.append(f"{token} is a reference {kind} (not shown here; keep the token as-is if the draft uses it)")
        return content, "; ".join(labels) or "No reference image is selected."

    async def enhance(self, request: EnhanceRequest, references: list[dict[str, Any]], frames: list[dict[str, Any]]) -> str:
        status = await self.status()
        if not status["online"]:
            await self.start()
        models = await self._request("GET", "/models", timeout=5)
        model_list = models.get("data", [])
        if not model_list or not model_list[0].get("id"):
            raise ServiceError("Qwen did not report an available model.")
        guide = self.config.prompt_guide.read_text() if self.config.prompt_guide.is_file() else ""
        if request.model == "wan_2_2":
            system = (
                "You improve prompts for local Wan 2.2 video generation. Return only one production-ready prompt "
                "in natural prose (about 80-120 words) describing a single shot: subject, scene, motion, camera "
                "movement, lighting and style. Wan has no audio, so leave out dialogue and sound. Preserve the "
                "user's intent and any LoRA trigger words exactly; do not add headings or commentary."
            )
        elif request.model == "ltx_2_5":
            system = (
                "You improve prompts for local LTX 2.5 video generation. Return only one production-ready prompt "
                "in natural prose. Describe a single continuous shot, visible subjects and actions, camera movement, "
                "lighting, style, dialogue, sound effects, ambience, and music when relevant. Preserve the user's "
                "intent and any @First or @Last aliases exactly; do not add headings or commentary."
            )
        else:
            system = (
                "You improve prompts for local MiniMax H3 video. Use the official guide below as your rules. "
                "Return only a usable H3 prompt, preserving the three fields integrated_multimodal_description:, "
                "overall_soundscape:, and non_diegetic_music:. Use supplied reference images to preserve the visible "
                "subject, wardrobe, composition, and scene. In Reference mode, keep canonical tokens such as "
                "<Picture 1>, <Video 1>, and <Audio 1> exactly unchanged wherever the draft uses them; do not invent "
                "new ones. Keep @First and @Last aliases unchanged for Keyframe mode. For FL2V describe a continuous "
                f"path from the first frame to the last frame.\n\nOFFICIAL H3 PROMPT GUIDE:\n{guide}"
            )
        images, labels = self.reference_content(request, references, frames)
        user = [{"type": "text", "text": f"Model: {MODEL_LABELS[request.model]}. Mode: {MODE_LABELS[request.mode]}. Reference aliases: {labels}.\n\nUser draft:\n{request.prompt}"}, *images]
        payload = {
            "model": model_list[0]["id"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.35, "max_tokens": 900, "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False},
        }
        result = await self._request("POST", "/chat/completions", payload, timeout=300)
        try:
            return result["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise ServiceError("Qwen returned an invalid enhancement response.") from exc
