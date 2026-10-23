"""Advanced workflow config: per-workflow model-file overrides.

Each studio (Image/Krea, Video, Music) lists the fixed model files
its workflows load - VAE, text encoder, upscalers, built-in LoRAs, ... - as
"slots". The user can point any slot at another file ComfyUI offers for that
loader; the override is swapped into the API graph right before it is queued.

A slot is (loader class, input, default file). An override only replaces a
node whose value is still the workflow's default, so per-request pickers
(checkpoint menus, the Qwen text-encoder switch, user LoRAs) always win.

Slots come from two places: seeds the studio passes in (so the card is useful
before the first render) and slots learned from real submitted graphs (so
loaders added in code show up without a catalog edit). Learning skips the
inputs that are user-picked elsewhere (LoRA lists, checkpoint menus).

Mounted read-only into each studio at backend/workflow_models.py; the
matching UI is shared/ui/workflow-models.js.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("bunny.workflow_models")

MODEL_EXTENSIONS = (".safetensors", ".sft", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".pkl", ".onnx")
CLASS_LABELS = {
    "VAELoader": "VAE",
    "CLIPLoader": "Text encoder",
    "CLIPLoaderGGUF": "Text encoder (GGUF)",
    "DualCLIPLoader": "Text encoder",
    "UNETLoader": "Diffusion model",
    "UnetLoaderGGUF": "Diffusion model (GGUF)",
    "CheckpointLoaderSimple": "Checkpoint",
    "UpscaleModelLoader": "Upscale model",
    "LatentUpscaleModelLoader": "Latent upscaler",
    "LoraLoaderModelOnly": "LoRA",
    "AudioEncoderLoader": "Audio encoder",
    "CLIPVisionLoader": "CLIP vision",
    "ModelPatchLoader": "Model patch",
}
# Picked per request elsewhere in the studios (LoRA lists, checkpoint menus),
# so a graph's value here says nothing about the workflow's fixed files.
LEARN_SKIP_INPUTS = frozenset({"lora_name", "unet_name", "ckpt_name"})
CHOICES_TTL = 30.0


class WorkflowModelRequest(BaseModel):
    """PUT body: one slot's file (null = default), or reset a whole workflow."""
    model_config = ConfigDict(extra="forbid")

    workflow: str = Field(min_length=1, max_length=80)
    slot: str | None = Field(default=None, max_length=600)
    file: str | None = Field(default=None, max_length=400)
    reset: bool = False


@dataclass(frozen=True)
class Slot:
    class_type: str
    input: str
    default: str
    label: str = ""

    @property
    def id(self) -> str:
        return f"{self.class_type}.{self.input}:{self.default}"

    def public(self) -> dict[str, Any]:
        return {"id": self.id, **asdict(self), "label": self.label or CLASS_LABELS.get(self.class_type, self.class_type)}


def is_model_file(value: Any) -> bool:
    return isinstance(value, str) and value.lower().endswith(MODEL_EXTENSIONS)


def slots_from_graph(graph: dict[str, Any], skip_inputs: frozenset[str] = frozenset()) -> list[Slot]:
    """Loader inputs holding a model file in an API-format graph."""
    found: dict[str, Slot] = {}
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs")
        if "Load" not in class_type or not isinstance(inputs, dict):
            continue
        title = str((node.get("_meta") or {}).get("title") or "")
        for name, value in inputs.items():
            if name in skip_inputs or not is_model_file(value):
                continue
            # Default node titles are just the class name - not worth showing.
            label = title if title and title != class_type else ""
            slot = Slot(class_type, name, value, label)
            found.setdefault(slot.id, slot)
    return list(found.values())


def _combo_options(spec: Any) -> list[str] | None:
    """Options of a ComfyUI /object_info combo input (old list form or COMBO)."""
    if not isinstance(spec, list) or not spec:
        return None
    if isinstance(spec[0], list):
        return [str(item) for item in spec[0]]
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return [str(item) for item in spec[1].get("options") or []]
    return None


# ComfyUI models/ folder a loader reads: by class first, then by input name.
CLASS_FOLDERS = {
    "CheckpointLoaderSimple": "checkpoints",
    "CLIPVisionLoader": "clip_vision",
    "UpscaleModelLoader": "upscale_models",
    "LatentUpscaleModelLoader": "latent_upscale_models",
    "ModelPatchLoader": "model_patches",
    "AudioEncoderLoader": "audio_encoders",
    "MOSSImageUNETLoader": "diffusion_models",
    "MOSSImageCLIPLoader": "text_encoders",
}
INPUT_FOLDERS = {
    "ckpt_name": "checkpoints",
    "unet_name": "diffusion_models",
    "vae_name": "vae",
    "clip_name": "text_encoders",
    "clip_name1": "text_encoders",
    "clip_name2": "text_encoders",
    "clip_name3": "text_encoders",
    "clip_name4": "text_encoders",
    "lora_name": "loras",
    "control_net_name": "controlnet",
    "style_model_name": "style_models",
    "audio_encoder_name": "audio_encoders",
}
MISSING_CODE = "missing_model_files"


