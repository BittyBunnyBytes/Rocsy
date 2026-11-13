from __future__ import annotations

import unittest

from pydantic import ValidationError

from backend import checkpoints, condcache, h3image
from backend.config import settings
from backend.generation import build_workflows, display_size
from backend.models import MODEL_H3_IMAGE, GenerationRequest

W4A8 = "minimax_h3_image_pruned_w4a8.safetensors"


def request(**extra) -> GenerationRequest:
    return GenerationRequest.model_validate({"model": MODEL_H3_IMAGE, "checkpoint": W4A8, "prompt": "a fox",
                                             "width": 2048, "height": 2048, **extra})


class H3ImageGraphTests(unittest.TestCase):
    def test_text_to_image(self):
        workflows, seed, stage = build_workflows(settings, request(seed=7, count=2))
        self.assertEqual((stage, seed, len(workflows)), ("render", 7, 2))
        graph = workflows[0]
        self.assertEqual(graph["1"]["inputs"]["unet_name"], W4A8)
        self.assertEqual(graph["2"]["inputs"], {"clip_name": settings.h3_image_clip_name, "type": "minimax", "device": "default"})
        self.assertEqual(graph["3"]["inputs"]["vae_name"], settings.h3_image_vae_name)
        cond = graph["5"]["inputs"]
        self.assertEqual((cond["width"], cond["height"], cond["prompt"]), (2048, 2048, "a fox"))
        self.assertFalse(any(key.startswith("images.") for key in cond))
        sampler = graph["7"]["inputs"]
        self.assertEqual((sampler["steps"], sampler["cfg"], sampler["sampler_name"], sampler["scheduler"]), (6, 1.0, "euler", "simple"))
        self.assertEqual(sampler["negative"], ["6", 0])
        self.assertEqual(sampler["latent_image"], ["5", 1])
        self.assertEqual(workflows[1]["7"]["inputs"]["seed"], 8)
        # Nothing reads the negative except a ConditioningZeroOut: condcache leaves the H3 node alone.
        self.assertEqual(condcache.wrap(graph)["5"]["class_type"], "MiniMaxH3ImageConditioning")

    def test_references_follow_picture_one(self):
        refs = [{"enabled": True, "image": "a.png", "width": 1000, "height": 750},
                {"enabled": False, "image": "off.png"}, {"enabled": True, "image": "b.png"}]
        req = request(use_references=True, references=refs, match_reference_size=True)
        graph = build_workflows(settings, req)[0][0]
        cond = graph["5"]["inputs"]
        self.assertEqual((cond["width"], cond["height"]), (0, 0))
        self.assertEqual(graph[cond["images.image_1"][0]]["inputs"]["image"], "a.png")
        self.assertEqual(graph[cond["images.image_2"][0]]["inputs"]["image"], "b.png")
        self.assertNotIn("images.image_3", cond)
        # The node scales Picture 1's short edge into 1024..2048 on the 32 px grid.
        self.assertEqual(display_size(req), (1376, 1024))
        fixed = build_workflows(settings, request(use_references=True, references=refs[:1], match_reference_size=False))[0][0]
        self.assertEqual((fixed["5"]["inputs"]["width"], fixed["5"]["inputs"]["height"]), (2048, 2048))

    def test_limits(self):
        request(width=4096, height=4096)
        request(width=5472, height=3072)
        with self.assertRaises(ValidationError):
            request(width=6272, height=6272)
        with self.assertRaises(ValidationError):
            request(enhance=True)
        with self.assertRaises(ValidationError):
            request(speed="turbo4")
        with self.assertRaises(ValidationError):   # Qwen keeps its own 2752 px side limit
            GenerationRequest(model="qwen_image_21", prompt="x", width=3072, height=1024)
        with self.assertRaises(ValidationError):   # Krea 2 still stops at 1536
            GenerationRequest(model="krea2", prompt="x", width=2048, height=1024)

    def test_checkpoint_family(self):
        self.assertEqual(checkpoints.family(W4A8), MODEL_H3_IMAGE)
        self.assertEqual(checkpoints.family("minimax_h3_image_pruned_int8_convrot.safetensors"), MODEL_H3_IMAGE)
        self.assertEqual(checkpoints.family("MiniMax-H3-Image-Pruned-Q4_K_M.gguf"), MODEL_H3_IMAGE)
        self.assertIsNone(checkpoints.family("P-H3-Image_Q4.gguf"))          # Pruna's model: different graph
        self.assertIsNone(checkpoints.family("p_h3_image_bf16.safetensors"))
        self.assertIsNone(checkpoints.family("minimax_h3_ref2va_pruned_w4a8_mixed.safetensors"))   # H3 video
        found = checkpoints.from_object_info({"UNETLoader": {"input": {"required": {"unet_name": [[W4A8, "krea2_turbo_int8_convrot.safetensors"]]}}}})
        self.assertEqual(found[MODEL_H3_IMAGE], [W4A8])
        self.assertEqual(checkpoints.resolve(MODEL_H3_IMAGE, None, found), W4A8)

    def test_follow_size_matches_node(self):
        self.assertEqual(h3image._follow_size(512, 512), (1024, 1024))
        self.assertEqual(h3image._follow_size(4000, 3000), (2720, 2048))


if __name__ == "__main__":
    unittest.main()
