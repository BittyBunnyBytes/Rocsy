"""Upload cache inventory and pin metadata for the Rocsy gateway."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from threading import Lock

from fastapi import HTTPException

# Also the request body cap for this upload route (security_headers.py).
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
MAX_ZIP_BYTES = 4 * 1024 * 1024 * 1024
# Text documents written in the page (reusable notes, briefs, style guides).
MAX_DOC_BYTES = 1024 * 1024
DOC_EXTS = {".md", ".txt", ".markdown", ".json", ".csv", ".yaml", ".yml", ".srt", ".vtt", ".xml", ".html", ".css", ".js", ".py", ".log"}


def clean_rag_terms(terms) -> list[str]:
    """Store short, distinct trigger phrases; an empty list disables auto matching."""
    if not isinstance(terms, list) or len(terms) > 30 or not all(isinstance(term, str) for term in terms):
        raise HTTPException(422, "Use up to 30 RAG terms")
    cleaned = list(dict.fromkeys(" ".join(term.split()).strip().casefold() for term in terms))
    if any(not term or len(term) > 80 for term in cleaned):
        raise HTTPException(422, "Each RAG term must be 1–80 characters")
    return cleaned


def clean_note(note) -> str:
    if not isinstance(note, str):
        raise HTTPException(422, "Context note must be text")
    note = note.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(note) > 2000:
        raise HTTPException(422, "Context note must be 2,000 characters or less")
    return note

class UploadCache:
    """A conservative inventory over the explicitly mounted upload root.

    Pins are metadata only: the file remains in the same user-owned upload
    directory and is excluded from automatic expiry until unpinned.
    """

    _SCAN_TTL = 3.0
    _THUMB_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

    def __init__(self, root: Path, metadata_path: Path, retention_seconds: int = 7 * 86400,
                 reference_db_path: Path | None = None):
        self.root = root
        self.metadata_path = metadata_path
        self.reference_db_path = reference_db_path
        self.groups_path = metadata_path.with_name("upload-cache-groups.json")
        self.retention_seconds = retention_seconds
        self.thumbs_dir = metadata_path.with_name("upload-thumbs")
        self._lock = Lock()
        # One directory walk serves a whole gallery burst instead of one per image.
        self._scan_lock = Lock()
        self._scan: tuple[float, list[tuple[str, Path, os.stat_result]], dict[str, tuple[str, Path, os.stat_result]]] | None = None

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.metadata_path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.metadata_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.metadata_path)

    def _load_groups(self) -> dict[str, dict]:
        try:
            data = json.loads(self.groups_path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_groups(self, groups: dict[str, dict]) -> None:
        self.groups_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.groups_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(groups, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.groups_path)

    def groups(self) -> list[dict]:
        with self._lock:
            groups = self._load_groups()
            pins = self._load()
        available = {self._id(relative) for relative, _, _ in self._files()}
        valid = {key for key, entry in pins.items() if isinstance(entry, dict)
                 and entry.get("pinned") in (True, "true") and key in available}
        return sorted(({"id": key, "name": group["name"],
                        "files": [item for item in group.get("files", []) if item in valid],
                        "rag_terms": group.get("rag_terms", []), "note": group.get("note", "")}
                       for key, group in groups.items() if isinstance(group, dict)
                       and isinstance(group.get("name"), str)), key=lambda group: group["name"].casefold())

    def save_group(self, group_id: str | None, name: str, file_ids: list[str], rag_terms: list[str] | None = None,
                   note: str | None = None) -> dict:
        name = " ".join(name.split()).strip()
        if not name or len(name) > 80 or any(char in name for char in "{}@\n\r"):
            raise HTTPException(422, "Group name must be 1–80 characters without @ or braces")
        if not isinstance(file_ids, list) or not file_ids or len(file_ids) > 200 or not all(isinstance(x, str) for x in file_ids):
            raise HTTPException(422, "Choose 1–200 pinned files")
        unique = list(dict.fromkeys(file_ids))
        available = {self._id(relative) for relative, _, _ in self._files()}
        with self._lock:
            pins = self._load()
            if any(key not in available or pins.get(key, {}).get("pinned") not in (True, "true") for key in unique):
                raise HTTPException(422, "Groups can contain only currently pinned files")
            groups = self._load_groups()
            if group_id and group_id not in groups:
                raise HTTPException(404, "Group not found")
            if any(key != group_id and group.get("name", "").casefold() == name.casefold() for key, group in groups.items()):
                raise HTTPException(409, "A group with this name already exists")
            key = group_id or uuid.uuid4().hex
            terms = clean_rag_terms(rag_terms if rag_terms is not None else groups.get(key, {}).get("rag_terms", []))
            saved_note = clean_note(note if note is not None else groups.get(key, {}).get("note", ""))
            groups[key] = {"name": name, "files": unique, "rag_terms": terms, "note": saved_note}
            self._save_groups(groups)
        return {"id": key, **groups[key]}

    def delete_group(self, group_id: str) -> None:
        with self._lock:
            groups = self._load_groups()
            if group_id not in groups:
                raise HTTPException(404, "Group not found")
            del groups[group_id]
            self._save_groups(groups)

    def _files(self) -> list[tuple[str, Path, os.stat_result]]:
        return self._indexed()[0]

    def _indexed(self) -> tuple[list[tuple[str, Path, os.stat_result]], dict[str, tuple[str, Path, os.stat_result]]]:
        with self._scan_lock:
            if self._scan and time.monotonic() - self._scan[0] < self._SCAN_TTL:
                return self._scan[1], self._scan[2]
            found = self._walk()
            index = {self._id(entry[0]): entry for entry in found}
            self._scan = (time.monotonic(), found, index)
            return found, index

    def _invalidate(self) -> None:
        with self._scan_lock:
            self._scan = None

    def _walk(self) -> list[tuple[str, Path, os.stat_result]]:
        try:
            root = self.root.resolve(strict=True)
        except OSError:
            return []
        if not root.is_dir():
            return []
        found = []
        for path in root.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.resolve().relative_to(root)
                stat = path.stat()
            except (OSError, ValueError):
                continue
            found.append((relative.as_posix(), path, stat))
        return sorted(found, key=lambda item: item[2].st_mtime, reverse=True)

    @staticmethod
    def _id(relative: str) -> str:
        return hashlib.sha256(relative.encode()).hexdigest()[:20]

    def _reference_kinds(self) -> tuple[set[str], set[str]]:
        """Classify opaque H3 reference names using their original metadata."""
        if self.reference_db_path is None:
            return set(), set()
        try:
            with sqlite3.connect(self.reference_db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2) as db:
                rows = db.execute("SELECT stored_name, filename, thumbnail_stored_name, role FROM h3_references").fetchall()
        except (OSError, sqlite3.Error):
            return set(), set()
        working = set()
        generated = set()
        for stored, original, thumb, role in rows:
            if thumb:
                working.add(thumb)
            if stored and isinstance(original, str) and (
                original.startswith("agentic-") and (
                    original.endswith("-final-frame.png") or "-tail-" in original
                )
            ):
                working.add(stored)
            if stored and role == "agentic-references":
                generated.add(stored)
        return working, generated

    def _reference_files(self) -> set[str]:
        """Names H3 still points at (references + their thumbnails): never deleted from here."""
        if self.reference_db_path is None:
            return set()
        try:
            with sqlite3.connect(self.reference_db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2) as db:
                rows = db.execute("SELECT stored_name, thumbnail_stored_name FROM h3_references").fetchall()
        except (OSError, sqlite3.Error):
            # Can't tell what H3 uses: treat everything as in use (fail closed).
            return {"*"}
        return {name for row in rows for name in row if name}

    def list(self) -> list[dict]:
        with self._lock:
            pins = self._load()
        now = time.time()
        working_names, generated_names = self._reference_kinds()
        result = []
        for relative, path, stat in self._files():
            file_id = self._id(relative)
            pin = pins.get(file_id, {})
            pinned = bool(pin.get("pinned"))
            working = "/" in relative or path.name.startswith(".") or path.name == "prompts.md" or path.name in working_names
            cache_kind = ("generated" if path.name in generated_names else "uploaded") if pinned or not working else "working"
            result.append({
                "id": file_id,
                "version": f"{stat.st_mtime_ns:x}-{stat.st_size:x}",
                "name": path.name,
                "path": relative,
                "size": stat.st_size,
                "uploaded_at": stat.st_mtime,
                "deletes_at": None if pinned else stat.st_mtime + self.retention_seconds,
                "pinned": pinned,
                "alias": pin.get("alias", "") if pinned else "",
                "rag_terms": pin.get("rag_terms", []) if pinned else [],
                "note": pin.get("note", "") if pinned else "",
                "cache_kind": cache_kind,
                "show_in_library": cache_kind != "working",
                "editable": path.suffix.lower() in DOC_EXTS and stat.st_size <= MAX_DOC_BYTES,
            })
        result.sort(key=lambda item: (not item["pinned"], -item["uploaded_at"]))
        return result

    def pin(self, file_id: str, alias: str, rag_terms: list[str] | None = None, note: str | None = None) -> dict:
        alias = " ".join(alias.split()).strip()
        if not alias or len(alias) > 80:
            raise HTTPException(status_code=422, detail="Alias must be between 1 and 80 characters")
        files = {self._id(relative): relative for relative, _, _ in self._files()}
        relative = files.get(file_id)
        if not relative:
            raise HTTPException(status_code=404, detail="Upload not found")
        with self._lock:
            pins = self._load()
            terms = clean_rag_terms(rag_terms if rag_terms is not None else pins.get(file_id, {}).get("rag_terms", []))
            saved_note = clean_note(note if note is not None else pins.get(file_id, {}).get("note", ""))
            pins[file_id] = {"path": relative, "alias": alias, "pinned": "true", "rag_terms": terms, "note": saved_note,
                             "updated_at": str(time.time())}
            self._save(pins)
        return next(item for item in self.list() if item["id"] == file_id)

    def content_path(self, file_id: str) -> Path:
        entry = self._indexed()[1].get(file_id)
        if entry:
            resolved = entry[1].resolve()
            try:
                resolved.relative_to(self.root.resolve())
                if resolved.is_file():
                    return resolved
            except (OSError, ValueError):
                pass
        raise HTTPException(status_code=404, detail="Upload not found")

    def make_zip(self, file_ids: list[str]) -> str:
        """Selected files as one stored (uncompressed) zip; the caller deletes it after sending."""
        sources = [self.content_path(file_id) for file_id in dict.fromkeys(file_ids)]
        if not sources:
            raise HTTPException(status_code=422, detail="Choose at least one file")
        if sum(path.stat().st_size for path in sources) > MAX_ZIP_BYTES:
            raise HTTPException(status_code=413, detail="Selection exceeds the 4 GB download limit")
        temp = tempfile.NamedTemporaryFile(prefix="rocsy-cache-", suffix=".zip", delete=False)
        temp.close()
        try:
            used: set[str] = set()
            # Media is already compressed; storing keeps big videos fast to pack.
            with zipfile.ZipFile(temp.name, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for path in sources:
                    name, index = path.name, 2
                    while name in used:
                        name = f"{path.stem} ({index}){path.suffix}"
                        index += 1
                    used.add(name)
                    archive.write(path, name)
            return temp.name
        except Exception:
            os.unlink(temp.name)
            raise

    def thumbnail_path(self, file_id: str) -> Path | None:
        """A small WebP of an image upload, rendered once per file version.

        None means "no thumbnail" (not an image, or Pillow can't read it);
        the caller then serves the original.
        """
        source = self.content_path(file_id)
        if source.suffix.lower() not in self._THUMB_EXTS:
            return None
        stat = source.stat()
        target = self.thumbs_dir / f"{file_id}-{stat.st_mtime_ns:x}-{stat.st_size:x}.webp"
        if target.exists():
            return target
        try:
            from PIL import Image, ImageOps
            with Image.open(source) as image:
                image.draft("RGB", (960, 960))  # JPEG: decode at reduced scale
                image = ImageOps.exif_transpose(image)
                image.thumbnail((480, 480))
                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGBA" if "transparency" in image.info or image.mode in ("LA", "PA") else "RGB")
                out = io.BytesIO()
                image.save(out, "WEBP", quality=80, method=4)
        except Exception:  # noqa: BLE001 - unreadable or not really an image
            return None
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)
        for stale in self.thumbs_dir.glob(f"{file_id}-*.webp"):
            stale.unlink(missing_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(out.getvalue())
        os.replace(tmp, target)
        return target

    def save_upload(self, upload) -> dict:
        filename = Path(str(upload.filename or "")).name.strip()
        if not filename or filename in {".", ".."}:
            raise HTTPException(status_code=422, detail="A file name is required")
        if len(filename) > 180:
            filename = filename[-180:]
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / filename
        if target.exists():
            stem, suffix = target.stem, target.suffix
            index = 2
            while target.exists():
                target = self.root / f"{stem} ({index}){suffix}"
                index += 1
        temp = target.with_name(f".{target.name}.uploading")
        total = 0
        try:
            with temp.open("wb") as handle:
                while chunk := upload.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="Upload exceeds the 250 MB limit")
                    handle.write(chunk)
            os.replace(temp, target)
            self._invalidate()
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        file_id = self._id(target.name)
        return next(item for item in self.list() if item["id"] == file_id)

    def unpin(self, file_id: str) -> dict:
        with self._lock:
            pins = self._load()
            pins.pop(file_id, None)
            self._save(pins)
            groups = self._load_groups()
            for group in groups.values():
                group["files"] = [key for key in group.get("files", []) if key != file_id]
            self._save_groups(groups)
        return next((item for item in self.list() if item["id"] == file_id), {"id": file_id, "pinned": False})

    def save_doc(self, file_id: str | None, name: str, text) -> dict:
        """Create a text document in the upload root (file_id None) or overwrite an existing one."""
        if not isinstance(text, str):
            raise HTTPException(status_code=422, detail="Document text must be text")
        data = text.replace("\r\n", "\n").encode()
        if len(data) > MAX_DOC_BYTES:
            raise HTTPException(status_code=413, detail="Documents can be at most 1 MB")
        if file_id:
            target = self.content_path(file_id)
            if target.suffix.lower() not in DOC_EXTS:
                raise HTTPException(status_code=422, detail="Only text files can be edited here")
        else:
            stem = "".join(c if c.isalnum() or c in " -_()." else "-" for c in " ".join(str(name or "").split())).strip(" .-")[:120]
            if not stem:
                raise HTTPException(status_code=422, detail="Give the document a name")
            if Path(stem).suffix.lower() not in DOC_EXTS:
                stem += ".md"
            self.root.mkdir(parents=True, exist_ok=True)
            target = self.root / stem
            index = 2
            while target.exists():
                target = self.root / f"{Path(stem).stem} ({index}){Path(stem).suffix}"
                index += 1
        temp = target.with_name(f".{target.name}.saving")
        try:
            temp.write_bytes(data)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        self._invalidate()
        file_id = self._id(target.resolve().relative_to(self.root.resolve()).as_posix())
        return next(item for item in self.list() if item["id"] == file_id)

    def delete(self, file_id: str) -> dict:
        """Delete a file for good (and its RAG pin, group memberships and thumbnail)."""
        path = self.content_path(file_id)
        used = self._reference_files()
        if "*" in used or path.name in used:
            raise HTTPException(status_code=409, detail=f"{path.name} is used by a Video reference — remove it there, or just remove it from RAG")
        with self._lock:
            path.unlink(missing_ok=True)
            pins = self._load()
            pins.pop(file_id, None)
            self._save(pins)
            groups = self._load_groups()
            for group in groups.values():
                group["files"] = [key for key in group.get("files", []) if key != file_id]
            self._save_groups(groups)
        for stale in self.thumbs_dir.glob(f"{file_id}-*.webp"):
            stale.unlink(missing_ok=True)
        self._invalidate()
        return {"deleted": file_id, "name": path.name}
