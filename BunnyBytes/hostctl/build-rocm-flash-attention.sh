#!/usr/bin/env bash
# Build ROCm FlashAttention into ComfyUI's Python environment (Models -> Manager -> Core Backend ->
# FlashAttention runs this through coreextras.py, job flash-install).
#
#   build-rocm-flash-attention.sh <source-dir>
#
# Environment (set by coreextras.py):
#   GPU_ARCHS / PYTORCH_ROCM_ARCH / AMDGPU_TARGETS   e.g. "gfx1100" or "gfx1100;gfx1101"
#   FLASH_ATTN_VENV                                   ComfyUI's .venv
#   FLASH_ATTN_LOG                                    log file to append to
#
# AMD's fork (github.com/ROCm/flash-attention, branch tridao) builds the Composable Kernel and
# Triton backends in one wheel; ComfyUI picks one at runtime with
# FLASH_ATTENTION_TRITON_AMD_ENABLE=TRUE (Triton) or FALSE (CK, the default).
# The installed torch is used as is (--no-build-isolation), never replaced.
set -euo pipefail

SRC=${1:?usage: build-rocm-flash-attention.sh <source-dir>}
VENV=${FLASH_ATTN_VENV:?FLASH_ATTN_VENV is not set}
LOG=${FLASH_ATTN_LOG:-/dev/null}
ARCHS=${GPU_ARCHS:?GPU_ARCHS is not set}
REPO=https://github.com/ROCm/flash-attention.git
BRANCH=tridao
PY="$VENV/bin/python"

exec >>"$LOG" 2>&1
say() { printf '%s [%s]  %s\n' "$(date '+%F %T')" "$1" "$2"; }

[ -x "$PY" ] || { say ERR "No Python at $PY"; exit 1; }
"$PY" -c 'import torch; assert torch.version.hip, "this torch is not a ROCm build"' \
    || { say ERR "ComfyUI's torch is not a ROCm build; install PyTorch for ROCm first"; exit 1; }
say INFO "Building for $ARCHS into $VENV"

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
git -C "$SRC" submodule update --init --recursive --depth 1
say OK "Source at $(git -C "$SRC" rev-parse --short HEAD)"

# The compile needs ~5 GB per job: size MAX_JOBS to free RAM (keep 24 GB for everything else).
avail_gb=$(awk '/MemAvailable/ {printf "%d", $2 / 1048576}' /proc/meminfo)
jobs=$(( (avail_gb - 24) / 5 ))
cpus=$(nproc)
[ "$jobs" -lt 1 ] && jobs=1
[ "$jobs" -gt "$cpus" ] && jobs=$cpus
export MAX_JOBS=$jobs
say INFO "MAX_JOBS=$MAX_JOBS (available RAM ${avail_gb} GB, ${cpus} CPUs); this takes 30-90 minutes"

export GPU_ARCHS PYTORCH_ROCM_ARCH="${PYTORCH_ROCM_ARCH:-$ARCHS}"
"$PY" -m pip install --no-build-isolation -v "$SRC"
"$PY" -c 'import flash_attn; print("flash_attn", flash_attn.__version__)'
say OK "FlashAttention build finished"
