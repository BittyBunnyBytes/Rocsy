from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import shlex
import shutil
import signal
import struct
import time
from urllib.parse import quote
import uuid
import zipfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from datasets_api import build_router as build_datasets_router
from finder import build_router as build_finder_router
from progress import parse_progress
from push import PushService
from rules_api import build_router as build_rules_router
from tagger import build_router as build_tagger_router, vision_models
from store import TrainerStore, utc_now
import families

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bunny-trainer")

PREFIX = os.getenv("TRAINER_MOUNT_PREFIX", "/Trainer").rstrip("/")
DATA_DIR = Path(os.getenv("TRAINER_DATA_DIR", "/data")).resolve()
DATASET_ROOT = Path(os.getenv("TRAINER_DATASET_ROOT", "/datasets")).resolve()
MODEL_ROOT = Path(os.getenv("TRAINER_MODEL_ROOT", "/models")).resolve()
TRAINER_ROOT = Path(os.getenv("TRAINER_REPO", "/opt/trainers")).resolve()
FIZGIG_LATEST = TRAINER_ROOT / "vendor" / "Fizgig-latest"  # up-to-date checkout (v8.7.9) for MOSS-Image and Qwen Image 2.1
QUEUE_URL = os.getenv("TRAINER_QUEUE_URL", "http://127.0.0.1:8600").rstrip("/")
H3_GALLERY_URL = os.getenv("TRAINER_H3_GALLERY_URL", "http://127.0.0.1:8000/Video").rstrip("/")
QUEUE_SECRET = os.getenv("TRAINER_QUEUE_INTERNAL_SECRET", "")
LOCK_HEADERS = {"X-Queue-Internal-Secret": QUEUE_SECRET}
BASE_DIR = Path(__file__).resolve().parent
store = TrainerStore(DATA_DIR / "trainer.sqlite3")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
push = PushService(DATA_DIR)

TERMINAL = {"complete", "failed", "cancelled"}
active_processes: dict[str, asyncio.subprocess.Process] = {}
pending_specs: dict[str, dict[str, Any]] = {}
job_tasks: set[asyncio.Task[Any]] = set()
setup_tasks: dict[str, asyncio.Task[Any]] = {}
setup_state: dict[str, dict[str, Any]] = {}
state_guard = asyncio.Lock()


def p(path: str) -> str:
    return f"{PREFIX}{path}"


class JobRequest(BaseModel):
    model: str = Field(min_length=1, max_length=80)
    dataset: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=64)
    rank: int = Field(default=16, ge=1, le=128)
    epochs: int = Field(default=10, ge=1, le=500)
    dit: str = ""
    vae: str = ""
    text_encoder: str = ""
    audio_vae: str = ""
    training_adapter: str = ""
    # Model files for slots beyond the five above (clip_l, t5xxl, ae, gemma2, byt5, ...), keyed by slot.
    files: dict[str, str] = Field(default_factory=dict)
    max_pixels: int = Field(default=655360, ge=65536, le=2097152)
    steps: int = Field(default=2000, ge=1, le=100000)
    resolution: int = Field(default=512, ge=256, le=1536)
    frames: int = Field(default=25, ge=9, le=121)
    repeats: int = Field(default=1, ge=1, le=100)
    learning_rate: str = Field(default="", max_length=16)
    trigger: str = Field(default="", max_length=64)

    def file(self, key: str) -> str:
        return (getattr(self, key, "") if key in {"dit", "vae", "text_encoder", "audio_vae", "training_adapter"} else "") or self.files.get(key, "")


def _family(model: str) -> families.Family:
    fam = families.get(model)
    if not fam:
        raise HTTPException(422, f"Unknown model family {model!r}.")
    return fam


def _lr(req: JobRequest, default: str) -> str:
    value = (req.learning_rate or "").strip() or default
    try:
        if not 0 < float(value) < 1:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "Learning rate must be a number like 1e-4.") from None
    return value


def _dataset(value: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise HTTPException(422, "Dataset name may contain only letters, numbers, _ and -.")
    target = (DATASET_ROOT / value).resolve()
    if not target.is_relative_to(DATASET_ROOT) or not target.is_dir():
        raise HTTPException(404, "Dataset folder was not found under the Trainer datasets directory.")
    return target


def _model_file(value: str, label: str, *, optional: bool = False) -> str | None:
    if not value.strip():
        if optional:
            return None
        raise HTTPException(422, f"Select a {label} model file under /models.")
    raw = Path(value.strip())
    target = (raw if raw.is_absolute() else MODEL_ROOT / raw).resolve()
    if not target.is_relative_to(MODEL_ROOT) or not target.is_file():
        raise HTTPException(422, f"The selected {label} file must exist under /models.")
    return str(target)


def _slot_files(req: JobRequest, fam: families.Family) -> dict[str, str | None]:
    """Every slot's chosen file, resolved under /models; a base model the header check calls
    unsupported is refused with its reason rather than failing an hour into the run."""
    out: dict[str, str | None] = {}
    for slot in fam.slots:
        path = _model_file(req.file(slot.key), f"{fam.title} {slot.label}", optional=not slot.required)
        if path and (slot.base or slot.check):
            rel = str(Path(path).relative_to(MODEL_ROOT))
            if slot.base:
                info = families.classify(Path(path), rel)
                verdict, reason = info["verdict"], info["reason"]
                if info["family"] not in (None, fam.id):
                    verdict, reason = families.UNSUPPORTED, f"that file is for {families.title(info['family'])}"
            else:
                verdict, reason = slot.check(families.read_header(Path(path)), rel)
            if verdict == families.UNSUPPORTED:
                raise HTTPException(422, f"{fam.title} can't train with {Path(path).name}: {reason or 'unsupported format'}.")
        out[slot.key] = path
    return out


def _safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_-")
    if not name:
        raise HTTPException(422, "Give this LoRA a name containing at least one letter or number.")
    return name[:64]


def _plan_note(plan: dict[str, Any]) -> str:
    c = plan["counts"]
    lines = [f"[Trainer] Training on {len(plan['included'])} item(s): {c['video']} clip(s), {c['image']} image(s), {c['audio']} audio."]
    if plan["excluded"]:
        lines.append(f"[Trainer] Left out {len(plan['excluded'])} item(s) (they stay in the dataset):")
        lines += [f"  - {e['file']}: {e['reason']}" for e in plan["excluded"]]
    lines += [f"[Trainer] Note: {w}" for w in plan["warnings"]]
    return "\n".join(lines) + "\n\n"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _make_fizgig_config(dataset: Path, job_id: str, res: int) -> Path:
    cache_dir = DATA_DIR / "cache" / job_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / "configs" / f"{job_id}.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "[general]\n"
        f"resolution = [{res}, {res}]\n"
        'caption_extension = ".txt"\n'
        "batch_size = 1\nnum_repeats = 1\nenable_bucket = true\nbucket_no_upscale = true\n\n"
        "[[datasets]]\n"
        f"image_directory = {_toml_string(str(dataset))}\n"
        f"cache_directory = {_toml_string(str(cache_dir))}\n",
        encoding="utf-8",
    )
    return target


def _make_sdscripts_config(dataset: Path, job_id: str, req: JobRequest, *, shuffle: bool) -> Path:
    target = DATA_DIR / "configs" / f"{job_id}.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    lo = max(256, (req.resolution // 2) // 64 * 64)
    target.write_text(
        "[general]\n"
        'caption_extension = ".txt"\n'
        "enable_bucket = true\nbucket_no_upscale = true\nbucket_reso_steps = 64\n"
        f"min_bucket_reso = {lo}\nmax_bucket_reso = {min(2048, req.resolution * 3 // 2 // 64 * 64)}\n\n"
        "[[datasets]]\n"
        f"resolution = {req.resolution}\nbatch_size = 1\n\n"
        "  [[datasets.subsets]]\n"
        f"  image_dir = {_toml_string(str(dataset))}\n"
        f"  num_repeats = {req.repeats}\n"
        f"  shuffle_caption = {str(shuffle).lower()}\n"
        f"  keep_tokens = {1 if shuffle else 0}\n"
        "  flip_aug = false\n",
        encoding="utf-8",
    )
    return target


def _make_ltx_config(dataset: Path, job_id: str, request: JobRequest) -> Path:
    starter = TRAINER_ROOT / "configs" / "ltx25_video_lora.yaml"
    if not starter.is_file():
        raise HTTPException(503, "The LTX 2.5 starter config is missing from Model Trainers.")
    config = yaml.safe_load(starter.read_text(encoding="utf-8"))
    proc = config["config"]["process"][0]
    proc["training_folder"] = str(DATA_DIR / "output" / job_id)
    proc["network"]["linear"] = request.rank
    proc["network"]["linear_alpha"] = request.rank
    proc["save"]["save_every"] = max(100, min(request.steps, 500))
    proc["datasets"][0]["folder_path"] = str(dataset)
    proc["datasets"][0]["resolution"] = [request.resolution, request.resolution]
    proc["datasets"][0]["num_frames"] = request.frames
    proc["train"]["steps"] = request.steps
    proc["sample"]["width"] = request.resolution
    proc["sample"]["height"] = request.resolution
    proc["sample"]["num_frames"] = request.frames
    target = DATA_DIR / "configs" / f"{job_id}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return target


