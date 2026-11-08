#!/usr/bin/env bash
set -euo pipefail
DEST="${1:?usage: install_uncensor.sh <target folder>}"
mkdir -p "$DEST"

# Keep these tools outside the LoRA venv so installation cannot change a running training job.
TORCH_SITE=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
python3 -m venv "$DEST/heretic-venv"
HERETIC_SITE=$("$DEST/heretic-venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
echo "$TORCH_SITE" > "$HERETIC_SITE/_container_rocm.pth"
"$DEST/heretic-venv/bin/python" -m pip install -U \
    'git+https://github.com/p-e-w/heretic.git@171bb98b8b9a41c081631b5e678f7133128973e0'

python3 -m venv "$DEST/abliteration-venv"
ABLITERATION_SITE=$("$DEST/abliteration-venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
echo "$TORCH_SITE" > "$ABLITERATION_SITE/_container_rocm.pth"
if [ -d "$DEST/abliteration/.git" ]; then
    git -C "$DEST/abliteration" fetch --depth 1 origin e43e3c8b550999bcf63bae150b9ae9a531ac423a
else
    git clone --depth 1 https://github.com/Orion-zhen/abliteration.git "$DEST/abliteration"
    git -C "$DEST/abliteration" fetch --depth 1 origin e43e3c8b550999bcf63bae150b9ae9a531ac423a
fi
git -C "$DEST/abliteration" checkout --detach e43e3c8b550999bcf63bae150b9ae9a531ac423a
"$DEST/abliteration-venv/bin/python" -m pip install \
    accelerate bitsandbytes datasets huggingface-hub pandas tqdm transformers matplotlib pyyaml
"$DEST/heretic-venv/bin/heretic" --help | grep -q -- '--model-action'
"$DEST/abliteration-venv/bin/python" -c 'import torch, transformers, yaml; print("Abliteration ready:", torch.__version__)'
echo "Uncensor tools ready in $DEST"
