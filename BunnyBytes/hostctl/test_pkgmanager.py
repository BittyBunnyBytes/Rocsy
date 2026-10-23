"""Package manager / init detection and package names for every supported Linux family."""
import unittest
from unittest.mock import patch

import pkgmanager as pm


def release(distro, like='', **extra):
    return {'ID': distro, 'ID_LIKE': like, 'PRETTY_NAME': distro.title(), **extra}


def only(*binaries):
    return lambda name: f'/usr/bin/{name}' if name in binaries else None


class DetectTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(pm, 'immutable', return_value='')
        guard.start()
        self.addCleanup(guard.stop)

    def test_families_by_os_release(self):
        cases = [(release('linuxmint', 'ubuntu debian'), 'apt-get', 'apt'), (release('pop', 'ubuntu debian'), 'apt-get', 'apt'),
                 (release('debian'), 'apt-get', 'apt'), (release('fedora'), 'dnf', 'dnf'), (release('rocky', 'rhel centos fedora'), 'dnf', 'dnf'),
                 (release('opensuse-tumbleweed', 'opensuse suse'), 'zypper', 'zypper'), (release('manjaro', 'arch'), 'pacman', 'pacman'),
                 (release('cachyos', 'arch'), 'pacman', 'pacman'), (release('alpine'), 'apk', 'apk'), (release('void'), 'xbps-install', 'xbps'),
                 (release('solus'), 'eopkg', 'eopkg'), (release('gentoo'), 'emerge', 'emerge')]
        for values, binary, manager in cases:
            self.assertEqual(pm.detect(values, which=only(binary))['manager'], manager, values['ID'])

    def test_old_rhel_falls_back_to_yum(self):
        self.assertEqual(pm.detect(release('centos', 'rhel fedora'), which=only('yum'))['manager'], 'yum')

    def test_unknown_distro_uses_the_manager_on_path(self):
        self.assertEqual(pm.detect(release('somethingnew'), which=only('zypper'))['manager'], 'zypper')
        self.assertEqual(pm.detect(release('somethingnew'), which=only())['manager'], '')

    def test_os_release_wins_over_a_stray_binary(self):
        # dpkg/apt-get can be installed on Arch (e.g. for building .debs); pacman is still the manager.
        self.assertEqual(pm.detect(release('arch'), which=only('pacman', 'apt-get'))['manager'], 'pacman')

    def test_immutable_systems_get_no_manager(self):
        with patch.object(pm, 'immutable', return_value='rpm-ostree'):
            info = pm.detect(release('fedora'), which=only('dnf', 'rpm-ostree'))
        self.assertEqual((info['manager'], info['immutable']), ('', 'rpm-ostree'))
        self.assertIn('rpm-ostree install', pm.manual_steps(info))


class PackageTests(unittest.TestCase):
    def test_every_manager_has_every_package(self):
        for manager in pm.MANAGERS:
            for name in ('python', 'venv', 'yaml', 'git', 'build'):
                self.assertIn(manager, pm.PACKAGES[name], f'{name} for {manager}')
            self.assertEqual(len(pm.DOCKER[manager]), 3, manager)

    def test_names_differ_per_manager(self):
        self.assertEqual(pm.packages('apt', 'yaml', 'venv'), ['python3-yaml', 'python3-venv'])
        self.assertEqual(pm.packages('dnf', 'yaml', 'venv'), ['python3-pyyaml'])
        self.assertEqual(pm.packages('apk', 'yaml', 'shadow'), ['py3-yaml', 'shadow'])
        self.assertEqual(pm.packages('pacman', 'python', 'venv'), ['python'])

    def test_first_available_docker_package_per_group(self):
        have = {'docker.io', 'docker-compose-plugin', 'docker-buildx'}
        self.assertEqual(pm.docker_packages('apt', have=lambda _m, name: name in have),
                         ['docker.io', 'docker-compose-plugin', 'docker-buildx'])
        self.assertEqual(pm.docker_packages('dnf', have=lambda _m, _name: False), [])

    def test_install_commands(self):
        with patch.object(pm.shutil, 'which', side_effect=lambda name: f'/usr/sbin/{name}'):
            self.assertEqual(pm.install_command('zypper', ['git']), ['/usr/sbin/zypper', '--non-interactive', 'install', '--no-recommends', 'git'])
            self.assertEqual(pm.install_command('apt', ['git'])[:3], ['env', 'DEBIAN_FRONTEND=noninteractive', '/usr/sbin/apt-get'])
            self.assertEqual(pm.refresh_command('xbps'), ['/usr/sbin/xbps-install', '-S'])
            self.assertIsNone(pm.refresh_command('dnf'))


class DockerRepoTests(unittest.TestCase):
    def test_apt_repo_uses_the_base_release(self):
        self.assertEqual(pm.docker_apt_repo(release('linuxmint', 'ubuntu', UBUNTU_CODENAME='noble', VERSION_CODENAME='wilma')), ('ubuntu', 'noble'))
        self.assertEqual(pm.docker_apt_repo(release('linuxmint', 'debian', DEBIAN_CODENAME='bookworm', VERSION_CODENAME='faye')), ('debian', 'bookworm'))
        self.assertEqual(pm.docker_apt_repo(release('debian', VERSION_CODENAME='bookworm')), ('debian', 'bookworm'))
        self.assertIsNone(pm.docker_apt_repo(release('someapt')))

    def test_rpm_repo(self):
        self.assertTrue(pm.docker_rpm_repo(release('fedora')).endswith('/fedora/docker-ce.repo'))
        self.assertTrue(pm.docker_rpm_repo(release('rhel', 'fedora')).endswith('/rhel/docker-ce.repo'))
        self.assertTrue(pm.docker_rpm_repo(release('rocky', 'rhel centos fedora')).endswith('/centos/docker-ce.repo'))
        self.assertTrue(pm.docker_rpm_repo(release('almalinux', 'rhel centos fedora')).endswith('/centos/docker-ce.repo'))
        self.assertTrue(pm.docker_rpm_repo(release('nobara', 'fedora')).endswith('/fedora/docker-ce.repo'))
        self.assertEqual(pm.docker_rpm_repo(release('mageia')), '')


class ServiceTests(unittest.TestCase):
    def test_service_commands_per_init(self):
        self.assertEqual(pm.service_commands('systemd'), [['systemctl', 'enable', '--now', 'docker.service']])
        self.assertEqual(pm.service_commands('openrc'), [['rc-update', 'add', 'docker', 'default'], ['rc-service', 'docker', 'start']])
        self.assertEqual(pm.service_commands('dinit'), [['dinitctl', 'enable', 'docker']])
        self.assertEqual(pm.service_commands('other'), [])

    def test_manual_steps_name_the_right_commands(self):
        info = {'manager': 'apk', 'init': 'openrc', 'immutable': ''}
        steps = pm.manual_steps(info)
        self.assertIn('apk add', steps)
        self.assertIn('docker-cli-compose', steps)
        self.assertIn('rc-service docker start', steps)
        self.assertIn('shadow', steps)
        self.assertIn('nixos-rebuild', pm.manual_steps({'manager': '', 'immutable': 'nixos'}))

    def test_samples_cover_every_manager(self):
        self.assertTrue(set(pm.MANAGERS) - {'yum'} <= set(pm.samples()))


if __name__ == '__main__':
    unittest.main()
