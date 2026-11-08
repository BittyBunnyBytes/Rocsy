"""@ calls to the RAG library (gateway RAG & Cache pins) for every Agentic mode.

Same syntax as the Master Agent's composer: `@{Name}` or `@Name` calls a pinned file by its
alias or file name; a group takes `all` (the default) or `random N`. A bare `@word` that names
nothing stays plain text, because prompts use @ for other things (Anima's @artist, Video's @First).
Pins are read fresh on every call; the gateway owns them, so these mounts are read-only.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import random
import re
from pathlib import Path
from typing import Any

ROOT = Path(os.getenv("FRAMEWORK_RAG_ROOT", "/pinned-uploads"))
INDEX = Path(os.getenv("FRAMEWORK_RAG_INDEX", "/pinned-cache-data/upload-cache.json"))
DOC_LIMIT = 20000      # characters of one document, the Master Agent's chat limit
TOTAL_LIMIT = 40000    # characters of all documents in one run
MAX_FILES = 20         # files per run, the Master Agent's attachment limit
MENTION = re.compile(r"(?<![\w@])@(?:\{([^{}\n]{1,200})\}|([\w.-]+))(\s+(?:all|random\s+\d+)\b)?", re.I)
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".json", ".csv", ".log", ".py", ".js", ".jsx", ".ts", ".tsx", ".css",
                 ".html", ".htm", ".yaml", ".yml", ".xml", ".srt", ".vtt", ".sh", ".toml", ".ini"}


def _kind(path: Path, mime: str) -> str:
    if path.suffix.lower() in TEXT_SUFFIXES or mime.startswith("text/"):
        return "text"
    head = mime.split("/")[0]
    return head if head in {"image", "video", "audio"} else "file"


def _pinned_file(key: str, entry: Any) -> dict[str, Any] | None:
    """One pin as a file record; anything that doesn't check out is treated as not pinned."""
    if not isinstance(entry, dict) or entry.get("pinned") not in (True, "true"):
        return None
    relative = str(entry.get("path") or "")
    part = Path(relative)
    if not relative or part.is_absolute() or ".." in part.parts:
        return None
    if hashlib.sha256(relative.encode()).hexdigest()[:20] != key:
        return None
    try:
        root = ROOT.resolve(strict=True)
        source = root / part
        path = source.resolve(strict=True)
        if not path.is_relative_to(root) or source.is_symlink() or not path.is_file():
            return None
        size = path.stat().st_size
    except OSError:
        return None
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    alias = entry.get("alias") if isinstance(entry.get("alias"), str) else ""
    note = entry.get("note") if isinstance(entry.get("note"), str) else ""
    return {"id": key, "name": path.name, "alias": alias.strip(), "note": note.strip(), "mime": mime,
            "kind": _kind(path, mime), "size": size, "path": relative}


def _library() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """(files by id, groups). A missing or broken index is an empty library."""
    try:
        pins = json.loads(INDEX.read_text())
    except (OSError, ValueError):
        return {}, []
    if not isinstance(pins, dict):
        return {}, []
    files = {key: item for key, entry in pins.items() if (item := _pinned_file(key, entry))}
    try:
        raw = json.loads(INDEX.with_name("upload-cache-groups.json").read_text())
    except (OSError, ValueError):
        raw = {}
    groups = []
    for group in raw.values() if isinstance(raw, dict) else []:
        if isinstance(group, dict) and isinstance(group.get("name"), str) and isinstance(group.get("files"), list):
            members = [file_id for file_id in group["files"] if file_id in files]
            note = group.get("note") if isinstance(group.get("note"), str) else ""
            groups.append({"name": group["name"].strip(), "files": members, "note": note.strip()})
    return files, groups


def catalog() -> dict[str, Any]:
    """What the @ menu offers. No paths: the browser only ever needs names."""
    files, groups = _library()
    public = ("name", "alias", "kind", "size")
    return {"files": sorted(({key: item[key] for key in public} for item in files.values()),
                            key=lambda item: (item["alias"] or item["name"]).casefold()),
            "groups": sorted(({"name": group["name"], "count": len(group["files"])} for group in groups if group["name"]),
                             key=lambda group: group["name"].casefold())}


