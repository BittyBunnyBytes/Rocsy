"""The host's package manager, init system and package names, for any Linux distro.

Rocsy installs a few host packages (Docker Engine + Compose + Buildx, Python, git, PyYAML,
venv, build tools for Core Backend). Distros are matched by their package manager, not one
by one: /etc/os-release ID / ID_LIKE picks it when it names a known family (so Linux Mint,
Pop!_OS, Zorin… are APT; Manjaro, EndeavourOS, CachyOS… are pacman; Rocky, Alma… are DNF),
otherwise the first manager found on PATH. Immutable systems (Fedora Atomic, NixOS, SteamOS,
openSUSE MicroOS) can't install packages the normal way: they get manual steps instead.

stdlib only: the bundle installer imports it before anything else is installed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# id -> label, the binary that proves it, how to refresh its index and install packages.
MANAGERS = {
    "apt": {"name": "APT", "bin": "apt-get", "refresh": ["apt-get", "update"],
            "install": ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y"]},
    "dnf": {"name": "DNF", "bin": "dnf", "refresh": None, "install": ["dnf", "install", "-y"]},
    "yum": {"name": "YUM", "bin": "yum", "refresh": None, "install": ["yum", "install", "-y"]},
    "zypper": {"name": "Zypper", "bin": "zypper", "refresh": ["zypper", "--non-interactive", "refresh"],
               "install": ["zypper", "--non-interactive", "install", "--no-recommends"]},
    "pacman": {"name": "pacman", "bin": "pacman", "refresh": None, "install": ["pacman", "-S", "--needed", "--noconfirm"]},
    "apk": {"name": "APK", "bin": "apk", "refresh": ["apk", "update"], "install": ["apk", "add"]},
    "xbps": {"name": "XBPS", "bin": "xbps-install", "refresh": ["xbps-install", "-S"], "install": ["xbps-install", "-y"]},
    "eopkg": {"name": "eopkg", "bin": "eopkg", "refresh": ["eopkg", "update-repo"], "install": ["eopkg", "install", "-y"]},
    "emerge": {"name": "Portage", "bin": "emerge", "refresh": None, "install": ["emerge", "--noreplace", "--quiet-build"]},
}
# os-release ID / ID_LIKE words -> manager.
FAMILIES = {
    "debian": "apt", "ubuntu": "apt", "linuxmint": "apt", "mint": "apt", "pop": "apt", "elementary": "apt",
    "zorin": "apt", "kali": "apt", "raspbian": "apt", "neon": "apt", "devuan": "apt", "mx": "apt", "deepin": "apt",
    "fedora": "dnf", "rhel": "dnf", "centos": "dnf", "rocky": "dnf", "almalinux": "dnf", "ol": "dnf", "nobara": "dnf",
    "amzn": "dnf", "mageia": "dnf",
    "opensuse": "zypper", "suse": "zypper", "sles": "zypper", "opensuse-leap": "zypper", "opensuse-tumbleweed": "zypper",
    "arch": "pacman", "manjaro": "pacman", "endeavouros": "pacman", "cachyos": "pacman", "garuda": "pacman", "artix": "pacman",
    "alpine": "apk", "postmarketos": "apk",
    "void": "xbps",
    "solus": "eopkg",
    "gentoo": "emerge", "funtoo": "emerge",
}
# Logical package -> names per manager. An empty list: the manager's Python already has it.
PACKAGES = {
    "python": {"apt": ["python3"], "dnf": ["python3"], "yum": ["python3"], "zypper": ["python3"], "pacman": ["python"],
               "apk": ["python3"], "xbps": ["python3"], "eopkg": ["python3"], "emerge": ["dev-lang/python"]},
    "venv": {"apt": ["python3-venv"], "dnf": [], "yum": [], "zypper": ["python3"], "pacman": ["python"],
             "apk": ["python3"], "xbps": ["python3"], "eopkg": ["python3"], "emerge": []},
    "yaml": {"apt": ["python3-yaml"], "dnf": ["python3-pyyaml"], "yum": ["python3-pyyaml"], "zypper": ["python3-PyYAML"],
             "pacman": ["python-yaml"], "apk": ["py3-yaml"], "xbps": ["python3-PyYAML"], "eopkg": ["python-yaml"],
             "emerge": ["dev-python/pyyaml"]},
    "git": {"apt": ["git"], "dnf": ["git"], "yum": ["git"], "zypper": ["git"], "pacman": ["git"], "apk": ["git"],
            "xbps": ["git"], "eopkg": ["git"], "emerge": ["dev-vcs/git"]},
    # Core Backend compiles llama.cpp on the host (cmake + Ninja + a C/C++ compiler).
    # sg/newgrp/usermod: BusyBox systems (Alpine, Void) need the shadow tools for the docker group.
    "shadow": {"apk": ["shadow"], "xbps": ["shadow"]},
    "build": {"apt": ["cmake", "ninja-build", "build-essential"], "dnf": ["cmake", "ninja-build", "gcc-c++", "make"],
              "yum": ["cmake", "ninja-build", "gcc-c++", "make"], "zypper": ["cmake", "ninja", "gcc-c++", "make"],
              "pacman": ["cmake", "ninja", "base-devel"], "apk": ["cmake", "samurai", "build-base"],
              "xbps": ["cmake", "ninja", "base-devel"], "eopkg": ["cmake", "ninja", "gcc", "make"],
              "emerge": ["dev-build/cmake", "dev-build/ninja"]},
}
# Docker Engine, the Compose plugin and Buildx: per group the candidates in order; the first one
# the repositories have is installed (distros name the plugins differently, and versions differ).
DOCKER = {
    "apt": [["docker.io"], ["docker-compose-v2", "docker-compose-plugin", "docker-compose"], ["docker-buildx", "docker-buildx-plugin"]],
    "dnf": [["moby-engine", "docker"], ["docker-compose", "docker-compose-plugin"], ["docker-buildx", "docker-buildx-plugin"]],
    "yum": [["moby-engine", "docker"], ["docker-compose", "docker-compose-plugin"], ["docker-buildx", "docker-buildx-plugin"]],
    "zypper": [["docker"], ["docker-compose"], ["docker-buildx"]],
    "pacman": [["docker"], ["docker-compose"], ["docker-buildx"]],
    "apk": [["docker"], ["docker-cli-compose"], ["docker-cli-buildx"]],
    "xbps": [["docker"], ["docker-compose"], ["docker-buildx"]],
    "eopkg": [["docker"], ["docker-compose"], ["docker-buildx"]],
    "emerge": [["app-containers/docker", "app-containers/docker-cli"], ["app-containers/docker-compose"], ["app-containers/docker-buildx"]],
}
# Docker's own repositories (docs.docker.com/engine/install), used where the distro has no Docker
# packages (RHEL, Rocky, Alma, CentOS Stream, Oracle Linux) or no Compose/Buildx (older Debian).
DOCKER_CE_PACKAGES = ["docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"]
DOCKER_RPM_REPOS = {"fedora": "https://download.docker.com/linux/fedora/docker-ce.repo",
                    "rhel": "https://download.docker.com/linux/rhel/docker-ce.repo",
                    "centos": "https://download.docker.com/linux/centos/docker-ce.repo"}
DOCKER_DOCS = "https://docs.docker.com/engine/install/"


def os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    values = {}
    try:
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def immutable(values: dict[str, str]) -> str:
    """Why normal package installs don't work here ('' when they do)."""
    distro = values.get("ID", "").lower()
    if Path("/run/ostree-booted").exists():
        return "rpm-ostree"
    if distro == "nixos" or Path("/etc/NIXOS").exists():
        return "nixos"
    if distro == "steamos":
        return "steamos"
    if shutil.which("transactional-update") and Path("/usr/sbin/transactional-update").exists() and _readonly_root():
        return "transactional"
    return ""


