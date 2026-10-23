"""storage.py against throwaway folders; never touches the real models folder, compose file or containers."""
import hashlib
import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

OVERRIDE = """# personal mounts
services:
  llama:
    volumes:
      # extra GGUF folders
      - /a:/a:ro

  modelhub:
    environment:
      X: "1"
    volumes:
      - /b:/laya:ro

  rea:
    volumes:
      - /c:/folders/c:ro
"""


class Storage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.models = root / "ComfyUI/models"
        (self.models / "loras").mkdir(parents=True)
        (self.models / "llm/big").mkdir(parents=True)
        self.loc = root / "Drive/Rocsy models"
        self.loc.mkdir(parents=True)
        # A second filesystem, so moves there must copy (and verify) instead of hard-linking.
        self.other = Path(tempfile.mkdtemp(dir="/dev/shm", prefix="rocsy-storage-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.other, ignore_errors=True))
        os.environ["STORAGE_STATE"] = str(root / "storage.json")
        os.environ["CORE_COMFY_DIR"] = str(root / "ComfyUI")
        import storage
        self.s = importlib.reload(storage)
        self.s._jobs.clear()
        self.s._queue.clear()
        self.s._apply.update(pending=[], checked=time.time())
        self.s.save({"locations": [{"id": "drive", "label": "Drive", "path": str(self.loc), "added": 0},
                                   {"id": "shm", "label": "Shm", "path": str(self.other), "added": 0}],
                     "defaults": {"models": "comfy", "loras": "drive", "llm": "comfy"}, "known": []})

    def write(self, rel, data=b"x" * 3_000_000):
        path = self.models / rel
        path.write_bytes(data)
        return path

    def move(self, rel, to):
        job = self.s.start_move(rel, to)
        for _ in range(200):
            if self.s._jobs[job["id"]]["state"] in ("done", "failed", "cancelled"):
                break
            time.sleep(0.02)
        return self.s._jobs[job["id"]]

    def test_same_drive_round_trip(self):
        link = self.write("loras/a.safetensors")
        (self.models / "loras/a.safetensors.sha256").write_text("abc")
        job = self.move("loras/a.safetensors", "drive")
        self.assertEqual(job["state"], "done", job["error"])
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path(os.readlink(link)), self.loc / "loras/a.safetensors")
        self.assertEqual(link.read_bytes(), b"x" * 3_000_000)
        self.assertEqual(os.stat(link).st_nlink, 1)                          # old name freed, not a 2nd link
        self.assertTrue((self.models / "loras/a.safetensors.sha256").is_file())  # sidecar stays with the link
        self.assertEqual(self.s.links()["loras/a.safetensors"]["location"], "drive")
        self.s._cache.clear()
        job = self.move("loras/a.safetensors", "comfy")
        self.assertEqual(job["state"], "done", job["error"])
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_bytes(), b"x" * 3_000_000)
        self.assertFalse((self.loc / "loras").exists())                      # emptied folders pruned

    def test_cross_drive_copy_verified_and_shards(self):
        data = os.urandom(5_000_000)
        names = [f"llm/big/m-0000{n}-of-00003.gguf" for n in (1, 2, 3)]
        for name in names:
            self.write(name, data)
        mtime = os.stat(self.models / names[0]).st_mtime
        job = self.move(names[0], "shm")
        self.assertEqual(job["state"], "done", job["error"])
        self.assertEqual(job["files"], 3)
        for name in names:
            link = self.models / name
            self.assertTrue(link.is_symlink())
            self.assertTrue(str(os.readlink(link)).startswith(str(self.other)))
            self.assertEqual(hashlib.sha256(link.read_bytes()).digest(), hashlib.sha256(data).digest())
        self.assertEqual(os.stat(self.models / names[0]).st_mtime, mtime)    # hash caches stay valid
        self.s._cache.clear()
        job = self.move(names[0], "drive")                                   # location -> location
        self.assertEqual(job["state"], "done", job["error"])
        self.assertTrue(str(os.readlink(self.models / names[1])).startswith(str(self.loc)))
        self.assertEqual(list(self.other.rglob("*.gguf")), [])

    def test_refusals(self):
        self.write("loras/b.safetensors")
        foreign = Path(self.temp.name) / "elsewhere.safetensors"
        foreign.write_bytes(b"y" * 10)
        os.symlink(foreign, self.models / "loras/f.safetensors")
        with self.assertRaisesRegex(self.s.Refused, "doesn't manage"):
            self.s.start_move("loras/f.safetensors", "drive")
        with self.assertRaisesRegex(self.s.Refused, "already there"):
            self.s.start_move("loras/b.safetensors", "comfy")
        with self.assertRaisesRegex(self.s.Refused, "Bad model path"):
            self.s.start_move("../x.safetensors", "drive")
        self.s._apply["pending"] = [{"service": "llama", "name": "Llama", "missing": [str(self.loc)], "stale": []}]
        with self.assertRaisesRegex(self.s.Refused, "still being connected"):
            self.s.start_move("loras/b.safetensors", "drive")
        self.s._apply["pending"] = []
        self.move("loras/b.safetensors", "drive")
        with self.assertRaisesRegex(self.s.Refused, "still live there"):
            self.s.remove_location("drive")

    def test_override_blocks(self):
        mounts = [Path("/run/media/u"), Path("/mnt/data")]
        existing = {"modelhub": {"all": {"/mnt/data"}, "same": set()}, "llama": {"all": set(), "same": {"/run"}}}
        text = self.s.render_override(OVERRIDE, mounts, existing)
        self.assertEqual(text.count(self.s.BEGIN), 6)
        llama = text.split("  llama:")[1].split("  modelhub:")[0]
        self.assertIn("/mnt/data", llama)
        self.assertNotIn("/run/media/u", llama)                    # covered by its own /run mount already
        self.assertIn("  krea:" + self.s.OWN_MARK + "\n" + self.s.OWN_VOLUMES, text)
        modelhub = text.split("  modelhub:")[1].split("  rea:")[0]
        self.assertNotIn("/mnt/data", modelhub)                    # never a duplicate target
        self.assertNotIn("read_only", modelhub)                    # Models writes into locations
        self.assertEqual(self.s.render_override(text, [], {}), OVERRIDE)   # removing restores the file exactly
        import yaml
        doc = yaml.safe_load(text)
        vol = doc["services"]["krea"]["volumes"][0]
        self.assertEqual((vol["source"], vol["target"], vol["read_only"], vol["bind"]["propagation"]),
                         ("/run/media/u", "/run/media/u", True, "rslave"))

    def test_default_for(self):
        self.assertEqual(self.s.default_for("loras/KreaCustom")["path"], str(self.loc))
        self.assertEqual(self.s.default_for("diffusion_models")["path"], "")


if __name__ == "__main__":
    unittest.main()
