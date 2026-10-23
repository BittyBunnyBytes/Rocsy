"""Rules tab: edit the prompts and the Hermes skill file behind auto-tag / auto-cut.

Every save keeps the previous text in /data/rules-history/<key>/ (last 30) so any version can
be restored. The Hermes skill file is edited in place in the mounted skill folder; its shipped
text is snapshotted once to /data/rules-defaults/ so "Reset to default" works for it too.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import finder
import tagger

SKILL_DIR = Path(os.getenv("HERMES_SKILL_DIR", "/hermes-skill"))
KEEP = 30

# key -> (title, what it does, where it lives, required placeholders)
RULES: dict[str, dict[str, Any]] = {
    "system_prompt": {"title": "Captioning prompt", "group": "Auto-tag",
                      "help": "System prompt the vision model gets for every clip it captions (Datasets → ✨ Auto-tag, and Hermes' tag.py start).",
                      "required": []},
    "user_template": {"title": "Caption request template", "group": "Auto-tag",
                      "help": "The per-clip message sent with the frames. Placeholders: {trigger} {concept} {style} {what} {existing} {file}.",
                      "required": ["{trigger}", "{style}", "{what}"]},
    "cut_prompt": {"title": "Clip scout prompt", "group": "Auto-cut",
                   "help": "System prompt for ✨ Auto-cut in the trimmer (and Hermes' tag.py autocut): how candidate windows are judged. The JSON reply format must stay: frames, same_shot, keep, score, subject, reason.",
                   "required": []},
    "cut_template": {"title": "Clip scout request template", "group": "Auto-cut",
                     "help": "The per-window message. Placeholders: {concept} {index} {count} {start} {end} {nframes}.",
                     "required": ["{nframes}"]},
    "find_prompt": {"title": "Clip finder brief", "group": "Find clips",
                    "help": "What Hermes is told when you press 🔎 Find clips in the Datasets tab: how to search sites, explore unfamiliar ones, judge and propose source videos. Placeholders: {job_id} {dataset} {query} {concept} {sites} {duration} {count} {max_count} {max_steps} {script}. Literal braces must be doubled ({{q}}).",
                    "required": ["{job_id}", "{script}"]},
    "fetch_prompt": {"title": "Link fetch brief", "group": "Find clips",
                     "help": "What Hermes is told when you paste a non-YouTube link into Download & trim: get that one video (age gates, JavaScript players) and download it whole. Placeholders: {job_id} {dataset} {url} {script}. Literal braces must be doubled.",
                     "required": ["{job_id}", "{url}", "{script}"]},
    "skill": {"title": "Hermes skill (SKILL.md)", "group": "Hermes",
              "help": "The dataset-tagging skill Hermes reads: when to use it and how to drive the Trainer. Keep the --- front-matter block at the top (name, description).",
              "required": ["name:", "description:"]},
}


class RuleBody(BaseModel):
    text: str = Field(min_length=1, max_length=60000)


def build_router(prefix: str, data_dir: Path, give_back: Callable[[Path], None]) -> APIRouter:
    router = APIRouter(prefix=prefix)
    hist_root = data_dir / "rules-history"
    defaults_dir = data_dir / "rules-defaults"
    settings_file = data_dir / "tagging.json"
    skill_file = SKILL_DIR / "SKILL.md"

    # Snapshot the shipped skill once, so "Reset to default" can restore it.
    with suppress(OSError):
        defaults_dir.mkdir(parents=True, exist_ok=True)
        if skill_file.is_file() and not (defaults_dir / "SKILL.md").is_file():
            shutil.copy2(skill_file, defaults_dir / "SKILL.md")

    def default_of(key: str) -> str:
        if key == "skill":
            with suppress(OSError):
                return (defaults_dir / "SKILL.md").read_text(encoding="utf-8")
            return ""
        return {"system_prompt": tagger.DEFAULT_SYSTEM_PROMPT, "user_template": tagger.DEFAULT_USER_TEMPLATE,
                "cut_prompt": tagger.DEFAULT_CUT_PROMPT, "cut_template": tagger.DEFAULT_CUT_TEMPLATE,
                "find_prompt": finder.DEFAULT_FIND_PROMPT, "fetch_prompt": finder.DEFAULT_FETCH_PROMPT}[key]

    def saved_settings() -> dict[str, Any]:
        with suppress(OSError, ValueError):
            return json.loads(settings_file.read_text(encoding="utf-8"))
        return {}

    def current_of(key: str) -> str:
        if key == "skill":
            with suppress(OSError):
                return skill_file.read_text(encoding="utf-8")
            return ""
        return saved_settings().get(key) or default_of(key)

    def history_of(key: str) -> list[dict[str, Any]]:
        folder = hist_root / key
        if not folder.is_dir():
            return []
        out = []
        for f in sorted(folder.glob("*.txt"), reverse=True):
            with suppress(OSError, ValueError):
                out.append({"id": f.stem, "saved_at": float(f.stem.split("-")[0]), "chars": f.stat().st_size})
        return out

    def push_history(key: str, text: str) -> None:
        folder = hist_root / key
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{time.time():.3f}-{os.getpid()}.txt").write_text(text, encoding="utf-8")
        for old in sorted(folder.glob("*.txt"))[:-KEEP]:
            old.unlink(missing_ok=True)

    def write(key: str, text: str) -> None:
        before = current_of(key)
        if before.strip() and before != text:
            push_history(key, before)
        if key == "skill":
            if not SKILL_DIR.is_dir():
                raise HTTPException(503, "The Hermes skill folder is not mounted in the Trainer.")
            tmp = skill_file.with_suffix(".md.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(skill_file)
            give_back(skill_file)
            return
        data = saved_settings()
        if text == default_of(key):
            data.pop(key, None)       # back on the built-in default
        else:
            data[key] = text
        settings_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def item(key: str) -> dict[str, Any]:
        cur = current_of(key)
        return {"key": key, **RULES[key], "text": cur, "default": default_of(key),
                "is_default": cur == default_of(key), "history": history_of(key),
                "path": "~/.hermes/skills/media/dataset-tagging/SKILL.md" if key == "skill" else "Trainer data/tagging.json"}

    def check(key: str, text: str) -> None:
        missing = [r for r in RULES[key]["required"] if r not in text]
        if missing:
            raise HTTPException(422, f"{RULES[key]['title']} must contain: {', '.join(missing)}")
        if key == "skill" and not text.lstrip().startswith("---"):
            raise HTTPException(422, "The skill file must start with its --- front-matter block (name, description).")

    @router.get("/api/rules")
    async def list_rules() -> dict[str, Any]:
        return {"rules": [item(k) for k in RULES]}

    @router.put("/api/rules/{key}")
    async def put_rule(key: str, body: RuleBody) -> dict[str, Any]:
        if key not in RULES:
            raise HTTPException(404, "Unknown rule.")
        text = body.text.replace("\r\n", "\n")
        check(key, text)
        write(key, text)
        return item(key)

    @router.post("/api/rules/{key}/reset")
    async def reset_rule(key: str) -> dict[str, Any]:
        if key not in RULES:
            raise HTTPException(404, "Unknown rule.")
        d = default_of(key)
        if not d:
            raise HTTPException(409, "No default is stored for this rule.")
        write(key, d)
        return item(key)

    @router.get("/api/rules/{key}/history/{version}")
    async def get_version(key: str, version: str) -> dict[str, Any]:
        if key not in RULES or not all(c.isdigit() or c in ".-" for c in version):
            raise HTTPException(404, "Unknown version.")
        f = hist_root / key / f"{version}.txt"
        if not f.is_file():
            raise HTTPException(404, "Unknown version.")
        return {"text": f.read_text(encoding="utf-8")}

    return router