def _make_aitk_config(dataset: Path, job_id: str, req: JobRequest, fam: families.Family) -> Path:
    """An upstream AI Toolkit example with only the per-run parts changed (paths, rank, steps, size)."""
    config = yaml.safe_load(Path(fam.extra["example"]).read_text(encoding="utf-8"))
    config["config"]["name"] = _safe_name(req.name)
    proc = config["config"]["process"][0]
    proc["training_folder"] = str(DATA_DIR / "output" / job_id)
    proc["device"] = "cuda:0"
    proc.pop("performance_log_every", None)
    if "network" in proc:
        proc["network"]["linear"] = req.rank
        proc["network"]["linear_alpha"] = req.rank
    proc.setdefault("save", {})["save_every"] = max(100, min(req.steps, 500))
    for ds in proc.get("datasets") or []:
        ds["folder_path"] = str(dataset)
        ds["resolution"] = [req.resolution]
        if "num_frames" in ds and "frames" in fam.knobs:
            ds["num_frames"] = req.frames
    proc.setdefault("train", {})["steps"] = req.steps
    if req.learning_rate.strip():
        proc["train"]["lr"] = float(_lr(req, "1e-4"))
    if isinstance(proc.get("sample"), dict):
        proc["sample"]["sample_every"] = max(req.steps, 250)  # previews only at the end; they cost VRAM and time
    target = DATA_DIR / "configs" / f"{job_id}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return target


def _assert_env(fam: families.Family) -> Path:
    python = families.backend_python(fam.backend)
    if not python.is_file():
        raise HTTPException(503, f"{fam.title} trains with {families.BACKENDS[fam.backend]['title']}, which is not installed. Use its Install button first.")
    if fam.backend == families.SDS and not families.SDSCRIPTS_DIR.is_dir():
        raise HTTPException(503, f"sd-scripts was not found at {families.SDSCRIPTS_DIR}.")
    return python


def _fizgig_spec(req: JobRequest, fam: families.Family, files: dict[str, str | None], plan: dict[str, Any],
                 trainset: Path, job_id: str, name: str) -> list[str]:
    root = TRAINER_ROOT / "vendor" / "Fizgig"
    py = str(families.backend_python(families.FIZGIG))
    out = str(DATA_DIR / "output" / job_id)
    dit, vae, encoder = files["dit"], files["vae"], files["text_encoder"]
    if fam.id == "krea2":
        config = _make_fizgig_config(trainset, job_id, 768)
        train = [
            "--dataset_config", str(config), "--dit", dit,
            "--output_dir", out, "--output_name", name,
            "--network_dim", str(req.rank), "--network_alpha", str(req.rank),
            "--learning_rate", _lr(req, "1e-4"), "--max_train_epochs", str(req.epochs),
            "--save_every_n_epochs", "1", "--quantize_4bit", "--seed", "42",
        ]
        if req.trigger.strip():
            train += ["--metadata_trigger_phrase", req.trigger.strip()]
        return [
            "__fizgig__", py, str(root / "src/fizgig/scripts/krea2_cache_latents.py"),
            "--dataset_config", str(config), "--vae", vae, "--skip_existing",
            "&&", py, str(root / "src/fizgig/scripts/krea2_cache_text.py"),
            "--dataset_config", str(config), "--text_encoder", encoder, "--skip_existing",
            "&&", py, str(root / "src/fizgig/scripts/krea2_train.py"), *train,
        ]
    if fam.id == "qwen_image_21":
        # Fizgig's family driver, the GUI's "Standard" preset: rank 16, adaptive LR 1e-4..2e-4 (Fast: rank 8,
        # 2e-4..4e-4), EMA 0.98, the Fizgig training adapter when present, auto precision/block swap. Runs from
        # vendor/Fizgig-latest so the SDPA shim applies: the Qwen3-VL encoder's masked attention otherwise lands on
        # an AOTriton kernel that fails on gfx1100 with hipErrorInvalidValue.
        res = max(256, min(1536, round(req.max_pixels ** 0.5 / 32) * 32))
        config = _make_fizgig_config(trainset, job_id, res)
        lr = _lr(req, "1e-4")
        train = [
            "--family", "qwen_image21", "--dataset_config", str(config), "--dit", dit,
            "--text_encoder", encoder,   # the loss watch re-encodes repaired captions
            "--output_dir", out, "--output_name", name,
            "--network_dim", str(req.rank), "--network_alpha", str(req.rank),
            "--learning_rate", lr, "--adaptive_lr", "--adaptive_lr_min", lr, "--adaptive_lr_max", f"{float(lr) * 2:g}",
            "--max_train_epochs", str(req.epochs), "--save_every_n_epochs", "1", "--seed", "42",
            "--optimizer_type", "adamw8bit", "--precision", "auto", "--blocks_to_swap", "-1",
            "--compile_blocks", "off", "--ema_decay", "0.98", "--log_per_image_loss", "--save_state_on_train_end",
        ]
        if files.get("training_adapter"):
            train += ["--training_adapter", files["training_adapter"]]
        if req.trigger.strip():
            train += ["--metadata_trigger_phrase", req.trigger.strip()]
        fam_py = str(FIZGIG_LATEST / "src/fizgig/families")
        return [
            "__fizgig__", py, f"{fam_py}/cache.py", "--family", "qwen_image21", "--stage", "latents",
            "--dataset_config", str(config), "--model", vae, "--skip_existing",
            "&&", py, f"{fam_py}/cache.py", "--family", "qwen_image21", "--stage", "text",
            "--dataset_config", str(config), "--model", encoder, "--skip_existing",
            "&&", py, f"{fam_py}/train.py", *train,
        ]
    if fam.id == "moss_image":
        # Fizgig's "MOSS-Image Standard" preset (families/moss_image.py): fixed-LR AdamW, EMA 0.98, auto
        # INT8/NF4 base, every epoch saved, Clean-end share 30% (v8.7.0's training fix: the reference schedule
        # puts ~11% of steps at the clean end, so likeness came slowly and drifted). Only the up-to-date checkout
        # has MOSS-Image, so it runs from vendor/Fizgig-latest with the H3 checkout's venv (same requirements).
        res = max(256, min(2048, round(req.max_pixels ** 0.5 / 16) * 16))
        config = _make_fizgig_config(trainset, job_id, res)
        train = [
            "--family", "moss_image", "--dataset_config", str(config), "--dit", dit,
            "--vae", vae, "--text_encoder", encoder,
            "--output_dir", out, "--output_name", name,
            "--network_dim", str(req.rank), "--network_alpha", str(req.rank),
            "--learning_rate", _lr(req, "1e-4"), "--max_train_epochs", str(req.epochs),
            "--save_every_n_epochs", "1", "--seed", "42",
            "--optimizer_type", "adamw", "--precision", "auto", "--blocks_to_swap", "-1",
            # torch.compile of the blocks is untested on this ROCm wheel; flash_attn comes from the SDPA shim.
            "--compile_blocks", "off", "--ema_decay", "0.98", "--save_state_on_train_end",
            "--family_option", "lownoise_pct=30",
        ]
        if "sft" in Path(dit).name.lower():
            train += ["--family_option", "lora_sft=1"]  # SFT base: attention-only LoRA
        if req.trigger.strip():
            train += ["--metadata_trigger_phrase", req.trigger.strip()]
        fam_py = str(FIZGIG_LATEST / "src/fizgig/families")
        return [
            "__fizgig__", py, f"{fam_py}/cache.py", "--family", "moss_image", "--stage", "latents",
            "--dataset_config", str(config), "--model", vae, "--skip_existing",
            "&&", py, f"{fam_py}/cache.py", "--family", "moss_image", "--stage", "text",
            "--dataset_config", str(config), "--model", encoder, "--skip_existing",
            "&&", py, f"{fam_py}/train.py", *train,
        ]
    if fam.id == "klein9b":
        config = _make_fizgig_config(trainset, job_id, 1024)
        train = [
            "--dataset_config", str(config), "--dit", dit, "--vae", vae, "--text_encoder", encoder,
            "--sdpa", "--mixed_precision", "bf16", "--fp8_base", "--fp8_scaled", "--gradient_checkpointing",
            "--blocks_to_swap", "0", "--optimizer_type", "adamw8bit", "--learning_rate", _lr(req, "1e-4"),
            "--network_module", "fizgig.networks.lora_klein",
            "--network_dim", str(req.rank), "--network_alpha", str(req.rank),
            "--timestep_sampling", "flux2_shift", "--max_train_epochs", str(req.epochs),
            "--save_every_n_epochs", "1", "--seed", "42", "--output_dir", out, "--output_name", name,
            "--adaptive_lr", "--adaptive_lr_min", "5e-5", "--adaptive_lr_max", "4e-4",
        ]
        if req.trigger.strip():
            train += ["--metadata_trigger_phrase", req.trigger.strip()]
        return [
            "__fizgig__", py, str(root / "src/fizgig/scripts/cache_latents.py"),
            "--dataset_config", str(config), "--vae", vae, "--skip_existing",
            "&&", py, str(root / "src/fizgig/scripts/cache_text.py"),
            "--dataset_config", str(config), "--text_encoder", encoder, "--skip_existing",
            "&&", py, str(root / "src/fizgig/scripts/train.py"), *train,
        ]
    # MiniMax H3
    if plan["counts"]["audio"] and not files.get("audio_vae"):
        raise HTTPException(422, "Select the MiniMax H3 audio VAE when training on audio recordings.")
    config = _make_fizgig_config(trainset, job_id, req.resolution)
    cache = ["--dataset_config", str(config), "--vae", vae, "--clip_still", "--skip_existing"]
    if files.get("audio_vae"):
        cache += ["--audio_vae", files["audio_vae"]]
    train = [
        "--dataset_config", str(config), "--dit", dit,
        "--output_dir", out, "--output_name", name,
        "--network_dim", str(req.rank), "--network_alpha", str(req.rank),
        # Fizgig's shipped H3 recipe: Automagic v3 sets its own rate from a 1e-6
        # start (a flat 2e-4 on adamw8bit overshoots; Fizgig measured 1e-4 as
        # destructive), EMA 0.98 checkpoints, and TREAD routing on clip steps.
        "--optimizer_type", "automagic3", "--learning_rate", "1e-6",
        "--ema_decay", "0.98", "--caption_dropout", "0.05",
        "--tread_ratio", "0.5", "--tread_start", "2", "--tread_end", "47",
        "--max_train_epochs", str(req.epochs),
        "--save_every_n_epochs", "1", "--base_quant", "auto",
        "--blocks_to_swap", "auto", "--gradient_checkpointing", "auto",
        "--shift", "0.666667", "--photo_blocks", "20-49",
        "--clip_blocks", "20-49", "--audio_blocks", "20-49",
        "--no_train_adaln", "--clip_still_as_photo", "--seed", "42",
        "--save_state_on_train_end",
    ]
    if req.trigger.strip():
        train += ["--metadata_trigger_phrase", req.trigger.strip()]
    if files.get("training_adapter"):
        train += ["--training_adapter_path", files["training_adapter"]]
    return [
        "__fizgig__", py, str(root / "src/fizgig/scripts/minimax_cache_latents.py"), *cache,
        "&&", py, str(root / "src/fizgig/scripts/minimax_cache_text.py"),
        "--dataset_config", str(config), "--text_encoder", encoder,
        "&&", py, str(root / "src/fizgig/scripts/minimax_train.py"), *train,
    ]


