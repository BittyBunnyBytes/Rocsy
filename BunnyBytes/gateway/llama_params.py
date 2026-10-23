"""Per-model llama.cpp parameters for the Llama "Parameters" page.

config.yaml (mounted read-only) holds each model's defaults; this page writes
overrides next to H3 Machine's context file in the shared overrides folder:

    <model id>.ctx   context size (same file H3 Machine's "Agent context" uses)
    <model id>.args  one "--flag value" per line, appended by llama-launch

llama-launch reads both on every model load, so a save applies the next time
that model starts -- a model that is already loaded keeps running untouched.
Multi-GPU is a toggle here ("--multi-gpu on", read by llama-launch to pick the
device list); off by default. Attention type defaults to flash attention; the GPU
backend (ROCm or Vulkan) is offered only when the build in use has Vulkan.

Every value is validated here AND again by llama-launch's flag allowlist.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.getenv("LLAMA_CONFIG", "/llama-config.yaml"))
OVERRIDE_DIR = Path(os.getenv("LLAMA_OVERRIDE_DIR", "/llama-overrides"))

CACHE_TYPES = ["f16", "bf16", "q8_0", "q5_1", "q5_0", "q4_1", "q4_0", "iq4_nl"]
ATTENTION_TYPES = ["on", "off", "auto"]
# llama-server options only RDNA Boost builds understand (a stock build refuses to start).
BOOST_ONLY = ["--lazy-mode on-direct", "--moe-expert-cache", "draft-mtp-adaptive"]
BACKENDS = ["rocm", "vulkan"]
# What the build llama-swap uses supports; written by hostctl's Core Backend (corebackend.py).
BACKEND_INFO = OVERRIDE_DIR / "_backend.json"
SPEC_TYPES = ["none", "draft-mtp", "draft-mtp-adaptive", "ngram-mod", "ngram-simple",
              "ngram-map-k", "ngram-map-k4v", "ngram-cache"]

# flag, label, group, kind, limits/choices, help
PARAMS = [
    ("--multi-gpu", "Multi-GPU", "Load", "toggle", None, "Split the model across GPU 0 and GPU 1. Off keeps it on GPU 0 only."),
    ("--build", "llama.cpp build", "Load", "choice", [], "Which installed llama.cpp runs this model. Core Backend's choice follows Models → Manager → Core Backend → Use this build."),
    ("--backend", "GPU backend", "Load", "choice", BACKENDS, "rocm: ROCm/HIP (where RDNA Boost's kernels are). vulkan: llama.cpp's Vulkan backend (RADV)."),
    ("--flash-attn", "Attention type", "Load", "choice", ATTENTION_TYPES, "on: flash attention (default, fastest, less VRAM). off: standard attention. auto: llama.cpp decides per model."),
    ("--ctx-size", "Context size", "Load", "int", (4096, 262144), "Tokens of context. Shared with H3 Machine's Agent context."),
    ("--batch-size", "Batch size", "Load", "int", (32, 8192), "Logical batch for prompt processing (-b)."),
    ("--ubatch-size", "Micro-batch size", "Load", "int", (32, 4096), "Physical batch (-ub). Larger is faster but uses more VRAM."),
    ("--threads", "Threads", "Load", "int", (1, 64), "CPU threads for generation (-t)."),
    ("--threads-batch", "Batch threads", "Load", "int", (1, 64), "CPU threads for prompt processing."),
    ("--gpu-layers", "GPU layers", "Load", "layers", None, "all, auto, or a layer count (-ngl)."),
    ("--parallel", "Parallel slots", "Load", "int", (1, 16), "Concurrent requests; context is split between slots."),
    ("--cache-type-k", "KV cache K", "Load", "choice", CACHE_TYPES, "K cache precision."),
    ("--cache-type-v", "KV cache V", "Load", "choice", CACHE_TYPES, "V cache precision."),
    ("--n-cpu-moe", "MoE layers on CPU", "Load", "int", (0, 256), "Keep the experts of the first N layers in system RAM."),
    ("--fit-target", "Fit margin (MiB)", "Load", "int", (0, 16384), "VRAM left free when auto-fitting layers."),
    ("--spec-type", "Draft type", "Draft", "choice", SPEC_TYPES, "Speculative decoding. draft-mtp needs a model with an MTP head; ngram types need no draft model."),
    ("--spec-draft-n-max", "Max draft tokens", "Draft", "int", (1, 64), "Tokens drafted per step."),
    ("--spec-draft-n-min", "Min draft tokens", "Draft", "int", (0, 64), "Minimum drafted tokens before verifying."),
    ("--temp", "Temperature", "Sampling", "float", (0.0, 2.0), "Default when a request doesn't set one."),
    ("--top-p", "Top P", "Sampling", "float", (0.0, 1.0), "Default nucleus sampling cutoff."),
    ("--top-k", "Top K", "Sampling", "int", (0, 1000), "Default top-k (0 = off)."),
    ("--min-p", "Min P", "Sampling", "float", (0.0, 1.0), "Default min-p cutoff."),
    ("--repeat-penalty", "Repeat penalty", "Sampling", "float", (0.5, 2.0), "Default repetition penalty (1 = off)."),
    ("--presence-penalty", "Presence penalty", "Sampling", "float", (-2.0, 2.0), "Default presence penalty."),
    ("--reasoning-budget", "Reasoning budget", "Sampling", "int", (-1, 131072), "Thinking tokens (-1 = unlimited, 0 = off)."),
    ("--n-predict", "Max output tokens", "Sampling", "int", (-1, 262144), "Default output cap (-1 = unlimited)."),
]
_BY_FLAG = {param[0]: param for param in PARAMS}

# Models served by Strata (cmd = strata-swap-bridge) have their own engine, so the
# llama.cpp flags above don't apply. Their choices go to the same <id>.args file;
# hostctl's coreengines.run_strata_serve applies them when Strata starts.
STRATA_BRIDGE = "strata-swap-bridge"
STRATA_PARAMS = [
    ("--vision", "Vision", "Strata", "choice", ["on", "off"], "Read pictures (image_url) with the GPU image encoder. Off frees its ~1.5 GB of VRAM for the expert cache; text only."),
    ("--ngram", "N-gram table", "Strata", "choice", ["default", "bf16"], "BF16 uses the checkpoint's own n-gram table at full precision instead of the model's 4-bit copy (about 8% off per row). It is read from the SSD a few rows a token, so it costs 102 GB of disk, not RAM or VRAM. Download it once in Models -> Core Backend -> Strata; until then the model's own table is used."),
]
_STRATA_BY_FLAG = {param[0]: param for param in STRATA_PARAMS}
STRATA_DEFAULTS = {"--vision": "on", "--ngram": "default"}
_MODEL_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class ParamError(ValueError):
    pass


CHOICE_LABELS = {
    "--flash-attn": {"on": "Flash attention", "off": "Standard attention", "auto": "Auto (llama.cpp decides)"},
    "--backend": {"rocm": "ROCm (HIP)", "vulkan": "Vulkan"},
    "--vision": {"on": "On (GPU image encoder)", "off": "Off (text only)"},
    "--ngram": {"default": "The model's own (4-bit, in its shard 2)", "bf16": "BF16, full precision (on disk)"},
}


def backend_info() -> dict:
    try:
        import json
        return json.loads(BACKEND_INFO.read_text())
    except (OSError, ValueError):
        return {}


def spec() -> list[dict]:
    info = backend_info()
    out = []
    for flag, label, group, kind, limits, help_text in PARAMS:
        item = {"flag": flag, "label": label, "group": group, "kind": kind,
                "limits": list(limits) if kind in ("int", "float") else None,
                "choices": limits if kind == "choice" else None, "help": help_text,
                "choice_labels": CHOICE_LABELS.get(flag)}
        if flag == "--build":
            builds = info.get("builds") or []
            item["choices"] = ["active"] + [b["id"] for b in builds]
            item["choice_labels"] = {"active": f"Core Backend's choice ({info.get('build') or 'built-in'})",
                                     **{b["id"]: b["name"] + (" · + Vulkan" if b.get("vulkan") else "") for b in builds}}
            item["builds"] = builds
        if flag == "--backend":
            # Only offered when an installed build has a Vulkan backend AND the llama
            # container has the Vulkan runtime.
            if not (any(b.get("vulkan") for b in info.get("builds") or []) and info.get("runtime")):
                item["unavailable"] = info.get("vulkan_reason") or "Vulkan isn't set up on this machine yet."
        out.append(item)
    return out


def strata_spec() -> list[dict]:
    return [{"flag": flag, "label": label, "group": group, "kind": kind, "limits": None, "choices": limits,
             "help": help_text, "choice_labels": CHOICE_LABELS.get(flag)}
            for flag, label, group, kind, limits, help_text in STRATA_PARAMS]


def _is_strata(entry: dict) -> bool:
    return STRATA_BRIDGE in str(entry.get("cmd", ""))


def _read_strata_overrides(model_id: str) -> dict[str, str]:
    values: dict[str, str] = {}
    args_file = OVERRIDE_DIR / f"{model_id}.args"
    if args_file.is_file():
        for line in args_file.read_text().splitlines():
            flag, _, value = line.strip().partition(" ")
            if flag in _STRATA_BY_FLAG and value.strip() in _STRATA_BY_FLAG[flag][4]:
                values[flag] = value.strip()
    return values


def _defaults(cmd: str) -> dict[str, str]:
    """Values config.yaml's cmd passes for the editable flags. The first two
    words after ${llama_launch} are llama-launch's GPU-layer and fit-margin
    defaults."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    values: dict[str, str] = {"--multi-gpu": "off", "--build": "active", "--backend": "rocm", "--flash-attn": "on"}
    if words and words[0] == "${llama_launch}":
        if len(words) > 1:
            values["--gpu-layers"] = words[1]
        if len(words) > 2 and words[2] != "none":
            values["--fit-target"] = words[2]
    for index, word in enumerate(words[:-1]):
        if word in _BY_FLAG:
            values[word] = words[index + 1]
    return values


