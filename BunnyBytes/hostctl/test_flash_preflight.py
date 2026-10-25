"""flash_preflight.py with a fake torch and fake hipcc: no GPU, no ROCm, nothing compiled."""
import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flash_preflight  # noqa: E402


class Preflight(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        torch = types.ModuleType("torch")
        torch.version = types.SimpleNamespace(hip="7.2.53211")
        modules = patch.dict(sys.modules, {"torch": torch})
        modules.start()
        self.addCleanup(modules.stop)

    def rocm(self, version):
        home = Path(self.temp.name) / f"rocm-{version}"
        (home / "bin").mkdir(parents=True)
        (home / "bin/hipcc").write_text(f'#!/bin/sh\necho "HIP version: {version}.12345-abc"\n')
        (home / "bin/hipcc").chmod(0o755)
        return home

    def run_check(self, archs, rocm_path):
        out = io.StringIO()
        with patch.dict(os.environ, {"GPU_ARCHS": archs, "ROCM_PATH": str(rocm_path)}), \
             patch.object(sys, "argv", ["flash_preflight.py"]), contextlib.redirect_stdout(out):
            code = flash_preflight.main()
        return code, out.getvalue().splitlines()

    def test_supported_gpu_and_matching_hip(self):
        home = self.rocm("7.2")
        code, lines = self.run_check("gfx1100;gfx1030", home)
        self.assertEqual(code, 0)
        self.assertIn(f"ROCM_HOME={home}", lines)
        self.assertIn("ARCHS=gfx1100", lines)  # RDNA2 dropped with a warning
        self.assertTrue(any(line.startswith("WARN skipping") for line in lines))

    def test_blockers(self):
        cases = [("gfx1100", Path(self.temp.name) / "none", "No ROCm compiler"),
                 ("gfx1100", self.rocm("6.4"), "hipcc is HIP 6.4"),
                 ("gfx1030", self.rocm("7.0"), "doesn't support gfx1030"),
                 ("", self.rocm("7.1"), "No ROCm GPU architecture")]
        for archs, home, message in cases:
            with self.subTest(message):
                code, lines = self.run_check(archs, home)
                self.assertEqual(code, 1)
                self.assertTrue(any(line.startswith("ERR ") and message in line for line in lines), lines)

    def test_reads_supported_targets_from_the_source(self):
        source = Path(self.temp.name) / "src"
        source.mkdir()
        (source / "setup.py").write_text('allowed_archs = ["native", "gfx1100", "gfx1300"]\n')
        self.assertEqual(flash_preflight.supported(source), ["gfx1100", "gfx1300"])
        self.assertEqual(flash_preflight.supported(None), flash_preflight.SUPPORTED)


if __name__ == "__main__":
    unittest.main()
