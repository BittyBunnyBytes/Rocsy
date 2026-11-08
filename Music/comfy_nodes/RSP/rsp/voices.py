"""RSP's custom voice library: models/RSP/voices/<id>/{voice.<ext>, voice.json}. HTTP routes at /rsp/voices."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid

SUFFIXES = {".flac", ".wav", ".mp3", ".ogg", ".opus", ".m4a"}
_ID = re.compile(r"^[a-f0-9]{16}$")


def root() -> str:
    import folder_paths

    return os.path.join(folder_paths.models_dir, "RSP", "voices")


def _folder(base: str, voice_id: str) -> str:
    if not _ID.match(voice_id or ""):
        raise ValueError("That isn't a voice id.")
    return os.path.join(base, voice_id)


def listing(base: str | None = None) -> list[dict]:
    base = base or root()
    found = []
    if os.path.isdir(base):
        for name in os.listdir(base):
            try:
                with open(os.path.join(base, name, "voice.json"), encoding="utf-8") as handle:
                    found.append(json.load(handle))
            except (OSError, ValueError):
                continue
    return sorted(found, key=lambda item: str(item.get("name", "")).lower())


def add(name: str, filename: str, data: bytes, base: str | None = None) -> dict:
    base = base or root()
    name = " ".join((name or "").split())[:80]
    suffix = os.path.splitext(filename or "")[1].lower()
    if not name:
        raise ValueError("Give the voice a name.")
    if suffix not in SUFFIXES:
        raise ValueError("Upload an audio file (FLAC, WAV, MP3, Ogg, Opus, M4A).")
    if not data:
        raise ValueError("That file is empty.")
    voice_id = uuid.uuid4().hex[:16]
    folder = _folder(base, voice_id)
    os.makedirs(folder)
    item = {"id": voice_id, "name": name, "file": "voice" + suffix, "created": int(time.time())}
    with open(os.path.join(folder, item["file"]), "wb") as handle:
        handle.write(data)
    # Written last: a folder without voice.json isn't listed, so a crash leaves no half voice.
    with open(os.path.join(folder, "voice.json"), "w", encoding="utf-8") as handle:
        json.dump(item, handle, ensure_ascii=False)
    return item


def path_of(voice_id: str, base: str | None = None) -> str:
    base = base or root()
    folder = _folder(base, voice_id)
    with open(os.path.join(folder, "voice.json"), encoding="utf-8") as handle:
        name = os.path.basename(json.load(handle)["file"])
    return os.path.join(folder, name)


def rename(voice_id: str, name: str, base: str | None = None) -> dict:
    name = " ".join((name or "").split())[:80]
    if not name:
        raise ValueError("Give the voice a name.")
    path = os.path.join(_folder(base or root(), voice_id), "voice.json")
    with open(path, encoding="utf-8") as handle:
        item = json.load(handle)
    item["name"] = name
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(item, handle, ensure_ascii=False)
    os.replace(path + ".tmp", path)   # never a half-written voice.json
    return item


def remove(voice_id: str, base: str | None = None) -> None:
    shutil.rmtree(_folder(base or root(), voice_id))


def register_routes() -> None:
    """Skipped outside a running ComfyUI (tests), and a failure only costs the HTTP API, not the nodes."""
    import sys

    if "server" not in sys.modules:     # ComfyUI imports it before any custom node; importing it here would load the GPU stack
        return
    try:
        from aiohttp import web
        from server import PromptServer

        routes = PromptServer.instance.routes
    except Exception as error:
        print(f"[RSP] voice routes not registered: {error}")
        return

    @routes.get("/rsp/voices")
    async def list_voices(_request):
        return web.json_response({"voices": listing()})

    @routes.post("/rsp/voices")
    async def add_voice(request):
        form = await request.post()
        upload = form.get("file")
        if not hasattr(upload, "file"):
            return web.json_response({"message": "Upload an audio file."}, status=422)
        try:
            return web.json_response(add(str(form.get("name") or ""), upload.filename, upload.file.read()))
        except ValueError as error:
            return web.json_response({"message": str(error)}, status=422)

    @routes.get("/rsp/voices/{voice_id}/audio")
    async def voice_audio(request):
        try:
            return web.FileResponse(path_of(request.match_info["voice_id"]))
        except (ValueError, OSError, KeyError):
            return web.json_response({"message": "That voice is gone."}, status=404)

    @routes.patch("/rsp/voices/{voice_id}")
    async def rename_voice(request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        try:
            return web.json_response(rename(request.match_info["voice_id"], str((body or {}).get("name") or "")))
        except OSError:
            return web.json_response({"message": "That voice is gone."}, status=404)
        except ValueError as error:
            return web.json_response({"message": str(error)}, status=422)

    @routes.delete("/rsp/voices/{voice_id}")
    async def delete_voice(request):
        try:
            remove(request.match_info["voice_id"])
        except (ValueError, OSError):
            return web.json_response({"message": "That voice is gone."}, status=404)
        return web.json_response({"deleted": True})
