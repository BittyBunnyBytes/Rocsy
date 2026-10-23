"""Where downloads go after they land on disk.

- LoRAs for a studio's base model go into that studio's custom LoRA folder and
  are registered in its LoRA gallery (H3 Machine: MiniMax H3 + LTX; Krea: Krea 2
  + Qwen 2.1) with the metadata Civitai / Hugging Face already gave us.
- GGUF LLMs get a llama-swap entry in ~/AI/Llama/config.yaml. llama-swap only
  reads its config on start, so it is restarted (through hostctl) once no model
  is loaded - never while one is in use.
- "Used by" lookups for the Installed tab, so uninstalling never surprises.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
import unicodedata
from pathlib import Path

import httpx

SECRET = os.getenv("QUEUE_INTERNAL_SECRET", "")
VID = os.getenv("MODELHUB_VID_URL", "http://127.0.0.1:8000/Vid")
KREA = os.getenv("MODELHUB_KREA_URL", "http://127.0.0.1:8300/Krea")
LLAMA = os.getenv("MODELHUB_LLAMA_URL", "http://127.0.0.1:8400")
LLAMA_KEY = os.getenv("LLAMA_API_KEY", "")
LLAMA_CONFIG = Path(os.getenv("MODELHUB_LLAMA_CONFIG", "/llama-config.yaml"))
HOSTCTL = os.getenv("HOSTCTL_URL", "http://127.0.0.1:8899").rstrip("/")
HOSTCTL_SECRET = os.getenv("HOSTCTL_SECRET", "")
# Host path of ComfyUI's models folder (compose: ${ROCSY_COMFY_ROOT}/models); llama-swap entries use it.
MODELS_HOST_DIR = os.getenv("MODELHUB_MODELS_HOST_DIR") or "/comfy-models"

# Civitai base model -> (studio, gallery model id, custom LoRA folder under models/, label)
GALLERIES = {
    "MiniMax H3": ("Vid", "minimax_h3", "loras/H3MachineCustom", "Video LoRA gallery (MiniMax H3)"),
    "LTXV 2.5": ("Vid", "ltx_2_5", "loras/H3MachineCustom", "Video LoRA gallery (LTX 2.5)"),
    "LTXV 2.3": ("Vid", "ltx_2_5", "loras/H3MachineCustom", "Video LoRA gallery (LTX 2.5)"),
    # Wan pairs (high + low noise files) are joined into one gallery LoRA by Video (custom_loras.wan_expert).
    "Wan Video 2.2 T2V-A14B": ("Vid", "wan_2_2", "loras/H3MachineCustom", "Video LoRA gallery (Wan 2.2)"),
    "Wan Video 2.2 I2V-A14B": ("Vid", "wan_2_2", "loras/H3MachineCustom", "Video LoRA gallery (Wan 2.2)"),
    "Wan Video 14B t2v": ("Vid", "wan_2_2", "loras/H3MachineCustom", "Video LoRA gallery (Wan 2.2)"),
    "Wan Video 14B i2v 480p": ("Vid", "wan_2_2", "loras/H3MachineCustom", "Video LoRA gallery (Wan 2.2)"),
    "Wan Video 14B i2v 720p": ("Vid", "wan_2_2", "loras/H3MachineCustom", "Video LoRA gallery (Wan 2.2)"),
    "Krea 2": ("Krea", "krea", "loras/KreaCustom", "Image LoRA gallery (Krea 2)"),
    "Qwen 2.1": ("Krea", "qwen", "loras/KreaCustom", "Image LoRA gallery (Qwen Image 2.1)"),
}
STUDIO_URL = {"Vid": VID, "Krea": KREA}
STUDIO_NAME = {"Vid": "Video", "Krea": "Image"}


def gallery_for(base: str) -> dict | None:
    hit = GALLERIES.get(base)
    if not hit:
        return None
    studio, model, folder, label = hit
    return {"studio": studio, "model": model, "dest": folder, "label": label}


def storage_name(name: str, folder: Path) -> str:
    """The studios' rule for custom LoRA file names: ASCII, no spaces, unique."""
    stem = name.replace("\\", "/").rsplit("/", 1)[-1]
    if stem.lower().endswith(".safetensors"):
        stem = stem[:-12]
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:80].strip("._-") or "LoRA"
    candidate, counter = f"{stem}.safetensors", 2
    while (folder / candidate).exists() or (folder / (candidate + ".part")).exists():
        candidate, counter = f"{stem}-{counter}.safetensors", counter + 1
    return candidate


