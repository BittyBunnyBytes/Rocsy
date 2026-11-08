"""Suno's cover controls -> YuE2 options. Pure functions, no ComfyUI or torch.

Suno doesn't publish what its sliders do, so this is RSP's own mapping onto the
controls YuE2 really has. 50% Weirdness and 50% Style Influence are YuE2's released
defaults (Suno calls 50% Weirdness the "normal" result).
"""
from __future__ import annotations

import importlib.util

GENDERS = ("auto", "female", "male")
AUDIO_FULL_FROM = 70.0      # melody + chords
AUDIO_MELODY_FROM = 35.0    # melody only; below this: lyrics and style only


def _curve(value: float, low: float, middle: float, high: float) -> float:
    """low at 0%, middle at 50%, high at 100%, straight lines between."""
    value = max(0.0, min(100.0, float(value)))
    if value <= 50.0:
        return low + (middle - low) * value / 50.0
    return middle + (high - middle) * (value - 50.0) / 50.0


def audio_mode(audio_influence: float) -> str:
    """YuE2 has no smooth "follow the recording" control, so the slider has three bands."""
    if audio_influence >= AUDIO_FULL_FROM:
        return "full"
    if audio_influence >= AUDIO_MELODY_FROM:
        return "melody"
    return "off"


def cfg_scale(style_influence: float) -> float:
    """0 means "released default" to YuE2, which is what 50% asks for."""
    if abs(float(style_influence) - 50.0) < 1e-6:
        return 0.0
    return round(_curve(style_influence, 0.5, 1.0, 3.0), 3)


def sampling(weirdness: float) -> dict[str, float | int]:
    return {
        "temperature": round(_curve(weirdness, 0.7, 1.0, 1.4), 3),
        "top_p": round(_curve(weirdness, 0.85, 0.95, 0.99), 3),
        "top_k": int(round(_curve(weirdness, 50, 100, 200))),
        "repetition_penalty": round(_curve(weirdness, 1.3, 1.2, 1.1), 3),
    }


def style_text(style: str, vocal_gender: str = "auto") -> str:
    style = (style or "").strip()
    if vocal_gender == "auto" or vocal_gender not in GENDERS:
        return style
    return f"{style}, {vocal_gender} vocal".strip(", ")


def attention_backend() -> str:
    """Upstream's sdpa took 262 s of the 342 s singing a 211 s song on ROCm. Each choice sings a seed its own way."""
    return "flash" if importlib.util.find_spec("flash_attn") else "fast"


def yue2_options(audio_influence: float, style_influence: float, weirdness: float, *, transpose: int = 0,
                 max_seconds: float = 0.0, ode_steps: int = 32, allow_downloads: bool = False,
                 low_vram: bool = False) -> dict:
    """The inputs of a YuE2 Options node. int8 is the checkpoint Music already uses."""
    mode = audio_mode(audio_influence)
    return {
        "cot": mode, "max_seconds": float(max_seconds), "keep_model_loaded": False,
        "cfg_scale": cfg_scale(style_influence), "ode_steps": int(ode_steps),
        # YuE2 refuses transpose without a score.
        "transpose": int(transpose) if mode != "off" else 0,
        "quantization": "int8", "attention_backend": attention_backend(), "download": "auto" if allow_downloads else "off", "low_vram": bool(low_vram),
        **sampling(weirdness),
    }
