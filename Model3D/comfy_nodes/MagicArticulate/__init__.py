"""MagicArticulate (Seed3D, CVPR 2025) skeleton generation as a ComfyUI node, for Rocsy's 3D Models studio.

The model needs transformers 4.39 while ComfyUI runs 5.x, so it runs in this pack's own venv (install.sh:
layered on ComfyUI's ROCm torch + flash-attn) as a subprocess. ComfyUI's models are unloaded first so the
GPU has room (it needs ~4.6 GB). Weights: ComfyUI/models/magicarticulate/.

Node: "MagicArticulate Skeleton" (3d/rigging). Input: a 3D model file in ComfyUI's input folder (as Load 3D
names it, e.g. "3d/x.glb"). Output (UI + STRING): the skeleton as JSON
{"joints": [[x, y, z]...], "bones": [[parent, child]...], "root": i}.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import folder_paths

HERE = Path(__file__).resolve().parent
VENV_PY = HERE / ".venv" / "bin" / "python"
RUNNER = HERE / "run_skeleton.py"


class MagicArticulateSkeleton:
    CATEGORY = "3d/rigging"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("skeleton_json",)
    FUNCTION = "run"
    OUTPUT_NODE = True
    DESCRIPTION = "Generate a skeleton for any 3D model with MagicArticulate (autoregressive, hierarchical order)."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model_file": ("STRING", {"default": "3d/model.glb", "tooltip": "Path in ComfyUI's input folder."}),
            "seed": ("INT", {"default": 0, "min": 0, "max": 2**31 - 1}),
            "points": ("INT", {"default": 8192, "min": 2048, "max": 16384, "step": 1024,
                               "tooltip": "Surface points sampled for the shape encoder (trained on 8192)."}),
        }}

    def run(self, model_file: str, seed: int, points: int):
        if not VENV_PY.exists():
            raise RuntimeError("MagicArticulate isn't set up: run custom_nodes/MagicArticulate/install.sh, then restart ComfyUI.")
        path = Path(folder_paths.get_annotated_filepath(model_file))
        if not path.is_file():
            raise FileNotFoundError(f"No model file {model_file} in ComfyUI's input folder.")
        try:   # give the GPU to the subprocess
            import comfy.model_management as mm
            mm.unload_all_models()
            mm.soft_empty_cache()
        except Exception:  # noqa: BLE001
            pass
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "skeleton.json"
            env = dict(os.environ)
            env.setdefault("MAGICARTICULATE_WEIGHTS", str(Path(folder_paths.models_dir) / "magicarticulate"))
            proc = subprocess.run([str(VENV_PY), str(RUNNER), "--mesh", str(path), "--out", str(out),
                                   "--seed", str(seed), "--points", str(points)],
                                  capture_output=True, text=True, env=env, timeout=900)
            if proc.returncode != 0 or not out.exists():
                tail = (proc.stderr or proc.stdout or "")[-1500:]
                raise RuntimeError(f"MagicArticulate failed:\n{tail}")
            text = out.read_text()
        return {"ui": {"skeleton": [json.loads(text)]}, "result": (text,)}


NODE_CLASS_MAPPINGS = {"MagicArticulateSkeleton": MagicArticulateSkeleton}
NODE_DISPLAY_NAME_MAPPINGS = {"MagicArticulateSkeleton": "MagicArticulate Skeleton"}
