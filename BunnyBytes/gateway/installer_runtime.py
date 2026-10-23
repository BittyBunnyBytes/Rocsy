#!/usr/bin/env python3
"""Rocsy installer shipped inside a bundle (stdlib only): a local browser UI, or terminal prompts
(`Install Rocsy.sh --terminal`, `--defaults` for the default install without questions)."""
import json
import hashlib
import os
import grp
import pwd
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

BUNDLE = Path(sys.argv[1]).resolve()
DEFAULT_DIR = sys.argv[2] if len(sys.argv) > 2 else '~/Rocsy'
TOKEN = secrets.token_urlsafe(24)
UI = (BUNDLE / 'installer' / 'ui.html').read_text()
sys.path.append(str(BUNDLE / 'BunnyBytes' / 'hostctl'))
import integrations  # noqa: E402  (the `rocsy` command + Rocsy Desktop; shared with the uninstall worker)
import comfyinstall  # noqa: E402  (ComfyUI + its supervisor; shared with Models → Core Backend)
import dockeraccess  # noqa: E402  (docker group + bind folders; shared with the host helper)
import windowshost  # noqa: E402  (Windows via WSL2, untested: GPU override, Windows shortcuts)
import pkgmanager  # noqa: E402  (APT, DNF, Zypper, pacman, APK, XBPS, eopkg, Portage; init systems)
STATE_LOCK = threading.Lock()
STATE = {'job_id': None, 'phase': 'idle', 'progress': 0, 'message': '', 'log': [], 'result': None}
LOG_FILE = None  # <dest>/install.log once the destination exists: the full, untrimmed log
TERMINAL = {'on': False, 'echo': None}  # terminal mode: sudo prompts here; echo(line) shows command output
# Lines worth keeping in an error even when later output pushes them out of the tail
# (a parallel `docker compose build` keeps printing other targets after one fails).
ERROR_LINE = re.compile(r'\bERROR\b|failed to solve|did not complete successfully|^E: |^error: ', re.I)
# Download failures that succeed when retried (dropped mirror/CDN connections), not missing packages.
TRANSIENT_DOWNLOAD = re.compile(r'failed retrieving file|failed to retrieve some files|Failed to fetch|'
                                r'Connection reset|Connection timed out|Temporary failure resolving|'
                                r'Could not connect|SSL_ERROR|Recv failure|unexpected EOF|'
                                r'ReadTimeoutError|Connection broken|IncompleteRead|'
                                # DNF/YUM, Zypper, APK, XBPS, Portage
                                r'Curl error|Cannot download|Failed to download|Download \(curl\) error|'
                                r'Timeout was reached|Could not resolve host|temporary error|network error|'
                                r'Operation too slow|Unable to connect|failed to fetch', re.I)
# What the installer can set up. Services are Compose profiles (COMPOSE_PROFILES in BunnyBytes/.env,
# so later rebuilds keep the choice); backends run once, after Rocsy is up, and each may fail alone.
COMPONENTS = {'motion': True, 'rea': False, 'trainer': False, 'comfyui': True, 'hermes': True, 'ytdlp': True}
SERVICE_PROFILES = ('motion', 'rea', 'trainer')
ALL_PROFILES = (*SERVICE_PROFILES, 'hermes')
PROFILE_SERVICES = {'motion': ['motion'], 'rea': ['rea'], 'trainer': ['trainer'], 'hermes': ['hermes-agent', 'hermes-dashboard']}
SUPERVISOR_UNIT = comfyinstall.UNIT


def update_state(progress, message):
    with STATE_LOCK:
        STATE['progress'] = progress
        STATE['message'] = message
        STATE['log'].append(message)


def run_install(job_id, payload):
    try:
        result = install(payload, update_state)
        with STATE_LOCK:
            STATE.update(phase='complete', progress=100, message='Installation complete.', result=result)
            STATE['log'].append('Installation complete.')
    except Exception as exc:
        with STATE_LOCK:
            STATE.update(phase='error', message=str(exc))
            STATE['log'].append('Error: ' + str(exc))



def distro():
    """This Linux, by its package manager: family = the manager id ('apt', 'dnf', 'pacman', …),
    or 'other' when there is none Rocsy can drive (unknown, or an immutable system)."""
    info = pkgmanager.detect()
    return {**info, 'family': info['manager'] or 'other', 'manual': pkgmanager.manual_steps(info)}


def prerequisites():
    python = sys.version_info >= (3, 9)
    docker = shutil.which('docker') is not None
    compose = docker and command_ok(['docker', 'compose', 'version'])
    buildx = docker and command_ok(['docker', 'buildx', 'version'])
    daemon = docker and command_ok(['docker', 'info'])
    return {'python': python, 'docker': docker, 'compose': compose, 'buildx': buildx, 'daemon': daemon}


def command_ok(command):
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=12).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def log_line(line):
    with STATE_LOCK:
        STATE['log'].append(line.rstrip()[:500])
        STATE['log'] = STATE['log'][-300:]
        if LOG_FILE:
            try:
                with LOG_FILE.open('a') as stream:
                    stream.write(line.rstrip() + '\n')
            except OSError:
                pass
    if TERMINAL['echo']:
        TERMINAL['echo'](line.rstrip())


def start_log_file(dest):
    """Write the log so far to <dest>/install.log and keep appending every line to it."""
    global LOG_FILE
    path = dest / 'install.log'
    with STATE_LOCK:
        try:
            with path.open('a') as stream:
                stream.write('\n'.join(STATE['log']) + '\n')
        except OSError:
            return
        LOG_FILE = path


class CommandFailed(RuntimeError):
    def __init__(self, message, output):
        super().__init__(message)
        self.output = output


def run_command(command, progress, percent, message, cwd=None):
    progress(percent, message)
    log_line('$ ' + shlex.join(command))
    try:
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors='replace', bufsize=1)
    except OSError as exc:
        raise RuntimeError(f'{message}: {exc}') from exc
    recent, errors = [], []
    for line in process.stdout:
        if line.strip():
            log_line(line)
            recent.append(line.rstrip())
            recent = recent[-60:]
            if ERROR_LINE.search(line):
                errors.append(line.rstrip())
                errors = errors[-20:]
    code = process.wait()
    if code:
        # The real failure first, then the tail for context.
        output = [line for line in errors if line not in recent] + recent
        hint = f' See {LOG_FILE}.' if LOG_FILE else ' Open the log for details.'
        raise CommandFailed(f'{message} failed (exit {code}).{hint}', '\n'.join(output))


def run_with_retries(command, progress, percent, message, retry_message, attempts=3, transient_only=True, cwd=None):
    """run_command, again up to `attempts` times on a failure (only download failures when transient_only)."""
    for attempt in range(1, attempts + 1):
        try:
            return run_command(command, progress, percent,
                               message if attempt == 1 else f'{retry_message} ({attempt}/{attempts})…', cwd=cwd)
        except CommandFailed as exc:
            transient = TRANSIENT_DOWNLOAD.search(exc.output) and not re.search(r'\b404\b', exc.output)
            if attempt == attempts or (transient_only and not transient):
                raise
            log_line(f'{message} failed; retrying ({attempt + 1}/{attempts})…')


def privileged(command):
    if os.geteuid() == 0:
        return command
    # In the terminal, sudo asks for the password right there (it reads /dev/tty, not stdin).
    if TERMINAL['on'] and sys.stdin.isatty() and (executable := shutil.which('sudo') or shutil.which('doas')):
        return [executable, *command]
    if executable := shutil.which('pkexec'):
        return [executable, *command]
    if executable := shutil.which('sudo'):
        return [executable, '-n', *command]
    if executable := shutil.which('doas'):
        return [executable, '-n', *command]
    raise RuntimeError('Install a graphical authentication agent (pkexec) or configure sudo or doas, then retry.')


def package_installed(name):
    try:
        result = subprocess.run(['dpkg-query', '-W', '-f=${db:Status-Abbrev}', name],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=12)
        return result.returncode == 0 and result.stdout.startswith('ii')
    except (OSError, subprocess.TimeoutExpired):
        return False


def docker_command(command, use_group):
    if not use_group:
        return command
    if shutil.which('sg'):
        return ['sg', 'docker', '-c', shlex.join(command)]
    if shutil.which('newgrp'):
        return ['newgrp', 'docker', '-c', shlex.join(command)]
    raise RuntimeError('Docker group access needs sg or newgrp. Install the shadow package, then retry.')


def account_in_docker_group():
    try:
        docker_group = grp.getgrnam('docker')
    except KeyError:
        return False
    account = pwd.getpwuid(os.getuid())
    return account.pw_name in docker_group.gr_mem or account.pw_gid == docker_group.gr_gid


def prepare_bind_mount_permissions(compose_dir, use_group, progress):
    # Docker creates missing bind-mount source folders as root. The web apps
    # run as non-root users and need to create databases and working folders.
    services = (
        ('gateway', 'bunnygate', ('/app/data',)),
        ('lock', 'bunnylock', ('/app/data',)),
        ('connections', 'connections', ('/data',)),
        ('agent', 'agent', ('/data', '/attachments', '/workspace')),
        ('modelhub', f'{os.getuid()}:{os.getgid()}', ('/data',)),
        ('h3-machine', 'h3web', ('/data/app', '/data/logs', '/data/uploads', '/llama-overrides')),
        ('music', 'music', ('/data/app', '/data/logs')),
        ('krea', 'krea', ('/data/app', '/data/logs')),
    )
    for service, owner, paths in services:
        account = owner if ':' in owner else f'{owner}:{owner}'
        command = ['docker', 'compose', 'run', '--rm', '--no-deps', '--user', '0',
                   '--entrypoint', 'chown', service, '-R', account, *paths]
        run_command(docker_command(command, use_group), progress, 93,
                    f'Preparing writable folders for {service}…', cwd=compose_dir)
    # This mount contains another bind mount for ComfyUI references. Change
    # only the top-level H3 input folder, not the external ComfyUI files.
    command = ['docker', 'compose', 'run', '--rm', '--no-deps', '--user', '0',
               '--entrypoint', 'chown', 'h3-machine', 'h3web:h3web', '/data/input']
    run_command(docker_command(command, use_group), progress, 93,
                'Preparing H3 input folder…', cwd=compose_dir)


