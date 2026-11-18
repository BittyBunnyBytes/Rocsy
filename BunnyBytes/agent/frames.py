"""`$R frames`: reference stills from a video, in one call.

  frames "breaking bad bathtub acid scene" [--n 6] [--at 1:35] [--url URL]
      yt-dlp search (or the URL), a low-res download, N frames spread over the video (or around --at), each labelled
      with its number and time, tiled into ONE contact sheet the agent looks at with vision.
  frames pick SLUG K [--name NAME]
      the full-size frame K of that sheet, for `share` (the app makes it an attachment for --ref).

Replaces the scripts agents kept writing by hand for this (yt-dlp search flags, nested shell quoting, ffmpeg
hstack/vstack sizes, vision calls racing a background download).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MANAGED = Path("/opt/rocsy-tools/yt-dlp/yt-dlp")
SEARCH_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 200
LONG = 600                   # longer videos: only a 10-minute section is downloaded


class FramesError(Exception):
    pass


def _ytdlp() -> list[str]:
    return ([sys.executable, str(MANAGED)] if MANAGED.is_file() else [sys.executable, "-m", "yt_dlp"]) + ["--ignore-config", "--no-warnings"]


def _run(args: list[str], timeout: int) -> str:
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise FramesError(f"timed out after {timeout} s") from exc
    if process.returncode:
        tail = [l for l in (err or out or "").strip().splitlines() if l.strip()][-2:]
        raise FramesError("; ".join(tail)[-400:] or f"exit {process.returncode}")
    return out or ""


def seconds(value: Any) -> float | None:
    """"95", "1:35", "00:01:35.5" -> seconds."""
    if value in (None, ""):
        return None
    try:
        parts = [float(p) for p in str(value).strip().split(":")]
    except ValueError:
        raise FramesError(f"--at must be seconds or m:ss, not {value!r}") from None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def clock(t: float) -> str:
    t = max(0, int(round(t)))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def clock1(t: float) -> str:
    """1:23.4 (tenths: precise enough to cut on)."""
    tenths = int(round(max(0.0, float(t)) * 10))
    return f"{clock(tenths // 10)}.{tenths % 10}"


def search(query: str, count: int = 8) -> list[dict[str, Any]]:
    out = _run(_ytdlp() + ["--flat-playlist", "-J", f"ytsearch{count}:{query}"], SEARCH_TIMEOUT)
    try:
        entries = json.loads(out).get("entries") or []
    except ValueError as exc:
        raise FramesError("yt-dlp returned no search results") from exc
    hits = []
    for e in entries:
        if not isinstance(e, dict) or e.get("live_status") in ("is_live", "is_upcoming"):
            continue
        url = e.get("url") or (f"https://www.youtube.com/watch?v={e['id']}" if e.get("id") else "")
        if url:
            hits.append({"url": url, "title": str(e.get("title") or "")[:120], "duration": e.get("duration")})
    return hits


_STOP = {"the", "a", "an", "of", "in", "on", "and", "scene", "clip", "season", "episode", "with", "to", "from", "at", "is"}


def choose(hits: list[dict[str, Any]], query: str = "") -> dict[str, Any] | None:
    """The title that matches the most search words, then a scene clip rather than a full episode, compilation or
    10-second short; ties keep the search order."""
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _STOP and len(w) > 2}

    def score(item: tuple[int, dict[str, Any]]) -> tuple[float, float, int]:
        i, h = item
        d = h.get("duration") or 0
        title = set(re.findall(r"[a-z0-9]+", h["title"].lower()))
        fit = 0 if 20 <= d <= 600 else 1 if 600 < d <= 1800 else 2 if d else 1.5
        return (-len(words & title), fit, i)
    return min(enumerate(hits), key=score)[1] if hits else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "frames"


def probe(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], 30)
    try:
        return float(out.strip())
    except ValueError as exc:
        raise FramesError("the download has no readable duration") from exc


def link_info(url: str) -> dict[str, Any]:
    """Title and length of a link before downloading anything (a full episode or stream isn't downloaded whole)."""
    try:
        out = _run(_ytdlp() + ["--no-playlist", "--skip-download", "--print", "%(duration)s\x1e%(id)s\x1e%(title)j", url], SEARCH_TIMEOUT)
    except FramesError as exc:
        raise FramesError(f"yt-dlp can't read that link ({exc})") from None
    line = next((l for l in reversed(out.split("\n")) if l.count("\x1e") == 2), "")   # splitlines() breaks on \x1e too
    dur, vid, raw = (line.split("\x1e") + ["", "", ""])[:3]
    try:
        title = json.loads(raw)
    except ValueError:
        title = raw
    # Titles can be several lines of decoration: keep the first line with letters in it.
    title = next((l.strip() for l in str(title or "").splitlines() if re.search(r"\w{2}", l)), "")
    try:
        duration = float(dur)
    except ValueError:
        duration = None
    return {"url": url, "title": title[:120], "duration": duration, "id": vid if vid not in ("NA", "None") else ""}


def _frame_times(length: float, n: int, at_s: float | None, offset: float = 0.0) -> list[float]:
    """Times in the source: spread over it (past intro/outro cards), or ±(n/2) s around --at."""
    if at_s is not None:
        centre = min(max(0.0, at_s - offset), length)
        span = min(length, max(4.0, n * 1.5))
        start = max(0.0, min(centre - span / 2, length - span))
        times = [start + span * i / (n - 1) for i in range(n)]
    else:
        times = [length * (0.08 + 0.84 * i / (n - 1)) for i in range(n)]
    return [min(t, max(0.0, length - 0.25)) for t in times]       # the very last instant has no frame to decode


def make_sheet(query: str, workspace: Path, sid: str, n: int = 6, at: Any = None, url: str = "",
               local: Path | None = None, label: str = "") -> dict[str, Any]:
    """local: a video already here (an upload, a finished job, a workspace file); url: a link yt-dlp reads;
    otherwise query is searched."""
    n = max(2, min(12, int(n or 6)))
    at_s = seconds(at)
    hits: list[dict[str, Any]] = []
    if local is not None:
        if not local.is_file():
            raise FramesError(f"{label or local} isn't a file")
        pick = {"url": "", "title": label or local.name, "duration": None}
    elif url:
        if not re.match(r"(?i)https?://", url):
            raise FramesError("--url must be an http(s) link")
        pick = link_info(url)
    else:
        if not query.strip():
            raise FramesError('give a search, e.g. frames "breaking bad bathtub acid scene"')
        hits = search(query)
        pick = choose(hits, query)
        if not pick:
            raise FramesError(f"no videos found for {query!r}; try other words")
    if at_s is not None and pick.get("duration") and at_s > pick["duration"] + 1:
        raise FramesError(f"--at {clock(at_s)} is past the end of the video ({clock(pick['duration'])})")
    base = workspace / sid / "frames"
    slug = _slug(query or pick["title"] or (f"video-{pick['id']}" if pick.get("id") else "") or "frames")
    folder = base / slug
    k = 2
    while folder.exists():
        folder, k = base / f"{slug}-{k}", k + 1
    tmp = folder / ".dl"
    tmp.mkdir(parents=True)
    try:
        offset = 0.0
        fmt = "bv*[height<=480][ext=mp4]/bv*[height<=480]/b[height<=480]/wv*/b"
        duration = pick.get("duration") or 0
        if local is not None:
            src: str = str(local)
            length = probe(local)
        elif duration > LONG and at_s is None:
            # A long video: seek in the stream for each frame instead of downloading it all.
            try:
                stream = (_run(_ytdlp() + ["--no-playlist", "-f", fmt, "-g", pick["url"]], SEARCH_TIMEOUT).strip().splitlines() or [""])[0]
            except FramesError as exc:
                raise FramesError(f"yt-dlp can't stream that video ({exc})") from None
            if not stream:
                raise FramesError("yt-dlp gave no stream for that video")
            src, length = stream, float(duration)
        else:
            cmd = _ytdlp() + ["--no-playlist", "--no-progress", "--socket-timeout", "20", "--retries", "2",
                              "-f", fmt, "--max-filesize", "300M", "-o", str(tmp / "src.%(ext)s")]
            if at_s is not None:
                offset = max(0.0, at_s - 20)
                cmd += ["--download-sections", f"*{offset:g}-{at_s + 20:g}"]
            try:
                _run(cmd + [pick["url"]], DOWNLOAD_TIMEOUT)
            except FramesError as exc:
                raise FramesError(f"download failed ({exc})") from None
            sources = [p for p in tmp.iterdir() if p.is_file() and p.stat().st_size > 1024 and not p.name.endswith(".part")]
            if not sources:
                raise FramesError("the download saved no video")
            src = str(max(sources, key=lambda p: p.stat().st_size))
            length = probe(Path(src))
        if at_s is not None and offset == 0.0 and at_s > length + 1:
            raise FramesError(f"--at {clock(at_s)} is past the end of the video ({clock(length)})")
        times = _frame_times(length, n, at_s, offset)
        frames = []
        for i, t in enumerate(times, 1):
            full = folder / f"frame-{i:02d}.jpg"
            _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{t:.2f}", "-i", src,
                  "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(full)], 60)
            if not full.is_file():
                raise FramesError(f"no picture at {clock(t + offset)} (is it a video with a picture track?)")
            label = f"{i}  {clock(t + offset)}".replace(":", r"\:")
            _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(full), "-vf",
                  "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2,"
                  f"drawtext=fontfile={FONT}:text='{label}':x=10:y=10:fontsize=30:fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=6",
                  str(tmp / f"cell-{i:02d}.jpg")], 60)
            frames.append({"n": i, "time": round(t + offset, 2), "path": full})
        cols = 3 if n <= 9 else 4
        rows = math.ceil(n / cols)
        sheet = folder / "sheet.jpg"
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-framerate", "1", "-i", str(tmp / "cell-%02d.jpg"),
              "-vf", f"tile={cols}x{rows}:padding=6:color=black", "-frames:v", "1", "-q:v", "3", str(sheet)], 60)
        meta = {"slug": folder.name, "query": query, "url": pick["url"], "title": pick["title"], "duration": round(length + offset, 1),
                "at": at_s, "frames": [{"n": f["n"], "time": f["time"], "file": f["path"].name} for f in frames], "made": time.time()}
        (folder / "meta.json").write_text(json.dumps(meta, indent=1))
        return {**meta, "sheet": sheet, "folder": folder, "others": [h for h in hits if h["url"] != pick["url"]][:4], "cols": cols}
    except Exception:
        if not any(folder.glob("sheet.jpg")):
            shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def frame_path(workspace: Path, sid: str, slug: str, k: Any) -> tuple[Path, dict[str, Any]]:
    folder = workspace / sid / "frames" / _slug(slug) if not re.fullmatch(r"[a-z0-9-]+", slug or "") else workspace / sid / "frames" / slug
    try:
        meta = json.loads((folder / "meta.json").read_text())
    except (OSError, ValueError):
        made = sorted(p.name for p in (workspace / sid / "frames").glob("*/meta.json")) if (workspace / sid / "frames").is_dir() else []
        raise FramesError(f"no frame sheet {slug!r}" + (f"; this chat has: {', '.join(m.split('/')[0] for m in made)}" if made else "")) from None
    try:
        k = int(str(k).lstrip("sS#"))            # shot sheets number panels S1, S2, ...
    except (TypeError, ValueError):
        raise FramesError("give the frame number from the sheet, e.g. frames pick SLUG 3 (or S12 on a shot sheet)") from None
    hit = next((f for f in meta["frames"] if f["n"] == k), None)
    if not hit:
        raise FramesError(f"sheet {slug} has frames 1-{len(meta['frames'])}")
    return folder / hit["file"], meta


