#!/usr/bin/env bash
# Build the RDNA Boost llama.cpp fork for the RDNA3 (gfx1100) GPU.
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_dir="$root_dir/llama.cpp-rdna-boost"
build_dir="$source_dir/build-rocm-gfx1100-rdna-boost"
cmake_bin="${CMAKE_BIN:-cmake}"

# Arch/CachyOS installs ROCm beneath /opt/rocm without adding it to PATH.
export ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
export PATH="$ROCM_PATH/bin:$PATH"

if ! command -v hipcc >/dev/null || ! command -v rocminfo >/dev/null; then
  echo "ROCm is not installed. Run: sudo pacman -S --needed cmake ninja hip-runtime-amd hipblas rocblas rocm-device-libs rocminfo" >&2
  exit 1
fi

if [[ ! -d "$source_dir/.git" ]]; then
  echo "RDNA Boost source is missing: $source_dir" >&2
  exit 1
fi

gpu_arch="${GPU_ARCH:-gfx1100}"
HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -R)" \
"$cmake_bin" -S "$source_dir" -B "$build_dir" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_HIP=ON \
  -DGGML_HIP_GRAPHS=ON \
  -DCMAKE_HIP_FLAGS="--rocm-path=$ROCM_PATH -mllvm --amdgpu-unroll-threshold-local=600" \
  -DAMDGPU_TARGETS="$gpu_arch" \
  -DLLAMA_BUILD_SERVER=ON \
  -DLLAMA_BUILD_TESTS=OFF
"$cmake_bin" --build "$build_dir" --target llama-cli llama-server --parallel "$(nproc)"

echo "RDNA Boost ROCm build ready: $build_dir/bin/{llama-cli,llama-server}"
