import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import workflow_models as wm


GRAPH = {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "dit.safetensors"}},
    "2": {"class_type": "VAELoader", "inputs": {"vae_name": "video_vae.safetensors"}},
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "audio_vae.safetensors"}, "_meta": {"title": "Audio VAE"}},
    "4": {"class_type": "LoraLoaderModelOnly", "inputs": {"lora_name": "user.safetensors", "model": ["1", 0]}},
    "5": {"class_type": "LoadImage", "inputs": {"image": "ref.png"}},
    "6": {"class_type": "KSampler", "inputs": {"model": ["4", 0]}},
}
OBJECT_INFO = {
    "VAELoader": {"input": {"required": {"vae_name": [["video_vae.safetensors", "audio_vae.safetensors", "fast_vae.safetensors"]]}}},
    "CLIPLoader": {"input": {"required": {"clip_name": ["COMBO", {"options": ["te.safetensors", "te_alt.safetensors"]}]}}},
}


def transport(online=True):
    def handler(request):
        if not online:
            raise httpx.ConnectError("down")
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={name: OBJECT_INFO[name]} if name in OBJECT_INFO else {})
    return httpx.MockTransport(handler)


class WorkflowModelsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "workflow-models.json"
        self.seeds = {"video": {"label": "Video", "slots": [
            wm.Slot("VAELoader", "vae_name", "video_vae.safetensors", "Video VAE"),
            wm.Slot("CLIPLoader", "clip_name", "te.safetensors")]}}
        real = httpx.AsyncClient
        self.patch = patch.object(wm.httpx, "AsyncClient", lambda **kw: real(transport=transport(self.online), **kw))
        self.online = True
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def store(self):
        return wm.WorkflowModels(self.path, "http://comfy", self.seeds, aliases={"legacy": "video"})

    def test_slots_from_graph_skips_user_picked_inputs_and_non_loaders(self):
        slots = wm.slots_from_graph(GRAPH, wm.LEARN_SKIP_INPUTS)
        self.assertEqual([(s.class_type, s.default, s.label) for s in slots],
                         [("VAELoader", "video_vae.safetensors", ""), ("VAELoader", "audio_vae.safetensors", "Audio VAE")])

    async def test_override_applies_only_to_default_value_and_persists(self):
        store = self.store()
        slot_id = "VAELoader.vae_name:video_vae.safetensors"
        await store.set("video", slot_id, "fast_vae.safetensors")
        graph = json.loads(json.dumps(GRAPH))
        changes = self.store().apply("legacy", graph)  # reloaded from disk, alias resolved
        self.assertEqual(graph["2"]["inputs"]["vae_name"], "fast_vae.safetensors")
        self.assertEqual(graph["3"]["inputs"]["vae_name"], "audio_vae.safetensors")
        self.assertEqual(changes, [{"node": "2", "input": "vae_name", "from": "video_vae.safetensors", "to": "fast_vae.safetensors"}])
        # The audio VAE was learned from the graph (before the swap) and is now a slot.
        ids = [slot.id for slot in self.store().slots("video")]
        self.assertIn("VAELoader.vae_name:audio_vae.safetensors", ids)
        self.assertNotIn("UNETLoader.unet_name:dit.safetensors", ids)

    async def test_set_validates_against_comfy_choices(self):
        store = self.store()
        with self.assertRaises(ValueError):
            await store.set("video", "VAELoader.vae_name:video_vae.safetensors", "nope.safetensors")
        with self.assertRaises(ValueError):
            await store.set("video", "missing-slot", "fast_vae.safetensors")
        await store.set("video", "CLIPLoader.clip_name:te.safetensors", "te_alt.safetensors")  # COMBO form
        self.online = False
        store._choices.clear()  # file lists are cached for 30 s
        with self.assertRaises(ValueError):
            await store.set("video", "VAELoader.vae_name:video_vae.safetensors", "fast_vae.safetensors")
        # Going back to default never needs ComfyUI.
        await store.set("video", "CLIPLoader.clip_name:te.safetensors", None)
        self.assertEqual(store.overrides("video"), {})

    async def test_state_and_reset(self):
        store = self.store()
        await store.set("video", "VAELoader.vae_name:video_vae.safetensors", "fast_vae.safetensors")
        state = await store.state("legacy")
        self.assertTrue(state["online"])
        self.assertEqual(state["current"], "video")
        row = state["workflows"][0]["slots"][0]
        self.assertEqual((row["label"], row["override"], row["default_missing"]), ("Video VAE", "fast_vae.safetensors", False))
        self.assertEqual(state["workflows"][0]["overridden"], 1)
        store.reset("video")
        self.assertEqual(self.store().overrides("video"), {})

    def test_unknown_model_and_corrupt_store_never_block_renders(self):
        self.path.write_text("{not json")
        graph = json.loads(json.dumps(GRAPH))
        self.assertEqual(self.store().apply("other", graph), [])
        self.assertEqual(self.store().apply("video", graph), [])
        self.assertEqual(graph["2"]["inputs"]["vae_name"], "video_vae.safetensors")


    async def test_missing_files_lists_unlisted_files_and_unknown_nodes(self):
        graph = json.loads(json.dumps(GRAPH))
        graph["2"]["inputs"]["vae_name"] = "gone_vae.safetensors"
        graph["7"] = {"class_type": "CLIPLoader", "inputs": {"clip_name": "te.safetensors"}}
        missing = await wm.missing_files("http://comfy", [graph])
        # UNETLoader / LoraLoaderModelOnly aren't in this fake ComfyUI: reported as nodes, their files unchecked.
        self.assertEqual(missing["nodes"], ["LoraLoaderModelOnly", "UNETLoader"])
        self.assertEqual(missing["files"], [{"name": "gone_vae.safetensors", "folder": "vae", "class_type": "VAELoader",
                                             "input": "vae_name", "label": "VAE"}])
        self.assertIn("gone_vae.safetensors", wm.missing_message(missing))

    async def test_missing_files_is_empty_when_comfy_is_offline_or_nothing_is_missing(self):
        graph = {"1": {"class_type": "VAELoader", "inputs": {"vae_name": "video_vae.safetensors"}}}
        self.assertEqual(await wm.missing_files("http://comfy", [graph]), {"files": [], "nodes": []})
        self.online = False
        graph["1"]["inputs"]["vae_name"] = "gone.safetensors"
        self.assertEqual(await wm.missing_files("http://comfy", [graph]), {"files": [], "nodes": []})


if __name__ == "__main__":
    unittest.main()
