"""Civitai, Civitai Red and Hugging Face, normalized into one shape.

A *listing* item: {source, id, name, author, kind, category, base, type, preview,
nsfw, stats, url, updated}. A *detail* adds versions: [{id, name, published,
base, notes, variants: [{key, label, files: [{name, size, sha256, url, kind}],
size, quant, format, primary, dest}]}].

Civitai Red is the same API on civitai.red with mature results allowed; the
plain Civitai source asks for SFW results only.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
from datetime import datetime, timezone
import time
import re
from urllib.parse import quote

import httpx

from hardware import quant_of

CIVITAI_HOSTS = {"civitai": "https://civitai.com", "civitai_red": "https://civitai.red"}
HF = "https://huggingface.co"
UA = {"User-Agent": "Rocsy-ModelHub/1.0"}

VIDEO_BASES = {"CogVideoX", "Hunyuan Video", "LTXV", "LTXV2", "LTXV 2.3", "LTXV 2.5", "Mochi", "SVD", "SVD XT",
               "MiniMax H3", "Flux 3 Video", "Kling", "Seedance", "Sora 2", "Veo 3", "Vidu Q1", "HappyHorse"}
AUDIO_BASES = {"ACE Audio", "MiniMax Music 3", "YuE2", "Sonilo"}
THREE_D_BASES = {"Hunyuan3D", "Tripo", "Trellis.2", "Pixal3D", "PolyGen"}
# Single-file "Checkpoint" uploads for these bases are diffusion-model-only (UNet) files.
UNET_BASES = ("Krea", "Qwen", "Flux", "Wan", "LTX", "MiniMax", "HiDream", "Chroma", "ZImage", "Hunyuan",
              "Lumina", "Anima", "Ernie", "Boogu", "Cosmos", "Kolors", "AuraFlow", "Ming", "Lens")
LORA_TYPES = ("LORA", "LoCon", "DoRA")
MODEL_TYPES = ("Checkpoint", "UNet", "LLM", "VisionLanguage", "TextEncoder", "VAE", "Upscaler", "Controlnet", "CLIP")


def civitai_category(base: str, model_type: str) -> str:
    if model_type in ("LLM", "VisionLanguage"):
        return "llm"
    if base.startswith("Wan Video") or base in VIDEO_BASES:
        return "video"
    if base in AUDIO_BASES:
        return "audio"
    if base in THREE_D_BASES:
        return "other"
    return "image"


def all_bases_for(category: str, enum_bases: list[str]) -> list[str]:
    if category == "video":
        return [b for b in enum_bases if b in VIDEO_BASES or b.startswith("Wan Video")]
    if category == "audio":
        return [b for b in enum_bases if b in AUDIO_BASES]
    if category == "image":
        return [b for b in enum_bases if b not in VIDEO_BASES and b not in AUDIO_BASES
                and b not in THREE_D_BASES and not b.startswith("Wan Video")]
    return []


def suggest_dest(kind: str, category: str, model_type: str, base: str, filename: str, repo: str = "") -> str:
    """Folder relative to ComfyUI's models/ for a downloaded file."""
    lower = filename.lower()
    if kind == "lora" or model_type in LORA_TYPES:
        return "loras"
    if category == "llm":
        folder = re.sub(r"[^\w.-]+", "-", (repo.split("/")[-1] if repo else _stem(filename)))[:80]
        return f"llm/{folder}"
    if model_type == "VAE" or re.search(r"(^|[_\-/.])vae([_\-.]|$)", lower):
        return "vae"
    if model_type in ("TextEncoder", "CLIP") or re.search(r"umt5|t5xxl|clip_[lg]|text_encoder|qwen.?[23].?[.\d]*.?vl|gemma|llama|mistral", lower):
        return "text_encoders"
    if model_type == "Upscaler" or re.search(r"(\d)x[_-]|esrgan|upscal", lower):
        return "upscale_models"
    if model_type == "Controlnet":
        return "controlnet"
    if model_type == "CLIPVision":
        return "clip_vision"
    if model_type == "UNet" or (model_type in ("Checkpoint", "") and any(base.startswith(b) for b in UNET_BASES)):
        return "diffusion_models"
    if category in ("video",) or lower.endswith(".gguf"):
        return "diffusion_models"
    return "checkpoints"


def _stem(name: str) -> str:
    return name.rsplit("/", 1)[-1].rsplit(".", 1)[0]


# ---- base models -----------------------------------------------------------------

