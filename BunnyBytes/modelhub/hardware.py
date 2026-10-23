"""Hardware scan + "will this run here" scoring for model files.

The scan reads sysfs and /proc (both visible inside the container), so it shows
what is installed right now. Compatibility is an estimate from file size, the
kind of model and its quantization: memory needed = weights + a working
overhead per kind, compared with usable VRAM (minus a reserve on the GPU that
drives the desktop), all GPUs together (LLMs: llama.cpp splits layers) and RAM
(ComfyUI offloads diffusion weights to system memory, slowly).
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

GB = 1024 ** 3
DISPLAY_RESERVE = 1.5 * GB  # Xorg + browser on the GPU that drives the monitor

# PCI device id -> (name, LLVM target). Unknown cards still count by VRAM.
AMD_GPUS = {
    "0x744c": ("Radeon RX 7900 XTX / Radeon RX 7900 XT", "gfx1100"), "0x7448": ("Radeon Pro W7900", "gfx1100"),
    "0x747e": ("Radeon RX 7800 XT / Radeon RX 7700 XT", "gfx1101"), "0x7480": ("Radeon RX 7600", "gfx1102"),
    "0x7550": ("Radeon RX 9070 XT / Radeon RX 9070", "gfx1201"), "0x7590": ("Radeon RX 9060 XT", "gfx1200"),
    "0x73bf": ("Radeon RX 6800 XT – 6950 XT", "gfx1030"), "0x73df": ("Radeon RX 6700 XT", "gfx1031"),
}


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _on_nvme(path: Path) -> bool:
    """Whether the mount containing the download folder is backed by local NVMe."""
    target = str(path.resolve())
    best = (0, False)
    for line in _read(Path("/proc/self/mountinfo")).splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields, source = left.split(), right.split()
        if len(fields) < 5 or len(source) < 2:
            continue
        mount = fields[4].replace("\\040", " ")
        if (target == mount or target.startswith(mount.rstrip("/") + "/")) and len(mount) > best[0]:
            best = (len(mount), bool(re.fullmatch(r"/dev/nvme\d+n\d+(?:p\d+)?", source[1])))
    return best[1]


def scan(disk_paths: dict[str, Path] | None = None) -> dict:
    gpus = []
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        device = card / "device"
        vendor = _read(device / "vendor")
        if not vendor or "-" in card.name:
            continue
        total = int(_read(device / "mem_info_vram_total") or 0)
        used = int(_read(device / "mem_info_vram_used") or 0)
        ident = _read(device / "device")
        if vendor == "0x1002":
            name, arch = AMD_GPUS.get(ident, (f"AMD GPU {ident}", ""))
            if " / " in name:  # one PCI id, two cards: VRAM tells them apart
                big, small = name.split(" / ")
                name = big if total >= {"0x744c": 22, "0x747e": 14, "0x7550": 15}.get(ident, 0) * GB else small
            name = "AMD " + name
        elif vendor == "0x10de":
            name, arch = "NVIDIA GPU", "cuda"
        else:
            continue
        display = _read(device / "boot_vga") == "1"
        gpus.append({"name": name, "arch": arch, "vendor": "amd" if vendor == "0x1002" else "nvidia",
                     "vram": total, "vram_used": used, "display": display,
                     "usable": max(0, total - (DISPLAY_RESERVE if display else 0))})
    meminfo = {line.split(":")[0]: int(line.split()[1]) * 1024
               for line in _read(Path("/proc/meminfo")).splitlines() if line.split()[1:2]}
    cpu = next((line.split(":", 1)[1].strip() for line in _read(Path("/proc/cpuinfo")).splitlines()
                if line.startswith("model name")), "")
    disks = {}
    for label, path in (disk_paths or {}).items():
        try:
            usage = shutil.disk_usage(path)
            disks[label] = {"free": usage.free, "total": usage.total, "nvme": _on_nvme(path)}
        except OSError:
            pass
    return {"gpus": gpus, "ram": meminfo.get("MemTotal", 0), "ram_available": meminfo.get("MemAvailable", 0),
            "cpu": cpu, "cores": os.cpu_count() or 0, "disks": disks}


# ---- quantization labels -------------------------------------------------

# (pattern, label, quality rank, bits per weight roughly). First match wins.
QUANTS = [
    (r"(?<![a-z0-9])f32|fp32", "F32", 100, 32), (r"bf16", "BF16", 92, 16), (r"(?<![a-z0-9])f16|fp16", "F16", 91, 16),
    (r"q8_0|q8(?![0-9_])", "Q8_0", 84, 8.5), (r"nvfp4|mxfp4|(?<![a-z])fp4", "FP4", 44, 4.5),
    (r"fp8|e4m3|e5m2|float8", "FP8", 80, 8), (r"int8|w8a8", "INT8", 79, 8),
    (r"q6_k|q6(?![0-9_])", "Q6_K", 72, 6.6), (r"q5_k_m", "Q5_K_M", 64, 5.7), (r"q5_k_s", "Q5_K_S", 62, 5.5),
    (r"q5_[01]", "Q5_0", 60, 5.5), (r"iq4_nl", "IQ4_NL", 54, 4.5), (r"q4_k_m", "Q4_K_M", 55, 4.8),
    (r"q4_k_s", "Q4_K_S", 52, 4.6), (r"iq4_xs", "IQ4_XS", 53, 4.3), (r"q4_[01]", "Q4_0", 48, 4.5),
    (r"int4|w4a16|nf4", "4-bit", 46, 4.5), (r"q3_k_l", "Q3_K_L", 42, 4.0), (r"q3_k_m", "Q3_K_M", 40, 3.9),
    (r"iq3_m", "IQ3_M", 39, 3.7), (r"q3_k_s", "Q3_K_S", 36, 3.5), (r"iq3_xs|iq3_xxs", "IQ3_XS", 35, 3.3),
    (r"q2_k", "Q2_K", 26, 3.0), (r"iq2", "IQ2", 22, 2.4), (r"iq1|q1_", "IQ1", 12, 1.8),
]


def quant_of(name: str, precision: str = "") -> dict:
    text = f"{name} {precision}".lower()
    for pattern, label, rank, bits in QUANTS:
        if re.search(pattern, text):
            return {"label": label, "rank": rank, "bits": bits}
    return {"label": "", "rank": 85, "bits": 16}  # unlabeled safetensors are usually fp16/bf16


# ---- compatibility --------------------------------------------------------

OVERHEAD = {"llm": (1.10, 1.5), "image": (1.0, 3.0), "video": (1.0, 7.0), "audio": (1.0, 2.0),
            "encoder": (1.0, 0.5), "lora": (1.0, 0.0), "other": (1.0, 1.0)}


def ngram_nvme_bytes(size: int, category: str, name: str, fmt: str, quant: dict) -> int:
    """Approximate Qwen3.8 Flash Next PLE table bytes in a quantized GGUF.

    The FP8 table is 47.684 GiB. GGUF variants quantize it differently, so
    this is a fit estimate, not a substitute for reading GGUF tensor metadata.
    """
    if category != "llm" or ".gguf" not in name.lower() and fmt.lower() != "gguf":
        return 0
    if not re.search(r"qwen3[.\-_]?8.*flash[.\-_ ]?next", name, re.I):
        return 0
    bits = min(float(quant.get("bits", 8)), 8)
    return min(int(47.684 * GB * bits / 8), int(size * 0.8))


def fit(size: int, category: str, name: str = "", fmt: str = "", hw: dict | None = None,
        quant: dict | None = None) -> dict:
    """{tier: gpu|multi|offload|no|unknown, label, note, need}."""
    hw = hw or {}
    gpus = hw.get("gpus") or []
    lower = f"{name} {fmt}".lower()
    q = quant or quant_of(name, fmt)
    notes = []
    amd = any(gpu["vendor"] == "amd" for gpu in gpus)
    rdna3 = any(gpu["arch"].startswith("gfx11") for gpu in gpus)
    if amd:
        if re.search(r"nvfp4|mxfp4", lower):
            return {"tier": "no", "label": "NVIDIA-only format", "note": "NVFP4/MXFP4 kernels need an NVIDIA Blackwell GPU", "need": 0}
        if re.search(r"\bmlx\b|-mlx", lower):
            return {"tier": "no", "label": "Apple-only format", "note": "MLX is Apple-silicon only", "need": 0}
        if category == "llm" and re.search(r"\b(awq|gptq|exl2|exl3)\b", lower):
            return {"tier": "no", "label": "Not GGUF (llama.cpp)", "note": "llama.cpp needs GGUF; AWQ/GPTQ/EXL are for other runtimes", "need": 0}
        if "nf4" in lower or "bnb" in lower:
            notes.append("bitsandbytes 4-bit is unreliable on ROCm")
        if q["label"] == "FP8" and rdna3:
            notes.append("FP8 weights are computed in BF16 on RDNA 3 (no FP8 math)")
    if category in ("image", "video", "audio") and ".gguf" in lower:
        notes.append("GGUF diffusion needs ComfyUI-GGUF (installed)")
    if not size:
        return {"tier": "unknown", "label": "Size unknown", "note": "; ".join(notes), "need": 0}
    if category == "lora":
        return {"tier": "gpu", "label": "Loads with base model", "tight": False,
                "note": "; ".join(notes) or f"Adds ~{size / GB * 1024:.0f} MB on top of its base model", "need": size}
    factor, extra = OVERHEAD.get(category, OVERHEAD["other"])
    need = int(size * factor + extra * GB)
    if not gpus:
        return {"tier": "unknown", "label": "No GPU found", "note": "; ".join(notes), "need": need}
    best = max(gpus, key=lambda gpu: gpu["usable"])
    total = sum(gpu["usable"] for gpu in gpus)
    ram = hw.get("ram", 0)
    tight = False
    ngram = ngram_nvme_bytes(size, category, name, fmt, q)
    nvme = any(disk.get("nvme") for disk in (hw.get("disks") or {}).values())
    disk_need = need - int(ngram * factor)
    if need <= best["usable"]:
        spare = (best["usable"] - need) / GB
        tight = spare < best["usable"] / GB * 0.15
        tier, label = "gpu", ("Fits in VRAM (tight)" if tight else "Fits in VRAM")
        gpu = best["name"].replace("AMD ", "").replace("Radeon ", "")
        left = f"{spare:.1f}" if spare < 2 else f"{spare:.0f}"
        notes.insert(0, f"Needs ~{need / GB:.0f} GB on the {gpu}, ~{left} GB left"
                        + (": close other GPU apps first" if tight else ""))
    elif category == "llm" and len(gpus) > 1 and need <= total:
        tier, label = "multi", "Split across GPUs"
        notes.insert(0, f"llama.cpp splits it over {len(gpus)} GPUs")
    elif ngram and nvme and disk_need <= max(total, best["usable"] + ram * 0.6):
        tier, label = "nvme", "NGram on NVMe"
        place = ("GPU" if disk_need <= best["usable"] else
                 "GPUs" if len(gpus) > 1 and disk_need <= total else "GPU + RAM")
        notes.insert(0, f"Estimated ~{ngram / GB:.0f} GB PLE NGram table stays on local NVMe; "
                        f"~{disk_need / GB:.0f} GB still needs {place}. Requires llama.cpp mmap + lazy on-direct; "
                        "NVMe random reads may slow prompt processing")
    elif need <= best["usable"] + ram * 0.6:
        tier = "offload"
        over = (need - best["usable"]) / GB
        if category == "llm":
            label = "Needs CPU offload"
            notes.insert(0, f"~{over:.0f} GB more than VRAM: llama.cpp runs those layers on the CPU (much slower tokens/s)")
        else:
            label = "Needs RAM offload"
            notes.insert(0, f"~{over:.0f} GB more than VRAM: ComfyUI keeps that part in system RAM and streams it in (several times slower)")
    else:
        tier, label = "no", "Too big for VRAM + RAM"
        notes.insert(0, "More than your VRAM + RAM can hold")
    return {"tier": tier, "label": label, "tight": tight, "note": "; ".join(notes), "need": need}


TIER_ORDER = {"gpu": 0, "multi": 1, "nvme": 2, "offload": 3, "unknown": 4, "no": 5}


def best_variant(variants: list[dict]) -> str | None:
    """Key of the best-quality variant that fits in the best available tier.
    Each variant has: key, fit (from fit()), quant (from quant_of())."""
    usable = [v for v in variants if v["fit"]["tier"] in ("gpu", "multi", "nvme", "offload")]
    if not usable:
        return None
    top = min(TIER_ORDER[v["fit"]["tier"]] for v in usable)
    pool = [v for v in usable if TIER_ORDER[v["fit"]["tier"]] == top]
    # Prefer comfortable headroom over the very largest that only just fits.
    roomy = [v for v in pool if not v["fit"].get("tight")] or pool
    return max(roomy, key=lambda v: (v["quant"]["rank"], -v.get("size", 0)))["key"]