STRENGTH = re.compile(r"(?:strength|weight)[^0-9\n]{0,24}?([0-2](?:\.\d{1,2})?)(?:\s*(?:-|to|~)\s*([0-2](?:\.\d{1,2})?))?", re.I)


def recommended_strength(text: str) -> tuple[float | None, str]:
    """A strength the author states ("weight 0.6-0.8"), else None."""
    match = STRENGTH.search(text or "")
    if not match:
        return None, ""
    low = float(match.group(1))
    high = float(match.group(2)) if match.group(2) else low
    value = round((low + high) / 2, 2)
    if not 0 < value <= 2:
        return None, ""
    span = f"{low}-{high}" if high != low else f"{low}"
    return value, f"Author suggests {span}"


async def register_lora(job: dict, after: dict) -> str:
    """Tell the studio about a LoRA that just finished downloading into its folder."""
    gallery = after["gallery"]
    body = {key: after.get(key) for key in ("label", "triggers", "description", "creator", "version", "base_model",
                                             "url", "strength_note", "default_strength", "category", "previews")}
    body.update(model=gallery["model"], storage_name=job["name"], source_key=f"models:{job['group']}")
    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(f"{STUDIO_URL[gallery['studio']]}/api/internal/lora-gallery/register",
                                     json=body, headers={"X-Queue-Internal-Secret": SECRET})
    if response.status_code != 200:
        detail = response.text[-200:]
        with contextlib.suppress(ValueError):
            detail = response.json().get("message") or response.json().get("detail") or detail
        raise RuntimeError(f"{gallery['label']} refused it: {detail}")
    warning = response.json().get("warning") or ""
    return f"Added to the {gallery['label']}" + (f" ({warning})" if warning else "")


# Folders ComfyUI's diffusion-model loaders read; Image picks up new files from here.
DIFFUSION_DIRS = ("diffusion_models", "unet")


async def video_rescan(name: str) -> bool:
    """Video re-reads diffusion_models (its H3 scan fingerprints safetensors and GGUF files); True when it lists
    this file. Best effort: Video may be down, its next start scans anyway."""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{VID}/api/checkpoints/rescan")
        data = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return False
    found = data.get("minimax_h3_checkpoints", []) + data.get("scanned_checkpoints", [])
    return any(str(item.get("value", "")).split("/")[-1] == name for item in found)


async def register_checkpoint(job: dict, base: str) -> str:
    """Tell Video and Image a diffusion model landed (Image gets its base model), so they list it now."""
    if await video_rescan(Path(job["name"]).name):
        return "Added to Video's checkpoint list"
    if GALLERIES.get(base, ("",))[0] == "Vid" and base.startswith("Wan Video"):
        # Video's Wan 2.2 has no checkpoint menu: each expert file is a slot of the Advanced workflow card.
        return ("Ready for Video's Wan 2.2: pick it for the high- or low-noise expert in Video → Advanced workflow"
                if "2.2" in base else "Wan 2.1 model: Video's Wan 2.2 uses 2.2 A14B experts, so it isn't used there")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{KREA}/api/internal/checkpoints/register",
                                     json={"filename": job["name"], "base": base},
                                     headers={"X-Queue-Internal-Secret": SECRET})
    if response.status_code != 200:
        raise RuntimeError(f"Image refused it: {response.text[-200:]}")
    data = response.json()
    if data.get("listed"):
        return "Added to Image's checkpoint list"
    if data.get("warning"):
        return f"Image will list it once ComfyUI is back ({data['warning']})"
    return f"Image has no workflow for {base or 'this base model'}, so it isn't listed there"


async def categories(gallery: dict) -> list[str]:
    with contextlib.suppress(httpx.HTTPError, ValueError):
        async with httpx.AsyncClient(timeout=10) as client:
            data = (await client.get(f"{STUDIO_URL[gallery['studio']]}/api/lora-gallery/catalog/{gallery['model']}")).json()
            return [c for c in data.get("categories") or [] if isinstance(c, str)]
    return []


