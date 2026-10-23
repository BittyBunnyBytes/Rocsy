from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _path(name: str, default: Path) -> Path:
    return Path(os.getenv(name, str(default))).expanduser().resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = field(default_factory=lambda: os.getenv("MUS_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("MUS_PORT", "8200")))
    mount_prefix: str = field(default_factory=lambda: os.getenv("MUS_MOUNT_PREFIX", "").rstrip("/"))
    comfy_url: str = field(default_factory=lambda: os.getenv("MUS_COMFY_URL", "http://127.0.0.1:8189").rstrip("/"))
    # comfyui_supervisor.py: the always-on host process (network_mode: host
    # makes it reachable here) that owns the ComfyUI child process - this app
    # has no GPU and cannot launch ComfyUI itself. See
    # BunnyBytes/supervisors/comfyui_supervisor.py.
    comfyui_supervisor_url: str = field(default_factory=lambda: os.getenv("MUS_COMFYUI_SUPERVISOR_URL", "http://127.0.0.1:8083").rstrip("/"))
    lock_url: str = field(default_factory=lambda: os.getenv("MUS_LOCK_URL", "http://127.0.0.1:8600").rstrip("/"))
    lock_heartbeat_interval: float = field(default_factory=lambda: float(os.getenv("MUS_LOCK_HEARTBEAT_INTERVAL", "20")))
    queue_internal_secret: str = field(default_factory=lambda: os.getenv("MUS_QUEUE_INTERNAL_SECRET", ""))
    workflow_path: Path = field(default_factory=lambda: _path("MUS_WORKFLOW_PATH", BASE_DIR / "workflows" / "YuE2-INT8.json"))
    minimax_workflow_path: Path = field(default_factory=lambda: _path("MUS_MINIMAX_WORKFLOW_PATH", BASE_DIR / "workflows" / "MiniMax-Music3-INT8.json"))
    models_dir: Path = field(default_factory=lambda: _path("MUS_MODELS_DIR", Path("/models")))
    output_dir: Path = field(default_factory=lambda: _path("MUS_OUTPUT_DIR", BASE_DIR / "Output"))
    data_dir: Path = field(default_factory=lambda: _path("MUS_DATA_DIR", BASE_DIR / "data"))
    log_dir: Path = field(default_factory=lambda: _path("MUS_LOG_DIR", BASE_DIR / "logs"))
    monitor_interval: float = field(default_factory=lambda: float(os.getenv("MUS_MONITOR_INTERVAL", "2")))

    def prepare(self) -> None:
        for directory in (self.output_dir, self.data_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
