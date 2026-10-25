"""Per-model llama.cpp parameters for the Llama "Parameters" page.

config.yaml (mounted read-only) holds each model's defaults; this page writes
overrides next to Video's context file in the shared overrides folder:

    <model id>.ctx   context size (same file Video's "Agent context" uses)
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
import struct
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.getenv("LLAMA_CONFIG", "/llama-config.yaml"))
OVERRIDE_DIR = Path(os.getenv("LLAMA_OVERRIDE_DIR", "/llama-overrides"))
# GGUF LoRAs (models/llm/loras, read-only here); the Trainer's "Use in Llama" puts its LLM LoRAs there.
LORA_DIR = Path(os.getenv("LLAMA_LORA_DIR", "/llama-loras"))
_LORA_NAME = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*\.gguf$")
# Draft models for speculative decoding: any GGUF under models/llm (read-only here, same tree llama-launch reads).
LLM_DIR = Path(os.getenv("LLAMA_LLM_DIR", "/llama-llm"))
_DRAFT_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+-]*(/[A-Za-z0-9_][A-Za-z0-9._+-]*){0,3}\.gguf$")

CACHE_TYPES = ["f16", "bf16", "q8_0", "q5_1", "q5_0", "q4_1", "q4_0", "iq4_nl"]
ATTENTION_TYPES = ["on", "off", "auto"]
# llama-server options only RDNA Boost builds understand (a stock build refuses to start).
BOOST_ONLY = ["--lazy-mode on-direct", "--moe-expert-cache", "draft-mtp-adaptive"]
BACKENDS = ["rocm", "vulkan"]
# What the build llama-swap uses supports; written by hostctl's Core Backend (corebackend.py).
BACKEND_INFO = OVERRIDE_DIR / "_backend.json"
SPEC_TYPES = ["none", "draft-simple", "draft-mtp", "draft-mtp-adaptive", "draft-eagle3", "draft-dflash",
              "ngram-mod", "ngram-simple", "ngram-map-k", "ngram-map-k4v", "ngram-cache"]
# Draft types that read a draft model file (MTP can also use the model's own MTP head).
DRAFT_FILE_TYPES = {"draft-simple", "draft-eagle3", "draft-dflash"}
MTP_TYPES = {"draft-mtp", "draft-mtp-adaptive"}

# flag, label, group, kind, limits/choices, help
PARAMS = [
    ("--multi-gpu", "Multi-GPU", "Load", "toggle", None, "Split the model across GPU 0 and GPU 1. Off keeps it on GPU 0 only."),
    ("--build", "llama.cpp build", "Load", "choice", [], "Which installed llama.cpp runs this model. Core Backend's choice follows Models → Manager → Core Backend → Use this build."),
    ("--backend", "GPU backend", "Load", "choice", BACKENDS, "rocm: ROCm/HIP (where RDNA Boost's kernels are). vulkan: llama.cpp's Vulkan backend (RADV)."),
    ("--flash-attn", "Attention type", "Load", "choice", ATTENTION_TYPES, "on: flash attention (default, fastest, less VRAM). off: standard attention. auto: llama.cpp decides per model."),
    ("--ctx-size", "Context size", "Load", "int", (4096, 262144), "Tokens of context. Shared with Video's Agent context."),
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
    ("--spec-type", "Draft type", "Draft", "choice", SPEC_TYPES, "Speculative decoding: a fast drafter guesses the next tokens and this model checks them in one pass. ★ = this model has its own MTP head. n-gram types need no draft model."),
    ("--spec-draft-model", "Draft model", "Draft", "choice", [], "A small model with the same tokenizer (draft-simple), or an MTP/EAGLE3/DFlash drafter made for this model. ★ = recommended for this model."),
    ("--spec-draft-n-max", "Max draft tokens", "Draft", "int", (1, 64), "Tokens drafted per step."),
    ("--spec-draft-n-min", "Min draft tokens", "Draft", "int", (0, 64), "Minimum drafted tokens before verifying."),
    ("--spec-draft-n-min-adaptive", "Adaptive MTP start depth", "Draft", "int", (1, 16), "draft-mtp-adaptive: the depth it starts at and never drops below."),
    ("--spec-draft-p-min", "Draft confidence", "Draft", "float", (0.0, 1.0), "Stop drafting when the drafter's top token is less likely than this."),
    ("--spec-draft-ngl", "Draft model GPU layers", "Draft", "layers", None, "all, auto, or a layer count for the draft model."),
    ("--spec-draft-type-k", "Draft KV cache K", "Draft", "choice", CACHE_TYPES, "Draft model's K cache precision."),
    ("--spec-draft-type-v", "Draft KV cache V", "Draft", "choice", CACHE_TYPES, "Draft model's V cache precision."),
    ("--temp", "Temperature", "Sampling", "float", (0.0, 2.0), "Default when a request doesn't set one."),
    ("--top-p", "Top P", "Sampling", "float", (0.0, 1.0), "Default nucleus sampling cutoff."),
    ("--top-k", "Top K", "Sampling", "int", (0, 1000), "Default top-k (0 = off)."),
    ("--min-p", "Min P", "Sampling", "float", (0.0, 1.0), "Default min-p cutoff."),
    ("--repeat-penalty", "Repeat penalty", "Sampling", "float", (0.5, 2.0), "Default repetition penalty (1 = off)."),
    ("--presence-penalty", "Presence penalty", "Sampling", "float", (-2.0, 2.0), "Default presence penalty."),
    ("--reasoning-budget", "Reasoning budget", "Sampling", "int", (-1, 131072), "Thinking tokens (-1 = unlimited, 0 = off)."),
    ("--n-predict", "Max output tokens", "Sampling", "int", (-1, 262144), "Default output cap (-1 = unlimited)."),
    ("--mmproj", "Vision head", "Vision", "choice", [], "The multimodal projector (mmproj) that lets this model read pictures (and audio, when the head has an audio encoder). Its projection width must match this model's width or llama.cpp refuses to load it. ★ = made for this model. None = text only (frees its VRAM)."),
    ("--lora", "LoRA adapter", "LoRA", "choice", [], "A GGUF LoRA from models/llm/loras (the Trainer's LLM LoRAs land there). Use one trained on this model or its base."),
    ("--lora-scale", "LoRA strength", "LoRA", "float", (0.0, 2.0), "How strongly the LoRA applies (1 = as trained)."),
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
    "--spec-type": {"none": "Off", "draft-simple": "Draft model (small model, same tokenizer)",
                    "draft-mtp": "MTP (multi-token prediction head)", "draft-mtp-adaptive": "MTP, adaptive depth (RDNA Boost)",
                    "draft-eagle3": "EAGLE-3 drafter", "draft-dflash": "DFlash drafter", "ngram-mod": "n-gram (mod)",
                    "ngram-simple": "n-gram (simple)", "ngram-map-k": "n-gram map (k)", "ngram-map-k4v": "n-gram map (k4v)",
                    "ngram-cache": "n-gram cache"},
}


# ---- GGUF metadata (draft model recommendations) ------------------------------------------------

_GGUF_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_GGUF_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
_gguf_cache: dict[str, tuple[float, int, dict]] = {}


def gguf_meta(path: Path) -> dict:
    """A GGUF's key/values (strings and numbers; arrays become their length), cached by mtime/size."""
    try:
        st = path.stat()
    except OSError:
        return {}
    hit = _gguf_cache.get(str(path))
    if hit and hit[:2] == (st.st_mtime, st.st_size):
        return hit[2]
    out: dict = {}
    try:
        with path.open("rb", buffering=1 << 20) as f:
            if f.read(4) != b"GGUF":
                raise ValueError
            f.read(4 + 8)
            count = struct.unpack("<Q", f.read(8))[0]

            def text() -> str:
                n = struct.unpack("<Q", f.read(8))[0]
                if n > 1 << 24:
                    raise ValueError
                return f.read(n).decode("utf-8", "replace")
            for _ in range(min(count, 4096)):
                key = text()
                kind = struct.unpack("<I", f.read(4))[0]
                if kind == 8:
                    out[key] = text()[:300]
                elif kind == 9:
                    sub_kind, n = struct.unpack("<IQ", f.read(12))
                    out[key] = ("array", n)
                    if sub_kind == 8:
                        for _ in range(n):
                            text()
                    else:
                        f.seek(_GGUF_SIZES[sub_kind] * n, 1)
                else:
                    out[key] = struct.unpack(_GGUF_SCALAR[kind], f.read(_GGUF_SIZES[kind]))[0]
    except (OSError, ValueError, KeyError, struct.error):
        pass
    _gguf_cache[str(path)] = (st.st_mtime, st.st_size, out)
    return out