def log_docker_diagnostics():
    commands = ((['systemctl', 'status', 'docker.service', '--no-pager', '--full'],
                 ['journalctl', '-u', 'docker.service', '-n', '30', '--no-pager'])
                if pkgmanager.init_system() == 'systemd' else (['rc-service', 'docker', 'status'], ['sv', 'status', 'docker']))
    for command in commands:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, errors='replace', timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        log_line('$ ' + shlex.join(command))
        for line in (result.stdout or result.stderr).splitlines()[-30:]:
            log_line(line)


def ensure_docker(progress, allow_system_upgrade=False):
    state = prerequisites()
    if not state['python']:
        raise RuntimeError('Python 3.9 or newer is required.')
    if not (state['docker'] and state['compose'] and state['buildx']):
        host = distro()
        manager = host['manager']
        if host['immutable']:
            raise RuntimeError(f'{host["name"]} has a read-only system ({host["immutable"]}), so the installer can\'t add Docker. '
                               'Follow “Manual setup commands” in the installer, then retry.')
        if not manager:
            raise RuntimeError(f'No supported package manager was found. Install Docker Engine, Compose and Buildx ({pkgmanager.DOCKER_DOCS}), then retry.')
        if manager == 'pacman':
            install_docker_pacman(host, progress, allow_system_upgrade)
        else:
            install_docker_packages(host, state, progress)
        state = prerequisites()
        if not (state['docker'] and state['compose'] and state['buildx']):
            missing = [name for key, name in (('docker', 'Docker Engine'), ('compose', 'Docker Compose'), ('buildx', 'Buildx')) if not state[key]]
            raise RuntimeError(f'{", ".join(missing)} still missing after installing with {host["manager_name"]}. '
                               f'Install {"it" if len(missing) == 1 else "them"} by hand ({pkgmanager.DOCKER_DOCS}), then retry.')
    if not state['daemon']:
        start_docker_service(progress)
        if command_ok(['docker', 'info']):
            return False
        manager = distro()['manager']
        if not (shutil.which('sg') or shutil.which('newgrp')) and manager and pkgmanager.packages(manager, 'shadow'):
            # BusyBox systems (Alpine, Void) have no sg/newgrp/usermod without the shadow tools.
            run_with_retries(privileged(pkgmanager.install_command(manager, pkgmanager.packages(manager, 'shadow'))),
                             progress, 20, 'Installing the shadow tools for Docker group access…', 'Retrying failed package downloads')
        if not (shutil.which('sg') or shutil.which('newgrp')):
            raise RuntimeError('Docker is running, but this session cannot access it and neither sg nor newgrp is available.')
        try:
            docker_group = grp.getgrnam('docker')
        except KeyError:
            if groupadd := shutil.which('groupadd'):
                run_command(privileged([groupadd, '-f', 'docker']), progress, 21, 'Preparing Docker access…')
            elif addgroup := shutil.which('addgroup'):
                run_command(privileged([addgroup, '-S', 'docker']), progress, 21, 'Preparing Docker access…')
            else:
                raise RuntimeError('Docker group is missing and groupadd is unavailable.')
            docker_group = grp.getgrnam('docker')
        username = pwd.getpwuid(os.getuid()).pw_name
        if username not in docker_group.gr_mem and pwd.getpwuid(os.getuid()).pw_gid != docker_group.gr_gid:
            if usermod := shutil.which('usermod'):
                command = [usermod, '-aG', 'docker', username]
            elif addgroup := shutil.which('addgroup'):
                command = [addgroup, username, 'docker']  # BusyBox
            else:
                raise RuntimeError('usermod is unavailable; grant this account Docker access manually.')
            run_command(privileged(command), progress, 23, 'Granting this account Docker access…')
        if not command_ok(docker_command(['docker', 'info'], True)):
            log_docker_diagnostics()
            raise RuntimeError('Docker is installed, but this account cannot reach the daemon. Check the log, then sign out and back in if group membership was just changed.')
        return True
    return False


def install_docker_pacman(host, progress, allow_system_upgrade):
    """pacman: Docker, Compose and Buildx; a 404 means a stale package database (offer -Syu)."""
    manager = shutil.which('pacman')
    if not manager:
        raise RuntimeError('pacman is unavailable; install Docker, Compose and Buildx manually.')
    packages = ['docker', 'docker-compose', 'docker-buildx']
    command = [manager, '-Syu' if allow_system_upgrade else '-S', '--needed', '--noconfirm', *packages]
    try:
        # pacman keeps what it already downloaded, so a retry only fetches the missing files.
        run_with_retries(privileged(command), progress, 8,
                         'Updating the system and installing Docker packages…' if allow_system_upgrade else 'Downloading and installing Docker packages…',
                         'Retrying failed package downloads')
    except CommandFailed as exc:
        if '404' not in exc.output:
            raise
        if not allow_system_upgrade:
            raise RuntimeError('Docker package URLs returned 404. Select “Refresh mirrors and update the system” in the installer, then retry.') from exc
        mirrors = shutil.which('cachyos-rate-mirrors') if host['id'] == 'cachyos' else None
        if not mirrors:
            raise RuntimeError('Package URLs still return 404 after a system refresh. Update your mirrors, then retry.') from exc
        run_command(privileged([mirrors]), progress, 9, 'Refreshing CachyOS mirrors…')
        run_command(privileged(command), progress, 12, 'Retrying the system update and Docker installation…')


def refresh_packages(manager, progress, percent=6):
    if refresh := pkgmanager.refresh_command(manager):
        run_with_retries(privileged(refresh), progress, percent, 'Updating package lists…', 'Retrying package list downloads')


def install_docker_packages(host, state, progress):
    """APT, DNF/YUM, Zypper, APK, XBPS, eopkg, Portage: the distro's Docker packages, or Docker's own
    repository where the distro has no Docker Engine / Compose plugin / Buildx (RHEL rebuilds, older Debian)."""
    manager = host['manager']
    if manager == 'apt':
        official = package_installed('docker-ce')
        if state['docker'] and not official and not package_installed('docker.io'):
            raise RuntimeError('An existing Docker installation is not managed by apt. Install its missing plugins manually.')
    else:
        official = False
    refresh_packages(manager, progress)
    packages = ['docker-compose-plugin', 'docker-buildx-plugin'] if official else pkgmanager.docker_packages(manager)
    if packages:
        run_with_retries(privileged(pkgmanager.install_command(manager, packages)), progress, 12,
                         'Downloading and installing Docker packages…', 'Retrying failed package downloads')
    state = prerequisites()
    if state['docker'] and state['compose'] and state['buildx']:
        return
    # The distro has no (or an old) Docker Engine, Compose plugin or Buildx: Docker's own repository.
    log_line(f'{host["manager_name"]} packages left part of Docker missing; trying Docker’s own repository.')
    if not add_docker_repository(host, progress):
        if not packages:
            raise RuntimeError(f'{host["manager_name"]} has no Docker packages here. Install Docker Engine, Compose and Buildx ({pkgmanager.DOCKER_DOCS}), then retry.')
        return  # ensure_docker reports what is still missing
    command = pkgmanager.install_command(manager, pkgmanager.DOCKER_CE_PACKAGES)
    if manager in ('dnf', 'yum'):
        command.insert(command.index('-y'), '--allowerasing')  # docker-ce replaces Fedora's moby-engine
    run_with_retries(privileged(command), progress, 14, 'Installing Docker from Docker’s repository…', 'Retrying failed package downloads')


def download(url, timeout=60):
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310  (fixed https URLs below)
        return response.read()


def install_root_file(data, target, progress, percent, message):
    """Write `data` to a root-owned file through the privileged helper (sudo/pkexec/doas)."""
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False) as stream:
        stream.write(data)
    try:
        os.chmod(stream.name, 0o644)
        run_command(privileged(['install', '-D', '-m', '644', stream.name, str(target)]), progress, percent, message)
    finally:
        os.unlink(stream.name)


def add_docker_repository(host, progress):
    """Docker's own APT or RPM repository (docs.docker.com/engine/install). Returns False when
    this distro has none, so the distro packages that do exist are installed instead."""
    values = pkgmanager.os_release()
    manager = host['manager']
    try:
        if manager == 'apt' and (repo := pkgmanager.docker_apt_repo(values)):
            base, codename = repo
            arch = subprocess.run(['dpkg', '--print-architecture'], capture_output=True, text=True, timeout=10).stdout.strip() or 'amd64'
            install_root_file(download(f'https://download.docker.com/linux/{base}/gpg'), '/etc/apt/keyrings/docker.asc',
                              progress, 7, 'Adding Docker’s package signing key…')
            line = f'deb [arch={arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/{base} {codename} stable\n'
            install_root_file(line.encode(), '/etc/apt/sources.list.d/docker.list', progress, 7, 'Adding Docker’s APT repository…')
            refresh_packages(manager, progress, 8)
            return True
        if manager in ('dnf', 'yum') and (url := pkgmanager.docker_rpm_repo(values)):
            install_root_file(download(url), '/etc/yum.repos.d/docker-ce.repo', progress, 7, 'Adding Docker’s RPM repository…')
            return True
    except OSError as exc:  # urllib errors are OSErrors
        log_line(f'Could not add Docker’s repository: {exc}')
    return False


