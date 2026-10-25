"""Installer command planning tests; no package manager or Docker is invoked."""
import importlib.util
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).with_name('installer_runtime.py')
INTEGRATIONS = Path(__file__).resolve().parents[1] / 'hostctl' / 'integrations.py'
REAL_CACHE = Path.home() / '.cache'
WORKER = Path(__file__).resolve().parents[1] / 'hostctl' / 'uninstall_worker.py'


def host(manager, distro_id, name, init='systemd'):
    """A distro() result, as pkgmanager.detect() + installer_runtime.distro() build it."""
    names = {'apt': 'APT', 'pacman': 'pacman', 'dnf': 'DNF', 'zypper': 'Zypper', 'apk': 'APK', 'xbps': 'XBPS'}
    return {'id': distro_id, 'name': name, 'distro': distro_id, 'like': [], 'manager': manager, 'manager_name': names.get(manager, ''),
            'init': init, 'immutable': '', 'family': manager or 'other', 'manual': ''}


UBUNTU = host('apt', 'ubuntu', 'Ubuntu 24.04')
ARCH = host('pacman', 'arch', 'Arch Linux')
CACHYOS = host('pacman', 'cachyos', 'CachyOS')


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bundle = self.root / 'bundle'
        (bundle / 'installer').mkdir(parents=True)
        (bundle / 'installer/ui.html').write_text('<html></html>')
        (bundle / 'BunnyBytes/hostctl').mkdir(parents=True)
        shutil.copy(INTEGRATIONS, bundle / 'BunnyBytes/hostctl/integrations.py')
        shutil.copy(INTEGRATIONS.with_name('comfyinstall.py'), bundle / 'BunnyBytes/hostctl/comfyinstall.py')
        shutil.copy(INTEGRATIONS.with_name('dockeraccess.py'), bundle / 'BunnyBytes/hostctl/dockeraccess.py')
        shutil.copy(INTEGRATIONS.with_name('windowshost.py'), bundle / 'BunnyBytes/hostctl/windowshost.py')
        shutil.copy(INTEGRATIONS.with_name('pkgmanager.py'), bundle / 'BunnyBytes/hostctl/pkgmanager.py')
        (bundle / 'BunnyBytes/cli').mkdir()
        (bundle / 'BunnyBytes/cli/rocsy').write_text('#!/bin/sh\n')
        # The rocsy command and menu entry land in a throwaway home, never the real one.
        self.home = self.root / 'home'
        self.home.mkdir()
        env = patch.dict('os.environ', {'HOME': str(self.home), 'XDG_DATA_HOME': str(self.home / '.local/share'),
                                        'PATH': str(self.home / '.local/bin') + ':/usr/bin'})
        env.start()
        self.addCleanup(env.stop)
        old_argv = sys.argv
        try:
            sys.argv = [str(SOURCE), str(bundle)]
            spec = importlib.util.spec_from_file_location('installer_under_test', SOURCE)
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        finally:
            sys.argv = old_argv
        self.bundle = bundle
        # Never install host packages or touch real systemd units from a test; tests opt in.
        self.real_ensure_host_tools = self.module.ensure_host_tools
        for name, value in (('ensure_host_tools', lambda *_a, **_k: None), ('prepare_bind_folders', lambda *_a, **_k: None)):
            guard = patch.object(self.module, name, side_effect=value)
            guard.start()
            self.addCleanup(guard.stop)

    def make_bundle(self, env_example='MASTER_PASSWORD=\nCOOKIE_SECRET=\n'):
        for name in ('BunnyBytes', 'Video', 'Music', 'Krea', 'Llama', 'Trainer'):
            (self.bundle / name).mkdir(exist_ok=True)
        (self.bundle / 'BunnyBytes' / '.env.example').write_text(env_example)

    def install(self, **payload):
        return self.module.install({'destination': str(self.root / 'installed'), 'comfy_root': '', 'mode': 'install',
                                    'password_mode': 'custom', 'new_password': 'ChosenPassword123', **payload})

    def test_ubuntu_installs_missing_packages(self):
        missing = dict(python=True, docker=False, compose=False, buildx=False, daemon=False)
        ready = dict(python=True, docker=True, compose=True, buildx=True, daemon=True)
        m = self.module
        with patch.object(m, 'prerequisites', side_effect=[missing, ready, ready]), \
             patch.object(m, 'distro', return_value=UBUNTU), \
             patch.object(m.pkgmanager, 'available', return_value=True), \
             patch.object(m.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
             patch.object(m, 'privileged', side_effect=lambda command: command), \
             patch.object(m, 'package_installed', return_value=False), \
             patch.object(m, 'run_command') as run:
            self.assertFalse(m.ensure_docker(lambda *_: None))
        self.assertEqual(run.call_args_list[0].args[0], ['/usr/bin/apt-get', 'update'])
        self.assertEqual(run.call_args_list[1].args[0], ['env', 'DEBIAN_FRONTEND=noninteractive', '/usr/bin/apt-get', 'install', '-y',
                                                         'docker.io', 'docker-compose-v2', 'docker-buildx'])

    def test_arch_404_offers_system_update(self):
        m = self.module
        missing = dict(python=True, docker=False, compose=False, buildx=False, daemon=False)
        with patch.object(m, 'prerequisites', return_value=missing), \
             patch.object(m, 'distro', return_value=CACHYOS), \
             patch.object(m.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
             patch.object(m, 'privileged', side_effect=lambda command: command), \
             patch.object(m, 'run_command', side_effect=m.CommandFailed('download failed', 'requested URL returned error: 404')) as run:
            with self.assertRaisesRegex(RuntimeError, 'Refresh mirrors and update the system'):
                m.ensure_docker(lambda *_: None)
        self.assertEqual(run.call_args.args[0][1], '-S')

    def test_cachyos_retries_after_refreshing_mirrors(self):
        m = self.module
        missing = dict(python=True, docker=False, compose=False, buildx=False, daemon=False)
        ready = dict(python=True, docker=True, compose=True, buildx=True, daemon=True)
        with patch.object(m, 'prerequisites', side_effect=[missing, ready]), \
             patch.object(m, 'distro', return_value=CACHYOS), \
             patch.object(m.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
             patch.object(m, 'privileged', side_effect=lambda command: command), \
             patch.object(m, 'run_command', side_effect=[m.CommandFailed('download failed', 'requested URL returned error: 404'), None, None]) as run:
            self.assertFalse(m.ensure_docker(lambda *_: None, allow_system_upgrade=True))
        self.assertEqual(run.call_args_list[0].args[0][1], '-Syu')
        self.assertEqual(run.call_args_list[1].args[0], ['/usr/bin/cachyos-rate-mirrors'])
        self.assertEqual(run.call_args_list[2].args[0][1], '-Syu')

    def test_arch_retries_dropped_package_downloads(self):
        m = self.module
        missing = dict(python=True, docker=False, compose=False, buildx=False, daemon=False)
        ready = dict(python=True, docker=True, compose=True, buildx=True, daemon=True)
        dropped = m.CommandFailed('download failed', "error: failed retrieving file 'docker.pkg.tar.zst' : Recv failure: Connection reset by peer")
        with patch.object(m, 'prerequisites', side_effect=[missing, ready]), \
             patch.object(m, 'distro', return_value=ARCH), \
             patch.object(m.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
             patch.object(m, 'privileged', side_effect=lambda command: command), \
             patch.object(m, 'run_command', side_effect=[dropped, dropped, None]) as run:
            self.assertFalse(m.ensure_docker(lambda *_: None))
        self.assertEqual(run.call_count, 3)

    def test_ubuntu_gives_up_after_three_dropped_downloads(self):
        m = self.module
        missing = dict(python=True, docker=False, compose=False, buildx=False, daemon=False)
        dropped = m.CommandFailed('update failed', 'E: Failed to fetch http://deb.debian.org/x.deb  Connection reset by peer')
        with patch.object(m, 'prerequisites', return_value=missing), \
             patch.object(m, 'distro', return_value=UBUNTU), \
             patch.object(m.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
             patch.object(m, 'privileged', side_effect=lambda command: command), \
             patch.object(m, 'package_installed', return_value=False), \
             patch.object(m, 'run_command', side_effect=dropped) as run:
            with self.assertRaises(m.CommandFailed):
                m.ensure_docker(lambda *_: None)
        self.assertEqual(run.call_count, 3)

    def test_failed_build_keeps_the_error_line(self):
        m = self.module
        script = 'echo "target llama: failed to solve: process did not complete"; for i in $(seq 100); do echo apt line $i; done; exit 1'
        with self.assertRaises(m.CommandFailed) as caught:
            m.run_command(['sh', '-c', script], lambda *_: None, 90, 'Building Rocsy images…')
        self.assertTrue(caught.exception.output.startswith('target llama: failed to solve'))
        self.assertIn('apt line 100', caught.exception.output)

    def test_docker_group_is_used_without_logout(self):
        m = self.module
        state = dict(python=True, docker=True, compose=True, buildx=True, daemon=False)
        def check(command):
            return command[:3] == ['/usr/bin/systemctl', 'is-active', '--quiet'] or command[:3] == ['sg', 'docker', '-c']
        with patch.object(m, 'prerequisites', return_value=state), \
             patch.object(m.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
             patch.object(m, 'privileged', side_effect=lambda command: command), \
             patch.object(m, 'command_ok', side_effect=check), \
             patch.object(m.grp, 'getgrnam', return_value=type('Group', (), {'gr_mem': [] , 'gr_gid': 957})()), \
             patch.object(m, 'run_command') as run:
            self.assertTrue(m.ensure_docker(lambda *_: None))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:3], ['-aG', 'docker'])

    def test_newgrp_fallback_without_sg(self):
        m = self.module
        with patch.object(m.shutil, 'which', side_effect=lambda name: None if name == 'sg' else '/usr/bin/' + name):
            self.assertEqual(m.docker_command(['docker', 'compose', 'up', '-d'], True),
                             ['newgrp', 'docker', '-c', 'docker compose up -d'])

    def test_existing_group_membership_works_with_newgrp(self):
        m = self.module
        state = dict(python=True, docker=True, compose=True, buildx=True, daemon=False)
        username = m.pwd.getpwuid(m.os.getuid()).pw_name
        group = type('Group', (), {'gr_mem': [username], 'gr_gid': 957})()
        with patch.object(m, 'prerequisites', return_value=state), \
             patch.object(m.shutil, 'which', side_effect=lambda name: None if name == 'sg' else '/usr/bin/' + name), \
             patch.object(m, 'command_ok', side_effect=lambda command: command[:3] in
                          (['/usr/bin/systemctl', 'is-active', '--quiet'], ['newgrp', 'docker', '-c'])), \
             patch.object(m.grp, 'getgrnam', return_value=group), \
             patch.object(m, 'run_command') as run:
            self.assertTrue(m.ensure_docker(lambda *_: None))
        run.assert_not_called()

    def test_docker_socket_fallback_when_service_start_fails(self):
        m = self.module
        state = dict(python=True, docker=True, compose=True, buildx=True, daemon=False)
        username = m.pwd.getpwuid(m.os.getuid()).pw_name
        group = type('Group', (), {'gr_mem': [username], 'gr_gid': 957})()
        def command_ok(command):
            return command[:3] == ['sg', 'docker', '-c']
        with patch.object(m, 'prerequisites', return_value=state), \
             patch.object(m.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
             patch.object(m, 'command_ok', side_effect=command_ok), \
             patch.object(m, 'privileged', side_effect=lambda command: command), \
             patch.object(m.grp, 'getgrnam', return_value=group), \
             patch.object(m, 'run_command', side_effect=[m.CommandFailed('service failed', 'exit 1'), None]) as run:
            self.assertTrue(m.ensure_docker(lambda *_: None))
        self.assertEqual(run.call_args_list[0].args[0][-1], 'docker.service')
        self.assertEqual(run.call_args_list[1].args[0][-1], 'docker.socket')

    def test_install_builds_and_starts_compose(self):
        m = self.module
        for name in ('BunnyBytes', 'Video', 'Music', 'Krea', 'Llama', 'Trainer'):
            (self.bundle / name).mkdir(exist_ok=True)
            (self.bundle / name / 'example.txt').write_text(name)
        (self.bundle / 'BunnyBytes' / '.env.example').write_text('MASTER_PASSWORD=\nCOOKIE_SECRET=\n')
        destination = self.root / 'installed'
        with self.assertRaisesRegex(ValueError, 'Choose how Rocsy should protect access'):
            m.install({'destination': str(destination), 'mode': 'install', 'password_mode': 'generated'})
        with patch.object(m, 'ensure_docker', return_value=False), \
             patch.object(m, 'setup_host_helper'), \
             patch.object(m, 'install_backends', return_value=[]) as backends, \
             patch.object(m, 'run_command') as run:
            result = m.install({'destination': str(destination), 'comfy_root': '',
                                'mode': 'install', 'password_mode': 'custom',
                                'new_password': 'ChosenPassword123'})
        backends.assert_called_once()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0], ['docker', 'compose', 'build'])
        self.assertTrue((destination / 'install.log').is_file())
        self.assertIn(['docker', 'compose', 'run', '--rm', '--no-deps', '--user', '0',
                       '--entrypoint', 'chown', 'lock', '-R', 'bunnylock:bunnylock', '/app/data'], commands)
        self.assertEqual(commands[-1], ['docker', 'compose', 'up', '-d'])
        self.assertEqual(run.call_args.kwargs['cwd'], destination / 'BunnyBytes')
        self.assertEqual(result['url'], 'http://127.0.0.1:8080/')
        self.assertEqual(json.loads((destination / '.rocsy-install.json').read_text())['owned_destination'], True)

    def test_failed_image_build_is_retried(self):
        m = self.module
        for name in ('BunnyBytes', 'Video', 'Music', 'Krea', 'Llama', 'Trainer'):
            (self.bundle / name).mkdir(exist_ok=True)
        (self.bundle / 'BunnyBytes' / '.env.example').write_text('MASTER_PASSWORD=\n')
        flaky = m.CommandFailed('build failed', 'E: Failed to fetch x11-common.deb  400  Bad Request')
        outcomes = iter([flaky, None])
        def run(command, *_args, **_kwargs):
            if command == ['docker', 'compose', 'build']:
                outcome = next(outcomes)
                if outcome:
                    raise outcome
        with patch.object(m, 'ensure_docker', return_value=False), \
             patch.object(m, 'setup_host_helper'), \
             patch.object(m, 'install_backends', return_value=[]), \
             patch.object(m, 'run_command', side_effect=run) as runner:
            m.install({'destination': str(self.root / 'installed'), 'comfy_root': '', 'mode': 'install',
                       'password_mode': 'custom', 'new_password': 'ChosenPassword123'})
        builds = [call for call in runner.call_args_list if call.args[0] == ['docker', 'compose', 'build']]
        self.assertEqual(len(builds), 2)

    def test_rea_and_trainer_are_off_by_default(self):
        m = self.module
        self.make_bundle()
        with patch.object(m, 'ensure_docker', return_value=False), patch.object(m, 'setup_host_helper'), \
             patch.object(m, 'install_backends', return_value=[]) as backends, patch.object(m, 'run_command'):
            result = self.install()
        env = (self.root / 'installed/BunnyBytes/.env').read_text()
        self.assertIn('COMPOSE_PROFILES=motion\n', env)
        self.assertEqual(result['components'], m.COMPONENTS)
        self.assertEqual(backends.call_args.args[3], m.COMPONENTS)

    def test_chosen_services_become_compose_profiles(self):
        m = self.module
        self.make_bundle()
        with patch.object(m, 'ensure_docker', return_value=False), patch.object(m, 'setup_host_helper'), \
             patch.object(m, 'install_backends', return_value=[]), patch.object(m, 'run_command') as run:
            self.install(components={'rea': True, 'trainer': True, 'motion': False, 'hermes': False})
        self.assertIn('COMPOSE_PROFILES=rea,trainer\n', (self.root / 'installed/BunnyBytes/.env').read_text())
        self.assertEqual(run.call_args_list[0].args[0], ['docker', 'compose', 'build'])
        with self.assertRaisesRegex(ValueError, 'Choose which components'):
            self.install(destination=str(self.root / 'other'), components={'rea': 'yes'})
        with self.assertRaisesRegex(ValueError, 'Choose which components'):
            self.install(destination=str(self.root / 'other'), components={'everything': True})

    def test_update_removes_services_turned_off(self):
        m = self.module
        self.make_bundle()
        with patch.object(m, 'ensure_docker', return_value=False), patch.object(m, 'setup_host_helper'), \
             patch.object(m, 'install_backends', return_value=[]), patch.object(m, 'run_command'):
            self.install(components={'rea': True})
        dest = self.root / 'installed'
        self.assertTrue(m.current_components(dest)['rea'])
        with patch.object(m, 'ensure_docker', return_value=False), patch.object(m, 'setup_host_helper'), \
             patch.object(m, 'install_backends', return_value=[]), patch.object(m, 'run_command') as run:
            m.install({'destination': str(dest), 'mode': 'update', 'components': {'rea': False}})
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(['docker', 'compose', '--profile', 'motion', '--profile', 'rea', '--profile', 'trainer',
                       '--profile', 'hermes', 'rm', '--stop', '--force', 'rea'], commands)
        self.assertIn('COMPOSE_PROFILES=motion\n', (dest / 'BunnyBytes/.env').read_text())

    def test_installs_from_before_components_keep_their_services(self):
        m = self.module
        dest = self.root / 'old'
        (dest / 'BunnyBytes').mkdir(parents=True)
        (dest / 'BunnyBytes/.env').write_text('MASTER_PASSWORD=x\n')
        self.assertEqual({k: v for k, v in m.current_components(dest).items() if k in m.SERVICE_PROFILES},
                         {'motion': True, 'rea': True, 'trainer': True})

    def test_backend_failure_is_reported_not_fatal(self):
        m = self.module
        dest = self.root / 'installed'
        (dest / 'BunnyBytes').mkdir(parents=True)
        (dest / 'BunnyBytes/.env').write_text('COMPOSE_PROFILES=motion\n')
        jobs = []
        def job(_dest, name, *_args):
            jobs.append(name)
            if name == 'ytdlp-install':
                raise m.CommandFailed('Downloading yt-dlp… failed (exit 1).', 'HTTP 503')
        with patch.object(m, 'run_backend_job', side_effect=job), patch.object(m, 'setup_comfyui') as comfy, \
             patch.object(m, 'setup_h3', return_value=[]) as h3, patch.object(m, 'setup_supervisor') as supervisor:
            results = m.install_backends(dest, dest / 'external/ComfyUI', False, dict(m.COMPONENTS), False, lambda *_: None)
        self.assertEqual(jobs, ['ytdlp-install', 'hermes-download'])
        comfy.assert_called_once()
        h3.assert_called_once()
        supervisor.assert_called_once()
        self.assertEqual([(r['name'], r['ok']) for r in results], [('yt-dlp', False), ('ComfyUI', True), ('Hermes Agent', True)])
        self.assertIn('COMPOSE_PROFILES=motion,hermes\n', (dest / 'BunnyBytes/.env').read_text())

    def test_existing_comfyui_only_gets_the_supervisor(self):
        m = self.module
        with patch.object(m, 'setup_comfyui') as comfy, patch.object(m, 'setup_supervisor') as supervisor, \
             patch.object(m, 'setup_h3', return_value=['ComfyUI-KJNodes: offline']) as h3:
            results = m.install_backends(self.root, self.root / 'mine', True,
                                         {**m.COMPONENTS, 'hermes': False, 'ytdlp': False}, False, lambda *_: None)
        comfy.assert_not_called()
        h3.assert_called_once()  # the H3 packs/patches also go onto a ComfyUI the user brought
        supervisor.assert_called_once()
        self.assertTrue(results[0]['ok'])
        self.assertIn('1 H3 optimization step', results[0]['message'])

    def test_comfyui_download_keeps_existing_model_files(self):
        m = self.module
        comfy = self.root / 'external/ComfyUI'
        (comfy / 'models/vae').mkdir(parents=True)
        (comfy / 'models/vae/mine.safetensors').write_text('mine')
        commands = []
        def run(command, *_args, **_kwargs):
            commands.append(command)
            if command[:2] == ['git', 'clone']:
                stage = Path(command[-1])
                (stage / 'models/vae').mkdir(parents=True)
                (stage / 'models/vae/put_vae_here').write_text('')
                (stage / 'main.py').write_text('')
                (stage / 'requirements.txt').write_text('')
                (stage / 'README.md').write_text('pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/rocm7.1\n'
                                                 'pip install --pre torch --index-url https://download.pytorch.org/whl/nightly/rocm7.2\n')
            if command[1:3] == ['-m', 'venv']:
                (Path(command[3]) / 'bin').mkdir(parents=True)
                (Path(command[3]) / 'bin/python').write_text('')
        with patch.object(m, 'run_command', side_effect=run), patch.object(m, 'comfy_python', return_value='/usr/bin/python3'):
            m.setup_comfyui(comfy, lambda *_: None)
        self.assertEqual((comfy / 'models/vae/mine.safetensors').read_text(), 'mine')
        self.assertTrue((comfy / 'main.py').is_file())
        self.assertFalse((self.root / 'external/.ComfyUI-download').exists())
        torch = next(c for c in commands if 'torch' in c)
        self.assertEqual(torch[-2:], ['--index-url', 'https://download.pytorch.org/whl/rocm7.1'])

    def test_supervisor_is_never_taken_from_another_install(self):
        m = self.module
        m.USER_UNITS.mkdir(parents=True, exist_ok=True)
        (m.USER_UNITS / m.SUPERVISOR_UNIT).write_text('ExecStart=/somewhere/else/supervisors/comfyui_supervisor.py\n')
        with patch.object(m.subprocess, 'run') as systemctl:
            with self.assertRaisesRegex(RuntimeError, 'another ComfyUI'):
                m.setup_supervisor(self.root / 'installed', self.root / 'comfy')
        # Only read-only status checks; nothing enabled, restarted or reloaded.
        self.assertTrue(all('is-active' in call.args[0] for call in systemctl.call_args_list), systemctl.call_args_list)
        self.assertIn('/somewhere/else', (m.USER_UNITS / m.SUPERVISOR_UNIT).read_text())

    def test_host_helper_gets_pyyaml_for_the_units_python(self):
        m = self.module
        checked = []
        def ok(command):
            checked.append(command[0])
            return not (command[0] == '/usr/bin/python3' and command[-1] == 'import yaml')  # system python lacks it
        with patch.object(m, 'command_ok', side_effect=ok), patch.object(m, 'distro', return_value=ARCH), \
             patch.object(m.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
             patch.object(m, 'privileged', side_effect=lambda command: command), \
             patch.object(m, 'run_command') as run:
            self.real_ensure_host_tools(lambda *_: None, {**m.COMPONENTS, 'comfyui': False, 'hermes': False, 'ytdlp': False}, True, True)
        self.assertIn('/usr/bin/python3', checked)
        self.assertEqual(run.call_args.args[0], ['/usr/bin/pacman', '-S', '--needed', '--noconfirm', 'python-yaml'])

    def test_host_helper_gets_tools_for_installing_later(self):
        m = self.module
        with patch.object(m, 'command_ok', return_value=False), patch.object(m, 'distro', return_value=UBUNTU), \
             patch.object(m.shutil, 'which', side_effect=lambda name: None if name == 'git' else '/usr/bin/' + name), \
             patch.object(m, 'privileged', side_effect=lambda command: command), \
             patch.object(m, 'run_command') as run:
            # Nothing chosen now, but the host helper is on: Core Backend jobs need git, PyYAML and venv later.
            self.real_ensure_host_tools(lambda *_: None, {key: False for key in m.COMPONENTS}, True, True)
        self.assertEqual(run.call_args.args[0], ['env', 'DEBIAN_FRONTEND=noninteractive', '/usr/bin/apt-get', 'install', '-y',
                                                 'git', 'python3-yaml', 'python3-venv'])

    def test_crash_looping_unit_fails_loudly(self):
        m = self.module
        def systemctl(command, **_kwargs):
            if command[:3] == ['systemctl', '--user', 'is-active']:
                return m.subprocess.CompletedProcess(command, 3)
            return m.subprocess.CompletedProcess(command, 0, stdout="ModuleNotFoundError: No module named 'yaml'\n")
        with patch.object(m.subprocess, 'run', side_effect=systemctl), patch.object(m.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, "did not stay running: ModuleNotFoundError"):
                m.require_running('bunny-hostctl.service')

    def test_terminal_options(self):
        m = self.module
        with patch.dict(os.environ, {'DISPLAY': ':0'}):
            self.assertTrue(m.parse_terminal_args([])['ui'])  # a desktop session: the browser UI
            self.assertEqual(m.parse_terminal_args(['--dir', '/srv/rocsy']), {**m.parse_terminal_args([]), 'dir': '/srv/rocsy'})
            self.assertFalse(m.parse_terminal_args(['--terminal'])['ui'])
            self.assertEqual(m.parse_terminal_args(['-y', '--dir', '/srv/rocsy'])['dir'], '/srv/rocsy')
            self.assertFalse(m.parse_terminal_args(['--dir=/x', '--verbose'])['ui'])
            with self.assertRaises(SystemExit):
                m.parse_terminal_args(['--bogus'])
        with patch.dict(os.environ, {'DISPLAY': '', 'WAYLAND_DISPLAY': ''}):
            self.assertFalse(m.parse_terminal_args([])['ui'])  # SSH / no desktop: the terminal by itself

    def terminal_run(self, options, answers=(), secrets=()):
        m = self.module
        answers, secrets = iter(answers), iter(secrets)
        with patch.object(m, 'install', return_value={'mode': 'install', 'url': 'http://127.0.0.1:8080/', 'command': 'docker compose up -d',
                                                       'backends': [], 'terminal_command': None, 'desktop_app': None}) as install, \
             patch.object(m, 'prerequisites', return_value=dict(python=True, docker=True, compose=True, buildx=True, daemon=True)), \
             patch.object(m, 'distro', return_value=ARCH), \
             patch.object(m.sys.stdin, 'isatty', return_value=True), patch('builtins.print'):
            code = m.terminal_main({'terminal': True, 'defaults': False, 'verbose': False, 'help': False, 'dir': None, **options},
                                   read=lambda _prompt: next(answers), secret=lambda _prompt: next(secrets))
        return code, install

    def test_defaults_install_asks_nothing(self):
        m = self.module
        code, install = self.terminal_run({'defaults': True, 'dir': str(self.root / 'fresh')})
        self.assertEqual(code, 0)
        payload = install.call_args.args[0]
        self.assertEqual((payload['mode'], payload['components'], payload['host_helper'], payload['password_mode']),
                         ('install', None, True, 'custom'))
        self.assertGreaterEqual(len(payload['new_password']), 20)
        self.assertTrue(m.TERMINAL['on'])

    def test_enter_takes_the_default_install(self):
        # Enter = default install, Enter = yes to a password, Enter at the password = generate one, start now.
        code, install = self.terminal_run({'dir': str(self.root / 'fresh')}, answers=['', '', ''], secrets=[''])
        self.assertEqual(code, 0)
        payload = install.call_args.args[0]
        self.assertEqual((payload['mode'], payload['password_mode']), ('install', 'custom'))
        self.assertGreaterEqual(len(payload['new_password']), 20)

    def test_default_install_asks_for_a_typed_password(self):
        code, install = self.terminal_run({'dir': str(self.root / 'fresh')}, answers=['', 'y', ''],
                                          secrets=['short', 'Secret-pass1', 'other-pass1', 'Secret-pass1', 'Secret-pass1'])
        self.assertEqual(code, 0)
        payload = install.call_args.args[0]
        self.assertEqual((payload['password_mode'], payload['new_password']), ('custom', 'Secret-pass1'))

    def test_default_install_without_a_password(self):
        code, install = self.terminal_run({'dir': str(self.root / 'fresh')}, answers=['', 'n', ''])
        self.assertEqual(code, 0)
        payload = install.call_args.args[0]
        self.assertEqual((payload['password_mode'], payload['no_password_ack'], payload['new_password']), ('none', True, ''))

    def test_no_password_flag_skips_the_question(self):
        m = self.module
        with patch.dict(os.environ, {'DISPLAY': ':0'}):
            self.assertEqual(m.parse_terminal_args(['--no-password']), {**m.parse_terminal_args([]), 'no_password': True, 'ui': False})
        code, install = self.terminal_run({'dir': str(self.root / 'fresh'), 'no_password': True}, answers=['', ''])
        self.assertEqual(code, 0)
        self.assertEqual(install.call_args.args[0]['password_mode'], 'none')
        code, install = self.terminal_run({'defaults': True, 'dir': str(self.root / 'quiet'), 'no_password': True})
        self.assertEqual(code, 0)
        payload = install.call_args.args[0]
        self.assertEqual((payload['password_mode'], payload['no_password_ack']), ('none', True))

    def test_customize_asks_every_step(self):
        answers = ['c', str(self.root / 'custom'), '',          # customize, folder, ComfyUI folder
                   '', 'y', 'n', 'n', '', 'n',                  # motion, rea, trainer, comfyui, hermes, ytdlp
                   'y',                                         # a password (typed below)
                   'n', '', 'y', '', '',                        # desktop app, rocsy cmd, launcher, helper, system upgrade
                   '']                                          # start now
        code, install = self.terminal_run({}, answers=answers, secrets=['Secret-pass1', 'Secret-pass1'])
        self.assertEqual(code, 0)
        payload = install.call_args.args[0]
        self.assertEqual(payload['components'], {'motion': True, 'rea': True, 'trainer': False, 'comfyui': False, 'hermes': True, 'ytdlp': False})
        self.assertEqual((payload['new_password'], payload['desktop_app'], payload['desktop_launcher'], payload['allow_system_upgrade']),
                         ('Secret-pass1', False, True, False))

    def test_defaults_refuse_a_folder_with_other_files(self):
        (self.root / 'busy').mkdir()
        (self.root / 'busy/notes.txt').write_text('mine')
        code, install = self.terminal_run({'defaults': True, 'dir': str(self.root / 'busy')})
        self.assertEqual(code, 1)
        install.assert_not_called()

    def test_no_terminal_without_defaults_asks_for_them(self):
        m = self.module
        with patch.object(m, 'prerequisites', return_value=dict(python=True, docker=True, compose=True, buildx=True, daemon=True)), \
             patch.object(m, 'distro', return_value=ARCH), \
             patch.object(m.sys.stdin, 'isatty', return_value=False), patch('builtins.print') as printed, \
             patch.object(m, 'install') as install:
            code = m.terminal_main({'terminal': True, 'defaults': False, 'verbose': False, 'help': False, 'dir': None})
        self.assertEqual(code, 1)
        install.assert_not_called()
        self.assertIn('--defaults', ' '.join(str(c.args[0]) for c in printed.call_args_list))

    def test_reinstall_password_requirement(self):
        destination = self.root / 'installed'
        (destination / 'BunnyBytes').mkdir(parents=True)
        env_file = destination / 'BunnyBytes' / '.env'
        env_file.write_text('MASTER_PASSWORD=abcdefghijklmnopqrstuvwx\n')
        self.assertFalse(self.module.reinstall_password_required(destination, {}))
        self.assertFalse(self.module.reinstall_password_required(destination, {'password_mode': 'generated'}))
        self.assertTrue(self.module.reinstall_password_required(destination, {'password_mode': 'custom'}))
        env_file.write_text('MASTER_PASSWORD=ChosenPassword123\n')
        self.assertTrue(self.module.reinstall_password_required(destination, {}))
        env_file.write_text('MASTER_PASSWORD=generatedsecret\nROCSY_NO_PASSWORD=1\n')
        self.assertFalse(self.module.reinstall_password_required(destination, {'password_mode': 'none'}))
        env_file.unlink()
        self.assertFalse(self.module.reinstall_password_required(destination, {}))

    def test_update_prepares_the_existing_comfy_root(self):
        m = self.module
        for name in ('BunnyBytes', 'Video', 'Music', 'Krea', 'Llama', 'Trainer'):
            (self.bundle / name).mkdir(exist_ok=True)
            (self.bundle / name / 'example.txt').write_text(name)
        destination = self.root / 'installed'
        (destination / 'BunnyBytes').mkdir(parents=True)
        (destination / '.rocsy-install.json').write_text(json.dumps({
            'format': 1, 'app': 'Rocsy', 'owned_destination': True,
        }))
        external_comfy = self.root / 'comfy'
        (external_comfy / 'models').mkdir(parents=True)
        (destination / 'BunnyBytes' / '.env').write_text(
            f'ROCSY_COMFY_ROOT={external_comfy}\nROCSY_UID=1000\nROCSY_GID=1000\n')
        with patch.object(m, 'ensure_docker', return_value=False), \
             patch.object(m, 'setup_host_helper'), \
             patch.object(m, 'run_command'):
            result = m.install({'destination': str(destination), 'mode': 'update'})
        self.assertEqual(result['comfy'], str(external_comfy))
        self.assertTrue((external_comfy / 'input/VoiceRefs').is_dir())
        self.assertTrue((external_comfy / 'output/postedit-sep').is_dir())

    def test_update_keeps_the_users_llama_models(self):
        m = self.module
        self.make_bundle()
        (self.bundle / 'Llama/config.yaml').write_text('models: {}\n')
        (self.bundle / 'Llama/llama-launch').write_text('new launcher\n')
        dest = self.root / 'installed'
        with patch.object(m, 'ensure_docker', return_value=False), patch.object(m, 'setup_host_helper'), \
             patch.object(m, 'install_backends', return_value=[]), patch.object(m, 'run_command'):
            self.install()
        self.assertEqual((dest / 'Llama/config.yaml').read_text(), 'models: {}\n')
        # The user adds a model (Models → Manager does this); the next update ships newer code.
        (dest / 'Llama/config.yaml').write_text('models:\n  my-model: {}\n')
        (dest / 'Llama/llama-launch').write_text('old launcher\n')
        (dest / 'Llama/overrides').mkdir()
        (dest / 'Llama/overrides/my-model.args').write_text('--temp 0.7\n')
        for mode in ('update', 'repair'):
            with patch.object(m, 'ensure_docker', return_value=False), patch.object(m, 'setup_host_helper'), \
                 patch.object(m, 'install_backends', return_value=[]), patch.object(m, 'run_command'):
                m.install({'destination': str(dest), 'mode': mode})
            self.assertEqual((dest / 'Llama/config.yaml').read_text(), 'models:\n  my-model: {}\n')
            self.assertEqual((dest / 'Llama/llama-launch').read_text(), 'new launcher\n')
            self.assertEqual((dest / 'Llama/overrides/my-model.args').read_text(), '--temp 0.7\n')
        self.assertIn('Kept your Llama/config.yaml', (dest / 'install.log').read_text())

    def test_password_set_in_settings_is_required_for_reinstall(self):
        destination = self.root / 'installed'
        data = destination / 'BunnyBytes' / 'gateway' / 'data'
        data.mkdir(parents=True)
        (destination / 'BunnyBytes' / '.env').write_text('MASTER_PASSWORD=generatedsecret\nROCSY_NO_PASSWORD=1\n')
        salt = bytes.fromhex('01' * 16)
        record = {'version': 1, 'salt': salt.hex(),
                  'hash': hashlib.pbkdf2_hmac('sha256', b'LaterPassword123', salt, 600_000).hex(),
                  'revision': 'test-revision'}
        (data / 'password.json').write_text(json.dumps(record))
        self.assertTrue(self.module.reinstall_password_required(destination, {'password_mode': 'none'}))
        self.assertTrue(self.module.verify_existing_password(destination, 'LaterPassword123'))
        self.assertFalse(self.module.verify_existing_password(destination, 'generatedsecret'))


    def _bundle_apps(self):
        for name in ('BunnyBytes', 'Video', 'Music', 'Krea', 'Llama', 'Trainer'):
            (self.bundle / name).mkdir(exist_ok=True)
            (self.bundle / name / 'example.txt').write_text(name)
        (self.bundle / 'BunnyBytes' / '.env.example').write_text('MASTER_PASSWORD=\nCOOKIE_SECRET=\n')

    def test_terminal_and_desktop_toggles(self):
        m = self.module
        self._bundle_apps()
        destination = self.root / 'installed'
        payload = {'destination': str(destination), 'comfy_root': '', 'mode': 'install', 'password_mode': 'custom',
                   'new_password': 'ChosenPassword123', 'terminal_command': True, 'desktop_app': True}
        with patch.object(m, 'ensure_docker', return_value=False), \
             patch.object(m, 'setup_host_helper'), patch.object(m, 'run_command'):
            result = m.install(payload)
        link = self.home / '.local/bin/rocsy'
        entry = self.home / '.local/share/applications/rocsy.desktop'
        self.assertEqual(Path(os.path.realpath(link)), destination / 'BunnyBytes/cli/rocsy')
        self.assertTrue(os.access(link, os.X_OK))
        self.assertTrue(result['terminal_command']['on_path'])
        self.assertIn(f'X-Rocsy-Install={destination}\n', entry.read_text())
        self.assertIn('desktop\n', entry.read_text())
        marker = json.loads((destination / '.rocsy-install.json').read_text())
        self.assertEqual(marker['terminal_command'], str(link))
        # Update with both toggles off takes them away again.
        with patch.object(m, 'ensure_docker', return_value=False), \
             patch.object(m, 'setup_host_helper'), patch.object(m, 'run_command'):
            m.install({**payload, 'mode': 'update', 'terminal_command': False, 'desktop_app': False})
        self.assertFalse(link.is_symlink() or entry.exists())
        self.assertNotIn('terminal_command', json.loads((destination / '.rocsy-install.json').read_text()))

    def test_foreign_rocsy_command_is_never_replaced(self):
        m = self.module
        self._bundle_apps()
        link = self.home / '.local/bin/rocsy'
        link.parent.mkdir(parents=True)
        link.write_text('#!/bin/sh\necho not rocsy\n')
        with patch.object(m, 'ensure_docker', return_value=False) as docker, \
             self.assertRaisesRegex(ValueError, "isn't Rocsy's"):
            m.install({'destination': str(self.root / 'installed'), 'comfy_root': '', 'mode': 'install',
                       'password_mode': 'custom', 'new_password': 'ChosenPassword123', 'terminal_command': True})
        docker.assert_not_called()
        self.assertEqual(link.read_text(), '#!/bin/sh\necho not rocsy\n')

    def test_uninstall_removes_terminal_association(self):
        m = self.module
        self._bundle_apps()
        # The worker refuses installs under /tmp, so this one lives in the real ~/.cache.
        outside = Path(tempfile.mkdtemp(prefix='rocsy-installer-test-', dir=REAL_CACHE))
        self.addCleanup(shutil.rmtree, outside)
        destination = outside / 'installed'
        with patch.object(m, 'ensure_docker', return_value=False), patch.object(m, 'install_backends', return_value=[]), \
             patch.object(m, 'setup_host_helper'), patch.object(m, 'run_command'):
            m.install({'destination': str(destination), 'comfy_root': '', 'mode': 'install', 'password_mode': 'custom',
                       'new_password': 'ChosenPassword123', 'terminal_command': True, 'desktop_app': True})
        (destination / 'BunnyBytes/docker-compose.yml').write_text('services: {}\n')
        units = self.home / '.config/systemd/user'
        units.mkdir(parents=True)
        (units / 'bunny-hostctl.service').write_text(f'ExecStart=/usr/bin/python3 {destination}/BunnyBytes/hostctl/hostctl.py\n')
        spec = importlib.util.spec_from_file_location('worker_under_test', WORKER)
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        with patch.object(worker.subprocess, 'run',
                          side_effect=lambda argv, **_k: worker.subprocess.CompletedProcess(argv, 0, stdout='', stderr='')) as run:
            worker.run(destination, 'keep')
        self.assertFalse((self.home / '.local/bin/rocsy').is_symlink())
        self.assertFalse((self.home / '.local/share/applications/rocsy.desktop').exists())
        self.assertFalse((units / 'bunny-hostctl.service').exists())
        self.assertIn(['systemctl', '--user', 'disable', '--now', 'bunny-hostctl.service'],
                      [call.args[0] for call in run.call_args_list])


    # ---- other package managers ----

    def test_rhel_like_without_docker_packages_uses_dockers_repo(self):
        m = self.module
        rocky = host('dnf', 'rocky', 'Rocky Linux 9')
        missing = dict(python=True, docker=False, compose=False, buildx=False, daemon=True)
        ready = dict(python=True, docker=True, compose=True, buildx=True, daemon=True)
        with patch.object(m, 'prerequisites', side_effect=[missing, missing, ready]), \
             patch.object(m, 'distro', return_value=rocky), \
             patch.object(m.pkgmanager, 'available', return_value=False), \
             patch.object(m.pkgmanager, 'os_release', return_value={'ID': 'rocky', 'ID_LIKE': 'rhel centos fedora'}), \
             patch.object(m, 'download', return_value=b'[docker-ce-stable]\n') as download, \
             patch.object(m.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
             patch.object(m, 'privileged', side_effect=lambda command: command), \
             patch.object(m, 'run_command') as run:
            self.assertFalse(m.ensure_docker(lambda *_: None))
        self.assertEqual(download.call_args.args[0], 'https://download.docker.com/linux/centos/docker-ce.repo')
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][:5], ['install', '-D', '-m', '644', commands[0][4]])
        self.assertEqual(commands[0][-1], '/etc/yum.repos.d/docker-ce.repo')
        self.assertEqual(commands[-1], ['/usr/bin/dnf', 'install', '--allowerasing', '-y', *m.pkgmanager.DOCKER_CE_PACKAGES])

    def test_openrc_starts_docker_without_systemctl(self):
        m = self.module
        alpine = host('apk', 'alpine', 'Alpine Linux', init='openrc')
        stopped = dict(python=True, docker=True, compose=True, buildx=True, daemon=False)
        with patch.object(m, 'prerequisites', return_value=stopped), patch.object(m, 'distro', return_value=alpine), \
             patch.object(m.pkgmanager, 'init_system', return_value='openrc'), \
             patch.object(m.shutil, 'which', side_effect=lambda name: '/sbin/' + name), \
             patch.object(m, 'privileged', side_effect=lambda command: command), \
             patch.object(m, 'command_ok', return_value=True), patch.object(m.os.path, 'exists', return_value=True), \
             patch.object(m, 'run_command') as run:
            self.assertFalse(m.ensure_docker(lambda *_: None))
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [['/sbin/rc-update', 'add', 'docker', 'default'], ['/sbin/rc-service', 'docker', 'start']])

    def test_read_only_systems_get_manual_steps(self):
        m = self.module
        silverblue = {**host('', 'fedora', 'Fedora Silverblue'), 'immutable': 'rpm-ostree'}
        with patch.object(m, 'prerequisites', return_value=dict(python=True, docker=False, compose=False, buildx=False, daemon=False)), \
             patch.object(m, 'distro', return_value=silverblue):
            with self.assertRaisesRegex(RuntimeError, 'read-only system'):
                m.ensure_docker(lambda *_: None)

    def test_without_systemd_the_host_helper_is_skipped(self):
        m = self.module
        self._bundle_apps()
        with patch.object(m, 'systemd_user_ok', return_value=False), patch.object(m, 'ensure_docker', return_value=False), \
             patch.object(m, 'install_backends', return_value=[]), patch.object(m, 'setup_host_helper') as helper, \
             patch.object(m, 'run_command'):
            result = self.install(host_helper=True)
        helper.assert_not_called()
        self.assertFalse(result['host_helper'])

    # ---- Windows (WSL2, untested) ----

    def test_windows_launcher_opens_the_browser_installer(self):
        m = self.module
        with patch.dict(os.environ, {'DISPLAY': '', 'WAYLAND_DISPLAY': ''}):
            self.assertTrue(m.parse_terminal_args(['--windows'])['ui'])
            self.assertFalse(m.parse_terminal_args(['--windows', '--terminal'])['ui'])

    def wsl(self, version='10.0.22631'):
        """Pretend this runs inside WSL2 on the given Windows version."""
        w = self.module.windowshost
        return patch.multiple(w, is_wsl=lambda: True, windows_version=lambda: w.classify(version))

    def test_windows_7_and_old_windows_10_are_refused(self):
        self._bundle_apps()
        for version, text in (('6.1.7601', 'Windows 7'), ('6.3.9600', 'Windows 8.1'), ('10.0.18363', 'build 18363')):
            with self.wsl(version), self.assertRaisesRegex(ValueError, text):
                self.install()
        self.assertFalse((self.root / 'installed').exists())

    def test_windows_install_adapts_compose_and_makes_windows_shortcuts(self):
        m = self.module
        self._bundle_apps()
        destination = self.root / 'installed'
        config = {'services': {'llama': {'devices': [{'source': '/dev/kfd', 'target': '/dev/kfd'}]}, 'gateway': {}}}

        def fake_run(command, **_kwargs):
            output = '2.27.0\n' if 'version' in command else json.dumps(config)
            return m.subprocess.CompletedProcess(command, 0, output, '')
        record = {'id': f'Ubuntu:{destination}', 'folder': '/mnt/c/x', 'links': ['/mnt/c/Desktop/Rocsy.lnk']}
        with self.wsl(), patch.dict(os.environ, {'WSL_DISTRO_NAME': 'Ubuntu'}), \
             patch.object(m.windowshost, 'gpu_status', return_value={'dxg': True, 'rocm_wsl': False}), \
             patch.object(m.windowshost, 'install_shortcuts', return_value=record) as shortcuts, \
             patch.object(m.subprocess, 'run', side_effect=fake_run), \
             patch.object(m, 'ensure_docker', return_value=False), patch.object(m, 'install_backends', return_value=[]), \
             patch.object(m, 'setup_host_helper'), patch.object(m, 'run_command'):
            result = self.install(desktop_app=True, desktop_launcher=True, windows_autostart=True, terminal_command=False)
        override = (destination / 'BunnyBytes/docker-compose.wsl.yml').read_text()
        self.assertIn('"llama":\n    devices: !override\n      - /dev/dxg:/dev/dxg', override)
        self.assertNotIn('gateway', override)
        self.assertIn('COMPOSE_FILE=docker-compose.yml:docker-compose.wsl.yml\n', (destination / 'BunnyBytes/.env').read_text())
        self.assertEqual(shortcuts.call_args.args[:2], (destination, 'Ubuntu'))
        self.assertEqual(shortcuts.call_args.args[4:], (True, True))
        # No Linux menu entry or desktop launcher: Windows gets shortcuts instead.
        self.assertFalse((self.home / '.local/share/applications/rocsy.desktop').exists())
        self.assertIsNone(result['desktop_launcher'])
        self.assertTrue(result['windows']['untested'])
        self.assertEqual(result['windows_shortcuts'], record['links'])
        marker = json.loads((destination / '.rocsy-install.json').read_text())
        self.assertEqual((marker['windows_shortcuts'], marker['windows_autostart']), (record, True))

    def test_windows_needs_compose_with_override_tags(self):
        m = self.module
        env = self.root / '.env'
        env.write_text('X=1\n')
        with patch.object(m.subprocess, 'run', return_value=m.subprocess.CompletedProcess([], 0, '2.20.2\n', '')):
            with self.assertRaisesRegex(RuntimeError, '2.24 or newer'):
                m.setup_wsl_compose(self.root, env, False, lambda *_: None)


if __name__ == '__main__':
    unittest.main()