def file_folder(class_type: str, input_name: str) -> str:
    return CLASS_FOLDERS.get(class_type) or INPUT_FOLDERS.get(input_name) or ""


async def missing_files(comfy_url: str, graphs: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """Model files these API graphs load that ComfyUI doesn't offer, plus loader
    nodes ComfyUI doesn't have. Asked fresh (no cache), so a file linked or
    downloaded a moment ago counts. Both lists are empty when ComfyUI can't be
    asked: the studio's own offline error says more than a guess would."""
    wanted: dict[str, list[tuple[str, str]]] = {}
    for graph in graphs:
        for node in graph.values():
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                continue
            class_type = str(node.get("class_type") or "")
            for name, value in node["inputs"].items():
                if class_type and is_model_file(value) and (name, value) not in wanted.setdefault(class_type, []):
                    wanted[class_type].append((name, value))
    wanted = {key: value for key, value in wanted.items() if value}
    if not wanted:
        return {"files": [], "nodes": []}
    base = comfy_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            async def info(class_type: str) -> dict[str, Any]:
                response = await client.get(f"{base}/object_info/{class_type}")
                response.raise_for_status()
                return (response.json() or {}).get(class_type) or {}
            infos = dict(zip(wanted, await asyncio.gather(*(info(c) for c in wanted))))
    except (httpx.HTTPError, ValueError):
        return {"files": [], "nodes": []}
    files: dict[str, dict[str, str]] = {}
    nodes = []
    for class_type, pairs in wanted.items():
        spec = infos[class_type]
        if not spec:
            nodes.append(class_type)
            continue
        inputs = spec.get("input") or {}
        for name, value in pairs:
            options = _combo_options((inputs.get("required") or {}).get(name) or (inputs.get("optional") or {}).get(name))
            if options is None or value in options:
                continue  # free-text input (nothing to check) or installed
            files.setdefault(value, {"name": value, "folder": file_folder(class_type, name),
                                     "class_type": class_type, "input": name,
                                     "label": CLASS_LABELS.get(class_type, class_type)})
    return {"files": list(files.values()), "nodes": sorted(nodes)}


def missing_message(missing: dict[str, list]) -> str:
    """One line for toasts, agent logs and API clients that don't show the dialog."""
    parts = []
    if missing["files"]:
        names = [item["name"].rsplit("/", 1)[-1] for item in missing["files"]]
        parts.append(f"{len(names)} model file{'s' if len(names) > 1 else ''} missing in ComfyUI: "
                     + ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else ""))
    if missing["nodes"]:
        parts.append("ComfyUI doesn't have the node" + ("s " if len(missing["nodes"]) > 1 else " ")
                     + ", ".join(missing["nodes"]) + " (Models → Core Backend → Custom nodes)")
    return ". ".join(parts) + "."


