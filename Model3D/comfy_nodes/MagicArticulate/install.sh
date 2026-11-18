#!/usr/bin/env bash
# MagicArticulate (Seed3D, Apache-2.0) for Rocsy 3D Models: its own venv on top of ComfyUI's ROCm torch and
# flash-attn (same trick as RSP), so ComfyUI's packages are never changed. MagicArticulate's model code is
# written for transformers 4.39, ComfyUI runs 5.x: the older one lives only in this venv.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
COMFY="$(cd "$ROOT/../.." && pwd)"
MA_COMMIT=de2172d8bc688ff838506a35e29a9d0b30edc10b
MICH_COMMIT=c88949f294fffccf40b191357ce669cae8694fd9

if [ ! -d "$ROOT/vendor/MagicArticulate/.git" ]; then
  mkdir -p "$ROOT/vendor"
  git clone https://github.com/Seed3D/MagicArticulate.git "$ROOT/vendor/MagicArticulate"
fi
git -C "$ROOT/vendor/MagicArticulate" checkout -q "$MA_COMMIT"
git -C "$ROOT/vendor/MagicArticulate" submodule update --init --recursive
git -C "$ROOT/vendor/MagicArticulate/third_partys/Michelangelo" checkout -q "$MICH_COMMIT"

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  "$COMFY/.venv/bin/python" -m venv "$ROOT/.venv"
fi
SITE="$("$ROOT/.venv/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
# ComfyUI's own site-packages (torch ROCm, flash-attn, numpy, skimage...), read after this venv's own.
"$COMFY/.venv/bin/python" -c 'import site; print(site.getsitepackages()[0])' > "$SITE/zz_comfyui_torch.pth"
"$ROOT/.venv/bin/python" -m pip install --quiet --upgrade pip
"$ROOT/.venv/bin/python" -m pip install --quiet "transformers==4.39.3" "huggingface_hub>=0.20,<0.30" \
  "tokenizers>=0.15,<0.19" "accelerate==0.28.0" "trimesh>=4.2,<5" omegaconf einops

# Weights live with ComfyUI's models (Models -> 3D lists them), not inside the custom node.
mkdir -p "$COMFY/models/magicarticulate"
"$ROOT/.venv/bin/python" "$ROOT/run_skeleton.py" --check
echo "MagicArticulate is installed. Restart ComfyUI once while it's idle."
