"""Dataset browser/editor API: list datasets, view items with captions and H3 clip checks,
edit captions, upload (chunked, to fit under the gateway's body limit), delete items and
convert clips to the H3 spec. Video work uses VAAPI on the GPU."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import time
import urllib.parse
from contextlib import suppress
from glob import escape as glob_escape
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import families

VIDEO = {".mp4", ".mov", ".webm", ".mkv"}
IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}   # what Fizgig trains on
AUDIO = {".wav", ".flac", ".m4a", ".mp3", ".ogg"}
UPLOAD_TYPES = VIDEO | IMAGE | AUDIO | {".txt", ".csv"}
H3_FRAMES = [5 + 17 * n for n in range(8)]          # 5, 22, 39 … 124
H3_AUDIO_SECONDS = [f / 24 for f in H3_FRAMES if f >= 22]   # voice clips must be one of these (±25 ms)
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
FILE_RE = re.compile(r"[^/\\\x00]{1,200}")
VAAPI = ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi"]
ORIGINALS = "_originals"
INCOMING = ".incoming"          # YouTube downloads waiting to be trimmed (hidden from training)
H3_MAX_TRIM = 124 / 24          # longest H3 training clip
MAX_TRIM, MIN_TRIM = 15.0, 1.0  # same limits as H3 Machine's reference trimmer


class TrimSegment(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)


class TrimBody(BaseModel):
    segments: list[TrimSegment] = Field(min_length=1, max_length=50)
    h3_ready: bool = True


class YoutubeBody(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    force: bool = False            # download again even if the registry says it was downloaded before


TRACKING = re.compile(r"^(utm_.*|si|feature|fbclid|gclid|ref|ref_src|igshid|pp|ab_channel|list|index|start_radio|t)$", re.I)


def video_key(url: str) -> str:
    """One key per video, whatever form the link has (youtu.be / watch?v= / shorts, www., tracking
    parameters, trailing slash), so the same clip is never downloaded twice by accident."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (p.hostname or "").lower()
    for pre in ("www.", "m.", "music."):
        host = host.removeprefix(pre)
    q = urllib.parse.parse_qs(p.query)
    if host in ("youtube.com", "youtube-nocookie.com", "youtu.be"):
        vid = (q.get("v") or [None])[0] or (p.path.strip("/").split("/")[-1] if host == "youtu.be" or p.path.startswith(("/shorts/", "/embed/", "/live/")) else None)
        if vid:
            return f"youtube:{vid}"
    keep = sorted((k, v) for k, vs in q.items() if not TRACKING.match(k) for v in vs)
    return host + p.path.rstrip("/") + (("?" + urllib.parse.urlencode(keep)) if keep else "")


class CaptionBody(BaseModel):
    caption: str = Field(max_length=20000)


