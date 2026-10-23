"""Windows (WSL2) support - untested on real Windows; these cover the parts that run anywhere."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import windowshost as w


class WindowsHostTests(unittest.TestCase):
    def test_classify_every_supported_windows(self):
        cases = {'6.1.7601': ('Windows 7', False), '6.2.9200': ('Windows 8', False), '6.3.9600': ('Windows 8.1', False),
                 '10.0.17763': ('Windows 10', False), '10.0.19041': ('Windows 10', True), '10.0.19045.5011': ('Windows 10', True),
                 '10.0.22000': ('Windows 11', True), '10.0.26100': ('Windows 11', True)}
        for version, (name, wsl2) in cases.items():
            info = w.classify(version)
            self.assertEqual((info['name'], info['wsl2']), (name, wsl2), version)
            self.assertEqual(bool(info['reason']), not wsl2, version)

    def test_override_swaps_gpu_devices_only(self):
        config = {'services': {
            'voice': {'devices': [{'source': '/dev/kfd', 'target': '/dev/kfd'}, {'source': '/dev/dri', 'target': '/dev/dri'}]},
            'hermes-agent': {'devices': ['/dev/kfd', '/dev/dri/renderD128']},
            'gateway': {'volumes': [{'source': '/dev/dri', 'target': '/dev/dri'}]}}}
        text = w.compose_override(config, dxg=True, hsa=True)
        self.assertIn('"hermes-agent":\n    devices: !override\n      - /dev/dxg:/dev/dxg', text)
        self.assertIn(f'{w.ROCM_WSL_HSA}:{w.ROCM_WSL_HSA}:ro', text)
        self.assertNotIn('gateway', text)
        self.assertIn('"voice":\n    devices: !reset []', w.compose_override(config, dxg=False, hsa=False))
        self.assertIn('services: {}', w.compose_override({'services': {'gateway': {}}}, True, True))

    def test_compose_version_gate(self):
        self.assertTrue(w.compose_supports_override('2.24.0'))
        self.assertTrue(w.compose_supports_override('v2.27.1-desktop.1'))
        self.assertTrue(w.compose_supports_override('5.5.1'))
        self.assertFalse(w.compose_supports_override('2.20.2'))
        self.assertFalse(w.compose_supports_override(''))

    def test_compose_files_keep_a_personal_override(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(w.compose_files_value(Path(folder)), 'docker-compose.yml:docker-compose.wsl.yml')
            (Path(folder) / 'docker-compose.override.yml').write_text('services: {}\n')
            self.assertTrue(w.compose_files_value(Path(folder)).endswith(':docker-compose.override.yml'))

    def test_launcher_scripts_are_crlf_and_marked(self):
        scripts = w.launcher_scripts(Path('/home/rocsy/Rocsy'), 'Ubuntu-24.04', 8080)
        for data in scripts.values():
            self.assertNotIn(b'\n', data.replace(b'\r\n', b''))
            self.assertIn(b'X-Rocsy-Install=Ubuntu-24.04:/home/rocsy/Rocsy', data)
        self.assertIn(b'--cd "/home/rocsy/Rocsy/BunnyBytes"', scripts['background.cmd'])
        self.assertIn(b'--app=http://127.0.0.1:8080/', scripts['start.cmd'])
        for dest, distro in ((Path('/home/rocsy/Ro%csy'), 'Ubuntu'), (Path('/home/rocsy/R"x'), 'Ubuntu'), (Path('/home/rocsy/Rocsy'), 'Ub&untu')):
            with self.assertRaises(ValueError):
                w.launcher_scripts(dest, distro, 8080)

    def test_remove_shortcuts_only_removes_this_installs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home, desk = root / 'Rocsy' / 'Ubuntu-abcd', root / 'Desktop'
            home.mkdir(parents=True)
            desk.mkdir()
            dest = Path('/home/rocsy/Rocsy')
            (home / 'start.cmd').write_bytes(w.launcher_scripts(dest, 'Ubuntu', 8080)['start.cmd'])
            ours, other = desk / 'Rocsy.lnk', desk / 'Notes.lnk'
            ours.write_text('x')
            other.write_text('x')
            record = {'id': f'Ubuntu:{dest}', 'folder': str(home), 'links': [str(ours), str(other)]}
            self.assertEqual(w.remove_shortcuts(record, Path('/home/rocsy/Other')), [])
            self.assertEqual(w.remove_shortcuts({**record, 'id': 'Ubuntu:/home/rocsy/Other'}, Path('/home/rocsy/Other')), [])
            self.assertTrue(ours.exists())
            w.remove_shortcuts(record, dest)
            self.assertFalse(ours.exists() or home.exists())
            self.assertTrue(other.exists())

    def test_not_wsl_reports_linux(self):
        with patch.object(w, 'is_wsl', return_value=False):
            self.assertEqual(w.platform()['os'], 'linux')
            self.assertFalse(w.platform()['untested'])

    def test_wsl_platform_is_labelled_untested(self):
        with patch.object(w, 'is_wsl', return_value=True), \
             patch.dict('os.environ', {'ROCSY_WINDOWS': '10.0.22631', 'WSL_DISTRO_NAME': 'Ubuntu-24.04'}):
            info = w.platform()
        self.assertEqual(info['name'], 'Windows 11 · WSL2 (Ubuntu-24.04)')
        self.assertTrue(info['untested'])
        self.assertIn('untested', info['note'])

    def test_torch_hsa_swap(self):
        with tempfile.TemporaryDirectory() as folder:
            comfy = Path(folder)
            lib = comfy / '.venv/lib/python3.12/site-packages/torch/lib'
            lib.mkdir(parents=True)
            (lib / 'libhsa-runtime64.so').write_text('stock')
            runtime = comfy / 'wsl-hsa.so'
            runtime.write_text('wsl')
            with patch.object(w, 'ROCM_WSL_HSA', comfy / 'missing.so'):
                self.assertIn('not installed', w.fix_torch_hsa(comfy))
            with patch.object(w, 'ROCM_WSL_HSA', runtime):
                w.fix_torch_hsa(comfy)
            self.assertEqual((lib / 'libhsa-runtime64.so').read_text(), 'wsl')


if __name__ == '__main__':
    unittest.main()
