"""Finds MiniMax H3 checkpoints in ComfyUI's diffusion_models folder and
works out which ones support Reference (Ref2V) mode.

Ref2V and FL2V (Text/Keyframe) checkpoints have identical tensor names,
shapes and dtypes - Ref2V-ness lives purely in the trained weights. So each
checkpoint is fingerprinted: a handful of small unquantized tensors (input
projections, output head, conditioning projection) are projected onto the
line from the official FL2V checkpoint (0) to the official Ref2V one (1).
The official Ref2V checkpoint scores 1.00 and the official/standard FL2V ones
0.00; community merges land anywhere in between (one measured 0.85).
The median over tensors is used - one tensor is stored in a different
layout in some conversions and would skew a mean.

GGUF checkpoints (ComfyUI-GGUF's UnetLoaderGGUF) are fingerprinted the same
way: their small tensors stay F32/F16/BF16. Only GGUFs with an architecture
ComfyUI-GGUF accepts load in ComfyUI (Vantage's H3 files say "wan"); the
stable-diffusion.cpp ones (Unsloth, no architecture) are reported unsupported.

Only the header and those few MB are read, and results are cached by file
size + mtime, so a rescan only touches new or changed files."""
from __future__ import annotations

import json
import logging
import re
import statistics
import struct
from array import array
from pathlib import Path
from typing import Any

log = logging.getLogger("h3machine.checkpoints")

REF2V_ANCHOR = "minimaxH3INT8INT4_ref2vaINT8Pruned.safetensors"
FL2V_ANCHOR = "minimax_h3_fl2va_pruned_int8_convrot_official.safetensors"
FINGERPRINT_TENSORS = (
    "audio_patch_proj.bias", "video_patch_proj.bias", "condition_proj.bias", "final_layer.norm.weight",
    "final_layer.adaln_proj.linear.bias", "final_layer.video_out.bias", "adaln_t_table",
    "token_refiner.blocks.1.norm2.weight",
)
# Present in every H3 checkpoint (and the delta/"hybrid" formats), absent from other model families.
H3_SIGNATURE = ("audio_patch_proj.bias", "condition_proj.bias", "final_layer.video_out.bias")
_PREFIXES = ("", "diffusion_model.", "model.diffusion_model.")
REF2V_THRESHOLD = 0.75
HYBRID_THRESHOLD = 0.25


def _header(path: Path) -> tuple[int, dict[str, Any]]:
    with path.open("rb") as handle:
        size = struct.unpack("<Q", handle.read(8))[0]
        if size > 100_000_000:
            raise ValueError("not a safetensors header")
        return 8 + size, json.loads(handle.read(size))


def _find(header: dict[str, Any], name: str) -> str | None:
    return next((prefix + name for prefix in _PREFIXES if prefix + name in header), None)


def _vector(path: Path, offset: int, info: dict[str, Any]) -> list[float] | None:
    start, end = info["data_offsets"]
    if end - start > 4_000_000:
        return None
    with path.open("rb") as handle:
        handle.seek(offset + start)
        raw = handle.read(end - start)
    if info["dtype"] == "BF16":
        halves = array("H")
        halves.frombytes(raw)
        return list(array("f", array("I", (value << 16 for value in halves)).tobytes()))
    if info["dtype"] == "F32":
        return list(array("f", raw))
    if info["dtype"] == "F16":
        return [value[0] for value in struct.iter_unpack("<e", raw)]
    return None


# GGUF value types -> struct format (8 = string, 9 = array are read separately).
_GGUF_SCALARS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
# GGUF tensor types read for fingerprints (the quantized ones are skipped, like int8 safetensors).
_GGUF_DTYPES = {0: ("F32", 4), 1: ("F16", 2), 30: ("BF16", 2)}
# Architectures ComfyUI-GGUF's UnetLoaderGGUF accepts for diffusion models (its IMG_ARCH_LIST).
GGUF_ARCHES = {"flux", "sd1", "sdxl", "sd3", "aura", "hidream", "cosmos", "ltxv", "hyvid", "wan", "lumina2", "qwen_image"}


