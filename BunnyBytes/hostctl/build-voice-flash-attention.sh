#!/usr/bin/env bash
# Build ROCm FlashAttention for the Voice container (Qwen3-TTS "FlashAttention 2" attention).
# Models -> Manager -> Core Backend -> Voice engines runs this through voiceengines.py.
#
#   build-voice-flash-attention.sh <out-dir>
#
# ComfyUI's FlashAttention build can't be passed through: it is compiled for ComfyUI's
# Python and torch, and the Voice image has its own (Python 3.11, torch for ROCm 7.2).
# So this compiles in a throw-away container started from the Voice image itself, against
# its exact torch, with the host's /opt/rocm for hipcc. The result is a pip --target folder
# at <out-dir>/site that docker-compose mounts read-only and puts on the Voice PYTHONPATH.
#
# Environment: VOICE_IMAGE (default bunnybytes-voice), GPU_ARCHS (default gfx1100),
# ROCM_PATH (default /opt/rocm), FLASH_ATTN_LOG (log file to append to).
set -euo pipefail

OUT=${1:?usage: build-voice-flash-attention.sh <out-dir>}
IMAGE=${VOICE_IMAGE:-bunnybytes-voice}
ARCHS=${GPU_ARCHS:-gfx1100}
ROCM=${ROCM_PATH:-/opt/rocm}
LOG=${FLASH_ATTN_LOG:-/dev/null}
REPO=https://github.com/ROCm/flash-attention.git
BRANCH=tridao

exec >>"$LOG" 2>&1
say() { printf '%s [%s]  %s\n' "$(date '+%F %T')" "$1" "$2"; }

[ -x "$ROCM/bin/hipcc" ] || { say ERR "No hipcc in $ROCM"; exit 1; }
docker image inspect "$IMAGE" >/dev/null || { say ERR "Voice image $IMAGE not found; install Qwen3-TTS first"; exit 1; }
mkdir -p "$OUT"

# The compile needs ~5 GB per job: size MAX_JOBS to free RAM (keep 24 GB for everything else).
avail_gb=$(awk '/MemAvailable/ {printf "%d", $2 / 1048576}' /proc/meminfo)
jobs=$(( (avail_gb - 24) / 5 ))
cpus=$(nproc)
[ "$jobs" -lt 1 ] && jobs=1
[ "$jobs" -gt "$cpus" ] && jobs=$cpus
say INFO "Building for $ARCHS from $IMAGE, MAX_JOBS=$jobs (available RAM ${avail_gb} GB); this takes 30-90 minutes"

# Root inside the container only for apt; every file it leaves behind is handed back to this user.
docker run --rm --network host \
    -v "$ROCM:/opt/rocm:ro" -v "$OUT:/build" \
    -e GPU_ARCHS="$ARCHS" -e PYTORCH_ROCM_ARCH="$ARCHS" -e MAX_JOBS="$jobs" \
    -e ROCM_HOME=/opt/rocm -e ROCM_PATH=/opt/rocm -e HOST_IDS="$(id -u):$(id -g)" \
    -e REPO="$REPO" -e BRANCH="$BRANCH" \
    "$IMAGE" bash -euo pipefail -c '
say() { printf "%s [%s]  %s\n" "$(date "+%F %T")" "$1" "$2"; }
trap "chown -R \"$HOST_IDS\" /build" EXIT
apt-get update -qq
apt-get install -y -qq --no-install-recommends git build-essential ninja-build >/dev/null
pip install -q ninja packaging wheel
src=/build/src
if [ -d "$src/.git" ]; then
    say INFO "Updating source ($BRANCH)"
    git -C "$src" fetch --depth 1 origin "$BRANCH"
    git -C "$src" checkout -q -B "$BRANCH" FETCH_HEAD
else
    say INFO "Cloning $REPO ($BRANCH)"
    rm -rf "$src"
    git clone --depth 1 --branch "$BRANCH" "$REPO" "$src"
fi
git -C "$src" submodule update --init --recursive --depth 1
say OK "Source at $(git -C "$src" rev-parse --short HEAD)"
export PATH=/opt/rocm/bin:$PATH
rm -rf /build/wheel /build/site.new
# The image torch is used as is (--no-build-isolation), never replaced.
nice -n 19 pip wheel --no-build-isolation --no-deps -v -w /build/wheel "$src"
pip install -q --no-deps --target /build/site.new /build/wheel/flash_attn-*.whl
python -c "import torch; print(torch.__version__)" > /build/site.new/.rocsy-torch
PYTHONPATH=/build/site.new python -c "import torch, flash_attn_2_cuda, flash_attn; print(\"flash_attn\", flash_attn.__version__, \"for torch\", torch.__version__)"
'
# Swap in the finished build; the Voice container mounts <out-dir>, not site/ itself.
rm -rf "$OUT/site.old"
[ -d "$OUT/site" ] && mv "$OUT/site" "$OUT/site.old"
mv "$OUT/site.new" "$OUT/site"
rm -rf "$OUT/site.old" "$OUT/wheel"
say OK "Voice FlashAttention build finished"
