"""comfynodes update detection + updates against throwaway git repos; never touches the real ComfyUI."""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


def commit(repo, name, text):
    (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", name)


class ComfyNodeUpdates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.comfy = root / "ComfyUI"
        (self.comfy / "custom_nodes").mkdir(parents=True)
        # A fake venv python: `pip list` answers, `pip install` fails when the requirements say so.
        venv = self.comfy / ".venv/bin"
        venv.mkdir(parents=True)
        (venv / "python3").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in *'pip list'*) echo '[{\"name\": \"torch\", \"version\": \"2.9.1\"}]';; "
            "*'pip install'*) ! grep -q broken \"$4\";; esac\n")
        (venv / "python3").chmod(0o755)
        env = patch.dict(os.environ, {"CORE_COMFY_DIR": str(self.comfy), "CORE_EXTRAS_STATE": str(root / "state")})
        env.start()
        self.addCleanup(env.stop)
        sys.path.insert(0, str(HERE))
        self.addCleanup(sys.path.remove, str(HERE))
        import comfynodes
        self.m = importlib.reload(comfynodes)
        # Upstream repo + a shallow clone in custom_nodes, like the installer makes.
        self.upstream = root / "upstream"
        self.upstream.mkdir()
        git(self.upstream, "init", "-q", "-b", "main")
        commit(self.upstream, "nodes.py", "v1")
        self.pack = self.comfy / "custom_nodes/Pack"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.upstream}", str(self.pack)], check=True)
        # inspect() only checks http(s) remotes; point it at the file remote through a URL rewrite.
        git(self.pack, "remote", "set-url", "origin", "https://example.invalid/Pack")
        git(self.pack, "config", f"url.file://{self.upstream}.insteadOf", "https://example.invalid/Pack")

    def status(self):
        return {p["dir"]: p for p in self.m.check()["packs"]}["Pack"]

    def run_update(self):
        self.m.start_update.__globals__["_launch"] = lambda clean: None  # run in-process instead of systemd-run
        self.m.start_update(["Pack"])
        self.m.run()
        return json.loads(self.m.STATE.read_text())

    def test_current_then_behind_then_updated(self):
        self.assertEqual(self.status()["status"], "current")
        commit(self.upstream, "nodes.py", "v2")
        self.assertEqual(self.status()["status"], "behind")
        state = self.run_update()
        self.assertEqual(state["repos"][0]["state"], "updated", state)
        self.assertTrue(state["restart_needed"])
        self.assertEqual((self.pack / "nodes.py").read_text(), "v2")
        self.assertEqual(self.status()["status"], "current")

    def test_local_edits_are_never_updated(self):
        commit(self.upstream, "nodes.py", "v2")
        (self.pack / "nodes.py").write_text("my patch")
        self.assertEqual(self.status()["status"], "modified")
        with self.assertRaises(self.m.Refused):
            self.m.start_update(["Pack"])
        self.assertEqual((self.pack / "nodes.py").read_text(), "my patch")

    def test_own_commits_are_never_updated(self):
        commit(self.upstream, "nodes.py", "v2")
        commit(self.pack, "mine.py", "local work")
        self.assertEqual(self.status()["status"], "diverged")

    def test_failed_requirements_roll_back(self):
        commit(self.upstream, "requirements.txt", "broken-package")
        self.assertEqual(self.status()["status"], "behind")
        before = subprocess.run(["git", "-C", str(self.pack), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        state = self.run_update()
        self.assertEqual(state["repos"][0]["state"], "failed", state)
        after = subprocess.run(["git", "-C", str(self.pack), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        self.assertEqual(before, after)
        self.assertFalse((self.pack / "requirements.txt").exists())

    def test_folder_without_git_is_local(self):
        (self.comfy / "custom_nodes/MyNodes").mkdir()
        packs = {p["dir"]: p for p in self.m.check()["packs"]}
        self.assertEqual(packs["MyNodes"]["status"], "local")
        with self.assertRaises(self.m.Refused):
            self.m.start_update(["MyNodes"])


if __name__ == "__main__":
    unittest.main()