def _read_doc(item: dict[str, Any]) -> str:
    with (ROOT / item["path"]).open(encoding="utf-8", errors="replace") as handle:
        return handle.read(DOC_LIMIT + 1)


def resolve(text: str, chooser: random.Random | None = None) -> dict[str, Any]:
    """Every @ call in `text`: its documents' text, its media files, and what couldn't be used."""
    result: dict[str, Any] = {"calls": [], "docs": [], "media": [], "group_notes": [], "warnings": []}
    if "@" not in text:
        return result
    matches = list(MENTION.finditer(text))
    if not matches:
        return result
    files, groups = _library()
    chooser = chooser or random.SystemRandom()
    by_name: dict[str, dict[str, Any]] = {}
    for item in files.values():        # an alias beats another file's name
        by_name.setdefault(item["name"].casefold(), item)
    for item in files.values():
        if item["alias"]:
            by_name[item["alias"].casefold()] = item
    by_group = {group["name"].casefold(): group for group in groups if group["name"]}
    chosen: dict[str, dict[str, Any]] = {}
    labels: dict[str, str] = {}

    for match in matches:
        braced, bare, suffix = match.group(1), match.group(2), (match.group(3) or "").strip().lower()
        name = (braced or bare).strip()
        if bare:   # "@Bitty." at the end of a sentence
            name = name.rstrip(".-") or name
        label = f"@{{{name}}}"
        group = by_group.get(name.casefold())
        item = None if group else by_name.get(name.casefold())
        if group:
            members = [files[file_id] for file_id in group["files"]]
            if not members:
                result["warnings"].append(f"{label} is a group with no available files")
                continue
            count = len(members)
            if suffix.startswith("random"):
                count = int(suffix.split()[-1])
                if not 1 <= count <= len(members):
                    result["warnings"].append(f"{label} random {count}: the group has {len(members)} file(s), using all of them")
                    count = len(members)
                members = chooser.sample(members, count)
            for member in members:
                chosen.setdefault(member["id"], member)
                labels.setdefault(member["id"], label)
            if group["note"]:
                result["group_notes"].append({"label": label, "note": group["note"]})
            result["calls"].append(label)
        elif item:
            chosen.setdefault(item["id"], item)
            labels.setdefault(item["id"], label)
            result["calls"].append(label)
        elif braced:   # only the braces are an unmistakable RAG call
            result["warnings"].append(f"{label} is not in the RAG library")

    total = 0
    for index, item in enumerate(chosen.values()):
        label = labels[item["id"]]
        if index >= MAX_FILES:
            result["warnings"].append(f"{label}: over the limit of {MAX_FILES} files, left out")
            continue
        if item["kind"] != "text":
            result["media"].append({**item, "label": label})
            continue
        try:
            content = _read_doc(item)
        except OSError:
            result["warnings"].append(f"{label}: {item['name']} could not be read")
            continue
        room = min(DOC_LIMIT, TOTAL_LIMIT - total)
        if len(content) > room:
            result["warnings"].append(f"{label}: {item['name']} was cut to {max(room, 0):,} characters")
            content = content[:max(room, 0)]
        total += len(content)
        result["docs"].append({"label": label, "name": item["name"], "note": item["note"], "text": content})
    result["calls"] = list(dict.fromkeys(result["calls"]))
    return result


def context(resolved: dict[str, Any], media_lines: list[str] | None = None) -> str:
    """The block the planner reads. `media_lines` says how this studio used each media file."""
    parts = []
    for doc in resolved["docs"]:
        note = f"\nUser's note: {doc['note']}" if doc["note"] else ""
        parts.append(f"{doc['label']} is the document {doc['name']}:{note}\n<<<\n{doc['text']}\n>>>")
    parts.extend(media_lines or [])
    parts.extend(f"{item['label']} group, user's note: {item['note']}" for item in resolved["group_notes"])
    if not parts:
        return ""
    return ("RAG LIBRARY — material the user called with @ in the request or guide. Use it as they asked. "
            "An @ call is not prompt text: never copy @{...} into what you write.\n\n" + "\n\n".join(parts))
