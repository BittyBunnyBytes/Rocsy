import asyncio
import hashlib
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


def pin(root: Path, name: str, content: bytes = b"x", alias: str = "", note: str = "", pinned="true"):
    (root / name).write_bytes(content)
    return hashlib.sha256(name.encode()).hexdigest()[:20], {"path": name, "alias": alias, "note": note, "pinned": pinned}


class RagBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "uploads"; self.root.mkdir()
        (base / "data").mkdir()
        self.index = base / "data" / "upload-cache.json"
        os.environ.update(FRAMEWORK_RAG_ROOT=str(self.root), FRAMEWORK_RAG_INDEX=str(self.index), FRAMEWORK_DATA_DIR=self.tmp.name)
        import rag
        self.rag = importlib.reload(rag)
        pins = dict([pin(self.root, "lore.md", b"The moon is cheese.", "Lore", "world notes"),
                     pin(self.root, "a1.png", b"png", "Bitty", "the mascot"),
                     pin(self.root, "voice.wav", b"wav"),
                     pin(self.root, "b.png", b"png", "B"),
                     pin(self.root, "hidden.png", b"png", "Hidden", pinned=False)])
        self.ids = {entry["path"]: key for key, entry in pins.items()}
        self.index.write_text(json.dumps(pins))
        (base / "data" / "upload-cache-groups.json").write_text(json.dumps(
            {"g": {"name": "Cast", "files": [self.ids["a1.png"], self.ids["b.png"], "gone"], "note": "main cast"}}))


class ResolveTest(RagBase):
    def test_catalog_hides_paths_and_unpinned(self):
        catalog = self.rag.catalog()
        self.assertEqual([f["alias"] or f["name"] for f in catalog["files"]], ["B", "Bitty", "Lore", "voice.wav"])
        self.assertNotIn("path", catalog["files"][0])
        self.assertEqual(catalog["groups"], [{"name": "Cast", "count": 2}])

    def test_document_and_media(self):
        out = self.rag.resolve("Use @{Lore} and @bitty. Ask me@Lore no")
        self.assertEqual(out["docs"][0]["text"], "The moon is cheese.")
        self.assertEqual([m["name"] for m in out["media"]], ["a1.png"])
        self.assertEqual(out["warnings"], [])
        context = self.rag.context(out, ["line"])
        self.assertIn("The moon is cheese.", context)
        self.assertIn("world notes", context)

    def test_bare_at_word_stays_text_but_braces_warn(self):
        out = self.rag.resolve("by @artist and @{Nope} and @{Hidden}")
        self.assertEqual(out["calls"], [])
        self.assertEqual(len(out["warnings"]), 2)
        self.assertEqual(self.rag.resolve("no calls at all")["calls"], [])

    def test_groups(self):
        self.assertEqual({m["name"] for m in self.rag.resolve("@{Cast}")["media"]}, {"a1.png", "b.png"})
        self.assertEqual(len(self.rag.resolve("@Cast random 1")["media"]), 1)
        out = self.rag.resolve("@{Cast} random 9")
        self.assertEqual(len(out["media"]), 2)
        self.assertTrue(out["warnings"])
        self.assertEqual(out["group_notes"][0]["note"], "main cast")

    def test_limits_and_tampered_index(self):
        key, entry = pin(self.root, "big.txt", b"y" * 30000, "Big")
        data = json.loads(self.index.read_text())
        data[key] = entry
        bad = {"path": "../data/upload-cache.json", "alias": "Evil", "pinned": "true"}
        data[hashlib.sha256(bad["path"].encode()).hexdigest()[:20]] = bad
        data["0" * 20] = {"path": "lore.md", "alias": "Wrong", "pinned": "true"}
        self.index.write_text(json.dumps(data))
        out = self.rag.resolve("@{Big} @{Evil} @{Wrong}")
        self.assertEqual(len(out["docs"][0]["text"]), self.rag.DOC_LIMIT)
        self.assertEqual(len(out["docs"]), 1)
        self.assertEqual(len(out["warnings"]), 3)

    def test_missing_index_is_empty(self):
        self.index.unlink()
        self.assertEqual(self.rag.catalog(), {"files": [], "groups": []})
        self.assertEqual(self.rag.resolve("@{Lore}")["docs"], [])


class AgenticRagTest(RagBase):
    def agentic(self):
        import agentic
        agentic = importlib.reload(agentic)
        agentic.load()
        return agentic

    def row(self, studio, settings, request="Use @{Lore} with @{Bitty}"):
        return {"id": "x", "studio": studio, "request": request, "guide": "", "settings": settings, "log": [], "log_revision": 0, "updated": 0}

    def test_image_picture_becomes_reference_and_doc_reaches_plan(self):
        agentic = self.agentic()
        row = self.row("image", {"model": "krea2", "use_references": False, "references": [{"enabled": False, "image": None}]})
        with patch.object(agentic, "upload_reference", AsyncMock(return_value="stored.png")) as upload, patch.object(agentic, "save_soon"):
            asyncio.run(agentic.apply_rag(row))
            asyncio.run(agentic.apply_rag(row))
        upload.assert_awaited_once()
        self.assertEqual(row["settings"]["references"], [{"enabled": True, "strength": 1.0, "image": "stored.png"}])
        self.assertTrue(row["settings"]["use_references"])
        text = agentic.plan_input(row)
        self.assertIn("The moon is cheese.", text)
        self.assertIn("reference image 1", text)
        self.assertNotIn("rag_context", agentic.summary(row))

    def test_user_references_kept_and_slot_limit(self):
        agentic = self.agentic()
        own = [{"enabled": True, "image": f"{n}.png"} for n in "123"]
        row = self.row("image", {"model": "krea2", "use_references": True, "references": own})
        with patch.object(agentic, "upload_reference", AsyncMock()) as upload, patch.object(agentic, "save_soon"):
            asyncio.run(agentic.apply_rag(row))
        upload.assert_not_awaited()
        self.assertEqual(row["settings"]["references"], own)
        self.assertTrue(any(e["kind"] == "warn" for e in row["log"]))

    def test_music_notes_media_without_using_it(self):
        agentic = self.agentic()
        row = self.row("music", {"model": "yue2"})
        with patch.object(agentic, "save_soon"):
            asyncio.run(agentic.apply_rag(row))
        self.assertNotIn("references", row["settings"])
        self.assertIn("can't use it as an input", row["rag_context"])

    def test_preview_shows_rag(self):
        agentic = self.agentic()
        preview = asyncio.run(agentic.preview("voice", agentic.PreviewRequest(request="Read @{Lore}", settings={"engine": "qwen3"})))
        self.assertIn("The moon is cheese.", preview["messages"][1]["content"])


if __name__ == "__main__":
    unittest.main()
