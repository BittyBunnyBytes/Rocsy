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

# The newest Python 3.9+ on PATH (RHEL-like systems keep an old python3 next to python3.11 etc.).
python_bin() {
    for name in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9; do
        if command -v "$name" >/dev/null 2>&1 && "$name" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
            command -v "$name"
            return 0
        fi
    done
    return 1
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif [ -t 0 ] && command -v sudo >/dev/null 2>&1; then
        sudo "$@"   # a terminal: sudo asks for the password here
    elif [ -t 0 ] && command -v doas >/dev/null 2>&1; then
        doas "$@"
    elif command -v pkexec >/dev/null 2>&1; then
        pkexec "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo -n "$@"
    else
        return 1
    fi
}

# The package manager, from os-release ID / ID_LIKE first (Mint -> apt, Rocky -> dnf, Manjaro ->
# pacman ...), else the first one on PATH. Same table as BunnyBytes/hostctl/pkgmanager.py.
package_manager() {
    words=''
    if [ -r /etc/os-release ]; then
        # ID and ID_LIKE are supplied by the operating system's os-release file.
        words=$(. /etc/os-release; echo "${ID:-} ${ID_LIKE:-}")
    fi
    for word in $words; do
        case "$word" in
            debian|ubuntu|linuxmint|mint|pop|elementary|zorin|kali|raspbian|neon|devuan|mx|deepin) want=apt-get ;;
            fedora|rhel|centos|rocky|almalinux|ol|nobara|amzn|mageia) want=dnf ;;
            opensuse*|suse|sles) want=zypper ;;
            arch|manjaro|endeavouros|cachyos|garuda|artix) want=pacman ;;
            alpine|postmarketos) want=apk ;;
            void) want=xbps-install ;;
            solus) want=eopkg ;;
            gentoo|funtoo) want=emerge ;;
            *) continue ;;
        esac
        [ "$want" = dnf ] && ! command -v dnf >/dev/null 2>&1 && want=yum
        if command -v "$want" >/dev/null 2>&1; then echo "$want"; return 0; fi
    done
    for want in apt-get dnf yum zypper pacman apk xbps-install eopkg emerge; do
        if command -v "$want" >/dev/null 2>&1; then echo "$want"; return 0; fi
    done
    return 1
}

install_python() {
    manager=$(package_manager) || fail 'No supported package manager was found. Install Python 3.9 or newer, then open Rocsy again.'
    case "$manager" in
        apt-get) as_root apt-get update && as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 ;;
        dnf|yum) as_root "$manager" install -y python3 || true
                 # RHEL 8-like: python3 is 3.6, the newer ones are separate packages.
                 python_bin >/dev/null || as_root "$manager" install -y python3.11 || as_root "$manager" install -y python39 ;;
        zypper) as_root zypper --non-interactive install python3 ;;
        pacman) as_root pacman -S --needed --noconfirm python ;;
        apk) as_root apk add python3 ;;
        xbps-install) as_root xbps-install -Sy python3 ;;
        eopkg) as_root eopkg install -y python3 ;;
        emerge) as_root emerge --noreplace dev-lang/python ;;
    esac || fail "Could not install Python with $manager. Check administrator authentication and the package manager output."
}

python_bin >/dev/null || install_python
PYTHON=$(python_bin) || fail 'The available Python 3 is older than 3.9. Install Python 3.9 or newer, then open Rocsy again.'
exec "$PYTHON" "$ROOT/Install Rocsy.py" "$@"
