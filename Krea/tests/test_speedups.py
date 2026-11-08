from __future__ import annotations

import copy
import unittest

from backend import speedups
from backend.config import settings
from backend.generation import build_workflows
from backend.models import GenerationRequest

MODELS = ("krea2", "lustify", "qwen_image_21", "moss", "anima")


class AttentionPatchTests(unittest.TestCase):
    def workflow(self, model: str, **extra) -> dict:
        return build_workflows(settings, GenerationRequest(model=model, prompt="a fox", **extra))[0][0]

    def test_flash_leaves_graph_unchanged(self) -> None:
        for model in MODELS:
            workflow = self.workflow(model)
            before = copy.deepcopy(workflow)
            self.assertFalse(speedups.apply_attention(workflow, "flash"))
            self.assertEqual(workflow, before)

    def test_every_sampler_gets_the_patched_model(self) -> None:
        for model in MODELS:
            for extra in ({}, {"speed": "viggle"} if model == "qwen_image_21" else {"speed": "dmd8"} if model == "moss" else {}):
                with self.subTest(model=model, **extra):
                    workflow = self.workflow(model, **extra)
                    self.assertTrue(speedups.apply_attention(workflow, "pytorch"))
                    patches = {k: n for k, n in workflow.items() if n["class_type"] == speedups.ATTENTION_NODE}
                    self.assertTrue(patches)
                    for node in workflow.values():
                        if node["class_type"] in speedups.SAMPLER_CLASSES and "model" in node["inputs"]:
                            self.assertIn(node["inputs"]["model"][0], patches)
                    for node in patches.values():
                        self.assertEqual(node["inputs"]["attention"], "pytorch")
                        self.assertNotIn(node["inputs"]["model"][0], patches)   # no patch chains

    def test_auto_uses_measured_best(self) -> None:
        self.assertEqual(speedups.resolve_attention("unknown-model", "auto"), "flash")
        self.assertEqual(speedups.resolve_attention("krea2", "kitchen"), "kitchen")
        self.assertEqual(speedups.resolve_attention("krea2", "bogus"), "flash")
        self.assertEqual(speedups.resolve_attention("moss", "kitchen"), "flash")   # MOSS ignores it
        for model, best in speedups.ATTENTION_BEST.items():
            self.assertEqual(speedups.resolve_attention(model, "auto"), best)
            self.assertIn(best, speedups.ATTENTION_BACKENDS)


if __name__ == "__main__":
    unittest.main()
