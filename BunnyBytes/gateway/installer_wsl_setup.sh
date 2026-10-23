#!/bin/sh
# Root-side setup for Rocsy on Windows (WSL2) - UNTESTED. "Install Rocsy (Windows).cmd" runs it
# inside the WSL distro as root (wsl.exe -u root), so the installer never needs sudo there.
#   wsl-setup.sh systemd        turn systemd on in /etc/wsl.conf (exit 3: changed, restart the distro)
#   wsl-setup.sh packages USER  Python, git, Docker Engine + Compose + Buildx, Docker access for USER,
#                               and lingering user services (Rocsy's helpers run without a terminal)
set -eu

case "${1:-}" in
systemd)
    [ -d /run/systemd/system ] && exit 0
    touch /etc/wsl.conf
    if grep -q '^systemd *=' /etc/wsl.conf; then
        sed -i 's/^systemd *=.*/systemd=true/' /etc/wsl.conf
    elif grep -q '^\[boot\]' /etc/wsl.conf; then
        sed -i '/^\[boot\]/a systemd=true' /etc/wsl.conf
    else
        printf '\n[boot]\nsystemd=true\n' >> /etc/wsl.conf
    fi
    exit 3
    ;;
packages)
    user=${2:?user name}
    command -v apt-get >/dev/null 2>&1 || { echo 'Rocsy on Windows needs an Ubuntu (apt) WSL distro.' >&2; exit 1; }
    need=''
    for package in python3 python3-venv python3-yaml git; do
        dpkg -s "$package" >/dev/null 2>&1 || need="$need $package"
    done
    if ! command -v docker >/dev/null 2>&1; then
        need="$need docker.io docker-compose-v2 docker-buildx"
    else
        docker compose version >/dev/null 2>&1 || need="$need docker-compose-v2"
        docker buildx version >/dev/null 2>&1 || need="$need docker-buildx"
    fi
    if [ -n "$need" ]; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        # shellcheck disable=SC2086  # one word per package
        apt-get install -y $need
    fi
    systemctl enable --now docker.service
    getent group docker >/dev/null || groupadd docker
    [ "$user" = root ] || usermod -aG docker "$user"
    loginctl enable-linger "$user"
    version=$(docker compose version --short 2>/dev/null || echo 0)
    major=$(echo "$version" | sed 's/^v//' | cut -d. -f1)
    minor=$(echo "$version" | sed 's/^v//' | cut -d. -f2)
    if [ "${major:-0}" -lt 2 ] || { [ "${major:-0}" -eq 2 ] && [ "${minor:-0}" -lt 24 ]; }; then
        echo "Docker Compose $version is too old for Rocsy on WSL (2.24 or newer is needed)." >&2
        echo 'Use Ubuntu 24.04, or install Docker from docker.com inside the distro.' >&2
        exit 1
    fi
    ;;
*)
    echo "usage: $0 systemd | packages USER" >&2
    exit 2
    ;;
esac
