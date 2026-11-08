"""Music covers (RSP): model discovery, checked + resumed downloads, and RSP's copy into ComfyUI. No network."""
import hashlib
import http.server
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import comfyinstall  # noqa: E402
import rspmodels  # noqa: E402

BODY = bytes(range(256)) * 4096  # 1 MiB
SHA = hashlib.sha256(BODY).hexdigest()


class RangeHandler(http.server.BaseHTTPRequestHandler):
    ranges = True

    def do_GET(self):  # noqa: N802
        start = 0
        if self.ranges and self.headers.get("Range"):
            start = int(self.headers["Range"].split("=")[1].rstrip("-"))
            self.send_response(206)
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(BODY) - start))
        self.end_headers()
        self.wfile.write(BODY[start:])

    def log_message(self, *args):
        pass


class Models(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.comfy = self.base / "ComfyUI"
        (self.comfy / "models").mkdir(parents=True)
        env = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.base / "cache")})
        env.start()
        self.addCleanup(env.stop)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/file"

    def test_extra_model_paths_are_searched_after_models(self):
        shared = self.base / "Shared"
        (shared / "models/checkpoints").mkdir(parents=True)
        (self.comfy / "extra_model_paths.yaml").write_text(
            f"shared:\n  base_path: {shared}\n  checkpoints: |\n    models/checkpoints\n    /abs/ckpts\n")
        self.assertEqual(rspmodels.model_roots(self.comfy, "checkpoints"),
                         [self.comfy / "models/checkpoints", shared / "models/checkpoints", Path("/abs/ckpts")])
        (shared / "models/checkpoints/x.safetensors").write_bytes(b"12345")
        self.assertEqual(rspmodels.find(self.comfy, "checkpoints", "x.safetensors", 5),
                         shared / "models/checkpoints/x.safetensors")
        self.assertIsNone(rspmodels.find(self.comfy, "checkpoints", "x.safetensors", 6))  # wrong size: not it

    def test_whisper_goes_to_openai_whispers_cache(self):
        self.assertEqual(rspmodels.model_roots(self.comfy, ""), [self.base / "cache/whisper"])

    def test_download_resumes_and_checks_the_hash(self):
        dest = self.comfy / "models/YuE2/f.ckpt"
        dest.parent.mkdir(parents=True)
        dest.with_name("f.ckpt.part").write_bytes(BODY[:300000])
        rspmodels.download(self.url, dest, len(BODY), SHA)
        self.assertEqual(dest.read_bytes(), BODY)
        self.assertFalse(dest.with_name("f.ckpt.part").exists())

    def test_server_without_ranges_starts_again(self):
        RangeHandler.ranges = False
        self.addCleanup(setattr, RangeHandler, "ranges", True)
        dest = self.comfy / "models/YuE2/f.ckpt"
        dest.parent.mkdir(parents=True)
        dest.with_name("f.ckpt.part").write_bytes(b"junk" * 1000)
        rspmodels.download(self.url, dest, len(BODY), SHA)
        self.assertEqual(dest.read_bytes(), BODY)

    def test_wrong_hash_is_removed_never_renamed(self):
        dest = self.comfy / "models/YuE2/f.ckpt"
        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            rspmodels.download(self.url, dest, len(BODY), "0" * 64)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name("f.ckpt.part").exists())

    def test_download_missing_skips_files_on_disk(self):
        on_disk = self.comfy / "models/checkpoints/have.safetensors"
        on_disk.parent.mkdir(parents=True)
        on_disk.write_bytes(BODY)
        models = [("have", "Have", "", "checkpoints", "have.safetensors", None, self.url + "?never", len(BODY), SHA),
                  ("get", "Get", "", "YuE2", "sub/get.ckpt", None, self.url, len(BODY), SHA)]
        log = []
        with patch.object(rspmodels, "MODELS", models), patch.object(rspmodels, "SPARE", 0):
            problems = rspmodels.download_missing(self.comfy, type("Out", (), {"write": log.append, "flush": lambda: None}))
        self.assertEqual(problems, [])
        self.assertEqual((self.comfy / "models/YuE2/sub/get.ckpt").read_bytes(), BODY)
        self.assertNotIn("?never", "".join(log))
        status = None
        with patch.object(rspmodels, "MODELS", models):
            status = rspmodels.status(self.comfy)
        self.assertEqual([f["on_disk"] for f in status["files"][:2]], [True, True])


class LocalPack(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root, self.comfy = base / "Rocsy", base / "ComfyUI"
        self.source = self.root / "Music/comfy_nodes/RSP"
        (self.source / "rsp").mkdir(parents=True)
        (self.source / "__init__.py").write_text("v2\n")
        (self.source / "rsp/nodes.py").write_text("nodes v2\n")
        (self.source / ".venv").mkdir()
        (self.source / ".venv/junk").write_text("never copied")
        packs = patch.object(comfyinstall, "STUDIO_PACKS", [])
        packs.start()
        self.addCleanup(packs.stop)
        local = patch.object(comfyinstall, "STUDIO_LOCAL_PACKS", [("Music", "RSP")])
        local.start()
        self.addCleanup(local.stop)
        self.target = self.comfy / "custom_nodes/RSP"

    def setup(self, sync):
        return comfyinstall.studio_setup(self.comfy, self.root, lambda *a: None, sync=sync)

    def test_copied_when_missing_without_per_machine_folders(self):
        self.assertEqual(self.setup(False), [])
        self.assertEqual((self.target / "rsp/nodes.py").read_text(), "nodes v2\n")
        self.assertFalse((self.target / ".venv").exists())
        self.assertEqual(comfyinstall.studio_status(self.comfy, self.root)["rsp"], "current")

    def test_sync_updates_code_and_keeps_venv_and_backups(self):
        (self.target / "rsp").mkdir(parents=True)
        (self.target / "__init__.py").write_text("v1\n")
        (self.target / "rsp/nodes.py.bak-1").write_text("backup")
        (self.target / ".venv").mkdir()
        self.assertEqual(comfyinstall.studio_status(self.comfy, self.root)["rsp"], "outdated")
        self.setup(False)
        self.assertEqual((self.target / "__init__.py").read_text(), "v1\n")  # only the Core Backend job writes over it
        self.setup(True)
        self.assertEqual((self.target / "__init__.py").read_text(), "v2\n")
        self.assertTrue((self.target / "rsp/nodes.py.bak-1").is_file())
        self.assertTrue((self.target / ".venv").is_dir())
        self.assertEqual(comfyinstall.studio_status(self.comfy, self.root)["rsp"], "current")

    def test_a_link_to_rocsys_copy_is_left_alone(self):
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(self.source)
        self.assertEqual(comfyinstall.studio_status(self.comfy, self.root)["rsp"], "linked")
        self.setup(True)
        self.assertTrue(self.target.is_symlink())


if __name__ == "__main__":
    unittest.main()
