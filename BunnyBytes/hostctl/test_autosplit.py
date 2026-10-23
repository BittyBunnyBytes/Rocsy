import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE = Path(__file__).resolve().with_name('coreextras.py')


def git(*args):
    return subprocess.run(['git', *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()


class AutoSplitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hermes-autosplit-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        spec = importlib.util.spec_from_file_location('coreextras_autosplit_test', MODULE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.personal = self.root / 'personal'
        git('init', '-b', 'main', self.personal)
        git('-C', self.personal, 'config', 'user.name', 'Test')
        git('-C', self.personal, 'config', 'user.email', 'test@example.test')
        (self.personal / 'example.txt').write_text('base\n')
        git('-C', self.personal, 'add', 'example.txt')
        git('-C', self.personal, 'commit', '-m', 'base')
        base = git('-C', self.personal, 'rev-parse', 'HEAD')
        (self.personal / 'example.txt').write_text('base\nrocsy\n')
        patch_text = git('-C', self.personal, 'diff')
        git('-C', self.personal, 'restore', 'example.txt')
        patch_file = self.root / 'addon.patch'
        patch_file.write_text(patch_text + '\n')
        launcher = self.root / 'hermes'
        launcher.write_text('#!/bin/sh\n')
        launcher.chmod(0o755)
        self.module.PERSONAL_HERMES = self.personal
        self.module.PERSONAL_LAUNCHER = launcher
        self.module.HERMES = self.root / 'rocsy'
        self.module.HERMES_URL = str(self.personal)
        self.module.HERMES_PATCH = patch_file
        self.module.HERMES_PATCH_BASE = base
        self.module.HERMES_STATE = self.root / 'hermes.json'

    def test_creates_independent_patched_rocsy_checkout(self):
        m = self.module
        with (self.root / 'install.log').open('w') as output:
            m.create_rocsy_checkout(output)
        self.assertEqual(git('-C', m.HERMES, 'rev-parse', 'HEAD'), m.HERMES_PATCH_BASE)
        self.assertEqual((self.personal / 'example.txt').read_text(), 'base\n')
        self.assertEqual(git('-C', self.personal, 'status', '--porcelain'), '')
        with (self.root / 'install.log').open('a') as output:
            self.assertTrue(m.apply_hermes_addons(output))
        self.assertEqual((m.HERMES / 'example.txt').read_text(), 'base\nrocsy\n')
        self.assertNotEqual((m.HERMES / '.git').resolve(), (self.personal / '.git').resolve())

    def test_detects_personal_once_without_overwriting(self):
        m = self.module
        def queued(job, *, auto=False):
            self.assertEqual((job, auto), ('hermes-download', True))
            m.HERMES_STATE.write_text(json.dumps({'auto_requested': True}))
        with patch.object(m, 'start', side_effect=queued) as start:
            m.auto_provision_rocsy()
            m.auto_provision_rocsy()
            self.assertEqual(start.call_count, 1)

    def test_existing_rocsy_is_not_replaced(self):
        m = self.module
        m.HERMES.mkdir()
        with patch.object(m, 'start') as start:
            m.auto_provision_rocsy()
            start.assert_not_called()

    def test_fresh_install_creates_personal_before_rocsy(self):
        m = self.module
        m.PERSONAL_HERMES = self.root / 'fresh-personal'
        m.PERSONAL_LAUNCHER = self.root / 'fresh-launcher'
        real_step = m.run_step
        installer_calls = []

        def step(output, args, **kwargs):
            if args[0] == 'bash':
                installer_calls.append(args)
                git('clone', self.personal, m.PERSONAL_HERMES)
                m.PERSONAL_LAUNCHER.write_text('#!/bin/sh\n')
            else:
                real_step(output, args, **kwargs)

        with patch.object(m, 'run_step', side_effect=step):
            with (self.root / 'install.log').open('w') as output:
                m.create_rocsy_checkout(output)
        self.assertEqual(len(installer_calls), 1)
        self.assertTrue((m.PERSONAL_HERMES / '.git').exists())
        self.assertTrue((m.HERMES / '.git').exists())
        self.assertNotEqual(m.PERSONAL_HERMES.resolve(), m.HERMES.resolve())

    def test_launcher_only_install_creates_rocsy_without_reinstalling_personal(self):
        m = self.module
        m.PERSONAL_HERMES = self.root / 'external-personal'
        m.HERMES_URL = str(self.personal)
        installer_calls = []
        real_step = m.run_step

        def step(output, args, **kwargs):
            if args[0] == 'bash':
                installer_calls.append(args)
            else:
                real_step(output, args, **kwargs)

        with patch.object(m, 'run_step', side_effect=step):
            with (self.root / 'install.log').open('w') as output:
                m.create_rocsy_checkout(output)
        self.assertEqual(installer_calls, [])
        self.assertFalse(m.PERSONAL_HERMES.exists())
        self.assertTrue((m.HERMES / '.git').exists())


if __name__ == '__main__':
    unittest.main()
