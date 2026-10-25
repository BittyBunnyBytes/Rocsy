"""Download queue: resumable (.part + HTTP Range), SHA-256 checked, disk-space
checked, persisted across restarts (an interrupted download comes back paused).

Civitai answers an unauthenticated download of a gated file with a small HTML
login page and status 200, so HTML responses fail with a hint about the API key.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import struct
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

ACTIVE = ("queued", "downloading", "verifying")
CHUNK = 4 * 1024 * 1024
# Automatic retry: network drops, timeouts, rate limits, server errors and short files are retried
# (resuming from the .part) after these waits; login walls, missing files and a full disk are not.
RETRY_WAITS = (15, 45, 120, 300, 600)
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
_PARTIAL_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_UNSATISFIED_RANGE = re.compile(r"bytes \*/(\d+)")
PROGRESS_RESET = 256 * 1024 ** 2  # an attempt that got this far starts the retry count over


class EarlyAccess(RuntimeError):
    """Civitai answered 403 {"error": "Early Access", "deadline": ...}: not bought (yet)."""
    def __init__(self, until: float):
        super().__init__("Early Access")
        self.until = until


def _early_access(response: httpx.Response) -> EarlyAccess | None:
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("error") != "Early Access":
        return None
    try:
        until = datetime.fromisoformat(str(data.get("deadline") or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        until = 0.0  # no deadline: paid for good
    return EarlyAccess(until)


class Transient(RuntimeError):
    """A failure worth retrying by itself. `wait` overrides the backoff (Retry-After)."""
    def __init__(self, message: str, wait: float | None = None):
        super().__init__(message)
        self.wait = wait


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return min(900.0, max(5.0, float(response.headers.get("retry-after", ""))))
    except ValueError:
        return None

# The installed llama.cpp build cannot load GGUF files with these architectures.
UNSUPPORTED_LLAMA_ARCHITECTURES = {"glm5next"}


def llama_gguf_architecture(header: bytes) -> str:
    """Read the architecture from the beginning of a GGUF file."""
    if len(header) < 36 or header[:4] != b"GGUF":
        raise ValueError("Could not read a valid GGUF header from this download")
    # GGUF v3 starts with magic, version, tensor count, KV count. The first
    # metadata entry in llama.cpp GGUFs is general.architecture.
    key_size = struct.unpack_from("<Q", header, 24)[0]
    end = 32 + key_size
    if key_size > 256 or end + 12 > len(header) or header[32:end] != b"general.architecture":
        raise ValueError("Could not determine this GGUF model's architecture")
    if struct.unpack_from("<I", header, end)[0] != 8:
        raise ValueError("Invalid GGUF architecture metadata")
    value_size = struct.unpack_from("<Q", header, end + 4)[0]
    if value_size > 256 or end + 12 + value_size > len(header):
        raise ValueError("Invalid GGUF architecture metadata")
    arch = header[end + 12:end + 12 + value_size].decode("ascii", errors="replace")
    if arch in UNSUPPORTED_LLAMA_ARCHITECTURES:
        raise ValueError(f"{arch} GGUF models cannot run in the installed llama.cpp build. Download a supported model or update llama.cpp first.")
    return arch


# The architectures ComfyUI-GGUF loads (its loader.py IMG_ARCH_LIST / TXT_ARCH_LIST). Files without one are
# stable-diffusion.cpp GGUFs (Unsloth's MiniMax-H3, leejet's): ComfyUI-GGUF refuses those.
COMFY_GGUF_ARCHES = {
    "diffusion": {"flux", "sd1", "sdxl", "sd3", "aura", "hidream", "cosmos", "ltxv", "hyvid", "wan", "lumina2", "qwen_image"},
    "text": {"t5", "t5encoder", "llama", "qwen2vl", "qwen3", "qwen3vl", "gemma3"},
}
_GGUF_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def gguf_architecture(header: bytes) -> str | None:
    """general.architecture from a GGUF's metadata (any position), or None when the file has none.
    Raises ValueError when the bytes aren't a GGUF header or end before the metadata does."""
    if len(header) < 24 or header[:4] != b"GGUF":
        raise ValueError("Could not read a valid GGUF header from this download")
    entries = struct.unpack_from("<Q", header, 16)[0]
    offset = 24

    def string() -> str:
        nonlocal offset
        size = struct.unpack_from("<Q", header, offset)[0]
        if size > len(header):
            raise ValueError("Invalid GGUF metadata")
        offset += 8
        text = header[offset:offset + size].decode("utf-8", errors="replace")
        offset += size
        return text

    def skip(kind: int) -> None:
        nonlocal offset
        if kind == 8:
            string()
        elif kind == 9:
            item, count = struct.unpack_from("<IQ", header, offset)
            offset += 12
            if item in _GGUF_SIZES:
                offset += _GGUF_SIZES[item] * count
            else:
                for _ in range(count):
                    skip(item)
        elif kind in _GGUF_SIZES:
            offset += _GGUF_SIZES[kind]
        else:
            raise ValueError("Invalid GGUF metadata")

    try:
        for _ in range(entries):
            key = string()
            kind = struct.unpack_from("<I", header, offset)[0]
            offset += 4
            if key == "general.architecture" and kind == 8:
                return string()
            skip(kind)
            if offset > len(header):
                raise struct.error
    except struct.error:
        raise ValueError("The GGUF metadata is larger than the part read") from None
    return None