def _gguf_header(path: Path) -> tuple[int, dict[str, Any], str | None]:
    """(data offset, safetensors-style header {name: {dtype, data_offsets}}, general.architecture)."""
    with path.open("rb") as handle:
        def read(fmt: str) -> Any:
            return struct.unpack("<" + fmt, handle.read(struct.calcsize("<" + fmt)))[0]

        def string() -> str:
            size = read("Q")
            if size > 100_000_000:
                raise ValueError("not a GGUF header")
            return handle.read(size).decode("utf-8", "replace")

        def value(kind: int) -> Any:
            if kind == 8:
                return string()
            if kind == 9:
                item, count = read("I"), read("Q")
                return [value(item) for _ in range(count)]
            return read(_GGUF_SCALARS[kind])

        if handle.read(4) != b"GGUF":
            raise ValueError("not a GGUF file")
        read("I")  # version
        tensors, entries = read("Q"), read("Q")
        meta = {}
        for _ in range(entries):
            key = string()
            meta[key] = value(read("I"))
        header: dict[str, Any] = {}
        for _ in range(tensors):
            name = string()
            count = 1
            for _dim in range(read("I")):
                count *= read("Q")
            kind, start = read("I"), read("Q")
            dtype, width = _GGUF_DTYPES.get(kind, (f"GGML{kind}", 0))
            header[name] = {"dtype": dtype, "data_offsets": [start, start + count * width]}
        alignment = int(meta.get("general.alignment") or 32)
        offset = -(-handle.tell() // alignment) * alignment
    arch = meta.get("general.architecture")
    return offset, header, arch if isinstance(arch, str) else None


def _fingerprint(path: Path) -> dict[str, list[float]] | None:
    """The fingerprint tensors, or None when this isn't an H3 checkpoint."""
    offset, header = _gguf_header(path)[:2] if path.suffix.lower() == ".gguf" else _header(path)
    if not all(_find(header, name) for name in H3_SIGNATURE):
        return None
    vectors = {}
    for name in FINGERPRINT_TENSORS:
        key = _find(header, name)
        if key and header[key].get("dtype") in ("BF16", "F32", "F16"):
            vector = _vector(path, offset, header[key])
            if vector:
                vectors[name] = vector
    return vectors


def _score(sample: dict[str, list[float]], ref2v: dict[str, list[float]], fl2v: dict[str, list[float]]) -> float | None:
    scores = []
    for name, x in sample.items():
        r, f = ref2v.get(name), fl2v.get(name)
        if not r or not f or not (len(x) == len(r) == len(f)):
            continue
        direction = [a - b for a, b in zip(r, f)]
        norm = sum(value * value for value in direction)
        if norm:
            scores.append(sum((xi - fi) * di for xi, fi, di in zip(x, f, direction)) / norm)
    return statistics.median(scores) if scores else None


def _kind(score: float | None, name: str) -> tuple[str, str]:
    """(kind, how it was decided). Falls back to the filename when the
    anchors aren't installed to compare against."""
    if score is not None:
        kind = "ref2v" if score >= REF2V_THRESHOLD else "hybrid" if score >= HYBRID_THRESHOLD else "fl2v"
        return kind, "weights"
    if re.search(r"ref2v", name, re.I):
        return "ref2v", "filename"
    if re.search(r"fl2v", name, re.I):
        return "fl2v", "filename"
    return "unknown", "none"


class CheckpointScanner:
    def __init__(self, models_dir: Path, cache_path: Path):
        self.models_dir = models_dir
        self.cache_path = cache_path
        self.results: list[dict[str, Any]] = []

    def _load_cache(self) -> dict[str, Any]:
        try:
            return json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return {}

    def scan(self) -> list[dict[str, Any]]:
        if not self.models_dir.is_dir():
            log.warning("Checkpoint folder %s is not mounted - no Ref2V detection", self.models_dir)
            return self.results
        cache = self._load_cache()
        anchors: dict[str, dict[str, list[float]] | None] = {}

        def anchor(name: str) -> dict[str, list[float]] | None:
            if name not in anchors:
                path = self.models_dir / name
                anchors[name] = _fingerprint(path) if path.is_file() else None
            return anchors[name]

        fresh: dict[str, Any] = {}
        paths = [*self.models_dir.rglob("*.safetensors"), *self.models_dir.rglob("*.gguf")]
        for path in sorted(paths):
            value = path.relative_to(self.models_dir).as_posix()
            stat = path.stat()
            cached = cache.get(value)
            if cached and cached.get("size") == stat.st_size and cached.get("mtime") == int(stat.st_mtime):
                fresh[value] = cached
                continue
            entry: dict[str, Any] = {"size": stat.st_size, "mtime": int(stat.st_mtime), "h3": False}
            if path.suffix.lower() == ".gguf":
                entry["format"] = "gguf"
            try:
                sample = _fingerprint(path)
                if sample is not None and entry.get("format") == "gguf":
                    arch = _gguf_header(path)[2]
                    if arch not in GGUF_ARCHES:
                        # ComfyUI-GGUF refuses it ("Unexpected architecture"), so it isn't offered
                        entry["unsupported"] = f"GGUF architecture {arch or 'missing (stable-diffusion.cpp file)'}"
                        log.info("H3 GGUF %s not loadable in ComfyUI: %s", value, entry["unsupported"])
                        sample = None
                if sample is not None:
                    ref2v, fl2v = anchor(REF2V_ANCHOR), anchor(FL2V_ANCHOR)
                    score = _score(sample, ref2v, fl2v) if ref2v and fl2v else None
                    kind, basis = _kind(score, value)
                    entry.update(h3=True, score=None if score is None else round(score, 3) + 0.0, kind=kind, basis=basis)
                    log.info("H3 checkpoint %s: %s (score %s, by %s)", value, kind, entry["score"], basis)
            except Exception as exc:  # noqa: BLE001 - one unreadable file mustn't stop the scan
                entry["error"] = str(exc)[:200]
            fresh[value] = entry
        try:
            self.cache_path.write_text(json.dumps(fresh, indent=1))
        except OSError:
            log.warning("Could not write checkpoint scan cache %s", self.cache_path)
        self.results = [{"value": value, **entry} for value, entry in fresh.items() if entry.get("h3")]
        return self.results