def pick_category(existing: list[str], tags: list[str], nsfw: bool) -> str:
    """An existing gallery category matching the model's tags; NSFW first."""
    lowered = {c.casefold(): c for c in existing}
    if nsfw and "nsfw" in lowered:
        return lowered["nsfw"]
    synonyms = {"style": ["style", "styles", "art style"], "character": ["characters", "character"],
                "concept": ["concept", "concepts", "general"], "clothing": ["clothing", "outfits"],
                "realistic": ["realism"], "photography": ["realism", "camera & film"], "camera": ["camera", "camera & film"],
                "motion": ["motion"], "anime": ["anime", "2d animation"], "3d": ["3d animation", "3d"]}
    for tag in tags:
        tag = tag.casefold()
        for candidate in [tag] + synonyms.get(tag, []):
            if candidate in lowered:
                return lowered[candidate]
    return ""


# ---- llama-swap ----------------------------------------------------------------------

BLOCK = re.compile(r'^  "(?P<id>[^"]+)":(?: &\S+)?\n(?P<body>(?:^(?:    .*|\s*)\n)*)', re.M)
MANAGED_BEGIN = "  # >>> rocsy-models:{id}"
MANAGED_END = "  # <<< rocsy-models:{id}"


def host_path(rel: str) -> str:
    return f"{MODELS_HOST_DIR}/{rel}"


def llama_models() -> list[dict]:
    """llama-swap entries: id, name, model/mmproj paths, and whether Models added it."""
    try:
        text = LLAMA_CONFIG.read_text()
    except OSError:
        return []
    out = []
    for match in BLOCK.finditer(text):
        body = match.group("body")
        name = re.search(r'^    name:\s*"?([^"\n]+)"?', body, re.M)
        out.append({"id": match.group("id"), "name": name.group(1) if name else match.group("id"),
                    "model": (re.search(r"--model\s+(\S+)", body) or [None, ""])[1],
                    "mmproj": (re.search(r"--mmproj\s+(\S+)", body) or [None, ""])[1],
                    "managed": MANAGED_BEGIN.format(id=match.group("id")) in text})
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-.")[:60] or "model"


def _write_in_place(text: str) -> None:
    # Other containers bind-mount this single file: replacing it (new inode)
    # would leave them reading the old copy, so rewrite the same file.
    backup = LLAMA_CONFIG.with_name(f"config.yaml.bak-models-{time.strftime('%Y%m%d-%H%M%S')}")
    with contextlib.suppress(OSError):
        if LLAMA_CONFIG.parent.is_dir() and os.access(LLAMA_CONFIG.parent, os.W_OK):
            backup.write_text(LLAMA_CONFIG.read_text())
    with LLAMA_CONFIG.open("r+") as handle:
        handle.seek(0)
        handle.write(text)
        handle.truncate()


def add_llama_model(name: str, model_rel: str, mmproj_rel: str, offload: bool, source: str,
                    ngram_nvme: bool = False) -> str:
    """Append a llama-swap entry (between rocsy-models markers) and return its id."""
    text = LLAMA_CONFIG.read_text()
    path = host_path(model_rel)
    existing = next((m for m in llama_models() if m["model"] == path), None)
    if existing:
        return existing["id"]
    taken = {m["id"] for m in llama_models()}
    base = _slug(name)
    model_id, counter = base, 2
    while model_id in taken:
        model_id, counter = f"{base}-{counter}", counter + 1
    lines = [MANAGED_BEGIN.format(id=model_id),
             f"  # Added by Rocsy Models {time.strftime('%Y-%m-%d')} from {source}",
             f'  "{model_id}":', f'    name: "{name.replace(chr(34), "")}"', "    cmd: |",
             # GPU-sized models load fully on ROCm0; bigger ones let llama.cpp fit what it can (2.5 GB margin) and run the rest on the CPU.
             f"      ${{llama_launch}} {'auto 2560' if offload else 'all none'}",
             f"      --model {path}"]
    if mmproj_rel:
        lines.append(f"      --mmproj {host_path(mmproj_rel)}")
    lines += ["      --ctx-size 65536", "      --parallel 1"] + (["      --fit on"] if offload else []) + (
              ["      --load-mode mmap", "      --lazy-mode on-direct"] if ngram_nvme else []) + [
              "      --flash-attn on", "      --cache-type-k q8_0", "      --cache-type-v q8_0",
              "      --host 127.0.0.1", "      --port ${PORT}", "    ttl: 1800", MANAGED_END.format(id=model_id)]
    text = text.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n"
    _write_in_place(text)
    return model_id


