"""dockeraccess (docker group re-exec, bind folders) + the uninstall cleanup; no real docker or units."""
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dockeraccess  # noqa: E402


class Group:
    def __init__(self, gid, members):
        self.gr_gid, self.gr_mem = gid, members


class DockerGroup(unittest.TestCase):
    def setUp(self):
        user = type("User", (), {"pw_name": "rocsy", "pw_gid": 1000})()
        for target, value in ((dockeraccess.pwd, {"getpwuid": lambda _uid: user}),
                              (dockeraccess.grp, {"getgrnam": lambda _name: Group(969, ["rocsy"])})):
            for name, func in value.items():
                stub = patch.object(target, name, side_effect=func)
                stub.start()
                self.addCleanup(stub.stop)

    def test_member_without_the_group_in_this_process_is_wrapped(self):
        with patch.object(dockeraccess.os, "getgroups", return_value=[1000]), patch.object(dockeraccess.os, "getegid", return_value=1000), \
             patch.object(dockeraccess.shutil, "which", side_effect=lambda name: None if name == "sg" else "/usr/bin/" + name):
            self.assertTrue(dockeraccess.lacks_docker_group())
            self.assertEqual(dockeraccess.group_command(["docker", "compose", "up", "-d"]),
                             ["/usr/bin/newgrp", "docker", "-c", "docker compose up -d"])

    def test_process_that_has_the_group_is_left_alone(self):
        with patch.object(dockeraccess.os, "getgroups", return_value=[1000, 969]):
            self.assertFalse(dockeraccess.lacks_docker_group())
            self.assertEqual(dockeraccess.group_command(["docker", "ps"]), ["docker", "ps"])

    def test_reexec_happens_once(self):
        with patch.object(dockeraccess, "lacks_docker_group", return_value=True), \
             patch.object(dockeraccess.shutil, "which", return_value="/usr/bin/sg"), \
             patch.object(dockeraccess.os, "execv") as execv, patch.dict(os.environ, {}, clear=False):
            os.environ.pop(dockeraccess.REEXEC_FLAG, None)
            dockeraccess.reexec_with_docker_group()
            self.assertEqual(execv.call_count, 1)
            self.assertEqual(execv.call_args.args[1][:3], ["/usr/bin/sg", "docker", "-c"])
            dockeraccess.reexec_with_docker_group()  # the re-run process: flag set, no second exec
            self.assertEqual(execv.call_count, 1)


class BindFolders(unittest.TestCase):
    def test_missing_folders_are_created_files_and_system_paths_are_not(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "exists").mkdir()
            config = {"services": {
                "a": {"volumes": [{"type": "bind", "source": str(root / "data/app")},
                                  {"type": "bind", "source": str(root / "exists")},
                                  {"type": "bind", "source": str(root / "Llama/config.yaml")},
                                  {"type": "bind", "source": "/dev/dri"},
                                  {"type": "volume", "source": "named-data"}]},
                "b": {"volumes": [{"type": "bind", "source": str(root / "data/app")},
                                  {"type": "bind", "source": str(root / "comfy/models/loras/KreaCustom")}]}}}
            self.assertEqual(dockeraccess.bind_sources(config),
                             [root / "data/app", root / "comfy/models/loras/KreaCustom"])
            run = lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout=__import__("json").dumps(config))  # noqa: E731
            made = dockeraccess.precreate_binds(root, run=run)
            self.assertEqual(len(made), 2)
            self.assertTrue((root / "comfy/models/loras/KreaCustom").is_dir())
            self.assertFalse((root / "Llama/config.yaml").exists())

    def test_compose_config_asks_for_every_profile(self):
        seen = []
        def run(argv, **_kwargs):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 1, stdout="")
        with patch.object(dockeraccess, "lacks_docker_group", return_value=False):
            self.assertEqual(dockeraccess.compose_config(Path("."), run=run), {})
        self.assertEqual(seen[0][:2], ["docker", "compose"])
        for name in dockeraccess.ALL_PROFILES:
            self.assertIn(name, seen[0])


class UninstallCleanup(unittest.TestCase):
    def test_container_owned_files_are_removed_through_a_rocsy_image(self):
        import uninstall_worker
        importlib.reload(uninstall_worker)
        calls = []
        rmtree = [PermissionError("root-owned"), None]
        def run(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1 if argv[:3] == ["docker", "image", "inspect"] and argv[3] == "gone" else 0)
        with patch.object(uninstall_worker.subprocess, "run", side_effect=run), \
             patch.object(uninstall_worker.shutil, "rmtree", side_effect=rmtree) as removed:
            uninstall_worker.remove_tree(Path("/srv/Rocsy"), ["gone", "rocsy-lock"])
        self.assertEqual(removed.call_count, 2)
        docker_run = next(c for c in calls if c[:2] == ["docker", "run"])
        self.assertIn("/srv/Rocsy:/target", docker_run)
        self.assertIn("rocsy-lock", docker_run)


if __name__ == "__main__":
    unittest.main()
