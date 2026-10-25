#!/usr/bin/env bash
# LLM trainer environment (Trainer → Install LLM trainer): a venv that reuses the container's ROCm
# PyTorch (no second multi-GB torch download) plus PEFT/transformers/bitsandbytes, and llama.cpp's
# Python converter (gguf-py + convert_lora_to_gguf.py only) to turn finished LoRAs into GGUF.
set -euo pipefail
DEST="${1:?usage: install_llm_trainer.sh <target folder>}"
mkdir -p "$DEST"
cd "$DEST"

TORCH_SITE=$(python3 -c 'import os, torch; print(os.path.dirname(os.path.dirname(torch.__file__)))')
echo "Using the container's PyTorch from $TORCH_SITE"
python3 -m venv venv
SITE=$(venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
# Appended after the venv's own site-packages, so anything pip installs here wins over the image's copy.
echo "$TORCH_SITE" > "$SITE/_container_torch.pth"
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install \
    "transformers==5.5.3" "peft==0.18.1" "accelerate==1.15.0" "bitsandbytes==0.50.2" \
    huggingface_hub safetensors sentencepiece protobuf tiktoken pyyaml tqdm requests

if [ -d llama.cpp/.git ]; then
    git -C llama.cpp sparse-checkout set gguf-py conversion
    git -C llama.cpp pull --ff-only
else
    git clone --depth 1 --filter=blob:none --sparse https://github.com/ggml-org/llama.cpp.git llama.cpp
    git -C llama.cpp sparse-checkout set gguf-py conversion
fi

venv/bin/python - <<'EOF'
import torch, transformers, peft, bitsandbytes
print("PROBE", torch.__version__, torch.version.hip, torch.cuda.is_available(),
      "transformers", transformers.__version__, "peft", peft.__version__, "bitsandbytes", bitsandbytes.__version__)
EOF
venv/bin/python llama.cpp/convert_lora_to_gguf.py --help > /dev/null && echo "llama.cpp LoRA converter ready"
echo "LLM trainer installed in $DEST"