# Civitai base model name -> Hugging Face repos LoRAs name as base_model:adapter:<repo>.
HF_BASE_REPOS = {
    "Krea 2": ["krea/Krea-2-Turbo", "krea/Krea-2-Raw", "Comfy-Org/Krea-2"],
    "Qwen 2.1": ["Qwen/Qwen-Image-2.1", "Comfy-Org/Qwen-Image-2.1"],
    "Qwen": ["Qwen/Qwen-Image", "Qwen/Qwen-Image-2512", "Qwen/Qwen-Image-Edit-2511", "Qwen/Qwen-Image-Edit-2509"],
    "MiniMax H3": ["MiniMaxAI/MiniMax-H3", "Comfy-Org/MiniMax-H3"],
    "LTXV 2.5": ["Lightricks/LTX-2.5"], "LTXV 2.3": ["Lightricks/LTX-2.3"], "LTXV2": ["Lightricks/LTX-2"],
    "LTXV": ["Lightricks/LTX-Video"], "Anima": ["circlestone-labs/Anima"],
    "Wan Video 2.2 I2V-A14B": ["Wan-AI/Wan2.2-I2V-A14B", "Wan-AI/Wan2.2-I2V-A14B-Diffusers"],
    "Wan Video 2.2 T2V-A14B": ["Wan-AI/Wan2.2-T2V-A14B", "Wan-AI/Wan2.2-T2V-A14B-Diffusers"],
    "Flux.1 D": ["black-forest-labs/FLUX.1-dev"], "Flux.1 Krea": ["black-forest-labs/FLUX.1-Krea-dev"],
    "Flux.2 Klein 9B": ["black-forest-labs/FLUX.2-klein-9B", "black-forest-labs/FLUX.2-klein-base-9B"],
    "Flux.2 Klein 4B": ["black-forest-labs/FLUX.2-klein-4B", "black-forest-labs/FLUX.2-klein-base-4B"],
    "ZImageTurbo": ["Tongyi-MAI/Z-Image-Turbo"], "ZImageBase": ["Tongyi-MAI/Z-Image"],
    "SDXL 1.0": ["stabilityai/stable-diffusion-xl-base-1.0"], "SD 1.5": ["runwayml/stable-diffusion-v1-5"],
    "HiDream": ["HiDream-ai/HiDream-I1-Full", "HiDream-ai/HiDream-I1-Dev"],
}
# LoRAs that load on a newer base too (checked: LTX 2.3 LoRAs fit LTX 2.5's 48 blocks / 4096 dim).
ALSO_WORKS_ON = {"LTXV 2.5": ["LTXV 2.3"],
                 # Wan 2.1 14B LoRAs fit the 2.2 A14B experts (40 blocks / 5120 dim).
                 "Wan Video 2.2 T2V-A14B": ["Wan Video 14B t2v"],
                 "Wan Video 2.2 I2V-A14B": ["Wan Video 14B i2v 480p", "Wan Video 14B i2v 720p"]}


REPO_TO_BASE = {repo.casefold(): name for name, repos in HF_BASE_REPOS.items() for repo in repos}


def base_name(base: str) -> str:
    """Civitai base model name for a Civitai base or a Hugging Face base repo id."""
    return REPO_TO_BASE.get(base.casefold(), base)


def base_family(base: str) -> list[str]:
    return [base] + ALSO_WORKS_ON.get(base, [])


# ---- Civitai -----------------------------------------------------------------

def _civitai_preview(version: dict) -> tuple[str, int, str]:
    for image in version.get("images") or []:
        if image.get("url"):
            return image["url"], int(image.get("nsfwLevel") or 0), image.get("type") or "image"
    return "", 0, "image"


def civitai_paywall(version: dict) -> dict | None:
    """Civitai "Early Access": the creator charges Buzz for a version until a deadline (then it's
    free), or for good (`paidAccess.permanent`). The public API lists such versions as "Public";
    only these fields tell, and the download answers 403 {"error": "Early Access"} until bought."""
    paid = version.get("paidAccess") or {}
    if paid.get("permanent"):
        return {"permanent": True, "ends": ""}
    ends = version.get("earlyAccessDeadline") or paid.get("endsAt") or ""
    if ends:
        with contextlib.suppress(ValueError):
            if datetime.fromisoformat(ends.replace("Z", "+00:00")) > datetime.now(timezone.utc):
                return {"permanent": False, "ends": ends}
    return None


def _civitai_item(model: dict, source: str) -> dict:
    versions = model.get("modelVersions") or [{}]
    first = versions[0]
    # The list API often leaves the newest version's images empty (2-7 models per page of 24)
    # while older versions have plenty: use the first version that has one.
    preview, level, media = next((found for found in map(_civitai_preview, versions) if found[0]), ("", 0, "image"))
    base = first.get("baseModel") or ""
    model_type = model.get("type") or ""
    return {
        "source": source, "id": str(model["id"]), "name": model.get("name") or "",
        "author": (model.get("creator") or {}).get("username") or "",
        "kind": "lora" if model_type in LORA_TYPES else "model", "type": model_type,
        "category": civitai_category(base, model_type), "base": base,
        "preview": preview, "preview_kind": media, "nsfw": bool(model.get("nsfw")) or level >= 4,
        "stats": {"downloads": (model.get("stats") or {}).get("downloadCount", 0),
                  "likes": (model.get("stats") or {}).get("thumbsUpCount", 0)},
        "url": f"{CIVITAI_HOSTS[source]}/models/{model['id']}",
        "updated": first.get("publishedAt") or first.get("updatedAt") or "",
        "latest_version": str(first.get("id") or ""),
        "paywall": civitai_paywall(first),
        "free_version": any(not civitai_paywall(v) for v in versions[1:]),  # an older version needs no Buzz
        "primary": _primary_file(first),
        "quants": _civitai_quants(first),
        "gguf": any((f.get("name") or "").lower().endswith(".gguf") for f in first.get("files") or []),
    }