def _readonly_root() -> bool:
    try:
        return bool(os.statvfs("/").f_flag & os.ST_RDONLY)
    except OSError:
        return False


def init_system() -> str:
    if Path("/run/systemd/system").is_dir():
        return "systemd"
    if Path("/run/openrc").exists() or shutil.which("openrc"):
        return "openrc"
    if shutil.which("dinitctl"):
        return "dinit"
    if shutil.which("sv") and any(Path(p).is_dir() for p in ("/etc/sv", "/etc/runit/sv")):
        return "runit"
    return "other"


def detect(values: dict[str, str] | None = None, which=shutil.which) -> dict:
    """{'id', 'name', 'manager' (id or ''), 'manager_name', 'init', 'immutable', 'distro', 'like'}."""
    values = os_release() if values is None else values
    distro = values.get("ID", "").lower()
    like = values.get("ID_LIKE", "").lower().split()
    manager = ""
    for word in [distro, *like]:
        candidate = FAMILIES.get(word)
        if candidate == "dnf" and not which("dnf") and which("yum"):
            candidate = "yum"
        if candidate and which(MANAGERS[candidate]["bin"]):
            manager = candidate
            break
    if not manager:
        manager = next((key for key, spec in MANAGERS.items() if which(spec["bin"])), "")
    fixed = immutable(values)
    return {"id": distro, "name": values.get("PRETTY_NAME") or values.get("NAME") or "Linux", "distro": distro, "like": like,
            "manager": "" if fixed else manager, "manager_name": MANAGERS[manager]["name"] if manager and not fixed else "",
            "init": init_system(), "immutable": fixed}


