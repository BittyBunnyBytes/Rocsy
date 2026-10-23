"""Uninstall only a dedicated Rocsy folder created by the bundle installer."""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path


def installation() -> tuple[Path, str, bool]:
    root = Path(__file__).resolve().parents[2]
    marker = root / '.rocsy-install.json'
    try:
        info = json.loads(marker.read_text())
    except (OSError, ValueError):
        return root, 'This copy was not installed by the Rocsy installer.', False
    if not isinstance(info, dict) or info.get('format') != 1 or info.get('app') != 'Rocsy' or type(info.get('owned_destination')) is not bool:
        return root, 'Invalid Rocsy installation marker.', False
    home = Path.home().resolve()
    if root in (Path('/'), home, home.parent) or root.is_relative_to(Path('/tmp')):
        return root, 'Unsafe installation location.', False
    if not (root / 'BunnyBytes' / 'docker-compose.yml').is_file() or not (root / 'BunnyBytes' / 'hostctl' / 'uninstall_worker.py').is_file():
        return root, 'Rocsy installation is incomplete.', False
    return root, '', info['owned_destination']


def last_run(root: Path) -> dict | None:
    """The last uninstall of this folder, as the worker recorded it (uninstall_worker.RESULT)."""
    try:
        data = json.loads((Path.home() / '.local/state/rocsy/uninstall-last.json').read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get('root') == str(root) else None


def status() -> tuple[int, dict]:
    root, reason, owned = installation()
    return 200, {'available': not reason, 'can_delete_files': owned, 'root': str(root), 'reason': reason,
                 'last_run': last_run(root)}


def start(delete_files: bool) -> tuple[int, dict]:
    root, reason, owned = installation()
    if reason:
        return 409, {'error': reason}
    if delete_files and not owned:
        return 409, {'error': 'This folder had pre-existing files. Keep files when uninstalling Rocsy.'}
    worker = Path(__file__).with_name('uninstall_worker.py')
    if not worker.is_file():
        return 503, {'error': 'Uninstall worker is missing.'}
    unit = 'rocsy-uninstall-' + uuid.uuid4().hex[:12]
    # A transient user unit survives stopping hostctl and removing its source directory.
    command = ['systemd-run', '--user', '--collect', '--unit', unit,
               '/usr/bin/python3', str(worker), str(root), 'delete' if delete_files else 'keep']
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    if result.returncode:
        return 503, {'error': (result.stderr or result.stdout).strip()[:300] or 'Could not start uninstall.'}
    return 202, {'message': 'Uninstall started. Rocsy will go offline shortly.', 'unit': unit}
