#!/usr/bin/env bash
# SimpleTuner environment (Trainer → Install SimpleTuner): MiniMax Music 3 LoRAs. SimpleTuner needs torch >= 2.13,
# newer than the container's 2.9, so it gets its own venv with the ROCm 7.1 wheels AI Toolkit already runs on here
# (torch 2.13.0+rocm7.1 + triton_rocm). SIMPLETUNER_PLATFORM=cpu keeps setup.py from adding PyPI's CUDA "triton",
# which would overwrite triton_rocm's module; the constraints file keeps pip from swapping the ROCm torch out.
set -euo pipefail
DEST="${1:?usage: install_simpletuner.sh <target folder>}"
INDEX=https://download.pytorch.org/whl/rocm7.1

if [ -d "$DEST/.git" ]; then
    git -C "$DEST" pull --ff-only
else
    git clone --depth 1 https://github.com/bghira/SimpleTuner.git "$DEST"
fi
cd "$DEST"
echo "SimpleTuner $(git log -1 --format='%h %ci')"

[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip wheel
.venv/bin/python -m pip install --index-url "$INDEX" "torch==2.13.0+rocm7.1" "torchvision==0.28.0+rocm7.1" "torchaudio==2.11.0+rocm7.1"
.venv/bin/python -m pip freeze | grep -E '^(torch|torchvision|torchaudio|triton|triton[-_]rocm|pytorch[-_]triton[-_]rocm)==' > .rocm-constraints.txt
cat .rocm-constraints.txt
SIMPLETUNER_PLATFORM=cpu .venv/bin/python -m pip install -c .rocm-constraints.txt --extra-index-url "$INDEX" -e .

.venv/bin/python - <<'EOF'
import torch, diffusers, transformers, simpletuner
from simpletuner.helpers.models.minimaxmusic.model import MiniMaxMusic
print("PROBE", torch.__version__, torch.version.hip, torch.cuda.is_available(),
      "diffusers", diffusers.__version__, "transformers", transformers.__version__, "simpletuner", simpletuner.__version__)
EOF
echo "SimpleTuner installed in $DEST"
