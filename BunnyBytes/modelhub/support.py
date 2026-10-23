"""Which browse results can actually run here: a Rocsy studio template, a ComfyUI
workflow saved under user/default/workflows/<Category>/<Family>/, or llama.cpp (GGUF).

GGUF results (diffusion models in ComfyUI-GGUF's format) only count where something loads them: a studio
with a GGUF checkpoint picker (STUDIO_GGUF) or a saved workflow of the family using a GGUF diffusion loader.

Used for the "Supported only" filter and the card's template badge. A family counts by
its base model (Civitai base, Hugging Face base_model tag) or, for Hugging Face repos
without one, by the family name in the repo name (Comfy-Org/Krea-2, MiniMaxAI/MiniMax-H3).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import sources

WORKFLOWS = Path(os.getenv("MODELHUB_WORKFLOWS_DIR", "/comfy-workflows"))
SKIP_DIRS = {"archive", "imports", "api examples"}

# Families with a generation template in the studios: base -> (label, other names).
STUDIOS = {
    "MiniMax H3": ("Video · MiniMax H3", []),
    "LTXV 2.5": ("Video · LTX 2.5", ["LTX 2.5"]),
    "LTXV 2.3": ("Video · LTX 2.3/2.5", ["LTX 2.3"]),  # 2.3 LoRAs load on 2.5 (sources.ALSO_WORKS_ON)
    "Krea 2": ("Image · Krea 2", ["Lustify"]),     # Lustify v10 is a Krea 2 finetune
    "Qwen 2.1": ("Image · Qwen Image 2.1", ["Qwen Image 2.1"]),
    "Anima": ("Image · Anima", []),
    # Wan 2.2 A14B (Video's Wan 2.2: T2V + I2V experts). Not the TI2V 5B, which has another VAE and layout.
    "Wan Video 2.2 T2V-A14B": ("Video · Wan 2.2", ["Wan2.2 T2V"]),
    "Wan Video 2.2 I2V-A14B": ("Video · Wan 2.2", ["Wan2.2 I2V"]),
    "Wan Video 14B t2v": ("Video · Wan 2.1 → 2.2", []),        # 2.1 14B LoRAs load on the 2.2 A14B experts
    "Wan Video 14B i2v 480p": ("Video · Wan 2.1 → 2.2", []),
    "Wan Video 14B i2v 720p": ("Video · Wan 2.1 → 2.2", []),
    "YuE2": ("Music · YuE2", ["YuE 2"]),
    "MiniMax Music 3": ("Music · MiniMax Music 3", []),
}
# Studios whose model picker loads .gguf diffusion models (Video's H3 checkpoint list, Image's checkpoint list).
STUDIO_GGUF = {"MiniMax H3", "Krea 2", "Qwen 2.1"}
# A saved workflow node that loads a GGUF diffusion model (ComfyUI-GGUF, MultiGPU variants, calcuis' LoaderGGUF).
GGUF_LOADER = re.compile(r'"(?:type|class_type)"\s*:\s*"(?:Unet\w*GGUF\w*|UNET\w*GGUF\w*|LoaderGGUF\w*)"')
# Model types that load in any ComfyUI workflow regardless of base.
ANY_BASE_TYPES = {"Upscaler"}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower()).replace("ltxv", "ltx")


def _pattern(name: str) -> re.Pattern:
    """Whole-word match of a family name inside a repo or file name ("anima" but not "animagine")."""
    tokens = re.findall(r"[a-z]+|\d+", name.lower())
    body = r"[\W_]*".join("ltxv?" if t in ("ltx", "ltxv") else re.escape(t) for t in tokens)
    end = r"(?![0-9])" if tokens[-1].isdigit() else r"(?![a-z])"
    return re.compile(r"(?<![a-z0-9])" + body + end)


_cache: dict = {"at": 0.0, "families": []}


def _uses_gguf(folder: Path) -> bool:
    """A saved workflow here (archives too) loads a GGUF diffusion model."""
    for path in folder.rglob("*.json"):
        try:
            if GGUF_LOADER.search(path.read_text(errors="replace")):
                return True
        except OSError:
            continue
    return False


def families() -> list[dict]:
    """[{label, gguf (label when GGUFs run, else ""), keys (normalized names), patterns}] for studio
    templates + saved ComfyUI workflows."""
    if time.monotonic() - _cache["at"] < 60:
        return _cache["families"]
    found = []
    for base, (label, aliases) in STUDIOS.items():
        names = [base, *aliases]
        found.append({"label": f"{label} template", "gguf": f"{label} template · GGUF" if base in STUDIO_GGUF else "",
                      "keys": {_norm(n) for n in names}, "patterns": [_pattern(n) for n in names]})
    try:
        for group in sorted(WORKFLOWS.iterdir()):
            if not group.is_dir() or group.name.startswith("."):
                continue
            for family in sorted(group.iterdir()):
                if (not family.is_dir() or family.name.startswith(".") or family.name.lower() in SKIP_DIRS
                        or not any(family.rglob("*.json"))):
                    continue
                key = _norm(family.name)
                known = next((f for f in found if key in f["keys"]), None)
                workflow = f"ComfyUI workflow · {group.name}/{family.name}"
                gguf = f"{workflow} · GGUF" if _uses_gguf(family) else ""
                if known:
                    known["gguf"] = known["gguf"] or gguf  # the studio template covers the rest
                    continue
                found.append({"label": workflow, "gguf": gguf, "keys": {key}, "patterns": [_pattern(family.name)]})
    except OSError:
        pass  # workflows folder not mounted: studio templates only
    _cache.update(at=time.monotonic(), families=found)
    return found


def supported(item: dict) -> str:
    """What runs this result here ("" = nothing installed can)."""
    if item.get("category") == "llm":
        return "llama.cpp (GGUF)" if item.get("gguf") else ""
    if item.get("type") in ANY_BASE_TYPES:
        return "ComfyUI upscale"
    label = "gguf" if item.get("gguf") else "label"  # a GGUF result runs only where GGUFs load
    base = sources.base_name(item.get("base") or "")
    keys = {_norm(b) for b in sources.base_family(base)} if base else set()
    for family in families():
        if keys & family["keys"]:
            return family[label]
    if item.get("source") == "hf" or not base:
        # No usable base: Hugging Face repos of the family itself (Comfy-Org/Krea-2, LTX-2.5 GGUFs).
        name = f"{item.get('id', '')} {item.get('name', '')} {item.get('base', '')}".lower()
        for family in families():
            if any(p.search(name) for p in family["patterns"]):
                return family[label]
    return ""