def _civitai_quants(version: dict) -> list[dict]:
    """Every model weight file of the latest version, for the card's recommended-quant badge."""
    files = version.get("files") or []
    modern = any((f.get("name") or "").lower().endswith((".safetensors", ".gguf")) for f in files)
    out = []
    for f in files:
        meta = f.get("metadata") or {}
        name = f.get("name") or ""
        if not name.lower().endswith(WEIGHT_EXT) and (meta.get("format") or "") not in ("SafeTensor", "GGUF", "PickleTensor"):
            continue  # training data zips, workflows, configs
        if modern and name.lower().endswith(PICKLE_EXT) or TRAINING_JUNK.search(name.lower()):
            continue  # a pickle copy next to the safetensors, or trainer state
        if (f.get("type") or "Model") in ("VAE", "Text Encoder"):
            continue  # extras, not the model itself
        out.append({"name": name, "size": int(float(f.get("sizeKB") or 0) * 1024),
                    "precision": f"{meta.get('fp') or ''} {meta.get('size') or ''} {meta.get('format') or ''}".strip(),
                    "quant_tag": quant_tag(name) or (meta.get("fp") or "").upper()})
    return out


def _primary_file(version: dict) -> dict:
    files = version.get("files") or []
    file = next((f for f in files if f.get("primary")), files[0] if files else None)
    if not file:
        return {}
    meta = file.get("metadata") or {}
    return {"name": file.get("name") or "", "size": int(float(file.get("sizeKB") or 0) * 1024),
            "precision": f"{meta.get('fp') or ''} {meta.get('format') or ''}".strip()}


def _civitai_versions(model: dict) -> list[dict]:
    model_type = model.get("type") or ""
    out = []
    for version in model.get("modelVersions") or []:
        base = version.get("baseModel") or ""
        category = civitai_category(base, model_type)
        kind = "lora" if model_type in LORA_TYPES else "model"
        variants = []
        modern = any((f.get("name") or "").lower().endswith((".safetensors", ".gguf")) for f in version.get("files") or [])
        for file in version.get("files") or []:
            meta = file.get("metadata") or {}
            name = file.get("name") or ""
            if not name.lower().endswith(WEIGHT_EXT) and (meta.get("format") or "") not in ("SafeTensor", "GGUF", "PickleTensor"):
                continue  # training data zips, workflows, configs
            if modern and name.lower().endswith(PICKLE_EXT) or TRAINING_JUNK.search(name.lower()):
                continue  # a pickle copy next to the safetensors, or trainer state
            precision = f"{meta.get('fp') or ''} {meta.get('size') or ''} {meta.get('format') or ''}"
            file_type = file.get("type") or "Model"
            dest = suggest_dest(kind, category, {"VAE": "VAE", "Text Encoder": "TextEncoder"}.get(file_type, model_type),
                                base, name)
            label = " · ".join(x for x in (file_type if file_type != "Model" else "", meta.get("fp") or "",
                                            meta.get("size") or "", meta.get("format") or "") if x) or name
            variants.append({
                "key": str(file.get("id")), "label": label,
                "files": [{"name": name, "size": int(float(file.get("sizeKB") or 0) * 1024),
                           "sha256": ((file.get("hashes") or {}).get("SHA256") or "").lower(),
                           "url": file.get("downloadUrl") or f"https://civitai.com/api/download/models/{version.get('id')}",
                           "dest": dest}],
                "size": int(float(file.get("sizeKB") or 0) * 1024), "quant": quant_of(name, precision),
                "format": meta.get("format") or "", "primary": bool(file.get("primary")), "dest": dest,
                "quant_tag": quant_tag(name) or (meta.get("fp") or "").upper(),
            })
        preview, level, media = _civitai_preview(version)
        out.append({"id": str(version.get("id")), "name": version.get("name") or "", "base": base,
                    "published": version.get("publishedAt") or "", "category": category,
                    "triggers": version.get("trainedWords") or [], "variants": variants,
                    "preview": preview, "preview_kind": media, "nsfw": level >= 4,
                    "paywall": civitai_paywall(version),
                    "early_access": bool(civitai_paywall(version)),
                    "images": [{"url": i.get("url"), "type": i.get("type") or "image", "nsfwLevel": int(i.get("nsfwLevel") or 1)}
                               for i in (version.get("images") or [])[:8] if i.get("url")]})
    return out


SPARSE_SEARCH_CALLS = 12  # Civitai keyword search: requests per page while results are sparse


