"""GPU (VA-API) video encode/decode for every ffmpeg job: Post-Edit, Stitch,
downloads, playback copies, posters, frame grabs.

The user's rule (2026-10-01): never use the CPU for work the GPU can do. The
RX 7900 XTX encodes H.264 on its media engine (h264_vaapi) and decodes there
too; the container gets /dev/dri/renderD128 (docker-compose) and mesa's
radeonsi VA driver (Dockerfile). Filters with no GPU version (drawtext,
subtitles, xfade, eq, ...) still run between the GPU decode and the GPU encode.

CPU (libx264) stays only as the fallback for a machine without a usable GPU,
e.g. a portable copy run elsewhere.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("h3machine.gpu")

RENDER_NODE = os.getenv("H3_VAAPI_DEVICE", "/dev/dri/renderD128")
_available: bool | None = None


def available() -> bool:
    """Whether VA-API H.264 encoding works here (checked once with a tiny encode)."""
    global _available
    if _available is None:
        _available = False
        if Path(RENDER_NODE).exists():
            try:
                result = subprocess.run(
                    ["ffmpeg", "-nostdin", "-v", "error", *init_args(force=True), "-f", "lavfi", "-i", "testsrc2=s=256x144:d=0.2",
                     "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-f", "null", "-"],
                    capture_output=True, timeout=10,  # startup awaits this check
                )
                _available = result.returncode == 0
                if not _available:
                    log.warning("VA-API encode test failed, using the CPU: %s", result.stderr.decode(errors="replace")[-300:])
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("VA-API encode test failed, using the CPU: %s", exc)
        log.info("Video encoding on the %s", "GPU (VA-API h264_vaapi)" if _available else "CPU (libx264) - no usable GPU")
    return _available


def init_args(force: bool = False) -> list[str]:
    """Global options (before the inputs): the VA-API device, for decode and hwupload."""
    if not (force or available()):
        return []
    return ["-init_hw_device", f"vaapi=va:{RENDER_NODE}", "-filter_hw_device", "va"]


def decode_args() -> list[str]:
    """Put before a video file's -i: decode it on the GPU (frames come back to
    system memory, so the CPU-only filters downstream still work)."""
    return ["-hwaccel", "vaapi", "-hwaccel_device", "va"] if available() else []


# The 7900 XTX's H.264 encoder only takes 128-4096 px per side (VA-API
# constraint); a smaller/larger output can't be GPU-encoded at all.
MIN_SIDE, MAX_SIDE = 128, 4096


def encodes(width: int | None = None, height: int | None = None) -> bool:
    """Whether this output size is encoded on the GPU (unknown size: assume yes)."""
    if not available():
        return False
    return all(side is None or not side or MIN_SIDE <= side <= MAX_SIDE for side in (width, height))


def upload(width: int | None = None, height: int | None = None) -> str:
    """Appended to the last video filter of a chain feeding the encoder."""
    return ",format=nv12,hwupload" if encodes(width, height) else ",format=yuv420p"


def h264(crf: int, width: int | None = None, height: int | None = None) -> list[str]:
    """H.264 encode at libx264-CRF-like size/quality. Fixed QP doesn't adapt
    like CRF: measured on a 1856x1024 H3 clip, QP = CRF + 4 lands near the old
    libx264 file sizes (CRF 18 was ~10 Mbit/s; QP 22 ~12 Mbit/s, SSIM ~0.98).
    QP = CRF - 2 had made files ~3x bigger - slow to download. Pass the output
    size when known: outside 128-4096 px only the CPU can encode."""
    if encodes(width, height):
        return ["-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", str(min(51, max(1, crf + 4)))]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]
