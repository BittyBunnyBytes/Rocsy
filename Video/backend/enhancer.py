"""Hermes prompt-refinement task sent to the local llama-swap service."""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .comfy import ServiceError
from .agentic import plan_guide
from .generation import reference_category, reference_token
from .models import MODE_LABELS, MODEL_LABELS

STATE_KEY = "enhancement_settings"
REFERENCE_DIR = Path(__file__).resolve().parent
REFERENCE_FILES = ("h3-additional-syntax.md", "h3-guide.md", "h3-example.md")
LLAMA_URL = os.getenv("H3_LLAMA_URL", "http://127.0.0.1:8400").rstrip("/")
LLAMA_KEY = os.getenv("H3_LLAMA_API_KEY", "")
# llama-swap reads <model id>.ctx here when it starts a model (see ~/AI/Llama/llama-launch).
LLAMA_OVERRIDE_DIR = Path(os.getenv("H3_LLAMA_OVERRIDE_DIR", "/llama-overrides"))
# Models named "cli:<cli>/<model>" are external coding CLIs on the host (Settings → Sub-agents),
# answered by bunny-hostctl's chat-completions bridge instead of llama; "hermes:<provider>/<model>"
# are Hermes' other providers (Kimi Coding ...), proxied by bunny-hostctl with Hermes' own keys.
HOSTCTL_URL = os.getenv("HOSTCTL_URL", "http://127.0.0.1:8899").rstrip("/")
HOSTCTL_SECRET = os.getenv("HOSTCTL_SECRET", "")
CONTEXT_LIMITS = (8192, 262144)
DEFAULT_INSTRUCTIONS = (
    "You are the Hermes agent refining the user's MiniMax H3 video prompt. Treat the user's prompt as the task. "
    "Apply the three attached reference files according to the selected generation mode. Preserve the user's intent, "
    "exact dialogue, lyrics, visible text, timing and supplied reference tokens. Never invent references. "
    "Return only a complete refined prompt, with no explanation or markdown fence."
)
LTX_INSTRUCTIONS = (
    "You are the Hermes agent refining an LTX 2.5 video prompt. Preserve intent, dialogue and reference aliases. "
    "Return only one production-ready prompt with visual action, camera, lighting and sound."
)
# Wan 2.2's own prompt guide: one shot in plain prose, subject + scene +
# motion + camera + lighting/style, about 80-120 words. It renders no audio.
WAN_INSTRUCTIONS = (
    "You are the Hermes agent refining a Wan 2.2 video prompt. Preserve intent and any visible text. "
    "Return one production-ready prompt in plain prose (about 80-120 words) for a single 5-10 second shot: "
    "the subject and its appearance, the scene, the motion as it unfolds, camera movement and framing, "
    "lighting, color and style. Wan renders no sound, so leave out dialogue, music and sound effects."
)
EXAMPLE_MODES = ("default", "text", "fl2v", "ref2v")
DEFAULT_REFINEMENT_PROMPT = (
    "Review the sampled frames against the clip prompt and the user's master prompt. "
    "Look only for glaring visual failures: broken anatomy, severe camera movement, "
    "lost subject or scene continuity, and a clear misunderstanding of the requested action. "
    "Pass acceptable footage. If a serious issue is visible, explain it and return a complete "
    "revised H3 prompt that keeps the original intent, timing, references, and continuation "
    "placeholders. Do not suggest or edit application code. "
    "Treat these sampled frames as limited evidence; do not claim to have heard the audio."
)
DEFAULTS: dict[str, Any] = {
    "provider": "llama", "model": "qwen3.8-flash-next-uncensored",
    "temperature": 0.7, "thinking_level": "none", "context_size": 65536, "top_p": 1.0, "top_k": 40,
    "min_p": 0.0, "repeat_penalty": 1.0, "seed": -1, "extra_body": {},
    "instructions": DEFAULT_INSTRUCTIONS,
    "agentic_stage_enabled": False, "agentic_stage_preset": "",
    "agentic_full_preset": "", "agentic_stage_review": True,
    "agentic_stage_approval": False, "agentic_whisper_check": False,
    "agentic_stage_series_review": False, "agentic_stage_best_of_two": False,
    "agentic_final_whisper_check": False, "agentic_final_series_review": False, "agentic_final_best_of_two": False,
    "agentic_final_review": False, "agentic_refinement_prompt": DEFAULT_REFINEMENT_PROMPT,
    "agentic_browser_enabled": False, "agentic_manage_references": False, "agentic_video_search": False,
    "agentic_character_screenshots": False, "agentic_lora_gallery": False, "agentic_continuation_tail": False,
    "agentic_continuation_tail_audio": False,
    "agentic_scene_screenshots": False, "agentic_rushed_dialogue_check": False,
    "agentic_review_cap": True, "agentic_review_rounds": 1,
    # Per-stage thinking levels; "inherit" uses thinking_level (which also writes the plan).
    "agentic_thinking_research": "inherit", "agentic_thinking_plan_review": "inherit",
    "agentic_thinking_plan_fixes": "inherit", "agentic_thinking_footage_review": "inherit",
    "agentic_thinking_frame_analysis": "inherit", "agentic_thinking_screenshots": "low",
}
THINKING_LEVELS = {"none", "default", "minimal", "low", "medium", "high", "xhigh", "max"}
# Revision rounds each Hermes review (clip plan, preview footage, final footage) may run before Agentic moves on.
REVIEW_ROUNDS_MAX = 10
# With the cap switched off reviews run until they pass; this ceiling only stops a reviewer that never passes.
REVIEW_ROUNDS_UNCAPPED = 25


