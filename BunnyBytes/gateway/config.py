from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when a required setting is missing -- the gateway must refuse to
    start rather than run with an insecure default."""


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(
            f"{name} is required and must not be empty. Set it in the environment "
            f"(e.g. BunnyBytes/.env) before starting the gateway. Refusing to start "
            f"with an unset secret would silently disable authentication."
        )
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    # Required, no defaults -- constructing Settings() raises ConfigError if unset.
    # Only a first-start seed: auth.py hashes it into data/password.json and ignores it after
    # that, so it can (and should) be removed from .env once the gateway has started.
    master_password: str = field(default_factory=lambda: os.getenv("MASTER_PASSWORD", ""))
    no_password: bool = field(default_factory=lambda: os.getenv("ROCSY_NO_PASSWORD", "").lower() in {"1", "true", "yes"})
    cookie_secret: str = field(default_factory=lambda: _required("COOKIE_SECRET"))

    cookie_name: str = field(default_factory=lambda: os.getenv("COOKIE_NAME", "bb_session"))
    cookie_ttl_days: int = field(default_factory=lambda: int(os.getenv("COOKIE_TTL_DAYS", "30")))

    # Upstream app base URLs. Ports match the plan's per-app port allocation;
    # override per-deployment via env.
    vid_upstream: str = field(default_factory=lambda: os.getenv("VID_UPSTREAM", "http://127.0.0.1:8000").rstrip("/"))
    mus_upstream: str = field(default_factory=lambda: os.getenv("MUS_UPSTREAM", "http://127.0.0.1:8200").rstrip("/"))
    krea_upstream: str = field(default_factory=lambda: os.getenv("KREA_UPSTREAM", "http://127.0.0.1:8300").rstrip("/"))
    trainer_upstream: str = field(default_factory=lambda: os.getenv("TRAINER_UPSTREAM", "http://127.0.0.1:8500").rstrip("/"))
    # The queue coordinator's browser-facing surface (/Queue/...). Its
    # internal surface (/enqueue etc.) is called directly by each app, never
    # through this gateway.
    queue_upstream: str = field(default_factory=lambda: os.getenv("QUEUE_UPSTREAM", "http://127.0.0.1:8600").rstrip("/"))
    llama_upstream: str = field(default_factory=lambda: os.getenv("LLAMA_UPSTREAM", "http://127.0.0.1:8400").rstrip("/"))
    hermes_upstream: str = field(default_factory=lambda: os.getenv("HERMES_UPSTREAM", "http://127.0.0.1:9119").rstrip("/"))
    connections_upstream: str = field(default_factory=lambda: os.getenv("CONNECTIONS_UPSTREAM", "http://127.0.0.1:8800").rstrip("/"))
    agent_upstream: str = field(default_factory=lambda: os.getenv("AGENT_UPSTREAM", "http://127.0.0.1:8900").rstrip("/"))
    voice_upstream: str = field(default_factory=lambda: os.getenv("VOICE_UPSTREAM", "http://127.0.0.1:8950").rstrip("/"))
    models_upstream: str = field(default_factory=lambda: os.getenv("MODELS_UPSTREAM", "http://127.0.0.1:8960").rstrip("/"))
    rea_upstream: str = field(default_factory=lambda: os.getenv("REA_UPSTREAM", "http://127.0.0.1:8976").rstrip("/"))
    # Shared studio framework (Image/Music/Voice Agentic sessions); not proxied.
    framework_upstream: str = field(default_factory=lambda: os.getenv("FRAMEWORK_UPSTREAM", "http://127.0.0.1:8700").rstrip("/"))

    # Bearer key accepted on /Llama/* as an alternative to a logged-in
    # session -- headless callers (Hermes agents, on this machine or others)
    # can't do the cookie-login flow, so this route needs its own credential
    # distinct from MASTER_PASSWORD. Required, no default: same reasoning as
    # master_password/cookie_secret above.
    llama_api_key: str = field(default_factory=lambda: _required("LLAMA_API_KEY"))

    gateway_host: str = field(default_factory=lambda: os.getenv("GATEWAY_HOST", "127.0.0.1"))
    gateway_port: int = field(default_factory=lambda: int(os.getenv("GATEWAY_PORT", "8080")))

    # Shared with the apps (they use it for the GPU queue too); signs their
    # events to the notification hub's /internal/notify. Empty disables it.
    queue_internal_secret: str = field(default_factory=lambda: os.getenv("QUEUE_INTERNAL_SECRET", ""))

    # Settings -> Services: the host helper (systemd supervisors, containers, boot).
    hostctl_url: str = field(default_factory=lambda: os.getenv("HOSTCTL_URL", "http://127.0.0.1:8899").rstrip("/"))
    hostctl_secret: str = field(default_factory=lambda: os.getenv("HOSTCTL_SECRET", ""))

    max_body_mb: int = field(default_factory=lambda: int(os.getenv("MAX_BODY_MB", "50")))

    # Explicit, administrator-mounted roots for the Settings -> Cache/Storage
    # cleanup tool. Keep these separate from source, models and configuration.
    storage_comfy_output_dir: Path = field(default_factory=lambda: Path(os.getenv("ROCSY_COMFY_OUTPUT_DIR", "/storage/comfy-output")))
    storage_agent_workspace_dir: Path = field(default_factory=lambda: Path(os.getenv("ROCSY_AGENT_WORKSPACE_DIR", "/storage/agent-workspace")))
    storage_user_uploads_dir: Path = field(default_factory=lambda: Path(os.getenv("ROCSY_USER_UPLOADS_DIR", "/storage/user-uploads")))

    # Exposes developer-only maintenance tools in the UI and API. Keep off in deployments.
    developer_build: bool = field(default_factory=lambda: os.getenv("ROCSY_DEVELOPER_BUILD", "").lower() in {"1", "true", "yes"})

    @property
    def max_body_bytes(self) -> int:
        return self.max_body_mb * 1024 * 1024

    @property
    def cookie_max_age_seconds(self) -> int:
        return self.cookie_ttl_days * 24 * 60 * 60


settings = Settings()