def _vocab(meta: dict) -> int:
    tokens = meta.get("tokenizer.ggml.tokens")
    return tokens[1] if isinstance(tokens, tuple) else 0


def _info(path: Path) -> dict:
    """What decides whether a GGUF can draft for another: architecture, tokenizer, size, MTP head."""
    meta = gguf_meta(path)
    arch = str(meta.get("general.architecture") or "")
    parts = re.match(r"(.*)-00001-of-(\d{5})\.gguf$", path.name)
    size = sum(p.stat().st_size for p in path.parent.glob(f"{parts.group(1)}-*-of-{parts.group(2)}.gguf")) if parts else path.stat().st_size
    nextn = int(meta.get(f"{arch}.nextn_predict_layers") or 0)
    if arch.endswith("-assistant") or (nextn and int(meta.get(f"{arch}.block_count") or 99) <= 8):
        kind = "mtp"
    elif arch in ("eagle3", "dflash") or any(k.startswith("dflash.") for k in meta):
        kind = "dflash" if arch == "dflash" or any(k.startswith("dflash.") for k in meta) else "eagle3"
    else:
        kind = "model"
    return {"arch": arch, "kind": kind, "size": size, "nextn": nextn, "vocab": _vocab(meta),
            "tok": (meta.get("tokenizer.ggml.model"), meta.get("tokenizer.ggml.pre")),
            "hidden": int(meta.get(f"{arch}.embedding_length") or 0),
            # MTP/EAGLE3/DFlash drafters record the hidden size of the model they were made for.
            "for_hidden": int(meta.get(f"{arch}.embedding_length_out") or meta.get(f"{arch}.target_hidden_size") or 0),
            "name": str(meta.get("general.name") or path.stem),
            "family": str(meta.get("general.basename") or ""), "size_label": str(meta.get("general.size_label") or ""),
            "base_name": str(meta.get("general.base_model.0.name") or "")}