def packages(manager: str, *names: str) -> list[str]:
    found: list[str] = []
    for name in names:
        for package in PACKAGES[name].get(manager, []):
            if package not in found:
                found.append(package)
    return found


def install_command(manager: str, names: list[str]) -> list[str]:
    command = list(MANAGERS[manager]["install"])
    command[command.index(MANAGERS[manager]["bin"])] = shutil.which(MANAGERS[manager]["bin"]) or MANAGERS[manager]["bin"]
    return [*command, *names]


def refresh_command(manager: str) -> list[str] | None:
    refresh = MANAGERS[manager]["refresh"]
    if not refresh:
        return None
    return [shutil.which(refresh[0]) or refresh[0], *refresh[1:]]


def available(manager: str, package: str) -> bool:
    """Whether the repositories have `package` (needs a refreshed index for APT/APK/XBPS)."""
    checks = {
        "apt": ["apt-cache", "policy", package], "dnf": ["dnf", "-q", "info", package], "yum": ["yum", "-q", "info", package],
        "zypper": ["zypper", "--non-interactive", "--quiet", "search", "--match-exact", package],
        "pacman": ["pacman", "-Si", package], "apk": ["apk", "search", "--exact", package],
        "xbps": ["xbps-query", "-R", package], "eopkg": ["eopkg", "info", package],
    }
    command = checks.get(manager)
    if not command:
        return True  # Portage: emerge -p is slow; the install itself reports a missing atom
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if manager == "apt":
        return "Candidate:" in result.stdout and "Candidate: (none)" not in result.stdout
    if manager == "apk":
        return bool(result.stdout.strip())
    return result.returncode == 0


def docker_packages(manager: str, have=None) -> list[str]:
    """Docker Engine + Compose + Buildx for this manager: the first available name per group."""
    have = have or available
    chosen = []
    for group in DOCKER.get(manager, []):
        name = next((package for package in group if have(manager, package)), None)
        if name:
            chosen.append(name)
    return chosen


def docker_rpm_repo(values: dict[str, str]) -> str:
    """Docker's own .repo for this RPM distro (Fedora, RHEL, or a RHEL rebuild); '' if none fits."""
    distro = values.get("ID", "").lower()
    like = values.get("ID_LIKE", "").lower().split()
    if distro == "fedora":
        return DOCKER_RPM_REPOS["fedora"]
    if distro == "rhel":
        return DOCKER_RPM_REPOS["rhel"]
    if distro in ("centos", "rocky", "almalinux", "ol") or {"rhel", "centos"} & set(like):
        return DOCKER_RPM_REPOS["centos"]
    return DOCKER_RPM_REPOS["fedora"] if "fedora" in like else ""


def docker_apt_repo(values: dict[str, str]) -> tuple[str, str] | None:
    """(ubuntu|debian, codename) for Docker's own APT repo. Derivatives use their base release:
    Linux Mint and Pop!_OS set UBUNTU_CODENAME, LMDE sets DEBIAN_CODENAME."""
    distro = values.get("ID", "").lower()
    like = values.get("ID_LIKE", "").lower().split()
    if values.get("UBUNTU_CODENAME"):
        return "ubuntu", values["UBUNTU_CODENAME"]
    if distro == "ubuntu" and values.get("VERSION_CODENAME"):
        return "ubuntu", values["VERSION_CODENAME"]
    if values.get("DEBIAN_CODENAME"):
        return "debian", values["DEBIAN_CODENAME"]
    if (distro == "debian" or "debian" in like) and values.get("VERSION_CODENAME"):
        return "debian", values["VERSION_CODENAME"]
    return None