def start_docker_service(progress):
    """Enable + start Docker under systemd, OpenRC, dinit or runit, then wait for it to answer."""
    init = pkgmanager.init_system()
    commands = pkgmanager.service_commands(init)
    if not commands:
        raise RuntimeError(f'Start the docker service with your init system ({init}), then retry.')
    if init == 'systemd':
        systemctl = shutil.which('systemctl')
        if command_ok([systemctl, 'is-active', '--quiet', 'docker.service']):
            return
        try:
            run_command(privileged([systemctl, 'enable', '--now', 'docker.service']), progress, 18, 'Starting Docker Engine…')
        except CommandFailed:
            log_line('Docker service did not start; trying Docker socket activation…')
            try:
                run_command(privileged([systemctl, 'enable', '--now', 'docker.socket']), progress, 19, 'Starting Docker socket…')
            except CommandFailed as exc:
                log_docker_diagnostics()
                raise RuntimeError('Docker Engine could not start. Read the service diagnostics in the installer log.') from exc
        return
    try:
        for command in commands:
            run_command(privileged([shutil.which(command[0]) or command[0], *command[1:]]), progress, 18,
                        f'Starting Docker Engine ({init})…')
    except CommandFailed as exc:
        log_docker_diagnostics()
        raise RuntimeError('Docker Engine could not start. Read the service diagnostics in the installer log.') from exc
    for _ in range(20):  # runit/OpenRC return before dockerd listens
        if os.path.exists('/var/run/docker.sock'):
            break
        time.sleep(1)


def systemd_user_ok():
    """User units (host helper, ComfyUI supervisor) need systemd as the init system."""
    return pkgmanager.init_system() == 'systemd' and shutil.which('systemctl') is not None


def safe_destination(raw):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 500 or any(ch in raw for ch in ('\0', '\n', '\r')):
        raise ValueError('Enter an installation folder.')
    path = Path(os.path.expandvars(os.path.expanduser(raw.strip()))).resolve()
    if path in (Path('/'), Path.home().resolve(), BUNDLE, BUNDLE.parent) or path.is_relative_to(BUNDLE) or BUNDLE.is_relative_to(path):
        raise ValueError('Choose a dedicated folder outside your home root and the temporary installer.')
    if path.exists() and not path.is_dir():
        raise ValueError('The selected path is not a folder.')
    return path


def existing_install(dest):
    marker = dest / '.rocsy-install.json'
    if not marker.exists():
        return None
    if marker.is_symlink():
        raise ValueError('The installation marker is a symlink.')
    try:
        info = json.loads(marker.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError('The existing Rocsy installation marker is invalid.') from exc
    if (not isinstance(info, dict) or info.get('format') != 1 or info.get('app') != 'Rocsy'
            or type(info.get('owned_destination')) is not bool):
        raise ValueError('The existing Rocsy installation marker is invalid.')
    return info


def existing_password(dest):
    env_file = dest / 'BunnyBytes' / '.env'
    try:
        lines = env_file.read_text().splitlines()
    except FileNotFoundError:
        return ''
    except OSError as exc:
        raise ValueError('The existing Rocsy password file is unavailable.') from exc
    for line in lines:
        if line.strip() == 'ROCSY_NO_PASSWORD=1':
            return ''
    for line in lines:
        if line.startswith('MASTER_PASSWORD='):
            return line.partition('=')[2].strip().strip('"\'')
    return ''


def saved_password_record(dest):
    path = dest / 'BunnyBytes' / 'gateway' / 'data' / 'password.json'
    if path.is_symlink():
        raise ValueError('The saved Rocsy password file is a symlink.')
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text())
        if (not isinstance(record, dict) or record.get('version') != 1
                or not all(isinstance(record.get(key), str) for key in ('salt', 'hash', 'revision'))):
            raise ValueError()
        bytes.fromhex(record['salt'])
        bytes.fromhex(record['hash'])
    except (OSError, ValueError) as exc:
        raise ValueError('The saved Rocsy password file is invalid.') from exc
    return record


def verify_existing_password(dest, candidate):
    record = saved_password_record(dest)
    if record is not None:
        actual = hashlib.pbkdf2_hmac('sha256', candidate.encode(), bytes.fromhex(record['salt']), 600_000)
        return secrets.compare_digest(actual, bytes.fromhex(record['hash']))
    return secrets.compare_digest(candidate, existing_password(dest))


def reinstall_password_required(dest, info):
    if saved_password_record(dest) is not None:
        return True
    password = existing_password(dest)
    if not password or info.get('password_mode') in ('generated', 'none'):
        return False
    if info.get('password_mode') == 'custom':
        return True
    # Older installer markers did not record the password choice. That installer
    # generated a 24-character URL-safe password by default.
    return re.fullmatch(r'[A-Za-z0-9_-]{24}', password) is None


def remove_managed_launcher(info, dest):
    raw = info.get('desktop_launcher')
    if not isinstance(raw, str):
        return
    launcher = Path(raw)
    if (launcher.is_file() and not launcher.is_symlink() and launcher.name.startswith('Start Rocsy')
            and launcher.suffix == '.desktop' and f'X-Rocsy-Install={dest}\n' in launcher.read_text()):
        launcher.unlink()


USER_UNITS = Path.home() / '.config/systemd/user'
HOST_HELPERS = {True: 'bunny-hostctl.service', False: 'rocsy-uninstall-helper.service'}


def host_helper_installed(dest):
    unit_file = USER_UNITS / HOST_HELPERS[True]
    return unit_file.is_file() and str(dest / 'BunnyBytes' / 'hostctl' / 'hostctl.py') in unit_file.read_text()


def setup_host_helper(dest, env_file, full):
    """Settings talk to the host through one helper on HOSTCTL_PORT: the full bunny-hostctl
    (Services, GPU tuning, Core Backend, REA folders, model loading priority … and uninstall)
    or, when that is turned off, a helper that can only uninstall. Both use HOSTCTL_SECRET."""
    values = dict(line.partition('=')[::2] for line in env_file.read_text().splitlines() if '=' in line and not line.startswith('#'))
    secret = values.get('HOSTCTL_SECRET', '')
    if not secret:
        raise ValueError('HOSTCTL_SECRET is missing; the host helper cannot start.')
    hostctl = dest / 'BunnyBytes' / 'hostctl'
    script = hostctl / ('hostctl.py' if full else 'uninstall_server.py')
    if not script.is_file():
        raise ValueError('The host helper was not included in the bundle.')
    USER_UNITS.mkdir(parents=True, exist_ok=True)
    unit_file = USER_UNITS / HOST_HELPERS[full]
    if unit_file.exists() and str(hostctl) not in unit_file.read_text():
        raise ValueError(f'{unit_file.name} is already set up for another Rocsy installation.')
    # Both bind HOSTCTL_PORT: stop this install's other helper first.
    other = USER_UNITS / HOST_HELPERS[not full]
    if other.is_file() and str(hostctl) in other.read_text():
        subprocess.run(['systemctl', '--user', 'disable', '--now', other.name], check=False, timeout=30)
        other.unlink()
    helper_env = hostctl / ('hostctl.env' if full else 'uninstall-helper.env')
    helper_env.write_text('HOSTCTL_SECRET=' + secret + '\n')
    helper_env.chmod(0o600)
    if full:
        template = (hostctl / 'bunny-hostctl.service').read_text()
        unit_file.write_text(template.replace('@ROCSY_ROOT@', str(dest).replace('%', '%%')))
    else:
        unit_file.write_text('[Unit]\nDescription=Rocsy uninstall helper\n[Service]\nType=simple\n'
                             f'ExecStart=/usr/bin/python3 {shlex.quote(str(script).replace("%", "%%"))}\n'
                             f'EnvironmentFile={shlex.quote(str(helper_env).replace("%", "%%"))}\n'
                             'Restart=on-failure\n[Install]\nWantedBy=default.target\n')
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, timeout=30)
    subprocess.run(['systemctl', '--user', 'enable', unit_file.name], check=True, timeout=30)
    # restart, not start: an update must run the new helper code.
    subprocess.run(['systemctl', '--user', 'restart', unit_file.name], check=True, timeout=30)
    require_running(unit_file.name)


def require_running(unit, wait=4):
    """A unit that dies at start-up (Restart=on-failure) would crash-loop silently: check it stayed up."""
    time.sleep(wait)
    if subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit], timeout=15).returncode == 0:
        return
    journal = subprocess.run(['journalctl', '--user', '-u', unit, '-n', '15', '--no-pager', '-o', 'cat'],
                             capture_output=True, text=True, errors='replace', timeout=15).stdout
    for line in journal.splitlines():
        log_line(line)
    last = next((line for line in reversed(journal.splitlines()) if line.strip()), 'see the log')
    raise RuntimeError(f'{unit} did not stay running: {last[:200]}')


def apply_integrations(dest, payload, info):
    """The installer's terminal/desktop toggles: add what is on, remove this install's copy of what is off."""
    result = {}
    for key, add, remove in (('terminal_command', integrations.install_command, integrations.remove_command),
                             ('desktop_app', integrations.install_desktop, integrations.remove_desktop)):
        if payload.get(key):
            result[key] = add(dest)
            info[key] = result[key]['path']
        else:
            remove(dest)
            info.pop(key, None)
    return result


