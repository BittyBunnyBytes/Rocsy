"""MagicArticulate skeleton generation for one mesh (run by this pack's own .venv, see install.sh).

    python run_skeleton.py --mesh model.glb --out skeleton.json [--seed 0] [--points 8192]
    python run_skeleton.py --check          # imports + weights present, no GPU work

Writes {"joints": [[x, y, z], ...] (in the input mesh's coordinates), "bones": [[parent, child], ...],
"root": index, "seconds": float}. Follows MagicArticulate's demo.py (hierarchical ordering checkpoint):
8192 surface points with normals -> unit cube -> autoregressive bone tokens -> merged joints -> back to the
mesh's own scale.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor" / "MagicArticulate"
COMFY = HERE.parent.parent
WEIGHTS = Path(os.environ.get("MAGICARTICULATE_WEIGHTS", COMFY / "models" / "magicarticulate"))
SKELETON_CKPT = WEIGHTS / "checkpoint_trainonv2_hier.pth"
SHAPE_CKPT = WEIGHTS / "shapevae-256.ckpt"


def _prepare_imports() -> None:
    # MagicArticulate uses paths relative to its repo root (configs, imports).
    os.chdir(VENDOR)
    sys.path.insert(0, str(VENDOR))
    # mesh2sdf (optional watertight pass, off here) and pyrender/cv2 (preview renders) aren't needed for
    # inference; stand-ins keep their imports from failing when they aren't installed.
    import importlib.util
    for name in ("mesh2sdf", "pyrender", "cv2"):
        if name not in sys.modules and importlib.util.find_spec(name) is None:
            stub = types.ModuleType(name)
            stub.__file__ = f"<{name} stand-in>"

            def _placeholder(attr: str, _name: str = name):   # any "from x import Y" gets a placeholder class
                if attr.startswith("__"):
                    raise AttributeError(attr)
                return type(attr, (), {})
            stub.__getattr__ = _placeholder
            sys.modules[name] = stub
            if name == "mesh2sdf":
                stub.core = types.ModuleType("mesh2sdf.core")
                sys.modules["mesh2sdf.core"] = stub.core


def check() -> int:
    _prepare_imports()
    problems = [f"missing {p}" for p in (SKELETON_CKPT, SHAPE_CKPT) if not p.exists()]
    import torch  # noqa: F401
    import transformers
    import flash_attn  # noqa: F401
    from skeleton_models.skeletongen import SkeletonGPT  # noqa: F401
    print(json.dumps({"ok": not problems, "problems": problems, "transformers": transformers.__version__,
                      "torch": torch.__version__, "gpu": torch.cuda.is_available()}))
    return 0 if not problems else 1


def run(mesh_path: str, out: str, seed: int, points: int) -> None:
    started = time.time()
    _prepare_imports()
    import numpy as np
    import torch
    import trimesh

    import third_partys.Michelangelo.encode as mich
    import skeleton_models.skeletongen as sg
    from data_utils.save_npz import normalize_to_unit_cube
    from utils.save_utils import merge_duplicate_joints_and_fix_bones, pred_joints_and_bones

    # Both checkpoints carry a few numpy scalars/dtypes from training: allow exactly those and keep
    # torch.load's safe weights-only mode (no arbitrary unpickling).
    # (The files name numpy 1's "numpy.core.multiarray.scalar"; numpy 2 keeps it in numpy._core.)
    np_types = [(np._core.multiarray.scalar, "numpy.core.multiarray.scalar"), np._core.multiarray.scalar, np.dtype] \
        + [type(np.dtype(t)) for t in ("float64", "float32", "int64", "int32", "bool")]
    # Plus the data containers the checkpoints store (listed with torch.serialization.get_unsafe_globals_in_checkpoint):
    # Lightning's omegaconf hyper-parameters and the training argparse.Namespace. Nothing that runs code.
    import argparse
    import collections
    import typing
    from omegaconf import DictConfig, ListConfig
    from omegaconf.base import ContainerMetadata, Metadata
    from omegaconf.nodes import AnyNode
    torch.serialization.add_safe_globals(np_types + [DictConfig, ListConfig, ContainerMetadata, Metadata, AnyNode,
                                                     collections.defaultdict, typing.Any, argparse.Namespace,
                                                     int, dict, list])

    original_load = mich.load_model
    sg.load_model = lambda: original_load(ckpt_path=str(SHAPE_CKPT))

    args = types.SimpleNamespace(input_pc_num=points, num_beams=1, llm="facebook/opt-350m", pad_id=-1,
                                 n_discrete_size=128, n_max_bones=100, seed=seed, precision="fp16",
                                 batchsize_per_gpu=1, hier_order=True)
    torch.manual_seed(seed)
    np.random.seed(seed)
    model = sg.SkeletonGPT(args).cuda()
    pkg = torch.load(SKELETON_CKPT, map_location="cpu", weights_only=True)
    model.load_state_dict(pkg["model"])
    model.eval()

    mesh = trimesh.load(mesh_path, force="mesh")
    pts, face_idx = mesh.sample(points, return_index=True)
    normals = mesh.face_normals[face_idx]
    pc, center, scale = normalize_to_unit_cube(np.asarray(pts, dtype=np.float64).copy(), scale_factor=0.9995)
    bounds = np.array([pc.min(axis=0), pc.max(axis=0)])
    pc_center = (bounds[0] + bounds[1]) / 2
    pc_scale = (bounds[1] - bounds[0]).max() + 1e-5
    pc_normal = np.concatenate([pc.astype(np.float32), normals.astype(np.float32)], axis=-1).astype(np.float16)
    batch = {"pc_normal": torch.from_numpy(pc_normal)[None].cuda()}
    with torch.no_grad(), torch.autocast("cuda", dtype=torch.float16):
        bone_coords = model.generate(batch)
    skeleton = bone_coords[0].float().cpu().numpy().squeeze()
    joints, bones = pred_joints_and_bones(skeleton)
    root = int(bones[0][0])
    joints, bones, root = merge_duplicate_joints_and_fix_bones(joints, bones, root_index=root)
    joints = np.asarray(joints) * pc_scale + pc_center      # back onto the point cloud
    joints = joints / scale + center                         # then onto the original mesh
    Path(out).write_text(json.dumps({
        "joints": np.asarray(joints, dtype=float).round(6).tolist(),
        "bones": np.asarray(bones, dtype=int).tolist(), "root": int(root),
        "seconds": round(time.time() - started, 2)}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mesh")
    ap.add_argument("--out")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--points", type=int, default=8192)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        sys.exit(check())
    run(a.mesh, a.out, a.seed, a.points)