def _is_vision_head(path: Path) -> bool:
    return "mmproj" in path.name.lower() or (path.stat().st_size < 4 * 1024**3 and gguf_meta(path).get("general.architecture") == "clip")


def vision_files() -> list[str]:
    """Vision heads (mmproj GGUFs) under models/llm."""
    found = []
    if LLM_DIR.is_dir():
        for path in LLM_DIR.rglob("*.gguf"):
            rel = path.relative_to(LLM_DIR).as_posix()
            if (_DRAFT_PATH.match(rel) and not rel.startswith(("loras/", "llama.cpp"))
                    and not re.search(r"-000(0[2-9]|[1-9]\d)-of-\d{5}\.gguf$", path.name) and _is_vision_head(path)):
                found.append(rel)
    return sorted(found, key=str.casefold)


def _vision_info(path: Path) -> dict:
    meta = gguf_meta(path)
    vision, audio = bool(meta.get("clip.has_vision_encoder")), bool(meta.get("clip.has_audio_encoder"))
    proj = int(meta.get("clip.vision.projection_dim") or meta.get("clip.audio.projection_dim") or 0)
    ptype = str(meta.get("clip.vision.projector_type") or meta.get("clip.projector_type") or meta.get("clip.audio.projector_type") or "")
    return {"proj": proj, "ptype": ptype, "vision": vision or not audio, "audio": audio, "size": path.stat().st_size,
            "name": str(meta.get("general.name") or path.stem)}


def _letters(text: str) -> str:
    return (re.match(r"[a-z]+", text.lower()) or [""])[0]