class WorkflowModels:
    """Overrides + learned slots for one studio, stored in one JSON file."""

    def __init__(self, path: Path, comfy_url: str, workflows: dict[str, dict[str, Any]],
                 aliases: dict[str, str] | None = None):
        # workflows: key -> {"label": str, "slots": [Slot, ...]} (seeds, in display order)
        self.path = Path(path)
        self.comfy_url = comfy_url.rstrip("/")
        self.workflows = workflows
        self.aliases = aliases or {}
        self._choices: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._data = self._read()

    # -- storage --------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"overrides": {}, "learned": {}}
        except (OSError, json.JSONDecodeError):
            log.exception("Unreadable %s - moved aside, starting with no overrides", self.path)
            try:
                self.path.replace(self.path.with_name(self.path.name + ".corrupt"))
            except OSError:
                pass
            return {"overrides": {}, "learned": {}}
        data.setdefault("overrides", {})
        data.setdefault("learned", {})
        return data

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".workflow-models-")
        with os.fdopen(fd, "w") as handle:
            json.dump(self._data, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    # -- catalog --------------------------------------------------------------
    def key_for(self, model: str | None) -> str | None:
        key = self.aliases.get(model or "", model)
        return key if key in self.workflows else None

    def slots(self, key: str) -> list[Slot]:
        seeds = list(self.workflows[key].get("slots") or [])
        seen = {slot.id for slot in seeds}
        for item in self._data["learned"].get(key, []):
            slot = Slot(item["class_type"], item["input"], item["default"], item.get("label", ""))
            if slot.id not in seen:
                seen.add(slot.id)
                seeds.append(slot)
        return seeds

    def overrides(self, key: str) -> dict[str, str]:
        return dict(self._data["overrides"].get(key) or {})

    def learn(self, key: str, graph: dict[str, Any]) -> None:
        known = {slot.id for slot in self.slots(key)}
        new = [slot for slot in slots_from_graph(graph, LEARN_SKIP_INPUTS) if slot.id not in known]
        if not new:
            return
        self._data["learned"].setdefault(key, []).extend(asdict(slot) for slot in new)
        try:
            self._write()
        except OSError:
            log.exception("Could not save learned workflow slots")

    # -- render time ----------------------------------------------------------
    def apply(self, model: str | None, graph: dict[str, Any]) -> list[dict[str, str]]:
        """Learn this graph's slots, then swap in the overrides for its
        workflow. Returns what changed (for logs). Never raises: a broken
        override store must not block a render."""
        key = self.key_for(model)
        if key is None:
            return []
        try:
            self.learn(key, graph)
            wanted = {slot_id: file for slot_id, file in self.overrides(key).items() if file}
            if not wanted:
                return []
            by_id = {slot.id: slot for slot in self.slots(key)}
            changes = []
            for node_id, node in graph.items():
                if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                    continue
                for slot_id, file in wanted.items():
                    slot = by_id.get(slot_id)
                    if slot and node.get("class_type") == slot.class_type and node["inputs"].get(slot.input) == slot.default:
                        node["inputs"][slot.input] = file
                        changes.append({"node": str(node_id), "input": slot.input, "from": slot.default, "to": file})
            if changes:
                log.info("Workflow overrides for %s: %s", key,
                         ", ".join(f"{c['from']} -> {c['to']}" for c in changes))
            return changes
        except Exception:
            log.exception("Workflow overrides failed for %s; rendering with defaults", key)
            return []

    # -- API ------------------------------------------------------------------
    async def _class_info(self, client: httpx.AsyncClient, class_type: str) -> dict[str, Any] | None:
        cached = self._choices.get(class_type)
        if cached and time.monotonic() - cached[0] < CHOICES_TTL:
            return cached[1]
        try:
            response = await client.get(f"{self.comfy_url}/object_info/{class_type}", timeout=8)
            response.raise_for_status()
            info = (response.json() or {}).get(class_type)
        except (httpx.HTTPError, ValueError):
            return None  # offline: don't cache, try again next time
        self._choices[class_type] = (time.monotonic(), info)
        return info

    async def choices(self, slots: list[Slot]) -> tuple[bool, dict[str, list[str] | None]]:
        """(ComfyUI reachable, {class.input: options or None if unknown})."""
        result: dict[str, list[str] | None] = {}
        online = True
        async with httpx.AsyncClient() as client:
            for slot in slots:
                key = f"{slot.class_type}.{slot.input}"
                if key in result:
                    continue
                info = await self._class_info(client, slot.class_type)
                if info is None and slot.class_type not in self._choices:
                    online = False
                inputs = (info or {}).get("input") or {}
                spec = (inputs.get("required") or {}).get(slot.input) or (inputs.get("optional") or {}).get(slot.input)
                result[key] = _combo_options(spec)
        return online, result

    async def state(self, model: str | None = None) -> dict[str, Any]:
        all_slots = [slot for key in self.workflows for slot in self.slots(key)]
        online, options = await self.choices(all_slots)
        workflows = []
        for key, meta in self.workflows.items():
            overrides = self.overrides(key)
            rows = []
            for slot in self.slots(key):
                choices = options.get(f"{slot.class_type}.{slot.input}")
                rows.append({**slot.public(), "override": overrides.get(slot.id),
                             "choices": choices,
                             "default_missing": choices is not None and slot.default not in choices})
            workflows.append({"key": key, "label": meta.get("label", key), "note": meta.get("note", ""),
                              "slots": rows, "overridden": sum(1 for row in rows if row["override"])})
        return {"online": online, "current": self.key_for(model), "aliases": self.aliases, "workflows": workflows}

    async def set(self, key: str, slot_id: str, file: str | None) -> None:
        """file None/"" = back to the workflow default. Raises ValueError."""
        if key not in self.workflows:
            raise ValueError("Unknown workflow.")
        slot = next((item for item in self.slots(key) if item.id == slot_id), None)
        if slot is None:
            raise ValueError("Unknown model slot for this workflow.")
        overrides = self._data["overrides"].setdefault(key, {})
        if not file or file == slot.default:
            overrides.pop(slot_id, None)
        else:
            online, options = await self.choices([slot])
            allowed = options.get(f"{slot.class_type}.{slot.input}")
            if allowed is None:
                raise ValueError("ComfyUI is offline - can't check that file exists. Try again when it's up."
                                 if not online else f"ComfyUI doesn't list options for {slot.class_type}.")
            if file not in allowed:
                raise ValueError(f"{file} isn't available to {slot.class_type} in ComfyUI.")
            overrides[slot_id] = file
        if not overrides:
            self._data["overrides"].pop(key, None)
        self._write()

    def reset(self, key: str) -> None:
        if key not in self.workflows:
            raise ValueError("Unknown workflow.")
        self._data["overrides"].pop(key, None)
        self._write()
