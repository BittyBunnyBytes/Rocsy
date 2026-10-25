"""ComfyUI setup + supervisor for components skipped in the installer; never touches real units or ComfyUI."""
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
README = "pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/rocm7.1\n"


class InstallLater(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Rocsy"
        (self.root / "BunnyBytes/supervisors").mkdir(parents=True)
        (self.root / "BunnyBytes/supervisors/comfyui-supervisor.service").write_text(
            "[Service]\nExecStart=/usr/bin/python3 @ROCSY_ROOT@/BunnyBytes/supervisors/comfyui_supervisor.py\n"
            "Environment=ROCSY_COMFY_ROOT=@COMFY_ROOT@\n")
        self.comfy = self.root / "external/ComfyUI"
        (self.comfy / "models/vae").mkdir(parents=True)  # what the installer pre-creates
        home = patch.dict(os.environ, {"HOME": str(Path(self.temp.name) / "home"),
                                       "CORE_COMFY_DIR": str(self.comfy), "CORE_EXTRAS_STATE": str(self.root / "state"),
                                       "CORE_COMPOSE_DIR": str(self.root / "BunnyBytes")})
        home.start()
        self.addCleanup(home.stop)
        sys.path.insert(0, str(HERE))
        self.addCleanup(sys.path.remove, str(HERE))
        import comfyinstall
        import coreextras
        self.ci = importlib.reload(comfyinstall)
        self.cx = importlib.reload(coreextras)
        self.cx.ROCSY_ROOT = self.root
        # systemctl: everything succeeds and units report active; recorded, never real.
        self.calls = []

        def fake(command, *args, **kwargs):
            self.calls.append(command)
            if command[:2] == ["git", "clone"]:
                stage = Path(command[-1])
                (stage / "models/vae").mkdir(parents=True)
                for name, text in (("main.py", ""), ("requirements.txt", ""), ("README.md", README)):
                    (stage / name).write_text(text)
            if command[1:3] == ["-m", "venv"]:
                (Path(command[3]) / "bin").mkdir(parents=True)
                (Path(command[3]) / "bin/python").write_text("")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        runner = patch.object(subprocess, "run", side_effect=fake)
        runner.start()
        self.addCleanup(runner.stop)
        self.find = patch.object(self.ci, "find_python", return_value="/usr/bin/python3")
        self.find.start()
        self.addCleanup(self.find.stop)
        self.h3 = patch.object(self.ci, "h3_setup", return_value=[])  # H3Setup below covers it
        self.h3.start()
        self.addCleanup(self.h3.stop)

    def unit(self):
        return Path(os.environ["HOME"]) / ".config/systemd/user/comfyui-supervisor.service"

    def test_download_later_sets_up_comfyui_and_supervisor(self):
        self.assertFalse(self.ci.ready(self.comfy))
        with open(os.devnull, "w") as out, patch.object(self.ci.time, "sleep"):
            self.assertEqual(self.cx.run_comfy_setup(out), 0)
        self.assertTrue(self.ci.ready(self.comfy))
        self.assertTrue((self.comfy / "models/vae").is_dir())
        text = self.unit().read_text()
        self.assertIn(str(self.root / "BunnyBytes/supervisors"), text)
        self.assertIn(f"ROCSY_COMFY_ROOT={self.comfy}", text)
        self.assertEqual(self.ci.supervisor_status(self.root)["state"], "ours")
        self.assertIn(["systemctl", "--user", "enable", "comfyui-supervisor.service"], self.calls)
        torch = next(c for c in self.calls if "torch" in c)
        self.assertEqual(torch[-1], "https://download.pytorch.org/whl/rocm7.1")
        self.ci.h3_setup.assert_called_once()

    def test_finish_setup_resumes_without_cloning_again(self):
        (self.comfy / "main.py").write_text("")
        (self.comfy / "requirements.txt").write_text("")
        (self.comfy / "README.md").write_text(README)
        with open(os.devnull, "w") as out, patch.object(self.ci.time, "sleep"):
            self.cx.run_comfy_setup(out)
        self.assertFalse(any(c[:2] == ["git", "clone"] for c in self.calls))
        self.assertTrue(self.ci.ready(self.comfy))

    def test_supervisor_of_another_comfyui_is_left_alone(self):
        self.unit().parent.mkdir(parents=True)
        self.unit().write_text("[Service]\nExecStart=/usr/bin/python3 /elsewhere/supervisor/comfyui_supervisor.py\n")
        status = self.ci.supervisor_status(self.root)
        self.assertEqual((status["state"], status["script"]), ("other", "/elsewhere/supervisor/comfyui_supervisor.py"))
        with open(os.devnull, "w") as out, patch.object(self.ci.time, "sleep"):
            self.assertEqual(self.cx.run_comfy_setup(out), 0)  # ComfyUI set up; supervisor note only
            self.assertEqual(self.cx.run_comfy_supervisor(out), 1)
        self.assertIn("/elsewhere/", self.unit().read_text())
        self.assertFalse(any(c[:3] == ["systemctl", "--user", "enable"] for c in self.calls))

    def test_skipped_services_keep_their_profile_after_installing(self):
        env = self.root / "BunnyBytes/.env"
        env.write_text("MASTER_PASSWORD=x\nCOMPOSE_PROFILES=motion\n")
        self.cx.enable_profile("trainer")
        self.cx.enable_profile("rea")
        self.cx.enable_profile("hermes")
        self.cx.enable_profile("rea")
        self.assertEqual(env.read_text(), "MASTER_PASSWORD=x\nCOMPOSE_PROFILES=motion,trainer,rea,hermes\n")

    def test_profiles_in_a_setup_without_them(self):
        env = self.root / "BunnyBytes/.env"
        env.write_text("MASTER_PASSWORD=x\n")
        self.cx.enable_profile("motion")  # not a profile in the main compose: nothing to remember
        self.assertNotIn("COMPOSE_PROFILES", env.read_text())
        self.cx.enable_profile("rea")
        self.assertIn("COMPOSE_PROFILES=rea\n", env.read_text())

class H3Setup(unittest.TestCase):
    """h3_setup against local git repos (no network): packs at their commit, stacked patches, re-runs."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        sys.path.insert(0, str(HERE))
        self.addCleanup(sys.path.remove, str(HERE))
        import comfyinstall
        self.ci = importlib.reload(comfyinstall)
        git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
        def repo(path, files):
            path.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
            for name, text in files.items():
                (path / name).parent.mkdir(parents=True, exist_ok=True)
                (path / name).write_text(text)
            subprocess.run([*git, "-C", str(path), "add", "-A"], check=True)
            subprocess.run([*git, "-C", str(path), "commit", "-qm", "c"], check=True)
            return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.comfy = base / "ComfyUI"
        repo(self.comfy, {"vae.py": "a\nb\nc\n"})
        (self.comfy / ".venv/bin").mkdir(parents=True)
        (self.comfy / ".venv/bin/python").symlink_to(sys.executable)
        upstream = base / "upstream-pack"
        tested = repo(upstream, {"nodes.py": "one\ntwo\nthree\nfour\nfive\nsix\nseven\n"})
        (upstream / "nodes.py").write_text("newer upstream\n")
        subprocess.run([*git, "-C", str(upstream), "commit", "-qam", "newer"], check=True)
        self.root = base / "Rocsy"
        tools = self.root / "Video/tools"
        tools.mkdir(parents=True)
        (tools / "core.patch").write_text("--- a/vae.py\n+++ b/vae.py\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n")
        (tools / "first.patch").write_text("--- a/nodes.py\n+++ b/nodes.py\n@@ -1,4 +1,4 @@\n one\n-two\n+TWO\n three\n four\n")
        (tools / "second.patch").write_text("--- a/nodes.py\n+++ b/nodes.py\n@@ -2,3 +2,3 @@\n TWO\n-three\n+THREE\n four\n")
        (self.root / "Video/comfy_nodes/local-pack").mkdir(parents=True)
        (self.root / "Video/comfy_nodes/local-pack/__init__.py").write_text("")
        for name, value in (("H3_PACKS", [("pack", str(upstream), tested, "")]), ("H3_LOCAL_PACKS", ["local-pack"]),
                            ("H3_PATCHES", [("", "core.patch"), ("custom_nodes/pack", "first.patch"),
                                            ("custom_nodes/pack", "second.patch")])):
            patcher = patch.object(self.ci, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.steps = []

    def run_step(self, command, message, network):
        self.steps.append(message)
        subprocess.run(command, check=True, capture_output=True)

    def test_installs_at_the_tested_commit_patches_and_reruns_cleanly(self):
        self.assertEqual(self.ci.h3_setup(self.comfy, self.root, self.run_step), [])
        self.assertEqual((self.comfy / "custom_nodes/pack/nodes.py").read_text().splitlines()[:3], ["one", "TWO", "THREE"])
        self.assertEqual((self.comfy / "vae.py").read_text(), "a\nB\nc\n")
        self.assertTrue((self.comfy / "custom_nodes/local-pack/__init__.py").is_file())
        branch = subprocess.run(["git", "-C", str(self.comfy / "custom_nodes/pack"), "status", "-sb"],
                                capture_output=True, text=True).stdout
        self.assertIn("[behind 1]", branch)  # on its branch, so Core Backend's update check still sees it
        status = self.ci.h3_status(self.comfy, self.root)
        self.assertEqual(status, {"missing_packs": [], "patches": dict.fromkeys(
            ["core.patch", "first.patch", "second.patch"], "applied")})
        self.steps.clear()
        self.assertEqual(self.ci.h3_setup(self.comfy, self.root, self.run_step), [])
        self.assertEqual(self.steps, [])

    def test_core_patch_comes_off_for_an_update_and_goes_back(self):
        self.ci.h3_setup(self.comfy, self.root, self.run_step)
        self.assertEqual(self.ci.unpatch_core(self.comfy, self.root), ["core.patch"])
        self.assertEqual((self.comfy / "vae.py").read_text(), "a\nb\nc\n")
        self.assertEqual(self.ci.h3_setup(self.comfy, self.root, self.run_step), [])
        self.assertEqual((self.comfy / "vae.py").read_text(), "a\nB\nc\n")

    def test_conflicting_patch_is_reported_not_forced(self):
        (self.comfy / "vae.py").write_text("x\ny\nz\n")
        problems = self.ci.h3_setup(self.comfy, self.root, self.run_step)
        self.assertEqual(len(problems), 1)
        self.assertIn("core.patch", problems[0])
        self.assertEqual((self.comfy / "vae.py").read_text(), "x\ny\nz\n")


if __name__ == "__main__":
    unittest.main()