def remove_llama_model(model_id: str) -> bool:
    """Remove an entry Models added (only those: hand-written entries are never touched)."""
    text = LLAMA_CONFIG.read_text()
    begin, end = MANAGED_BEGIN.format(id=model_id), MANAGED_END.format(id=model_id)
    start, stop = text.find(begin), text.find(end)
    if start < 0 or stop < 0:
        return False
    stop += len(end)
    text = text[:start].rstrip("\n") + "\n" + text[stop:].lstrip("\n")
    _write_in_place(text if text.endswith("\n") else text + "\n")
    return True


async def delete_llama_hipblaslt_tuning(model_id: str) -> None:
    """Remove a GGUF model's per-model calibration before deleting its file."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model_id):
        raise ValueError("Invalid llama-swap model id")
    if not HOSTCTL_SECRET:
        raise RuntimeError("Host helper is required to remove model tuning data")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(f"{HOSTCTL}/llama-hipblaslt/delete",
                                     headers={"X-Hostctl-Secret": HOSTCTL_SECRET},
                                     json={"model": model_id})
    if response.status_code >= 300:
        try:
            detail = response.json().get("error", "Could not remove tuning data")
        except ValueError:
            detail = "Could not remove tuning data"
        raise RuntimeError(detail)


class LlamaRestarter:
    """llama-swap reads config.yaml only on start: restart it once nothing is loaded."""

    def __init__(self):
        self.pending = False
        self.reason = ""
        self.last = 0.0

    def request(self, reason: str) -> None:
        self.pending, self.reason = True, reason

    async def loaded(self) -> list[str] | None:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                data = (await client.get(f"{LLAMA}/running", headers={"Authorization": f"Bearer {LLAMA_KEY}"})).json()
            return [str(item.get("model")) for item in data.get("running", [])]
        except (httpx.HTTPError, ValueError):
            return None

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            if not self.pending or not HOSTCTL_SECRET:
                continue
            loaded = await self.loaded()
            if loaded is None or loaded:
                continue  # unreachable or a model is in use: try again later
            with contextlib.suppress(httpx.HTTPError):
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.post(f"{HOSTCTL}/items/llama/restart", headers={"X-Hostctl-Secret": HOSTCTL_SECRET})
                if response.status_code == 200:
                    self.pending, self.last = False, time.time()


# ---- who uses what ---------------------------------------------------------------------

async def lora_usage() -> dict[str, list[dict]]:
    """loras/<file> -> [{studio, model, label, custom_id}] from the studios' galleries."""
    usage: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=8) as client:
        for base, (studio, model, _folder, label) in GALLERIES.items():
            if any(u["model"] == model for uses in usage.values() for u in uses):
                continue  # LTXV 2.3 and 2.5 share one gallery
            with contextlib.suppress(httpx.HTTPError, ValueError):
                data = (await client.get(f"{STUDIO_URL[studio]}/api/lora-gallery/catalog/{model}")).json()
                for filename in data.get("files") or data.get("order") or []:  # "files" adds a Wan pair's second half
                    usage.setdefault(f"loras/{filename}", []).append({"studio": studio, "model": model, "label": label})
        for studio in ("Vid", "Krea"):
            with contextlib.suppress(httpx.HTTPError, ValueError):
                data = (await client.get(f"{STUDIO_URL[studio]}/api/lora-gallery/custom")).json()
                for item in data.get("items") or []:
                    for use in usage.setdefault(f"loras/{item['filename']}", []):
                        use["custom_id"] = item["id"]
                    if not usage[f"loras/{item['filename']}"]:
                        usage[f"loras/{item['filename']}"].append({"studio": studio, "model": item.get("model"),
                                                                   "label": f"{STUDIO_NAME[studio]} LoRA gallery",
                                                                   "custom_id": item["id"]})
    return usage


async def delete_custom_lora(studio: str, custom_id: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.delete(f"{STUDIO_URL[studio]}/api/lora-gallery/custom/{custom_id}")
    if response.status_code not in (200, 404):
        raise RuntimeError(f"The {STUDIO_NAME[studio]} gallery refused: {response.text[-200:]}")
