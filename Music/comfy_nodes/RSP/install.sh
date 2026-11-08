#!/usr/bin/env bash
# RSP's Seed-VC environment: its own venv on top of ComfyUI's ROCm torch (same trick as
# Rocsy's AudioSeparator), so ComfyUI's packages are never changed. Downloads weights; no GPU use.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
COMFY="$(cd "$ROOT/../.." && pwd)"
SEED_COMMIT=51383ef

if [ ! -d "$ROOT/vendor/seed-vc/.git" ]; then
  mkdir -p "$ROOT/vendor"
  git clone https://github.com/Plachtaa/seed-vc.git "$ROOT/vendor/seed-vc"
fi
git -C "$ROOT/vendor/seed-vc" checkout -q "$SEED_COMMIT"

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  "$COMFY/.venv/bin/python" -m venv "$ROOT/.venv"
fi
SITE="$("$ROOT/.venv/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
# ComfyUI's own site-packages (whichever Python its venv uses), read after RSP's own.
"$COMFY/.venv/bin/python" -c 'import site; print(site.getsitepackages()[0])' > "$SITE/zz_comfyui_torch.pth"
"$ROOT/.venv/bin/python" -m pip install --quiet --upgrade pip
# Packages here win over ComfyUI's: Seed-VC's Whisper code needs transformers 4.
"$ROOT/.venv/bin/python" -m pip install --quiet \
  "transformers>=4.57,<5" "huggingface_hub>=0.34,<1" librosa soundfile munch descript-audio-codec

# Weights live with ComfyUI's models, not inside the custom node.
mkdir -p "$COMFY/models/RSP/seed-vc"
if [ ! -e "$ROOT/vendor/seed-vc/checkpoints" ]; then
  ln -s "$COMFY/models/RSP/seed-vc" "$ROOT/vendor/seed-vc/checkpoints"
fi
"$ROOT/.venv/bin/python" "$ROOT/rsp/seedvc_runner.py" --prefetch
echo "RSP is installed. Restart ComfyUI once while it's idle."