def load(store: Any) -> dict[str, Any]:
    saved = store.get_state(STATE_KEY) or {}
    merged = {**DEFAULTS, **{key: value for key, value in saved.items() if key in DEFAULTS}}
    if saved.get("provider") in {"deepseek", "qwen"}:
        merged["provider"] = "llama"
        if str(saved.get("model", "")).startswith("deepseek-"):
            merged["model"] = DEFAULTS["model"]
    return merged


def public(settings: dict[str, Any]) -> dict[str, Any]:
    return {**settings, "base_url": LLAMA_URL, "default_instructions": DEFAULT_INSTRUCTIONS}


def update(store: Any, changes: dict[str, Any]) -> dict[str, Any]:
    settings = load(store)
    if isinstance(changes.get("model"), str) and changes["model"].strip():
        settings["model"] = changes["model"].strip()[:200]
    if "thinking_level" in changes:
        if changes["thinking_level"] not in THINKING_LEVELS:
            raise ValueError("Unsupported thinking level")
        settings["thinking_level"] = changes["thinking_level"]
    for key in [key for key in DEFAULTS if key.startswith("agentic_thinking_")]:
        if key in changes:
            if changes[key] not in THINKING_LEVELS | {"inherit"}:
                raise ValueError(f"Unsupported thinking level for {key}")
            settings[key] = changes[key]
    bounds = {"temperature": (0, 2), "top_p": (0, 1), "min_p": (0, 1), "repeat_penalty": (0, 4),
              "top_k": (0, 1000), "seed": (-1, 2147483647), "context_size": CONTEXT_LIMITS}
    for key, (low, high) in bounds.items():
        if key in changes:
            try:
                value = float(changes[key])
            except (ValueError, TypeError):
                raise ValueError(f"{key} must be a number") from None
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            settings[key] = int(value) if key in {"top_k", "seed", "context_size"} else value
    if isinstance(changes.get("instructions"), str):
        settings["instructions"] = changes["instructions"].strip() or DEFAULT_INSTRUCTIONS
    if "extra_body" in changes:
        extra = changes["extra_body"]
        if not isinstance(extra, dict) or len(str(extra)) > 20000 or any(key in extra for key in ("model", "messages", "stream")):
            raise ValueError("Extra settings must be a JSON object and cannot replace model, messages or stream")
        settings["extra_body"] = extra
    for key in ("agentic_stage_enabled", "agentic_stage_review", "agentic_stage_approval", "agentic_whisper_check", "agentic_final_review",
                "agentic_stage_series_review", "agentic_stage_best_of_two", "agentic_final_whisper_check",
                "agentic_final_series_review", "agentic_final_best_of_two",
                "agentic_browser_enabled", "agentic_manage_references", "agentic_video_search",
                "agentic_character_screenshots", "agentic_lora_gallery", "agentic_continuation_tail",
                "agentic_continuation_tail_audio", "agentic_scene_screenshots", "agentic_rushed_dialogue_check",
                "agentic_review_cap"):
        if key in changes:
            if not isinstance(changes[key], bool):
                raise ValueError(f"{key} must be a toggle")
            settings[key] = changes[key]
    if "agentic_review_rounds" in changes:
        rounds = changes["agentic_review_rounds"]
        if isinstance(rounds, bool) or not isinstance(rounds, (int, float)) or rounds != int(rounds) \
                or not 0 <= rounds <= REVIEW_ROUNDS_MAX:
            raise ValueError(f"Review rounds must be a whole number from 0 to {REVIEW_ROUNDS_MAX}")
        settings["agentic_review_rounds"] = int(rounds)
    for key in ("agentic_stage_preset", "agentic_full_preset"):
        if key in changes:
            if not isinstance(changes[key], str) or len(changes[key]) > 60:
                raise ValueError(f"{key} must be a saved preset name")
            settings[key] = changes[key].strip()
    if "agentic_refinement_prompt" in changes:
        prompt = changes["agentic_refinement_prompt"]
        if not isinstance(prompt, str):
            raise ValueError("Refinement prompt must be text")
        settings["agentic_refinement_prompt"] = prompt.strip() or DEFAULT_REFINEMENT_PROMPT
    store.set_state(STATE_KEY, settings)
    return settings


