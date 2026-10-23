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


if __name__ == "__main__":
    unittest.main()
