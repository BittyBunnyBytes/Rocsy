#!/usr/bin/env bash
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
model_dir="${MODEL_DIR:-${ROCSY_COMFY_ROOT:-$HOME/ComfyUI}/models/llm/Qwen3.8-27B-Uncensored-GGUF}"
server_bin="${LLAMA_SERVER_BIN:-${ROCSY_ROOT:-$(dirname "$(dirname "$here")")}/llama.cpp-rdna-boost/build-rocm-gfx1100-rdna-boost/bin/llama-server}"

export ROCM_PATH="/opt/rocm-7.2.4"
export PATH="${ROCM_PATH}/bin:${PATH}"
export LD_LIBRARY_PATH="${ROCM_PATH}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

# Defaults are tuned for a Radeon RX 7900 XTX (24 GiB), Ryzen 7 5800X3D,
# and 64 GiB system RAM. Override the public knobs with environment variables.
exec "${server_bin}" \
  --model "${model_dir}/Qwen3.8-27B-Uncensored-IQ4_XS.gguf" \
  --mmproj "${model_dir}/mmproj-Qwen3.8-27B-Uncensored-F16.gguf" \
  --image-min-tokens 1024 \
  --image-max-tokens "${QWEN38_IMAGE_MAX_TOKENS:-2048}" \
  --device ROCm0 \
  --gpu-layers 999999 \
  --ctx-size "${QWEN38_CTX_SIZE:-32768}" \
  --parallel 1 \
  --batch-size 1024 \
  --ubatch-size 512 \
  --threads "${QWEN38_THREADS:-8}" \
  --threads-batch "${QWEN38_THREADS_BATCH:-16}" \
  --flash-attn on \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --kv-offload \
  --reasoning-preserve \
  --spec-type draft-mtp \
  --spec-draft-n-max 2 \
  --spec-draft-device ROCm0 \
  --gpu-layers-draft 999999 \
  --fit off \
  --host "${QWEN38_HOST:-127.0.0.1}" \
  --port "${QWEN38_PORT:-8081}"
