from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _path(name: str, default: Path) -> Path:
    return Path(os.getenv(name, str(default))).expanduser().resolve()


def local_variant(path: Path) -> Path:
    """`<name>.local.json` next to a workflow wins when it exists: this machine's own defaults
    (checkpoint, LoRA rows) that stay out of the public repository (*.local.json is git-ignored)."""
    local = path.with_name(f"{path.stem}.local{path.suffix}")
    return local if local.is_file() else path


def _workflow(name: str, default: Path) -> Path:
    return local_variant(_path(name, default))


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = field(default_factory=lambda: os.getenv("KREA_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("KREA_PORT", "8300")))
    mount_prefix: str = field(default_factory=lambda: os.getenv("KREA_MOUNT_PREFIX", "").rstrip("/"))
    comfy_url: str = field(default_factory=lambda: os.getenv("KREA_COMFY_URL", "http://127.0.0.1:8189").rstrip("/"))
    # comfyui_supervisor.py: the always-on host process (network_mode: host
    # makes it reachable here) that owns the ComfyUI child process - this app
    # has no GPU and cannot launch ComfyUI itself. See
    # BunnyBytes/supervisors/comfyui_supervisor.py.
    comfyui_supervisor_url: str = field(default_factory=lambda: os.getenv("KREA_COMFYUI_SUPERVISOR_URL", "http://127.0.0.1:8083").rstrip("/"))
    lock_url: str = field(default_factory=lambda: os.getenv("KREA_LOCK_URL", "http://127.0.0.1:8600").rstrip("/"))
    lock_heartbeat_interval: float = field(default_factory=lambda: float(os.getenv("KREA_LOCK_HEARTBEAT_INTERVAL", "20")))
    queue_internal_secret: str = field(default_factory=lambda: os.getenv("KREA_QUEUE_INTERNAL_SECRET", ""))
    workflow_path: Path = field(default_factory=lambda: _workflow("KREA_WORKFLOW_PATH", BASE_DIR / "workflows" / "Krea2-Advanced-LoRA-API.json"))
    moss_workflow_path: Path = field(default_factory=lambda: _path("KREA_MOSS_WORKFLOW_PATH", BASE_DIR / "workflows" / "MOSS-Image-INT8-API.json"))
    moss_fast_workflow_path: Path = field(default_factory=lambda: _path("KREA_MOSS_FAST_WORKFLOW_PATH", BASE_DIR / "workflows" / "MOSS-Image-Few-Step-API.json"))
    moss_clip_name: str = field(default_factory=lambda: os.getenv("KREA_MOSS_CLIP_NAME", "qwen3.5_9b_int8_convrot.safetensors"))
    moss_vae_name: str = field(default_factory=lambda: os.getenv("KREA_MOSS_VAE_NAME", "flux2-vae.safetensors"))
    output_dir: Path = field(default_factory=lambda: _path("KREA_OUTPUT_DIR", BASE_DIR / "Output"))
    data_dir: Path = field(default_factory=lambda: _path("KREA_DATA_DIR", BASE_DIR / "data"))
    log_dir: Path = field(default_factory=lambda: _path("KREA_LOG_DIR", BASE_DIR / "logs"))
    monitor_interval: float = field(default_factory=lambda: float(os.getenv("KREA_MONITOR_INTERVAL", "2")))

    def prepare(self) -> None:
        for directory in (self.output_dir, self.data_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
