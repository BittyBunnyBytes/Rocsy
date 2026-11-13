"""Bunny conditioning cache: text encoding without reloading the text encoder for prompts it has seen.

The Image service runs ComfyUI with --cache-none (shared Krea 2 / Lustify / Qwen state poisoned fp8
renders), so every prompt reloads the 4-9 GB text encoder and re-encodes, even for the same prompt
(Retry, seed rerolls, every image of an "x4" batch). On few-step models that is up to half the job.

BunnyCachedEncode wraps one stock encoder node (CLIPTextEncode, TextEncodeQwenImage21,
TextEncodeQwenImageEditPlus) and stores its outputs, keyed by: the encoder, its widget values, the
full upstream graph of its clip/vae inputs (loader names + LoRA settings + model file size/mtime) and
the pixels of its reference images. clip and vae are lazy inputs: on a hit neither is evaluated, so
the text encoder is never loaded. On a miss the stock node's own code runs, so outputs are exactly
what the stock node returns; a hit returns a copy of those same tensors.

Only text-encoder OUTPUTS are kept - no model state survives a prompt. RAM LRU + a size-capped disk
copy (survives ComfyUI restarts / profile switches). BUNNY_COND_CACHE=0 turns caching off (passthrough).
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict

import torch

import folder_paths
import nodes

log = logging.getLogger("bunny_cond_cache")

VERSION = 1
ENCODERS = ("CLIPTextEncode", "TextEncodeQwenImage21", "TextEncodeQwenImageEditPlus")
IMAGE_SLOTS = 16
RAM_ENTRIES = int(os.environ.get("BUNNY_COND_CACHE_RAM_ENTRIES", "64"))
DISK_BYTES = int(float(os.environ.get("BUNNY_COND_CACHE_DISK_GB", "2")) * 1024 ** 3)
ENABLED = os.environ.get("BUNNY_COND_CACHE", "1") != "0"
MODEL_EXTS = (".safetensors", ".gguf", ".sft", ".pt", ".pth", ".ckpt", ".bin")
MODEL_FOLDERS = ("text_encoders", "clip", "vae", "loras", "diffusion_models", "unet", "checkpoints")

try:
    from comfyui_version import __version__ as COMFY_VERSION
except Exception:  # noqa: BLE001
    COMFY_VERSION = "?"

_ram: OrderedDict[str, tuple] = OrderedDict()
_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0}


def _disk_dir() -> str:
    path = os.path.join(folder_paths.get_user_directory(), "bunny_cond_cache")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- key

def _file_stat(name: str):
    if not name.lower().endswith(MODEL_EXTS) or len(name) > 400:
        return None
    for folder in MODEL_FOLDERS:
        try:
            full = folder_paths.get_full_path(folder, name)
        except Exception:  # noqa: BLE001  (folder not registered)
            full = None
        if full:
            st = os.stat(full)
            return [folder, st.st_size, st.st_mtime_ns]
    return None


def _files_in(value, out: dict) -> None:
    if isinstance(value, str):
        stat = _file_stat(value)
        if stat:
            out[value] = stat
    elif isinstance(value, dict):
        for v in value.values():
            _files_in(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _files_in(v, out)


def _is_link(value) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int)


def _subgraph(prompt: dict, link, files: dict, memo: dict):
    """Canonical description of everything upstream of a link (class + inputs, recursively)."""
    node_id, output = link
    if node_id not in memo:
        node = prompt.get(node_id) or {}
        inputs = {}
        for name, value in sorted((node.get("inputs") or {}).items()):
            if _is_link(value):
                inputs[name] = _subgraph(prompt, value, files, memo)
            else:
                inputs[name] = value
                _files_in(value, files)
        memo[node_id] = {"class": node.get("class_type"), "inputs": inputs}
    return {"node": memo[node_id], "out": output}


def _image_hash(image) -> str:
    if image is None:
        return "-"
    t = image.detach().to("cpu").contiguous()
    h = hashlib.sha256(f"{t.dtype}{tuple(t.shape)}".encode())
    h.update(t.view(torch.uint8).numpy().tobytes() if t.dtype == torch.bfloat16 else t.numpy().tobytes())
    return h.hexdigest()


def _cache_key(encoder: str, params: str, prompt: dict, unique_id: str, images: dict) -> str:
    me = (prompt or {}).get(str(unique_id)) or {}
    files: dict = {}
    memo: dict = {}
    upstream = {}
    for name in ("clip", "vae"):
        link = (me.get("inputs") or {}).get(name)
        upstream[name] = _subgraph(prompt, link, files, memo) if _is_link(link) else None
    material = {
        "v": VERSION, "comfy": COMFY_VERSION, "encoder": encoder, "params": json.loads(params or "{}"),
        "upstream": upstream, "files": files,
        "images": {k: _image_hash(v) for k, v in sorted(images.items())},
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------- storage

def _to_cpu(value):
    if isinstance(value, torch.Tensor):
        return value.detach().to("cpu", copy=True)
    if isinstance(value, dict):
        return {k: _to_cpu(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_cpu(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_to_cpu(v) for v in value)
    return value


def _get(key: str):
    with _lock:
        if key in _ram:
            _ram.move_to_end(key)
            return copy.deepcopy(_ram[key])
    path = os.path.join(_disk_dir(), key + ".pt")
    if os.path.isfile(path):
        try:
            value = tuple(torch.load(path, map_location="cpu", weights_only=True))
            os.utime(path)
            _remember(key, value)
            return copy.deepcopy(value)
        except Exception as exc:  # noqa: BLE001
            log.warning("bunny_cond_cache: unreadable entry %s (%s), re-encoding", key[:12], exc)
            try:
                os.remove(path)
            except OSError:
                pass
    return None


def _remember(key: str, value: tuple) -> None:
    with _lock:
        _ram[key] = value
        _ram.move_to_end(key)
        while len(_ram) > RAM_ENTRIES:
            _ram.popitem(last=False)


def _put(key: str, value: tuple) -> None:
    value = _to_cpu(value)
    _remember(key, value)
    folder = _disk_dir()
    tmp = os.path.join(folder, f".{key}.{os.getpid()}.tmp")
    try:
        torch.save(list(value), tmp)
        torch.load(tmp, map_location="cpu", weights_only=True)  # only keep what loads back safely
        os.replace(tmp, os.path.join(folder, key + ".pt"))
    except Exception as exc:  # noqa: BLE001
        log.info("bunny_cond_cache: entry %s kept in RAM only (%s)", key[:12], exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return
    _trim_disk(folder)


def _trim_disk(folder: str) -> None:
    entries = []
    for name in os.listdir(folder):
        if name.endswith(".pt"):
            try:
                st = os.stat(os.path.join(folder, name))
                entries.append((st.st_mtime, st.st_size, name))
            except OSError:
                pass
    total = sum(e[1] for e in entries)
    for _, size, name in sorted(entries):
        if total <= DISK_BYTES:
            break
        try:
            os.remove(os.path.join(folder, name))
            total -= size
        except OSError:
            pass


# ---------------------------------------------------------------- encoders

class _PositiveOnlyClip:
    """For a TextEncodeQwenImage21 whose negative output nothing reads (BasicGuider, or cfg 1): the
    stock node's second tokenize/encode (negative prompt + every reference image through the vision
    tower again) is skipped and the positive conditioning stands in for the unused negative."""

    def __init__(self, clip):
        self._clip = clip
        self._positive = None

    def tokenize(self, text, **kwargs):
        return None if self._positive is not None else self._clip.tokenize(text, **kwargs)

    def encode_from_tokens_scheduled(self, tokens, **kwargs):
        if self._positive is None:
            self._positive = self._clip.encode_from_tokens_scheduled(tokens, **kwargs)
        return self._positive

    def __getattr__(self, name):
        return getattr(self._clip, name)


def _run_encoder(encoder: str, params: dict, clip, vae, images: dict) -> tuple:
    if encoder == "CLIPTextEncode":
        return (nodes.CLIPTextEncode().encode(clip, params["text"])[0], None, None)
    cls = nodes.NODE_CLASS_MAPPINGS[encoder]
    if encoder == "TextEncodeQwenImage21":
        if params.get("negative_unused"):
            clip = _PositiveOnlyClip(clip)
        out = cls.execute(clip=clip, prompt=params["prompt"], negative_prompt=params["negative_prompt"], vae=vae,
                          resolution=params.get("resolution", 1024), images=dict(images))
        return tuple(out.args)
    if encoder == "TextEncodeQwenImageEditPlus":
        slots = {f"image{i}": images.get(f"image_{i}") for i in (1, 2, 3)}
        out = cls.execute(clip=clip, prompt=params["prompt"], vae=vae, **slots)
        return (out.args[0], None, None)
    raise ValueError(f"BunnyCachedEncode: unsupported encoder {encoder}")


class BunnyCachedEncode:
    @classmethod
    def INPUT_TYPES(cls):
        optional = {"clip": ("CLIP", {"lazy": True}), "vae": ("VAE", {"lazy": True})}
        for i in range(1, IMAGE_SLOTS + 1):
            optional[f"image_{i}"] = ("IMAGE",)
        return {
            "required": {
                "encoder": (list(ENCODERS),),
                "params": ("STRING", {"default": "{}", "multiline": True}),
            },
            "optional": optional,
            "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("CONDITIONING", "CONDITIONING", "LATENT")
    RETURN_NAMES = ("conditioning", "negative", "latent")
    FUNCTION = "encode"
    CATEGORY = "model/conditioning"
    DESCRIPTION = "Runs a stock text encoder node and reuses its outputs for identical inputs (the text encoder isn't loaded on a hit)."

    def _key(self, encoder, params, prompt, unique_id, kwargs):
        images = {k: v for k, v in kwargs.items() if k.startswith("image_") and v is not None}
        return _cache_key(encoder, params, prompt, unique_id, images), images

    def check_lazy_status(self, encoder, params, prompt=None, unique_id=None, **kwargs):
        pending = [name for name in ("clip", "vae") if name in kwargs and kwargs[name] is None]
        if not pending:
            return []
        if ENABLED:
            key, _ = self._key(encoder, params, prompt, unique_id, kwargs)
            hit = _get(key)
            if hit is not None:
                self._hit = (key, hit)
                return []
        return pending

    def encode(self, encoder, params, prompt=None, unique_id=None, clip=None, vae=None, **kwargs):
        started = time.perf_counter()
        key, images = self._key(encoder, params, prompt, unique_id, kwargs)
        cached = getattr(self, "_hit", None)
        self._hit = None
        if cached and cached[0] == key:
            _stats["hits"] += 1
            log.info("bunny_cond_cache: hit %s (%s), text encoder skipped [%d hits / %d misses]",
                     key[:12], encoder, _stats["hits"], _stats["misses"])
            return cached[1]
        if clip is None:   # a hit was promised but is gone (shouldn't happen): fail clearly
            raise RuntimeError("BunnyCachedEncode: cache entry vanished and clip wasn't loaded; retry the job")
        result = _run_encoder(encoder, json.loads(params or "{}"), clip, vae, images)
        _stats["misses"] += 1
        if ENABLED:
            _put(key, result)
        log.info("bunny_cond_cache: miss %s (%s) encoded in %.2fs", key[:12], encoder, time.perf_counter() - started)
        return result


class BunnyAttentionBackend:
    """Per-job dense attention backend: any function registered in comfy.ldm.modules.attention
    (stock ModelAttentionBackend only offers pytorch / comfy kitchen). Same mechanism as the stock
    node: model.clone() + set_model_optimized_attention, so it only affects this job's sampler."""

    @classmethod
    def INPUT_TYPES(cls):
        import comfy.ldm.modules.attention as attention
        return {"required": {"model": ("MODEL",),
                             "attention": (sorted(attention.REGISTERED_ATTENTION_FUNCTIONS),)}}

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "patch"
    CATEGORY = "model/patch"

    def patch(self, model, attention):
        import comfy.ldm.modules.attention as attn
        function = attn.get_attention_function(attention, None)
        if function is None:
            raise RuntimeError(f"Attention backend '{attention}' isn't available in this ComfyUI")
        m = model.clone()
        m.set_model_optimized_attention(function)
        return (m,)


NODE_CLASS_MAPPINGS = {"BunnyCachedEncode": BunnyCachedEncode, "BunnyAttentionBackend": BunnyAttentionBackend}
NODE_DISPLAY_NAME_MAPPINGS = {"BunnyCachedEncode": "Cached Text Encode (Bunny)",
                              "BunnyAttentionBackend": "Attention Backend (Bunny, any registered)"}