# sd-scripts entry point, network module and model-file flags per family.
SDS_RECIPES: dict[str, dict[str, Any]] = {
    "anima": {"script": "anima_train_network.py", "module": "networks.lora_anima",
              "files": {"dit": "--pretrained_model_name_or_path", "text_encoder": "--qwen3", "vae": "--vae"},
              "args": ["--attn_mode=torch", "--qwen3_max_token_length=256", "--t5_max_token_length=256",
                       "--timestep_sampling=sigmoid", "--sigmoid_scale=1.0", "--loss_type=l2", "--qwen_image_vae_2d",
                       "--network_train_unet_only", "--network_dropout=0.05"],
              # The tested character run: AdamW8bit, warmup, shuffled tags keeping the trigger first.
              "optimizer": ["--optimizer_type=AdamW8bit", "--lr_scheduler=constant_with_warmup", "--lr_warmup_steps=50"],
              "shuffle": True, "cache_text": False},
    "sdxl": {"script": "sdxl_train_network.py", "module": "networks.lora", "files": {"dit": "--pretrained_model_name_or_path"},
             "args": ["--sdpa", "--network_train_unet_only", "--no_half_vae"], "cache_text": True},
    "sd15": {"script": "train_network.py", "module": "networks.lora", "files": {"dit": "--pretrained_model_name_or_path"},
             "args": ["--sdpa"], "cache_text": False},
    "flux1": {"script": "flux_train_network.py", "module": "networks.lora_flux",
              "files": {"dit": "--pretrained_model_name_or_path", "clip_l": "--clip_l", "t5xxl": "--t5xxl", "ae": "--ae"},
              "args": ["--sdpa", "--network_train_unet_only", "--fp8_base", "--blocks_to_swap=18", "--guidance_scale=1.0",
                       "--timestep_sampling=flux_shift", "--model_prediction_type=raw"], "cache_text": True},
    "sd3": {"script": "sd3_train_network.py", "module": "networks.lora_sd3",
            "files": {"dit": "--pretrained_model_name_or_path", "clip_l": "--clip_l", "clip_g": "--clip_g", "t5xxl": "--t5xxl"},
            "args": ["--sdpa", "--network_train_unet_only", "--blocks_to_swap=16", "--weighting_scheme=uniform"], "cache_text": True},
    "lumina2": {"script": "lumina_train_network.py", "module": "networks.lora_lumina",
                "files": {"dit": "--pretrained_model_name_or_path", "gemma2": "--gemma2", "ae": "--ae"},
                "args": ["--timestep_sampling=nextdit_shift", "--discrete_flow_shift=6.0", "--model_prediction_type=raw",
                         "--system_prompt=You are an assistant designed to generate high-quality images based on user prompts."],
                "cache_text": True},
    "hunyuan_image21": {"script": "hunyuan_image_train_network.py", "module": "networks.lora_hunyuan_image",
                        "files": {"dit": "--pretrained_model_name_or_path", "text_encoder": "--text_encoder", "byt5": "--byt5", "vae": "--vae"},
                        "args": ["--attn_mode=torch", "--split_attn", "--network_train_unet_only", "--model_prediction_type=raw",
                                 "--discrete_flow_shift=5.0", "--blocks_to_swap=18"], "cache_text": True},
}