def create_desktop_launcher(dest):
    compose_dir = dest / 'BunnyBytes'
    script = dest / 'Start Rocsy.sh'
    script.write_text('#!/bin/sh\nset -eu\n'
                      f'cd {shlex.quote(str(compose_dir))}\n'
                      'if docker info >/dev/null 2>&1; then\n'
                      '  docker compose up -d\n'
                      'elif command -v sg >/dev/null 2>&1; then\n'
                      "  sg docker -c 'docker compose up -d'\n"
                      'elif command -v newgrp >/dev/null 2>&1; then\n'
                      "  newgrp docker -c 'docker compose up -d'\n"
                      'else\n'
                      '  echo "Docker is unavailable to this account." >&2\n'
                      '  exit 1\n'
                      'fi\n'
                      'if command -v xdg-open >/dev/null 2>&1; then\n'
                      '  xdg-open http://127.0.0.1:8080/ >/dev/null 2>&1 || true\n'
                      'fi\n')
    script.chmod(0o755)
    desktop_dir = Path.home() / 'Desktop'
    if shutil.which('xdg-user-dir'):
        result = subprocess.run(['xdg-user-dir', 'DESKTOP'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip() and Path(result.stdout.strip()).expanduser() != Path.home():
            desktop_dir = Path(result.stdout.strip()).expanduser()
    desktop_dir.mkdir(parents=True, exist_ok=True)
    launcher = desktop_dir / 'Start Rocsy.desktop'
    if launcher.exists() and f'X-Rocsy-Install={dest}\n' not in launcher.read_text():
        for number in range(2, 100):
            candidate = desktop_dir / f'Start Rocsy ({number}).desktop'
            if not candidate.exists() or f'X-Rocsy-Install={dest}\n' in candidate.read_text():
                launcher = candidate
                break
        else:
            raise RuntimeError('No free Desktop launcher name is available.')
    executable = str(script).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')
    icon = dest / 'BunnyBytes/gateway/static/icons/rocsy-192.png'
    launcher.write_text('[Desktop Entry]\nType=Application\nName=Start Rocsy\n'
                        'Comment=Start the Rocsy Docker suite\n'
                        f'Exec="{executable}"\n'
                        f'Icon={icon}\nTerminal=true\nCategories=Utility;\n'
                        f'X-Rocsy-Install={dest}\n')
    launcher.chmod(0o755)
    return launcher


def profile_args(names):
    return [arg for name in names for arg in ('--profile', name)]


def prepare_bind_folders(compose_dir):
    """Make every folder the services mount (all profiles) as this user before Docker can make it as root."""
    made = dockeraccess.precreate_binds(compose_dir, log=log_line)
    if made:
        log_line(f'Created {len(made)} folder(s) the services mount.')


def env_value(env_file, key):
    try:
        for line in reversed(env_file.read_text().splitlines()):
            if line.startswith(key + '='):
                return line.partition('=')[2].strip()
    except OSError:
        pass
    return ''


def set_env_value(env_file, key, value):
    """Replace KEY=… in .env (or append it), keeping the file's 0600 mode."""
    lines = env_file.read_text().splitlines()
    kept = [line for line in lines if not line.startswith(key + '=')]
    kept.append(f'{key}={value}')
    env_file.write_text('\n'.join(kept) + '\n')


def hermes_ready(dest):
    return (dest / 'hermes-rocsy' / 'Dockerfile').is_file()


def ytdlp_ready(dest):
    return (dest / 'core-backend-extras' / 'yt-dlp' / 'yt-dlp').is_file()


def supervisor_installed(dest):
    return comfyinstall.supervisor_status(dest)['state'] == 'ours'


def current_components(dest):
    """What an existing install has now (Update/Repair start from it)."""
    profiles = set(env_value(dest / 'BunnyBytes' / '.env', 'COMPOSE_PROFILES').split(',')) - {''}
    if not (dest / 'BunnyBytes' / '.env').is_file() or 'COMPOSE_PROFILES' not in (dest / 'BunnyBytes' / '.env').read_text():
        profiles = {'motion', 'rea', 'trainer'}  # installs made before components existed had all three
    return {'motion': 'motion' in profiles, 'rea': 'rea' in profiles, 'trainer': 'trainer' in profiles,
            'comfyui': supervisor_installed(dest), 'hermes': hermes_ready(dest), 'ytdlp': ytdlp_ready(dest)}


def chosen_components(raw, existing_dest=None):
    defaults = current_components(existing_dest) if existing_dest else COMPONENTS
    if raw is None:
        return dict(defaults)
    if not isinstance(raw, dict) or set(raw) - COMPONENTS.keys() or any(type(value) is not bool for value in raw.values()):
        raise ValueError('Choose which components to install.')
    return {key: raw.get(key, defaults[key]) for key in COMPONENTS}


def ensure_host_tools(progress, components, host_helper, download_comfy):
    """git for ComfyUI/Hermes, PyYAML for the host helper and Core Backend jobs, venv for ComfyUI.
    With the host helper on, anything skipped here can be installed later in Models → Core Backend,
    so it gets every tool those jobs use (git, PyYAML, venv) whether or not it is chosen now."""
    needs_git = host_helper or (components['comfyui'] and download_comfy) or components['hermes']
    needs_yaml = host_helper or components['hermes'] or components['ytdlp']
    missing = []
    if needs_git and not shutil.which('git'):
        missing.append('git')
    # The host units run /usr/bin/python3 (see bunny-hostctl.service); the Core Backend jobs run here.
    pythons = {sys.executable, *(['/usr/bin/python3'] if Path('/usr/bin/python3').exists() else [])}
    if needs_yaml and not all(command_ok([python, '-c', 'import yaml']) for python in pythons):
        missing.append('yaml')
    if (host_helper or (components['comfyui'] and download_comfy)) and not command_ok([sys.executable, '-c', 'import ensurepip, venv']):
        missing.append('venv')
    # Core Backend builds llama.cpp on the host: cmake, Ninja and a C++ compiler.
    if host_helper and not (shutil.which('cmake') and (shutil.which('ninja') or shutil.which('samu'))
                            and (shutil.which('c++') or shutil.which('g++'))):
        missing.append('build')
    if not missing:
        return
    host = distro()
    manager = host['manager']
    packages = pkgmanager.packages(manager, *missing) if manager else []
    if not packages:
        if 'build' in missing and len(missing) == 1:
            log_line('cmake/Ninja/a C++ compiler are missing; Core Backend builds need them. Install them, then build again.')
            return
        raise RuntimeError(f'Install {", ".join(missing)} for Python 3 (or turn those components off), then retry.')
    refresh_packages(manager, progress, 24)
    run_with_retries(privileged(pkgmanager.install_command(manager, packages)), progress, 24,
                     f'Installing {", ".join(packages)}…', 'Retrying failed package downloads')


def run_backend_job(dest, job, use_group, progress, percent, message):
    """A Models → Core Backend job (hostctl/coreextras.py), run here and now instead of as a unit."""
    script = dest / 'BunnyBytes' / 'hostctl' / 'coreextras.py'
    log = dest / 'core-backend-extras' / f'{job}.log'
    try:
        run_command(docker_command([sys.executable, str(script), job], use_group), progress, percent, message,
                    cwd=dest / 'BunnyBytes' / 'hostctl')
    except CommandFailed as exc:
        tail = log.read_text(errors='replace').splitlines()[-40:] if log.is_file() else []
        for line in tail:
            log_line(line)
        raise CommandFailed(str(exc), '\n'.join(tail) or exc.output) from exc
    finally:
        if log.is_file():
            log_line(f'(full log: {log})')


def comfy_python():
    """The newest Python 3.10–3.14 on PATH (PyTorch's ROCm wheels cover those)."""
    for name in ('python3.13', 'python3.12', 'python3.14', 'python3.11', 'python3.10', 'python3'):
        if (path := shutil.which(name)) and command_ok([path, '-c', 'import sys; raise SystemExit(not (3, 10) <= sys.version_info[:2] <= (3, 14))']):
            return path
    raise RuntimeError('ComfyUI needs Python 3.10–3.14; install one (for example python3.13), then run Repair.')


def comfy_python():
    return comfyinstall.find_python()


def setup_comfyui(comfy, progress):
    """ComfyUI checkout + .venv + PyTorch for ROCm + requirements (hostctl/comfyinstall.py)."""
    def run(command, message, network):
        if network:
            run_with_retries(command, progress, 94, message, 'Retrying the download')
        else:
            run_command(command, progress, 93, message)
    comfyinstall.setup(comfy, run, find=comfy_python)


def setup_supervisor(dest, comfy):
    """The ComfyUI supervisor (port 8083) the studios ask to start ComfyUI on demand."""
    comfyinstall.install_supervisor(dest, comfy)


def remove_supervisor(dest):
    comfyinstall.remove_supervisor(dest)


def install_backends(dest, comfy, existing_comfy, components, use_group, progress):
    """Set up the chosen core backends. A failure is reported, never fatal: Rocsy is already running."""
    results = []

    def attempt(name, action):
        try:
            message = action()
            results.append({'name': name, 'ok': True, 'message': message})
        except Exception as exc:  # noqa: BLE001
            log_line(f'{name} failed: {exc}')
            results.append({'name': name, 'ok': False, 'message': f'{exc} Retry from Models → Manager → Core Backend.'})

    if components['ytdlp'] and not ytdlp_ready(dest):
        attempt('yt-dlp', lambda: run_backend_job(dest, 'ytdlp-install', use_group, progress, 93, 'Downloading yt-dlp…') or 'Installed.')
    if components['comfyui']:
        def comfyui():
            if not existing_comfy:
                setup_comfyui(comfy, progress)
            if not systemd_user_ok():
                return (f'Installed, but the supervisor needs systemd. Start ComfyUI yourself: '
                        f'{comfy}/.venv/bin/python {comfy}/main.py --listen 127.0.0.1 --port 8188')
            setup_supervisor(dest, comfy)
            note = ''
            if windowshost.is_wsl() and not windowshost.gpu_status()['rocm_wsl']:
                note = f' ROCm for WSL is missing, so it can\'t use the GPU yet: {windowshost.ROCM_WSL_DOCS}'
            return ('Ready; the studios start it when they need it.' if not existing_comfy else 'Supervisor set up for your ComfyUI folder.') + note
        attempt('ComfyUI', comfyui)
    if components['hermes'] and not hermes_ready(dest):
        def hermes():
            run_backend_job(dest, 'hermes-download', use_group, progress, 96,
                            'Downloading and building Hermes Agent (Personal + Rocsy)…')
            env_file = dest / 'BunnyBytes' / '.env'
            profiles = set(env_value(env_file, 'COMPOSE_PROFILES').split(',')) | {'hermes'}
            set_env_value(env_file, 'COMPOSE_PROFILES', ','.join(p for p in ALL_PROFILES if p in profiles))
            return 'Installed and running.'
        attempt('Hermes Agent', hermes)
    return results


def check_wsl(payload, dest):
    """Windows (WSL2, untested): fail before copying anything when the distro isn't ready."""
    if not windowshost.is_wsl():
        return None
    windows = windowshost.windows_version()
    if windows and not windows['wsl2']:
        raise ValueError(windows['reason'])
    if not Path('/run/systemd/system').is_dir():
        raise ValueError('systemd is off in this WSL distro. Run “Install Rocsy (Windows).cmd” from Windows: '
                         'it turns systemd on and restarts the distro.')
    if (payload.get('desktop_app') or payload.get('windows_autostart')) and not windowshost.SAFE_DEST.match(str(dest)):
        raise ValueError('For the Windows shortcuts, the Rocsy folder may only use letters, digits, spaces and . _ - /')
    return windows


def setup_wsl_compose(compose_dir, env_file, use_group, progress):
    """GPU containers get WSL's /dev/dxg instead of /dev/kfd + /dev/dri (docker-compose.wsl.yml)."""
    progress(88, 'Adapting the containers to WSL (Windows, untested)…')
    version = subprocess.run(docker_command(['docker', 'compose', 'version', '--short'], use_group),
                             capture_output=True, text=True, timeout=30).stdout
    if not windowshost.compose_supports_override(version):
        raise RuntimeError(f'Docker Compose {version.strip() or "(unknown)"} is too old for WSL; 2.24 or newer is needed.')
    result = subprocess.run(docker_command(['docker', 'compose', '-f', 'docker-compose.yml', *profile_args(ALL_PROFILES),
                                            'config', '--no-interpolate', '--format', 'json'], use_group),
                            cwd=compose_dir, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError('docker compose config failed: ' + (result.stderr.strip().splitlines() or ['?'])[-1])
    gpu = windowshost.gpu_status()
    (compose_dir / windowshost.OVERRIDE_NAME).write_text(
        windowshost.compose_override(json.loads(result.stdout), gpu['dxg'], gpu['rocm_wsl']))
    set_env_value(env_file, 'COMPOSE_FILE', windowshost.compose_files_value(compose_dir))
    if not gpu['dxg']:
        log_line('WSL has no GPU (/dev/dxg missing): containers start without one. Install the current AMD '
                 'Windows driver, then run Repair.')
    elif not gpu['rocm_wsl']:
        log_line(f'ROCm for WSL is not installed in this distro, so ROCm apps can\'t use the GPU yet: {windowshost.ROCM_WSL_DOCS}')
    return gpu


def apply_windows_shortcuts(dest, payload, info, distro):
    """Rocsy Desktop on Windows = Desktop + Start menu shortcuts (Edge app window); optional Startup entry."""
    windowshost.remove_shortcuts(info.get('windows_shortcuts'), dest)
    info.pop('windows_shortcuts', None)
    if not (payload.get('desktop_app') or payload.get('windows_autostart')):
        return None
    port = int(env_value(dest / 'BunnyBytes' / '.env', 'GATEWAY_PORT') or 8080)
    record = windowshost.install_shortcuts(dest, distro, port, dest / 'BunnyBytes/gateway/static/icons/rocsy.ico',
                                           bool(payload.get('desktop_app')), bool(payload.get('windows_autostart')))
    info['windows_shortcuts'] = record
    return record


def install(payload, progress=lambda _percent, _message: None):
    progress(2, 'Checking install location…')
    dest = safe_destination(payload.get('destination'))
    prior = existing_install(dest)
    mode = payload.get('mode')
    if prior and mode not in {'update', 'repair', 'reinstall'}:
        raise ValueError('Choose Update, Repair, or Complete reinstall for this Rocsy installation.')
    if not prior and mode != 'install':
        raise ValueError('This folder is not a recognized Rocsy installation.')
    if mode == 'reinstall':
        if not prior['owned_destination']:
            raise ValueError('Complete reinstall is unavailable because this folder contained other files before Rocsy was installed.')
        phrase = payload.get('confirmation_phrase')
        password = payload.get('current_password')
        if phrase != 'REINSTALL ROCSY' or not isinstance(password, str):
            raise ValueError('Type REINSTALL ROCSY to confirm deletion.')
        if reinstall_password_required(dest, prior):
            if not verify_existing_password(dest, password):
                raise ValueError('Enter the current Rocsy password to reinstall.')
        elif password and not verify_existing_password(dest, password):
            raise ValueError('Leave the current password blank or enter the correct password.')
    password_mode = payload.get('password_mode', 'custom')
    if mode in {'install', 'reinstall'}:
        if password_mode not in {'custom', 'none'}:
            raise ValueError('Choose how Rocsy should protect access.')
        if password_mode == 'custom':
            chosen_password = payload.get('new_password')
            if (not isinstance(chosen_password, str) or len(chosen_password) < 8 or len(chosen_password) > 200
                    or chosen_password != chosen_password.strip()
                    or any(ch in chosen_password for ch in ('\n', '\r', '\0', '$', '#', '\\', '"', "'"))):
                raise ValueError('Use 8 to 200 characters, without leading/trailing spaces or $, #, quotes, or backslashes.')
        else:
            chosen_password = None
        if password_mode == 'none' and payload.get('no_password_ack') is not True:
            raise ValueError('Confirm that anyone who can reach Rocsy can sign in and uninstall it without a password.')
    else:
        chosen_password = None
    if type(payload.get('desktop_launcher', False)) is not bool:
        raise ValueError('Choose whether to create a desktop launcher.')
    for key in ('terminal_command', 'desktop_app', 'host_helper'):
        if type(payload.get(key, False)) is not bool:
            raise ValueError('Choose which terminal and desktop integrations to install.')
    # Fail before copying or building anything when a toggle can't be applied.
    if problem := integrations.conflicts(dest, payload.get('terminal_command', False), payload.get('desktop_app', False)):
        raise ValueError(problem)
    if payload.get('host_helper'):
        unit_file = USER_UNITS / HOST_HELPERS[True]
        if unit_file.exists() and str(dest / 'BunnyBytes' / 'hostctl') not in unit_file.read_text():
            raise ValueError('bunny-hostctl is already set up for another Rocsy installation. Turn off the host services helper.')
    if type(payload.get('allow_system_upgrade', False)) is not bool:
        raise ValueError('Choose whether system updates are allowed.')
    if type(payload.get('windows_autostart', False)) is not bool:
        raise ValueError('Choose whether Rocsy starts when you sign in to Windows.')
    windows = check_wsl(payload, dest)
    if windows:
        payload = {**payload, 'desktop_launcher': False}  # Linux desktop files; Windows gets shortcuts instead
    components = chosen_components(payload.get('components'), dest if prior and mode in {'update', 'repair'} else None)
    owned_destination = not dest.exists() or not any(dest.iterdir())
    if not prior and dest.exists() and any(dest.iterdir()) and not payload.get('allow_existing'):
        raise ValueError('This folder already has files. Confirm that you want to add Rocsy there.')
    comfy_raw = payload.get('comfy_root', '').strip()
    if prior and mode in {'update', 'repair'} and not comfy_raw:
        prior_env = dest / 'BunnyBytes' / '.env'
        if prior_env.is_file():
            for line in prior_env.read_text().splitlines():
                if line.startswith('ROCSY_COMFY_ROOT='):
                    comfy_raw = line.partition('=')[2].strip()
                    break
    comfy = Path(os.path.expandvars(os.path.expanduser(comfy_raw))).resolve() if comfy_raw else dest / 'external' / 'ComfyUI'
    if comfy_raw and (not comfy.is_dir() or not (comfy / 'models').is_dir()):
        raise ValueError('The ComfyUI folder must exist and contain a models folder.')
    progress(3, 'Checking Docker dependencies…')
    had_docker_group = account_in_docker_group()
    use_group = ensure_docker(progress, payload.get('allow_system_upgrade', False))
    ensure_host_tools(progress, components, payload.get('host_helper', False), not comfy_raw)
    docker_group_added = not had_docker_group and account_in_docker_group()
    if mode == 'reinstall':
        progress(25, 'Removing the existing Rocsy installation and saved data…')
        compose_dir = dest / 'BunnyBytes'
        compose_file = compose_dir / 'docker-compose.yml'
        if not compose_file.is_file() or compose_file.is_symlink():
            raise ValueError('The existing Rocsy Compose file is missing or unsafe.')
        run_command(docker_command(['docker', 'compose', *profile_args(ALL_PROFILES), 'down', '--remove-orphans', '--volumes'], use_group),
                    progress, 25, 'Removing Rocsy containers and volumes…', cwd=compose_dir)
        remove_managed_launcher(prior, dest)
        if windows:
            windowshost.remove_shortcuts(prior.get('windows_shortcuts'), dest)
        remove_supervisor(dest)
        integrations.remove_all(dest)
        shutil.rmtree(dest)
        owned_destination = True
    progress(27, 'Preparing destination…')
    dest.mkdir(parents=True, exist_ok=True)
    start_log_file(dest)
    sources = ('BunnyBytes', 'H3Machine', 'Music', 'Krea', 'Llama', 'Trainer')
    total = sum(len(files) for name in sources for _, _, files in os.walk(BUNDLE / name)) or 1
    copied = 0
    for name in sources:
        progress(30 + int(40 * copied / total), f'Copying {name}…')
        def copy_file(src, target):
            nonlocal copied
            result = shutil.copy2(src, target)
            copied += 1
            with STATE_LOCK:
                STATE['progress'] = 30 + int(40 * copied / total)
            return result
        shutil.copytree(BUNDLE / name, dest / name, dirs_exist_ok=True, copy_function=copy_file)
    progress(72, 'Preparing backend folders…')
    (dest / 'external' / 'ComfyUI').mkdir(parents=True, exist_ok=True)
    # Read-only mounts of REA and the Agent: created here so Docker doesn't make them root-owned.
    # Models → Core Backend fills them (REA engines/mod loaders, yt-dlp).
    for relative in ('rea-engines', 'rea-mod-installs', 'yt-dlp'):
        (dest / 'core-backend-extras' / relative).mkdir(parents=True, exist_ok=True)
    try:
        for relative in ('models', 'input/VoiceRefs', 'output/postedit-sep'):
            (comfy / relative).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f'ComfyUI folder is not writable: {exc}') from exc
    env_file = dest / 'BunnyBytes' / '.env'
    password = None
    progress(76, 'Configuring Rocsy…')
    if not env_file.exists():
        values = {
            'MASTER_PASSWORD': chosen_password if password_mode == 'custom' else secrets.token_urlsafe(18),
            'COOKIE_SECRET': secrets.token_urlsafe(32),
            'LLAMA_API_KEY': secrets.token_urlsafe(32),
            'QUEUE_INTERNAL_SECRET': secrets.token_urlsafe(32),
            'HOSTCTL_SECRET': secrets.token_urlsafe(32),
            'ROCSY_UID': str(os.getuid()),
            'ROCSY_GID': str(os.getgid()),
            'ROCSY_ROOT': str(dest),
        }
        lines = (dest / 'BunnyBytes' / '.env.example').read_text().splitlines()
        present = set()
        for index, line in enumerate(lines):
            key, sep, value = line.partition('=')
            if sep and key in values:
                present.add(key)
                if not value:
                    lines[index] = f'{key}={values[key]}'
        lines.extend(f'{key}={value}' for key, value in values.items() if key not in present)
        lines = [line for line in lines if not line.startswith(('ROCSY_COMFY_ROOT=', 'COMFY_URL='))]
        lines.extend([f'ROCSY_COMFY_ROOT={comfy}', 'COMFY_URL=http://127.0.0.1:8188'])
        if password_mode == 'none':
            lines.append('ROCSY_NO_PASSWORD=1')
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write('\n'.join(lines) + '\n')
        progress(81, 'Created local credentials.')
    else:
        progress(81, 'Preserved existing credentials.')
        current_env = env_file.read_text()
        missing_ids = [f'{key}={value}' for key, value in
                       (('ROCSY_UID', os.getuid()), ('ROCSY_GID', os.getgid()), ('ROCSY_ROOT', dest))
                       if not any(line.startswith(f'{key}=') for line in current_env.splitlines())]
        if missing_ids:
            with env_file.open('a') as stream:
                stream.write(('' if current_env.endswith('\n') or not current_env else '\n')
                             + '\n'.join(missing_ids) + '\n')
    marker = dest / '.rocsy-install.json'
    if not marker.exists():
        marker.write_text(json.dumps({'format': 1, 'app': 'Rocsy',
                                      'owned_destination': owned_destination,
                                      'password_mode': password_mode}) + '\n')
    progress(85, 'Preparing the host helper…')
    if systemd_user_ok():
        setup_host_helper(dest, env_file, payload.get('host_helper', False))
    else:
        # OpenRC/runit/dinit systems: no user units, so no helper (Settings → Services, uninstall from Settings).
        log_line('No systemd here: the host services helper is skipped. Uninstall with `docker compose down` in BunnyBytes/.')
        payload = {**payload, 'host_helper': False}
    compose_dir = dest / 'BunnyBytes'
    previous_profiles = set(env_value(env_file, 'COMPOSE_PROFILES').split(',')) - {''}
    profiles = [name for name in SERVICE_PROFILES if components[name]]
    if hermes_ready(dest):
        profiles.append('hermes')  # its image builds from the downloaded source
    set_env_value(env_file, 'COMPOSE_PROFILES', ','.join(profiles))
    gpu = setup_wsl_compose(compose_dir, env_file, use_group, progress) if windows else None
    prepare_bind_folders(compose_dir)
    # Services turned off in an Update/Repair: stop and remove their containers (data volumes stay).
    dropped = [service for name in sorted(previous_profiles - set(profiles)) for service in PROFILE_SERVICES.get(name, [])]
    if dropped:
        run_command(docker_command(['docker', 'compose', *profile_args(ALL_PROFILES), 'rm', '--stop', '--force', *dropped], use_group),
                    progress, 86, 'Removing services you turned off…', cwd=compose_dir)
    # Image builds download from Debian/Arch mirrors and CDNs that drop connections now and
    # then. BuildKit caches every finished layer, so a retry only redoes the failed step.
    run_with_retries(docker_command(['docker', 'compose', 'build'], use_group), progress, 90,
                     'Building Rocsy images…', 'Retrying failed image builds', transient_only=False, cwd=compose_dir)
    prepare_bind_mount_permissions(compose_dir, use_group, progress)
    command = ['docker', 'compose', 'up', '-d']
    if mode == 'repair':
        command.append('--force-recreate')
    run_command(docker_command(command, use_group), progress, 92,
                'Starting Rocsy…', cwd=compose_dir)
    backends = install_backends(dest, comfy, bool(comfy_raw), components, use_group, progress)
    launcher = None
    if payload.get('desktop_launcher'):
        progress(97, 'Creating desktop launcher…')
        launcher = create_desktop_launcher(dest)
        info = json.loads(marker.read_text())
        info['desktop_launcher'] = str(launcher)
        marker.write_text(json.dumps(info) + '\n')
    progress(98, 'Setting up the rocsy command and Rocsy Desktop…')
    info = json.loads(marker.read_text())
    shortcuts = None
    if windows:
        # Rocsy Desktop is a Windows shortcut here; the Linux menu entry would need WSLg + Chromium.
        added = apply_integrations(dest, {**payload, 'desktop_app': False}, info)
        progress(98, 'Creating the Windows shortcuts…')
        try:
            shortcuts = apply_windows_shortcuts(dest, payload, info, os.environ.get('WSL_DISTRO_NAME', ''))
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            log_line(f'Windows shortcuts failed: {exc}')
            backends.append({'name': 'Windows shortcuts', 'ok': False,
                             'message': f'{exc} Start Rocsy with `rocsy up` in Ubuntu, then open http://127.0.0.1:8080/.'})
        info['windows_autostart'] = bool(payload.get('windows_autostart')) and bool(shortcuts)
    else:
        added = apply_integrations(dest, payload, info)
    info['host_helper'] = payload.get('host_helper', False)
    marker.write_text(json.dumps(info) + '\n')
    progress(99, 'Rocsy containers started.')
    return {'terminal_command': added.get('terminal_command'), 'desktop_app': added.get('desktop_app'),
            'host_helper': payload.get('host_helper', False),
            'destination': str(dest), 'mode': mode, 'password': password, 'no_password': password_mode == 'none' and mode in {'install', 'reinstall'}, 'url': 'http://127.0.0.1:8080/',
            'docker_group_added': docker_group_added, 'docker_session_needs_refresh': use_group,
            'command': f'cd {shlex.quote(str(compose_dir))} && docker compose up -d --build', 'comfy': str(comfy),
            'desktop_launcher': str(launcher) if launcher else None, 'backends': backends,
            'components': components,
            'windows': windowshost.platform() if windows else None, 'gpu': gpu,
            'windows_shortcuts': shortcuts['links'] if shortcuts else []}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type='application/json'):
        data = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == '/':
            config = {'preview': False, 'token': TOKEN, 'distro': distro(), 'prerequisites': prerequisites(), 'default_dir': DEFAULT_DIR,
                      'windows': windowshost.platform() if windowshost.is_wsl() else None}
            html = UI.replace('__ROCSY_CONFIG__', json.dumps(config).replace('<', '\\u003c'))
            return self._send(200, html, 'text/html')
        if url.path == '/status' and self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}':
            query = parse_qs(url.query)
            if query.get('token') != [TOKEN]:
                return self._send(403, '{"error":"Request rejected."}')
            with STATE_LOCK:
                if query.get('job') != [STATE['job_id']]:
                    return self._send(404, '{"error":"Job not found."}')
                snapshot = dict(STATE)
            return self._send(200, json.dumps(snapshot))
        return self._send(404, '{}')

    def do_POST(self):
        if self.path not in {'/install', '/inspect'} or self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}' or self.headers.get('Origin') != f'http://127.0.0.1:{self.server.server_port}' or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self._send(403, '{"error":"Request rejected."}')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size < 1 or size > 4096:
                raise ValueError('Invalid request size.')
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict) or payload.get('token') != TOKEN:
                return self._send(403, '{"error":"Request rejected."}')
            if self.path == '/inspect':
                dest = safe_destination(payload.get('destination'))
                prior = existing_install(dest)
                password_required = bool(prior and reinstall_password_required(dest, prior))
                # Update/Repair start from what this install has now.
                current = {'terminal_command': integrations.owns_command(dest),
                           'desktop_app': (bool((prior.get('windows_shortcuts') or {}).get('links')) if windowshost.is_wsl()
                                           else integrations.owns_desktop(dest)),
                           'windows_autostart': bool(prior.get('windows_autostart')),
                           'host_helper': host_helper_installed(dest),
                           'components': current_components(dest)} if prior else None
                return self._send(200, json.dumps({'installed': bool(prior),
                                                    'can_reinstall': bool(prior and prior['owned_destination']),
                                                    'password_required': password_required, 'current': current}))
            with STATE_LOCK:
                if STATE['phase'] == 'running':
                    return self._send(409, '{"error":"An installation is already running."}')
                job_id = secrets.token_urlsafe(18)
                STATE.update(job_id=job_id, phase='running', progress=0, message='Starting…', log=['Starting…'], result=None)
            threading.Thread(target=run_install, args=(job_id, payload), daemon=True).start()
            self._send(202, json.dumps({'job_id': job_id}))
        except (ValueError, OSError) as exc:
            self._send(400, json.dumps({'error': str(exc)}))

    def log_message(self, *_args):
        pass


