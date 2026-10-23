"""Detached Rocsy uninstall worker. It runs as a transient systemd user unit."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import integrations

# Outside the installation folder, so a failure can still be reported after a partial uninstall
# (uninstall.status() reads it; Settings shows it).
RESULT = Path.home() / '.local/state/rocsy/uninstall-last.json'


def record(**fields) -> None:
    try:
        RESULT.parent.mkdir(parents=True, exist_ok=True)
        RESULT.write_text(json.dumps({**fields, 'at': time.time()}))
    except OSError:
        pass


def remove_tree(root: Path, images: list[str]) -> None:
    """rmtree; files a container wrote as root (or as its own user) are deleted from inside a
    throwaway container of one of Rocsy's own images, since this account can't remove them."""
    try:
        shutil.rmtree(root)
        return
    except PermissionError:
        pass
    for image in images:
        if subprocess.run(['docker', 'image', 'inspect', image], capture_output=True, timeout=30).returncode:
            continue
        subprocess.run(['docker', 'run', '--rm', '--user', '0', '--network', 'none', '--entrypoint', 'sh',
                        '-v', f'{root}:/target', image, '-c', 'find /target -mindepth 1 -delete'],
                       capture_output=True, timeout=600)
        break
    shutil.rmtree(root)


def run(root: Path, mode: str) -> None:
    marker = root / '.rocsy-install.json'
    if mode not in {'keep', 'delete'} or root in {Path('/'), Path.home(), Path.home().parent}:
        raise ValueError('Unsafe uninstall request')
    if root.is_relative_to(Path('/tmp')) or marker.is_symlink():
        raise ValueError('Unsafe installation location')
    info = json.loads(marker.read_text())
    if not isinstance(info, dict) or info.get('format') != 1 or info.get('app') != 'Rocsy' or type(info.get('owned_destination')) is not bool or (mode == 'delete' and not info['owned_destination']):
        raise ValueError('Invalid installation marker')
    compose = root / 'BunnyBytes' / 'docker-compose.yml'
    if not compose.is_file() or compose.is_symlink():
        raise ValueError('Rocsy Compose file is missing')
    # Keep named volumes when the user elects to preserve files.
    # Every optional service is a Compose profile (installer_runtime.ALL_PROFILES): name them all,
    # or `down` leaves the containers of profiles that are switched on in .env only.
    profiles = [arg for name in ('motion', 'rea', 'trainer', 'hermes') for arg in ('--profile', name)]
    # Rocsy's own images (still there after `down`) can clean up container-owned files later.
    listed = subprocess.run(['docker', 'compose', '-f', str(compose), *profiles, 'config', '--images'],
                            cwd=compose.parent, capture_output=True, text=True, timeout=120)
    images = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    command = ['docker', 'compose', '-f', str(compose), *profiles, 'down', '--remove-orphans']
    if mode == 'delete':
        command.append('--volumes')
    result = subprocess.run(command, cwd=compose.parent, capture_output=True, text=True, timeout=300)
    if result.returncode:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ['see its journal']
        raise RuntimeError(f'docker compose down failed: {tail[0][:240]}')
    # The host helper is either the full one (bunny-hostctl) or the uninstall-only one; the ComfyUI
    # supervisor is this install's when it runs from its supervisors/ folder.
    for name, folder in (('bunny-hostctl.service', 'hostctl'), ('rocsy-uninstall-helper.service', 'hostctl'),
                         ('comfyui-supervisor.service', 'supervisors')):
        unit_file = Path.home() / '.config/systemd/user' / name
        if unit_file.is_file() and str(root / 'BunnyBytes' / folder) in unit_file.read_text():
            subprocess.run(['systemctl', '--user', 'disable', '--now', name], check=False, timeout=30)
            unit_file.unlink()
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, timeout=30)
    # The `rocsy` terminal command and the Rocsy Desktop menu entry go with it.
    integrations.remove_all(root)
    launcher_path = info.get('desktop_launcher')
    if isinstance(launcher_path, str):
        launcher = Path(launcher_path)
        if (launcher.is_file() and not launcher.is_symlink()
                and launcher.name.startswith('Start Rocsy') and launcher.suffix == '.desktop'
                and f'X-Rocsy-Install={root}\n' in launcher.read_text()):
            launcher.unlink()
    if mode == 'delete':
        remove_tree(root, images)


if __name__ == '__main__':
    import dockeraccess  # the docker group in the session that installed Rocsy (see dockeraccess.py)
    dockeraccess.reexec_with_docker_group()
    target = Path(sys.argv[1]).resolve()
    record(phase='running', root=str(target), mode=sys.argv[2])
    try:
        run(target, sys.argv[2])
    except Exception as exc:
        record(phase='failed', root=str(target), mode=sys.argv[2], error=f'{type(exc).__name__}: {exc}'[:400])
        raise
    record(phase='done', root=str(target), mode=sys.argv[2])
