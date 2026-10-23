"""Storage locations (Settings -> Storage; the moves themselves run on the host in hostctl/storage.py).

A model moved out of ComfyUI's models folder is a symlink there to its file in a location;
every location is mounted at its own host path in this container, so the link resolves here
too. This module keeps a snapshot of the locations from the host helper and answers the
questions the rest of the app has: where does a link point, which location is that, and
where should a new download go.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import httpx

import integrations

snapshot: dict = {"locations": [], "defaults": {}, "links": {}, "jobs": []}


async def refresh() -> dict:
    if not integrations.HOSTCTL_SECRET:
        return snapshot
    with contextlib.suppress(httpx.HTTPError, ValueError):
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{integrations.HOSTCTL}/storage/links",
                                        headers={"X-Hostctl-Secret": integrations.HOSTCTL_SECRET})
        if response.status_code == 200:
            snapshot.update(response.json())
    return snapshot


async def loop() -> None:
    while True:
        await refresh()
        await asyncio.sleep(20 if any(j.get("state") in ("queued", "copying", "verifying") for j in snapshot["jobs"]) else 60)


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def location_of(target: Path) -> dict | None:
    for loc in snapshot["locations"]:
        if _inside(target, Path(loc["path"])):
            return loc
    return None


def moved_target(path: Path) -> Path | None:
    """The real file behind a link into a storage location (None for plain files / other links)."""
    if not path.is_symlink():
        return None
    target = Path(os.path.realpath(path))
    return target if location_of(target) else None


def remove(path: Path) -> int:
    """Delete a model file; for a moved one also its file in the location. Returns bytes freed."""
    target = moved_target(path)
    freed = 0
    if target is not None:
        with contextlib.suppress(OSError):
            freed = target.stat().st_size
            target.unlink()
    elif path.is_file():
        freed = path.stat().st_size
    path.unlink()
    return freed


def store_for(dest: str) -> str:
    """The location folder a new download into models/<dest> goes to ("" = the models folder itself).
    Only one this container can actually write to; otherwise the models folder."""
    top = dest.split("/", 1)[0]
    category = "loras" if top == "loras" else "llm" if top == "llm" else "models"
    loc_id = (snapshot.get("defaults") or {}).get(category) or "comfy"
    loc = next((l for l in snapshot["locations"] if l["id"] == loc_id), None)
    if not loc or not os.path.isdir(loc["path"]) or not os.access(loc["path"], os.W_OK):
        return ""
    return loc["path"]


def describe(rel: str) -> dict | None:
    """Where an installed file lives, for the Manager: None when it's in the models folder."""
    where = (snapshot.get("links") or {}).get(rel)
    if not where:
        return None
    loc = next((l for l in snapshot["locations"] if l["id"] == where.get("location")), None)
    return {"id": where.get("location") or "", "label": loc["label"] if loc else "Elsewhere",
            "target": where.get("target", ""), "online": bool(where.get("online")), "managed": bool(loc)}
