"""3D Models: the Rocsy studio that turns pictures (or a text prompt) into 3D meshes (GLB) with Hunyuan3D.

Renders go through the shared ComfyUI with its built-in nodes only (Hunyuan3D v2 for the shape,
the 3d/texturing nodes for colour) and take turns on the GPU through bunny-lock (app "Model3D").

A job runs up to three steps:
  1. picture  - text prompt only: the Image studio draws it as a hidden job (its own GPU turn).
  2. shape    - Hunyuan3D -> voxels -> mesh.
  (edit)     - an instruction edits a finished model's picture in the Image studio (Qwen Image 2.1 with the
               picture as reference); then either only the colours are repainted onto the existing mesh, or
               the model is rebuilt from the edited picture.
  (repaint)  - an uploaded GLB/OBJ/STL is painted from a picture, or from a prompt drawn over a render of it.
  3. paint    - optional: decimate, UV-unwrap, render the mesh's silhouette from the front/right/back/left,
                line each picture up with its silhouette, project them onto the UV texture
                (BakeTextureFromViews, unseen areas grown from the seen colours) and save a textured GLB.

  /Model3D/                     the page (served behind the Rocsy login by the gateway)
  /Model3D/api/health           health check (compose, hostctl Services, gateway home card)
  /Model3D/api/version          running version
  /Model3D/api/home-art         home card backdrop (input pictures of finished models)
  /Model3D/api/models           Hunyuan3D checkpoints, background removal, Image studio models for prompts
  /Model3D/api/jobs             GET list, POST new job (multipart: images or prompt + settings)
  /Model3D/api/jobs/{id}        GET one, DELETE (cancel if running, else remove)
  /Model3D/api/jobs/{id}/edit   POST: new job from a finished one (instruction, keep_shape)
  /Model3D/api/jobs/{id}/redo   POST: the same job again (new seed / new picture / changed settings)
  /Model3D/api/jobs/{id}/rig    POST: add or redo the skeleton + preview animations (CPU, no GPU turn)
  /Model3D/api/jobs/{id}/physics POST: loose parts that swing (presets / brush), simulated and baked in
  /Model3D/files/{id}/...       model.glb and input pictures
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import re
import struct
import logging
import os
import random
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

import physics
import rig
from bunnylock import BunnyLock, LeaseCancelled, LockUnavailable

VERSION = "1.6.0"
PREFIX = os.getenv("M3D_PREFIX", "/Model3D").rstrip("/")
COMFY = os.getenv("M3D_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
SUPERVISOR = os.getenv("M3D_SUPERVISOR_URL", "http://127.0.0.1:8083").rstrip("/")
IMAGE_STUDIO = os.getenv("M3D_IMAGE_URL", "http://127.0.0.1:8300/Image").rstrip("/")
SECRET = os.getenv("QUEUE_INTERNAL_SECRET", "")
DATA = Path(os.getenv("M3D_DATA_DIR", "/data"))
JOBS_DIR = DATA / "jobs"
STATIC = Path(__file__).with_name("static")
VIEWS = ("front", "left", "back", "right")
# Orbit camera order (yaw 0, 90, 180, 270 around +Y; yaw 0 looks at the mesh's front from +Z).
ORBIT = ("front", "yaw90", "back", "yaw270")
RENDER = 1024
REMBG_NODE = "easy imageRemBg"
MAX_UPLOAD = 25 * 1024 * 1024
STATES = {"queued": "waiting_for_gpu", "running": "rendering", "done": "complete", "error": "failed",
          "cancelled": "cancelled"}
PROMPT_SUFFIX = (", a single object shown whole and centred, three-quarter front view, plain pure white background, "
                 "soft even studio lighting, no shadows, no text, sharp details")
# Characters: the subject leads and is repeated as "fully clothed and equipped exactly as described", so the
# T-pose wording can't turn the picture into an anatomy reference (it did, 2026-11-17: "a knight" came back as
# an unclothed figure). The negative prompt backs that up.
CHARACTER_PROMPT = ("A full-body 3D game character model of {prompt}, fully clothed and equipped exactly as described: "
                    "the outfit, armour, hair and colours clearly visible. The character stands in a T-pose with both "
                    "arms held straight out to the sides and the hands open, legs straight and apart with a clear gap between "
                    "them, feet a shoulder-width apart, facing "
                    "the camera in a straight-on front view, symmetric, the whole character in frame from head to feet. "
                    "Plain pure white background, soft even studio lighting, no shadows, no text.")
CHARACTER_NEGATIVE = ("nude, naked, unclothed, topless, underwear, lingerie, anatomy chart, medical illustration, "
                      "mannequin, three-quarter view, side view, back view, arms down, arms at the sides, crossed arms, "
                      "holding a weapon, shield, sword in hand, cropped, cut-off feet, cut-off head, shadow, text, watermark")
VIEW_PROMPTS = {
    "back": ("Show this exact same {what} from directly behind (back view): same pose, same size, same framing and "
             "proportions, with all colours, clothing, armour and details consistent with the picture. Plain pure "
             "white background, soft even lighting, no shadows."),
    "left": ("Show this exact same {what} from its left side (a side profile view): same pose, same size, same "
             "framing, with all colours and details consistent with the picture. Plain pure white background, soft "
             "even lighting, no shadows."),
    "right": ("Show this exact same {what} from its right side (a side profile view): same pose, same size, same "
              "framing, with all colours and details consistent with the picture. Plain pure white background, soft "
              "even lighting, no shadows."),
}
EDIT_IMAGE_MODEL = "qwen_image_21"   # the Image studio model that edits a picture from references
MESH_EXT = (".glb", ".obj", ".stl")
MAX_MESH = 200 * 1024 * 1024
EDIT_PROMPT = ("Edit this picture: {instruction}. Keep everything else exactly the same: the same object, pose, "
               "camera angle, framing and proportions, shown whole and centred on a plain pure white background.")
REPAINT_PROMPT = ("Paint this exact 3D object as: {prompt}. Keep its outline, shape, pose, camera angle and framing "
                  "exactly the same; only add colours, materials and surface details. Plain pure white background, "
                  "soft even lighting, no shadows.")
FALLBACK_IMAGE_MODELS = [{"key": "krea2", "label": "Krea 2"}, {"key": "qwen_image_21", "label": "Qwen Image 2.1"}]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("model3d")

app = FastAPI(title="3D Models", docs_url=None, redoc_url=None, openapi_url=None)
jobs: dict[str, dict[str, Any]] = {}
tasks: dict[str, asyncio.Task] = {}
gpu = BunnyLock(app="Model3D")


# ------------------------------------------------------------------ storage

def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def save_job(job: dict[str, Any]) -> None:
    path = job_dir(job["id"]) / "job.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: v for k, v in job.items() if not k.startswith("_")}, indent=1), encoding="utf-8")
    tmp.replace(path)


def public(job: dict[str, Any]) -> dict[str, Any]:
    base = f"{PREFIX}/files/{job['id']}"
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    # Rocsy-wide job fields (gateway home card + notifications): state names and a 0-100 progress.
    out["state"] = STATES.get(job.get("status"), job.get("status"))
    out["progress"] = round((job.get("progress") or 0) * 100, 1)
    out["name"] = job.get("title")
    out["inputs"] = {view: f"{base}/{name}" for view, name in job.get("inputs", {}).items()}
    for extra in ("original", "reference"):
        if (job_dir(job["id"]) / f"{extra}.png").exists():
            out[f"{extra}_url"] = f"{base}/{extra}.png"
    out.pop("frame", None)
    out["glb"] = f"{base}/model.glb" if job.get("status") == "done" else None
    return out


def load_jobs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for path in JOBS_DIR.glob("*/job.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("status") in ("queued", "running"):
            job.update(status="error", error="Rocsy restarted before this model finished. Press Retry.", stage="")
            save_job(job)
        jobs[job["id"]] = job


# ------------------------------------------------------------------ ComfyUI

async def comfy(method: str, path: str, **kw: Any) -> Any:
    async with httpx.AsyncClient(base_url=COMFY, timeout=kw.pop("timeout", 30)) as client:
        response = await client.request(method, path, **kw)
        response.raise_for_status()
        return response.json() if "json" in response.headers.get("content-type", "") else response.content


async def comfy_online() -> bool:
    try:
        await comfy("GET", "/system_stats", timeout=4)
        return True
    except httpx.HTTPError:
        return False


async def ensure_comfy(job: dict[str, Any], timeout: float = 300) -> None:
    if await comfy_online():
        return
    set_stage(job, "Starting ComfyUI")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{SUPERVISOR}/start")
        except httpx.HTTPError:
            pass
        await asyncio.sleep(3)
        if await comfy_online():
            return
    raise RuntimeError("ComfyUI isn't answering. Start it from the Queue page and try again.")


async def node_exists(name: str) -> bool:
    try:
        return bool(await comfy("GET", f"/object_info/{name}", timeout=10))
    except httpx.HTTPError:
        return False


async def image_models() -> list[dict[str, str]]:
    """The Image studio's text-to-image models (its Advanced workflow card list)."""
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.get(f"{IMAGE_STUDIO}/api/workflow-models")
            r.raise_for_status()
            listed = [{"key": w["key"], "label": w.get("label") or w["key"]} for w in r.json().get("workflows", [])
                      if isinstance(w, dict) and w.get("key")]
        return [m for m in listed if m["key"] != "h3_image"] or FALLBACK_IMAGE_MODELS
    except (httpx.HTTPError, ValueError, KeyError, AttributeError):
        return FALLBACK_IMAGE_MODELS


# TRELLIS.2 / Pixal3D (ComfyUI's built-in nodes; Comfy-Org template 3d_pixal3d_trellis2_image_to_model):
# a structure -> shape -> upsample -> texture pipeline that makes its own PBR textures.
TRELLIS_FILES = {"shape_vae": "trellis_2_shape_vae_bf16.safetensors", "texture_vae": "trellis_2_texture_vae_bf16.safetensors",
                 "dino": "dino_v3_L_naf_fp32.safetensors", "bg": "birefnet.safetensors",
                 "moge": "moge_2_vitl_normal_fp16.safetensors"}
