"""Strata's n-gram (PLE) table at full precision: the 128 BF16 `ngram_embedding` shards of Qwen's own
checkpoint, fetched with HTTP Range requests (only those tensors, 102.4 GB of the 360 GB checkpoint)
into a one-tensor GGUF the engine reads with --ple-gguf (BF16 is one of its PLE formats, #586).

The models' shard 2 holds the same table as IQ4_NL (90 B a row, ~8% off per row); the abliterated
builds leave it untouched, so this one file serves every size. The engine reads the table from the
SSD a few rows a token (--ple-io direct), so the wider rows cost disk space, not RAM or VRAM.

Resumable: the .part file's length says how far it got. Checked at the end: exact size, then sampled
rows against the installed IQ4_NL table (same rows in the same order, within quantization error)."""
from __future__ import annotations

import json
import os
import random
import struct
import time
import urllib.request
from pathlib import Path
from typing import Callable

REPO = "Qwen/Qwen3.8-Flash-Next"
REVISION = "de4b8e4d43b917e7706784d8bb445c9af86a3540"   # the checkpoint SC117's MTP head was extracted from
BASE = f"https://huggingface.co/{REPO}/resolve/{REVISION}"
DIM, ROWS = 160, 320001536
GGML_TYPE_BF16, GGUF_TYPE_STRING, ALIGN = 30, 8, 32
CHUNK = 16 << 20
IQ4_NL = (-127, -104, -83, -65, -49, -35, -22, -10, 1, 13, 25, 38, 53, 69, 89, 113)


def get(url: str, start: int | None = None, end: int | None = None, timeout: float = 60):
    headers = {"User-Agent": "rocsy-strata-ngram"}
    if start is not None:
        headers["Range"] = f"bytes={start}-{'' if end is None else end}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def retry(fn, tries: int = 50):
    for n in range(tries):
        try:
            return fn()
        except (OSError, ValueError) as exc:
            if n == tries - 1:
                raise RuntimeError(f"download failed after {tries} tries: {exc}") from exc
            time.sleep(min(60, 5 * (n + 1)))


def gguf_string(s: str) -> bytes:
    b = s.encode()
    return struct.pack("<Q", len(b)) + b


def header() -> bytes:
    kvs = [("general.architecture", "strata-ple"), ("general.name", "PLE n-gram table, BF16 as in the checkpoint"),
           ("strata.ple.source", f"{REPO}@{REVISION}")]
    head = b"GGUF" + struct.pack("<IQQ", 3, 1, len(kvs))
    head += b"".join(gguf_string(k) + struct.pack("<I", GGUF_TYPE_STRING) + gguf_string(v) for k, v in kvs)
    head += gguf_string("per_layer_token_embd.weight") + struct.pack("<I", 2) + struct.pack("<QQ", DIM, ROWS)
    head += struct.pack("<I", GGML_TYPE_BF16) + struct.pack("<Q", 0)
    return head + b"\0" * ((-len(head)) % ALIGN)


def plan() -> list[tuple[str, int, int]]:
    """(file URL, absolute byte offset, length) of shard 0..127 in order."""
    index = json.load(retry(lambda: get(f"{BASE}/model.safetensors.index.json")))["weight_map"]
    names = {int(n.rsplit(".shard_", 1)[1].split(".")[0]): n for n in index
             if ".ngram_embedding.shard_" in n and n.endswith(".weight")}
    if sorted(names) != list(range(len(names))):
        raise RuntimeError("the checkpoint's n-gram shards are not numbered 0..N without gaps")
    headers: dict[str, tuple[dict, int]] = {}
    parts, rows = [], 0
    for k in range(len(names)):
        name, file = names[k], index[names[k]]
        if file not in headers:
            url = f"{BASE}/{file}"
            n = struct.unpack("<Q", retry(lambda: get(url, 0, 7).read()))[0]
            headers[file] = (json.loads(retry(lambda: get(url, 8, 7 + n).read())), 8 + n)
        hdr, base = headers[file]
        t = hdr[name]
        if t["dtype"] != "BF16" or len(t["shape"]) != 2 or t["shape"][1] != DIM:
            raise RuntimeError(f"{name} is {t['dtype']} {t['shape']}, not BF16 [rows, {DIM}]")
        a, b = t["data_offsets"]
        if b - a != t["shape"][0] * DIM * 2:
            raise RuntimeError(f"{name}: size does not match its shape")
        parts.append((f"{BASE}/{file}", base + a, b - a))
        rows += t["shape"][0]
    if rows != ROWS:
        raise RuntimeError(f"the shards hold {rows} rows, the engine expects {ROWS}")
    return parts


