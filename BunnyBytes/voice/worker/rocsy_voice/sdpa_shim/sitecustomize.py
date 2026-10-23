"""Rocsy Voice → Settings → Attention for engines running in their own Python (IndexTTS2, Fish S2).

The worker puts this folder on PYTHONPATH only when a backend is forced, so Python imports it
at startup. Math stays enabled as the fallback for calls the chosen kernel can't take.
"""
import os

_backend = os.environ.get("ROCSY_SDPA_BACKEND", "")
if _backend in ("flash", "efficient", "math"):
    import torch

    torch.backends.cuda.enable_flash_sdp(_backend == "flash")
    torch.backends.cuda.enable_mem_efficient_sdp(_backend == "efficient")
    torch.backends.cuda.enable_math_sdp(True)