class NewDataset(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class TriggerBody(BaseModel):
    trigger: str = Field(min_length=1, max_length=64)


def build_router(prefix: str, dataset_root: Path, data_dir: Path, busy_datasets, tag_activity=lambda: []) -> APIRouter:
    router = APIRouter(prefix=prefix)
    downloads: dict[str, dict[str, Any]] = {}
    thumbs = data_dir / "thumbs"
    # ffprobe results survive restarts (data/probes.json); without them the first open
    # of a dataset after a restart probed every clip one by one (~4.5 s for 106 clips).
    probe_file = data_dir / "probes.json"
    try:
        probes: dict[str, tuple[float, dict[str, Any]]] = {k: (v[0], v[1]) for k, v in json.loads(probe_file.read_text()).items()}
    except (OSError, ValueError, TypeError, IndexError):
        probes = {}
    probes_dirty = False
    thumb_slots = asyncio.Semaphore(6)        # VAAPI thumbnails at a time (a full page asks for ~100)
    thumb_locks: dict[str, asyncio.Lock] = {}
    warming: set[str] = set()

    def save_probes() -> None:
        nonlocal probes_dirty
        if not probes_dirty:
            return
        probes_dirty = False
        live = {k: v for k, v in probes.items() if Path(k).exists()}
        tmp = probe_file.with_suffix(".tmp")
        with suppress(OSError):
            tmp.write_text(json.dumps(live))
            tmp.replace(probe_file)

    def ds_path(name: str, must_exist: bool = True) -> Path:
        if not NAME_RE.fullmatch(name):
            raise HTTPException(422, "Dataset names may contain only letters, numbers, _ and -.")
        path = (dataset_root / name).resolve()
        if not path.is_relative_to(dataset_root.resolve()):
            raise HTTPException(400, "Invalid dataset path.")
        if must_exist and not path.is_dir():
            raise HTTPException(404, "Dataset not found.")
        return path

    def item_path(ds: Path, filename: str, staged: bool = False) -> Path:
        if not FILE_RE.fullmatch(filename) or filename.startswith("."):
            raise HTTPException(422, "Invalid file name.")
        folder = (ds / INCOMING) if staged else ds
        path = (folder / filename).resolve()
        if path.parent != folder.resolve():
            raise HTTPException(400, "Invalid file path.")
        return path

    def give_back(path: Path) -> None:
        """The container runs as root; hand anything it wrote back to the folder's owner so the
        user can edit/delete it from the desktop."""
        st = dataset_root.stat()
        targets = [path, *path.rglob("*")] if path.is_dir() else [path]
        for t in targets:
            try:
                if t.lstat().st_uid != st.st_uid:
                    os.lchown(t, st.st_uid, st.st_gid)
            except OSError:
                pass

    def count_frames(path: Path) -> int | None:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-count_packets", "-show_entries",
                              "stream=nb_read_packets", "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=120).stdout.strip()
        return int(out) if out.isdigit() else None

    def free_media_name(folder: Path, name: str) -> Path:
        """A path whose STEM no other media file in the folder uses (Fizgig refuses two items that
        share a stem, e.g. foo.mp4 + foo.png), keeping the extension."""
        stem, ext = Path(name).stem, Path(name).suffix
        taken = {p.stem.casefold() for p in folder.iterdir() if p.is_file() and kind_of(p)} if folder.is_dir() else set()
        cand, n = stem, 2
        while cand.casefold() in taken or (folder / f"{cand}{ext}").exists():
            cand = f"{stem}_{n}"; n += 1
        return folder / f"{cand}{ext}"

    def caption_of(media: Path) -> Path:
        """The clip's caption file: an exact-case <stem>.txt, else one matching case-insensitively
        (Fizgig matches captions that way), else the exact-case path for a new caption."""
        exact = media.with_suffix(".txt")
        if exact.is_file():
            return exact
        key = media.stem.casefold()
        for p in media.parent.iterdir():
            if p.is_file() and p.suffix.lower() == ".txt" and p.stem.casefold() == key:
                return p
        return exact

    def remove_captions(ds: Path, media: Path) -> list[str]:
        """Delete the tagging caption(s) of a removed clip: any .txt whose name matches the clip's
        (case-insensitive, like Fizgig's matching), unless another media file still uses it."""
        key = media.stem.casefold()
        if any(p.is_file() and p != media and kind_of(p) and p.stem.casefold() == key for p in ds.iterdir()):
            return []          # e.g. foo.png still needs foo.txt
        removed = []
        for p in ds.iterdir():
            if p.is_file() and p.suffix.lower() == ".txt" and p.stem.casefold() == key:
                p.unlink(missing_ok=True)
                removed.append(p.name)
        return removed

    def guard_jobs(name: str, filename: str | None = None, staged: bool = False, whole: bool = False) -> None:
        """Refuse changes that would pull a file (or the dataset) out from under an auto-cut/tag job."""
        for j in tag_activity():
            if j["dataset"] != name:
                continue
            if whole:
                raise HTTPException(409, f"A {({'cut': 'Hermes auto-cut', 'find': 'Hermes clip search'}).get(j['kind'], 'tagging')} job is running in this dataset; cancel it first.")
            if j["kind"] == "cut" and j.get("file") == filename and bool(j["staged"]) == bool(staged):
                raise HTTPException(409, "Hermes is still scouting this video for clips; wait for it or cancel it first.")

    def guard_busy(name: str) -> None:
        if name in busy_datasets():
            raise HTTPException(409, "A training job is using this dataset right now; wait for it to finish.")

    def kind_of(path: Path) -> str | None:
        ext = path.suffix.lower()
        return "video" if ext in VIDEO else "image" if ext in IMAGE else "audio" if ext in AUDIO else None

    def probe(path: Path) -> dict[str, Any]:
        nonlocal probes_dirty
        key, mtime = str(path), path.stat().st_mtime
        hit = probes.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        info: dict[str, Any] = {}
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_type,width,height,r_frame_rate,nb_frames,sample_rate,channels:format=duration",
                 "-of", "json", str(path)], capture_output=True, text=True, timeout=20).stdout
            data = json.loads(out or "{}")
            for s in data.get("streams", []):
                if s.get("codec_type") == "video" and "width" not in info:
                    num, _, den = (s.get("r_frame_rate") or "0/1").partition("/")
                    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
                    info.update(width=s.get("width"), height=s.get("height"), fps=round(fps, 3),
                                frames=int(s["nb_frames"]) if str(s.get("nb_frames", "")).isdigit() else None)
                elif s.get("codec_type") == "audio" and "sample_rate" not in info:
                    info.update(sample_rate=int(s.get("sample_rate") or 0), channels=s.get("channels"))
            info["duration"] = round(float(data.get("format", {}).get("duration") or 0), 3)
            if info.get("frames") is None and info.get("fps") and info.get("duration"):
                info["frames"] = round(info["fps"] * info["duration"])
        except Exception as exc:  # unreadable file: report, don't crash the listing
            info["error"] = str(exc)[:200]
        probes[key] = (mtime, info)
        probes_dirty = True
        return info

    def h3_issues(path: Path, info: dict[str, Any]) -> list[str]:
        issues = []
        if path.suffix.lower() != ".mp4":
            issues.append("not .mp4")
        if info.get("fps") and abs(info["fps"] - 24) > 0.01:
            issues.append(f"{info['fps']:g} fps (needs 24)")
        if info.get("frames") not in H3_FRAMES:
            issues.append(f"{info.get('frames')} frames (needs 22/39/56/73/90/107/124)")
        if info.get("width") and (info["width"] % 32 or info["height"] % 32):
            issues.append(f"{info['width']}x{info['height']} (sides must divide by 32)")
        if info.get("sample_rate") and not (info["sample_rate"] == 32000 and info.get("channels") == 2):
            issues.append("audio must be 32 kHz stereo or removed")
        return issues

    def summarize(ds: Path) -> dict[str, Any]:
        counts = {"video": 0, "image": 0, "audio": 0}
        missing, size = 0, 0
        captions = {p.stem.casefold() for p in ds.iterdir() if p.suffix.lower() == ".txt"}
        for p in ds.iterdir():
            if not p.is_file():
                continue
            size += p.stat().st_size
            k = kind_of(p)
            if k:
                counts[k] += 1
                if p.stem.casefold() not in captions:
                    missing += 1
        return {"name": ds.name, "counts": counts, "items": sum(counts.values()), "missing_captions": missing,
                "size_mb": round(size / 1048576, 1), "has_metadata_csv": (ds / "metadata.csv").is_file()}

    @router.get("/api/datasets")
    async def list_datasets() -> dict[str, Any]:
        def scan():
            out = []
            for p in sorted(dataset_root.iterdir(), key=lambda x: x.name.casefold()):
                if p.is_dir() and NAME_RE.fullmatch(p.name):
                    out.append(summarize(p))
            return out
        return {"datasets": await asyncio.to_thread(scan), "root_host": "~/AI/BunnyBytes/Datasets"}

    @router.post("/api/datasets", status_code=201)
    async def create_dataset(body: NewDataset) -> dict[str, Any]:
        ds = ds_path(body.name.strip(), must_exist=False)
        if ds.exists():
            raise HTTPException(409, "A dataset with that name already exists.")
        ds.mkdir(parents=True)
        give_back(ds)
        return summarize(ds)

    @router.delete("/api/datasets/{name}")
    async def delete_dataset(name: str, confirm: str = "") -> dict[str, Any]:
        ds = ds_path(name)
        if confirm != name:
            raise HTTPException(400, "Confirm by sending the dataset name.")
        guard_busy(name)
        guard_jobs(name, whole=True)
        await asyncio.to_thread(shutil.rmtree, ds)
        shutil.rmtree(thumbs / name, ignore_errors=True)
        return {"deleted": name}

    @router.get("/api/datasets/{name}")
    async def get_dataset(name: str) -> dict[str, Any]:
        ds = ds_path(name)

        def scan():
            items = []
            for p in sorted(ds.iterdir(), key=lambda x: x.name.casefold()):
                k = kind_of(p) if p.is_file() else None
                if not k:
                    continue
                cap = caption_of(p)
                caption = cap.read_text(encoding="utf-8", errors="replace") if cap.is_file() else None
                item = {"file": p.name, "kind": k, "size_mb": round(p.stat().st_size / 1048576, 2),
                        "caption": caption, "mtime": int(p.stat().st_mtime)}
                if k == "video":
                    info = probe(p)
                    item["info"] = info
                    item["h3_issues"] = h3_issues(p, info)
                elif k == "audio":
                    item["info"] = probe(p)
                items.append(item)
            originals = (ds / ORIGINALS)
            incoming = ds / INCOMING
            staged = [{"file": x.name, "info": probe(x), "mtime": int(x.stat().st_mtime), "size_mb": round(x.stat().st_size / 1048576, 1)}
                      for x in sorted(incoming.iterdir()) if x.is_file() and kind_of(x) == "video"] if incoming.is_dir() else []
            save_probes()
            return {**summarize(ds), "items": items, "staged": staged,
                    "originals": sorted(x.name for x in originals.iterdir()) if originals.is_dir() else []}
        result = await asyncio.to_thread(scan)
        if name not in warming:
            warming.add(name)
            asyncio.create_task(warm_thumbs(name, [it["file"] for it in result["items"] if it["kind"] == "video"]))
        return result

    def training_plan(name: str, model: str = "minimax_h3", trigger: str = "") -> dict[str, Any]:
        """What a training run on this dataset would actually use. Uncaptioned files and anything
        Fizgig would refuse or silently drop are LEFT OUT (with the reason) instead of failing the run."""
        ds = ds_path(name)
        included, excluded, warnings = [], [], []
        stems: dict[str, str] = {}
        media = sorted((p for p in ds.iterdir() if p.is_file() and kind_of(p)), key=lambda p: p.name.casefold())
        trig = trigger.strip().casefold()
        no_trigger = []
        leads: dict[str, int] = {}
        captioned = 0
        fam = families.get(model)
        accepts = fam.media if fam else frozenset({"image", "video", "audio"})
        fam_title = fam.title if fam else model
        h3 = model == "minimax_h3"
        for p in media:
            k = kind_of(p)
            cap = caption_of(p)
            text = ""
            if cap.is_file():
                raw = cap.read_bytes()
                for enc in ("utf-8-sig", "utf-16", "latin-1"):
                    try:
                        text = raw.decode(enc).strip(); break
                    except UnicodeDecodeError:
                        continue
            if text:
                captioned += 1
                lead = re.split(r"[,:;\n]", text, maxsplit=1)[0].strip()
                if lead and len(lead) <= 64 and len(lead.split()) <= 3:
                    leads[lead] = leads.get(lead, 0) + 1
            why = None
            if not cap.is_file():
                why = "no caption yet"
            elif not text:
                why = "caption is empty"
            elif k not in accepts:
                why = f"{fam_title} trains on {' and '.join(sorted(accepts)).replace('image', 'images').replace('video', 'clips')} only"
            elif k == "video" and p.suffix.lower() != ".mp4" and h3:
                why = f"{p.suffix} video (the trainer only reads .mp4): use Convert to H3 clip"
            elif k == "video" and h3:
                issues = h3_issues(p, probe(p))
                if issues:
                    why = "off the H3 clip spec: " + ", ".join(issues)
            elif k == "audio" and p.suffix.lower() == ".ogg" and h3:
                why = ".ogg audio (the trainer reads .wav/.flac/.m4a/.mp3)"
            elif k == "audio" and h3:
                d = probe(p).get("duration") or 0
                if not any(abs(d - g) <= 0.025 for g in H3_AUDIO_SECONDS):
                    why = f"audio is {d:.3f}s; it must be one of " + "/".join(f"{g:.3f}" for g in H3_AUDIO_SECONDS) + " s"
            if why is None and p.stem.casefold() in stems:
                why = f"shares its name with {stems[p.stem.casefold()]} (the trainer refuses two items with the same name)"
            if why:
                excluded.append({"file": p.name, "reason": why, "uncaptioned": why in ("no caption yet", "caption is empty")})
                continue
            stems[p.stem.casefold()] = p.name
            included.append({"file": p.name, "kind": k, "caption": text})
            if trig and trig not in text.casefold():
                no_trigger.append(p.name)
        if no_trigger:
            warnings.append(f"{len(no_trigger)} caption(s) don't contain the trigger word “{trigger.strip()}”: "
                            + ", ".join(no_trigger[:4]) + ("…" if len(no_trigger) > 4 else ""))
        # Captions start with "<trigger>, ...": the lead word most captions share is the trigger.
        guess = max(leads, key=leads.get) if leads else ""
        if guess and leads[guess] * 2 < captioned:
            guess = ""
        incoming = ds / INCOMING
        waiting = [x.name for x in incoming.iterdir() if x.is_file() and kind_of(x)] if incoming.is_dir() else []
        if waiting:
            warnings.append(f"{len(waiting)} video(s) are still waiting to be trimmed and are not part of the dataset yet.")
        counts = {"video": sum(1 for i in included if i["kind"] == "video"), "image": sum(1 for i in included if i["kind"] == "image"),
                  "audio": sum(1 for i in included if i["kind"] == "audio")}
        return {"dataset": name, "model": model, "included": included, "excluded": excluded, "warnings": warnings,
                "counts": counts, "trigger_guess": guess, "uncaptioned": sum(1 for e in excluded if e["uncaptioned"]),
                "problems": [e for e in excluded if not e["uncaptioned"]]}

    def build_trainset(name: str, plan: dict[str, Any], dest: Path) -> int:
        """Per-job training folder: symlinks to the included media + a frozen copy of each caption
        under the exact name the trainer expects (<media stem>.txt)."""
        ds = ds_path(name)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for it in plan["included"]:
            (dest / it["file"]).symlink_to(ds / it["file"])
            (dest / f"{Path(it['file']).stem}.txt").write_text(it["caption"] + "\n", encoding="utf-8")
        return len(plan["included"])

    router.training_plan = training_plan
    router.build_trainset = build_trainset

    def pictures(ds: Path) -> list[str]:
        """The dataset's images as media URLs, or its clips' thumbnail URLs when it has no images."""
        files = [p for p in ds.iterdir() if p.is_file() and not p.name.startswith(".")]
        images = [p for p in files if kind_of(p) == "image"]
        route = "media" if images else "thumb"
        chosen = images or [p for p in files if kind_of(p) == "video"]
        return [f"{prefix}/api/datasets/{urllib.parse.quote(ds.name)}/{route}/{urllib.parse.quote(p.name, safe='')}?v={int(p.stat().st_mtime)}"
                for p in chosen]

    @router.get("/api/showcase")
    async def showcase(dataset: str = "", limit: int = 24, exclude: str = "") -> dict[str, Any]:
        """Pictures for the Rocsy home card: a shuffled sample of one dataset (while it
        trains), or one random picture from each of a few random datasets. `exclude` is
        the comma-separated datasets kept off the home page (Rocsy Settings -> Modules)."""
        limit = max(1, min(48, limit))
        skip = {name for name in exclude.split(",") if name}

        def scan():
            if dataset:
                urls = pictures(ds_path(dataset))
                return random.sample(urls, min(limit, len(urls)))
            folders = [p for p in dataset_root.iterdir() if p.is_dir() and NAME_RE.fullmatch(p.name) and p.name not in skip]
            random.shuffle(folders)
            pool = []
            for ds in folders:
                if len(pool) >= min(limit, 12):
                    break
                if urls := pictures(ds):
                    pool.append(random.choice(urls))
            return pool
        return {"dataset": dataset or None, "pictures": await asyncio.to_thread(scan)}

    @router.get("/api/datasets/{name}/training-check")
    async def training_check(name: str, model: str = "minimax_h3", trigger: str = "") -> dict[str, Any]:
        plan = await asyncio.to_thread(training_plan, name, model, trigger)
        return {k: v for k, v in plan.items() if k != "included"} | {"included": len(plan["included"])}

    @router.get("/api/datasets/{name}/media/{filename}")
    async def media(name: str, filename: str, staged: bool = False):
        path = item_path(ds_path(name), filename, staged)
        if not path.is_file() or not kind_of(path):
            raise HTTPException(404, "File not found.")
        return FileResponse(path)

    async def make_thumb(name: str, path: Path) -> Path:
        """The clip's cached mid-point thumbnail (VAAPI), made at most once and six at a time."""
        target = thumbs / name / f"{path.name}.{int(path.stat().st_mtime)}.jpg"
        if target.is_file():
            return target
        async with thumb_locks.setdefault(str(target), asyncio.Lock()):
            if target.is_file():
                return target
            async with thumb_slots:
                target.parent.mkdir(parents=True, exist_ok=True)
                for old in target.parent.glob(f"{glob_escape(path.name)}.*.jpg"):
                    old.unlink(missing_ok=True)
                mid = max(0.0, (probe(path).get("duration") or 0) / 2)
                tmp = target.with_suffix(".tmp.jpg")
                vaapi = ["ffmpeg", "-y", "-loglevel", "error", *VAAPI, "-ss", f"{mid:.2f}", "-i", str(path),
                         "-frames:v", "1", "-vf", "scale_vaapi=w=360:h=-2,hwdownload,format=nv12", "-q:v", "4", str(tmp)]
                proc = await asyncio.to_thread(subprocess.run, vaapi, capture_output=True, timeout=60)
                if proc.returncode or not tmp.is_file():
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(500, "Thumbnail failed: " + proc.stderr.decode(errors="replace")[-300:])
                tmp.replace(target)
        thumb_locks.pop(str(target), None)
        return target

    async def warm_thumbs(name: str, files: list[str]) -> None:
        """After a dataset is opened, make any missing thumbnails in the background so the
        grid finds them ready instead of waiting on ffmpeg as it scrolls."""
        try:
            ds = ds_path(name)
            for file in files:
                path = ds / file
                if path.is_file() and not (thumbs / name / f"{file}.{int(path.stat().st_mtime)}.jpg").is_file():
                    with suppress(Exception):
                        await make_thumb(name, path)
        finally:
            warming.discard(name)
            await asyncio.to_thread(save_probes)

    @router.get("/api/datasets/{name}/thumb/{filename}")
    async def thumb(name: str, filename: str):
        ds = ds_path(name)
        path = item_path(ds, filename)
        if not path.is_file() or kind_of(path) != "video":
            raise HTTPException(404, "No thumbnail for this file.")
        target = await make_thumb(name, path)
        # The page asks with ?v=<mtime>, so a changed clip gets a new URL.
        return FileResponse(target, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @router.get("/api/datasets/{name}/frame/{filename}")
    async def frame(name: str, filename: str, t: float = 0.0, staged: bool = False, w: int = 240):
        """One video frame as a small JPEG (VAAPI), for the auto-cut scout feed."""
        path = item_path(ds_path(name), filename, staged)
        if not path.is_file() or kind_of(path) != "video":
            raise HTTPException(404, "Video not found.")
        w = max(96, min(640, w)) // 2 * 2
        cache = thumbs / "_frames" / name / f"{filename}.{int(path.stat().st_mtime)}.{t:.2f}.{w}.jpg"
        if not cache.is_file():
            cache.parent.mkdir(parents=True, exist_ok=True)
            cmd = ["ffmpeg", "-y", "-loglevel", "error", *VAAPI, "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1",
                   "-vf", f"scale_vaapi=w={w}:h=-2,hwdownload,format=nv12", str(cache)]
            proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=60)
            if proc.returncode or not cache.is_file():
                raise HTTPException(500, "Frame grab failed.")
        return FileResponse(cache, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @router.put("/api/datasets/{name}/caption/{filename}")
    async def put_caption(name: str, filename: str, body: CaptionBody) -> dict[str, Any]:
        ds = ds_path(name)
        guard_busy(name)
        path = item_path(ds, filename)
        if not path.is_file() or not kind_of(path):
            raise HTTPException(404, "File not found.")
        text = body.caption.strip()
        cap = caption_of(path)
        if text:
            cap.write_text(text + "\n", encoding="utf-8")
        else:
            cap.unlink(missing_ok=True)
        give_back(ds)
        return {"file": filename, "caption": text or None}

    @router.post("/api/datasets/{name}/trigger")
    async def add_trigger(name: str, body: TriggerBody) -> dict[str, int]:
        ds = ds_path(name)
        guard_busy(name)
        trig = body.trigger.strip()
        changed = 0
        for p in ds.iterdir():
            if p.is_file() and kind_of(p):
                cap = caption_of(p)
                text = cap.read_text(encoding="utf-8").strip() if cap.is_file() else ""
                if re.search(rf"(?<![\w]){re.escape(trig)}(?![\w])", text, re.I):
                    continue
                cap.write_text(f"{trig}, {text}\n" if text else f"{trig}\n", encoding="utf-8")
                changed += 1
        give_back(ds)
        return {"changed": changed}

    @router.delete("/api/datasets/{name}/items/{filename}")
    async def delete_item(name: str, filename: str) -> dict[str, Any]:
        ds = ds_path(name)
        guard_busy(name)
        guard_jobs(name, filename)
        path = item_path(ds, filename)
        if not path.is_file():
            raise HTTPException(404, "File not found.")
        path.unlink()
        captions = remove_captions(ds, path) if kind_of(path) else []
        for old in (thumbs / name).glob(f"{filename}.*.jpg") if (thumbs / name).is_dir() else []:
            old.unlink(missing_ok=True)
        return {"deleted": filename, "captions_deleted": captions}

    @router.post("/api/datasets/{name}/upload")
    async def upload_chunk(name: str, request: Request, filename: str, offset: int, total: int, staged: bool = False) -> dict[str, Any]:
        """Raw-body chunk upload: send the file in order, `offset` = bytes already sent.
        staged=true puts a VIDEO into "Waiting to be trimmed" (.incoming) instead of the dataset."""
        ds = ds_path(name)
        guard_busy(name)
        safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(filename).name).strip(" .")
        if not safe or Path(safe).suffix.lower() not in UPLOAD_TYPES:
            raise HTTPException(415, "Upload videos (.mp4 .mov .webm .mkv), images (.jpg .png .webp), audio, or .txt captions.")
        if not (0 <= offset <= total <= 8 * 1024**3):
            raise HTTPException(400, "Invalid upload range.")
        staged = staged and kind_of(Path(safe)) == "video"
        folder = ds / INCOMING if staged else ds
        if staged:
            folder.mkdir(exist_ok=True)
        if Path(safe).suffix.lower() == ".txt" and Path(safe).suffix != ".txt":
            safe = Path(safe).stem + ".txt"          # Fizgig only reads lower-case .txt captions
        final = item_path(ds, safe, staged)
        part = ds / ".uploads" / (safe + ".part")
        part.parent.mkdir(exist_ok=True)
        if offset == 0:
            part.unlink(missing_ok=True)
        elif not part.is_file() or part.stat().st_size != offset:
            raise HTTPException(409, "Upload out of order; start the file again.")
        with part.open("ab") as fh:
            async for chunk in request.stream():
                fh.write(chunk)
        size = part.stat().st_size
        if size > total:
            part.unlink(missing_ok=True)
            raise HTTPException(400, "Upload larger than declared.")
        if size == total:
            if kind_of(final):
                final = free_media_name(folder, final.name)
            elif final.exists():
                stem, ext = final.stem, final.suffix
                n = 2
                while (folder / f"{stem}_{n}{ext}").exists():
                    n += 1
                final = folder / f"{stem}_{n}{ext}"
            part.rename(final)
            give_back(ds)
            return {"done": True, "file": final.name, "staged": staged}
        return {"done": False, "received": size}

    @router.post("/api/datasets/{name}/convert/{filename}")
    async def convert_h3(name: str, filename: str) -> dict[str, Any]:
        """Re-encode a clip onto the H3 spec on the GPU: 24 fps, the longest grid length that
        fits (from the start, max 124 frames), sides cropped to multiples of 32, long side <= 1024,
        audio resampled to 32 kHz stereo. The original moves to _originals/."""
        ds = ds_path(name)
        guard_busy(name)
        guard_jobs(name, filename)
        src = item_path(ds, filename)
        if not src.is_file() or kind_of(src) != "video":
            raise HTTPException(404, "Video not found.")
        info = probe(src)
        if not info.get("width"):
            raise HTTPException(422, "Could not read this video.")
        frames24 = int((info.get("duration") or 0) * 24)
        n = max([f for f in H3_FRAMES if f <= frames24], default=0)
        if n < 22:
            raise HTTPException(422, "Clip is shorter than 22 frames (~0.9 s) at 24 fps.")
        w, h = info["width"], info["height"]
        scale = min(1.0, 1024 / max(w, h))
        tw, th = int(w * scale) // 32 * 32, int(h * scale) // 32 * 32
        sw, sh = int(round(w * scale / 2) * 2), int(round(h * scale / 2) * 2)
        out = ds / f".{src.stem}.h3tmp.mp4"
        vf = f"fps=24,scale_vaapi=w={sw}:h={sh},hwdownload,format=nv12,crop={tw}:{th},tpad=stop_mode=clone:stop_duration=1,hwupload"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-vaapi_device", "/dev/dri/renderD128", *VAAPI, "-i", str(src), "-vf", vf, "-frames:v", str(n),
               "-c:v", "h264_vaapi", "-qp", "18"]
        cmd += (["-af", "apad", "-c:a", "aac", "-ar", "32000", "-ac", "2", "-shortest"] if info.get("sample_rate") else ["-an"])
        cmd.append(str(out))
        proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=600)
        if proc.returncode or not out.is_file():
            out.unlink(missing_ok=True)
            raise HTTPException(500, "Conversion failed: " + proc.stderr.decode(errors="replace")[-400:])
        got = count_frames(out)
        if got != n:
            out.unlink(missing_ok=True)
            raise HTTPException(500, f"Conversion produced {got} frames instead of {n}; nothing was changed.")
        keep = ds / ORIGINALS
        keep.mkdir(exist_ok=True)
        cap = caption_of(src)
        shutil.move(str(src), keep / src.name)
        dest = src.with_suffix(".mp4")
        if dest.exists() or any(p != src and p.is_file() and kind_of(p) and p.stem.casefold() == src.stem.casefold() for p in ds.iterdir()):
            dest = free_media_name(ds, dest.name)
        out.rename(dest)
        if cap.is_file() and cap != dest.with_suffix(".txt"):
            cap.rename(dest.with_suffix(".txt"))
        give_back(ds)
        return {"file": dest.name, "frames": n, "size": f"{tw}x{th}", "original": f"{ORIGINALS}/{src.name}"}

    def _stem(text: str) -> str:
        return (re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_-") or "clip")[:60]

    @router.post("/api/datasets/{name}/trim/{filename}")
    async def trim(name: str, filename: str, body: TrimBody, staged: bool = False) -> dict[str, Any]:
        """Cut each segment into its own clip (GPU encode), then delete the source video.
        h3_ready: 24 fps, length snapped DOWN to the H3 grid (22..124 frames), sides cropped to
        multiples of 32 with the long side <= 1024, audio 32 kHz stereo."""
        ds = ds_path(name)
        guard_busy(name)
        guard_jobs(name, filename, staged)
        src = item_path(ds, filename, staged)
        if not src.is_file() or kind_of(src) != "video":
            raise HTTPException(404, "Video not found.")
        info = probe(src)
        dur = info.get("duration") or 0
        if not info.get("width") or not dur:
            raise HTTPException(422, "Could not read this video.")
        limit = H3_MAX_TRIM if body.h3_ready else MAX_TRIM
        plans = []
        for i, seg in enumerate(sorted(body.segments, key=lambda s: s.start), 1):
            start, end = max(0.0, seg.start), min(seg.end, dur)
            length = end - start
            if length < MIN_TRIM - 1e-3 or length > limit + 0.02:
                raise HTTPException(422, f"Trim {i} is {length:.2f}s; it must be {MIN_TRIM:g}-{limit:.2f}s.")
            frames = None
            if body.h3_ready:
                frames = max([f for f in H3_FRAMES if f <= round(length * 24)], default=0)
                if frames < 22:
                    raise HTTPException(422, f"Trim {i} is too short for H3 (needs at least 22 frames, ~0.92s).")
            plans.append((start, length, frames))
        w, h = info["width"], info["height"]
        has_audio = bool(info.get("sample_rate"))
        base = _stem(src.stem)
        src_cap = caption_of(src) if not staged else None
        caption = src_cap.read_text(encoding="utf-8") if src_cap and src_cap.is_file() else None
        made: list[dict[str, Any]] = []
        try:
            for start, length, frames in plans:
                n = 1
                while (ds / f"{base}_trim{n:02d}.mp4").exists():
                    n += 1
                out = ds / f"{base}_trim{n:02d}.mp4"
                tmp = ds / f".{out.stem}.tmp.mp4"
                if body.h3_ready:
                    scale = min(1.0, 1024 / max(w, h))
                    tw, th = int(w * scale) // 32 * 32, int(h * scale) // 32 * 32
                    sw, sh = int(round(w * scale / 2) * 2), int(round(h * scale / 2) * 2)
                    # tpad clones the last frame if the source runs out (a trim ending right at the
                    # video's end used to come out one frame short = off the H3 grid).
                    vf = f"fps=24,scale_vaapi=w={sw}:h={sh},hwdownload,format=nv12,crop={tw}:{th},tpad=stop_mode=clone:stop_duration=1,hwupload"
                    limit_args = ["-frames:v", str(frames)]
                    # apad + -shortest: audio is padded/cut to exactly the video's length
                    audio = ["-af", "apad", "-c:a", "aac", "-ar", "32000", "-ac", "2", "-shortest"] if has_audio else ["-an"]
                else:
                    vf = f"scale_vaapi=w={w // 2 * 2}:h={h // 2 * 2}"
                    limit_args = ["-t", f"{length:.3f}"]
                    audio = ["-c:a", "aac", "-b:a", "192k"] if has_audio else ["-an"]
                cmd = ["ffmpeg", "-y", "-loglevel", "error", "-vaapi_device", "/dev/dri/renderD128", *VAAPI,
                       "-ss", f"{start:.3f}", "-i", str(src), "-vf", vf, *limit_args,
                       "-c:v", "h264_vaapi", "-qp", "18", *audio, *([] if body.h3_ready else ["-t", f"{length:.3f}"]), str(tmp)]
                proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=900)
                if proc.returncode or not tmp.is_file():
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(500, f"Cutting {start:.2f}s failed: " + proc.stderr.decode(errors="replace")[-300:])
                if frames and (got := count_frames(tmp)) != frames:
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(500, f"Cutting {start:.2f}s produced {got} frames instead of {frames}; nothing was changed.")
                tmp.rename(out)
                if caption:
                    out.with_suffix(".txt").write_text(caption, encoding="utf-8")
                made.append({"file": out.name, "start": round(start, 3), "frames": frames,
                             "seconds": round(frames / 24 if frames else length, 3)})
        except HTTPException:
            for m in made:  # all-or-nothing: don't leave half a set of trims behind
                (ds / m["file"]).unlink(missing_ok=True)
                (ds / m["file"]).with_suffix(".txt").unlink(missing_ok=True)
            raise
        src.unlink(missing_ok=True)
        if not staged:
            remove_captions(ds, src)       # its text was copied to every new clip already
            for old in (thumbs / name).glob(f"{filename}.*.jpg") if (thumbs / name).is_dir() else []:
                old.unlink(missing_ok=True)
        give_back(ds)
        return {"clips": made, "deleted": filename}

    @router.delete("/api/datasets/{name}/staged/{filename}")
    async def delete_staged(name: str, filename: str) -> dict[str, Any]:
        guard_jobs(name, filename, staged=True)
        path = item_path(ds_path(name), filename, staged=True)
        path.unlink(missing_ok=True)
        return {"deleted": filename}

    @router.post("/api/datasets/{name}/staged/{filename}/accept")
    async def accept_staged(name: str, filename: str) -> dict[str, Any]:
        """Move a waiting video into the dataset untrimmed (e.g. it is already a good clip)."""
        ds = ds_path(name)
        guard_busy(name)
        guard_jobs(name, filename, staged=True)
        src = item_path(ds, filename, staged=True)
        if not src.is_file():
            raise HTTPException(404, "Video not found in Waiting to be trimmed.")
        dest = free_media_name(ds, src.name)
        shutil.move(str(src), dest)
        give_back(ds)
        return {"file": dest.name}

    async def hls_slice(t: dict[str, Any], url: str, headers: dict[str, str], cookies: Path | None,
                        section: tuple[float, float], dest: Path) -> bool:
        """Download only the HLS segments that cover `section`, without seeking: ffmpeg 6.1 mangles
        seeks into byte-range fMP4 streams (Murrtube & co.). The segments (+ init) are fetched here
        with the page's Referer/cookies, joined, and ffmpeg just remuxes the local file.
        Returns False when the stream needs yt-dlp instead (separate audio, encryption)."""
        hdr = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36", **headers}
        host = urllib.parse.urlsplit(url).hostname or ""
        if cookies and cookies.is_file():
            jar = [ln.split("\t") for ln in cookies.read_text().splitlines() if ln and not ln.startswith("#")]
            pairs = [f"{c[5]}={c[6]}" for c in jar if len(c) >= 7 and (host == c[0].lstrip(".") or host.endswith("." + c[0].lstrip(".")))]
            if pairs:
                hdr["Cookie"] = "; ".join(pairs)
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), follow_redirects=True, headers=hdr) as c:
            text, base = (await c.get(url)).text, url
            if "#EXT-X-STREAM-INF" in text:
                if re.search(r"#EXT-X-MEDIA:[^\n]*TYPE=AUDIO[^\n]*URI=", text):
                    return False
                lines = text.splitlines()
                variants = [(int(m.group(1)) if (m := re.search(r"BANDWIDTH=(\d+)", lines[i])) else 0, lines[i + 1].strip())
                            for i in range(len(lines) - 1) if lines[i].startswith("#EXT-X-STREAM-INF")]
                base = urllib.parse.urljoin(url, max(variants)[1])
                text = (await c.get(base)).text
            if "#EXT-X-KEY" in text:
                return False
            init, segs, pos, dur, rng, last_end = None, [], 0.0, 0.0, None, {}
            for line in (ln.strip() for ln in text.splitlines()):
                if line.startswith("#EXT-X-MAP"):
                    uri = urllib.parse.urljoin(base, re.search(r'URI="([^"]+)"', line).group(1))
                    br = re.search(r'BYTERANGE="(\d+)@(\d+)"', line)
                    init = (uri, (int(br.group(2)), int(br.group(1))) if br else None)
                elif line.startswith("#EXTINF"):
                    dur = float(line.split(":", 1)[1].split(",")[0])
                elif line.startswith("#EXT-X-BYTERANGE"):
                    length, _, off = line.split(":", 1)[1].partition("@")
                    rng = (int(off) if off else None, int(length))
                elif line and not line.startswith("#"):
                    uri = urllib.parse.urljoin(base, line)
                    if rng is not None:
                        off = rng[0] if rng[0] is not None else last_end.get(uri, 0)
                        rng = (off, rng[1])
                        last_end[uri] = off + rng[1]
                    segs.append((pos, dur, uri, rng))
                    pos, dur, rng = pos + dur, 0.0, None
            keep = [sg for sg in segs if sg[0] < section[1] and sg[0] + sg[1] > section[0]]
            if not keep:
                return False
            raw = dest.with_suffix(".part.mp4" if init or keep[0][2].split("?")[0].endswith((".m4s", ".mp4")) else ".part.ts")
            t["state"] = "downloading"

            async def grab(uri: str, r: tuple[int, int] | None) -> bytes:
                resp = await c.get(uri, headers={"Range": f"bytes={r[0]}-{r[0] + r[1] - 1}"} if r else {})
                resp.raise_for_status()
                return resp.content
            try:
                with raw.open("wb") as out:
                    if init:
                        out.write(await grab(*init))
                    for i, (_, _, uri, r) in enumerate(keep):
                        out.write(await grab(uri, r))
                        t["percent"] = 95.0 * (i + 1) / len(keep)
                t["state"] = "merging"
                proc = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(raw),
                                                            "-c", "copy", "-movflags", "+faststart", str(dest),
                                                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, err = await proc.communicate()
                ok = proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 10_000
                if not ok:
                    t["note"] = err.decode(errors="replace")[-200:]
            finally:
                raw.unlink(missing_ok=True)
        if ok:
            t["file"] = dest.name
        else:
            dest.unlink(missing_ok=True)
        return ok

    # ---- download registry: every video ever downloaded, and where it went ----
    registry_file = data_dir / "downloads.json"

    def registry() -> dict[str, dict[str, Any]]:
        with suppress(OSError, ValueError):
            return json.loads(registry_file.read_text(encoding="utf-8"))
        return {}

    def downloaded(url: str) -> dict[str, Any] | None:
        """Registry entry for this video (any link form), with whether the file is still there."""
        e = registry().get(video_key(url))
        if e:
            e = {**e, "exists": (dataset_root / e["dataset"] / e["folder"] / e["file"]).is_file() if e.get("file") else False}
        return e

    def record(t: dict[str, Any], page_url: str, source: str) -> None:
        reg = registry()
        reg[video_key(page_url)] = {"url": page_url, "dataset": t["dataset"], "folder": INCOMING, "file": t["file"], "title": t.get("title"),
                                    "source": source, "at": time.strftime("%Y-%m-%d %H:%M"), "t": time.time(),
                                    "path": f"{t['dataset']}/{INCOMING}/{t['file']}"}
        tmp = registry_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(reg, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(registry_file)

    @router.get("/api/download-history")
    async def download_history(dataset: str = "", limit: int = 100) -> dict[str, Any]:
        items = sorted(({"key": k, **v} for k, v in registry().items() if not dataset or v["dataset"] == dataset), key=lambda e: -e.get("t", 0))[:limit]
        for e in items:
            e["exists"] = (dataset_root / e["dataset"] / e["folder"] / e["file"]).is_file() if e.get("file") else False
        return {"items": items}

    def start_download(name: str, url: str, section: tuple[float, float] | None = None, cookies: Path | None = None,
                       headers: dict[str, str] | None = None, title: str | None = None, page_url: str | None = None,
                       force: bool = False, source: str = "link") -> dict[str, Any]:
        """yt-dlp a video (any site it supports) into <dataset>/.incoming/; `section` = (start, end) s
        downloads only that part (cut at keyframes, the trimmer does the exact cut). `page_url` = the
        video's page when `url` is a raw stream. A video already in the registry is not downloaded
        again unless `force`: the task comes back as state "duplicate" with where it went."""
        ds = ds_path(name)
        task_id = os.urandom(8).hex()
        page = page_url or url
        prev = None if force else downloaded(page)
        if prev:
            downloads[task_id] = {"id": task_id, "dataset": name, "state": "duplicate", "percent": 100.0, "file": prev.get("file"), "error": None,
                                  "title": prev.get("title") or title, "url": page, "previous": prev}
            return downloads[task_id]
        downloads[task_id] = {"id": task_id, "dataset": name, "state": "starting", "percent": 0.0, "file": None, "error": None, "title": title, "url": page}
        incoming = ds / INCOMING
        incoming.mkdir(exist_ok=True)

        async def run():
            t = downloads[task_id]
            # A bare stream URL (index.m3u8) has no useful title: name the file after the page's title.
            base = (re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_")[:70] + "_" + hashlib.sha1(url.encode()).hexdigest()[:6]) if title else "%(title).70B_%(id)s"
            name_tpl = base + (f"_{int(section[0])}-{int(section[1])}s" if section else "") + ".%(ext)s"
            if headers and re.search(r"\.m3u8(\?|$)", url, re.I):
                # Stream the clip finder caught in the browser: fetch its segments ourselves (whole
                # video, or just the ones covering `section`) instead of letting ffmpeg seek.
                try:
                    dest = incoming / ((base if title else "stream_" + hashlib.sha1(url.encode()).hexdigest()[:8])
                                       + (f"_{int(section[0])}-{int(section[1])}s" if section else "") + ".mp4")
                    if await hls_slice(t, url, headers, cookies, section or (0.0, float("inf")), dest):
                        give_back(ds)
                        record(t, page, source)
                        t.update(state="done", percent=100.0, saved=f"{name}/{INCOMING}/{t['file']}")
                        return
                except Exception as exc:
                    t["note"] = f"segment slice failed ({str(exc)[:120]}); using yt-dlp"
            cmd = ["yt-dlp", "--newline", "--no-playlist", "--restrict-filenames",
                   "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "-S", "vcodec:h264,res,acodec:m4a",
                   "--merge-output-format", "mp4", "-o", str(incoming / name_tpl),
                   *(["--download-sections", f"*{section[0]:.2f}-{section[1]:.2f}"] if section else []),
                   *(["--cookies", str(cookies)] if cookies else []),     # clip finder: the job's browser session (age gates)
                   *[x for k, v in (headers or {}).items() for x in ("--add-header", f"{k}:{v}")],
                   "--print", "before_dl:TITLE %(title)s", "--print", "after_move:FILE %(filepath)s", url]
            try:
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                t["state"] = "downloading"
                tail: list[str] = []
                assert proc.stdout is not None
                while line := await proc.stdout.readline():
                    text = line.decode(errors="replace").strip()
                    tail = (tail + [text])[-8:]
                    if m := re.search(r"\[download\]\s+([\d.]+)%", text):
                        t["percent"] = float(m.group(1))
                    elif section and (m := re.search(r"time=(\d+):(\d+):([\d.]+)", text)):
                        # Section downloads go through ffmpeg, which reports time, not percent.
                        done = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                        t["percent"] = min(99.0, 100 * done / max(1.0, section[1] - section[0]))
                    elif text.startswith("TITLE "):
                        t["title"] = t["title"] or text[6:]
                    elif text.startswith("FILE "):
                        t["file"] = Path(text[5:]).name
                    elif "[Merger]" in text or "[VideoConvertor]" in text:
                        t["state"] = "merging"
                code = await proc.wait()
                if code or not t["file"]:
                    t.update(state="failed", error=" ".join(x for x in tail if "ERROR" in x)[-400:] or f"yt-dlp exited with {code}")
                else:
                    give_back(ds)
                    record(t, page, source)
                    t.update(state="done", percent=100.0, saved=f"{name}/{INCOMING}/{t['file']}")
            except Exception as exc:
                t.update(state="failed", error=str(exc)[:400])

        asyncio.create_task(run())
        return downloads[task_id]

    @router.post("/api/datasets/{name}/youtube", status_code=202)
    async def youtube(name: str, body: YoutubeBody) -> dict[str, Any]:
        url = body.url.strip()
        if not re.match(r"https?://", url):
            raise HTTPException(422, "Paste a full http(s) link.")
        return start_download(name, url, force=body.force, source="youtube box")

    @router.get("/api/downloads/{task_id}")
    async def download_status(task_id: str) -> dict[str, Any]:
        if task_id not in downloads:
            raise HTTPException(404, "Unknown download.")
        return downloads[task_id]

    # For the clip finder (finder.py).
    router.ds_path = ds_path
    router.start_download = start_download
    router.download_status = downloads.get
    router.downloaded = downloaded
    router.video_key = video_key
    return router