def reference_lines(mode: str, references: list[dict[str, Any]], frames: list[dict[str, Any]]) -> list[str]:
    if mode == "fl2v":
        names = ["@First (first frame image)", "@Last (last frame image)"]
        return [names[index] + (" - " + frame["label"] if frame.get("label") else "") for index, frame in enumerate(frames[:2])]
    if mode != "ref2v":
        return []
    lines = []
    for ref in references:
        token = reference_token(ref) or f"@{ref['alias']}"
        kind = reference_category(ref)
        detail = kind + (f", {ref['duration']:.1f}s" if ref.get("duration") else "")
        lines.append(f"{token} (plan alias: {ref['alias']}) - {detail}" + (" - " + ref["label"] if ref.get("label") else ""))
    return lines


def build_messages(settings: dict[str, Any], model: str, mode: str, prompt: str, duration: float | None,
                   references: list[dict[str, Any]], frames: list[dict[str, Any]],
                   additional_prompt: str = "", plan_mode: str = "none",
                   session_context: str = "", research_context: str = "",
                   plan_note: str = "", extra_parts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    h3 = model in ("minimax_h3", "seedhunter")
    documents = "\n\n".join(
        f"REFERENCE FILE: {name}\n<<<\n{(REFERENCE_DIR / name).read_text(encoding='utf-8')}\n>>>"
        for name in REFERENCE_FILES
    ) if h3 else ""
    context = [f"Model: {MODEL_LABELS.get(model, model)}", f"Mode: {MODE_LABELS.get(mode, mode)}"]
    if duration:
        context.append(f"Duration: {duration:g} seconds")
    if plan_mode == "always":
        # Agentic: the Studio form's values are only what the user left in the form, not a request.
        context[1:] = [f"Studio form mode: {MODE_LABELS.get(mode, mode)} (only a default: choose each clip's mode yourself unless "
                       "the request asks for one)"] + ([f"Studio form duration: {duration:g} s (only a default: give each clip the "
                       "length its action " + ("needs, 2-10 s (Wan 2.2 looks best at 5 s)" if model == "wan_2_2" else "and dialogue need, 5-15 s")
                       + ", unless the request states lengths)"] if duration else [])
    refs = reference_lines("ref2v" if plan_mode != "none" and references else mode, references, frames)
    context.append("Attached references:\n" + "\n".join(f"- {line}" for line in refs) if refs else "Attached references: none")
    if plan_mode != "none":
        context.append("Available keyframes: " + ("First" if frames else "none") + (", Last" if len(frames) > 1 else ""))
    instruction = settings["instructions"] if h3 else WAN_INSTRUCTIONS if model == "wan_2_2" else LTX_INSTRUCTIONS
    if plan_mode != "none":
        instruction += "\n\n" + plan_guide(model)
        instruction += ("\nThe additional Agentic guide is the user's latest instruction. "
                        "If it changes the scene or subject, follow it and discard conflicting older "
                        "prompt details rather than combining unrelated scenes.")
        if plan_mode == "always":
            instruction += "\nFor this Agentic request, ALWAYS return a ::VIDEO plan, even for one clip."
        if plan_note:
            instruction += "\n" + plan_note
    extra = (f"\n\nADDITIONAL REFINEMENT REQUEST:\n<<<\n{additional_prompt.strip()}\n>>>" if additional_prompt.strip() and additional_prompt.strip() != prompt.strip() else "")
    research = f"\n\nAGENT WEB RESEARCH AND ACQUIRED REFERENCES:\n<<<\n{research_context}\n>>>" if research_context else ""
    prior = f"\n\nPRIOR AGENTIC SESSION:\n<<<\n{session_context}\n>>>" if session_context else ""
    text = ("GENERATION SETTINGS:\n" + "\n".join(context) + prior + research + extra
            + f"\n\n{MODEL_LABELS.get(model, model).upper() if not h3 else 'H3'} PROMPT TO REFINE:\n<<<\n{prompt}\n>>>\n\nReturn only the refined prompt or the required clip plan.")
    # Frames of attached videos (Agentic), so Hermes sees what they show.
    user: Any = [{"type": "text", "text": text}, *extra_parts] if extra_parts else text
    return [{"role": "system", "content": instruction + ("\n\n" + documents if documents else "")},
            {"role": "user", "content": user}]


def clean_output(value: str) -> str:
    value = value.strip()
    fenced = re.fullmatch(r"```[\w-]*\n(.*?)\n?```", value, re.S)
    if fenced:
        value = fenced.group(1).strip()
    if value.startswith("<<<") and value.endswith(">>>"):
        value = value[3:-3].strip()
    return value


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {LLAMA_KEY}"} if LLAMA_KEY else {}


async def models() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(LLAMA_URL + "/v1/models", headers=_headers())
            response.raise_for_status()
            return [item["id"] for item in response.json().get("data", []) if isinstance(item.get("id"), str)]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise ServiceError(f"Could not list llama models: {exc}") from exc


async def apply_context_size(settings: dict[str, Any]) -> bool:
    """Write the agent's context size for its model; when it changed, unload so llama-swap restarts the model with it.
    Called at the start of every chat (inside the caller's GPU turn), so a new value applies to the next request."""
    model = str(settings.get("model") or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", model) or not LLAMA_OVERRIDE_DIR.is_dir():
        return False
    value = str(int(settings.get("context_size") or DEFAULTS["context_size"]))
    path = LLAMA_OVERRIDE_DIR / f"{model}.ctx"
    try:
        if path.exists() and path.read_text().strip() == value:
            return False
        path.write_text(value + "\n")
    except OSError:
        return False
    await unload()
    return True


async def chat(settings: dict[str, Any], messages: list[dict[str, Any]],
               progress: Callable[[int, int], None] | None = None,
               on_delta: Callable[[str, str], None] | None = None) -> dict[str, Any]:
    await apply_context_size(settings)
    payload: dict[str, Any] = {"model": settings["model"], "messages": messages, "stream": True}
    for key in ("temperature", "top_p", "top_k", "min_p", "repeat_penalty", "seed"):
        payload[key] = settings[key]
    payload.update(settings.get("extra_body") or {})
    level = settings["thinking_level"]
    if level == "default":
        payload.pop("reasoning_effort", None)
    else:
        payload["reasoning_effort"] = level
    if "qwen" in settings["model"].lower() and level != "default":
        template = payload.get("chat_template_kwargs")
        payload["chat_template_kwargs"] = {**(template if isinstance(template, dict) else {}),
                                           "enable_thinking": level != "none"}
    payload["stream"] = True
    payload["max_tokens"] = -1
    payload["n_predict"] = -1
    started = time.monotonic()
    content_parts: list[str] = []
    content_chars = reasoning_chars = 0
    finish_reason = None
    usage: dict[str, Any] = {}
    model = settings["model"]
    completed = False
    last_progress = 0.0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=15)) as client:
            bridge = "clis" if model.startswith("cli:") else "providers" if model.startswith("hermes:") else ""
            url, headers = ((f"{HOSTCTL_URL}/{bridge}/chat", {"X-Hostctl-Secret": HOSTCTL_SECRET}) if bridge
                            else (LLAMA_URL + "/v1/chat/completions", _headers()))
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode(errors="replace")
                    raise ServiceError(f"Llama service HTTP {response.status_code}: {detail[:300]}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        completed = True
                        break
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except ValueError as exc:
                        raise ServiceError("Llama service sent invalid streaming data") from exc
                    if data.get("error"):
                        raise ServiceError(f"Llama service: {str(data['error'])[:300]}")
                    if data.get("model"):
                        model = data["model"]
                    if data.get("usage"):
                        usage = data["usage"]
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        content = delta.get("content") or ""
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        if on_delta and (content or reasoning):
                            on_delta(content, reasoning)
                        if content:
                            content_parts.append(content)
                            content_chars += len(content)
                        reasoning_chars += len(reasoning)
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                    now = time.monotonic()
                    if progress and (content_chars or reasoning_chars) and now - last_progress >= 2:
                        progress(content_chars, reasoning_chars)
                        last_progress = now
    except httpx.HTTPError as exc:
        raise ServiceError(f"Could not reach llama service: {exc}") from exc
    if not completed and not finish_reason:
        raise ServiceError("Llama service ended its stream before the prompt was complete")
    if progress:
        progress(content_chars, reasoning_chars)
    return {"content": "".join(content_parts), "usage": usage, "model": model,
            "finish_reason": finish_reason, "seconds": round(time.monotonic() - started, 1)}


async def enhance(settings: dict[str, Any], model: str, mode: str, prompt: str, duration: float | None,
                  references: list[dict[str, Any]], frames: list[dict[str, Any]],
                  additional_prompt: str = "", progress: Callable[[int, int], None] | None = None,
                  plan_mode: str = "none", session_context: str = "",
                  research_context: str = "", on_delta: Callable[[str, str], None] | None = None,
                  plan_note: str = "", extra_parts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result = await chat(settings, build_messages(settings, model, mode, prompt, duration, references, frames,
                                                 additional_prompt, plan_mode, session_context, research_context,
                                                 plan_note, extra_parts), progress, on_delta)
    refined = clean_output(result["content"])
    if not refined:
        raise ServiceError("Hermes returned an empty prompt")
    if result["finish_reason"] == "length":
        raise ServiceError("The model stopped at its context limit before finishing the prompt.")
    return {"prompt": refined, "usage": result["usage"], "model": result["model"], "seconds": result["seconds"]}


async def unload() -> None:
    """Release llama-swap's model while still holding the shared GPU turn."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            await client.get(LLAMA_URL + "/unload", headers=_headers())
    except httpx.HTTPError:
        pass