async def check_comfy_gguf(url: str, headers: dict, kind: str) -> str:
    """Read a GGUF's metadata (first MB) and refuse files ComfyUI-GGUF can't load as a diffusion model
    ("diffusion") or text encoder ("text")."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        async with client.stream("GET", url, headers={**headers, "Range": "bytes=0-1048575"}) as response:
            response.raise_for_status()
            header = b""
            async for chunk in response.aiter_bytes(65536):
                header += chunk
                if len(header) >= 1048576:
                    break
    arch = gguf_architecture(header)
    if arch is None:
        raise ValueError("this GGUF is a stable-diffusion.cpp file (no architecture tag), which ComfyUI can't load. "
                         "Pick a ComfyUI GGUF of the same model")
    if arch not in COMFY_GGUF_ARCHES[kind]:
        what = "diffusion model" if kind == "diffusion" else "text encoder"
        raise ValueError(f"ComfyUI-GGUF can't load a {arch!r} GGUF as a {what}")
    return arch


async def check_llama_gguf(url: str, headers: dict) -> str:
    """Read only the GGUF header before queuing a llama-swap model."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        async with client.stream("GET", url, headers={**headers, "Range": "bytes=0-4095"}) as response:
            response.raise_for_status()
            header = b""
            async for chunk in response.aiter_bytes(4096):
                header += chunk
                if len(header) >= 4096:
                    break
    return llama_gguf_architecture(header)


