#!/usr/bin/env bash
# Run the local MiniCPM5-1B model.  The ROCm build is selected when present.
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
model="$root_dir/LLM/MiniCPM5-1B/MiniCPM5-1B-Q4_K_M.gguf"
bin="$root_dir/llama.cpp-rdna-boost/build-rocm-gfx1100-rdna-boost/bin/llama-cli"

if [[ ! -x "$bin" ]]; then
  bin="$root_dir/llama.cpp/build-cpu/bin/llama-cli"
fi

if [[ ! -x "$bin" || ! -f "$model" ]]; then
  echo "llama.cpp or MiniCPM5-1B is missing; see $root_dir/README.md" >&2
  exit 1
fi

exec "$bin" -m "$model" -ngl 999 --ctx-size 8192 "$@"