def vision_advice(cmd: str, files: list[str]) -> dict:
    """{vision head file: {star, fits, why, gb, audio}} for one model. Fits = the head's projection width is this
    model's width (llama.cpp's mtmd refuses anything else); ★ = it fits and is of the same family (or ships beside it)."""
    target = _model_file(cmd)
    t = _info(target) if target and target.is_file() else None
    advice = {}
    for rel in files:
        path = LLM_DIR / rel
        v = _vision_info(path)
        fits = star = None
        why = ""
        if t and t["hidden"]:
            fits = v["proj"] == t["hidden"]
            same_family = bool(_letters(v["ptype"])) and _letters(v["ptype"]) == _letters(t["arch"])
            beside = target.parent.resolve() == path.parent.resolve()
            star = fits and (same_family or beside)
            why = (("made for this model (ships beside it)" if beside else f"made for {t['arch']} models, width {v['proj']}")
                   if star else f"width {v['proj']} matches but it's a {v['ptype'] or 'different'} head: may give nonsense"
                   if fits else f"made for a model {v['proj'] or '?'} wide; this one is {t['hidden']}: llama.cpp refuses it")
        advice[rel] = {"star": star, "fits": fits, "why": why, "gb": round(v["size"] / 1024**3, 2), "audio": v["audio"],
                       "vision": v["vision"]}
    return advice


def draft_files() -> list[str]:
    """GGUFs under models/llm that can be a draft model (no projectors, vocab test files, LoRAs or later split parts)."""
    found = []
    if LLM_DIR.is_dir():
        for path in LLM_DIR.rglob("*.gguf"):
            rel = path.relative_to(LLM_DIR).as_posix()
            if (not _DRAFT_PATH.match(rel) or rel.startswith(("loras/", "llama.cpp"))
                    or re.search(r"mmproj|^ggml-vocab", path.name, re.I) or _is_vision_head(path)
                    or re.search(r"-000(0[2-9]|[1-9]\d)-of-\d{5}\.gguf$", path.name)):
                continue
            found.append(rel)
    return sorted(found, key=str.casefold)


_FAMILY = re.compile(r"(qwen|gemma|llama|mistral|ministral|glm|phi|deepseek|minicpm|gpt-oss|olmo|granite|falcon|smollm|"
                     r"exaone|internlm|yi|hermes|nemotron|kimi|seed)[\s_-]?[\d.]*", re.I)
_SIZE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?B(?:-A\d+(?:\.\d+)?B)?)(?![a-z])", re.I)


def search_stem(*names: str, size: str = "") -> tuple[str, str]:
    """(family like "Qwen3.8", "Qwen3.8-27B") from the first name that names a real model family: GGUF basenames
    can be anything ("abl"), so the base model's name and the llama-swap display name are tried too."""
    for name in names:
        if name and (family := _FAMILY.search(name)):
            fam = re.sub(r"[\s_]+", "-", family.group(0).strip(" -_"))
            found = size if size and _SIZE.fullmatch(size) else (m.group(1) if (m := _SIZE.search(name)) else "")
            return fam, f"{fam}-{found.upper()}" if found else fam
    return "", ""


_DRAFT_TYPE = {"model": "draft-simple", "mtp": "draft-mtp", "eagle3": "draft-eagle3", "dflash": "draft-dflash"}


def _model_file(cmd: str) -> Path | None:
    match = re.search(r"(?:--model|-m)\s+(\S+\.gguf)", cmd)
    if not match or "/models/llm/" not in match.group(1):
        return None
    return LLM_DIR / match.group(1).split("/models/llm/", 1)[1]


def draft_advice(cmd: str, files: list[str], display: str = "") -> tuple[int, dict, dict]:
    """(MTP head layers in this model, {draft file: {star, why, type}}, what to search drafters by) for one model."""
    target = _model_file(cmd)
    t = _info(target) if target and target.is_file() else None
    advice = {}
    for rel in files:
        path = LLM_DIR / rel
        if target and path.resolve() == target.resolve():
            continue
        d = _info(path)
        kind, gb = d["kind"], d["size"] / 1024**3
        star, why, same_tok = False, "", None
        if t:
            same_tok = d["tok"] == t["tok"] and abs(d["vocab"] - t["vocab"]) <= 128 and d["vocab"] > 0
            if kind == "model":
                share = d["size"] / t["size"] if t["size"] else 1
                star = same_tok and share <= 0.25
                why = (f"same tokenizer, {share:.0%} of this model's size" if same_tok else
                       "different tokenizer: llama.cpp refuses it as a draft for this model")
                if same_tok and share > 0.25:
                    why += " (too big to speed it up much)"
            else:
                made_for = d["for_hidden"] == t["hidden"] and t["hidden"] > 0
                star = same_tok and made_for
                label = {"mtp": "MTP drafter", "eagle3": "EAGLE-3 drafter", "dflash": "DFlash drafter"}[kind]
                why = (f"{label} made for this model" if star else
                       f"{label} made for another model (hidden size {d['for_hidden'] or '?'} vs {t['hidden'] or '?'})" if same_tok
                       else f"{label} with a different tokenizer")
        small = bool(t and t["size"] and d["size"] <= 0.25 * t["size"])
        advice[rel] = {"star": star, "why": why, "type": _DRAFT_TYPE[kind], "gb": round(gb, 1), "same_tokenizer": same_tok,
                       "small": small}
    family, stem = search_stem(*((t["family"], t["name"], t["base_name"]) if t else ()), display, size=t["size_label"] if t else "")
    search = {"family": family, "stem": stem, "arch": t["arch"] if t else ""}
    return (t["nextn"] if t and t["kind"] == "model" else 0), advice, search