# --- shots: where the cuts are ------------------------------------------------------------------------------------
SHOT_PAGE = 16               # panels per shot sheet (4 x 4)
SHOT_MAX = 600
SHOT_DOWNLOAD_LIMIT = 3 * 3600


def download(url: str, folder: Path, height: int = 360, sections: list[tuple[float, float]] | None = None,
             timeout: int = DOWNLOAD_TIMEOUT * 2) -> Path:
    """A link as a local video (whole, or the given sections as one file each is NOT supported here: one section)."""
    folder.mkdir(parents=True, exist_ok=True)
    cmd = _ytdlp() + ["--no-playlist", "--no-progress", "--socket-timeout", "20", "--retries", "2",
                      "-f", f"bv*[height<={height}][ext=mp4]+ba/b[height<={height}]/bv*[height<={height}]+ba/b",
                      "--merge-output-format", "mp4", "--max-filesize", "1500M", "-o", str(folder / "src.%(ext)s")]
    for a, b in sections or []:
        cmd += ["--download-sections", f"*{a:g}-{b:g}", "--force-keyframes-at-cuts"]
    try:
        _run(cmd + [url], timeout)
    except FramesError as exc:
        raise FramesError(f"download failed ({exc})") from None
    found = [p for p in folder.iterdir() if p.is_file() and p.name.startswith("src.") and p.stat().st_size > 1024
             and not p.name.endswith(".part")]
    if not found:
        raise FramesError("the download saved no video")
    return max(found, key=lambda p: p.stat().st_size)


