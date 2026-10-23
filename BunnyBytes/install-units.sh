#!/bin/sh
# Install Rocsy's host services as systemd --user units, with this checkout's paths filled in.
#   ./install-units.sh            hostctl, ComfyUI supervisor and Rocsy Voice worker (prints what it wrote)
# Paths come from BunnyBytes/.env (ROCSY_ROOT, ROCSY_COMFY_ROOT); ROCSY_ROOT defaults to
# the folder above this script. Existing units are backed up as <unit>.bak first.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
env_value() {
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$HERE/.env" 2>/dev/null | tail -n 1 | sed 's/^["'\'']//; s/["'\'']$//'
}
ROOT=${ROCSY_ROOT:-$(env_value ROCSY_ROOT)}
ROOT=${ROOT:-$(dirname "$HERE")}
COMFY=${ROCSY_COMFY_ROOT:-$(env_value ROCSY_COMFY_ROOT)}
[ -n "$COMFY" ] || { echo "Set ROCSY_COMFY_ROOT in $HERE/.env first (your ComfyUI folder)" >&2; exit 1; }
[ -f "$ROOT/BunnyBytes/hostctl/hostctl.py" ] || { echo "ROCSY_ROOT=$ROOT does not hold BunnyBytes/" >&2; exit 1; }

UNITS="hostctl/bunny-hostctl.service supervisors/comfyui-supervisor.service voice/rocsy-voice-worker.service"
DEST=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
mkdir -p "$DEST"
for unit in $UNITS; do
    name=$(basename "$unit")
    [ -f "$DEST/$name" ] && cp -p "$DEST/$name" "$DEST/$name.bak"
    # | is the sed separator, so it can't be in a path
    case "$ROOT$COMFY" in *'|'*) echo "Paths with | are not supported" >&2; exit 1 ;; esac
    sed "s|@ROCSY_ROOT@|$ROOT|g; s|@COMFY_ROOT@|$COMFY|g" "$HERE/$unit" > "$DEST/$name"
    echo "wrote $DEST/$name"
done
if [ ! -f "$HERE/hostctl/hostctl.env" ]; then
    SECRET=$(env_value HOSTCTL_SECRET)
    if [ -n "$SECRET" ]; then
        (umask 077; printf 'HOSTCTL_SECRET=%s\n' "$SECRET" > "$HERE/hostctl/hostctl.env")
        echo "wrote $HERE/hostctl/hostctl.env"
    else
        echo "Note: set HOSTCTL_SECRET in $HERE/.env and run this again (bunny-hostctl needs it)"
    fi
fi
systemctl --user daemon-reload
echo "Start them with: systemctl --user enable --now bunny-hostctl comfyui-supervisor rocsy-voice-worker"