def _fixed(cmd: str) -> list[str]:
    """The rest of config.yaml's cmd: flags this page can't change (model path, port, ...)."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    if words and words[0] == "${llama_launch}":
        words = words[3:]
    fixed, skip = [], False
    for index, word in enumerate(words):
        if skip:
            skip = False
            continue
        if word in _BY_FLAG:
            skip = index + 1 < len(words) and not words[index + 1].startswith("--")
            continue
        fixed.append(word)
    return fixed


def _read_overrides(model_id: str) -> dict[str, str]:
    values: dict[str, str] = {}
    ctx_file = OVERRIDE_DIR / f"{model_id}.ctx"
    if ctx_file.is_file():
        ctx = ctx_file.read_text().strip()
        if ctx.isdigit():
            values["--ctx-size"] = ctx
    args_file = OVERRIDE_DIR / f"{model_id}.args"
    if args_file.is_file():
        for line in args_file.read_text().splitlines():
            flag, _, value = line.strip().partition(" ")
            if flag in _BY_FLAG and value:
                values[flag] = value.strip()
    return values


def models(running: set[str]) -> list[dict]:
    config = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    result = []
    for model_id, entry in (config.get("models") or {}).items():
        if not isinstance(entry, dict):
            continue
        if _is_strata(entry):
            result.append({
                "id": model_id,
                "name": entry.get("name") or model_id,
                "running": model_id in running,
                "defaults": dict(STRATA_DEFAULTS),
                "fixed": [],
                "overrides": _read_strata_overrides(model_id),
                "engine": "strata",
                "params": strata_spec(),
            })
            continue
        result.append({
            "id": model_id,
            "name": entry.get("name") or model_id,
            "running": model_id in running,
            "defaults": _defaults(str(entry.get("cmd", ""))),
            "fixed": _fixed(str(entry.get("cmd", ""))),
            "overrides": _read_overrides(model_id),
            "engine": "llama.cpp" if str(entry.get("cmd", "")).lstrip().startswith("${llama_launch}") else "external",
        })
    return result


def _clean(flag: str, raw) -> str:
    _, label, _, kind, limits, _ = _BY_FLAG[flag]
    value = str(raw).strip().lower()
    if kind == "toggle":
        if value not in ("on", "off"):
            raise ParamError(f"{label}: must be on or off.")
        return value
    if flag == "--build":
        ids = ["active"] + [b["id"] for b in backend_info().get("builds") or []]
        if value not in ids:
            raise ParamError(f"{label}: {value} isn't installed. Choose one of {', '.join(ids)}.")
        return value
    if kind == "choice":
        if value not in limits:
            raise ParamError(f"{label}: choose one of {', '.join(limits)}.")
        return value
    if kind == "layers":
        if value in ("all", "auto") or (value.isdigit() and int(value) <= 999):
            return value
        raise ParamError(f"{label}: use all, auto, or a number up to 999.")
    try:
        number = int(value) if kind == "int" else float(value)
    except ValueError:
        raise ParamError(f"{label}: enter a {'whole ' if kind == 'int' else ''}number.") from None
    low, high = limits
    if not low <= number <= high:
        raise ParamError(f"{label}: must be between {low} and {high}.")
    return str(number) if kind == "int" else f"{number:g}"


def save(model_id: str, params: dict) -> dict[str, str]:
    """Replace a model's overrides. Flags missing or blank in `params` go back
    to the config.yaml default."""
    if not _MODEL_ID.match(model_id):
        raise ParamError("Unknown model.")
    config = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    if model_id not in (config.get("models") or {}):
        raise ParamError("Unknown model.")
    if _is_strata(config["models"][model_id]):
        return _save_strata(model_id, params)
    if not str(config["models"][model_id].get("cmd", "")).lstrip().startswith("${llama_launch}"):
        raise ParamError("This model uses its own inference engine; llama.cpp parameters do not apply.")
    cleaned: dict[str, str] = {}
    for flag, raw in (params or {}).items():
        if flag not in _BY_FLAG:
            raise ParamError(f"Unsupported parameter {flag}.")
        if raw is None or str(raw).strip() == "":
            continue
        cleaned[flag] = _clean(flag, raw)

    if cleaned.get("--multi-gpu") == "off":  # off is the default: nothing to store
        del cleaned["--multi-gpu"]
    if cleaned.get("--backend") == "rocm":  # ROCm is the default backend
        del cleaned["--backend"]
    info = backend_info()
    builds = {b["id"]: b for b in info.get("builds") or []}
    chosen = cleaned.get("--build", "active")
    if chosen == "active":
        cleaned.pop("--build", None)  # follow Core Backend: nothing to store
    build = builds.get(info.get("active_id") if chosen == "active" else chosen, {})
    if cleaned.get("--backend") == "vulkan" and not (build.get("vulkan") and info.get("runtime")):
        raise ParamError(f"GPU backend: {build.get('name') or 'that build'} has no Vulkan backend. Pick a ROCm + Vulkan build or ROCm.")
    if chosen != "active" and build and not build.get("boost"):
        cmd = str((config.get("models") or {}).get(model_id, {}).get("cmd", ""))
        used = [flag for flag in BOOST_ONLY if flag in cmd]
        if used:
            raise ParamError(f"llama.cpp build: {build['name']} can't run this model - its config uses RDNA Boost-only "
                             f"options ({', '.join(used)}).")
    ctx = cleaned.pop("--ctx-size", None)
    _write(OVERRIDE_DIR / f"{model_id}.ctx", f"{ctx}\n" if ctx else None)
    lines = [f"{flag} {cleaned[flag]}" for flag, *_ in PARAMS if flag in cleaned]
    _write(OVERRIDE_DIR / f"{model_id}.args", "".join(f"{line}\n" for line in lines) or None)
    return _read_overrides(model_id)


def _save_strata(model_id: str, params: dict) -> dict[str, str]:
    """A Strata model's choices; a value equal to its default is not stored. The
    .ctx file is left alone (H3 Machine's Agent context reads it for this model)."""
    cleaned: dict[str, str] = {}
    for flag, raw in (params or {}).items():
        if flag not in _STRATA_BY_FLAG:
            raise ParamError(f"Unsupported parameter {flag} for a Strata model.")
        value = "" if raw is None else str(raw).strip().lower()
        if value == "":
            continue
        _, label, _, _, choices, _ = _STRATA_BY_FLAG[flag]
        if value not in choices:
            raise ParamError(f"{label}: choose one of {', '.join(choices)}.")
        if value != STRATA_DEFAULTS.get(flag):
            cleaned[flag] = value
    lines = [f"{flag} {cleaned[flag]}" for flag, *_ in STRATA_PARAMS if flag in cleaned]
    _write(OVERRIDE_DIR / f"{model_id}.args", "".join(f"{line}\n" for line in lines) or None)
    return _read_strata_overrides(model_id)


def _write(path: Path, content: str | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content)
    os.replace(temporary, path)