def cut_points(src: Path, workdir: Path, threshold: float = 0.32) -> list[float]:
    """Times where the picture changes shot (ffmpeg's scene score on a small 8 fps copy: about ±0.1 s)."""
    log = workdir / f"scenes-{threshold}.txt"
    try:
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(src), "-an", "-sn", "-vf",
              f"fps=8,scale=192:-2,select='gt(scene\\,{threshold})',metadata=print:file={log}", "-f", "null", "-"], 1800)
        text = log.read_text() if log.is_file() else ""
    finally:
        log.unlink(missing_ok=True)
    return [float(t) for t in re.findall(r"pts_time:([0-9.]+)", text)]


def shot_list(cuts: list[float], length: float, min_len: float = 0.6) -> list[tuple[float, float]]:
    edges = [0.0] + [c for c in cuts if 0.2 < c < length - 0.2] + [length]
    shots: list[list[float]] = []
    for a, b in zip(edges, edges[1:]):
        if shots and b - a < min_len:          # a flash / dissolve frame: part of the shot before it
            shots[-1][1] = b
        else:
            shots.append([a, b])
    if len(shots) > 1 and shots[0][1] - shots[0][0] < min_len:
        shots[1][0] = shots.pop(0)[0]
    return [(round(a, 2), round(b, 2)) for a, b in shots]


