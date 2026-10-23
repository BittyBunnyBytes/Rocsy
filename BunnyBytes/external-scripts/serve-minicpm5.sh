#!/usr/bin/env bash
# Start a local-only OpenAI-compatible MiniCPM5 server on port 8080 by default.
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
model="$root_dir/LLM/MiniCPM5-1B/MiniCPM5-1B-Q4_K_M.gguf"
bin="$root_dir/llama.cpp-rdna-boost/build-rocm-gfx1100-rdna-boost/bin/llama-server"

if [[ ! -x "$bin" ]]; then
  bin="$root_dir/llama.cpp-rdna-boost/build-cpu/bin/llama-server"
fi

if [[ ! -x "$bin" || ! -f "$model" ]]; then
  echo "Server or model is missing; build with $root_dir/build-llama-rocm.sh" >&2
  exit 1
fi

exec "$bin" --model "$model" --host 127.0.0.1 --port "${LLAMA_PORT:-8080}" \
  --ctx-size 8192 --n-gpu-layers 999 "$@"
