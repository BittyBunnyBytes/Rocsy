from __future__ import annotations

import json
import unittest

from backend import condcache
from backend.config import settings
from backend.generation import build_workflows
from backend.models import GenerationRequest

REFS = [{"enabled": True, "image": "a.png", "strength": 1.0}, {"enabled": True, "image": "b.png", "strength": 1.0}]


class CondCacheWrapTests(unittest.TestCase):
    def check(self, request: GenerationRequest) -> dict:
        workflow = build_workflows(settings, request)[0][0]
        wrapped = condcache.wrap(workflow)
        self.assertEqual(set(wrapped), set(workflow))   # same node ids, so links/progress stay valid
        stock = [k for k, n in workflow.items() if n["class_type"] in condcache.ENCODERS]
        self.assertTrue(stock)
        for key in stock:
            node, original = wrapped[key], workflow[key]
            self.assertEqual(node["class_type"], condcache.NODE)
            self.assertEqual(node["inputs"]["encoder"], original["class_type"])
            self.assertEqual(node["inputs"]["clip"], original["inputs"]["clip"])
            params = json.loads(node["inputs"]["params"])
            for name in condcache.ENCODERS[original["class_type"]]:
                if name in original["inputs"]:
                    self.assertEqual(params[name], original["inputs"][name])
        self.assertTrue(all(n["class_type"] not in condcache.ENCODERS for n in wrapped.values()))
        self.assertNotEqual(workflow, wrapped)   # input untouched (deep copy)
        return wrapped

    def test_every_model(self) -> None:
        for request in (
            GenerationRequest(model="krea2", prompt="a fox"),
            GenerationRequest(model="lustify", prompt="a fox"),
            GenerationRequest(model="krea2", prompt="a fox", use_references=True, references=REFS),
            GenerationRequest(model="qwen_image_21", prompt="a fox"),
            GenerationRequest(model="moss", prompt="a fox"),
            GenerationRequest(model="anima", prompt="a fox"),
        ):
            with self.subTest(model=request.model, refs=request.use_references):
                self.check(request)

    def test_qwen_reference_images_map_to_slots(self) -> None:
        wrapped = self.check(GenerationRequest(model="qwen_image_21", prompt="a fox", use_references=True, references=REFS))
        node = next(n for n in wrapped.values() if n["class_type"] == condcache.NODE)
        self.assertIn("image_1", node["inputs"])
        self.assertIn("image_2", node["inputs"])
        self.assertIn("vae", node["inputs"])

    def test_linked_text_is_left_alone(self) -> None:
        workflow = {"1": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": ["9", 0]}}}
        self.assertEqual(condcache.wrap(workflow), workflow)


if __name__ == "__main__":
    unittest.main()