def download(out: Path, progress: Callable[[int, int], None]) -> None:
    head, parts = header(), plan()
    total = len(head) + sum(n for _, _, n in parts)
    part = out.with_name(out.name + ".part")
    out.parent.mkdir(parents=True, exist_ok=True)
    if not part.exists() or part.stat().st_size < len(head):
        part.write_bytes(head)
    else:
        with open(part, "rb") as r:
            if r.read(len(head)) != head:
                raise RuntimeError(f"{part} has a different header: delete it to start over")
    with open(part, "r+b") as w:
        done = w.seek(0, os.SEEK_END)
        at = len(head)
        for url, offset, length in parts:
            if done >= at + length:
                at += length
                continue
            skip = done - at

            def fetch() -> None:
                nonlocal skip, done
                with get(url, offset + skip, offset + length - 1, timeout=120) as r:
                    while skip < length:
                        block = r.read(min(CHUNK, length - skip))
                        if not block:
                            raise ValueError("connection closed early")
                        w.write(block)
                        skip += len(block)
                        done += len(block)
                        progress(done, total)
            retry(fetch)
            w.flush()
            at += length
    if part.stat().st_size != total:
        raise RuntimeError(f"size check failed: {part.stat().st_size} != {total}")
    os.replace(part, out)


def bf16_row(data: bytes) -> list[float]:
    return [struct.unpack("<f", struct.pack("<I", v << 16))[0] for v in struct.unpack(f"<{DIM}H", data)]


def iq4_nl_row(data: bytes) -> list[float]:
    out = [0.0] * DIM
    for b in range(DIM // 32):
        blk = data[b * 18:(b + 1) * 18]
        d = struct.unpack("<e", blk[:2])[0]
        for j in range(16):
            q = blk[2 + j]
            out[b * 32 + j] = d * IQ4_NL[q & 15]
            out[b * 32 + j + 16] = d * IQ4_NL[q >> 4]
    return out


def check(bf16: Path, iq4: Path, samples: int = 512) -> float:
    """The median relative error of sampled rows against the IQ4_NL table (shard 2 holds nothing else, so
    its rows end the file). A wrong order or offset gives ~1.4 (unrelated rows); quantization alone ~0.1."""
    b_start = bf16.stat().st_size - ROWS * DIM * 2
    q_start = iq4.stat().st_size - ROWS * 90
    if b_start <= 0 or q_start < 0:
        raise RuntimeError("unexpected table sizes")
    errors = []
    rng = random.Random(1)
    with open(bf16, "rb") as fb, open(iq4, "rb") as fq:
        for _ in range(samples):
            row = rng.randrange(ROWS)
            fb.seek(b_start + row * DIM * 2)
            fq.seek(q_start + row * 90)
            a, q = bf16_row(fb.read(DIM * 2)), iq4_nl_row(fq.read(90))
            norm = sum(x * x for x in a) ** 0.5
            if norm > 0:
                errors.append(sum((x - y) ** 2 for x, y in zip(a, q)) ** 0.5 / norm)
    errors.sort()
    median = errors[len(errors) // 2]
    if median > 0.35:
        raise RuntimeError(f"the BF16 table does not match the installed one (median row error {median:.2f})")
    return median
