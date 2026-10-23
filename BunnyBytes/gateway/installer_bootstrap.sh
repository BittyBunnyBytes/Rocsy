#!/bin/sh
# Start the installer (browser UI, or --terminal / --defaults in the terminal), installing a
# supported Python 3 first if needed. `sh "Install Rocsy.sh" --help` lists the options.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

fail() {
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --title='Rocsy installer' --text="$1" || true
    fi
    printf '%s\n' "$1" >&2
    exit 1
}

python_ready() {
    command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v pkexec >/dev/null 2>&1; then
        pkexec "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo -n "$@"
    else
        return 1
    fi
}

if ! python_ready; then
    [ -r /etc/os-release ] || fail 'Python 3.9 or newer is needed. Install it, then open Rocsy again.'
    # ID and ID_LIKE are supplied by the operating system's os-release file.
    . /etc/os-release
    case " ${ID:-} ${ID_LIKE:-} " in
        *arch*)
            command -v pacman >/dev/null 2>&1 || fail 'pacman is unavailable. Install Python 3.9 or newer manually.'
            as_root "$(command -v pacman)" -S --needed --noconfirm python || fail 'Could not install Python. Check administrator authentication and package manager output.'
            ;;
        *ubuntu*|*linuxmint*|*mint*)
            command -v apt-get >/dev/null 2>&1 || fail 'apt-get is unavailable. Install Python 3.9 or newer manually.'
            as_root "$(command -v apt-get)" update || fail 'Could not update package lists for Python.'
            as_root "$(command -v apt-get)" install -y python3 || fail 'Could not install Python 3.'
            ;;
        *)
            fail 'Automatic Python setup supports Arch Linux and Ubuntu-based systems. Install Python 3.9 or newer, then open Rocsy again.'
            ;;
    esac
fi

python_ready || fail 'The available Python 3 is older than 3.9. Upgrade the operating system or install a newer Python, then retry.'
exec python3 "$ROOT/Install Rocsy.py" "$@"
