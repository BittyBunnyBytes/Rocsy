# MagicArticulate node pack (Rocsy 3D Models)

[MagicArticulate](https://github.com/Seed3D/MagicArticulate) (Seed3D / ByteDance, CVPR 2025, Apache-2.0)
generates a skeleton for any 3D model. The 3D Models studio uses it for its "MagicArticulate AI" rig option
and does the skinning and preview clips itself (`Model3D/rig.py`, `rig_ai`).

- `install.sh` clones MagicArticulate + Michelangelo at pinned commits into `vendor/` and makes `.venv` on top
  of ComfyUI's ROCm torch and flash-attn, with transformers 4.39 inside (ComfyUI runs 5.x). Restart ComfyUI after.
- Weights (8.3 GB) go in `ComfyUI/models/magicarticulate/`: `checkpoint_trainonv2_hier.pth`
  (Seed3D/MagicArticulate) and `shapevae-256.ckpt` (Maikou/Michelangelo).
- Node: **MagicArticulate Skeleton** (`3d/rigging`): a model file in ComfyUI's input folder -> skeleton JSON
  (`joints`, `bones`, `root`). It unloads ComfyUI's models and runs `run_skeleton.py` in the venv (~4.6 GB VRAM,
  ~15 s including loading). Checkpoints load with torch's weights-only mode; the few numpy/omegaconf/argparse
  containers they store are allow-listed by name.
- hostctl copies this folder into ComfyUI (`STUDIO_LOCAL_PACKS`); `.venv` and `vendor/` are made per machine.
