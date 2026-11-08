"""Epoch cleanup (Trainer settings + a run's card): what is deleted, what always stays.

Run inside the Trainer image: TRAINER_DATA_DIR=<empty temp dir> python -m unittest test_epoch_cleanup
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("TRAINER_DATA_DIR", tempfile.mkdtemp(prefix="trainer-test-"))
import app  # noqa: E402

from fastapi import HTTPException  # noqa: E402


class EpochCleanup(unittest.TestCase):
    def setUp(self):
        self.job = os.urandom(16).hex()
        self.folder = app.DATA_DIR / "output" / self.job
        self.folder.mkdir(parents=True)
        self.item = {"id": self.job, "name": "Witty", "model": "qwen_image_21", "state": "complete",
                     "request_json": json.dumps({"kind": "lora"})}
        self.real_get = app.store.get
        app.store.get = lambda job_id: self.item if job_id == self.job else None
        self.logs = []
        self.real_log = app.store.append_log
        app.store.append_log = lambda _job, text: self.logs.append(text)
        app.SETTINGS_FILE.unlink(missing_ok=True)

    def tearDown(self):
        app.store.get, app.store.append_log = self.real_get, self.real_log

    def epochs(self, numbers, final=True, state_for=()):
        for n in numbers:
            (self.folder / f"Witty-{n:06d}.safetensors").write_bytes(b"x" * 10)
        for n in state_for:
            (self.folder / f"Witty-{n:06d}-state").mkdir()
            (self.folder / f"Witty-{n:06d}-state/model.safetensors").write_bytes(b"s" * 10)
        if final:
            (self.folder / "Witty.safetensors").write_bytes(b"f" * 10)

    def left(self):
        return sorted(p.relative_to(self.folder).as_posix() for p in self.folder.rglob("*.safetensors"))

    def test_every_nth_keeps_multiples_final_and_stars(self):
        self.epochs(range(1, 11), state_for=[3])
        stars = app.epoch_tests.folder(self.job)
        stars.mkdir(parents=True, exist_ok=True)
        (stars / "stars.json").write_text('["Epoch 7"]')
        result = app._run_cleanup(self.item, "every_nth", 5, "Test")
        self.assertEqual(result["kept_epochs"], [5, 7, 10])
        self.assertEqual(self.left(), ["Witty-000005.safetensors", "Witty-000007.safetensors",
                                       "Witty-000010.safetensors", "Witty.safetensors"])
        self.assertFalse((self.folder / "Witty-000003-state").exists())  # epoch 3's resume state went with it
        self.assertIn("Deleted epochs 1, 2, 3, 4, 6, 8, 9", "".join(self.logs))

    def test_all_but_final(self):
        self.epochs(range(1, 6))
        app._run_cleanup(self.item, "all_but_final", 5, "Test")
        self.assertEqual(self.left(), ["Witty.safetensors"])

    def test_without_a_final_file_the_newest_epoch_stays(self):
        self.epochs(range(1, 6), final=False)
        app._run_cleanup(self.item, "all_but_final", 5, "Test")
        self.assertEqual(self.left(), ["Witty-000005.safetensors"])

    def test_dry_run_and_refusals(self):
        self.epochs(range(1, 5))
        body = app.CleanupRequest(mode="all_but_final", dry_run=True)
        plan = asyncio.run(app.cleanup_epochs(self.job, body))
        self.assertEqual((plan["deleted_epochs"], plan["files"], plan["freed"]), ([1, 2, 3, 4], 4, 40))
        self.assertEqual(len(self.left()), 5)  # nothing deleted
        self.item["state"] = "running"
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(app.cleanup_epochs(self.job, app.CleanupRequest(mode="all_but_final")))
        self.assertEqual(caught.exception.status_code, 409)

    def test_settings_drive_the_automatic_cleanup(self):
        self.epochs(range(1, 7))
        self.assertEqual(asyncio.run(app.get_settings())["epoch_cleanup"]["mode"], "on_demand")
        asyncio.run(app._auto_cleanup(self.job))
        self.assertEqual(len(self.left()), 7)  # on demand: untouched
        asyncio.run(app.put_settings(app.TrainerSettings.model_validate({"epoch_cleanup": {"mode": "every_nth", "every": 3}})))
        asyncio.run(app._auto_cleanup(self.job))
        self.assertEqual(self.left(), ["Witty-000003.safetensors", "Witty-000006.safetensors", "Witty.safetensors"])

    def test_outputs_say_which_file_is_which(self):
        self.epochs([2], state_for=[2])
        outputs = {o["rel"]: o for o in app._job_outputs(self.item)}
        self.assertEqual(outputs["Witty-000002.safetensors"]["epoch"], 2)
        self.assertTrue(outputs["Witty-000002-state/model.safetensors"]["resume_state"])
        self.assertTrue(outputs["Witty.safetensors"]["final"])


if __name__ == "__main__":
    unittest.main()