AI_RIG_NODE = "MagicArticulateSkeleton"   # Model3D/comfy_nodes/MagicArticulate (installed into ComfyUI)
RIG_METHODS = {"builtin": "Built-in humanoid (instant; Idle/Walk/Run/Wave; physics)",
               "magicarticulate": "MagicArticulate AI (any shape, fingers and toes; a GPU turn)"}
TEXTURED_ENGINES = {"trellis2": "TRELLIS.2 (textured)", "pixal3d": "Pixal3D (textured, pixel-accurate)"}


async def _choices(node: str, field: str) -> list[str]:
    try:
        info = await comfy("GET", f"/object_info/{node}", timeout=10)
        spec = info[node]["input"]["required"][field]
        return list(spec[0]) if isinstance(spec[0], list) else list(spec[1].get("options", []))
    except (httpx.HTTPError, KeyError, IndexError, TypeError, AttributeError):
        return []


async def textured_engines() -> list[dict[str, Any]]:
    """TRELLIS.2 / Pixal3D entries whose files are all in place."""
    unets, vaes, clips, bgs, moges = await asyncio.gather(
        _choices("UNETLoader", "unet_name"), _choices("VAELoader", "vae_name"),
        _choices("CLIPVisionLoader", "clip_name"), _choices("LoadBackgroundRemovalModel", "bg_removal_name"),
        _choices("LoadMoGeModel", "model_name"))
    shared = (TRELLIS_FILES["shape_vae"] in vaes and TRELLIS_FILES["texture_vae"] in vaes
              and TRELLIS_FILES["dino"] in clips and TRELLIS_FILES["bg"] in bgs)
    out = []
    if not shared:
        return out
    for engine, prefix in (("trellis2", "trellis_2"), ("pixal3d", "pixal3d_")):
        files = sorted((u for u in unets if u.split("/")[-1].startswith(prefix) and "multiview" not in u),
                       key=lambda u: ("int8" not in u, u))           # the INT8 file first (half the VRAM)
        if not files or (engine == "pixal3d" and TRELLIS_FILES["moge"] not in moges):
            continue
        out.append({"name": files[0], "engine": engine, "multiview": False, "textured": True,
                    "label": TEXTURED_ENGINES[engine]})
    return out


async def available_models() -> dict[str, Any]:
    info = await comfy("GET", "/object_info/ImageOnlyCheckpointLoader", timeout=10)
    names = info["ImageOnlyCheckpointLoader"]["input"]["required"]["ckpt_name"][0]
    found = [n for n in names if "hunyuan" in n.lower() and "3d" in n.lower()]
    models = [{"name": n, "multiview": "-mv" in n.lower() or "_mv" in n.lower(), "engine": "hunyuan",
               "textured": False, "label": label_for(n)} for n in found]
    models.sort(key=lambda m: (m["multiview"], m["name"]))   # one-picture model first (the default)
    models += await textured_engines()
    rembg, paint, ai_rig = await asyncio.gather(node_exists(REMBG_NODE), node_exists("BakeTextureFromViews"),
                                                node_exists(AI_RIG_NODE))
    return {"models": models, "rembg": rembg, "paint": paint, "image_models": await image_models(),
            "rig_methods": RIG_METHODS if ai_rig else {"builtin": RIG_METHODS["builtin"]}}


def label_for(name: str) -> str:
    low = name.lower()
    if "-mv" in low or "_mv" in low:
        return "Hunyuan3D 2 MV (several views)"
    if "2.1" in low:
        return "Hunyuan3D 2.1 (one picture)"
    return name.rsplit(".", 1)[0]


def shape_nodes(job: dict[str, Any], uploaded: dict[str, str]) -> dict[str, Any]:
    """Hunyuan3D picture(s) -> MESH at node "mesh". Identical between the paint passes, so ComfyUI's
    cache reuses the shape in the second pass instead of generating it again."""
    s = job["settings"]
    wf: dict[str, Any] = {"ckpt": {"class_type": "ImageOnlyCheckpointLoader", "inputs": {"ckpt_name": job["model"]}}}
    encoded: dict[str, list] = {}
    for view, name in uploaded.items():
        wf[f"load_{view}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        image = [f"load_{view}", 0]
        if s["remove_background"]:
            wf[f"rembg_{view}"] = {"class_type": REMBG_NODE, "inputs": {
                "images": image, "rem_mode": "RMBG-1.4", "image_output": "Hide", "save_prefix": "Model3D/rembg",
                "add_background": "white"}}
            image = [f"rembg_{view}", 0]
        wf[f"enc_{view}"] = {"class_type": "CLIPVisionEncode",
                             "inputs": {"clip_vision": ["ckpt", 1], "image": image, "crop": "center"}}
        encoded[view] = [f"enc_{view}", 0]
    if job["multiview"]:
        wf["cond"] = {"class_type": "Hunyuan3Dv2ConditioningMultiView", "inputs": encoded}
    else:
        wf["cond"] = {"class_type": "Hunyuan3Dv2Conditioning", "inputs": {"clip_vision_output": encoded["front"]}}
    wf["latent"] = {"class_type": "EmptyLatentHunyuan3Dv2", "inputs": {"resolution": s["latent_tokens"], "batch_size": 1}}
    wf["shift"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["ckpt", 0], "shift": 1.0}}
    wf["sample"] = {"class_type": "KSampler", "inputs": {
        "model": ["shift", 0], "seed": s["seed"], "steps": s["steps"], "cfg": s["cfg"], "sampler_name": "euler",
        "scheduler": "normal", "positive": ["cond", 0], "negative": ["cond", 1], "latent_image": ["latent", 0],
        "denoise": 1.0}}
    wf["decode"] = {"class_type": "VAEDecodeHunyuan3D", "inputs": {
        "samples": ["sample", 0], "vae": ["ckpt", 2], "num_chunks": 8000, "octree_resolution": s["octree"]}}
    wf["mesh"] = {"class_type": "VoxelToMesh", "inputs": {
        "voxel": ["decode", 0], "algorithm": "surface net", "threshold": s["threshold"]}}
    return wf


