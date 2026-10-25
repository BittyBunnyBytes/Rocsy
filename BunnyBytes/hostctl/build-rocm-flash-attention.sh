#!/usr/bin/env bash
# Build ROCm FlashAttention into ComfyUI's Python environment (Models -> Manager -> Core Backend ->
# FlashAttention runs this through coreextras.py, job flash-install).
#
#   build-rocm-flash-attention.sh <source-dir>
#   build-rocm-flash-attention.sh --check <source-dir>   preflight only (flash_preflight.py), prints to stdout
#
# Environment (set by coreextras.py):
#   GPU_ARCHS / PYTORCH_ROCM_ARCH / AMDGPU_TARGETS   e.g. "gfx1100" or "gfx1100;gfx1101"
#   FLASH_ATTN_VENV                                   ComfyUI's .venv
#   FLASH_ATTN_LOG                                    log file to append to
#
# AMD's fork (github.com/ROCm/flash-attention, branch tridao) is built with its Composable Kernel
# backend (the default; the Triton one would need FLASH_ATTENTION_TRITON_AMD_ENABLE=TRUE at
# build time, which installs ROCm's aiter instead).
# The installed torch is used as is (--no-build-isolation), never replaced.
set -euo pipefail

CHECK=0
[ "${1:-}" = "--check" ] && { CHECK=1; shift; }
SRC=${1:?usage: build-rocm-flash-attention.sh [--check] <source-dir>}
VENV=${FLASH_ATTN_VENV:?FLASH_ATTN_VENV is not set}
LOG=${FLASH_ATTN_LOG:-/dev/null}
ARCHS=${GPU_ARCHS:?GPU_ARCHS is not set}
REPO=https://github.com/ROCm/flash-attention.git
BRANCH=tridao
PY="$VENV/bin/python"
PREFLIGHT="$(dirname "$(readlink -f "$0")")/flash_preflight.py"

if [ "$CHECK" = 1 ]; then
    [ -x "$PY" ] || { echo "ERR No Python at $PY"; exit 1; }
    exec "$PY" "$PREFLIGHT" "$SRC"
fi

exec >>"$LOG" 2>&1
say() { printf '%s [%s]  %s\n' "$(date '+%F %T')" "$1" "$2"; }

[ -x "$PY" ] || { say ERR "No Python at $PY"; exit 1; }
# Toolchain, HIP version vs torch, GPU targets: fail here, not 40 minutes into the compile.
preflight=$(GPU_ARCHS="$ARCHS" "$PY" "$PREFLIGHT" "$SRC") || { printf '%s\n' "$preflight"; say ERR "Preflight failed"; exit 1; }
printf '%s\n' "$preflight"
ROCM_HOME=$(printf '%s\n' "$preflight" | sed -n 's/^ROCM_HOME=//p')
ARCHS=$(printf '%s\n' "$preflight" | sed -n 's/^ARCHS=//p')
export ROCM_HOME ROCM_PATH="$ROCM_HOME" GPU_ARCHS="$ARCHS" PYTORCH_ROCM_ARCH="$ARCHS"
# A pip-installed ninja lands in .venv/bin; after the system's own tools so they win.
export PATH="$PATH:$VENV/bin"
say INFO "Building for $ARCHS into $VENV with $ROCM_HOME"

# setup.py's setup_requires: --no-build-isolation doesn't install them, and without ninja torch
# falls back to a serial compile of every kernel (many hours). None of them touches torch.
"$PY" -m pip install --progress-bar off ninja packaging psutil

# What the environment had before, to compare against if something changes unexpectedly.
mkdir -p "$(dirname "$SRC")"
"$PY" -m pip freeze >"$(dirname "$SRC")/pip-freeze-before-flash-attention.txt" 2>/dev/null || true

if [ -d "$SRC/.git" ]; then
    say INFO "Updating $SRC ($BRANCH)"
    git -C "$SRC" fetch --depth 1 origin "$BRANCH"
    git -C "$SRC" checkout -q -B "$BRANCH" FETCH_HEAD
else
    say INFO "Cloning $REPO ($BRANCH)"
    rm -rf "$SRC"
    git clone --depth 1 --branch "$BRANCH" "$REPO" "$SRC"
fi
# Only Composable Kernel: cutlass (NVIDIA) and aiter (the Triton backend) aren't built here.
git -C "$SRC" submodule update --init --recursive --depth 1 csrc/composable_kernel
say OK "Source at $(git -C "$SRC" rev-parse --short HEAD)"

# The compile needs ~5 GB per job: size MAX_JOBS to free RAM (keep 24 GB for everything else).
avail_gb=$(awk '/MemAvailable/ {printf "%d", $2 / 1048576}' /proc/meminfo)
jobs=$(( (avail_gb - 24) / 5 ))
cpus=$(nproc)
[ "$jobs" -lt 1 ] && jobs=1
[ "$jobs" -gt "$cpus" ] && jobs=$cpus
export MAX_JOBS=$jobs
say INFO "MAX_JOBS=$MAX_JOBS (available RAM ${avail_gb} GB, ${cpus} CPUs); this takes 30-90 minutes"

# FORCE_BUILD: never swap in a release wheel guessed by version (built for other targets).
FLASH_ATTENTION_FORCE_BUILD=TRUE "$PY" -m pip install --no-build-isolation -v "$SRC"
"$PY" -c 'import flash_attn; print("flash_attn", flash_attn.__version__)'
say OK "FlashAttention build finished"