def _panel(src: Path, t: float, full: Path, cell: Path, label: str) -> None:
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{t:.2f}", "-i", str(src),
          "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(full)], 60)
    if not full.is_file():
        raise FramesError(f"no picture at {clock(t)}")
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(full), "-vf",
          "scale=480:270:force_original_aspect_ratio=decrease,pad=480:270:(ow-iw)/2:(oh-ih)/2,"
          f"drawtext=fontfile={FONT}:text='{label}':x=8:y=8:fontsize=22:fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=5",
          str(cell)], 60)


def make_shots(workspace: Path, sid: str, local: Path | None = None, url: str = "", label: str = "",
               transcript: list[list[Any]] | None = None) -> dict[str, Any]:
    """Every shot of a video: a numbered list (start-end, with the lines spoken in it when a transcript is given) and
    contact sheets of one panel per shot, SHOT_PAGE per sheet."""
    from concurrent.futures import ThreadPoolExecutor
    info = {"url": url, "title": label, "duration": None, "id": ""}
    if url and local is None:
        info = link_info(url)
        if info.get("duration") and info["duration"] > SHOT_DOWNLOAD_LIMIT:
            raise FramesError(f"that video is {clock(info['duration'])} long; shots work on videos up to {clock(SHOT_DOWNLOAD_LIMIT)}")
    base = workspace / sid / "frames"
    slug = _slug(f"shots-{info['title'] or label or (info.get('id') or 'video')}")
    folder, k = base / slug, 2
    while folder.exists():
        folder, k = base / f"{slug}-{k}", k + 1
    tmp = folder / ".dl"
    tmp.mkdir(parents=True)
    try:
        src = local if local is not None else download(url, tmp)
        length = probe(src)
        cuts = cut_points(src, tmp)
        if len(cuts) > SHOT_MAX:
            cuts = cut_points(src, tmp, 0.45)
        shots = shot_list(cuts, length)[:SHOT_MAX]
        lines = transcript or []

        def spoken(a: float, b: float) -> str:
            return " ".join(str(c[2]).strip() for c in lines if c[0] < b and c[1] > a)[:160]

        jobs = []
        for i, (a, b) in enumerate(shots, 1):
            t = a + min(max((b - a) * 0.4, 0.05), max(0.0, b - a - 0.1))
            jobs.append((i, a, b, t))
        with ThreadPoolExecutor(6) as pool:
            list(pool.map(lambda j: _panel(src, j[3], folder / f"shot-{j[0]:03d}.jpg", tmp / f"cell-{j[0]:03d}.jpg",
                                           f"S{j[0]}  {clock1(j[1])}-{clock1(j[2])}".replace(":", r"\:")), jobs))
        pages = []
        for page, start in enumerate(range(0, len(jobs), SHOT_PAGE), 1):
            chunk = jobs[start:start + SHOT_PAGE]
            listing = tmp / f"page-{page}.txt"
            listing.write_text("".join(f"file '{tmp / f'cell-{j[0]:03d}.jpg'}'\n" for j in chunk))
            rows = math.ceil(len(chunk) / 4)
            sheet = folder / f"shots-p{page}.jpg"
            _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                  "-vf", f"tile=4x{rows}:padding=5:color=black", "-frames:v", "1", "-q:v", "3", str(sheet)], 120)
            pages.append({"page": page, "sheet": sheet, "first": chunk[0][0], "last": chunk[-1][0]})
        rows_txt = [f"S{i}  {clock1(a)}-{clock1(b)}  ({b - a:.1f} s)" + (f"  | {spoken(a, b)}" if spoken(a, b) else "")
                    for i, a, b, _ in jobs]
        (folder / "shots.txt").write_text(f"{info['title'] or label or url}\n" + "\n".join(rows_txt) + "\n")
        meta = {"slug": folder.name, "query": "", "url": url, "title": info["title"] or label, "duration": round(length, 1),
                "at": None, "shots": [[a, b] for _, a, b, _ in jobs], "made": time.time(),
                "frames": [{"n": i, "time": round(t, 2), "file": f"shot-{i:03d}.jpg"} for i, _, _, t in jobs]}
        (folder / "meta.json").write_text(json.dumps(meta, indent=1))
        return {**meta, "folder": folder, "pages": pages, "rows": rows_txt, "listing": folder / "shots.txt"}
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- cut: pieces that can be H3 video references ---------------------------------------------------------------
REF_MIN, REF_MAX = 5.0, 15.0