def backend_info() -> dict:
    try:
        import json
        return json.loads(BACKEND_INFO.read_text())
    except (OSError, ValueError):
        return {}


def loras() -> list[dict]:
    """GGUF LoRAs in models/llm/loras, with the base model from the Trainer's <name>.json when there is one."""
    out = []
    if LORA_DIR.is_dir():
        for path in sorted(LORA_DIR.glob("*.gguf"), key=lambda p: p.name.casefold()):
            if not _LORA_NAME.match(path.name):
                continue
            base = ""
            try:
                import json
                base = str(json.loads(path.with_suffix(".json").read_text()).get("base_model") or "")
            except (OSError, ValueError, AttributeError):
                pass
            out.append({"file": path.name, "base": base})
    return out


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
        if flag == "--spec-draft-model":
            files = draft_files()
            item["choices"] = ["none"] + files
            labels = {}
            for rel in files:
                d = _info(LLM_DIR / rel)
                labels[rel] = f"{rel.rsplit('/', 1)[-1]} · {d['size'] / 1024**3:.1f} GB" + (
                    {"mtp": " · MTP drafter", "eagle3": " · EAGLE-3", "dflash": " · DFlash"}.get(d["kind"], ""))
            item["choice_labels"] = {"none": "None", **labels}
            item["keep_case"] = True
        if flag == "--mmproj":
            files = vision_files()
            item["choices"] = ["none"] + files
            item["choice_labels"] = {"none": "None (text only)", **{
                rel: f"{rel.rsplit('/', 1)[-1]}" + (f" ({rel.rsplit('/', 2)[-2][:40]})" if "/" in rel else "")
                     + f" · {(v := _vision_info(LLM_DIR / rel))['size'] / 1024**3:.2f} GB"
                     + (" · vision + audio" if v["audio"] and v["vision"] else " · audio" if v["audio"] else "")
                for rel in files}}
            item["keep_case"] = True
        if flag == "--lora":
            found = loras()
            item["choices"] = ["none"] + [lora["file"] for lora in found]
            item["choice_labels"] = {"none": "None", **{lora["file"]: lora["file"] + (f" · trained on {lora['base']}" if lora["base"] else "")
                                                        for lora in found}}
            item["keep_case"] = True
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
    values: dict[str, str] = {"--multi-gpu": "off", "--build": "active", "--backend": "rocm", "--flash-attn": "on",
                              "--lora": "none", "--lora-scale": "1", "--spec-draft-model": "none", "--spec-type": "none",
                              "--mmproj": "none"}
    if words and words[0] == "${llama_launch}":
        if len(words) > 1:
            values["--gpu-layers"] = words[1]
        if len(words) > 2 and words[2] != "none":
            values["--fit-target"] = words[2]
    for index, word in enumerate(words[:-1]):
        if word in _BY_FLAG:
            values[word] = words[index + 1]
    if "/models/llm/" in values["--mmproj"]:  # config.yaml names the host path; the picker uses models/llm paths
        values["--mmproj"] = values["--mmproj"].split("/models/llm/", 1)[1]
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
    files = draft_files() if LLM_DIR.is_dir() else None
    heads = vision_files() if files is not None else []
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
        mtp_head, drafts, draft_search = (draft_advice(str(entry.get("cmd", "")), files, str(entry.get("name") or model_id))
                                          if files is not None else (0, {}, {}))
        result.append({
            "id": model_id,
            "name": entry.get("name") or model_id,
            "running": model_id in running,
            "mtp_head": mtp_head,
            "drafts": drafts,
            "draft_search": draft_search,
            "visions": vision_advice(str(entry.get("cmd", "")), heads) if heads else {},
            "defaults": _defaults(str(entry.get("cmd", ""))),
            "fixed": _fixed(str(entry.get("cmd", ""))),
            "overrides": _read_overrides(model_id),
            "engine": "llama.cpp" if str(entry.get("cmd", "")).lstrip().startswith("${llama_launch}") else "external",
        })
    return result