def trellis_nodes(job: dict[str, Any], front: str) -> dict[str, Any]:
    """TRELLIS.2 / Pixal3D: picture -> textured MESH at node "textured" (Comfy-Org's template, in API form)."""
    s = job["settings"]
    pixal = job.get("engine") == "pixal3d"
    seed = s["seed"]
    detail = {128: 1024, 256: 1536, 384: 2048, 512: 2048}.get(s["octree"], 1536)
    tex = s["texture"]
    wf: dict[str, Any] = {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": job["model"], "weight_dtype": "default"}},
        "shape_vae": {"class_type": "VAELoader", "inputs": {"vae_name": TRELLIS_FILES["shape_vae"]}},
        "tex_vae": {"class_type": "VAELoader", "inputs": {"vae_name": TRELLIS_FILES["texture_vae"]}},
        "dino": {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": TRELLIS_FILES["dino"]}},
        "bg_model": {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": TRELLIS_FILES["bg"]}},
        "load": {"class_type": "LoadImage", "inputs": {"image": front}},
        "bg_mask": {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["bg_model", 0], "image": ["load", 0]}},
        "crop": {"class_type": "ImageCropToMask", "inputs": {"images": ["load", 0], "masks": ["bg_mask", 0], "width": 1024,
                                                               "height": 1024, "pad_factor": 1.1, "grow_mask": 0,
                                                               "background": "#000000"}},
        # Sampler model chains, as in the template ("to match the original default pipeline behaviour").
        "cfg_a": {"class_type": "CFGOverride", "inputs": {"model": ["unet", 0], "cfg": 1.0, "start_percent": 0.667, "end_percent": 1.0}},
        "rescale_a": {"class_type": "RescaleCFG", "inputs": {"model": ["cfg_a", 0], "multiplier": 0.7}},
        "shift_a": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["rescale_a", 0], "shift": 5.0}},
        "cfg_b": {"class_type": "CFGOverride", "inputs": {"model": ["unet", 0], "cfg": 1.0, "start_percent": 0.769, "end_percent": 1.0}},
        "rescale_b": {"class_type": "RescaleCFG", "inputs": {"model": ["cfg_b", 0], "multiplier": 0.5}},
        "empty": {"class_type": "EmptyTrellis2LatentStructure", "inputs": {"batch_size": 1}},
    }
    if pixal:
        wf["moge"] = {"class_type": "LoadMoGeModel", "inputs": {"model_name": TRELLIS_FILES["moge"]}}
        wf["moge_run"] = {"class_type": "MoGeInference", "inputs": {
            "moge_model": ["moge", 0], "image": ["crop", 0], "resolution_level": 9, "fov_x_degrees": 0.0, "batch_size": 4,
            "force_projection": True, "apply_mask": True, "refine_steps": 3}}
        wf["fov"] = {"class_type": "MoGeGeometryToFOV", "inputs": {"moge_geometry": ["moge_run", 0], "axis": "horizontal", "unit": "degrees"}}
        wf["cond"] = {"class_type": "Pixal3DConditioning", "inputs": {"clip_vision_model": ["dino", 0], "image": ["crop", 0],
                                                                      "camera_angle_x": ["fov", 0]}}
    else:
        wf["cond"] = {"class_type": "Trellis2Conditioning", "inputs": {"clip_vision_model": ["dino", 0], "image": ["crop", 0]}}
    ks = lambda model, pos, neg, latent, sd, steps, cfg, sched: {"class_type": "KSampler", "inputs": {  # noqa: E731
        "model": model, "seed": sd, "steps": steps, "cfg": cfg, "sampler_name": "euler", "scheduler": sched,
        "positive": pos, "negative": neg, "latent_image": latent, "denoise": 1.0}}
    wf["s_struct"] = ks(["shift_a", 0], ["cond", 0], ["cond", 1], ["empty", 0], seed, 12, 7.5, "normal")
    wf["structure"] = {"class_type": "VaeDecodeStructureTrellis2", "inputs": {"samples": ["s_struct", 0], "vae": ["shape_vae", 0], "resolution": "32"}}
    wf["shape_stage"] = {"class_type": "Trellis2ShapeStage", "inputs": {"positive": ["cond", 0], "negative": ["cond", 1], "voxel": ["structure", 0]}}
    wf["s_shape"] = ks(["rescale_b", 0], ["shape_stage", 0], ["shape_stage", 1], ["shape_stage", 2], seed + 1, 20, 7.5, "normal")
    wf["up_stage"] = {"class_type": "Trellis2UpsampleStage", "inputs": {"positive": ["shape_stage", 0], "negative": ["shape_stage", 1],
                                                                        "shape_latent": ["s_shape", 0], "vae": ["shape_vae", 0],
                                                                        "target_resolution": detail}}
    wf["s_up"] = ks(["rescale_b", 0], ["up_stage", 0], ["up_stage", 1], ["up_stage", 2], seed + 2, 12, 7.5, "simple")
    wf["shape"] = {"class_type": "VaeDecodeShapeTrellis", "inputs": {"samples": ["s_up", 0], "vae": ["shape_vae", 0]}}
    wf["tex_stage"] = {"class_type": "Trellis2TextureStage", "inputs": {"positive": ["up_stage", 0], "negative": ["up_stage", 1], "shape_latent": ["s_up", 0]}}
    wf["s_tex"] = ks(["unet", 0], ["tex_stage", 0], ["tex_stage", 1], ["tex_stage", 2], seed + 3, 12, 1.0, "normal")
    wf["colors"] = {"class_type": "VaeDecodeTextureTrellis", "inputs": {"samples": ["s_tex", 0], "vae": ["tex_vae", 0], "shape_subdivides": ["shape", 1]}}
    # Clean mesh for games/engines: remesh, decimate to the triangle budget, unwrap, bake PBR + normal + AO.
    wf["remesh"] = {"class_type": "RemeshMesh", "inputs": {
        "mesh": ["shape", 0], "resolution": 768, "sign_mode": "udf", "sign_mode.qef": False,
        "sign_mode.drop_inverted_components": False, "sign_mode.drop_enclosed_components": False, "band": 1.0,
        "project_back": 0.0, "fix_poles": False, "smooth_iters": 20, "drop_small_components": 0.01, "precluster_max_verts": 20000000}}
    wf["decimate"] = {"class_type": "DecimateMesh", "inputs": {"mesh": ["remesh", 0], "target_face_count": s["faces"], "placement_mode": "midpoint"}}
    wf["smooth"] = {"class_type": "MeshSmoothNormals", "inputs": {"mesh": ["decimate", 0], "crease_angle": 180.0}}
    wf["unwrap"] = {"class_type": "UnwrapMesh", "inputs": {"mesh": ["smooth", 0], "segmenter": "pec", "resolution": tex, "padding": 1, "weld_distance": 0.0002}}
    wf["bake"] = {"class_type": "BakeTextureFromVoxel", "inputs": {"mesh": ["unwrap", 0], "voxel_colors": ["colors", 0], "texture_size": tex, "reference_mesh": ["shape", 0]}}
    wf["normal_map"] = {"class_type": "BakeNormalMapFromMesh", "inputs": {"low_poly": ["unwrap", 0], "high_poly": ["remesh", 0], "resolution": tex, "cage_distance": 0.05, "ignore_backfaces": True}}
    wf["ao"] = {"class_type": "BakeAmbientOcclusion", "inputs": {"low_poly": ["unwrap", 0], "high_poly": ["remesh", 0], "resolution": min(tex, 1024), "samples": 64, "max_distance": 0.71, "strength": 1.0, "bias": 0.01}}
    wf["apply"] = {"class_type": "ApplyTextureToMesh", "inputs": {"mesh": ["unwrap", 0], "base_color": ["bake", 0], "metallic": ["bake", 1],
                                                                  "roughness": ["bake", 2], "occlusion": ["ao", 0], "normal_map": ["normal_map", 0]}}
    wf["textured"] = {"class_type": "MeshSmoothNormals", "inputs": {"mesh": ["apply", 0], "crease_angle": 180.0}}
    return wf


def paint_prep_nodes(job: dict[str, Any]) -> dict[str, Any]:
    """MESH -> decimated, UV-unwrapped mesh ("unwrap") + the 4 orbit cameras ("orbit"), framed on the mesh
    (job["frame"] for loaded meshes; Hunyuan3D's own output sits in about [-1, 1])."""
    s = job["settings"]
    frame = job.get("frame") or {"center": [0.0, 0.0, 0.0], "radius": 1.0}
    cx, cy, cz = frame["center"]
    distance = 1.12 * frame["radius"] / math.tan(math.radians(17.5))   # ortho half-height = d * tan(fov/2)
    return {
        "decimate": {"class_type": "DecimateMesh", "inputs": {
            "mesh": ["mesh", 0], "target_face_count": s["faces"], "placement_mode": "midpoint"}},
        "unwrap": {"class_type": "UnwrapMesh", "inputs": {
            "mesh": ["decimate", 0], "segmenter": "pec", "resolution": s["texture"], "padding": 2, "weld_distance": 0.0}},
        "cam": {"class_type": "CreateCameraInfo", "inputs": {
            # Dynamic combos go flat in API prompts: the option, then "<input>.<field>" keys.
            "mode": "orbit", "mode.yaw": 0.0, "mode.pitch": 0.0, "mode.distance": round(distance, 4),
            "target_x": cx, "target_y": cy, "target_z": cz, "roll": 0.0, "fov": 35.0, "zoom": 1.0,
            "camera_type": "orthographic"}},
        "orbit": {"class_type": "CreateCameraOrbit", "inputs": {
            "camera_info": ["cam", 0], "count": 4, "yaw": 360.0, "pitch": 0.0, "poles": "none"}},
    }


def mesh_nodes(job: dict[str, Any], comfy_name: str) -> dict[str, Any]:
    """An existing model file (in ComfyUI's input/3d) -> MESH at node "mesh", turned Y-up if it is Z-up."""
    wf: dict[str, Any] = {
        "load3d": {"class_type": "Load3DAdvanced", "inputs": {
            "model_file": comfy_name, "viewport_state": {}, "width": RENDER, "height": RENDER}},
        "mesh_file": {"class_type": "Get3DComponents", "inputs": {"model_3d": ["load3d", 0]}},
    }
    if job.get("mesh_up") == "z":
        wf["mesh"] = {"class_type": "RotateMesh", "inputs": {
            "mesh": ["mesh_file", 0], "mode": "euler_xyz", "mode.angle_x": -90.0, "mode.angle_y": 0.0, "mode.angle_z": 0.0}}
    else:
        wf["mesh"] = {"class_type": "RotateMesh", "inputs": {
            "mesh": ["mesh_file", 0], "mode": "euler_xyz", "mode.angle_x": 0.0, "mode.angle_y": 0.0, "mode.angle_z": 0.0}}
    return wf


def _mat_mul(a: list[float], b: list[float]) -> list[float]:
    """4x4 column-major (glTF) matrix product a*b."""
    return [sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4)) for c in range(4) for r in range(4)]


def _node_matrix(node: dict[str, Any]) -> list[float]:
    if "matrix" in node:
        return [float(v) for v in node["matrix"]]
    tx, ty, tz = node.get("translation", [0, 0, 0])
    qx, qy, qz, qw = node.get("rotation", [0, 0, 0, 1])
    sx, sy, sz = node.get("scale", [1, 1, 1])
    r = [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy + qz * qw), 2 * (qx * qz - qy * qw),
         2 * (qx * qy - qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz + qx * qw),
         2 * (qx * qz + qy * qw), 2 * (qy * qz - qx * qw), 1 - 2 * (qx * qx + qy * qy)]
    return [r[0] * sx, r[1] * sx, r[2] * sx, 0, r[3] * sy, r[4] * sy, r[5] * sy, 0,
            r[6] * sz, r[7] * sz, r[8] * sz, 0, tx, ty, tz, 1]


def _glb_points(data: bytes) -> list[tuple[float, float, float]]:
    """Corners of every mesh primitive's POSITION bounds, placed by the scene's node transforms."""
    if data[:4] != b"glTF":
        raise ValueError("not a binary glTF (.glb) file")
    length = struct.unpack("<I", data[12:16])[0]
    gltf = json.loads(data[20:20 + length])
    nodes, meshes, accessors = gltf.get("nodes", []), gltf.get("meshes", []), gltf.get("accessors", [])
    points: list[tuple[float, float, float]] = []
    identity = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]

    def walk(index: int, parent: list[float], depth: int = 0) -> None:
        if depth > 64:
            return
        node = nodes[index]
        world = _mat_mul(parent, _node_matrix(node))
        if "mesh" in node:
            for prim in meshes[node["mesh"]].get("primitives", []):
                acc = accessors[prim["attributes"]["POSITION"]]
                lo, hi = acc.get("min"), acc.get("max")
                if not lo or not hi:
                    continue
                for x in (lo[0], hi[0]):
                    for y in (lo[1], hi[1]):
                        for z in (lo[2], hi[2]):
                            points.append(tuple(world[r] * x + world[4 + r] * y + world[8 + r] * z + world[12 + r]
                                                for r in range(3)))
        for child in node.get("children", []):
            walk(child, world, depth + 1)

    scene = gltf.get("scenes", [{}])[gltf.get("scene", 0)] if gltf.get("scenes") else {"nodes": range(len(nodes))}
    for root in scene.get("nodes", []):
        walk(root, identity)
    if not points and meshes:   # meshes without nodes
        for mesh in meshes:
            for prim in mesh.get("primitives", []):
                acc = accessors[prim["attributes"]["POSITION"]]
                if acc.get("min") and acc.get("max"):
                    points += [tuple(acc["min"]), tuple(acc["max"])]
    return points