async def civitai_search(client: httpx.AsyncClient, source: str, kind: str, category: str, query: str,
                         sort: str, cursor: str, enum_bases: list[str], key: str, base: str = "") -> dict:
    params: list[tuple[str, str]] = [("limit", "24"), ("nsfw", "true" if source == "civitai_red" else "false")]
    if query:
        params.append(("query", query))
    else:
        params.append(("sort", {"downloads": "Most Downloaded", "rating": "Highest Rated", "newest": "Newest"}.get(sort, "Most Downloaded")))
    if cursor:
        params.append(("cursor", cursor))
    if kind == "lora":
        params += [("types", t) for t in LORA_TYPES]
    elif category == "llm":
        params += [("types", "LLM"), ("types", "VisionLanguage")]
    else:
        params += [("types", t) for t in MODEL_TYPES if t not in ("LLM", "VisionLanguage")]
    if base:
        params += [("baseModels", b) for b in base_family(base)]
    elif category in ("image", "video", "audio"):
        params += [("baseModels", b) for b in all_bases_for(category, enum_bases)]
    headers = {**UA, **({"Authorization": f"Bearer {key}"} if key else {})}
    url = f"{CIVITAI_HOSTS[source]}/api/v1/models"
    response = await client.get(url, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    models = list(data.get("items") or [])
    next_cursor = (data.get("metadata") or {}).get("nextCursor") or ""
    # Keyword search filters types/base models AFTER paging the raw hits, so pages come back
    # short or empty with a next cursor anyway ("femboy" + Qwen 2.1 = ~40 empty pages). Read on
    # in bigger steps until there's a page's worth or the hits run out.
    calls = 1
    while query and next_cursor and len(models) < 24 and calls < SPARSE_SEARCH_CALLS:
        more = [(k, v) for k, v in params if k not in ("limit", "cursor")] + [("limit", "100"), ("cursor", next_cursor)]
        response = await client.get(url, params=more, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        models += data.get("items") or []
        next_cursor = (data.get("metadata") or {}).get("nextCursor") or ""
        calls += 1
    items = [_civitai_item(model, source) for model in models]
    for item in items:
        if not item["preview"]:
            apply_image_fallback(source, item)  # answered before: no need to ask again
    return {"items": items, "next": next_cursor}


# Images-API answers per model, kept a few hours: the call can take seconds (20 s seen), so the
# page shouldn't wait for it twice. None = asked, the model has no image either.
fallback_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
FALLBACK_TTL = 6 * 3600


def apply_image_fallback(source: str, item: dict) -> bool:
    """Puts a cached images-API answer on the card. True when the cache already had an answer."""
    hit = fallback_cache.get((source, str(item["id"])))
    if not hit or time.monotonic() - hit[0] > FALLBACK_TTL:
        return False
    if hit[1]:
        item.update(preview=hit[1]["url"], preview_kind=hit[1]["kind"], nsfw=item["nsfw"] or hit[1]["nsfw"])
    return True


CIVITAI_IMAGE_LEVELS = {"None": 1, "Soft": 2, "Mature": 4, "X": 8}


async def civitai_image_fallback(client: httpx.AsyncClient, source: str, item: dict, key: str) -> None:
    """No image on any version in the list answer: ask the images API for the model's top image.
    Usually well under a second, sometimes many; the caller decides how long the page waits."""
    headers = {**UA, **({"Authorization": f"Bearer {key}"} if key else {})}
    with contextlib.suppress(httpx.HTTPError, ValueError, KeyError, TypeError):
        response = await client.get(f"{CIVITAI_HOSTS[source]}/api/v1/images", headers=headers, timeout=20,
                                    params={"modelId": item["id"], "limit": "1", "sort": "Most Reactions",
                                            # Civitai Red shows mature results; without this the API skips X-rated images
                                            **({"nsfw": "X"} if source == "civitai_red" else {})})
        response.raise_for_status()
        image = (response.json().get("items") or [None])[0]
        if image and image.get("url"):
            level = image.get("nsfwLevel")
            level = CIVITAI_IMAGE_LEVELS.get(level, 1) if isinstance(level, str) else int(level or 1)
            item.update(preview=image["url"], preview_kind=image.get("type") or "image",
                        nsfw=item["nsfw"] or level >= 4)
        fallback_cache[(source, str(item["id"]))] = (time.monotonic(), image and image.get("url") and {
            "url": image["url"], "kind": image.get("type") or "image", "nsfw": level >= 4} or None)


async def civitai_model(client: httpx.AsyncClient, source: str, model_id: str, key: str) -> dict:
    headers = {**UA, **({"Authorization": f"Bearer {key}"} if key else {})}
    response = await client.get(f"{CIVITAI_HOSTS[source]}/api/v1/models/{int(model_id)}", headers=headers, timeout=30)
    response.raise_for_status()
    model = response.json()
    item = _civitai_item(model, source)
    item["versions"] = _civitai_versions(model)
    item["tags"] = [t if isinstance(t, str) else str((t or {}).get("name") or "") for t in model.get("tags") or []]
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", model.get("description") or "")
    text = re.sub(r"<[^>]+>", "", text)
    item["description"] = re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()[:1500]
    return item


async def civitai_by_hash(client: httpx.AsyncClient, sha256: str, key: str) -> dict | None:
    headers = {**UA, **({"Authorization": f"Bearer {key}"} if key else {})}
    response = await client.get(f"https://civitai.com/api/v1/model-versions/by-hash/{sha256}", headers=headers, timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


# ---- Hugging Face -------------------------------------------------------------

HF_PIPELINES = {"llm": ["text-generation", "image-text-to-text"], "image": ["text-to-image", "image-to-image"],
                "video": ["text-to-video", "image-to-video"], "audio": ["text-to-audio", "text-to-speech"]}
WEIGHT_EXT = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft")
PICKLE_EXT = (".bin", ".pt", ".pth")
# Trainer leftovers that share the weight extensions but aren't models.
TRAINING_JUNK = re.compile(r"(^|/)(training_args|optimizer|scheduler|scaler|rng_state[^/]*|trainer_state|"
                           r"tokenizer|vocab|sentencepiece[^/]*)\.(bin|pt|pth)$")
SHARD = re.compile(r"-(\d{5})-of-(\d{5})(?=\.gguf$)|-(\d{5})-of-(\d{5})(?=\.safetensors$)", re.I)


def hf_category(pipeline: str, tags: list[str]) -> str:
    for category, pipelines in HF_PIPELINES.items():
        if pipeline in pipelines:
            return category
    joined = " ".join(tags).lower()
    if "gguf" in joined and ("llama" in joined or "conversational" in joined):
        return "llm"
    if "video" in joined:
        return "video"
    if "audio" in joined or "music" in joined or "tts" in joined:
        return "audio"
    if "diffusers" in joined or "comfyui" in joined:
        return "image"
    return "other"


# Which lab a model comes from, so its card shows that lab's Hugging Face avatar (a bartowski/unsloth
# GGUF of Qwen gets Qwen's logo). Matched on the repo name, then its base model; the earliest hit wins,
# so "DeepSeek-R1-Distill-Qwen" is DeepSeek's.
LAB_LOGOS = [
    (r"qwen|qwq", "Qwen"), (r"mistral|mixtral|ministral|magistral|devstral|codestral|pixtral|voxtral", "mistralai"),
    (r"llama", "meta-llama"), (r"gemma|medgemma|paligemma", "google"), (r"(?<![a-z])phi[-_]?\d", "microsoft"),
    (r"deepseek", "deepseek-ai"), (r"chatglm|(?<![a-z])glm[-_]?\d|cogvideo|cogview", "zai-org"),
    (r"kimi|moonlight", "moonshotai"), (r"minimax", "MiniMaxAI"), (r"nemotron|parakeet|cosmos", "nvidia"),
    (r"granite", "ibm-granite"), (r"command[-_]?[ra]|(?<![a-z])aya(?![a-z])|c4ai", "CohereLabs"), (r"falcon", "tiiuae"),
    (r"olmo|tulu|molmo", "allenai"), (r"smollm|smolvlm|idefics", "HuggingFaceTB"), (r"hunyuan", "tencent"),
    (r"ernie", "baidu"), (r"internlm|internvl", "internlm"), (r"(?<![a-z])yi[-_]\d", "01-ai"), (r"exaone", "LGAI-EXAONE"),
    (r"gpt[-_]?oss|whisper", "openai"), (r"seed[-_]?oss|bagel", "ByteDance-Seed"), (r"flux", "black-forest-labs"),
    (r"stable[-_]?diffusion|sdxl|(?<![a-z])sd3|stablelm|stable[-_]?audio", "stabilityai"), (r"(?<![a-z])wan[-_]?\d", "Wan-AI"),
    (r"ltx", "Lightricks"), (r"(?<![a-z])step[-_]?\d|stepfun", "stepfun-ai"), (r"baichuan", "baichuan-inc"),
    (r"lfm[-_]?\d", "LiquidAI"), (r"jamba", "ai21labs"), (r"hidream", "HiDream-ai"), (r"hermes", "NousResearch"),
    (r"(?<![a-z])ace[-_]?step", "ACE-Step"), (r"chroma", "lodestones"), (r"lumina", "Alpha-VLLM"),
]
LAB_ORGS = {org.lower(): org for _, org in LAB_LOGOS}


def hf_logo_org(repo: str, base: str = "") -> str:
    author = repo.split("/")[0].lower() if "/" in repo else ""
    if author in LAB_ORGS:
        return LAB_ORGS[author]
    for text in (repo.split("/")[-1].lower(), base.lower()):
        hits = [(m.start(), org) for pattern, org in LAB_LOGOS if (m := re.search(pattern, text))]
        if hits:
            return min(hits)[1]
    # Otherwise the publisher's own logo when it is an organization (BAAI, sentence-transformers);
    # people have no org avatar and the logo endpoint 404s for them, leaving the cube.
    if author and author not in REPACKERS:
        return repo.split("/")[0]
    return ""


# Re-uploaders and quantizers: their logo isn't the model's.
REPACKERS = {"unsloth", "bartowski", "mradermacher", "lmstudio-community", "ggml-org", "quantfactory", "thebloke",
             "comfy-org", "city96", "kijai", "mlx-community", "nexaai", "second-state", "gaianet", "bullerwins",
             "huihui-ai", "davidau", "ista-daslab", "redhatai", "neuralmagic", "intel", "amd", "ollama"}


def _hf_item(model: dict, kind: str = "") -> dict:
    tags = model.get("tags") or []
    repo = model.get("id") or model.get("modelId") or ""
    is_lora = "lora" in tags or any(t.startswith("base_model:adapter:") for t in tags)
    base = next((t.split(":", 2)[-1] for t in tags if t.startswith("base_model:") and t.count(":") >= 2), "") or \
           next((t.split(":", 1)[1] for t in tags if t.startswith("base_model:") and t.count(":") == 1), "")
    return {"source": "hf", "id": repo, "name": repo.split("/")[-1], "author": repo.split("/")[0] if "/" in repo else "",
            "kind": kind or ("lora" if is_lora else "model"), "type": model.get("library_name") or "",
            "category": hf_category(model.get("pipeline_tag") or "", tags), "base": base,
            "preview": "", "preview_kind": "image", "nsfw": "not-for-all-audiences" in tags, "logo": hf_logo_org(repo, base),
            "gated": bool(model.get("gated")), "gguf": "gguf" in tags,
            "stats": {"downloads": model.get("downloads", 0), "likes": model.get("likes", 0)},
            "url": f"{HF}/{repo}", "updated": model.get("lastModified") or model.get("createdAt") or ""}


async def hf_search(client: httpx.AsyncClient, kind: str, category: str, query: str, sort: str,
                    cursor: str, token: str, gguf_only: bool, base: str = "") -> dict:
    headers = {**UA, **({"Authorization": f"Bearer {token}"} if token else {})}
    order = {"downloads": "downloads", "rating": "likes", "newest": "lastModified"}.get(sort, "downloads")
    common = [("sort", order), ("direction", "-1")] + [
        ("expand[]", f) for f in ("downloads", "likes", "lastModified", "pipeline_tag", "gated", "tags", "library_name")]
    if kind == "lora":
        common.append(("filter", "lora"))
    if gguf_only and category == "llm" and not base:
        common.append(("filter", "gguf"))
    # The API ANDs repeated filters, so OR-ing (two pipelines for "video", several
    # repos for one base model like Krea 2 Turbo + Raw) takes one request each;
    # the cursor is the list of every request's next-page URL.
    if base:
        repos = [r for b in base_family(base) for r in HF_BASE_REPOS.get(b, [])]
        tag = "base_model:adapter:" if kind == "lora" else "base_model:finetune:"
        branches = [[("filter", tag + r)] for r in repos] or [[]]
        if not repos:  # unknown on HF: search by the base's name instead
            query = f"{query} {base}".strip()
    else:
        branches = [[("pipeline_tag", p)] if p else [] for p in HF_PIPELINES.get(category, [None])]
    if query:
        common.append(("search", query))
    if cursor:
        calls = [(u, None) for u in cursor.split("|") if u]
    else:
        limit = ("limit", str(max(8, 24 // len(branches))))
        calls = [(f"{HF}/api/models", common + [limit] + extra) for extra in branches]
    items, nexts = [], []
    # Branches are independent requests: fetch them side by side.
    for response in await asyncio.gather(*(client.get(url, params=params, headers=headers, timeout=30) for url, params in calls)):
        response.raise_for_status()
        items += response.json()
        if response.links.get("next"):
            nexts.append(response.links["next"]["url"])
    seen, unique = set(), []
    for model in sorted(items, key=lambda m: m.get(order) or 0, reverse=True):
        if model["id"] not in seen:
            seen.add(model["id"])
            unique.append(_hf_item(model, kind if kind == "lora" else ""))
    return {"items": unique, "next": "|".join(nexts)}


QUANT_TAG = re.compile(r"(?<![a-z0-9])((?:ud-)?(?:i?q[1-8](?:_[a-z0-9]+)*|bf16|f16|f32|fp16|fp32|fp8(?:_[a-z0-9]+)*|"
                       r"mxfp4(?:_[a-z0-9]+)*|nvfp4|int[48]|nf4))(?![a-z0-9])", re.I)


def quant_tag(name: str) -> str:
    """The exact quant written in a file name (Q4_K_XL, UD-Q2_K_XL, IQ3_XXS), for the quant picker."""
    stem = re.sub(r"\.(gguf|safetensors|sft|ckpt|pt|pth|bin)$", "", name.split("/")[-1], flags=re.I)
    stem = SHARD.sub("", stem)
    found = QUANT_TAG.findall(stem)
    return found[-1].upper() if found else ""


def _hf_variants(repo: str, siblings: list[dict], category: str, kind: str) -> list[dict]:
    names = {s["rfilename"] for s in siblings}
    files = {s["rfilename"]: s for s in siblings}

    def file_entry(name: str, dest: str) -> dict:
        info = files[name]
        lfs = info.get("lfs") or {}
        return {"name": name, "size": int(info.get("size") or lfs.get("size") or 0),
                "sha256": (lfs.get("sha256") or lfs.get("oid") or "").lower(),
                "url": f"{HF}/{repo}/resolve/main/{quote(name)}", "dest": dest}

    variants = []
    if "model_index.json" in names:  # a diffusers / multi-file repo: offered whole
        dest = f"diffusers/{repo.split('/')[-1]}"
        entries = [file_entry(n, dest) for n in sorted(names) if not n.startswith(".")]
        size = sum(e["size"] for e in entries)
        variants.append({"key": "repo", "label": "Full repository (diffusers)", "files": entries, "size": size,
                         "quant": quant_of(repo), "format": "diffusers", "primary": False, "dest": dest, "whole_repo": True})
    groups: dict[str, list[str]] = {}
    mmproj = []
    lowers = [n.lower() for n in names]
    modern = any(n.endswith((".safetensors", ".gguf", ".sft")) for n in lowers)
    gguf = any(n.endswith(".gguf") for n in lowers)
    for name in sorted(names):
        lower = name.lower()
        if not lower.endswith(WEIGHT_EXT) or TRAINING_JUNK.search(lower):
            continue
        if lower.endswith(PICKLE_EXT) and (modern or "model_index.json" in names):
            continue  # old pickle copies of weights the repo also has as safetensors/GGUF
        if category == "llm" and gguf and not lower.endswith(".gguf"):
            continue  # llama.cpp only loads the GGUFs; the original safetensors are for training
        if "/" in name and "model_index.json" in names:
            continue  # parts of the diffusers layout
        if "mmproj" in lower:
            mmproj.append(name)
            continue
        groups.setdefault(SHARD.sub("", name), []).append(name)
    for group, members in groups.items():
        lower = group.lower()
        file_kind = kind if kind == "lora" else ("llm" if category == "llm" and lower.endswith(".gguf") else category)
        dest = _repo_folder(group) or suggest_dest(kind, file_kind, "", "", group.split("/")[-1], repo)
        entries = [file_entry(m, dest) for m in members]
        size = sum(e["size"] for e in entries)
        q = quant_of(group)
        label = (q["label"] or ("safetensors" if lower.endswith(".safetensors") else group.rsplit(".", 1)[-1].upper()))
        if len(members) > 1:
            label += f" · {len(members)} parts"
        variants.append({"key": group, "label": label, "file_label": group.split("/")[-1], "files": entries, "size": size,
                         "quant_tag": quant_tag(group),
                         "quant": q, "format": "GGUF" if lower.endswith(".gguf") else "SafeTensor", "primary": False,
                         "dest": dest, "companions": [], "role": ROLE_BY_FOLDER.get(dest) or _hf_role(group)})
    if mmproj and category == "llm":
        best = sorted(mmproj, key=lambda n: quant_of(n)["rank"])  # smallest precision that still is F16/BF16
        pick = next((n for n in best if quant_of(n)["label"] in ("F16", "BF16")), best[-1])
        for variant in variants:
            if variant["format"] == "GGUF":
                variant["companions"] = [file_entry(pick, variant["dest"])]
    mains = [v for v in variants if not v.get("whole_repo")]
    if mains and all(v.get("role") == "Draft model" for v in mains):
        for v in mains:  # the repo itself is a draft/MTP build: those files ARE the model
            v["role"] = ""
    return variants


async def hf_quants(client: httpx.AsyncClient, repo: str, token: str, category: str, kind: str) -> list[dict]:
    """Just the weight variants (name, size, quant) of a repo, for the search card's
    recommended-quant badge — one light call, no README or card parsing."""
    headers = {**UA, **({"Authorization": f"Bearer {token}"} if token else {})}
    response = await client.get(f"{HF}/api/models/{repo}", params={"blobs": "true"}, headers=headers, timeout=15)
    response.raise_for_status()
    model = response.json()
    return [{"name": v.get("file_label") or v["files"][0]["name"], "format": v["format"],
             "size": v["size"] + sum(c["size"] for c in v.get("companions") or []),
             "quant": v["quant"], "role": v.get("role", ""),
             "quant_tag": v.get("quant_tag") or v["quant"]["label"] or ("Full repo" if v.get("whole_repo") else "")}
            for v in _hf_variants(repo, model.get("siblings") or [], category, kind)]


# Repos laid out like ComfyUI (Comfy-Org "repackaged": split_files/<folder>/...)
# say where each file goes.
COMFY_FOLDERS = {"diffusion_models": "diffusion_models", "unet": "diffusion_models", "loras": "loras",
                 "text_encoders": "text_encoders", "clip": "text_encoders", "vae": "vae", "audio_encoders": "audio_encoders",
                 "clip_vision": "clip_vision", "checkpoints": "checkpoints", "upscale_models": "upscale_models",
                 "controlnet": "controlnet", "model_patches": "model_patches", "latent_upscale_models": "latent_upscale_models"}
ROLE_BY_FOLDER = {"loras": "LoRA", "text_encoders": "Text encoder", "vae": "VAE", "audio_encoders": "Audio encoder",
                  "clip_vision": "CLIP vision", "upscale_models": "Upscaler", "controlnet": "ControlNet",
                  "model_patches": "Model patch", "latent_upscale_models": "Latent upscaler"}


def _repo_folder(name: str) -> str:
    for part in name.split("/")[:-1]:
        if part in COMFY_FOLDERS:
            return COMFY_FOLDERS[part]
    return ""


def _hf_role(name: str) -> str:
    lower = name.lower()
    if re.search(r"(^|[_\-/.])(draft|mtp|dflash)([_\-.]|$)", lower):
        return "Draft model"
    if re.search(r"(^|[_\-/.])vae([_\-.]|$)", lower):
        return "VAE"
    if re.search(r"umt5|t5xxl|clip_[lg]|text_encoder|gemma|qwen.?\d.?\d?.?vl", lower):
        return "Text encoder"
    return ""


MEDIA_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm")
README_IMAGE = re.compile(r'!\[[^\]]*\]\(\s*<?([^)\s>]+)>?[^)]*\)|<(?:img|video|source)[^>]+src=["\']([^"\']+)["\']', re.I)
README_TRIGGER = re.compile(r'(?im)(?:trigger(?:\s*words?|\s*phrases?|\s*tokens?)?|activation\s*(?:words?|tokens?)|instance\s*prompt|触发词)'
                            r'[^:：=\n]{0,12}[:：=]\s*(.{2,160})$')
QUOTED = re.compile(r'[`"“]([^`"”\n]{2,60})[`"”]')


def _trigger_words(value: str) -> list[str]:
    """Words from a 'Trigger word: ...' line: quoted ones if any, else short comma-separated tokens."""
    quoted = QUOTED.findall(value)
    parts = quoted or re.split(r"[,;，、]| or ", value)
    words = []
    for part in parts:
        part = part.strip(" *_.`'")
        # A trigger is a token or short phrase, not a sentence.
        if 1 < len(part) <= 40 and (quoted or len(part.split()) <= 3) and not re.search(
                r"(?i)^(none|n/a|not (required|needed)|no trigger)|\b(the|is|are|it|this|use|put|add)\b", part):
            words.append(part)
    return words


def _readme_parts(repo: str, text: str) -> tuple[str, list[str], list[dict]]:
    """Description, trigger words and sample media from a model card's README."""
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    media = []
    for match in README_IMAGE.finditer(text):
        url = (match.group(1) or match.group(2) or "").strip()
        if not url.lower().split("?")[0].endswith(MEDIA_EXT):
            continue
        if not url.startswith("http"):
            url = f"{HF}/{repo}/resolve/main/{quote(url.lstrip('./'))}"
        if url.startswith("https://") and url not in [m["url"] for m in media]:
            media.append({"url": url, "type": "video" if url.lower().split("?")[0].endswith((".mp4", ".webm")) else "image",
                          "nsfwLevel": 1})
    triggers = []
    for match in README_TRIGGER.finditer(text):
        for word in _trigger_words(match.group(1)):
            if word.casefold() not in {t.casefold() for t in triggers}:
                triggers.append(word)
    plain = README_IMAGE.sub("", text)
    plain = re.sub(r"<[^>]+>", "", plain)
    plain = re.sub(r"```.*?```", "", plain, flags=re.S)
    plain = re.sub(r"^#+\s*", "", plain, flags=re.M)
    plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", plain)
    plain = re.sub(r"[*_`]{1,3}", "", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    return plain[:3000], triggers[:8], media[:8]


async def hf_model(client: httpx.AsyncClient, repo: str, token: str, kind: str = "") -> dict:
    headers = {**UA, **({"Authorization": f"Bearer {token}"} if token else {})}
    response = await client.get(f"{HF}/api/models/{repo}", params={"blobs": "true"}, headers=headers, timeout=30)
    response.raise_for_status()
    model = response.json()
    item = _hf_item(model, kind)
    variants = _hf_variants(repo, model.get("siblings") or [], item["category"], item["kind"])
    item["revision"] = model.get("sha") or ""
    item["versions"] = [{"id": item["revision"], "name": f"main @ {item['revision'][:7]}", "base": item["base"],
                         "published": model.get("lastModified") or "", "category": item["category"],
                         "triggers": [], "variants": variants, "preview": "", "nsfw": item["nsfw"]}]
    card = model.get("cardData") or {}
    item["license"] = card.get("license") or ""
    item["description"] = ""
    item["tags"] = [t for t in model.get("tags") or [] if ":" not in t]
    # LoRA cards: trigger word and sample images (widget outputs live in the repo).
    prompt = card.get("instance_prompt")
    triggers = [prompt] if isinstance(prompt, str) and prompt.strip() else []
    images = []
    for widget in card.get("widget") or []:
        url = ((widget or {}).get("output") or {}).get("url") if isinstance(widget, dict) else None
        if isinstance(url, str) and url:
            full = url if url.startswith("https://") else f"{HF}/{repo}/resolve/main/{quote(url)}"
            images.append({"url": full, "type": "video" if url.lower().endswith((".mp4", ".webm")) else "image", "nsfwLevel": 1})
    # The README is where most Hugging Face LoRAs put their description, trigger words and samples.
    with contextlib.suppress(httpx.HTTPError):
        readme = await client.get(f"{HF}/{repo}/raw/main/README.md", headers=headers, timeout=20, follow_redirects=True)
        if readme.status_code == 200 and len(readme.content) < 400_000:
            description, readme_triggers, readme_media = _readme_parts(repo, readme.text)
            item["description"] = description
            triggers += [t for t in readme_triggers if t.casefold() not in {x.casefold() for x in triggers}]
            images += [m for m in readme_media if m["url"] not in {i["url"] for i in images}]
    item["versions"][0]["triggers"] = triggers[:8]
    item["versions"][0]["images"] = images[:8]
    if images:
        item["versions"][0]["preview"] = images[0]["url"]
        item["versions"][0]["preview_kind"] = images[0]["type"]
    return item