def parse_ranges(text: str) -> list[tuple[float, float]]:
    """"0:05-0:15, 1:20-1:52" -> [(5, 15), (80, 112)]."""
    out = []
    for part in re.split(r"\s*,\s*", str(text or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"([0-9:.]+)\s*[-–]\s*([0-9:.]+)", part)
        if not m:
            raise FramesError(f"ranges look like 0:05-0:15 (several: 0:05-0:15,1:20-1:50), not {part!r}")
        a, b = seconds(m.group(1)), seconds(m.group(2))
        if b is None or a is None or b <= a:
            raise FramesError(f"{part}: the end must be after the start")
        out.append((a, b))
    if not out:
        raise FramesError("give the time ranges, e.g. cut att3 0:05-0:15")
    return out


def pieces(ranges: list[tuple[float, float]], length: float | None) -> list[tuple[float, float]]:
    """Ranges as H3-sized pieces: shorter than 5 s grows to 5 s, longer than 15 s splits into equal 5-15 s pieces."""
    out = []
    for a, b in ranges:
        if length:
            if a >= length:
                raise FramesError(f"{clock(a)} is past the end of the video ({clock(length)})")
            b = min(b, length)
        if b - a < REF_MIN:
            b = a + REF_MIN
            if length and b > length:
                a, b = max(0.0, length - REF_MIN), length
        count = math.ceil((b - a) / REF_MAX - 1e-9)
        step = (b - a) / count
        out += [(round(a + step * i, 2), round(a + step * (i + 1), 2)) for i in range(count)]
    if len(out) > 24:
        raise FramesError(f"that makes {len(out)} pieces; cut at most 24 at a time (6 minutes)")
    return out


def make_cuts(workspace: Path, sid: str, ranges: str, local: Path | None = None, url: str = "", label: str = "",
              audio: bool = True) -> dict[str, Any]:
    info = {"url": url, "title": label, "duration": None}
    if url and local is None:
        info = link_info(url)
    length = probe(local) if local is not None else info.get("duration")
    parts = pieces(parse_ranges(ranges), length)
    base = workspace / sid / "cuts"
    stem = _slug(info["title"] or label or "cut")[:30]
    base.mkdir(parents=True, exist_ok=True)
    made = []
    shared_tmp = base / f".dl-{os.getpid()}-all"
    shared: tuple[Path, float] | None = None
    try:
        if local is None and max(b for _, b in parts) - min(a for a, _ in parts) <= 420:   # close together: one download for all
            first, last = max(0.0, min(a for a, _ in parts) - 1), max(b for _, b in parts) + 1
            shared = (download(url, shared_tmp, 1080, [(first, last)]), first)
        for i, (a, b) in enumerate(parts, 1):
            out = base / f"{stem}-{clock(a).replace(':', '')}-{clock(b).replace(':', '')}.mp4"
            tmp = base / f".dl-{os.getpid()}-{i}"
            try:
                if local is not None:
                    src, offset = local, 0.0
                elif shared:
                    src, offset = shared
                else:
                    src, offset = download(url, tmp, 1080, [(max(0.0, a - 1), b + 1)]), max(0.0, a - 1)
                _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{a - offset:.3f}", "-i", str(src),
                      "-t", f"{b - a:.3f}", "-map", "0:v:0", *(["-map", "0:a:0?"] if audio else ["-an"]),
                      "-vf", "scale='min(1920,iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                      *(["-c:a", "aac", "-b:a", "160k"] if audio else []), "-movflags", "+faststart", str(out)], 300)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if not out.is_file() or out.stat().st_size < 1024:
                raise FramesError(f"the piece {clock(a)}-{clock(b)} came out empty")
            made.append({"start": a, "end": b, "path": out})
    finally:
        shutil.rmtree(shared_tmp, ignore_errors=True)
    return {"title": info["title"] or label or url, "url": url, "pieces": made, "length": length}


# --- transcript: the audio for Vid's GPU Whisper ---------------------------------------------------------------

def audio_file(workspace: Path, sid: str, local: Path | None = None, url: str = "") -> tuple[Path, dict[str, Any]]:
    """The source's audio as a small AAC file (44.1 kHz stereo: Vid can split vocals from music first)."""
    folder = workspace / sid / "transcripts"
    folder.mkdir(parents=True, exist_ok=True)
    info = {"url": url, "title": local.name if local else "", "duration": None}
    tmp = folder / f".dl-{os.getpid()}-{int(time.time())}"
    try:
        if local is None:
            info = link_info(url)
            tmp.mkdir()
            try:
                _run(_ytdlp() + ["--no-playlist", "--no-progress", "-f", "ba/b", "-o", str(tmp / "src.%(ext)s"), url], DOWNLOAD_TIMEOUT * 2)
            except FramesError as exc:
                raise FramesError(f"download failed ({exc})") from None
            found = [p for p in tmp.iterdir() if p.is_file() and not p.name.endswith(".part")]
            if not found:
                raise FramesError("the download saved nothing")
            src = max(found, key=lambda p: p.stat().st_size)
        else:
            src = local
        out = folder / f".audio-{os.getpid()}-{int(time.time() * 1000)}.m4a"
        try:
            _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src), "-vn", "-ac", "2", "-ar", "44100",
                  "-c:a", "aac", "-b:a", "128k", str(out)], 600)
        except FramesError as exc:
            raise FramesError(f"no readable audio ({exc})") from None
        if not out.is_file() or out.stat().st_size < 1024:
            out.unlink(missing_ok=True)
            raise FramesError("that video has no audio track")
        return out, info
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def transcript_lines(cues: list[list[Any]]) -> list[str]:
    return [f"[{clock1(c[0])}-{clock1(c[1])}] {str(c[2]).strip()}" for c in cues if str(c[2]).strip()]