def mesh_frame(path: Path, up: str) -> dict[str, Any]:
    """Centre and bounding radius of a model file, after turning it Y-up (as mesh_nodes does)."""
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".glb":
        points = _glb_points(data)
    elif suffix == ".obj":
        points = []
        for line in data.decode("utf-8", "replace").splitlines():
            if line.startswith("v "):
                parts = line.split()
                if len(parts) >= 4:
                    points.append((float(parts[1]), float(parts[2]), float(parts[3])))
    elif suffix == ".stl":
        if data[:5].lower() == b"solid" and b"facet" in data[:2000]:
            points = [tuple(float(v) for v in m.groups()) for m in
                      re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)]
        else:
            count = struct.unpack("<I", data[80:84])[0]
            points = []
            for i in range(min(count, (len(data) - 84) // 50)):
                base = 84 + i * 50 + 12
                for k in range(3):
                    points.append(struct.unpack("<3f", data[base + k * 12: base + k * 12 + 12]))
    else:
        raise ValueError("unsupported model format")
    if not points:
        raise ValueError("the model has no vertices")
    if up == "z":
        points = [(x, z, -y) for x, y, z in points]
    lo = [min(p[i] for p in points) for i in range(3)]
    hi = [max(p[i] for p in points) for i in range(3)]
    center = [(lo[i] + hi[i]) / 2 for i in range(3)]
    half = [(hi[i] - lo[i]) / 2 for i in range(3)]
    # Orthographic views at yaw 0/90/180/270: the frame must hold the height and the wider horizontal side.
    radius = max(half[1], max(half[0], half[2])) or 1.0
    return {"center": [round(c, 6) for c in center], "radius": round(radius, 6)}


def save_node(job: dict[str, Any], mesh: list) -> dict[str, Any]:
    return {"save": {"class_type": "SaveGLB", "inputs": {"mesh": mesh, "filename_prefix": f"Model3D/{job['id']}"}}}


def find_glb(outputs: dict[str, Any]) -> dict[str, Any] | None:
    for node in outputs.values():
        for items in node.values() if isinstance(node, dict) else ():
            for item in items if isinstance(items, list) else ():
                if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(".glb"):
                    return item
    return None


# ------------------------------------------------------------------ pictures

def prepare_image(raw: bytes) -> Image.Image:
    """Upright, flattened on white, object centred on a square canvas with a margin."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        box = img.getchannel("A").getbbox()
        if box:
            img = img.crop(box)
        flat = Image.new("RGB", img.size, "white")
        flat.paste(img, mask=img.getchannel("A"))
        img = flat
    img = img.convert("RGB")
    side = int(max(img.size) * 1.12)
    canvas = Image.new("RGB", (side, side), "white")
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    if side > 1024:
        canvas = canvas.resize((1024, 1024), Image.LANCZOS)
    return canvas


def object_mask(img: Image.Image) -> Image.Image:
    """White = the object: everything except the near-white background connected to the picture's border."""
    rgb = img.convert("RGB")
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, "white")).convert("L")
    near_white = diff.point(lambda v: 255 if v <= 18 else 0)
    # Flood the near-white pixels that touch the border: that is the background (white parts of the object stay).
    flood = near_white.copy()
    w, h = flood.size
    for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)):
        if flood.getpixel((x, y)) == 255:
            ImageDraw.floodfill(flood, (x, y), 128)
    return flood.point(lambda v: 0 if v == 128 else 255)


