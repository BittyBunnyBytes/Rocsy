"""Docker access for Rocsy's host processes (stdlib only; the installer bundles it too).

1. The docker group in the session that installed Rocsy. The installer adds the account to
   the `docker` group, but the user's systemd manager (which runs bunny-hostctl and every
   `systemd-run` job it starts) was started before that and keeps its old groups until the
   next login. reexec_with_docker_group(), called first in each script's __main__, re-runs
   the process through `sg docker` (or `newgrp docker`) when the account is in the group but
   this process isn't. With the group already active it does nothing.

2. Bind-mount folders owned by root. Docker creates a missing bind source as root; once a
   container writes into it, Update/Repair, the ComfyUI setup and uninstall can't change it.
   bind_sources() lists the folders a Compose file mounts (all profiles), and
   precreate_binds() makes the missing ones as this user before `docker compose up`.
"""
from __future__ import annotations

import grp
import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

REEXEC_FLAG = "ROCSY_DOCKER_GROUP_REEXEC"
# Every optional service is a Compose profile (installer_runtime.ALL_PROFILES).
ALL_PROFILES = ("motion", "rea", "trainer", "hermes")
# Never created: system paths, and sources that are files (a missing file must stay an error,
# not turn into a directory that the container then can't read as a file).
SYSTEM_PREFIXES = ("/dev", "/proc", "/sys", "/run", "/var/run", "/etc", "/usr", "/tmp/.X11-unix")
FILE_SUFFIXES = {".yaml", ".yml", ".json", ".py", ".js", ".mjs", ".css", ".html", ".txt", ".env", ".conf",
                 ".toml", ".ini", ".sock", ".cfg", ".sh", ".service", ".db", ".sqlite", ".sqlite3"}


def lacks_docker_group() -> bool:
    """True when this account is in the docker group but this process doesn't have it."""
    try:
        group = grp.getgrnam("docker")
    except KeyError:
        return False
    if group.gr_gid in os.getgroups() or os.getegid() == group.gr_gid:
        return False
    user = pwd.getpwuid(os.getuid())
    return user.pw_name in group.gr_mem or user.pw_gid == group.gr_gid


def group_command(argv: list[str]) -> list[str]:
    """argv run with the docker group (sg, else newgrp), or unchanged when it isn't needed."""
    if not lacks_docker_group():
        return argv
    for tool in ("sg", "newgrp"):
        path = shutil.which(tool)
        if path:
            return [path, "docker", "-c", shlex.join(argv)]
    return argv


def reexec_with_docker_group() -> None:
    """Call first thing in a script's __main__. Replaces the process once; never loops."""
    if os.environ.get(REEXEC_FLAG) or not lacks_docker_group():
        return
    command = group_command([sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]])
    if command[0] == sys.executable:
        return  # neither sg nor newgrp: carry on, docker calls will say what's wrong
    os.environ[REEXEC_FLAG] = "1"
    os.execv(command[0], command)


def compose_config(compose_dir: Path, run=subprocess.run) -> dict:
    """`docker compose config` with every profile on, as JSON ({} if Compose can't read it)."""
    profiles = [arg for name in ALL_PROFILES for arg in ("--profile", name)]
    result = run(group_command(["docker", "compose", *profiles, "config", "--format", "json"]),
                 cwd=compose_dir, capture_output=True, text=True, timeout=120)
    try:
        return json.loads(result.stdout) if result.returncode == 0 else {}
    except ValueError:
        return {}


def bind_sources(config: dict) -> list[Path]:
    """The folders a resolved Compose config bind-mounts that don't exist yet and are safe to create."""
    out: list[Path] = []
    for service in (config.get("services") or {}).values():
        for volume in service.get("volumes") or []:
            if not isinstance(volume, dict) or volume.get("type") != "bind" or not volume.get("source"):
                continue
            source = Path(volume["source"])
            if not source.is_absolute() or source.exists() or source.is_symlink():
                continue
            if str(source).startswith(SYSTEM_PREFIXES) or source.suffix.lower() in FILE_SUFFIXES:
                continue
            if source not in out:
                out.append(source)
    return out


def precreate_binds(compose_dir: Path, run=subprocess.run, log=lambda _line: None) -> list[Path]:
    """mkdir -p every missing bind folder as this user, so Docker never creates one as root."""
    made = []
    for source in bind_sources(compose_config(compose_dir, run)):
        try:
            source.mkdir(parents=True, exist_ok=True)
            made.append(source)
        except OSError as exc:
            log(f"Couldn't create {source}: {exc}")
    return made
