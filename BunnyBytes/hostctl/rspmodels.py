"""Music covers (RSP): every model file a cover loads, for Models -> Manager -> Core Backend.

RSP (Music/comfy_nodes/RSP) sings a cover with the YuE2-ComfyUI pack and converts the voice with
Seed-VC. Music asks both with downloads off, so a missing file fails the cover instead of starting
a several-GB download mid-render. This module lists those files, finds the ones already on disk
(any ComfyUI model folder, extra_model_paths.yaml included) and downloads the rest:

- each file at a pinned revision, resumed from <name>.part, checked against its SHA-256 before it
  is renamed into place, after a free-space check;
- into the folders the packs read: models/checkpoints, models/audio_encoders, models/YuE2, and
  openai-whisper's own cache for BunnyTranscribe (RSP's Whisper lyrics option).

Seed-VC's weights are not listed here: RSP's install.sh fetches them into models/RSP/seed-vc with
Seed-VC's own downloader, and seedvc_ready() reports them. Stdlib + PyYAML only (hostctl).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
import urllib.request
from pathlib import Path

import yaml

HF = "https://huggingface.co"
WHISPER_URL = ("https://openaipublic.azureedge.net/main/whisper/models/"
               "e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb/large-v3.pt")
YUE2 = ("Comfy-Org/YuE2", "2f76ca75e6ee094169de899cc7fc99d6887e2196")
ASR = ("Qwen/Qwen3-ASR-1.7B-hf", "bcd2b5b7f32b480ab5790554cfa8347f246a14f3")
VOCALS = ("KimberleyJSN/melbandroformer", "ac9b0614ab3cd7f77219e18ba494dfd93956c348")  # the revision YuE2-ComfyUI pins

# id, label, what uses it, ComfyUI model folder ("" = openai-whisper's cache), path inside it,
# (repo, revision) or None, path in the repo or a full URL, bytes, sha256.
MODELS = [
    ("yue2", "YuE2 3B (INT8)", "sings the cover", "checkpoints", "yue2_3b_int8_convrot.safetensors",
     YUE2, "checkpoints/yue2_3b_int8_convrot.safetensors", 3960938800,
     "96fe199377309001ed8cd26a944baeee8cc31a20ba7c36d1d3c0a7e1f4149db6"),
    ("sheetsage", "SheetSage2", "hears the song's melody and chords", "audio_encoders", "sheetsage2_bf16.safetensors",
     YUE2, "audio_encoders/sheetsage2_bf16.safetensors", 1386868122,
     "5fd960ce3df281e3f3a889d174584d88f96247711480cf96377b12d7e8b6adc5"),
    ("vocals", "Mel-Band RoFormer", "splits vocals from the band", "YuE2", "MelBandRoformer.ckpt",
     VOCALS, "MelBandRoformer.ckpt", 913106900,
     "87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e"),
    ("asr", "Qwen3-ASR 1.7B", "recognises the words when lyrics are empty", "YuE2", "Qwen3-ASR-1.7B/model.safetensors",
     ASR, "model.safetensors", 4076193080,
     "2db53c7d81bd9b8cbc6a074e89be2c968a0d373fb4ee68bb1b1e14f7042dfee1"),
    ("asr-tokenizer", "Qwen3-ASR tokenizer", "recognises the words when lyrics are empty", "YuE2",
     "Qwen3-ASR-1.7B/tokenizer.json", ASR, "tokenizer.json", 11429653,
     "fe1fad59be22a41ee293363fcf95fdedbc7c93f3b49270b1d2e18bd1399a7a05"),
    ("asr-config", "Qwen3-ASR config", "recognises the words when lyrics are empty", "YuE2",
     "Qwen3-ASR-1.7B/config.json", ASR, "config.json", 2399,
     "117ac8e63e2af7cae3665e5a632d6eb03f5f384915519ceb6403c15ec6533f63"),
    ("whisper", "Whisper large-v3", "the Whisper lyrics option (BunnyTranscribe)", "", "large-v3.pt",
     None, WHISPER_URL, 3087371615,
     "e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb"),
]
# What install.sh's Seed-VC prefetch leaves in models/RSP/seed-vc (Hugging Face cache layout).
SEEDVC_FILES = ["models--Plachta--Seed-VC", "models--lj1995--VoiceConversionWebUI", "models--funasr--campplus",
                "hf_cache/models--nvidia--bigvgan_v2_44khz_128band_512x", "hf_cache/models--openai--whisper-small"]
SEEDVC_BYTES = 1_700_000_000  # about what the prefetch downloads, for the size shown before it runs
CHUNK = 4 * 1024 * 1024
SPARE = 1024 ** 3  # keep 1 GB free after the downloads


def whisper_dir() -> Path:
    """Where openai-whisper's load_model() looks by default (the ComfyUI process runs as this user)."""
    return Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache") / "whisper"