def align_to(img: Image.Image, mask: Image.Image, target: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Scale + move the picture so its object's box matches the silhouette's box in `target` (a render mask)."""
    src_box, dst_box = mask.getbbox(), target.getbbox()
    size = target.size
    if not src_box or not dst_box:
        return Image.new("RGB", size, "white"), Image.new("L", size, 0)
    sw, sh = src_box[2] - src_box[0], src_box[3] - src_box[1]
    dw, dh = dst_box[2] - dst_box[0], dst_box[3] - dst_box[1]
    # Fit by height (the axis Hunyuan3D keeps most faithfully); fall back to width for flat, wide objects.
    scale = dh / sh if sh >= sw * 0.6 else dw / sw
    nw, nh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    big, big_mask = img.resize((nw, nh), Image.LANCZOS), mask.resize((nw, nh), Image.NEAREST)
    cx_src, cy_src = (src_box[0] + src_box[2]) / 2 * scale, (src_box[1] + src_box[3]) / 2 * scale
    cx_dst, cy_dst = (dst_box[0] + dst_box[2]) / 2, (dst_box[1] + dst_box[3]) / 2
    offset = (round(cx_dst - cx_src), round(cy_dst - cy_src))
    out, out_mask = Image.new("RGB", size, "white"), Image.new("L", size, 0)
    out.paste(big, offset)
    out_mask.paste(big_mask, offset)
    # Only project where the picture AND the silhouette agree, pulled in a few pixels from the outline:
    # edge texels then take colour grown from the surface instead of the picture's white background.
    out_mask = ImageChops.multiply(out_mask, target.convert("L")).filter(ImageFilter.MinFilter(7))
    return out, out_mask


def overlap(a: Image.Image, b: Image.Image) -> float:
    inter = ImageChops.multiply(a, b).histogram()[255]
    union = ImageChops.lighter(a, b).histogram()[255]
    return inter / union if union else 0.0


def plan_paint(pictures: dict[str, Image.Image], renders: list[Image.Image]) -> tuple[list[Image.Image], list[Image.Image], dict]:
    """One aligned picture + mask per orbit camera. Left/right go on whichever side camera their silhouette
    fits better (the picture conventions differ), the other picture on the other side."""
    targets = [r.convert("L").point(lambda v: 255 if v > 127 else 0) for r in renders]
    slots: dict[str, tuple[Image.Image, Image.Image]] = {}
    note = {}
    for view, orbit_name in (("front", "front"), ("back", "back")):
        if view in pictures:
            pic = pictures[view]
            slots[orbit_name] = align_to(pic, object_mask(pic), targets[ORBIT.index(orbit_name)])
    sides = [v for v in ("left", "right") if v in pictures]
    if sides:
        first = sides[0]
        pic = pictures[first]
        mask = object_mask(pic)
        fits = {}
        for cam in ("yaw90", "yaw270"):
            aligned, amask = align_to(pic, mask, targets[ORBIT.index(cam)])
            fits[cam] = (overlap(amask.point(lambda v: 255 if v > 127 else 0), targets[ORBIT.index(cam)]), aligned, amask)
        best = max(fits, key=lambda c: fits[c][0])
        other = "yaw270" if best == "yaw90" else "yaw90"
        slots[best] = fits[best][1:]
        note[first] = best
        if len(sides) == 2:
            pic2 = pictures[sides[1]]
            slots[other] = align_to(pic2, object_mask(pic2), targets[ORBIT.index(other)])
            note[sides[1]] = other
    images, masks = [], []
    for name in ORBIT:
        img, mask = slots.get(name, (Image.new("RGB", (RENDER, RENDER), "white"), Image.new("L", (RENDER, RENDER), 0)))
        images.append(img)
        masks.append(mask)
    return images, masks, note


# ------------------------------------------------------------------ jobs

def set_stage(job: dict[str, Any], stage: str, progress: float | None = None) -> None:
    job["stage"] = stage
    if progress is not None:
        job["progress"] = round(progress, 3)
    gpu.progress(job.get("_token"), percent=(job.get("progress") or 0) * 100, stage=stage)


async def upload(job_id: str, name: str, data: bytes) -> str:
    async with httpx.AsyncClient(base_url=COMFY, timeout=60) as client:
        r = await client.post("/upload/image", data={"type": "input", "subfolder": "Model3D", "overwrite": "true"},
                              files={"image": (f"{job_id}_{name}", data, "image/png")})
        r.raise_for_status()
        body = r.json()
    return f"{body['subfolder']}/{body['name']}" if body.get("subfolder") else body["name"]


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def run_prompt(job: dict[str, Any], workflow: dict[str, Any], stage: str, start: float, span: float,
                     expected: float) -> dict[str, Any]:
    result = await comfy("POST", "/prompt", json={"prompt": workflow, "client_id": uuid.uuid4().hex})
    prompt_id = result["prompt_id"]
    job["_prompt"] = prompt_id
    began = time.time()
    try:
        while True:
            await asyncio.sleep(1.5)
            hist = await comfy("GET", f"/history/{prompt_id}", timeout=15)
            entry = hist.get(prompt_id) if isinstance(hist, dict) else None
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    msgs = [m[1] for m in status.get("messages", []) if m[0] == "execution_error"]
                    err = msgs[0] if msgs else {}
                    raise RuntimeError(f"{err.get('node_type', 'ComfyUI')}: {err.get('exception_message', 'render failed')}".strip())
                if status.get("completed") or entry.get("outputs"):
                    return entry.get("outputs", {})
            # No per-step events without a websocket: estimate from elapsed time.
            frac = min(0.97, (time.time() - began) / max(5.0, expected))
            set_stage(job, stage, start + span * frac)
    finally:
        job.pop("_prompt", None)


async def fetch_output(item: dict[str, Any]) -> bytes:
    async with httpx.AsyncClient(base_url=COMFY, timeout=120) as client:
        r = await client.get("/view", params={"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                                              "type": item.get("type", "output")})
        r.raise_for_status()
        return r.content


async def studio_image(job: dict[str, Any], text: str, model: str, seed: int, reference: Path | None,
                       stage: str, progress: tuple[float, float], negative: str = "") -> bytes:
    """One picture from the Image studio as a hidden job (its own GPU turn); with a reference picture,
    Qwen Image 2.1 edits/redraws it. Returns the image bytes; the hidden job is removed afterwards."""
    headers = {"X-Queue-Internal-Secret": SECRET}
    request = {"model": model, "prompt": text[:4000], "width": 1024, "height": 1024,
               "seed": seed % (2**31), "count": 1, "enhance": False}
    if negative:
        request["negative_prompt"] = negative[:4000]
    lo, span = progress
    set_stage(job, f"{stage} (Image studio)", lo)
    async with httpx.AsyncClient(base_url=IMAGE_STUDIO, timeout=60) as client:
        if reference is not None:
            up = await client.post("/api/upload", files={"file": (f"m3d-{job['id']}-{reference.stem}.png", reference.read_bytes(), "image/png")})
            if up.status_code >= 400:
                raise RuntimeError(f"The Image studio refused the reference picture: {up.text[:300]}")
            request.update(use_references=True, match_reference_size=False, reference_resolution=1024,
                           references=[{"enabled": True, "image": up.json()["image"], "strength": 1.0,
                                        "width": 1024, "height": 1024}])
        r = await client.post("/api/internal/generate", headers=headers, json={"request": request, "under_lock": True})
        if r.status_code >= 400:
            raise RuntimeError(f"The Image studio refused the prompt: {r.text[:400]}")
        image_job = r.json()["id"]
        job["_image_job"] = image_job
        try:
            while True:
                await asyncio.sleep(1.0)
                try:
                    state = (await client.get(f"/api/jobs/{image_job}", timeout=10)).json()
                except (httpx.HTTPError, ValueError):
                    continue
                st = state.get("state")
                if st in ("complete", "completed"):
                    break
                if st in ("failed", "cancelled"):
                    raise RuntimeError(f"The Image studio's picture {st}: {state.get('error') or 'no reason given'}")
                label = "Waiting for the GPU (picture)" if st in ("waiting_for_gpu", "queued", "preparing") else stage
                set_stage(job, label, lo + span * min(1.0, float(state.get("progress") or 0) / 100))
            images = state.get("images") or [{"index": 0}]
            dl = await client.get(f"/api/outputs/{image_job}/download", params={"index": images[0].get("index", 0)}, timeout=120)
            dl.raise_for_status()
            return dl.content
        except BaseException:
            try:
                await client.post(f"/api/jobs/{image_job}/cancel", timeout=5)
            except httpx.HTTPError:
                pass
            raise
        finally:
            job.pop("_image_job", None)
            for _ in range(20):   # the hidden job and its file leave the Image studio (409 = still winding down)
                try:
                    gone = await client.delete(f"/api/internal/jobs/{image_job}", headers=headers, timeout=5)
                    if gone.status_code != 409:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)


async def draw_picture(job: dict[str, Any], reference: Path | None = None) -> None:
    """front.png: from the text prompt, or, with a reference picture, an edit of it."""
    s = job["settings"]
    if job.get("kind") == "edit":
        text, stage = EDIT_PROMPT.format(instruction=job["instruction"].rstrip(". ")), "Editing the picture"
    elif reference is not None:
        text, stage = REPAINT_PROMPT.format(prompt=job["prompt"].rstrip(". ")), "Drawing the picture"
    else:
        text = (CHARACTER_PROMPT.format(prompt=job["prompt"].rstrip(". ")) if s.get("character")
                else job["prompt"] + PROMPT_SUFFIX)
        stage = "Drawing the picture"
    negative = CHARACTER_NEGATIVE if s.get("character") else ""
    raw = await studio_image(job, text, EDIT_IMAGE_MODEL if reference is not None else s["image_model"],
                             s["image_seed"], reference, stage, (0.01, 0.06), negative)
    picture = await asyncio.to_thread(prepare_image, raw)
    picture.save(job_dir(job["id"]) / "front.png")
    job["inputs"] = {"front": "front.png"}
    save_job(job)


async def draw_views(job: dict[str, Any]) -> None:
    """Full texture: the Image studio redraws the front picture from behind and from both sides
    (Qwen Image 2.1, the front as reference), so painting has a real picture for every side."""
    folder = job_dir(job["id"])
    what = "character" if job["settings"].get("character") else "object"
    todo = [v for v in ("back", "left", "right") if not (folder / f"{v}.png").exists()]
    for i, view in enumerate(todo):
        text = VIEW_PROMPTS[view].format(what=what)
        raw = await studio_image(job, text, EDIT_IMAGE_MODEL, job["settings"]["image_seed"] + 101 * (i + 1),
                                 folder / "front.png", f"Drawing the {view} view", (0.07 + 0.03 * i, 0.03),
                                 CHARACTER_NEGATIVE.replace("side view, back view, ", "") if what == "character" else "")
        picture = await asyncio.to_thread(prepare_image, raw)
        picture.save(folder / f"{view}.png")
        job.setdefault("inputs", {})[view] = f"{view}.png"
        save_job(job)
    for view in ("back", "left", "right"):
        if (folder / f"{view}.png").exists():
            job["inputs"][view] = f"{view}.png"


async def upload_mesh(job: dict[str, Any]) -> str:
    """The job's model file into ComfyUI's input/3d (where Load 3D reads), returning its annotated name."""
    path = job_dir(job["id"]) / job["mesh"]
    async with httpx.AsyncClient(base_url=COMFY, timeout=300) as client:
        r = await client.post("/upload/image", data={"type": "input", "subfolder": "3d", "overwrite": "true"},
                              files={"image": (f"m3d_{job['id']}{path.suffix.lower()}", path.read_bytes(), "application/octet-stream")})
        r.raise_for_status()
        body = r.json()
    return f"{body.get('subfolder') or '3d'}/{body['name']}"


async def render_reference(job: dict[str, Any]) -> None:
    """Front view of an uploaded model (plain clay render) for the Image studio to paint over."""
    set_stage(job, "Waiting for the GPU (front view)", 0.0)
    async with gpu.lease(job["id"] + "-view", f"3D Models: front view of {job['title']}", uses_llm=False) as turn:
        job["_token"] = turn.token
        try:
            await ensure_comfy(job)
            wf = {**mesh_nodes(job, await upload_mesh(job)), **paint_prep_nodes(job)}
            wf["view"] = {"class_type": "RenderMesh", "inputs": {
                "mesh": ["mesh", 0], "mode": "solid", "width": RENDER, "height": RENDER, "background": "#ffffff",
                "camera_info": ["cam", 0]}}
            wf["view_out"] = {"class_type": "PreviewImage", "inputs": {"images": ["view", 0]}}
            for name in ("decimate", "unwrap", "orbit"):
                wf.pop(name)
            outputs = await run_prompt(job, wf, "Rendering the front view", 0.0, 0.03, 15)
            shots = (outputs.get("view_out") or {}).get("images") or []
            if not shots:
                raise RuntimeError("The model's front view didn't render.")
            (job_dir(job["id"]) / "reference.png").write_bytes(await fetch_output(shots[0]))
        finally:
            job.pop("_token", None)


async def run_job(job_id: str) -> None:
    job = jobs[job_id]
    folder = job_dir(job_id)
    try:
        # 1. The picture first, before this job queues for its own GPU turn (the Image studio takes its
        #    own turn; holding ours meanwhile would deadlock the queue).
        if not (folder / "front.png").exists():
            if job.get("kind") == "edit":
                await draw_picture(job, folder / "original.png")
            elif job.get("mesh"):
                if not (folder / "reference.png").exists():
                    await render_reference(job)      # a short GPU turn of our own, released before drawing
                await draw_picture(job, folder / "reference.png")
            elif job.get("prompt"):
                await draw_picture(job)
        s = job["settings"]
        if s.get("all_sides") and s.get("paint"):
            await draw_views(job)              # also Image studio turns, so still before our own
        label = f"3D Models: {job['title']}"
        set_stage(job, "Waiting for the GPU", job.get("progress") or 0.0)
        async with gpu.lease(job_id, label, uses_llm=False, model=job["model"],
                             config=f"oct{s['octree']}{' paint' if s['paint'] else ''}") as turn:
            job["_token"] = turn.token
            job.update(status="running", started=time.time())
            base = 0.1 if job.get("prompt") or job.get("kind") == "edit" else 0.0
            set_stage(job, "Preparing", base + 0.02)
            save_job(job)
            await ensure_comfy(job)
            if job.get("mesh"):
                # Keep the shape (an uploaded model, or the model being edited): paint the loaded mesh.
                outputs = await paint(job, mesh_nodes(job, await upload_mesh(job)), base, 0.0)
            elif job.get("engine") in TEXTURED_ENGINES:
                front = await upload(job_id, "front.png", (folder / "front.png").read_bytes())
                outputs = await run_prompt(job, {**trellis_nodes(job, front), **save_node(job, ["textured", 0])},
                                           f"{TEXTURED_ENGINES[job['engine']].split(' (')[0]}: shaping and texturing",
                                           base + 0.04, 0.94 - base, 260)
            else:
                uploaded = {view: await upload(job_id, name, (folder / name).read_bytes()) for view, name in job["inputs"].items()}
                shape = shape_nodes(job, uploaded)
                shape_time = s["steps"] * 1.1 + 25
                if s["paint"]:
                    outputs = await paint(job, shape, base, shape_time)
                else:
                    outputs = await run_prompt(job, {**shape, **save_node(job, ["mesh", 0])}, "Shaping the model",
                                               base + 0.04, 0.94 - base, shape_time)
            item = find_glb(outputs)
            if not item:
                raise RuntimeError("ComfyUI finished but saved no model. The picture may not show a clear object.")
            set_stage(job, "Saving", 0.98)
            data = await fetch_output(item)
            job.pop("rig", None)
            job.pop("rig_error", None)
            if s.get("rig"):
                set_stage(job, "Adding the skeleton", 0.985)
                try:
                    (folder / "static.glb").write_bytes(data)
                    if job.get("rig_method") == "magicarticulate":
                        (folder / "skeleton.json").unlink(missing_ok=True)
                        await ai_skeleton(job, folder / "static.glb")     # we hold the GPU turn already
                    data = await rig_model(job, folder / "static.glb")
                except rig.RigError as exc:
                    job["rig_error"] = f"No skeleton: {exc}."
                except Exception as exc:  # noqa: BLE001 - the model itself is fine; only the rig failed
                    log.exception("rig failed for %s", job_id)
                    job["rig_error"] = f"No skeleton: the auto-rig failed ({exc})."
            (folder / "model.glb").write_bytes(data)
            job.update(status="done", stage="", progress=1.0, finished=time.time(), size=len(data), error=None)
    except LeaseCancelled:
        job.update(status="cancelled", stage="")
    except asyncio.CancelledError:
        job.update(status="cancelled", stage="")
        if job.get("_prompt"):
            await interrupt(job["_prompt"])
    except LockUnavailable:
        job.update(status="error", stage="", error="The GPU queue isn't reachable, so the job couldn't be queued.")
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:600]
        job.update(status="error", stage="", error=f"ComfyUI said HTTP {exc.response.status_code}: {detail}")
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s failed", job_id)
        job.update(status="error", stage="", error=str(exc) or exc.__class__.__name__)
    finally:
        job.pop("_token", None)
        job.pop("_prompt", None)
        tasks.pop(job_id, None)
        save_job(job)


async def paint(job: dict[str, Any], shape: dict[str, Any], base: float, shape_time: float) -> dict[str, Any]:
    folder = job_dir(job["id"])
    prep = paint_prep_nodes(job)
    # Pass A: shape + unwrap + the mesh's silhouette from the 4 orbit cameras.
    pass_a = {**shape, **prep,
              "render": {"class_type": "RenderMesh", "inputs": {
                  "mesh": ["unwrap", 0], "mode": "solid", "width": RENDER, "height": RENDER, "background": "#000000",
                  "camera_info": ["orbit", 0]}},
              "render_mask": {"class_type": "MaskToImage", "inputs": {"mask": ["render", 1]}},
              "render_out": {"class_type": "PreviewImage", "inputs": {"images": ["render_mask", 0]}},
              # ComfyUI runs with --cache-none: keep the unwrapped mesh as a file so pass B loads it
              # instead of generating the whole shape again.
              "save_unwrap": {"class_type": "SaveGLB", "inputs": {"mesh": ["unwrap", 0],
                                                                  "filename_prefix": f"Model3D/{job['id']}_unwrap"}}}
    outputs = await run_prompt(job, pass_a, "Shaping the model" if shape_time else "Preparing the mesh",
                               base + 0.04, 0.6 - base, shape_time + 20)
    shots = (outputs.get("render_out") or {}).get("images") or []
    if len(shots) != len(ORBIT):
        raise RuntimeError("Painting failed: the mesh's silhouettes didn't render.")
    renders = [Image.open(io.BytesIO(await fetch_output(shot))) for shot in shots]
    unwrap_item = find_glb({"save_unwrap": outputs.get("save_unwrap") or {}})
    unwrap_name = None
    if unwrap_item:
        data = await fetch_output(unwrap_item)
        (folder / "unwrap.glb").write_bytes(data)
        async with httpx.AsyncClient(base_url=COMFY, timeout=300) as client:
            r = await client.post("/upload/image", data={"type": "input", "subfolder": "3d", "overwrite": "true"},
                                  files={"image": (f"m3d_unwrap_{job['id']}.glb", data, "application/octet-stream")})
            r.raise_for_status()
            body = r.json()
        unwrap_name = f"{body.get('subfolder') or '3d'}/{body['name']}"
    pictures = {view: Image.open(folder / name).convert("RGB") for view, name in job["inputs"].items()}
    set_stage(job, "Lining up the pictures", 0.62)
    images, masks, note = await asyncio.to_thread(plan_paint, pictures, renders)
    if note:
        job["paint_sides"] = note
    names_img, names_mask = [], []
    for i, (img, mask) in enumerate(zip(images, masks)):
        names_img.append(await upload(job["id"], f"paint_{i}.png", png(img)))
        names_mask.append(await upload(job["id"], f"paintmask_{i}.png", png(mask.convert("RGB"))))
    # Pass B: bake the pictures onto the UV texture of the unwrapped mesh saved by pass A (or, if that
    # file is missing, the same shape/unwrap nodes again: same seed, same mesh, just slower).
    s = job["settings"]
    if unwrap_name:
        pass_b = {"cam": prep["cam"], "orbit": prep["orbit"],
                  "unwrap_file": {"class_type": "Load3DAdvanced", "inputs": {
                      "model_file": unwrap_name, "viewport_state": {}, "width": RENDER, "height": RENDER}},
                  "unwrap": {"class_type": "Get3DComponents", "inputs": {"model_3d": ["unwrap_file", 0]}}}
    else:
        pass_b = {**shape, **prep}

    def batch(kind: str, names: list[str]) -> list:
        """LoadImage each file, joined in orbit order with ImageBatch; returns the batch's node link."""
        for i, name in enumerate(names):
            pass_b[f"{kind}{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        link = [f"{kind}0", 0]
        for i in range(1, len(names)):
            pass_b[f"{kind}batch{i}"] = {"class_type": "ImageBatch", "inputs": {"image1": link, "image2": [f"{kind}{i}", 0]}}
            link = [f"{kind}batch{i}", 0]
        return link

    images_ref, masks_ref = batch("img", names_img), batch("msk", names_mask)
    pass_b["mask_all"] = {"class_type": "ImageToMask", "inputs": {"image": masks_ref, "channel": "red"}}
    pass_b["bake"] = {"class_type": "BakeTextureFromViews", "inputs": {
        "mesh": ["unwrap", 0], "images": images_ref, "resolution": s["texture"], "blend_exponent": 4.0,
        "fill_unseen": True, "camera_info": ["orbit", 0], "mask": ["mask_all", 0]}}
    pass_b["textured"] = {"class_type": "ApplyTextureToMesh", "inputs": {"mesh": ["unwrap", 0], "base_color": ["bake", 0]}}
    pass_b.update(save_node(job, ["textured", 0]))
    return await run_prompt(job, pass_b, "Painting the model", 0.64, 0.34, 40)


async def interrupt(prompt_id: str) -> None:
    try:
        await comfy("POST", "/queue", json={"delete": [prompt_id]}, timeout=5)
        queue = await comfy("GET", "/queue", timeout=5)
        if any(item[1] == prompt_id for item in queue.get("queue_running", [])):
            await comfy("POST", "/interrupt", json={"prompt_id": prompt_id}, timeout=5)
    except (httpx.HTTPError, ValueError, IndexError, TypeError):
        pass


def on_queue_cancel(job_id: str, entry_id: str) -> Any:
    """The Queue page's Cancel. Waiting jobs are left to the client (None); a running one is stopped."""
    task = tasks.get(job_id)
    job = jobs.get(job_id)
    if task and job and job.get("status") == "running":
        task.cancel()
        return True
    return None


gpu.on_cancel = on_queue_cancel


def start(job_id: str) -> None:
    tasks[job_id] = asyncio.create_task(run_job(job_id))


# ------------------------------------------------------------------ routes

@app.on_event("startup")
async def startup() -> None:
    load_jobs()
    gpu.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    await gpu.stop()


@app.get(f"{PREFIX}/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": VERSION, "queue": gpu.connected}


@app.get(f"{PREFIX}/api/version")
async def version() -> dict[str, str]:
    return {"version": VERSION}


@app.get(f"{PREFIX}/api/models")
async def models() -> JSONResponse:
    try:
        return JSONResponse(await available_models())
    except (httpx.HTTPError, KeyError, ValueError):
        return JSONResponse({"models": [], "rembg": False, "paint": False, "image_models": await image_models(),
                             "error": "ComfyUI isn't answering right now."})


@app.get(f"{PREFIX}/api/jobs")
async def list_jobs() -> list[dict[str, Any]]:
    """Newest first, as a plain list like the other studios (gateway home/notify, hostctl busy check)."""
    ordered = sorted(jobs.values(), key=lambda j: j["created"], reverse=True)
    return [public(j) for j in ordered]


@app.get(f"{PREFIX}/api/jobs/{{job_id}}")
async def get_job(job_id: str) -> dict[str, Any]:
    if job_id not in jobs:
        raise HTTPException(404, "No such job")
    return public(jobs[job_id])


@app.post(f"{PREFIX}/api/jobs")
async def create_job(
    model: str = Form(...), title: str = Form(""), prompt: str = Form(""), image_model: str = Form("krea2"),
    steps: int = Form(30), cfg: float = Form(5.0),
    seed: int = Form(-1), octree: int = Form(256), threshold: float = Form(0.6), latent_tokens: int = Form(3072),
    remove_background: bool = Form(False), paint: bool = Form(True), texture: int = Form(2048), faces: int = Form(150000),
    front: UploadFile | None = File(None), left: UploadFile | None = File(None),
    back: UploadFile | None = File(None), right: UploadFile | None = File(None),
    mesh: UploadFile | None = File(None), mesh_up: str = Form("y"),
    character: bool = Form(False), all_sides: bool = Form(False), rigging: bool = Form(False),
    rig_method: str = Form("builtin"),
) -> dict[str, Any]:
    try:
        info = await available_models()
    except (httpx.HTTPError, KeyError, ValueError):
        info = {"models": [{"name": model, "multiview": "mv" in model.lower(), "label": model}],
                "rembg": False, "paint": False, "image_models": FALLBACK_IMAGE_MODELS}
    chosen = next((m for m in info["models"] if m["name"] == model), None)
    if not chosen:
        raise HTTPException(400, "That model isn't in ComfyUI's checkpoints folder.")
    repaint = mesh is not None and bool(mesh.filename)
    files = {"front": front, "left": left, "back": back, "right": right}
    if not chosen["multiview"] and not repaint:
        files = {"front": front}
    files = {v: f for v, f in files.items() if f is not None and f.filename}
    prompt = " ".join(prompt.split())[:1500]
    if "front" not in files and not prompt:
        raise HTTPException(400, "Add a picture or type a prompt.")
    if repaint and not info.get("paint"):
        raise HTTPException(400, "This ComfyUI has no texturing nodes, so models can't be repainted.")
    if repaint and Path(mesh.filename).suffix.lower() not in MESH_EXT:
        raise HTTPException(400, "Upload the model as .glb, .obj or .stl (convert FBX/USDZ to GLB first).")
    if "front" not in files:
        files = {}   # prompt mode: side pictures without a front can't be lined up with anything
        if not repaint and image_model not in {m["key"] for m in info["image_models"]}:
            raise HTTPException(400, "Pick one of the Image studio's models for the prompt.")
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    folder = job_dir(job_id)
    folder.mkdir(parents=True)
    inputs = {}
    try:
        for view, upload_file in files.items():
            raw = await upload_file.read(MAX_UPLOAD + 1)
            if len(raw) > MAX_UPLOAD:
                raise HTTPException(413, f"The {view} picture is over 25 MB.")
            try:
                image = await asyncio.to_thread(prepare_image, raw)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, f"The {view} picture couldn't be read as an image.") from exc
            image.save(folder / f"{view}.png")
            inputs[view] = f"{view}.png"
        frame = None
        if repaint:
            raw = await mesh.read(MAX_MESH + 1)
            if len(raw) > MAX_MESH:
                raise HTTPException(413, "The model file is over 200 MB.")
            mesh_name = "source" + Path(mesh.filename).suffix.lower()
            (folder / mesh_name).write_bytes(raw)
            try:
                frame = await asyncio.to_thread(mesh_frame, folder / mesh_name, "z" if mesh_up == "z" else "y")
            except (ValueError, KeyError, IndexError, struct.error, UnicodeError) as exc:
                raise HTTPException(400, f"That model file couldn't be read: {exc}") from exc
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    use_prompt = not inputs
    default_title = Path(mesh.filename).stem if repaint else prompt[:60] if use_prompt else Path(files["front"].filename).stem
    job = {
        "id": job_id, "created": time.time(), "status": "queued", "stage": "Waiting for the GPU", "progress": 0.0,
        "title": (title.strip() or default_title)[:80], "prompt": prompt if use_prompt else "",
        "model": model, "model_label": chosen.get("label", model), "multiview": chosen["multiview"], "inputs": inputs,
        "settings": {
            "steps": max(5, min(100, steps)), "cfg": max(1.0, min(15.0, cfg)),
            "seed": seed if seed >= 0 else random.randint(0, 2**31 - 1),
            "octree": octree if octree in (128, 256, 384, 512) else 256,
            "threshold": max(-1.0, min(1.0, threshold)),
            "latent_tokens": latent_tokens if latent_tokens in (2048, 3072, 4096) else 3072,
            "remove_background": bool(remove_background and info.get("rembg")),
            "paint": bool((paint or repaint) and info.get("paint")),
            "texture": texture if texture in (1024, 2048, 4096) else 2048,
            "faces": max(20000, min(500000, faces)),
            "image_model": (EDIT_IMAGE_MODEL if repaint else image_model) if use_prompt else None,
            "character": bool(character),
            # Full texture from drawn side views: needs a front picture to redraw (not for uploads/multi-view input).
            "all_sides": bool(all_sides and not repaint and len(files) <= 1),
            "rig": bool(rigging or character),
            "image_seed": random.randint(0, 2**31 - 1) if (use_prompt or all_sides) else None,
        },
    }
    job["engine"] = chosen.get("engine", "hunyuan")
    job["rig_method"] = rig_method if rig_method in info.get("rig_methods", {"builtin": 1}) else "builtin"
    if chosen.get("textured") and not repaint:
        # These engines texture the model themselves: no projection painting or drawn side views.
        job["settings"].update(paint=False, all_sides=False, native_texture=True)
    if repaint:
        job.update(kind="repaint", mesh=mesh_name, mesh_up="z" if mesh_up == "z" else "y", frame=frame,
                   model_label="Repaint (your model)")
    jobs[job_id] = job
    save_job(job)
    start(job_id)
    return public(job)


@app.post(f"{PREFIX}/api/jobs/{{job_id}}/edit")
async def edit_job(job_id: str, instruction: str = Form(...), keep_shape: bool = Form(False)) -> dict[str, Any]:
    """A new job from a finished one: the Image studio edits its picture from the instruction, then the colours
    are repainted onto the same mesh (keep_shape) or the model is rebuilt from the edited picture."""
    parent = jobs.get(job_id)
    if not parent:
        raise HTTPException(404, "No such job")
    src = job_dir(job_id)
    if parent.get("status") != "done" or not (src / "front.png").exists():
        raise HTTPException(409, "Only finished models with a picture can be edited.")
    instruction = " ".join(instruction.split())[:800]
    if not instruction:
        raise HTTPException(400, "Say what to change.")
    if keep_shape and not (src / "model.glb").exists():
        raise HTTPException(409, "That model's file is missing.")
    new_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    folder = job_dir(new_id)
    folder.mkdir(parents=True)
    shutil.copyfile(src / "front.png", folder / "original.png")
    settings = {**parent["settings"], "image_seed": random.randint(0, 2**31 - 1), "paint": True if keep_shape else parent["settings"]["paint"]}
    job = {
        "id": new_id, "created": time.time(), "status": "queued", "stage": "Waiting for the GPU", "progress": 0.0,
        "title": f"{parent['title']} (edited)"[:80], "prompt": "", "kind": "edit", "parent": job_id,
        "instruction": instruction, "keep_shape": bool(keep_shape),
        "model": parent["model"], "model_label": parent.get("model_label", parent["model"]),
        "engine": parent.get("engine", "hunyuan"),
        "multiview": parent.get("multiview", False), "inputs": {}, "settings": settings,
    }
    if keep_shape:
        shutil.copyfile(src / "model.glb", folder / "source.glb")
        job.update(mesh="source.glb", mesh_up="y", frame=await asyncio.to_thread(mesh_frame, folder / "source.glb", "y"))
    jobs[new_id] = job
    save_job(job)
    start(new_id)
    return public(job)


REDO_FILES = ("front.png", "left.png", "back.png", "right.png", "original.png", "reference.png", "physics_mask.png",
              "source.glb", "source.obj", "source.stl")


@app.post(f"{PREFIX}/api/jobs/{{job_id}}/redo")
async def redo_job(job_id: str, new_seed: bool = Form(True), new_picture: bool = Form(False),
                   octree: int | None = Form(None), steps: int | None = Form(None), cfg: float | None = Form(None),
                   paint: bool | None = Form(None), texture: int | None = Form(None)) -> dict[str, Any]:
    """Make a finished (or failed) model again as a new job: same pictures / prompt / instruction / model file,
    optionally a new shape seed, a newly drawn picture, and changed settings."""
    parent = jobs.get(job_id)
    if not parent:
        raise HTTPException(404, "No such job")
    if job_id in tasks:
        raise HTTPException(409, "It's still running.")
    src = job_dir(job_id)
    drawn = bool(parent.get("prompt")) or parent.get("kind") == "edit"   # its picture came from the Image studio
    redraw = bool(new_picture and drawn)
    new_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    folder = job_dir(new_id)
    folder.mkdir(parents=True)
    s_all = bool(parent["settings"].get("all_sides"))   # drawn side views belong to the old picture
    for name in REDO_FILES:
        if (src / name).exists() and not (redraw and name in ("front.png", "back.png", "left.png", "right.png") and (name == "front.png" or s_all)):
            shutil.copyfile(src / name, folder / name)
    s = dict(parent["settings"])
    if new_seed:
        s["seed"] = random.randint(0, 2**31 - 1)
    if redraw:
        s["image_seed"] = random.randint(0, 2**31 - 1)
    if octree in (128, 256, 384, 512):
        s["octree"] = octree
    if steps is not None:
        s["steps"] = max(5, min(100, steps))
    if cfg is not None:
        s["cfg"] = max(1.0, min(15.0, cfg))
    if paint is not None and not parent.get("mesh"):
        s["paint"] = bool(paint)
    if texture in (1024, 2048, 4096):
        s["texture"] = texture
    keep = ("prompt", "kind", "parent", "instruction", "keep_shape", "model", "model_label", "multiview",
            "mesh", "mesh_up", "frame", "physics", "engine", "rig_method")
    job = {k: parent[k] for k in keep if k in parent}
    title = re.sub(r" \(redo( \d+)?\)$", "", parent["title"])
    job.update(id=new_id, created=time.time(), status="queued", stage="Waiting for the GPU", progress=0.0,
               title=f"{title} (redo)"[:80], settings=s, redo_of=job_id,
               inputs={} if redraw else {k: v for k, v in parent.get("inputs", {}).items() if (folder / v).exists()})
    jobs[new_id] = job
    save_job(job)
    start(new_id)
    return public(job)


def physics_spec(job: dict[str, Any]) -> list[dict]:
    """The job's saved loose parts, ready for rig.rig (the brush mask is read back from its PNG)."""
    out = []
    folder = job_dir(job["id"])
    for part in job.get("physics") or []:
        spec = {k: part.get(k) for k in ("kind", "material", "stiffness", "gravity", "drag", "name")}
        if part.get("kind") == "brush":
            mask_path = folder / "physics_mask.png"
            front = folder / "front.png"
            if not mask_path.exists() or not front.exists():
                continue
            mask = Image.open(mask_path).convert("L")
            pic = Image.open(front).convert("RGB")
            if mask.size != pic.size:
                mask = mask.resize(pic.size, Image.NEAREST)
            box = object_mask(pic).getbbox()
            if not box:
                continue
            import numpy as np   # noqa: PLC0415 - only needed with a brush
            spec["mask"] = np.asarray(mask) > 127
            spec["box"] = box
        out.append(spec)
    return out


async def ai_skeleton(job: dict[str, Any], static: Path) -> dict:
    """MagicArticulate skeleton for the plain model, through the ComfyUI node. Call while holding a GPU turn."""
    folder = job_dir(job["id"])
    async with httpx.AsyncClient(base_url=COMFY, timeout=300) as client:
        r = await client.post("/upload/image", data={"type": "input", "subfolder": "3d", "overwrite": "true"},
                              files={"image": (f"m3d_rig_{job['id']}.glb", static.read_bytes(), "application/octet-stream")})
        r.raise_for_status()
        body = r.json()
    name = f"{body.get('subfolder') or '3d'}/{body['name']}"
    wf = {"skeleton": {"class_type": AI_RIG_NODE, "inputs": {"model_file": name, "seed": job["settings"]["seed"] % 100000,
                                                              "points": 8192}}}
    outputs = await run_prompt(job, wf, "Adding the skeleton (MagicArticulate)", job.get("progress") or 0.9, 0.05, 20)
    skel = ((outputs.get("skeleton") or {}).get("skeleton") or [None])[0]
    if not isinstance(skel, dict) or not skel.get("joints"):
        raise rig.RigError("MagicArticulate returned no skeleton")
    (folder / "skeleton.json").write_text(json.dumps(skel))
    return skel


async def rig_model(job: dict[str, Any], static: Path) -> bytes:
    """Rig the plain model (built-in humanoid rig with the job's physics parts, or the job's MagicArticulate
    skeleton from skeleton.json); updates job["rig"]."""
    if job.get("rig_method") == "magicarticulate":
        skel_path = job_dir(job["id"]) / "skeleton.json"
        if not skel_path.exists():
            raise rig.RigError("no AI skeleton yet")
        data, report = await asyncio.to_thread(rig.rig_ai, static.read_bytes(), json.loads(skel_path.read_text()))
        if job.get("physics"):
            report["notes"] = ["Physics needs the built-in humanoid rig; it isn't applied to AI skeletons."]
    else:
        data, report = await asyncio.to_thread(rig.rig, static.read_bytes(), physics_spec(job))
    job["rig"] = report
    job.pop("rig_error", None)
    return data


async def ai_rig_task(job_id: str) -> None:
    """Background: a GPU turn for the MagicArticulate skeleton, then skinning + clips (CPU)."""
    job = jobs[job_id]
    folder = job_dir(job_id)
    try:
        async with gpu.lease(job_id + "-rig", f"3D Models: skeleton for {job['title']}", uses_llm=False) as turn:
            job["_token"] = turn.token
            set_stage(job, "Adding the skeleton (MagicArticulate)", 0.2)
            await ensure_comfy(job)
            await ai_skeleton(job, folder / "static.glb")
        job.pop("_token", None)
        set_stage(job, "Skinning", 0.9)
        data = await rig_model(job, folder / "static.glb")
        (folder / "model.glb").write_bytes(data)
        job.update(size=len(data), finished=time.time())
        job["settings"]["rig"] = True
    except (LeaseCancelled, asyncio.CancelledError):
        job["rig_error"] = "Rigging was cancelled."
    except Exception as exc:  # noqa: BLE001 - the model itself is fine
        log.exception("AI rig failed for %s", job_id)
        job["rig_error"] = f"No AI skeleton: {exc}"
    finally:
        job.pop("_token", None)
        job.pop("_prompt", None)
        job.update(status="done", stage="", progress=1.0)
        tasks.pop(job_id, None)
        save_job(job)


@app.post(f"{PREFIX}/api/jobs/{{job_id}}/rig")
async def rig_job(job_id: str, method: str = Form("")) -> dict[str, Any]:
    """Add (or redo) the skeleton and preview animations on a finished model, from its un-rigged copy.
    method: builtin (CPU, a few seconds) or magicarticulate (a GPU turn; runs in the background)."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    if job.get("status") != "done" or job_id in tasks:
        raise HTTPException(409, "Only finished models can be rigged.")
    folder = job_dir(job_id)
    static = folder / "static.glb"
    if not static.exists():
        shutil.copyfile(folder / "model.glb", static)   # never rigged yet: keep the plain model
    if method in RIG_METHODS:
        job["rig_method"] = method
    if job.get("rig_method") == "magicarticulate":
        if not await node_exists(AI_RIG_NODE):
            raise HTTPException(409, "MagicArticulate isn't installed in ComfyUI.")
        job.pop("rig_error", None)
        job.update(status="running", stage="Waiting for the GPU (skeleton)", progress=0.05)
        save_job(job)
        tasks[job_id] = asyncio.create_task(ai_rig_task(job_id))
        return public(job)
    try:
        data = await rig_model(job, static)
    except rig.RigError as exc:
        job["rig_error"] = f"No skeleton: {exc}."
        save_job(job)
        raise HTTPException(422, job["rig_error"]) from exc
    (folder / "model.glb").write_bytes(data)
    job.update(size=len(data), finished=time.time())
    job["settings"]["rig"] = True
    save_job(job)
    return public(job)


@app.get(f"{PREFIX}/api/physics/options")
async def physics_options() -> dict[str, Any]:
    return {"presets": physics.PRESETS, "materials": {k: v["label"] for k, v in physics.MATERIALS.items()},
            "defaults": {k: {x: v[x] for x in ("stiffness", "gravity", "drag")} for k, v in physics.MATERIALS.items()}}


@app.post(f"{PREFIX}/api/jobs/{{job_id}}/physics")
async def physics_job(job_id: str, parts: str = Form("[]"), mask: UploadFile | None = File(None)) -> dict[str, Any]:
    """Set the loose parts that swing (presets and/or a brush painted on the front picture) and re-rig.
    parts: JSON list of {kind: skirt|cape|hair|brush, material, stiffness?, gravity?, drag?}. [] removes physics.
    CPU only (about 5-20 s), no GPU turn."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    if job.get("status") != "done" or job_id in tasks:
        raise HTTPException(409, "Only finished models can get physics.")
    try:
        wanted = json.loads(parts)
        assert isinstance(wanted, list) and len(wanted) <= 6
    except (ValueError, AssertionError) as exc:
        raise HTTPException(400, "parts must be a JSON list (at most 6).") from exc
    clean = []
    for part in wanted:
        kind = part.get("kind")
        if kind not in (*physics.PRESETS, "brush"):
            raise HTTPException(400, f"Unknown part kind: {kind}")
        item = {"kind": kind, "material": part.get("material") if part.get("material") in physics.MATERIALS else "cloth"}
        for key in ("stiffness", "gravity", "drag"):
            if part.get(key) not in (None, ""):
                item[key] = max(0.0, min(1.0, float(part[key])))
        clean.append(item)
    folder = job_dir(job_id)
    if any(p["kind"] == "brush" for p in clean):
        if mask is not None and mask.filename:
            raw = await mask.read(10 * 1024 * 1024 + 1)
            try:
                Image.open(io.BytesIO(raw)).convert("L").save(folder / "physics_mask.png")
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, "The brush mask couldn't be read.") from exc
        if not (folder / "physics_mask.png").exists():
            raise HTTPException(400, "Paint over the moving parts first.")
        if not (folder / "front.png").exists():
            raise HTTPException(400, "This model has no front picture to paint on.")
    static = folder / "static.glb"
    if not static.exists():
        shutil.copyfile(folder / "model.glb", static)
    previous = job.get("physics")
    job["physics"] = clean
    try:
        data = await rig_model(job, static)
    except rig.RigError as exc:
        job["physics"] = previous
        raise HTTPException(422, f"No skeleton: {exc}.") from exc
    (folder / "model.glb").write_bytes(data)
    job.update(size=len(data), finished=time.time())
    job["settings"]["rig"] = True
    save_job(job)
    return public(job)