# ---- terminal mode ----

TERMINAL_HELP = """Install Rocsy from the terminal.

  sh "Install Rocsy.sh"               the installer in your browser (default)
  sh "Install Rocsy.sh" --terminal    the same questions, asked here
  sh "Install Rocsy.sh" --defaults    the default install, no questions (-y)

Options:
  --dir PATH     the Rocsy folder (default: %s)
  --verbose      show every command's output, not only the steps
  --help         this text

The default install: Rocsy in the default folder with Motion, ComfyUI (downloaded, with its
supervisor), Hermes Agent and yt-dlp; REA and the Trainer are left out. It sets a generated
password (printed at the end), the rocsy command, Rocsy Desktop and the host services helper.
On a folder that already has Rocsy, the default is Update, which keeps everything as it is.
Without a graphical session the installer starts in the terminal by itself.
On Windows, run "Install Rocsy (Windows).cmd" instead (untested): it runs this inside WSL2."""
FLAG_ALIASES = {'--cli': '--terminal', '-t': '--terminal', '-y': '--defaults', '--yes': '--defaults', '-h': '--help'}


class Style:
    def __init__(self, enabled):
        codes = {'bold': '1', 'dim': '2', 'accent': '38;5;215', 'ok': '32', 'bad': '31', 'warn': '33'}
        for name, code in codes.items():
            setattr(self, name, (lambda text, c=code: f'\033[{c}m{text}\033[0m') if enabled else (lambda text: text))


