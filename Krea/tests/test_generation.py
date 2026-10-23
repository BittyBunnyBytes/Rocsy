from __future__ import annotations

import unittest

from backend.checkpoints import from_object_info, resolve
from backend.config import settings
from backend.generation import build_enhanced_workflows, build_workflows
from backend.models import ANIMA_LORA_SLOTS, ANIMA_VERSIONS, MOSS_LORA_SLOTS, GenerationRequest


class GenerationWorkflowTests(unittest.TestCase):
    def test_installed_checkpoints_select_workflow_model(self) -> None:
        info = {"UNETLoader": {"input": {"required": {"unet_name": [[
            "krea2_turbo_int8_convrot.safetensors",
            "lustifyNSFWCheckpoint_v10Krea2_fp8.safetensors",
            "custom/Krea-2-raw-fp8.safetensors",
            "qwen_image_2.1_int8_convrot.safetensors",
            "Qwen-Image-2.1-fp8.safetensors",
            "qwen3.5_9b_pe_t2i.safetensors",
            "anima_aestheticV11.safetensors",
        ]]}}}}
        found = from_object_info(info)
        self.assertEqual(len(found["krea2"]), 3)
        self.assertEqual(len(found["qwen_image_21"]), 2)
        for model, checkpoint in (("krea2", "lustifyNSFWCheckpoint_v10Krea2_fp8.safetensors"),
                                  ("krea2", "custom/Krea-2-raw-fp8.safetensors"),
                                  ("qwen_image_21", "Qwen-Image-2.1-fp8.safetensors")):
            with self.subTest(checkpoint=checkpoint):
                selected = resolve(model, checkpoint, found)
                workflow = build_workflows(settings, GenerationRequest(model=model, checkpoint=selected, prompt="a fox"))[0][0]
                self.assertEqual(workflow["1"]["inputs"]["unet_name"], checkpoint)
        with self.assertRaises(ValueError):
            resolve("krea2", "qwen_image_2.1_int8_convrot.safetensors", found)

    def test_moss_int8_checkpoint_and_workflow(self) -> None:
        info = {
            "UNETLoader": {"input": {"required": {"unet_name": [["moss_image_sft_int8_convrot.safetensors"]]}}},
            "MOSSImageUNETLoader": {"input": {"required": {"unet_name": ["COMBO", {"options": [
                "moss_image_sft_int8_convrot.safetensors", "moss_image_base_int8_convrot.safetensors"]}]}}},
        }
        found = from_object_info(info)
        self.assertEqual(len(found["moss"]), 2)
        self.assertEqual(from_object_info({"UNETLoader": info["UNETLoader"]})["moss"], [])
        checkpoint = resolve("moss", None, found)
        request = GenerationRequest(model="moss", checkpoint=checkpoint, prompt="a fox", seed=42,
                                    moss_steps=50, moss_cfg=3, negative_prompt="blurry")
        workflows, seed, stage = build_workflows(settings, request)
        graph = workflows[0]
        self.assertEqual((seed, stage), (42, "render"))
        self.assertEqual(graph["1"]["class_type"], "MOSSImageUNETLoader")
        self.assertEqual(graph["1"]["inputs"]["unet_name"], checkpoint)
        self.assertEqual(graph["2"]["class_type"], "MOSSImageCLIPLoader")
        self.assertEqual(graph["5"]["inputs"]["text"], "blurry")
        self.assertEqual((graph["16"]["inputs"]["steps"], graph["16"]["inputs"]["cfg"]), (50, 3))
        self.assertEqual(graph["18"]["class_type"], "SaveImage")
        with self.assertRaises(ValueError):
            GenerationRequest(model="moss", prompt="a fox", use_references=True)

    def test_moss_and_anima_loras_feed_the_sampler_model(self) -> None:
        MOSS_LORA_SLOTS.append("moss-test.safetensors")
        try:
            moss = GenerationRequest(model="moss", prompt="a fox", loras=[{"enabled": True, "strength": 0.7}])
            graph = build_workflows(settings, moss)[0][0]
            self.assertEqual(graph["1000"]["class_type"], "LoraLoaderModelOnly")
            self.assertEqual(graph["1000"]["inputs"]["lora_name"], "moss-test.safetensors")
            self.assertEqual(graph["15"]["inputs"]["model"], ["1000", 0])
        finally:
            MOSS_LORA_SLOTS.pop()
        # A fixture slot list, so the test doesn't depend on this machine's catalogue.
        installed = list(ANIMA_LORA_SLOTS)
        ANIMA_LORA_SLOTS[:] = ["anima-test.safetensors"]
        try:
            for version in ("base", "v38"):
                with self.subTest(version=version):
                    anima = GenerationRequest(model="anima", anima_version=version, prompt="a fox",
                                              loras=[{"enabled": True, "strength": 0.8}])
                    graph = build_workflows(settings, anima)[0][0]
                    self.assertEqual(graph["1000"]["inputs"]["lora_name"], "anima-test.safetensors")
                    consumer = "6"
                    self.assertEqual(graph[consumer]["inputs"]["model"], ["1000", 0])
        finally:
            ANIMA_LORA_SLOTS[:] = installed

    def test_moss_few_step_modes_use_adapter_sampler_and_bf16(self) -> None:
        for speed, adapter, steps in (("dmd8", "lodestones-8step", 8),
                                      ("dmad4", "rapid-moss-4step", 4)):
            with self.subTest(speed=speed):
                request = GenerationRequest(model="moss", prompt="a fox", speed=speed, seed=42)
                workflow = build_workflows(settings, request)[0][0]
                self.assertEqual(workflow["1"]["inputs"]["unet_name"], "moss_image_sft_bf16.safetensors")
                self.assertEqual(workflow["20"]["inputs"]["adapter"], adapter)
                self.assertEqual(workflow["20"]["inputs"]["steps"], steps)
                self.assertEqual(workflow["21"]["class_type"], "BasicGuider")
                self.assertEqual(workflow["23"]["class_type"], "SamplerCustomAdvanced")
                self.assertEqual(workflow["23"]["inputs"]["sampler"], ["20", 1])
                self.assertNotIn("16", workflow)
        with self.assertRaises(ValueError):
            GenerationRequest(model="anima", prompt="a fox", speed="dmd8")

    def test_gguf_checkpoints_use_gguf_loader(self) -> None:
        info = {"UNETLoader": {"input": {"required": {"unet_name": [["qwen_image_2.1_int8_convrot.safetensors"]]}}},
                "UnetLoaderGGUF": {"input": {"required": {"unet_name": [[
                    "Qwen-Image-2.1-Turbo-Q8_0.gguf", "krea2-Q8_0.gguf",
                    "Qwen-Image-2.1-Turbo-Abliterated-Uncensored-BF16.gguf"]]}}}}
        found = from_object_info(info)
        self.assertIn("Qwen-Image-2.1-Turbo-Q8_0.gguf", found["qwen_image_21"])
        # The abliterated file is a Qwen3-VL text encoder, not a denoiser.
        self.assertNotIn("Qwen-Image-2.1-Turbo-Abliterated-Uncensored-BF16.gguf", found["qwen_image_21"])
        self.assertIn("krea2-Q8_0.gguf", found["krea2"])
        for model, checkpoint in (("qwen_image_21", "Qwen-Image-2.1-Turbo-Q8_0.gguf"),
                                  ("krea2", "krea2-Q8_0.gguf")):
            with self.subTest(checkpoint=checkpoint):
                workflow = build_workflows(settings, GenerationRequest(model=model, checkpoint=resolve(model, checkpoint, found), prompt="a fox"))[0][0]
                loader = next(node for node in workflow.values() if node["inputs"].get("unet_name") == checkpoint)
                self.assertEqual(loader["class_type"], "UnetLoaderGGUF")
                self.assertNotIn("weight_dtype", loader["inputs"])

    def test_qwen_turbo_checkpoint_uses_fixed_8_step_schedule(self) -> None:
        for speed in ("standard", "viggle"):
            with self.subTest(speed=speed):
                request = GenerationRequest(model="qwen_image_21", checkpoint="qwen_image_2.1_turbo_int8_convrot.safetensors",
                                            prompt="a fox", seed=7, steps=25, cfg=4, speed=speed)
                workflow = build_workflows(settings, request)[0][0]
                self.assertEqual(workflow["7"]["class_type"], "SamplerCustomAdvanced")
                self.assertEqual(workflow["72"]["class_type"], "BasicGuider")
                self.assertEqual(workflow["72"]["inputs"]["model"], ["4", 0])  # no Viggle LoRA on Turbo
                self.assertNotIn("70", workflow)
                self.assertEqual(workflow["71"]["inputs"]["noise_seed"], 7)
                self.assertEqual(len(workflow["74"]["inputs"]["sigmas"].split(",")), 9)
        base = build_workflows(settings, GenerationRequest(model="qwen_image_21", checkpoint="qwen_image_2.1_int8_convrot.safetensors", prompt="a fox"))[0][0]
        self.assertEqual(base["7"]["class_type"], "KSampler")

    def test_qwen_text_encoder_choice(self) -> None:
        for choice, encoder in (("standard", "qwen3vl_8b_int8_convrot.safetensors"), ("abliterated", "qwen3vl_8b_abliterated_bf16.safetensors")):
            with self.subTest(choice=choice):
                request = GenerationRequest(model="qwen_image_21", checkpoint="qwen_image_2.1_int8_convrot.safetensors", prompt="a fox", text_encoder=choice)
                node = build_workflows(settings, request)[0][0]["2"]
                self.assertEqual(node["class_type"], "CLIPLoader")
                self.assertEqual(node["inputs"]["clip_name"], encoder)
                self.assertEqual(node["inputs"]["type"], "qwen_image")

    def test_anima_versions_build_distinct_installed_checkpoints(self) -> None:
        for version, (filename, _) in ANIMA_VERSIONS.items():
            with self.subTest(version=version):
                request = GenerationRequest(model="anima", anima_version=version, prompt="a fox", seed=42)
                workflows, seed, stage = build_workflows(settings, request)
                workflow = workflows[0]
                self.assertEqual((seed, stage), (42, "render"))
                self.assertEqual(workflow["1"]["inputs"].get("model_name", workflow["1"]["inputs"].get("unet_name")), filename)
                self.assertEqual(workflow["30"]["class_type"], "SaveImage")
                self.assertEqual(workflow["9"]["inputs"]["noise_seed"] if version == "v38" else workflow["6"]["inputs"]["seed"], 42)

    def test_anima_rejects_unsupported_reference(self) -> None:
        with self.assertRaises(ValueError):
            GenerationRequest(model="anima", prompt="a fox", use_references=True)

    def test_krea_family_references_survive_prompt_enhancement(self) -> None:
        for model in ("krea2", "lustify"):
            with self.subTest(model=model):
                self._assert_references_survive_prompt_enhancement(model)

    def _assert_references_survive_prompt_enhancement(self, model: str) -> None:
        request = GenerationRequest(
        model=model,
        prompt="a portrait",
        seed=42,
        use_references=True,
        references=[
            {"enabled": True, "image": "reference.png", "strength": 0.65, "width": 1200, "height": 800},
            {"enabled": False, "strength": 0.4},
        ],
        prompt_enhancer=True,
    )

        enhancer_workflows, seed, stage = build_workflows(settings, request)
        self.assertEqual(stage, "enhance")
        self.assertEqual(seed, 42)
        self.assertTrue(enhancer_workflows[0]["1"]["inputs"]["clip_name"].endswith("pe_i2i.int8_convrot.safetensors"))
        self.assertEqual(enhancer_workflows[0]["101"]["inputs"]["image"], "reference.png")

        workflows, prompt, size = build_enhanced_workflows(
            settings,
            request,
            seed,
            '{"rewritten_prompt":"an improved portrait","wh_ratio":"3:2","ratio_follow":""}',
        )

        workflow = workflows[0]
        self.assertEqual(prompt, "an improved portrait")
        self.assertEqual(size, (1248, 832))
        self.assertEqual(workflow["5"]["inputs"]["text"], prompt)
        # Official Krea 2 reference graph: style-reference LoRA + EditPlus + index_timestep_zero.
        self.assertEqual(workflow["50"]["inputs"]["lora_name"], "krea2_style_reference.safetensors")
        self.assertEqual(workflow["50"]["inputs"]["strength_model"], 0.65)
        self.assertEqual(workflow["53"]["inputs"]["image"], "reference.png")
        self.assertNotIn("54", workflow)
        self.assertEqual(workflow["51"]["class_type"], "TextEncodeQwenImageEditPlus")
        self.assertEqual(workflow["51"]["inputs"]["prompt"], prompt)
        self.assertEqual(workflow["51"]["inputs"]["image1"], ["53", 0])
        self.assertNotIn("image2", workflow["51"]["inputs"])
        self.assertEqual(workflow["52"]["inputs"]["reference_latents_method"], "index_timestep_zero")
        self.assertEqual(workflow["8"]["inputs"]["positive"], ["52", 0])
        self.assertEqual(workflow["8"]["inputs"]["model"], ["56", 0])
        self.assertEqual(workflow["56"]["class_type"], "ModelSamplingFlux")
        self.assertEqual(workflow["56"]["inputs"]["model"], ["50", 0])
        self.assertEqual((workflow["56"]["inputs"]["width"], workflow["56"]["inputs"]["height"]), (1248, 832))
        self.assertTrue(all(node not in workflow for node in ("12", "14", "15", "16", "33", "42")))


    def test_krea_family_reference_switch_disables_every_slot(self) -> None:
        for model in ("krea2", "lustify"):
            with self.subTest(model=model):
                self._assert_reference_switch_disables_every_slot(model)

    def _assert_reference_switch_disables_every_slot(self, model: str) -> None:
        request = GenerationRequest(
        model=model,
        prompt="a portrait",
        use_references=False,
        references=[{"enabled": True, "image": "reference.png", "strength": 0.65}],
    )

        workflows, _, stage = build_workflows(settings, request)
        workflow = workflows[0]
        self.assertEqual(stage, "render")
        self.assertEqual(workflow["8"]["inputs"]["positive"], ["5", 0])
        self.assertEqual(workflow["8"]["inputs"]["model"], ["4", 0])
        self.assertFalse(any(node.get("class_type") == "LoadImage" for node in workflow.values()))

    def test_krea_legacy_fourth_slot_is_dropped(self) -> None:
        request = GenerationRequest(model="krea2", prompt="a portrait", use_references=True, references=[
            {"enabled": True, "image": "a.png", "strength": 0.8}, {"enabled": True, "image": "b.png", "strength": 1.1},
            {"enabled": False}, {"enabled": False, "strength": 0.45},
        ])
        workflow = build_workflows(settings, request)[0][0]
        self.assertEqual(len(request.references), 3)
        self.assertEqual(workflow["50"]["inputs"]["strength_model"], 1.1)
        self.assertEqual(workflow["51"]["inputs"]["image2"], ["54", 0])
        with self.assertRaises(ValueError):
            GenerationRequest(model="krea2", prompt="x", use_references=True,
                              references=[{"enabled": True, "image": f"{i}.png"} for i in range(4)])


if __name__ == "__main__":
    unittest.main()