@app.post(f"{PREFIX}/api/jobs/{{job_id}}/retry")
async def retry_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    if job_id in tasks:
        raise HTTPException(409, "It's already running.")
    job.update(status="queued", stage="Waiting for the GPU", progress=0.0, error=None)
    save_job(job)
    start(job_id)
    return public(job)


@app.delete(f"{PREFIX}/api/jobs/{{job_id}}")
async def delete_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    task = tasks.get(job_id)
    if task:
        task.cancel()
        return {"cancelled": True}
    jobs.pop(job_id, None)
    shutil.rmtree(job_dir(job_id), ignore_errors=True)
    return {"deleted": True}


@app.get(f"{PREFIX}/api/home-art")
async def home_art() -> dict[str, Any]:
    done = sorted((j for j in jobs.values() if j.get("status") == "done"), key=lambda j: j["created"], reverse=True)
    return {"pictures": [f"{PREFIX}/files/{j['id']}/front.png" for j in done[:24]], "slideshow": True}


@app.get(f"{PREFIX}/files/{{job_id}}/{{name}}")
async def job_file(job_id: str, name: str) -> FileResponse:
    if job_id not in jobs or name not in ("model.glb", "static.glb", "original.png", "reference.png", "physics_mask.png", *(f"{v}.png" for v in VIEWS)):
        raise HTTPException(404)
    path = job_dir(job_id) / name
    if not path.is_file():
        raise HTTPException(404)
    if name.endswith(".glb"):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in jobs[job_id]["title"]) or job_id
        return FileResponse(path, media_type="model/gltf-binary", filename=f"{safe}.glb",
                            content_disposition_type="inline", headers={"Cache-Control": "no-cache"})
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


def page() -> str:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    stamp = int(max(p.stat().st_mtime for p in STATIC.glob("*.*")))
    return html.replace("{{P}}", PREFIX).replace("{{V}}", str(stamp))


@app.get(PREFIX)
@app.get(f"{PREFIX}/")
async def index() -> HTMLResponse:
    return HTMLResponse(page(), headers={"Cache-Control": "no-cache"})


app.mount(f"{PREFIX}/static", StaticFiles(directory=STATIC), name="static")