def parse_terminal_args(argv):
    """argv after the bundle and default folder → options; options['ui'] means the browser installer."""
    options = {'terminal': False, 'defaults': False, 'verbose': False, 'help': False, 'dir': None, 'windows': False}
    items = list(argv)
    while items:
        arg = FLAG_ALIASES.get(items[0], items[0])
        items.pop(0)
        if arg == '--dir' and items:
            options['dir'] = items.pop(0)
        elif arg.startswith('--dir='):
            options['dir'] = arg.partition('=')[2]
        elif arg in ('--terminal', '--defaults', '--verbose', '--help', '--windows'):
            options[arg[2:]] = True
        else:
            raise SystemExit(f'Unknown option {arg!r}. Run with --help to see the options.')
    # --windows: started by “Install Rocsy (Windows).cmd”; the browser UI opens in Windows.
    graphical = os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY') or options['windows']
    options['ui'] = bool(graphical) and not (options['terminal'] or options['defaults'] or options['help'] or options['verbose'])
    return options


class Prompter:
    def __init__(self, style, read=input, secret=None):
        import getpass
        self.s, self.read, self.secret = style, read, secret or getpass.getpass

    def text(self, label, default=''):
        answer = self.read(f'  {label}' + (f' [{self.s.dim(default)}]' if default else '') + ': ').strip()
        return answer or default

    def yes(self, label, default):
        hint = 'Y/n' if default else 'y/N'
        while True:
            answer = self.read(f'  {label} [{hint}]: ').strip().lower()
            if not answer:
                return default
            if answer in ('y', 'yes', 'n', 'no'):
                return answer.startswith('y')
            print('  Answer y or n.')

    def choose(self, label, options, default):
        """options: [(key, text)]; returns the chosen key."""
        print(f'  {label}')
        for number, (key, text) in enumerate(options, 1):
            print(f'    {number}) {text}' + (self.s.dim('  (default)') if key == default else ''))
        while True:
            answer = self.read('  Choose a number: ').strip()
            if not answer:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1][0]
            print(f'  Enter 1–{len(options)}, or press Enter for the default.')

    def password(self):
        while True:
            first = self.secret('  New password (8–200 characters): ')
            if (len(first) < 8 or len(first) > 200 or first != first.strip()
                    or any(ch in first for ch in ('\n', '\r', '\0', '$', '#', '\\', '"', "'"))):
                print('  Use 8–200 characters, no spaces at either end, and none of $ # " \' \\')
                continue
            if self.secret('  Again: ') == first:
                return first
            print("  The passwords don't match; try again.")