def _clean(flag: str, raw) -> str:
    _, label, _, kind, limits, _ = _BY_FLAG[flag]
    if flag == "--spec-draft-model":  # a path under models/llm, case kept
        value = str(raw).strip()
        if value.lower() == "none":
            return "none"
        if not _DRAFT_PATH.match(value) or ".." in value or not (LLM_DIR / value).is_file():
            raise ParamError(f"{label}: {value} isn't a GGUF in models/llm.")
        return value
    if flag == "--mmproj":  # a vision head under models/llm, case kept
        value = str(raw).strip()
        if value.lower() == "none":
            return "none"
        if not _DRAFT_PATH.match(value) or ".." in value or not (LLM_DIR / value).is_file() or not _is_vision_head(LLM_DIR / value):
            raise ParamError(f"{label}: {value} isn't a vision head (mmproj GGUF) in models/llm.")
        return value
    if flag == "--lora":  # file names keep their case
        value = str(raw).strip()
        if value.lower() == "none":
            return "none"
        if not _LORA_NAME.match(value) or not (LORA_DIR / value).is_file():
            raise ParamError(f"{label}: {value} isn't in models/llm/loras.")
        return value
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
    _check_draft(cleaned, str(config["models"][model_id].get("cmd", "")))
    _check_vision(cleaned, str(config["models"][model_id].get("cmd", "")))
    if cleaned.get("--lora", "none") == "none":  # no LoRA: its strength means nothing either
        cleaned.pop("--lora", None)
        cleaned.pop("--lora-scale", None)
    elif cleaned.get("--lora-scale") == "1":
        del cleaned["--lora-scale"]
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


def _check_draft(cleaned: dict[str, str], cmd: str) -> None:
    """Draft type and draft model must agree; llama-server would otherwise refuse to start."""
    if cleaned.get("--spec-draft-model") == "none":
        del cleaned["--spec-draft-model"]
    kind = cleaned.get("--spec-type") or _defaults(cmd).get("--spec-type", "none")
    draft = cleaned.get("--spec-draft-model")
    if draft and kind not in DRAFT_FILE_TYPES | MTP_TYPES:
        raise ParamError(f"Draft model: Draft type is {kind}, which doesn't use a draft model. "
                         f"Choose {_DRAFT_TYPE[_info(LLM_DIR / draft)['kind']]} or set Draft model to None.")
    if kind in DRAFT_FILE_TYPES and not draft:
        raise ParamError(f"Draft type {kind} needs a Draft model.")
    if kind in MTP_TYPES and not draft:
        target = _model_file(cmd)
        if target and target.is_file() and not _info(target)["nextn"]:
            raise ParamError("Draft type MTP: this model's GGUF has no MTP head. Pick an MTP drafter as Draft model, or another type.")
    if draft:
        drafter = _info(LLM_DIR / draft)["kind"]
        if kind in MTP_TYPES and drafter != "mtp" or kind in DRAFT_FILE_TYPES and _DRAFT_TYPE[drafter] != kind:
            raise ParamError(f"Draft model {draft.rsplit('/', 1)[-1]} is a {_DRAFT_TYPE[drafter]} drafter; Draft type is {kind}.")


def _check_vision(cleaned: dict[str, str], cmd: str) -> None:
    """The config's own head is no override; a head of another width would stop the model from loading."""
    head = cleaned.get("--mmproj")
    if head is None:
        return
    if head == _defaults(cmd)["--mmproj"]:
        del cleaned["--mmproj"]
        return
    if head != "none":
        fits = vision_advice(cmd, [head])[head]
        if fits["fits"] is False:
            raise ParamError(f"Vision head {head.rsplit('/', 1)[-1]}: {fits['why']}.")


def _save_strata(model_id: str, params: dict) -> dict[str, str]:
    """A Strata model's choices; a value equal to its default is not stored. The
    .ctx file is left alone (Video's Agent context reads it for this model)."""
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