def service_commands(init: str, service: str = "docker") -> list[list[str]]:
    """Enable + start a system service under this init system ([] when it must be done by hand)."""
    if init == "systemd":
        return [["systemctl", "enable", "--now", f"{service}.service"]]
    if init == "openrc":
        return [["rc-update", "add", service, "default"], ["rc-service", service, "start"]]
    if init == "dinit":
        return [["dinitctl", "enable", service]]
    if init == "runit":
        source = next((p for p in (f"/etc/sv/{service}", f"/etc/runit/sv/{service}") if Path(p).is_dir()), "")
        target = next((p for p in ("/var/service", "/run/runit/service", "/etc/runit/runsvdir/default") if Path(p).is_dir()), "")
        return [["ln", "-sfn", source, f"{target}/{service}"]] if source and target else []
    return []


def manual_steps(info: dict) -> str:
    """Commands to run by hand: shown in the installer and used when automatic setup can't run."""
    fixed = info.get("immutable")
    if fixed == "rpm-ostree":
        return ("rpm-ostree install moby-engine docker-compose docker-buildx git python3-pyyaml\n"
                "systemctl reboot\nsudo systemctl enable --now docker.service\nsudo usermod -aG docker \"$USER\"")
    if fixed == "nixos":
        return ("# /etc/nixos/configuration.nix:\n#   virtualisation.docker.enable = true;\n"
                "#   users.users.<you>.extraGroups = [ \"docker\" ];\n"
                "#   environment.systemPackages = with pkgs; [ docker-compose docker-buildx git (python3.withPackages (p: [ p.pyyaml ])) ];\n"
                "sudo nixos-rebuild switch")
    if fixed == "steamos":
        return "# SteamOS's system is read-only: install Docker in a container toolbox or a Distrobox, or use another distro."
    if fixed == "transactional":
        return ("sudo transactional-update pkg install docker docker-compose docker-buildx git python3-PyYAML\n"
                "sudo reboot\nsudo systemctl enable --now docker.service\nsudo usermod -aG docker \"$USER\"")
    manager = info.get("manager")
    if not manager:
        return f"# Install Python 3.9+, git, Docker Engine, Docker Compose and Buildx: {DOCKER_DOCS}"
    lines = []
    if refresh := MANAGERS[manager]["refresh"]:
        lines.append("sudo " + " ".join(refresh))
    docker = [group[0] for group in DOCKER.get(manager, [])]
    tools = packages(manager, "python", "venv", "yaml", "git", "shadow")
    lines.append("sudo " + " ".join([*MANAGERS[manager]["install"], *tools, *docker]))
    lines += ["sudo " + " ".join(command) for command in service_commands(info.get("init", ""))]
    if info.get("init") not in ("systemd", "openrc", "dinit", "runit"):
        lines.append("# start the docker service with your init system")
    lines += ['sudo usermod -aG docker "$USER"', "# Sign out and back in before using docker compose without sudo."]
    return "\n".join(lines)


# One sample system per package manager, for the developer's installer preview.
SAMPLES = {"apt": ("Linux Mint 22", "linuxmint", "ubuntu debian", "systemd"), "dnf": ("Fedora Linux 41", "fedora", "", "systemd"),
           "zypper": ("openSUSE Tumbleweed", "opensuse-tumbleweed", "opensuse suse", "systemd"),
           "pacman": ("CachyOS", "cachyos", "arch", "systemd"), "apk": ("Alpine Linux 3.20", "alpine", "", "openrc"),
           "xbps": ("Void Linux", "void", "", "runit"), "eopkg": ("Solus", "solus", "", "systemd"),
           "emerge": ("Gentoo Linux", "gentoo", "", "openrc")}


def samples() -> dict[str, dict]:
    out = {}
    for manager, (name, distro, like, init) in SAMPLES.items():
        info = {"id": distro, "name": name, "distro": distro, "like": like.split(), "manager": manager,
                "manager_name": MANAGERS[manager]["name"], "init": init, "immutable": ""}
        out[manager] = {**info, "family": manager, "manual": manual_steps(info)}
    for key, name, distro, fixed in (("rpm-ostree", "Fedora Silverblue 41", "fedora", "rpm-ostree"), ("nixos", "NixOS 24.11", "nixos", "nixos")):
        info = {"id": distro, "name": name, "distro": distro, "like": [], "manager": "", "manager_name": "",
                "init": "systemd", "immutable": fixed}
        out[key] = {**info, "family": "other", "manual": manual_steps(info)}
    return out
