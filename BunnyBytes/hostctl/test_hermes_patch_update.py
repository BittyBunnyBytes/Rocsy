"""Rocsy Hermes update across a patch refresh: the earlier Rocsy patch comes off, the release changes,
the current patch goes on; on a failure the checkout goes back exactly as it was. Throwaway git repos only."""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().with_name("coreextras.py")


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()


class PatchRefresh(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hermes-patch-refresh-")
        self.addCleanup(self.temp.cleanup)
        root = self.root = Path(self.temp.name)
        spec = importlib.util.spec_from_file_location("coreextras_patch_refresh", MODULE)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        up = root / "upstream"
        git("init", "-q", "-b", "main", up)
        git("-C", up, "config", "user.name", "T")
        git("-C", up, "config", "user.email", "t@example.test")
        (up / "runs.py").write_text("old layout\nagent()\n")
        git("-C", up, "add", ".")
        git("-C", up, "commit", "-qm", "v1")
        git("-C", up, "tag", "v1")
        # The release the earlier patch was made for, and that patch.
        (up / "runs.py").write_text("old layout\nagent(rocsy=True)\n")
        legacy = git("-C", up, "diff") + "\n"
        git("-C", up, "checkout", "-q", "runs.py")
        # A release that restructures the same lines: the earlier patch no longer applies.
        (up / "runs.py").write_text("new layout\nadmission()\nagent()\n")
        git("-C", up, "commit", "-qam", "v2")
        git("-C", up, "tag", "v2")
        self.v2 = git("-C", up, "rev-parse", "HEAD")
        (up / "runs.py").write_text("new layout\nadmission(rocsy=True)\nagent(rocsy=True)\n")
        current = git("-C", up, "diff") + "\n"
        git("-C", up, "checkout", "-q", "runs.py")
        patches = root / "patches"
        (patches / "previous").mkdir(parents=True)
        (patches / "previous" / "v1.patch").write_text(legacy)
        (patches / "rocsy-addons.patch").write_text(current)
        checkout = root / "rocsy"
        git("clone", "-q", up, checkout)
        git("-C", checkout, "switch", "-q", "--detach", "v1")
        git("-C", checkout, "apply", patches / "previous" / "v1.patch")
        (checkout / "runs.py.bak-20261017-mine").write_text("a backup\n")  # untracked: must not block
        self.m.HERMES, self.m.HERMES_PATCH = checkout, patches / "rocsy-addons.patch"
        self.m.HERMES_LEGACY_PATCHES = patches / "previous"
        self.checkout = checkout
        self.log = (root / "log.txt").open("w")
        self.addCleanup(self.log.close)

    def test_status_names_the_earlier_patch(self):
        status = self.m.hermes_addons_status()
        self.assertFalse(status["addons_applied"])
        self.assertTrue(status["addons_legacy"])
        self.assertEqual(self.m.legacy_hermes_patch().name, "v1.patch")

    def test_update_swaps_the_earlier_patch_for_the_current_one(self):
        with patch.object(self.m, "hermes_stable_target", return_value={"commit": self.v2, "version": "2"}):
            self.assertTrue(self.m.pull_hermes(self.log))
        self.assertEqual(git("-C", self.checkout, "rev-parse", "HEAD"), self.v2)
        self.assertEqual((self.checkout / "runs.py").read_text(), "new layout\nadmission(rocsy=True)\nagent(rocsy=True)\n")
        self.assertTrue(self.m.hermes_addons_status()["addons_applied"])

    def test_a_failed_update_leaves_the_checkout_as_it_was(self):
        real = self.m.run_step

        def failing(output, args, *rest, **kw):
            if args[-1] == str(self.m.HERMES_PATCH) and "--reverse" not in args and "--check" not in args:
                raise RuntimeError("apply failed")
            return real(output, args, *rest, **kw)
        with patch.object(self.m, "hermes_stable_target", return_value={"commit": self.v2, "version": "2"}), \
             patch.object(self.m, "run_step", side_effect=failing):
            with self.assertRaises(RuntimeError):
                self.m.pull_hermes(self.log)
        self.assertEqual(git("-C", self.checkout, "describe", "--tags"), "v1")
        self.assertEqual((self.checkout / "runs.py").read_text(), "old layout\nagent(rocsy=True)\n")
        self.assertEqual(self.m.legacy_hermes_patch().name, "v1.patch")


if __name__ == "__main__":
    unittest.main()