def _sdscripts_spec(req: JobRequest, fam: families.Family, files: dict[str, str | None], trainset: Path,
                    job_id: str, name: str) -> tuple[list[str], dict[str, str]]:
    recipe = SDS_RECIPES[fam.id]
    config = _make_sdscripts_config(trainset, job_id, req, shuffle=recipe.get("shuffle", False))
    alpha = max(1, round(req.rank * fam.extra.get("alpha_ratio", 0.5)))
    out = DATA_DIR / "output" / job_id
    command = [
        str(families.backend_python(families.SDS)), "-m", "accelerate.commands.launch",
        "--num_cpu_threads_per_process=2", "--num_processes=1", "--num_machines=1", "--dynamo_backend=no",
        "--mixed_precision=bf16", recipe["script"],
    ]
    for key, flag in recipe["files"].items():
        if files.get(key):
            command.append(f"{flag}={files[key]}")
    chroma = fam.id == "flux1" and families.classify(Path(files["dit"]), "")["variant"] == "Chroma"
    if fam.id == "flux1" and not chroma and not files.get("clip_l"):
        raise HTTPException(422, "Flux.1 needs a CLIP-L text encoder (only Chroma trains without one).")
    command += [
        f"--dataset_config={config}", f"--output_dir={out}", f"--output_name={name}",
        f"--logging_dir={out / 'logs'}", "--save_model_as=safetensors", "--save_precision=bf16",
        f"--save_every_n_epochs={max(1, req.epochs // 6)}", f"--max_train_epochs={req.epochs}",
        f"--network_module={recipe['module']}", f"--network_dim={req.rank}", f"--network_alpha={alpha}",
        f"--learning_rate={_lr(req, str(fam.defaults.get('learning_rate', '1e-4')))}",
        *recipe.get("optimizer", ["--optimizer_type=AdamW8bit", "--lr_scheduler=constant"]),
        "--max_grad_norm=1.0", "--mixed_precision=bf16", "--gradient_checkpointing",
        "--cache_latents", "--cache_latents_to_disk", "--max_data_loader_n_workers=2",
        "--persistent_data_loader_workers", "--seed=42", *recipe["args"],
    ]
    if recipe.get("cache_text"):
        command += ["--cache_text_encoder_outputs", "--cache_text_encoder_outputs_to_disk"]
    if chroma:  # sd-scripts' Chroma mode: no CLIP-L, no guidance embedding, sigmoid timesteps
        command = [c for c in command if not c.startswith(("--clip_l=", "--guidance_scale", "--timestep_sampling"))]
        command += ["--model_type=chroma", "--apply_t5_attn_mask", "--guidance_scale=0.0", "--timestep_sampling=sigmoid"]
    if req.trigger.strip():
        command.append(f"--training_comment=trigger: {req.trigger.strip()}")
    cache = DATA_DIR / "cache" / "sd-scripts"
    env = {"HF_HOME": str(DATA_DIR / "hf-cache"), "XDG_CACHE_HOME": str(cache), "TORCH_HOME": str(cache / "torch"),
           "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    return command, env


def _prepare_spec(req: JobRequest, job_id: str) -> dict[str, Any]:
    fam = _family(req.model)
    _dataset(req.dataset)
    name = _safe_name(req.name)
    _assert_env(fam)
    files = _slot_files(req, fam)
    if fam.id == "ltx25" and req.frames % 8 != 1:
        raise HTTPException(422, "LTX frame count must be 8n+1 (for example 25 or 49).")
    # Only captioned, trainer-ready files reach the trainer: uncaptioned clips (and anything it
    # would refuse or silently skip) stay in the dataset but are left out of this run.
    plan = datasets_router.training_plan(req.dataset, req.model, req.trigger)
    if not plan["included"]:
        why = f" ({plan['uncaptioned']} without a caption)" if plan["uncaptioned"] else (
            f": {plan['excluded'][0]['reason']}" if plan["excluded"] else ": the dataset is empty")
        raise HTTPException(422, f"Nothing in this dataset is ready to train for {fam.title} yet" + why + ".")
    trainset = DATA_DIR / "train-sets" / job_id
    datasets_router.build_trainset(req.dataset, plan, trainset)
    spec: dict[str, Any] = {"request": req.model_dump(), "name": name, "env": {}, "note": _plan_note(plan)}
    if fam.backend == families.FIZGIG:
        spec["command"] = _fizgig_spec(req, fam, files, plan, trainset, job_id, name)
    elif fam.backend == families.SDS:
        spec["command"], spec["env"] = _sdscripts_spec(req, fam, files, trainset, job_id, name)
        spec["cwd"] = str(families.SDSCRIPTS_DIR)
    else:
        config = _make_ltx_config(trainset, job_id, req) if fam.id == "ltx25" else _make_aitk_config(trainset, job_id, req, fam)
        py = str(families.backend_python(families.AITK))
        spec["command"] = [str(TRAINER_ROOT / "run.sh"), "ltx", str(config)]
        spec["env"] = {"AI_TOOLKIT_PYTHON": py}
    return spec


_progress_cache: dict[str, tuple[int, str, dict[str, Any]]] = {}


def _job_progress(item: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    key = (len(item["log_text"]), item["state"])
    cached = _progress_cache.get(item["id"])
    if cached and cached[:2] == key:
        return cached[2]
    result = parse_progress(item["log_text"], item["state"], request)
    _progress_cache[item["id"]] = (*key, result)
    return result


def _job_outputs(job_id: str) -> list[dict[str, Any]]:
    folder = DATA_DIR / "output" / job_id
    if not folder.is_dir():
        return []
    files = sorted(folder.rglob("*.safetensors"))
    return [{"name": f.name, "size_mb": round(f.stat().st_size / 1048576, 1),
             "host_path": f"~/AI/Trainer/data/output/{job_id}/{f.relative_to(folder)}"} for f in files]


def _public(item: dict[str, Any]) -> dict[str, Any]:
    request = json.loads(item["request_json"])
    return {
        "id": item["id"], "model": item["model"], "model_title": families.title(item["model"]),
        "name": item["name"], "dataset": item["dataset"], "state": item["state"],
        "stage": item["stage"], "created_at": item["created_at"], "started_at": item["started_at"],
        "completed_at": item["completed_at"], "error": item["error"],
        "request": request, "log": item["log_text"][-20000:],
        "progress": _job_progress(item, request), "outputs": _job_outputs(item["id"]),
    }


MODEL_FILE_EXTS = {".safetensors", ".sft", ".gguf", ".ckpt", ".pt", ".pth", ".bin"}
MODEL_FILE_FOLDERS = {
    "dit": ("diffusion_models", "unet", "checkpoints"),
    "vae": ("vae",),
    "text_encoder": ("text_encoders", "clip"),
    "training_adapter": ("training_adapters", "loras"),
}

# Built-in presets. "dataset", "name" and "trigger" are per run and never stored in a preset.
BUILTIN_PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    "minimax_h3": {
        "Tested: H3 video LoRA (rank 16, 10 epochs)": {
            "rank": 16, "epochs": 10, "resolution": 512,
            "dit": "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot_official.safetensors",
            "vae": "vae/minimax_h3_video_vae_fp16.safetensors",
            "text_encoder": "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
            "audio_vae": "",
            "training_adapter": "training_adapters/minimax_h3_training_adapter_v1.safetensors",
        },
    },
}
BUILTIN_PRESETS["anima"] = {
    # The character preset was tested on a character dataset (epoch 20 of 30 was the best).
    "Tested: Anima character": {
        "rank": 32, "epochs": 30, "resolution": 1024, "learning_rate": "2e-5", "repeats": 1,
        "dit": "diffusion_models/anima_baseV10.safetensors", "text_encoder": "text_encoders/qwen_3_06b_base.safetensors",
        "vae": "vae/qwen_image_vae.safetensors"},
    "Anima style (60-400 images)": {"rank": 32, "epochs": 10, "resolution": 1024, "learning_rate": "2e-5", "repeats": 1},
    "Anima artist (40-100 images)": {"rank": 32, "epochs": 25, "resolution": 1024, "learning_rate": "2e-5", "repeats": 1},
}
# Fizgig v8.7.9's own presets (families/moss_image.py, qwen_image.py). Qwen's adaptive LR runs from the
# learning rate to twice it, so 1e-4 is Standard's 1e-4..2e-4 and 2e-4 is Fast's 2e-4..4e-4.
BUILTIN_PRESETS["moss_image"] = {
    "Fizgig: MOSS-Image Standard (rank 32, 1 MP)": {"rank": 32, "epochs": 70, "max_pixels": 1048576, "learning_rate": "1e-4"},
    "Fizgig: MOSS-Image Fast (rank 16, 0.26 MP)": {"rank": 16, "epochs": 70, "max_pixels": 262144, "learning_rate": "1e-4"},
}
BUILTIN_PRESETS["qwen_image_21"] = {
    "Fizgig: Qwen 2.1 Standard (rank 16, LR 1e-4…2e-4)": {"rank": 16, "epochs": 30, "max_pixels": 524288, "learning_rate": "1e-4"},
    "Fizgig: Qwen 2.1 Fast (rank 8, LR 2e-4…4e-4)": {"rank": 8, "epochs": 30, "max_pixels": 524288, "learning_rate": "2e-4"},
}
SETTING_FIELDS = {"rank", "epochs", "resolution", "frames", "steps", "max_pixels", "repeats", "learning_rate"}
# Model-file slots of every family (dit, vae, text_encoder, clip_l, t5xxl, ae, gemma2, ...).
PRESET_FIELDS = SETTING_FIELDS | {slot.key for fam in families.FAMILIES for slot in fam.slots}
PRESETS_FILE = DATA_DIR / "presets.json"


def _user_presets() -> dict[str, dict[str, dict[str, Any]]]:
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class PresetSave(BaseModel):
    model: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    settings: dict[str, Any]


async def _queue_request(path: str, body: dict[str, Any] | None = None, *, timeout: float = 10.0) -> httpx.Response:
    if not QUEUE_SECRET:
        raise RuntimeError("TRAINER_QUEUE_INTERNAL_SECRET is not configured")
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(QUEUE_URL + path, json=body or {}, headers=LOCK_HEADERS)


async def _lock_heartbeat(token: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
        except asyncio.TimeoutError:
            with suppress(Exception):
                await _queue_request("/heartbeat", {"token": token}, timeout=6)


def _fizgig_command(args: list[str]) -> list[str]:
    root = TRAINER_ROOT / "vendor" / "Fizgig"
    if args[0] != "__fizgig__":
        return args
    raw = args[1:]
    # Each fixed CLI segment is split by a literal && token. `exec` receives
    # an argument vector; dataset/model paths never go through shell parsing.
    steps: list[list[str]] = [[]]
    for value in raw:
        if value == "&&":
            steps.append([])
        else:
            steps[-1].append(value)
    if any(not step for step in steps):
        raise HTTPException(500, "Empty Fizgig training command step.")
    # Families that run from Fizgig-latest (MOSS-Image, Qwen Image 2.1) call plain SDPA; the shim's sitecustomize
    # routes it to flash_attn and keeps masked calls (the text encoders) off the broken AOTriton kernels.
    shim = (" FIZGIG_SDPA_FLASH=1 PYTHONPATH=/opt/trainers/vendor/sdpa_shim:/opt/trainers/vendor/attn_overlay"
            if any(str(FIZGIG_LATEST) in v for v in raw) else "")
    script = (
        f"cd {shlex.quote(str(root))} && source venv/bin/activate && "
        "export FIZGIG_GPU_BACKEND=rocm MIOPEN_FIND_MODE=2 "
        "FLASH_ATTENTION_TRITON_AMD_ENABLE=TRUE FIZGIG_NO_EXPANDABLE=1 "
        "PYTORCH_ALLOC_CONF=max_split_size_mb:512,garbage_collection_threshold:0.8 "
        "PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:512,garbage_collection_threshold:0.8 "
        "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 FIZGIG_VAE_DTYPE=bf16 FIZGIG_TE_NO_FLASH=1 FIZGIG_FLASH_ATTN=1 PYTHONPATH=/opt/trainers/vendor/attn_overlay"
        f"{shim}; "
        "if [ -f rocm_env.sh ]; then source rocm_env.sh; fi; "
    )
    encoded = " && ".join(shlex.join(step) for step in steps)
    script += encoded
    return ["bash", "--noprofile", "--norc", "-c", script]


NAN_LOSS = re.compile(r"avr_loss=(nan|-?inf)\b", re.I)


async def _run_command(job_id: str, command: list[str], env_extra: dict[str, str], stage: str,
                       cwd: str | None = None) -> None:
    store.update(job_id, state="running", stage=stage)
    env = os.environ.copy()
    env.update(env_extra)
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd or str(TRAINER_ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    active_processes[job_id] = proc
    assert proc.stdout is not None
    diverged = None
    while line := await proc.stdout.readline():
        text = line.decode("utf-8", errors="replace")
        store.append_log(job_id, text)
        # A NaN/Inf running loss means the weights are already ruined: every later
        # checkpoint is garbage, so stop now instead of training for hours.
        if diverged is None and (match := NAN_LOSS.search(text)):
            epoch = re.search(r"epoch (\d+)/\d+", text)
            step = re.search(r"(\d+)/\d+ \[", text)    # a progress bar: "331/4690 [31:59<..."
            diverged = f"epoch {epoch.group(1)}" if epoch else f"step {step.group(1)}" if step else "the current epoch"
            store.append_log(job_id, f"\n[Trainer] Loss became {match.group(1)} during {diverged}: training diverged. "
                                     "Stopping; checkpoints saved before this are kept.\n")
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
    code = await proc.wait()
    active_processes.pop(job_id, None)
    item = store.get(job_id)
    if item and item["state"] == "cancelling":
        raise asyncio.CancelledError
    if diverged:
        raise RuntimeError(f"Training diverged: the loss became NaN during {diverged}. Stopped early; "
                           "use a checkpoint from before it, then retrain with a lower learning rate or a checked dataset.")
    if code:
        raise RuntimeError(f"{stage} exited with code {code}; inspect the job log.")


def _took(item: dict[str, Any]) -> str:
    try:
        start = datetime.fromisoformat(item["started_at"])
        end = datetime.fromisoformat(item["completed_at"])
    except (KeyError, TypeError, ValueError):
        return ""
    minutes = max(0, round((end - start).total_seconds() / 60))
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def notify_job(job_id: str) -> None:
    """Web Push when a training run finishes or fails (not on cancel - you did that yourself).
    Sent in the background so a slow push service never holds up the job."""
    item = store.get(job_id)
    if not item or item["state"] not in {"complete", "failed"}:
        return
    title = "LoRA training complete" if item["state"] == "complete" else "LoRA training failed"
    took = _took(item)
    detail = (item.get("error") or "")[:140] if item["state"] == "failed" else f"Dataset {item['dataset']}" + (f" · took {took}" if took else "")
    payload = {"title": title, "body": f"{families.title(item['model'])} · {item['name']}\n{detail}",
               "tag": f"trainer-job-{job_id}", "url": f"{PREFIX}/?job={job_id}"}
    task = asyncio.get_running_loop().create_task(_deliver_push(payload, item["state"]))
    job_tasks.add(task)
    task.add_done_callback(job_tasks.discard)


# BunnyBytes' gateway hub sends every studio's pushes from one subscription per
# device (Settings -> Notifications); without it, push to our own subscribers.
NOTIFY_HUB_URL = os.getenv("TRAINER_NOTIFY_HUB_URL", "http://127.0.0.1:8080/internal/notify")


async def _deliver_push(payload: dict[str, Any], event: str) -> None:
    if NOTIFY_HUB_URL and QUEUE_SECRET:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(NOTIFY_HUB_URL, headers={"x-internal-secret": QUEUE_SECRET},
                                             json={"service": "Trainer", "event": event, "icon": f"{PREFIX}/static/icons/icon-192.png",
                                                   "badge": f"{PREFIX}/static/icons/badge-96.png", **payload})
            if response.status_code == 200:
                return
            log.warning("Notification hub refused %s (%s); pushing directly", event, response.status_code)
        except httpx.HTTPError:
            log.info("Notification hub unreachable; pushing directly")
    await asyncio.to_thread(push.broadcast, payload)


async def _execute_job(job_id: str, token: str, spec: dict[str, Any]) -> None:
    stop_heartbeat = asyncio.Event()
    heartbeat = asyncio.create_task(_lock_heartbeat(token, stop_heartbeat))
    completed = False
    try:
        store.update(job_id, state="preparing", stage="Starting trainer", lock_token=token, started_at=utc_now())
        command = spec["command"]
        if spec.get("note"):
            store.append_log(job_id, spec["note"])
        if command and command[0] == "__fizgig__":
            await _run_command(job_id, _fizgig_command(command), spec["env"], "Caching and training")
        else:
            title = families.title(store.get(job_id)["model"])
            await _run_command(job_id, command, spec["env"], f"Training {title}", spec.get("cwd"))
        gallery_ready = store.get(job_id)["model"] in {"minimax_h3", "ltx25"}
        store.update(job_id, state="complete", stage="Complete · Gallery pending" if gallery_ready else "Complete",
                     completed_at=utc_now(), lock_token=None)
        completed = True
    except asyncio.CancelledError:
        store.update(job_id, state="cancelled", stage="Cancelled", completed_at=utc_now(), lock_token=None)
    except Exception as exc:
        log.exception("Trainer job %s failed", job_id)
        store.append_log(job_id, f"\n[Trainer error] {exc}\n")
        store.update(job_id, state="failed", stage="Failed", completed_at=utc_now(), error=str(exc)[:1000], lock_token=None)
    finally:
        stop_heartbeat.set()
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        with suppress(Exception):
            await _queue_request("/release", {"token": token}, timeout=6)
        notify_job(job_id)
    if completed:
        await _publish_final_lora(job_id)


def _nonfinite_tensors(path: Path) -> int:
    """How many tensors in a .safetensors file hold NaN or Inf: what a run that diverged saves."""
    import numpy as np
    bad = 0
    with path.open("rb") as stream:
        size = struct.unpack("<Q", stream.read(8))[0]
        header = json.loads(stream.read(size))
        for key, info in header.items():
            if key == "__metadata__" or info.get("dtype") not in {"BF16", "F16", "F32", "F64"}:
                continue
            start, end = info["data_offsets"]
            stream.seek(8 + size + start)
            raw = stream.read(end - start)
            if info["dtype"] == "BF16":    # NaN/Inf: every exponent bit set
                broken = ((np.frombuffer(raw, "<u2") & 0x7F80) == 0x7F80).any()
            else:
                broken = not np.isfinite(np.frombuffer(raw, {"F16": "<f2", "F32": "<f4", "F64": "<f8"}[info["dtype"]])).all()
            bad += bool(broken)
    return bad


async def _corrupt_lora(job_id: str, path: Path, tag: str) -> bool:
    """True (and logged) when the file can't be published because its weights hold NaN/Inf."""
    try:
        bad = await asyncio.to_thread(_nonfinite_tensors, path)
    except (OSError, ValueError, KeyError, struct.error) as exc:
        store.append_log(job_id, f"\n[{tag}] {path.name} is not a readable .safetensors file: {exc}\n")
        return True
    if bad:
        store.append_log(job_id, f"\n[{tag}] Not published: {bad} tensor(s) in {path.name} hold NaN/Inf weights "
                                 "(training diverged). Publish an earlier checkpoint instead.\n")
    return bool(bad)


def _final_lora(job_id: str, name: str) -> Path | None:
    folder = DATA_DIR / "output" / job_id
    final = folder / f"{name}.safetensors"
    if final.is_file():
        return final
    files = list(folder.rglob("*.safetensors")) if folder.is_dir() else []
    if not files:
        return None
    def order(path: Path) -> tuple[int, float]:
        match = re.search(r"(?:epoch[_-]?|[_-])(\d+)$", path.stem, re.I)
        return (int(match.group(1)) if match else -1, path.stat().st_mtime)
    return max(files, key=order)


CORRUPT_STAGE = "Complete · LoRA corrupt (NaN)"


def _refused_as_corrupt(job_id: str) -> bool:
    """Whether the publish attempt that just failed was refused by _corrupt_lora (its note ends the log)."""
    tail = ((store.get(job_id) or {}).get("log_text") or "")[-400:]
    return "hold NaN/Inf weights" in tail or "not a readable .safetensors" in tail


async def _publish_final_lora(job_id: str) -> bool:
    item = store.get(job_id)
    if not item or item["model"] not in {"minimax_h3", "ltx25"} or item["state"] != "complete":
        return False
    final = _final_lora(job_id, item["name"])
    if not final:
        store.append_log(job_id, "\n[Gallery] No final .safetensors output was found.\n")
        store.update(job_id, stage="Complete · Gallery pending")
        return False
    if await _corrupt_lora(job_id, final, "Gallery"):
        store.update(job_id, stage=CORRUPT_STAGE)  # not "Gallery pending": retrying can't fix the file
        return False
    request = json.loads(item["request_json"])
    payload = {"job_id": job_id, "filename": final.relative_to(DATA_DIR / "output" / job_id).as_posix(), "model": item["model"],
               "name": item["name"], "trigger": request.get("trigger", "")}
    last_error = "Unknown error"
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                response = await client.post(H3_GALLERY_URL + "/api/internal/trainer/lora", json=payload,
                    headers=LOCK_HEADERS)
                response.raise_for_status()
            store.append_log(job_id, f"\n[Gallery] Final LoRA {final.name} added to the Video gallery.\n")
            store.update(job_id, stage="Complete · In gallery")
            return True
        except Exception as exc:
            last_error = str(exc)
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
    store.append_log(job_id, f"\n[Gallery] Could not publish final LoRA: {last_error}. Use Publish to gallery to retry.\n")
    store.update(job_id, stage="Complete · Gallery pending")
    return False


# Image families the Image studio's custom LoRA gallery can use -> its model names there.
IMAGE_GALLERY = {"krea2": "krea", "qwen_image_21": "qwen", "moss_image": "moss", "anima": "anima"}
KREA_URL = os.getenv("TRAINER_KREA_URL", "http://127.0.0.1:8300/Image").rstrip("/")


def _copy_for_gallery(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".part")
    shutil.copyfile(source, temporary)
    owner = target.parent.stat()
    with suppress(OSError):  # the Image studio runs as the folder's owner and may delete it later
        os.chown(temporary, owner.st_uid, owner.st_gid)
    os.replace(temporary, target)


async def _publish_image_lora(job_id: str, checkpoint: str = "") -> bool:
    """Copy the final LoRA (or one saved checkpoint, e.g. "Name-000004.safetensors") into the
    Image studio's custom LoRA folder (the same ComfyUI models mount) and register it there.
    Publishing the same file again reuses its entry."""
    item = store.get(job_id)
    if not item or item["model"] not in IMAGE_GALLERY or item["state"] != "complete":
        return False
    folder = DATA_DIR / "output" / job_id
    if checkpoint:
        final = folder / checkpoint
        if checkpoint != Path(checkpoint).name or not checkpoint.endswith(".safetensors") or not final.is_file():
            store.append_log(job_id, f"\n[Image gallery] Checkpoint {checkpoint} was not found in this job's output.\n")
            return False
    else:
        final = _final_lora(job_id, item["name"])
    if not final:
        store.append_log(job_id, "\n[Image gallery] No final .safetensors output was found.\n")
        return False
    if await _corrupt_lora(job_id, final, "Image gallery"):
        if not checkpoint:
            store.update(job_id, stage=CORRUPT_STAGE)
        return False
    epoch = re.search(r"-0*(\d+)$", final.stem) if checkpoint else None
    target = MODEL_ROOT / "loras" / "KreaCustom" / (f"trainer-{job_id}-{final.stem[-6:]}.safetensors" if epoch else f"trainer-{job_id}.safetensors")
    label = f"{item['name']} (epoch {epoch.group(1)})" if epoch else item["name"]
    trigger = str(json.loads(item["request_json"]).get("trigger") or "").strip()
    try:
        if not target.is_file() or target.stat().st_size != final.stat().st_size:
            await asyncio.to_thread(_copy_for_gallery, final, target)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(KREA_URL + "/api/internal/trainer/lora", headers=LOCK_HEADERS, json={
                "storage_name": target.name, "model": IMAGE_GALLERY[item["model"]], "label": label,
                "job_id": job_id, "triggers": [trigger] if trigger else []})
            response.raise_for_status()
    except Exception as exc:
        store.append_log(job_id, f"\n[Image gallery] Could not publish final LoRA: {exc}\n")
        return False
    store.append_log(job_id, f"\n[Image gallery] LoRA {final.name} added to the Image studio's LoRA gallery.\n")
    store.update(job_id, stage="Complete · In image gallery")
    return True


async def _retry_pending_gallery() -> None:
    while True:
        await asyncio.sleep(300)
        for item in store.list(500):
            if item["state"] == "complete" and item["stage"] == "Complete · Gallery pending":
                await _publish_final_lora(item["id"])


async def _setup_worker(key: str, command: list[str], cwd: Path, env_extra: dict[str, str]) -> None:
    setup_state[key] = {"state": "installing", "log": ""}
    env = os.environ.copy()
    env.update(env_extra)
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, cwd=str(cwd), env=env, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
        )
        assert proc.stdout is not None
        while line := await proc.stdout.readline():
            current = setup_state[key]["log"]
            setup_state[key]["log"] = (current + line.decode("utf-8", errors="replace"))[-20000:]
        code = await proc.wait()
        setup_state[key]["state"] = "ready" if code == 0 else "failed"
        if code:
            setup_state[key]["log"] += f"\nInstaller exited with code {code}."
    except Exception as exc:
        setup_state[key]["state"] = "failed"
        setup_state[key]["log"] += f"\n{exc}"
    finally:
        setup_tasks.pop(key, None)


async def _recover_stale_jobs() -> None:
    for item in store.stale():
        token = item.get("lock_token")
        entry_id = item.get("queue_entry_id")
        if token:
            with suppress(Exception):
                await _queue_request("/release", {"token": token})
        elif entry_id:
            with suppress(Exception):
                await _queue_request(f"/dequeue/{entry_id}")
        store.update(item["id"], state="failed", stage="Service restarted", completed_at=utc_now(), error="Trainer service restarted before this job finished.", lock_token=None)


@asynccontextmanager
async def lifespan(_: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATASET_ROOT.mkdir(parents=True, exist_ok=True)
    await _recover_stale_jobs()
    gallery_retry = asyncio.create_task(_retry_pending_gallery())
    job_tasks.add(gallery_retry)
    gallery_retry.add_done_callback(job_tasks.discard)
    try:
        yield
    finally:
        for job_id, process in tuple(active_processes.items()):
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        for task in tuple(job_tasks) + tuple(batch_trim_tasks):
            task.cancel()
        if job_tasks or batch_trim_tasks:
            await asyncio.gather(*job_tasks, *batch_trim_tasks, return_exceptions=True)
        store.close()


app = FastAPI(title="BunnyBytes Trainer", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount(p("/static"), StaticFiles(directory=BASE_DIR / "static"), name="trainer-static")
# Cache-busting token for the CSS/JS links: changes whenever any static file changes.
ASSET_VERSION = __import__("hashlib").sha1(b"".join(
    f.read_bytes() for f in sorted((BASE_DIR / "static").glob("*")) if f.is_file() and ".bak-" not in f.name
)).hexdigest()[:10]


def _busy_datasets() -> set[str]:
    return {item["dataset"] for item in store.stale()}


def _tag_activity() -> list[dict[str, Any]]:
    return tagger_router.active_jobs() + finder_router.active_jobs()   # defined below; looked up at call time


datasets_router = build_datasets_router(PREFIX, DATASET_ROOT, DATA_DIR, _busy_datasets, _tag_activity)
app.include_router(datasets_router)


def _give_back(path: Path) -> None:
    """Files written by this root container belong to the Datasets folder's owner."""
    with suppress(OSError):
        st = DATASET_ROOT.stat()
        os.chown(path, st.st_uid, st.st_gid)


tagger_router, tagger_proceed = build_tagger_router(PREFIX, DATASET_ROOT, DATA_DIR, _queue_request, _give_back)
app.include_router(tagger_router)
finder_router, finder_proceed = build_finder_router(PREFIX, DATA_DIR, _queue_request, datasets_router)
app.include_router(finder_router)
app.include_router(build_rules_router(PREFIX, DATA_DIR, _give_back))


class BatchAutoTrimRequest(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    concept: str = Field(default="", max_length=300)
    count: int = Field(default=8, ge=1, le=50)
    scene_mode: bool = False


class BatchReviewRequest(BaseModel):
    file: str | None = Field(default=None, max_length=400)   # None = the oldest video waiting for review
    picks: list[int] = Field(default_factory=list, max_length=50)
    skip: bool = False


batch_trim_jobs: dict[str, dict[str, Any]] = {}
batch_trim_tasks: set[asyncio.Task[Any]] = set()
batch_trim_cut_locks: dict[str, asyncio.Lock] = {}
BATCH_ACTIVE = {"queued", "running", "review", "cancelling"}


def _batch_response(response: httpx.Response) -> dict[str, Any]:
    if response.is_error:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise RuntimeError(str(detail or f"Request failed ({response.status_code})"))
    return response.json()


def _batch_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://trainer.local", timeout=None)


def _batch_track(coro: Any) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro)
    batch_trim_tasks.add(task)
    task.add_done_callback(batch_trim_tasks.discard)
    return task


def _batch_settle(job: dict[str, Any]) -> None:
    """Scouting runs ahead of the user: the batch only ends once every scouted video is reviewed and cut."""
    if job.get("finished"):
        return
    if job["cancel_requested"]:
        for item in job["reviews"]:
            job["skipped"].append({"file": item["file"], "reason": "Batch cancelled before review"})
            job["done"] += 1
        job["reviews"] = []
    if not job["scouting_done"]:
        return
    if job["cutting"]:
        job.update(state="cancelling" if job["cancel_requested"] else "running", phase="cutting",
                   stage=f"Cutting {', '.join(job['cutting'])}")
    elif job["reviews"]:
        job.update(state="review", phase="review",
                   stage=f"Scouting finished · {len(job['reviews'])} video{'s' if len(job['reviews']) != 1 else ''} waiting for your review")
    else:
        cancelled = job["cancel_requested"]
        job.update(state="cancelled" if cancelled else "complete", current=None,
                   phase="cancelled" if cancelled else "done", stage="Cancelled" if cancelled else "Finished",
                   finished=time.time())


async def _run_batch_auto_trim(job: dict[str, Any], files: list[str], body: BatchAutoTrimRequest) -> None:
    name = job["dataset"]
    try:
        async with _batch_client() as client:
            for filename in files:
                if job["cancel_requested"]:
                    break
                job.update(current=filename, phase="queue", stage="Asking Hermes to find clips",
                           scout_progress={"processed": 0, "total": 0})
                if not job["cutting"]:
                    job["state"] = "running"
                try:
                    url = p(f"/api/datasets/{name}/autocut/{quote(filename, safe='')}")
                    response = await client.post(url, json={"model": body.model, "concept": body.concept,
                        "count": body.count, "variable": True, "min_seconds": 2, "max_seconds": 5,
                        "h3": True, "staged": True, "scene_mode": body.scene_mode, "source": "trainer"})
                    scout = _batch_response(response)
                    job["active_scout"] = scout["id"]
                    while scout["state"] in {"waiting_for_gpu", "preparing", "running"}:
                        if job["cancel_requested"] and not job.get("cancel_sent"):
                            await client.post(p(f"/api/tag-jobs/{scout['id']}/cancel"))
                            job["cancel_sent"] = True
                        await asyncio.sleep(1.5)
                        poll = await client.get(p(f"/api/tag-jobs/{scout['id']}"))
                        scout = _batch_response(poll)
                        job["stage"] = scout.get("stage") or "Scouting video"
                        job["phase"] = "queue" if scout["state"] == "waiting_for_gpu" else "scouting"
                        job["scout_progress"] = {"processed": scout.get("processed", 0), "total": scout.get("total", 0)}
                    job["active_scout"] = None
                    job["cancel_sent"] = False
                    if job["cancel_requested"]:
                        break
                    if scout["state"] != "complete":
                        raise RuntimeError(scout.get("error") or f"Hermes {scout['state']}")
                    picked_starts = {round(float(clip["start"]), 3) for clip in scout.get("segments") or []}
                    choices = [{"start": clip["start"], "end": clip["end"], "score": clip.get("score", 0),
                                "subject": clip.get("subject", ""), "reason": clip.get("reason", ""),
                                "picked": round(float(clip["start"]), 3) in picked_starts}
                               for clip in scout.get("windows") or [] if clip.get("keep")]
                    if not choices:
                        job["skipped"].append({"file": filename, "reason": "Hermes found no usable clips"})
                        job["done"] += 1
                        continue
                    # Don't wait for the user: park it for review and scout the next video.
                    job["reviews"].append({"file": filename, "choices": choices, "scouted_at": time.time()})
                except Exception as exc:
                    job["skipped"].append({"file": filename, "reason": str(exc)[:300]})
                    job["done"] += 1
                finally:
                    job["active_scout"] = None
                    job["scouted"] += 1
    except asyncio.CancelledError:
        job.update(state="cancelled", current=None, phase="cancelled", stage="Stopped with the service", finished=time.time())
        raise
    finally:
        job.update(current=None, scouting_done=True, scout_progress={"processed": 0, "total": 0})
        _batch_settle(job)


async def _batch_cut(job: dict[str, Any], filename: str, segments: list[dict[str, float]]) -> None:
    name = job["dataset"]
    lock = batch_trim_cut_locks.setdefault(job["id"], asyncio.Lock())
    try:
        async with lock, _batch_client() as client:   # one ffmpeg cut at a time; scouting keeps going meanwhile
            cut = await client.post(p(f"/api/datasets/{name}/trim/{quote(filename, safe='')}"),
                params={"staged": "true"}, json={"segments": segments, "h3_ready": True})
            made = _batch_response(cut)["clips"]
        job["clips"] += len(made)
        job["trimmed"].append(filename)
        job["saved"][filename] = len(made)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        job["skipped"].append({"file": filename, "reason": str(exc)[:300]})
    finally:
        with suppress(ValueError):
            job["cutting"].remove(filename)
        job["done"] += 1
        if not job["cutting"]:
            batch_trim_cut_locks.pop(job["id"], None)
        _batch_settle(job)


@app.post(p("/api/datasets/{name}/autotrim-all"), status_code=202)
async def start_batch_auto_trim(name: str, body: BatchAutoTrimRequest) -> dict[str, Any]:
    ds = _dataset(name)
    if name in _busy_datasets():
        raise HTTPException(409, "Training is using this dataset. Wait for training to finish.")
    if body.model not in {item["id"] for item in vision_models()}:
        raise HTTPException(422, "Choose an available vision model.")
    active = batch_trim_jobs.get(name)
    if active and active["state"] in BATCH_ACTIVE:
        raise HTTPException(409, "Auto-trim is already running for this dataset.")
    incoming = ds / ".incoming"
    files = sorted(item.name for item in incoming.iterdir() if item.is_file() and item.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}) if incoming.is_dir() else []
    if not files:
        raise HTTPException(422, "No videos are waiting to be trimmed.")
    job = {"id": uuid.uuid4().hex, "dataset": name, "state": "queued", "phase": "queue", "stage": "Starting", "current": None,
           "files": files, "scene_mode": body.scene_mode, "total": len(files), "done": 0, "scouted": 0, "clips": 0,
           "trimmed": [], "saved": {}, "skipped": [], "reviews": [], "cutting": [], "scouting_done": False,
           "scout_progress": {"processed": 0, "total": 0}, "active_scout": None,
           "cancel_requested": False, "cancel_sent": False, "created": time.time(), "finished": None}
    batch_trim_jobs[name] = job
    _batch_track(_run_batch_auto_trim(job, files, body))
    return {"job": job}


@app.get(p("/api/datasets/{name}/autotrim-all"))
async def get_batch_auto_trim(name: str) -> dict[str, Any]:
    _dataset(name)
    return {"job": batch_trim_jobs.get(name)}


@app.post(p("/api/datasets/{name}/autotrim-all/review"))
async def submit_batch_auto_trim_review(name: str, body: BatchReviewRequest) -> dict[str, Any]:
    _dataset(name)
    job = batch_trim_jobs.get(name)
    if not job or job["state"] not in BATCH_ACTIVE or job["cancel_requested"] or not job["reviews"]:
        raise HTTPException(409, "No video is waiting for Auto-trim review.")
    index = 0 if body.file is None else next((i for i, item in enumerate(job["reviews"]) if item["file"] == body.file), -1)
    if index < 0:
        raise HTTPException(409, f"{body.file} is not waiting for Auto-trim review.")
    review = job["reviews"][index]
    if not body.skip and (not body.picks or len(body.picks) != len(set(body.picks)) or
                          any(i < 0 or i >= len(review["choices"]) for i in body.picks)):
        raise HTTPException(422, "Select at least one distinct Kept clip, or skip this video.")
    job["reviews"].pop(index)
    if body.skip:
        # A video skipped in review leaves "Waiting to be trimmed" for good, like Discard.
        async with _batch_client() as client:
            gone = await client.delete(p(f"/api/datasets/{name}/staged/{quote(review['file'], safe='')}"))
        job["skipped"].append({"file": review["file"], "removed": not gone.is_error,
                               "reason": "Skipped during review · removed" if not gone.is_error else "Skipped during review"})
        job["done"] += 1
    else:
        segments = [{"start": review["choices"][i]["start"], "end": review["choices"][i]["end"]} for i in sorted(body.picks)]
        job["cutting"].append(review["file"])
        _batch_track(_batch_cut(job, review["file"], segments))
    _batch_settle(job)
    return {"job": job}


@app.post(p("/api/datasets/{name}/autotrim-all/cancel"))
async def cancel_batch_auto_trim(name: str) -> dict[str, Any]:
    _dataset(name)
    job = batch_trim_jobs.get(name)
    if not job:
        raise HTTPException(404, "No auto-trim batch found for this dataset.")
    if job["state"] in {"queued", "running", "review"}:
        job.update(cancel_requested=True, state="cancelling", phase="cancelling",
                   stage="Stopping · clips you already approved still get cut")
        _batch_settle(job)
    return {"job": job}


@app.get(p("/"))
async def index(request: Request):
    response = templates.TemplateResponse(request, "index.html", {"mount_prefix": PREFIX, "asset_version": ASSET_VERSION})
    response.headers["Cache-Control"] = "no-cache"
    return response


# The service worker must be served from the app's root (not /static/) so its
# scope covers the whole app; no-cache so updates to it land promptly.
@app.get(p("/sw.js"))
async def service_worker() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get(p("/manifest.webmanifest"))
async def web_manifest() -> JSONResponse:
    """Lets phones install the Trainer to the Home Screen - which iOS requires before a site may send notifications."""
    return JSONResponse({
        "name": "BunnyBytes Trainer", "short_name": "Trainer", "id": f"{PREFIX}/", "start_url": f"{PREFIX}/", "scope": f"{PREFIX}/",
        "display": "standalone", "background_color": "#0f1418", "theme_color": "#101619",
        "icons": [
            {"src": f"{PREFIX}/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": f"{PREFIX}/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": f"{PREFIX}/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, media_type="application/manifest+json")


@app.get(p("/api/push/key"))
async def push_key() -> dict[str, str]:
    return {"public_key": await asyncio.to_thread(push.public_key)}


@app.post(p("/api/push/subscribe"))
async def push_subscribe(body: dict[str, Any]) -> dict[str, bool]:
    endpoint, keys = body.get("endpoint"), body.get("keys") or {}
    if not isinstance(endpoint, str) or not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(422, "That push subscription is incomplete.")
    await asyncio.to_thread(push.save_subscription, {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}})
    return {"ok": True}


@app.post(p("/api/push/unsubscribe"))
async def push_unsubscribe(body: dict[str, Any]) -> dict[str, bool]:
    if isinstance(body.get("endpoint"), str):
        await asyncio.to_thread(push.delete_subscription, body["endpoint"])
    return {"ok": True}


@app.post(p("/api/push/test"))
async def push_test(body: dict[str, Any]) -> dict[str, bool]:
    """Sends one notification to the calling device's subscription."""
    subscription = next((item for item in push.subscriptions() if item["endpoint"] == body.get("endpoint")), None)
    if not subscription:
        raise HTTPException(404, "This device isn't subscribed to notifications.")
    sent = await asyncio.to_thread(push.send, subscription, {
        "title": "Trainer notifications are on", "body": "You'll get one of these when a LoRA finishes training.",
        "tag": "trainer-test", "url": f"{PREFIX}/",
    })
    return {"ok": sent}


@app.get(p("/api/health"))
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(p("/api/lora-gallery/shared"))
async def shared_lora_gallery() -> dict[str, Any]:
    """Read the same built-in galleries used by the video and image studios."""
    url = os.getenv("BUNNY_FRAMEWORK_URL", "http://127.0.0.1:8700").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{url}/v1/lora-gallery/all", headers=LOCK_HEADERS)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "Shared LoRA gallery is unavailable") from exc


@app.get(p("/api/model-files"))
async def model_files() -> dict[str, list[str]]:
    def scan() -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for kind, folders in MODEL_FILE_FOLDERS.items():
            found: list[str] = []
            for folder in folders:
                root = MODEL_ROOT / folder
                if not root.is_dir():
                    continue
                for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
                    rel_dir = Path(dirpath).relative_to(MODEL_ROOT)
                    if len(rel_dir.parts) > 3:
                        dirnames[:] = []
                        continue
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    for name in filenames:
                        if Path(name).suffix.lower() in MODEL_FILE_EXTS:
                            found.append(str(rel_dir / name))
            result[kind] = sorted(set(found), key=str.casefold)
        return result
    return await asyncio.to_thread(scan)


@app.get(p("/api/presets"))
async def list_presets() -> dict[str, Any]:
    return {"builtin": BUILTIN_PRESETS, "user": _user_presets()}


@app.put(p("/api/presets"))
async def save_preset(body: PresetSave) -> dict[str, Any]:
    name = body.name.strip()
    _family(body.model)
    if name in BUILTIN_PRESETS.get(body.model, {}):
        raise HTTPException(400, "Built-in presets can't be overwritten; save under a new name.")
    settings = {k: v for k, v in body.settings.items() if k in PRESET_FIELDS}
    async with state_guard:
        data = _user_presets()
        data.setdefault(body.model, {})[name] = settings
        PRESETS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"builtin": BUILTIN_PRESETS, "user": data}


@app.delete(p("/api/presets"))
async def delete_preset(model: str, name: str) -> dict[str, Any]:
    async with state_guard:
        data = _user_presets()
        if name not in data.get(model, {}):
            raise HTTPException(404, "Preset not found.")
        del data[model][name]
        PRESETS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"builtin": BUILTIN_PRESETS, "user": data}


_probe_cache: dict[str, tuple[float, str]] = {}


async def _probe_backend(py: Path) -> str:
    """Can this trainer's Python see a ROCm GPU? Importing torch takes seconds, so the answer is
    kept for 10 minutes (the page polls status every few seconds)."""
    if not py.is_file():
        return "not_installed"
    hit = _probe_cache.get(str(py))
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    state = "ready"
    try:
        probe = await asyncio.create_subprocess_exec(
            str(py), "-c", "import torch; print('PROBE', torch.__version__, torch.version.hip or 'NO_HIP', torch.cuda.is_available())",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(probe.communicate(), timeout=30)
        # ROCm libraries may print noise of their own (e.g. "(null): No such file"); read only our line.
        line = next((l for l in output.decode(errors="replace").splitlines() if l.startswith("PROBE ")), "")
        lines = line.split()[1:]
        if probe.returncode or len(lines) < 3 or lines[1] == "NO_HIP":
            state = "rocm_unavailable"
        elif lines[2].strip().lower() != "true":
            state = "gpu_unavailable"
    except Exception:
        state = "environment_error"
    if state == "ready":  # failures are re-checked every time (e.g. right after an install)
        _probe_cache[str(py)] = (time.monotonic(), state)
    return state


@app.get(p("/api/status"))
async def status() -> dict[str, Any]:
    backends = {}
    for key, info in families.BACKENDS.items():
        py = families.backend_python(key)
        state = await _probe_backend(py)
        if key == families.SDS and state == "ready" and not families.SDSCRIPTS_DIR.is_dir():
            state = "not_installed"
        backends[key] = {"state": state, "python": str(py), "title": info["title"], "covers": info["covers"],
                         "install": info.get("install"), "note": info.get("note", "")}
    datasets = sorted(path.name for path in DATASET_ROOT.iterdir() if path.is_dir() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", path.name))
    return {"rocm": "ROCm is required; CUDA/CPU training is disabled.", "backends": backends, "datasets": datasets, "setups": setup_state}


@app.get(p("/api/families"))
async def list_families() -> dict[str, Any]:
    """Every trainable model family, what is installed for it and what is missing."""
    return await asyncio.to_thread(families.scan, MODEL_ROOT)


MODELHUB_URL = os.getenv("TRAINER_MODELHUB_URL", "http://127.0.0.1:8960/Models").rstrip("/")


@app.post(p("/api/families/{family_id}/get/{slot_key}"))
async def get_family_file(family_id: str, slot_key: str) -> dict[str, Any]:
    """Download a missing training file through the Models hub (it shows up in Models → Downloads)."""
    fam = _family(family_id)
    slot = next((s for s in fam.slots if s.key == slot_key), None)
    if not slot or not slot.get:
        raise HTTPException(404, "No download is known for that file.")
    src = slot.get
    page = f"https://huggingface.co/{src['repo']}" + (f"/blob/main/{src['path']}" if src.get("path") else "")
    if src.get("manual"):
        raise HTTPException(409, f"Download this one by hand from {page}" + (f" and save it as models/{src['dest']}/{src['save_as']}" if src.get("save_as") else f" into models/{src['dest']}/")
                            + (" (accept the licence there first)" if src.get("gated") else "") + ".")
    body = {"source": "hf", "model_id": src["repo"], "version_id": "", "variant": src["path"], "dest": src["dest"], "companions": False}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{MODELHUB_URL}/api/download", json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"The Models hub is not reachable; download it from {page}.") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text[:200]
        raise HTTPException(502, f"Models hub: {detail or response.status_code}. You can also download it from {page}.")
    return {"ok": True, "page": page, "dest": src["dest"], "file": Path(src["path"]).name}


@app.post(p("/api/setup/{backend}"), status_code=202)
async def setup_backend(backend: str) -> dict[str, str]:
    if backend not in {"fizgig", "ltx"}:
        raise HTTPException(404, "Unknown trainer environment.")
    async with state_guard:
        task = setup_tasks.get(backend)
        if task and not task.done():
            return {"state": "installing"}
        if backend == "fizgig":
            command = ["bash", "./install_fizgig_rocm.sh"]
            cwd = TRAINER_ROOT / "vendor" / "Fizgig"
            env = {"ROCM_CHANNEL": "stable"}
        else:
            cwd = TRAINER_ROOT / "vendor" / "ai-toolkit"
            command = ["python3", "-m", "manager", "install"]
            env = {}
        task = asyncio.create_task(_setup_worker(backend, command, cwd, env))
        setup_tasks[backend] = task
    return {"state": "installing"}


@app.post(p("/api/jobs"), status_code=202)
async def create_job(request: JobRequest) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    spec = await asyncio.to_thread(_prepare_spec, request, job_id)
    row = {
        "id": job_id, "model": request.model, "name": spec["name"], "dataset": request.dataset,
        "state": "waiting_for_gpu", "stage": "Waiting for GPU queue", "created_at": utc_now(),
        "request": request.model_dump(),
    }
    store.create(row)
    pending_specs[job_id] = spec
    try:
        response = await _queue_request("/enqueue", {"app": "Trainer", "job_id": job_id, "label": f"LoRA: {families.title(request.model)} / {spec['name']}"})
        response.raise_for_status()
        entry = response.json()
        store.update(job_id, queue_entry_id=entry["entry_id"])
    except Exception as exc:
        pending_specs.pop(job_id, None)
        store.update(job_id, state="failed", stage="Queue error", completed_at=utc_now(), error=f"Could not enqueue training job: {exc}")
        raise HTTPException(503, "Could not reach the shared GPU queue.") from exc
    return _public(store.get(job_id))


@app.post(p("/api/internal/queue/proceed/{job_id}"))
async def queue_proceed(job_id: str, request: Request) -> dict[str, bool]:
    client_host = request.client.host if request.client else None
    if client_host not in {"127.0.0.1", "::1"} or request.headers.get("X-Queue-Internal-Secret") != QUEUE_SECRET:
        raise HTTPException(403, "This internal callback is only available to the local queue coordinator.")
    body = await request.json()
    token = body.get("token")
    if job_id.startswith(("tag", "find")):
        # Auto-tagging (tagger.py) and clip-finder (finder.py) jobs share the Trainer's GPU-queue slot.
        accepted = bool(token) and (finder_proceed if job_id.startswith("find") else tagger_proceed)(job_id, token)
        if not accepted and token:
            with suppress(Exception):
                await _queue_request("/release", {"token": token})
        return {"accepted": accepted}
    for _ in range(100):
        item = store.get(job_id)
        if item and item.get("queue_entry_id"):
            break
        await asyncio.sleep(0.1)
    item = store.get(job_id)
    spec = pending_specs.pop(job_id, None)
    if not item or item["state"] != "waiting_for_gpu" or not spec or not token:
        if token:
            with suppress(Exception):
                await _queue_request("/release", {"token": token})
        return {"accepted": False}
    task = asyncio.create_task(_execute_job(job_id, token, spec))
    job_tasks.add(task)
    task.add_done_callback(job_tasks.discard)
    return {"accepted": True}


@app.get(p("/api/jobs"))
async def list_jobs() -> dict[str, Any]:
    return {"jobs": [_public(item) for item in store.list()]}


@app.get(p("/api/jobs/{job_id}"))
async def get_job(job_id: str) -> dict[str, Any]:
    item = store.get(job_id)
    if not item:
        raise HTTPException(404, "Training job not found.")
    return _public(item)


@app.post(p("/api/jobs/{job_id}/publish-lora"))
async def publish_job_lora(job_id: str, request: Request) -> dict[str, Any]:
    item = store.get(job_id)
    if item and item["state"] == "complete" and item["model"] in IMAGE_GALLERY:
        body = await request.json() if int(request.headers.get("content-length") or 0) else {}
        checkpoint = str(body.get("checkpoint") or "") if isinstance(body, dict) else ""
        if not await _publish_image_lora(job_id, checkpoint):
            if _refused_as_corrupt(job_id):
                raise HTTPException(422, "This LoRA has NaN/Inf weights (training diverged), so it wasn't published. "
                                         "Publish a checkpoint from an epoch before the loss went NaN (the job log shows when it did).")
            raise HTTPException(502, "Could not add the final LoRA to the Image studio's gallery; check the job log and retry.")
        return _public(store.get(job_id))
    if not item or item["state"] != "complete" or item["model"] not in {"minimax_h3", "ltx25"}:
        raise HTTPException(422, "Only completed H3, LTX, Krea 2, Qwen Image 2.1, MOSS-Image and Anima jobs can be published.")
    if not await _publish_final_lora(job_id):
        if _refused_as_corrupt(job_id):
            raise HTTPException(422, "This LoRA has NaN/Inf weights (training diverged), so it wasn't published.")
        raise HTTPException(502, "Could not add the final LoRA to the video gallery; check the job log and retry.")
    return _public(store.get(job_id))


@app.post(p("/api/jobs/{job_id}/cancel"))
async def cancel_job(job_id: str) -> dict[str, Any]:
    item = store.get(job_id)
    if not item:
        raise HTTPException(404, "Training job not found.")
    if item["state"] in TERMINAL:
        return _public(item)
    if item["state"] == "waiting_for_gpu":
        entry_id = item.get("queue_entry_id")
        if entry_id:
            with suppress(Exception):
                await _queue_request(f"/dequeue/{entry_id}")
        store.update(job_id, state="cancelled", stage="Cancelled", completed_at=utc_now())
        pending_specs.pop(job_id, None)
    else:
        store.update(job_id, state="cancelling", stage="Stopping trainer")
        process = active_processes.get(job_id)
        if process and process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=15)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
    return _public(store.get(job_id))


@app.delete(p("/api/jobs/{job_id}"))
async def delete_job(job_id: str, delete_files: bool = False) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(422, "Invalid job id.")
    item = store.get(job_id)
    if not item:
        raise HTTPException(404, "Training job not found.")
    if item["state"] not in TERMINAL:
        raise HTTPException(409, "Cancel this job before removing it.")
    store.delete(job_id)
    _progress_cache.pop(job_id, None)
    (DATA_DIR / "configs" / f"{job_id}.toml").unlink(missing_ok=True)
    (DATA_DIR / "configs" / f"{job_id}.yaml").unlink(missing_ok=True)
    shutil.rmtree(DATA_DIR / "cache" / job_id, ignore_errors=True)
    shutil.rmtree(DATA_DIR / "train-sets" / job_id, ignore_errors=True)
    if delete_files:
        shutil.rmtree(DATA_DIR / "output" / job_id, ignore_errors=True)
    return {"deleted": job_id, "files_deleted": delete_files}


@app.exception_handler(Exception)
async def handle_error(_: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled Trainer error", exc_info=exc)
    return JSONResponse({"error": "internal_error", "message": "Trainer request failed."}, status_code=500)