class Downloads:
    def __init__(self, data_dir: Path, root: Path, tokens: Callable[[], dict], on_done: Callable[[dict], None]):
        self.path = data_dir / "downloads.json"
        self.root = root  # ComfyUI models/
        self.tokens = tokens
        self.on_done = on_done
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.wake = asyncio.Event()
        self.concurrency = 2
        # (dest, link path) -> a storage-location folder for the real file ("" = the models folder).
        self.store_for: Callable[[str, Path], str] = lambda dest, link: ""
        try:
            self.jobs = {job["id"]: job for job in json.loads(self.path.read_text())}
        except (OSError, ValueError):
            self.jobs = {}
        for job in self.jobs.values():
            if job["state"] in ACTIVE:
                job["state"], job["speed"] = "paused", 0  # interrupted by a restart

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        recent = sorted(self.jobs.values(), key=lambda j: j["created"])[-300:]
        tmp.write_text(json.dumps(recent))
        os.replace(tmp, self.path)

    def list(self) -> list[dict]:
        return sorted(self.jobs.values(), key=lambda j: j["created"], reverse=True)

    def active_count(self) -> int:
        return sum(job["state"] in ACTIVE for job in self.jobs.values())

    def target(self, dest: str, name: str) -> Path:
        folder = (self.root / dest).resolve()
        if not str(folder).startswith(str(self.root.resolve()) + os.sep) and folder != self.root.resolve():
            raise ValueError("Destination must be inside ComfyUI's models folder")
        safe = Path(name).name
        if not safe or safe.startswith("."):
            raise ValueError("Bad file name")
        return folder / safe

    def add(self, *, url: str, dest: str, name: str, size: int, sha256: str, source: str, group: str,
            link: dict, replace: bool = False, after: dict | None = None, not_before: float = 0,
            update_batch: str = "", update_expected: int = 0, supersede: str = "",
            supersede_identity: dict | None = None, supersede_files: list[dict] | None = None) -> dict:
        path = self.target(dest, name)
        if path.exists() and not replace:
            if sha256 and _sidecar_sha(path) == sha256:
                raise ValueError(f"{path.name} is already downloaded")
            raise ValueError(f"{dest}/{path.name} already exists")
        job = {"id": uuid.uuid4().hex, "group": group, "url": url, "dest": dest, "name": path.name, "size": size,
               "sha256": sha256.lower(), "source": source, "link": link, "replace": replace, "state": "queued",
               "bytes": 0, "speed": 0, "error": "", "created": time.time(), "finished": 0,
               "after": after or {}, "note": ""}
        store = self.store_for(dest, path)
        if store:
            # Settings -> Storage: the file goes to that location, models/<dest>/<name> becomes a link to it.
            real = self.real_path({**job, "store": store, "real_name": path.name})
            if real.exists() or real.with_name(real.name + ".part").exists():
                real = real.with_name(f"{path.stem}-{job['id'][:6]}{path.suffix}")
            job.update(store=store, real_name=real.name)
        if update_batch:
            job.update(update_batch=update_batch, update_expected=update_expected,
                       supersede=supersede, supersede_identity=supersede_identity or {},
                       supersede_files=supersede_files or [], verified=False)
        if not_before > time.time():  # Civitai early access: starts by itself when it becomes free
            job.update(state="scheduled", not_before=not_before)
        self.jobs[job["id"]] = job
        self.save()
        self.wake.set()
        return job

    def action(self, job_id: str, action: str) -> dict:
        job = self.jobs[job_id]
        if action in ("pause", "cancel") and job["state"] in ACTIVE:
            job.pop("retry", None)
            task = self.tasks.pop(job_id, None)
            if task:
                task.cancel()
            job["state"], job["speed"] = ("paused" if action == "pause" else "cancelled"), 0
        elif action == "cancel" and job["state"] == "paused":
            job["state"] = "cancelled"
        elif action == "cancel" and job["state"] == "scheduled":
            job["state"] = "cancelled"
        elif action == "start" and job["state"] == "scheduled":  # "Try now" (e.g. after buying it on Civitai)
            job["state"], job["error"] = "queued", ""
            self.wake.set()
        elif action in ("resume", "retry") and job["state"] in ("paused", "failed", "cancelled"):
            job["state"], job["error"] = "queued", ""
            job.pop("retry", None)  # a manual retry starts with a fresh set of automatic ones
            self.wake.set()
        elif action == "clear" and job["state"] not in ACTIVE:
            self.jobs.pop(job_id)
        if job.get("state") == "cancelled" or action == "clear":
            part = self._part(job)
            part.unlink(missing_ok=True)
        self.save()
        return job

    def real_path(self, job: dict) -> Path:
        """Where the bytes go: models/<dest>/<name>, or the storage location's copy of that path."""
        if not job.get("store"):
            return self.target(job["dest"], job["name"])
        self.target(job["dest"], job["real_name"])        # same name/folder checks as the models folder
        return Path(job["store"]) / job["dest"] / Path(job["real_name"]).name

    def _part(self, job: dict) -> Path:
        real = self.real_path(job)
        return real.with_name(real.name + ".part")

    async def run(self) -> None:
        while True:
            for job in self.jobs.values():
                if job["state"] == "scheduled" and job.get("not_before", 0) <= time.time():
                    job["state"] = "queued"
            for job in sorted(self.jobs.values(), key=lambda j: j["created"]):
                running = sum(1 for t in self.tasks.values() if not t.done())
                if running >= self.concurrency:
                    break
                if job["state"] == "queued" and (job.get("retry") or {}).get("at", 0) > time.time():
                    continue  # waiting out its retry delay
                if job["state"] == "queued" and job["id"] not in self.tasks:
                    job["state"] = "downloading"
                    self.tasks[job["id"]] = asyncio.create_task(self._download(job))
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), 2)
            except asyncio.TimeoutError:
                pass

    def _schedule_retry(self, job: dict, exc: Exception, part: Path, start_bytes: int) -> None:
        retry = job.get("retry") or {"attempt": 0}
        got = (part.stat().st_size if part.exists() else 0) - start_bytes
        if got >= PROGRESS_RESET:
            retry["attempt"] = 0  # it was moving: a flaky connection on a big file shouldn't use up the retries
        error = _describe(exc)
        if retry["attempt"] >= len(RETRY_WAITS):
            job.update(state="failed", speed=0,
                       error=f"{error} · gave up after {len(RETRY_WAITS)} automatic retries"
                             + (f" (Retry resumes from {size_label(part)})" if part.exists() and part.stat().st_size else ""))
            job.pop("retry", None)
            return
        wait = getattr(exc, "wait", None) or RETRY_WAITS[retry["attempt"]]
        retry.update(attempt=retry["attempt"] + 1, of=len(RETRY_WAITS), at=time.time() + wait, error=error)
        job["retry"] = retry
        job.update(state="queued", speed=0, error="")

    def _headers(self, job: dict) -> dict:
        tokens = self.tokens()
        headers = {"User-Agent": "Rocsy-ModelHub/1.0"}
        if job["source"].startswith("civitai") and tokens.get("civitai"):
            headers["Authorization"] = f"Bearer {tokens['civitai']}"
        if job["source"] == "hf" and tokens.get("hf"):
            headers["Authorization"] = f"Bearer {tokens['hf']}"
        return headers

    async def _download(self, job: dict) -> None:
        final = self.target(job["dest"], job["name"])
        part = self._part(job)
        start_bytes = part.stat().st_size if part.exists() else 0
        try:
            if job.get("store") and not os.path.isdir(job["store"]):
                raise RuntimeError(f"The storage location {job['store']} isn't reachable (drive unplugged?)")
            final.parent.mkdir(parents=True, exist_ok=True)
            part.parent.mkdir(parents=True, exist_ok=True)
            have = part.stat().st_size if part.exists() else 0
            free = shutil.disk_usage(part.parent).free
            if job["size"] and job["size"] - have > free - 2 * 1024 ** 3:
                raise RuntimeError(f"Not enough disk space ({free / 1024 ** 3:.0f} GB free)")
            digest = hashlib.sha256()
            if have:
                job["state_detail"] = "Checking the partial file…"
                await asyncio.to_thread(_hash_into, digest, part)
            headers = self._headers(job)
            if have:
                headers["Range"] = f"bytes={have}-"
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, read=120)) as client:
                async with client.stream("GET", job["url"], headers=headers) as response:
                    if response.status_code == 403 and "json" in response.headers.get("content-type", ""):
                        await response.aread()
                        locked = _early_access(response)
                        if locked is not None:
                            raise locked
                    if response.status_code in (401, 403):
                        raise RuntimeError("Access denied: this file needs an API key/token (Manager -> Settings), "
                                           "or accepting the model's terms on its page")
                    if response.status_code == 416:
                        # A 416 alone does not mean the local partial is complete.
                        # In particular, setup downloads can have no known size or hash.
                        match = _UNSATISFIED_RANGE.fullmatch(response.headers.get("content-range", ""))
                        total = int(match[1]) if match else 0
                        if not have or not total or have != total or (job["size"] and job["size"] != total):
                            raise RuntimeError("The server rejected the resume range without confirming the partial file is complete; cancel and retry to start over")
                        job["size"] = total
                        response_ok = False
                    elif response.status_code in RETRY_STATUS:
                        raise Transient(f"{'Rate limited' if response.status_code == 429 else 'Server error'} "
                                        f"(HTTP {response.status_code})", _retry_after(response))
                    else:
                        response.raise_for_status()
                        response_ok = True
                    if response_ok and "text/html" in response.headers.get("content-type", ""):
                        raise RuntimeError("Got a web page instead of the file (Civitai login-gated): add your Civitai API key")
                    if response_ok and have and response.status_code == 200:
                        have, digest = 0, hashlib.sha256()  # server ignored Range: start over
                    if response_ok:
                        if response.status_code == 206:
                            match = _PARTIAL_RANGE.fullmatch(response.headers.get("content-range", ""))
                            if not match:
                                raise RuntimeError("The server returned a partial response without a valid Content-Range")
                            start, end, total = map(int, match.groups())
                            if start != have or end < start or end >= total:
                                raise RuntimeError("The server returned bytes from the wrong resume range")
                            length = response.headers.get("content-length")
                            if length is not None and int(length) != end - start + 1:
                                raise RuntimeError("The server returned an inconsistent partial response length")
                            if job["size"] and job["size"] != total:
                                raise RuntimeError("The remote file size changed while resuming; cancel and retry to start over")
                        else:
                            total = int(response.headers.get("content-length") or 0) + have
                        if total and not job["size"]:
                            job["size"] = total
                        mode = "ab" if have else "wb"
                        job["bytes"], last_t, last_b = have, time.monotonic(), have
                        job.pop("state_detail", None)
                        with part.open(mode) as handle:
                            async for chunk in response.aiter_bytes(CHUNK):
                                handle.write(chunk)
                                digest.update(chunk)
                                job["bytes"] += len(chunk)
                                now = time.monotonic()
                                if now - last_t >= 1:
                                    job["speed"] = int((job["bytes"] - last_b) / (now - last_t))
                                    last_t, last_b = now, job["bytes"]
            job["state"], job["speed"] = "verifying", 0
            if job["size"] and part.stat().st_size != job["size"]:
                raise Transient(f"Incomplete download: expected {job['size']} bytes, got {part.stat().st_size}")
            actual = digest.hexdigest()
            if job["sha256"] and actual != job["sha256"]:
                part.unlink(missing_ok=True)
                raise Transient("SHA-256 mismatch: the download was corrupted or changed upstream")
            # A locally computed digest alone cannot authenticate the source.
            # Superseded files are retired only when upstream supplied a matching SHA-256.
            job["verified"] = bool(job["sha256"])
            if job["replace"] and final.exists():
                identity = job.get("supersede_identity") or {}
                if identity and not _same_identity(final, identity):
                    raise RuntimeError("The installed file changed during download; the update was not applied")
                # Keep the old file recoverable until every file in this update
                # is verified and post-processed. Never overwrite an older backup.
                old = final.with_name(final.name + ".old")
                if old.exists():
                    old = final.with_name(f"{final.name}.{job['id'][:8]}.old")
                final.rename(old)
                job["previous"] = str(old.relative_to(self.root))
            if final.exists():
                stem, ext = final.stem, final.suffix
                final = final.with_name(f"{stem}-{job['id'][:6]}{ext}")
                job["name"] = final.name
            try:
                if job.get("store"):
                    real = self.real_path(job)
                    if real.exists():
                        real = real.with_name(f"{real.stem}-{job['id'][:6]}{real.suffix}")
                        job["real_name"] = real.name
                    part.rename(real)
                    os.symlink(real, final)
                else:
                    part.rename(final)
            except Exception:
                if job.get("previous") and not final.exists():
                    old.rename(final)
                    job.pop("previous", None)
                raise
            final.with_name(final.name + ".sha256").write_text(actual)
            job.update(state="complete", finished=time.time(), sha256=actual, bytes=final.stat().st_size,
                       path=str(final.relative_to(self.root)))
            job.pop("retry", None)
            await self.on_done(job)
        except asyncio.CancelledError:
            raise
        except EarlyAccess as exc:
            if exc.until > time.time():  # not bought: wait for the creator's deadline, then try again
                job.update(state="scheduled", not_before=exc.until + 300, speed=0, error="",
                           note="Early access on Civitai: starts by itself when it becomes free")
            else:
                job.update(state="failed", speed=0, error="Paid on Civitai: buy it with Buzz on its page, then Retry")
        except (Transient, httpx.TransportError) as exc:  # dropped connection, timeout, 5xx, 429, short file
            self._schedule_retry(job, exc, part, start_bytes)
        except Exception as exc:  # noqa: BLE001 - shown on the job
            job.update(state="failed", error=str(exc)[:400] or exc.__class__.__name__, speed=0)
            job.pop("retry", None)
        finally:
            self.tasks.pop(job["id"], None)
            self.save()
            self.wake.set()


def size_label(part: Path) -> str:
    have = part.stat().st_size if part.exists() else 0
    return f"{have / 1024 ** 3:.1f} GB" if have >= 1024 ** 3 else f"{have / 1024 ** 2:.0f} MB"


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "Connection timed out"
    if isinstance(exc, httpx.TransportError):
        return f"Connection dropped ({exc.__class__.__name__})"
    return str(exc)[:400] or exc.__class__.__name__


def _hash_into(digest, path: Path) -> None:
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)


def _sidecar_sha(path: Path) -> str:
    try:
        return path.with_name(path.name + ".sha256").read_text().split()[0].lower()
    except (OSError, IndexError):
        return ""


def _same_identity(path: Path, identity: dict) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    return all(getattr(stat, field) == value for field, value in identity.items())
