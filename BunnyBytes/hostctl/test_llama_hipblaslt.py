from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import llama_hipblaslt as tuning


class HipblasltSelectionTest(unittest.TestCase):
    def test_keeps_auto_for_small_or_unstable_gain(self):
        scores = {"auto_first": {"pp": 100, "tg": 50},
                  "auto_last": {"pp": 100, "tg": 50},
                  "0": {"pp": 102, "tg": 50},
                  "1": {"pp": 110, "tg": 48}}
        self.assertEqual(tuning._choose(scores), "auto")
        scores["auto_last"]["pp"] = 90
        self.assertEqual(tuning._choose(scores), "auto")

    def test_selects_hipblaslt_for_clear_gain(self):
        scores = {"auto_first": {"pp": 100, "tg": 50},
                  "auto_last": {"pp": 101, "tg": 50},
                  "0": {"pp": 98, "tg": 50},
                  "1": {"pp": 120, "tg": 55}}
        self.assertEqual(tuning._choose(scores), "1")

    def test_delete_removes_record_and_launcher_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with mock.patch.object(tuning, "ROOT", root / "records"), \
                 mock.patch.object(tuning.opt, "OVERRIDES", root / "overrides"):
                record, override = tuning._paths("test-model")
                record.parent.mkdir()
                override.parent.mkdir()
                record.write_text("{}")
                override.write_text("1\n")
                tuning.delete("test-model")
                self.assertFalse(record.exists())
                self.assertFalse(override.exists())


if __name__ == "__main__":
    unittest.main()