COMPONENT_TEXT = {
    'motion': ('Motion', 'the video renderer the Master Agent uses for edits (Remotion)'),
    'rea': ('REA', 'game modding tools (experimental)'),
    'trainer': ('Trainer', 'LoRA training; a 20+ GB image'),
    'comfyui': ('ComfyUI', 'every studio renders through it; downloads ComfyUI + PyTorch for ROCm (several GB)'),
    'hermes': ('Hermes Agent', 'runs the Master Agent and Agentic modes'),
    'ytdlp': ('yt-dlp', 'web video references for the agent'),
}


def default_payload(dest, prior):
    """The default install (or Update of an existing one), as an install() payload."""
    payload = {'destination': str(dest), 'comfy_root': '', 'allow_existing': False, 'desktop_launcher': False,
               'allow_system_upgrade': False, 'components': None, 'windows_autostart': False}
    if prior:
        info = prior if isinstance(prior, dict) else {}
        payload.update(mode='update', terminal_command=integrations.owns_command(dest),
                       desktop_app=(bool((info.get('windows_shortcuts') or {}).get('links')) if windowshost.is_wsl()
                                    else integrations.owns_desktop(dest)),
                       windows_autostart=bool(info.get('windows_autostart')), host_helper=host_helper_installed(dest))
    else:
        payload.update(mode='install', terminal_command=True, desktop_app=True, host_helper=True,
                       password_mode='custom', new_password=secrets.token_urlsafe(18))
    return payload