def model_roots(comfy: Path, folder: str) -> list[Path]:
    """Every directory ComfyUI reads `folder` from: models/<folder> first, then extra_model_paths.yaml."""
    if not folder:
        return [whisper_dir()]
    roots = [comfy / "models" / folder]
    try:
        config = yaml.safe_load((comfy / "extra_model_paths.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        config = {}
    for section in config.values() if isinstance(config, dict) else []:
        if not isinstance(section, dict) or folder not in section:
            continue
        base = Path(os.path.expanduser(str(section.get("base_path") or comfy)))
        for line in str(section[folder] or "").splitlines():
            if line.strip():
                path = Path(os.path.expanduser(line.strip()))
                roots.append(path if path.is_absolute() else base / path)
    return list(dict.fromkeys(roots))


def find(comfy: Path, folder: str, rel: str, size: int) -> Path | None:
    """The file ComfyUI would load, when one of the right size is there."""
    for root in model_roots(comfy, folder):
        path = root / rel
        try:
            if path.is_file() and path.stat().st_size == size:
                return path
        except OSError:
            continue
    return None


def seedvc_ready(comfy: Path) -> bool:
    folder = comfy / "models" / "RSP" / "seed-vc"
    return all(any((folder / name / "snapshots").glob("*/*")) for name in SEEDVC_FILES)


def status(comfy: Path) -> dict:
    files = []
    for key, label, use, folder, rel, _repo, _source, size, _sha in MODELS:
        found = find(comfy, folder, rel, size)
        files.append({"id": key, "label": label, "use": use, "size": size, "on_disk": bool(found),
                      "path": str(found or model_roots(comfy, folder)[0] / rel)})
    seedvc = seedvc_ready(comfy)
    files.append({"id": "seedvc", "label": "Seed-VC weights", "use": "converts the voice", "size": SEEDVC_BYTES,
                  "on_disk": seedvc, "path": str(comfy / "models" / "RSP" / "seed-vc")})
    return {"files": files, "missing_bytes": sum(f["size"] for f in files if not f["on_disk"]),
            "ready": all(f["on_disk"] for f in files)}


def url_for(repo, source: str) -> str:
    return source if repo is None else f"{HF}/{repo[0]}/resolve/{repo[1]}/{source}"


def download(url: str, dest: Path, size: int, sha256: str, report=None) -> None:
    """dest via dest.part (resumed), checked against size and sha256 before it is renamed into place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    have = part.stat().st_size if part.is_file() else 0
    if have > size:
        part.unlink()
        have = 0
    with part.open("ab") as out:
        if have:
            with part.open("rb") as previous:
                for block in iter(lambda: previous.read(CHUNK), b""):
                    digest.update(block)
        if have < size:
            headers = {"User-Agent": "Rocsy-CoreBackend"}
            if have:
                headers["Range"] = f"bytes={have}-"
            token = os.getenv("HF_TOKEN")
            if token and url.startswith(HF):
                headers["Authorization"] = f"Bearer {token}"
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                if have and response.status != 206:  # the server ignored the range: start again
                    out.seek(0)
                    out.truncate()
                    digest, have = hashlib.sha256(), 0
                last = 0.0
                for block in iter(lambda: response.read(CHUNK), b""):
                    out.write(block)
                    digest.update(block)
                    have += len(block)
                    if report and time.monotonic() - last > 2:
                        report(have)
                        last = time.monotonic()
    if have != size:
        raise RuntimeError(f"{dest.name}: got {have} of {size} bytes; run it again to resume")
    if digest.hexdigest() != sha256:
        part.unlink()
        raise RuntimeError(f"{dest.name}: SHA-256 mismatch, the download was not the published file (removed)")
    os.replace(part, dest)
    if report:
        report(size)


def download_missing(comfy: Path, output, progress=None) -> list[str]:
    """Download every listed file that isn't on disk. Returns problems (empty = all in place)."""
    todo = [m for m in MODELS if not find(comfy, m[3], m[4], m[7])]
    if not todo:
        output.write("Every RSP model file is already on disk.\n")
        return []
    problems = []
    for key, label, _use, folder, rel, repo, source, size, sha in todo:
        dest = model_roots(comfy, folder)[0] / rel
        part = dest.with_name(dest.name + ".part")
        left = size - (part.stat().st_size if part.is_file() else 0)
        dest.parent.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(dest.parent).free
        if free < left + SPARE:
            problems.append(f"{label}: needs {left / 1e9:.1f} GB in {dest.parent}, only {free / 1e9:.1f} GB is free")
            output.write(problems[-1] + "\n")
            continue
        url = url_for(repo, source)
        output.write(f"\n=== {label} ({size / 1e9:.2f} GB) ===\n{url}\n  -> {dest}\n")
        output.flush()

        def report(done, label=label, size=size):
            output.write(f"  {done / 1e9:.2f} / {size / 1e9:.2f} GB\n")
            output.flush()
            if progress:
                progress(label, done, size)
        for attempt in range(1, 4):
            try:
                download(url, dest, size, sha, report)
                output.write(f"  checked (SHA-256) and in place\n")
                break
            except Exception as exc:  # noqa: BLE001 - network errors of every kind: retry, then report
                output.write(f"  {exc}\n")
                if attempt == 3:
                    problems.append(f"{label}: {exc}")
                else:
                    output.write(f"  retrying ({attempt + 1}/3)\n")
                    time.sleep(5 * attempt)
    return problems
