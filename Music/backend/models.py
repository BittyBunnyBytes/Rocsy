from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# YuE2GenerateMusic's own schema allows max_duration in [0.04, 900.0] seconds
# (comfy_extras/nodes_yue2.py). We narrow that to a saner web-UI range so a
# single request can't monopolize the shared GPU lock for 15 minutes.
MIN_DURATION = 10.0
MAX_DURATION = 300.0
DEFAULT_DURATION = 120.0

MIN_SEED = 0
MAX_SEED = 0xFFFFFFFFFFFFFFFF


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Literal["yue2", "minimax_music3"] = "yue2"
    style: str = Field(min_length=1, max_length=4_000)
    lyrics: str = Field(default="", max_length=20_000)
    seed: int | None = Field(default=None, ge=MIN_SEED, le=MAX_SEED)
    music_seed: int | None = Field(default=None, ge=MIN_SEED, le=MAX_SEED)
    abc_seed: int | None = Field(default=None, ge=MIN_SEED, le=MAX_SEED)
    sampler_seed: int | None = Field(default=None, ge=MIN_SEED, le=MAX_SEED)
    duration: float = Field(default=DEFAULT_DURATION, ge=MIN_DURATION, le=MAX_DURATION)

    # YuE2 conditioning / ABC composition options.
    music_mode: Literal["full", "melody"] = "full"
    abc_mode: Literal["full", "melody"] = "full"
    max_abc_tokens: int = Field(default=16000, ge=256, le=16000)
    abc_temperature: float = Field(default=0.7, ge=0.01, le=2.0)
    abc_top_p: float = Field(default=0.9, gt=0.0, le=1.0)
    abc_top_k: int = Field(default=30, ge=1, le=100)
    abc_repetition_penalty: float = Field(default=1.005, ge=0.5, le=2.0)
    abc_penalty_window: int = Field(default=100, ge=0, le=2000)
    music_top_k: int = Field(default=100, ge=1, le=200)

    # Reference-audio path and its strength dial (the template maps strength
    # into YuE2 temperature, top-p, and repetition penalty through nodes 62-64).
    use_reference: bool = False
    use_youtube: bool = False
    reference_audio: str = Field(default="Ref Music.mp3", max_length=240)
    youtube_url: str = Field(default="", max_length=2000)
    youtube_start_time: float = Field(default=0.0, ge=0.0, le=36000.0)
    youtube_duration: float = Field(default=0.0, ge=0.0, le=36000.0)
    youtube_force_redownload: bool = False
    reference_strength: float = Field(default=0.9, ge=0.0, le=1.0)
    reference_mode: Literal["full", "melody"] = "full"
    custom_music_sampling: bool = False
    music_temperature: float = Field(default=0.69, ge=0.01, le=1.5)
    music_top_p: float = Field(default=0.863, gt=0.0, le=1.0)
    music_repetition_penalty: float = Field(default=1.32, ge=0.5, le=2.0)

    # Diffusion sampler and audio decoding controls.
    steps: int = Field(default=32, ge=1, le=100)
    cfg: float = Field(default=1.0, ge=0.0, le=30.0)
    sampler_name: Literal["dpm_2", "euler", "euler_ancestral", "dpmpp_2m", "dpmpp_sde", "heun"] = "dpm_2"
    scheduler: Literal["sgm_uniform", "normal", "karras", "exponential", "simple", "ddim_uniform"] = "sgm_uniform"
    denoise: float = Field(default=1.0, ge=0.0, le=1.0)
    tile_size: int = Field(default=1920, ge=320, le=4096)
    overlap: int = Field(default=128, ge=0, le=1024)
    output_format: Literal["flac", "mp3", "opus"] = "flac"
    output_quality: Literal["V0", "64k", "96k", "128k", "192k", "320k"] = "V0"
    minimax_steps: int = Field(default=30, ge=1, le=100)
    minimax_cfg: float = Field(default=1.7, ge=0.0, le=30.0)
    minimax_tiled_decode: bool = True

    @field_validator("style")
    @classmethod
    def style_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a style/genre description.")
        return value

    @field_validator("lyrics")
    @classmethod
    def lyrics_trim(cls, value: str) -> str:
        return value.strip()

    @field_validator("reference_audio")
    @classmethod
    def local_audio_filename(cls, value: str) -> str:
        value = value.strip()
        if "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("Use an audio filename from ComfyUI's input folder, without a path.")
        return value

    @field_validator("youtube_url")
    @classmethod
    def valid_youtube_url_when_used(cls, value: str, info) -> str:
        value = value.strip()
        if info.data.get("use_reference") and info.data.get("use_youtube") and not value.startswith(("https://", "http://")):
            raise ValueError("Enter a YouTube URL beginning with http:// or https://.")
        return value

    @field_validator("output_quality")
    @classmethod
    def valid_quality_for_format(cls, value: str, info) -> str:
        output_format = info.data.get("output_format")
        valid = {"V0", "128k", "320k"} if output_format == "mp3" else {"64k", "96k", "128k", "192k", "320k"} if output_format == "opus" else set()
        if valid and value not in valid:
            raise ValueError(f"Choose an audio quality supported by {output_format.upper()}.")
        return value


class Job(BaseModel):
    id: str
    prompt_id: str | None = None
    style: str
    lyrics: str
    seed: int
    duration: float
    state: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    output_url: str | None = None
    download_url: str | None = None
    error: str | None = None
    progress: int = 0