def customize(ask, style, dest_default, family):
    """Walk the installer's steps as questions; returns (payload, generated password or None)."""
    print(style.bold('\n1. Location'))
    while True:
        try:
            dest = safe_destination(ask.text('Rocsy folder', dest_default))
            prior = existing_install(dest)
            break
        except ValueError as exc:
            print('  ' + style.bad(str(exc)))
    payload = default_payload(dest, prior)
    generated = None
    if prior:
        payload['mode'] = ask.choose('Rocsy is already installed here. What should happen?', [
            ('update', 'Update: new app files, rebuilt containers; settings and data kept'),
            ('repair', 'Repair: restore app files, recreate every container; settings and data kept'),
            *([('reinstall', 'Complete reinstall: DELETE this installation, its data and Docker volumes')]
              if prior['owned_destination'] else [])], 'update')
        if payload['mode'] == 'reinstall':
            phrase = ask.text('Type REINSTALL ROCSY to confirm deletion')
            payload['confirmation_phrase'] = phrase
            payload['current_password'] = ask.secret('  Current Rocsy password (Enter if you never set one): ')
    elif dest.exists() and any(dest.iterdir()):
        if not ask.yes(f'{dest} already has files. Add Rocsy to it anyway?', False):
            raise SystemExit('Nothing was installed.')
        payload['allow_existing'] = True
    payload['comfy_root'] = ask.text('Existing ComfyUI folder (Enter to let Rocsy set one up)', '')

    print(style.bold('\n2. What to install'))
    kept = current_components(dest) if prior and payload['mode'] != 'reinstall' else {}
    defaults = kept or COMPONENTS
    chosen = {}
    for key in ('motion', 'rea', 'trainer', 'comfyui', 'hermes', 'ytdlp'):
        name, text = COMPONENT_TEXT[key]
        if key in ('comfyui', 'hermes', 'ytdlp') and kept.get(key):
            print(f'  {name}: {style.ok("installed")} (kept; remove it in Models → Core Backend)')
            chosen[key] = True
            continue
        chosen[key] = ask.yes(f'{name} — {text}?', defaults[key])
    payload['components'] = chosen

    if payload['mode'] in ('install', 'reinstall'):
        print(style.bold('\n3. Access'))
        how = ask.choose('Rocsy password', [('generate', 'Generate one for me (printed at the end)'),
                                            ('custom', 'Set my own'),
                                            ('none', 'No password (trusted machines only)')], 'generate')
        if how == 'custom':
            payload.update(password_mode='custom', new_password=ask.password())
        elif how == 'none':
            print('  ' + style.warn('Anyone who can reach Rocsy can use it, change settings and uninstall it.'))
            if not ask.yes('Install without a password?', False):
                raise SystemExit('Nothing was installed.')
            payload.update(password_mode='none', no_password_ack=True, new_password='')
        else:
            generated = payload['new_password'] = secrets.token_urlsafe(18)
            payload['password_mode'] = 'custom'

    print(style.bold('\n4. Desktop & helpers'))
    if windowshost.is_wsl():
        payload['desktop_app'] = ask.yes('Rocsy shortcuts on the Windows desktop and Start menu?', payload['desktop_app'])
        payload['windows_autostart'] = ask.yes('Start Rocsy in the background when you sign in to Windows?', payload['windows_autostart'])
        payload['terminal_command'] = ask.yes('The rocsy terminal command (in Ubuntu)?', payload['terminal_command'])
    else:
        payload['desktop_app'] = ask.yes('Rocsy Desktop (its own window, in the app menu)?', payload['desktop_app'])
        payload['terminal_command'] = ask.yes('The rocsy terminal command?', payload['terminal_command'])
        payload['desktop_launcher'] = ask.yes('A "Start Rocsy" icon on the desktop?', False)
    payload['host_helper'] = ask.yes('Host services helper (Settings → Services, GPU tuning, Core Backend)?', payload['host_helper'])
    if family == 'pacman':
        payload['allow_system_upgrade'] = ask.yes('If Docker packages need installing, refresh mirrors and update the whole system?', False)
    return payload, generated


def summary_lines(payload, style, generated=None):
    if payload['components'] is not None:
        chosen = payload['components']
    elif payload['mode'] in ('update', 'repair'):
        chosen = current_components(Path(payload['destination']))
    else:
        chosen = COMPONENTS
    on = [COMPONENT_TEXT[key][0] for key, value in chosen.items() if value]
    off = [COMPONENT_TEXT[key][0] for key, value in chosen.items() if not value]
    access = ('current password kept' if payload['mode'] in ('update', 'repair') else 'no password' if payload.get('password_mode') == 'none'
              else 'generated password (shown at the end)' if generated else 'your own password')
    desk = [name for key, name in (('desktop_app', 'Windows shortcuts' if windowshost.is_wsl() else 'Rocsy Desktop'),
                                   ('windows_autostart', 'starts at Windows sign-in'), ('terminal_command', 'rocsy command'),
                                   ('desktop_launcher', 'desktop launcher'), ('host_helper', 'host helper')) if payload.get(key)]
    return [f'  Folder      {payload["destination"]}  ({payload["mode"]})',
            f'  ComfyUI     {payload["comfy_root"] or "set up inside the Rocsy folder"}',
            f'  Installs    {", ".join(on) or "core apps only"}' + (style.dim(f'  (skipped: {", ".join(off)})') if off else ''),
            f'  Access      {access}',
            f'  Desktop     {", ".join(desk) or "none"}']


def terminal_main(options, read=input, secret=None):
    tty = sys.stdin.isatty()
    style = Style(sys.stdout.isatty() and not os.environ.get('NO_COLOR'))
    if options['help']:
        print(TERMINAL_HELP % DEFAULT_DIR)
        return 0
    TERMINAL['on'] = True
    host = distro()
    state = prerequisites()
    print(style.accent(style.bold('Rocsy installer')) + style.dim('  ·  terminal'))
    windows = windowshost.platform() if windowshost.is_wsl() else None
    if windows:
        print('  ' + style.warn(f'{windows["name"]} — {windowshost.UNTESTED} Rocsy runs inside WSL2.'))
    print(f'  {host["name"]}: ' + ', '.join(
        (style.ok('✓ ') if state[key] else style.warn('○ ')) + name for key, name in
        (('python', 'Python 3.9+'), ('docker', 'Docker'), ('compose', 'Compose'), ('buildx', 'Buildx'), ('daemon', 'Docker access'))))
    if host['manager']:
        print(style.dim(f'  Packages: {host["manager_name"]} · services: {host["init"]}'))
    if not all(state.values()) and host['family'] == 'other':
        print(style.bad('  Install Docker Engine, Compose and Buildx first:' if not host['immutable']
                        else f'  This system is read-only ({host["immutable"]}). Set Docker up first:'))
        print('\n'.join('    ' + line for line in host['manual'].splitlines()))
        return 1
    ask = Prompter(style, read, secret)
    generated = None
    try:
        if options['defaults']:
            dest = safe_destination(options['dir'] or DEFAULT_DIR)
            prior = existing_install(dest)
            if not prior and dest.exists() and any(dest.iterdir()):
                print(style.bad(f'  {dest} already has files. Choose another folder with --dir, or run --terminal to confirm adding Rocsy there.'))
                return 1
            payload = default_payload(dest, prior)
            generated = payload.get('new_password')
        else:
            if not tty:
                print(style.bad('  There is no terminal to ask questions in. Run with --defaults for the default install.'))
                return 1
            print('\n  Press Enter for the default install, or type ' + style.bold('c') + ' to choose the options yourself.')
            if read('  > ').strip().lower().startswith('c'):
                payload, generated = customize(ask, style, options['dir'] or DEFAULT_DIR, host['family'])
            else:
                dest = safe_destination(options['dir'] or DEFAULT_DIR)
                prior = existing_install(dest)
                if not prior and dest.exists() and any(dest.iterdir()):
                    print(style.warn(f'  {dest} already has files, so the options are needed.'))
                    payload, generated = customize(ask, style, str(dest), host['family'])
                else:
                    payload = default_payload(dest, prior)
                    generated = payload.get('new_password')
        print(style.bold('\nSummary'))
        print('\n'.join(summary_lines(payload, style, generated)))
        if not options['defaults'] and not ask.yes('Start now?', True):
            print('Nothing was installed.')
            return 1
    except (KeyboardInterrupt, EOFError):
        print('\nNothing was installed.')
        return 130
    except ValueError as exc:
        print(style.bad(f'  {exc}'))
        return 1

    last = {'message': ''}

    def progress(percent, message):
        if message != last['message']:
            last['message'] = message
            print(f'{style.accent(f"[{percent:3d}%]")} {message}', flush=True)
    if options['verbose']:
        TERMINAL['echo'] = lambda line: print(style.dim('        ' + line), flush=True)
    print()
    try:
        result = install(payload, progress)
    except KeyboardInterrupt:
        print(style.bad('\nStopped. Run the installer again and choose Repair to finish.'))
        return 130
    except Exception as exc:  # noqa: BLE001
        print(style.bad(f'\nInstallation failed: {exc}'))
        tail = getattr(exc, 'output', '')
        if tail:
            print(style.dim('\n'.join('  ' + line for line in tail.splitlines()[-25:])))
        if LOG_FILE:
            print(f'Full log: {LOG_FILE}')
        return 1
    print(style.ok(style.bold('\nRocsy is ' + {'update': 'updated', 'repair': 'repaired'}.get(result['mode'], 'installed') + '.')))
    print(f'  Open        {result["url"]}')
    if generated and payload.get('password_mode') == 'custom' and result['mode'] in ('install', 'reinstall'):
        print(f'  Password    {style.bold(generated)}   ← save it now; change it in Settings → Security')
    if result.get('no_password'):
        print('  Password    none: anyone who can reach Rocsy can use it')
    if result.get('terminal_command'):
        print('  Terminal    rocsy' + ('' if result['terminal_command'].get('on_path') else '  (add ~/.local/bin to PATH, then open a new terminal)'))
    if result.get('desktop_app'):
        print('  Desktop     Rocsy Desktop is in your app menu')
    for link in result.get('windows_shortcuts') or []:
        print(f'  Windows     {link}')
    if result.get('windows'):
        print(style.warn(f'  {windowshost.UNTESTED} The Rocsy shortcut starts WSL and the containers, then opens Rocsy.'))
    for item in result.get('backends') or []:
        mark = style.ok('✓') if item['ok'] else style.bad('✗')
        print(f'  {mark} {item["name"]}: {item["message"]}')
    if result.get('docker_group_added') or result.get('docker_session_needs_refresh'):
        print(style.warn('  Sign out and back in before using docker compose without sudo (no restart needed).'))
    print(style.dim(f'  Rebuild later: {result["command"]}'))
    if LOG_FILE:
        print(style.dim(f'  Full log: {LOG_FILE}'))
    return 0


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    url = f'http://127.0.0.1:{server.server_port}/'
    print(f'Rocsy installer: {url}', flush=True)
    opener = windowshost.open_url if windowshost.is_wsl() else webbrowser.open
    threading.Timer(0.4, lambda: opener(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    OPTIONS = parse_terminal_args(sys.argv[3:])
    if OPTIONS['dir']:
        DEFAULT_DIR = OPTIONS['dir']  # also the folder the browser installer starts with
    if OPTIONS['ui']:
        main()
    else:
        sys.exit(terminal_main(OPTIONS))
