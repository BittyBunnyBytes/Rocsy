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
    host: str = field(default_factory=lambda: os.getenv("H3_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("H3_PORT", "8000")))

    # Reverse-proxy path-prefix awareness (e.g. "/Video" when mounted behind the
    # BunnyBytes gateway). Empty string for standalone/local-dev use.
    mount_prefix: str = field(default_factory=lambda: os.getenv("H3_MOUNT_PREFIX", "").rstrip("/"))

    comfy_url: str = field(default_factory=lambda: os.getenv("H3_COMFY_URL", "http://127.0.0.1:8188").rstrip("/"))
    comfy_mode: str = field(default_factory=lambda: os.getenv("H3_COMFY_MODE", "external").lower())
    comfy_command: str = field(default_factory=lambda: os.getenv("H3_COMFY_COMMAND", ""))
    # comfyui_supervisor.py: the always-on host process (network_mode: host
    # makes it reachable here) that owns the ComfyUI child process when this
    # app runs in "external" mode (the container deployment - it has no GPU
    # and cannot launch ComfyUI itself). Not used in "local" mode, where this
    # app already manages ComfyUI directly via comfy_command.
    comfyui_supervisor_url: str = field(default_factory=lambda: os.getenv("H3_COMFYUI_SUPERVISOR_URL", "http://127.0.0.1:8083").rstrip("/"))
    qwen_url: str = field(default_factory=lambda: os.getenv("H3_QWEN_URL", "http://127.0.0.1:8081/v1").rstrip("/"))
    qwen_mode: str = field(default_factory=lambda: os.getenv("H3_QWEN_MODE", "external").lower())
    qwen_command_path: Path = field(default_factory=lambda: _path("H3_QWEN_COMMAND", BASE_DIR / "config" / "server_command.txt"))

    # bunny-lock: the BunnyBytes cross-app GPU queue coordinator. It is
    # 127.0.0.1-only; this app calls its internal surface (/enqueue,
    # /heartbeat, /release, /dequeue) directly and unconditionally on every
    # generation, gated by queue_internal_secret below.
    lock_url: str = field(default_factory=lambda: os.getenv("H3_LOCK_URL", "http://127.0.0.1:8600").rstrip("/"))
    queue_internal_secret: str = field(default_factory=lambda: os.getenv("H3_QUEUE_INTERNAL_SECRET", ""))
    workflow_path: Path = field(default_factory=lambda: _workflow("H3_WORKFLOW_PATH", BASE_DIR / "workflows" / "MiniMax-H3.json"))
    ltx_workflow_path: Path = field(default_factory=lambda: _workflow("H3_LTX_WORKFLOW_PATH", BASE_DIR / "workflows" / "LTX-2.5-Final-Optimized.json"))
    wan_workflow_path: Path = field(default_factory=lambda: _workflow("H3_WAN_WORKFLOW_PATH", BASE_DIR / "workflows" / "Wan-2.2-A14B.json"))
    upload_dir: Path = field(default_factory=lambda: _path("H3_UPLOAD_DIR", BASE_DIR / "uploads"))
    input_dir: Path = field(default_factory=lambda: _path("H3_INPUT_DIR", BASE_DIR / "input"))
    output_dir: Path = field(default_factory=lambda: _path("H3_OUTPUT_DIR", BASE_DIR / "Output"))
    data_dir: Path = field(default_factory=lambda: _path("H3_DATA_DIR", BASE_DIR / "data"))
    log_dir: Path = field(default_factory=lambda: _path("H3_LOG_DIR", BASE_DIR / "logs"))
    prompt_guide: Path = field(default_factory=lambda: _path("H3_PROMPT_GUIDE", BASE_DIR / "Official-H3-Prompting-Guidelines.md"))
    max_upload_bytes: int = field(default_factory=lambda: int(os.getenv("H3_MAX_UPLOAD_MB", "20")) * 1024 * 1024)
    max_video_upload_bytes: int = field(default_factory=lambda: int(os.getenv("H3_MAX_VIDEO_UPLOAD_MB", "200")) * 1024 * 1024)
    monitor_interval: float = field(default_factory=lambda: float(os.getenv("H3_MONITOR_INTERVAL", "2")))

    @property
    def stitch_dir(self) -> Path:
        # Stitched-together videos are this app's own creation, not something
        # ComfyUI ever writes - they live under data_dir (writable) rather
        # than output_dir, which the container mounts read-only (:ro in
        # docker-compose.yml, since it's ComfyUI's real, shared output tree).
        return self.data_dir / "stitched"

    @property
    def postedit_dir(self) -> Path:
        # Post-Edit renders (see backend/postedit.py) - same reasoning as stitch_dir.
        return self.data_dir / "postedit"

    def prepare(self) -> None:
        for directory in (self.upload_dir, self.input_dir, self.output_dir, self.data_dir, self.log_dir, self.stitch_dir, self.postedit_dir):
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
