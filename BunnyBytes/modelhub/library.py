"""Installed models/LoRAs, where they came from, and whether upstream has a newer
version. Checking never downloads anything: updates are applied from the
Manager tab only.

A file is linked to its source by (first match): a download made here, the
ComfyUI LoRA Manager's .metadata.json, its SHA-256 (sidecar or computed on
request) looked up on Civitai, a Hugging Face repo whose name matches its
llm/<folder>, or a link the user pastes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

import httpx

import sources

WEIGHT_EXT = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".sft")
FOLDERS = {"loras": "lora", "checkpoints": "model", "diffusion_models": "model", "unet": "model",
           "text_encoders": "model", "vae": "model", "llm": "model", "classifiers": "model", "upscale_models": "model",
           "audio_encoders": "model", "clip_vision": "model", "controlnet": "model", "vae_approx": "model",
           "model_patches": "model", "latent_upscale_models": "model", "diffusers": "model"}
SHARD_REST = re.compile(r"-(0000[2-9]|000[1-9]\d|00[1-9]\d\d)-of-\d{5}\.(gguf|safetensors)$")
MIN_SIZE = 1024 * 1024
TRAINER_URL = os.getenv("TRAINER_UPSTREAM", "http://127.0.0.1:8500").rstrip("/") + "/Trainer"


class Library:
    def __init__(self, data_dir: Path, root: Path, tokens):
        self.root = root
        self.tokens = tokens
        self.links_path = data_dir / "library.json"
        self.hashes_path = data_dir / "hashes.json"
        self.links: dict[str, dict] = _load(self.links_path)
        self.hashes: dict[str, dict] = _load(self.hashes_path)
        self.items: list[dict] = []
        self.task = {"running": False, "kind": "", "current": "", "done": 0, "total": 0, "message": ""}
        self.last_check = self.links.pop("__last_check__", {}).get("at", 0) if "__last_check__" in self.links else 0

    def save(self) -> None:
        _dump(self.links_path, {**self.links, "__last_check__": {"at": self.last_check}})
        _dump(self.hashes_path, self.hashes)

    # ---- inventory -------------------------------------------------------
    def scan(self) -> list[dict]:
        items = []
        for folder, kind in FOLDERS.items():
            base = self.root / folder
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if not name.lower().endswith(WEIGHT_EXT) or SHARD_REST.search(name) or name.startswith("ggml-vocab-"):
                        continue
                    path = Path(dirpath) / name
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    if stat.st_size < MIN_SIZE:
                        continue
                    rel = str(path.relative_to(self.root))
                    items.append({"path": rel, "folder": folder, "name": name, "kind": kind, "size": stat.st_size,
                                  "mtime": stat.st_mtime, "sha256": self._known_sha(path, stat)})
        for item in items:
            if item["path"] not in self.links:
                link = self._lora_manager_link(self.root / item["path"])
                if link:
                    self.links[item["path"]] = link
        present = {item["path"] for item in items}
        for path in [p for p in self.links if p not in present and not p.startswith("__")]:
            self.links.pop(path)  # deleted from disk
        self.items = items
        self.save()
        return items

    def _known_sha(self, path: Path, stat) -> str:
        cached = self.hashes.get(str(path.relative_to(self.root)))
        if cached and cached["size"] == stat.st_size and cached["mtime"] == stat.st_mtime:
            return cached["sha256"]
        sidecar = path.with_name(path.name + ".sha256")
        meta = path.with_name(path.stem + ".metadata.json")
        sha = ""
        try:
            sha = sidecar.read_text().split()[0].lower()
        except (OSError, IndexError):
            try:
                sha = str(json.loads(meta.read_text()).get("sha256") or "").lower()
            except (OSError, ValueError):
                pass
        if re.fullmatch(r"[0-9a-f]{64}", sha):
            self.hashes[str(path.relative_to(self.root))] = {"size": stat.st_size, "mtime": stat.st_mtime, "sha256": sha}
            return sha
        return ""

    def _lora_manager_link(self, path: Path) -> dict | None:
        try:
            meta = json.loads(path.with_name(path.stem + ".metadata.json").read_text())
        except (OSError, ValueError):
            return None
        civ = meta.get("civitai") or {}
        if not civ.get("modelId") or not civ.get("id"):
            return None
        return {"source": "civitai", "model_id": str(civ["modelId"]), "version_id": str(civ["id"]),
                "version_name": civ.get("name") or "", "name": (civ.get("model") or {}).get("name") or meta.get("model_name") or "",
                "base": civ.get("baseModel") or meta.get("base_model") or "", "linked_by": "LoRA Manager"}

    def installed_bases(self) -> list[dict]:
        """Base models on this machine (main model files only, not text encoders):
        the Civitai base of linked files, else a guess from the file name."""
        found: dict[str, list[str]] = {}
        for item in self.items:
            if item["folder"] not in ("diffusion_models", "checkpoints", "unet", "diffusers"):
                continue
            link = self.links.get(item["path"]) or {}
            manual = link.get("base_manual")
            base = manual or (link.get("base") if link.get("base") not in (None, "", "Other") else _guess_base(item["name"]))
            if base:
                found.setdefault(base, []).append(item["name"])
        return sorted(({"base": b, "files": sorted(f)} for b, f in found.items()),
                      key=lambda x: (-len(x["files"]), x["base"]))

    def view(self) -> list[dict]:
        out = []
        for item in self.items:
            link = self.links.get(item["path"])
            out.append({**item, "link": link, "status": _status(link)})
        return out

    def link(self, rel: str, link: dict) -> None:
        self.links[rel] = {**link, "checked_at": time.time(), "update": None}
        self.save()

    # ---- identification ---------------------------------------------------
    def hash_file(self, rel: str) -> str:
        path = self.root / rel
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(8 * 1024 * 1024):
                digest.update(chunk)
        sha = digest.hexdigest()
        self.hashes[rel] = {"size": stat.st_size, "mtime": stat.st_mtime, "sha256": sha}
        try:  # LoRA Manager reads the same sidecar
            path.with_name(path.name + ".sha256").write_text(sha)
        except OSError:
            pass
        return sha

    async def identify(self, paths: list[str], compute: bool) -> None:
        """Link unknown files: by known/computed SHA-256 on Civitai, LLM folders by HF repo name."""
        if self.task["running"]:
            return
        self.task.update(running=True, kind="identify", done=0, total=len(paths), message="")
        found = 0
        try:
            async with httpx.AsyncClient() as client:
                for rel in paths:
                    item = next((i for i in self.items if i["path"] == rel), None)
                    if not item:
                        continue
                    self.task["current"] = item["name"]
                    link = None
                    if item["folder"] == "llm":
                        link = await self._guess_hf(client, rel)
                    if not link:
                        sha = item["sha256"]
                        if not sha and compute:
                            self.task["current"] = f"Hashing {item['name']}…"
                            sha = await asyncio.to_thread(self.hash_file, rel)
                            item["sha256"] = sha
                        if sha:
                            try:
                                version = await sources.civitai_by_hash(client, sha, self.tokens().get("civitai", ""))
                            except httpx.HTTPError:
                                version = None
                            if version:
                                link = {"source": "civitai", "model_id": str(version.get("modelId")),
                                        "version_id": str(version.get("id")), "version_name": version.get("name") or "",
                                        "name": (version.get("model") or {}).get("name") or "", "base": version.get("baseModel") or "",
                                        "nsfw": bool((version.get("model") or {}).get("nsfw")), "linked_by": "SHA-256"}
                            await asyncio.sleep(0.4)
                            # Renamed or re-hosted files: search both sites by the file name, keep only a hash match.
                            if not link:
                                self.task["current"] = f"Searching for {item['name']}…"
                                link = await self._search_by_name(client, item, sha)
                    if not link and item["kind"] == "lora":
                        link = await self._trainer_output(client, item)
                    if not link and item.get("sha256"):
                        self.links[rel] = {"source": "", "not_found": True, "sha256": item["sha256"], "checked_at": time.time()}
                    if link:
                        found += 1
                        self.link(rel, link)
                    self.task["done"] += 1
            self.task["message"] = f"Linked {found} of {len(paths)}"
        finally:
            self.task.update(running=False, current="")
            self.save()

    async def _search_by_name(self, client: httpx.AsyncClient, item: dict, sha: str) -> dict | None:
        """Hugging Face and Civitai searched by the file's name; a hit counts only when the
        remote file's SHA-256 equals ours (same-architecture files share sizes)."""
        stem = re.sub(r"\.(safetensors|gguf|pth|pt|ckpt|sft)$", "", item["name"], flags=re.I)
        queries = list(dict.fromkeys([stem, re.sub(r"[_.-](bf16|fp16|fp32|fp8\w*|int8\w*|nvfp4\w*|q\d\w*)$", "", stem, flags=re.I)]))
        for query in queries:
            try:
                found = (await client.get(f"{sources.HF}/api/models", params={"search": query[:90], "limit": "8"},
                                          headers=sources.UA, timeout=20)).json()
            except (httpx.HTTPError, ValueError):
                found = []
            for model in found[:8]:
                try:
                    info = (await client.get(f"{sources.HF}/api/models/{model['id']}", params={"blobs": "true"},
                                             headers=sources.UA, timeout=20)).json()
                except (httpx.HTTPError, ValueError):
                    continue
                hit = next((x for x in info.get("siblings") or [] if (x.get("lfs") or {}).get("sha256") == sha), None)
                if hit:
                    return {"source": "hf", "repo": model["id"], "file": hit["rfilename"], "name": model["id"],
                            "revision": info.get("sha", ""), "linked_by": "HF search + SHA-256"}
        # Civitai names files <ModelName>_<VersionName>[_<fileId>]: search by the model part.
        first = stem.split("_")[0]
        key = self.tokens().get("civitai", "")
        headers = {**sources.UA, **({"Authorization": f"Bearer {key}"} if key else {})}
        for query in dict.fromkeys([re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", first), first]):
            try:
                data = (await client.get("https://civitai.com/api/v1/models", params={"query": query, "limit": "10", "nsfw": "true"},
                                         headers=headers, timeout=30)).json()
            except (httpx.HTTPError, ValueError):
                continue
            await asyncio.sleep(0.3)
            for model in data.get("items") or []:
                for version in model.get("modelVersions") or []:
                    if any(((f.get("hashes") or {}).get("SHA256") or "").lower() == sha for f in version.get("files") or []):
                        return {"source": "civitai", "model_id": str(model["id"]), "version_id": str(version["id"]),
                                "version_name": version.get("name") or "", "name": model.get("name") or "",
                                "base": version.get("baseModel") or "", "nsfw": bool(model.get("nsfw")),
                                "linked_by": "Civitai search + SHA-256"}
        return None

    async def _trainer_output(self, client: httpx.AsyncClient, item: dict) -> dict | None:
        """A LoRA trained here: the name of one of the Trainer's runs is in the file name."""
        try:
            jobs = (await client.get(TRAINER_URL + "/api/jobs", timeout=10)).json()
        except (httpx.HTTPError, ValueError):
            return None
        jobs = jobs.get("jobs", jobs) if isinstance(jobs, dict) else jobs
        stem = re.sub(r"[^a-z0-9]", "", item["name"].lower().rsplit(".", 1)[0])
        for job in jobs if isinstance(jobs, list) else []:
            run = re.sub(r"[^a-z0-9]", "", str(job.get("name") or "").lower())
            if len(run) >= 4 and run in stem and job.get("state") == "complete":
                return {"source": "local", "origin": "trainer", "name": item["name"].rsplit(".", 1)[0],
                        "base": "MiniMax H3" if job.get("model") == "minimax_h3" else str(job.get("model") or ""),
                        "note": f"Trained here: Trainer run \"{job.get('name')}\" on the {job.get('dataset')} dataset",
                        "linked_by": "Trainer runs"}
        return None

    async def _guess_hf(self, client: httpx.AsyncClient, rel: str) -> dict | None:
        parts = Path(rel).parts
        if len(parts) < 3:
            return None
        folder = parts[1]
        try:
            response = await client.get(f"{sources.HF}/api/models", params={"search": folder, "limit": "20"},
                                        headers=sources.UA, timeout=20)
            matches = [m["id"] for m in response.json() if m["id"].split("/")[-1].lower() == folder.lower()]
        except (httpx.HTTPError, ValueError):
            return None
        file = "/".join(parts[2:])
        size = (self.root / rel).stat().st_size
        # Forks share repo names: take the one that has this exact file at this size.
        for repo in matches[:6]:
            try:
                info = (await client.get(f"{sources.HF}/api/models/{repo}", params={"blobs": "true"},
                                         headers=sources.UA, timeout=20)).json()
            except (httpx.HTTPError, ValueError):
                continue
            sibling = next((x for x in info.get("siblings") or [] if x.get("rfilename") == file), None)
            if sibling and int(sibling.get("size") or 0) == size:
                return {"source": "hf", "repo": repo, "file": file, "name": repo, "revision": info.get("sha", ""),
                        "linked_by": "repo + file match"}
        return None

    # ---- updates ------------------------------------------------------------
    def _already_installed(self, update: dict, model: dict) -> None:
        """A newer version that is already on disk under another name isn't an update to fetch."""
        if not update.get("available"):
            return
        version = next((v for v in model["versions"] if v["id"] == update["version_id"]), None)
        shas = {f["sha256"] for v in (version or {}).get("variants", []) for f in v["files"] if f.get("sha256")}
        names = {Path(f["name"]).name for v in (version or {}).get("variants", []) for f in v["files"]}
        for item in self.items:
            if (item["sha256"] and item["sha256"] in shas) or item["name"] in names:
                update.update(available=False, installed_as=item["path"])
                return

    async def check_updates(self, paths: list[str] | None = None) -> None:
        if self.task["running"]:
            return
        targets = [rel for rel, link in self.links.items()
                   if not rel.startswith("__") and link.get("source") and link["source"] != "local" and (paths is None or rel in paths)
                   and any(i["path"] == rel for i in self.items)]
        self.task.update(running=True, kind="updates", done=0, total=len(targets), message="")
        cache: dict[str, dict] = {}
        updates = 0
        try:
            async with httpx.AsyncClient() as client:
                for rel in targets:
                    link = self.links[rel]
                    self.task["current"] = Path(rel).name
                    try:
                        if link["source"].startswith("civitai"):
                            key = link["model_id"]
                            if key not in cache:
                                cache[key] = await sources.civitai_model(client, "civitai_red" if link.get("nsfw") else "civitai",
                                                                         key, self.tokens().get("civitai", ""))
                                await asyncio.sleep(0.3)
                            link["update"] = _civitai_update(link, cache[key])
                            self._already_installed(link["update"], cache[key])
                            link["name"] = link.get("name") or cache[key]["name"]
                        elif link.get("local_changes"):
                            # A copy edited here (e.g. a rewritten header): an upstream hash never matches it.
                            link["update"] = {"available": False, "note": "Changed locally; compare upstream by hand"}
                        elif link["source"] == "hf":
                            key = "hf:" + link["repo"]
                            if key not in cache:
                                cache[key] = await sources.hf_model(client, link["repo"], self.tokens().get("hf", ""))
                            item = next(i for i in self.items if i["path"] == rel)
                            link["update"] = _hf_update(link, cache[key], item)
                        link.pop("error", None)
                    except httpx.HTTPStatusError as exc:
                        link["error"] = "Removed upstream" if exc.response.status_code == 404 else f"HTTP {exc.response.status_code}"
                    except (httpx.HTTPError, ValueError, KeyError, StopIteration) as exc:
                        link["error"] = str(exc)[:120] or "Check failed"
                    link["checked_at"] = time.time()
                    updates += bool((link.get("update") or {}).get("available"))
                    self.task["done"] += 1
            self.last_check = time.time()
            self.task["message"] = f"{updates} update{'s' if updates != 1 else ''} available"
        finally:
            self.task.update(running=False, current="")
            self.save()


# File name -> Civitai base model name, for model files Civitai can't identify.
BASE_PATTERNS = [
    (r"krea.?2", "Krea 2"), (r"qwen.?image.?2[._]?1", "Qwen 2.1"), (r"qwen.?image", "Qwen"),
    (r"minimax.?h3|h3.?eros|(^|[_\-])h3[_\-]", "MiniMax H3"), (r"ltx.?2[._]?5", "LTXV 2.5"),
    (r"ltx.?2[._]?3", "LTXV 2.3"), (r"ltx.?2(?![._]?\d)", "LTXV2"), (r"ltx.?video", "LTXV"), (r"anima", "Anima"),
    (r"wan.?2[._]?2.*i2v", "Wan Video 2.2 I2V-A14B"), (r"wan.?2[._]?2.*t2v", "Wan Video 2.2 T2V-A14B"),
    (r"wan.?2[._]?2.*5b", "Wan Video 2.2 TI2V-5B"), (r"wan.?2[._]?1", "Wan Video"),
    (r"flux.?1.?krea", "Flux.1 Krea"), (r"flux.?1.*(dev|d\b)|flux1-dev", "Flux.1 D"), (r"flux.?2.*klein.*9b", "Flux.2 Klein 9B"),
    (r"flux.?2.*klein.*4b", "Flux.2 Klein 4B"), (r"z.?image.?turbo", "ZImageTurbo"), (r"z.?image", "ZImageBase"),
    (r"hidream", "HiDream"), (r"chroma", "Chroma"), (r"hunyuan.?video", "Hunyuan Video"), (r"illustrious|noob", "Illustrious"),
    (r"pony", "Pony"), (r"sdxl|juggernaut.?xl|realvis.?xl", "SDXL 1.0"), (r"sd.?1[._]?5|v1-5", "SD 1.5"),
    (r"yue", "YuE2"), (r"ace.?step", "ACE Audio"),
]


def _guess_base(name: str) -> str:
    lower = name.lower()
    return next((base for pattern, base in BASE_PATTERNS if re.search(pattern, lower)), "")


def _stem(version_name: str) -> set[str]:
    """Words of a version name without version numbers: "aesthetic-v1.1" -> {"aesthetic"}."""
    words = re.split(r"[^a-z0-9]+", version_name.lower())
    return {w for w in words if w and not re.fullmatch(r"v?\d+[a-z]?|ver|version|v|alt|final|new|update[d]?", w)}


def _civitai_update(link: dict, model: dict) -> dict:
    """A newer version counts as an update only on the same base model and the
    same line: its name keeps every word of the installed one (H3 V1 -> H3 VBVR
    Pro, aesthetic-v1.1 -> aesthetic-v1.2). Pages also list parallel variants
    (base / turbo, DEV / DISTILLED, REF2VA / FL2VA); those are only mentioned."""
    versions = model["versions"]
    ids = [v["id"] for v in versions]
    installed = link.get("version_id")
    if installed not in ids:
        return {"available": False, "note": "Installed version is no longer listed"}
    current = versions[ids.index(installed)]
    newer = versions[:ids.index(installed)]
    base = link.get("base") or current["base"]
    words = _stem(current["name"])
    same_line = [v for v in newer if (not base or v["base"] == base) and words <= _stem(v["name"])]
    others = [f"{v['name']} ({v['base']})" for v in newer if v not in same_line]
    if not same_line:
        return {"available": False, "other_versions": others}
    latest = same_line[0]
    return {"available": True, "version_id": latest["id"], "version_name": latest["name"], "published": latest["published"],
            "count": len(same_line), "base": latest["base"], "early_access": latest.get("early_access", False),
            "other_versions": others}


def _hf_update(link: dict, model: dict, item: dict) -> dict:
    files = {f["name"]: f for v in model["versions"][0]["variants"] for f in v["files"] + v.get("companions", [])}
    upstream = files.get(link.get("file", ""))
    link["revision_latest"] = model.get("revision", "")
    if not upstream:
        return {"available": False, "note": "File no longer in the repo"}
    changed = (upstream["sha256"] and item["sha256"] and upstream["sha256"] != item["sha256"]) or \
              (not item["sha256"] and upstream["size"] and upstream["size"] != item["size"])
    if not changed:
        return {"available": False}
    return {"available": True, "version_id": model.get("revision", ""), "version_name": f"main @ {model.get('revision', '')[:7]}",
            "published": model["versions"][0]["published"], "count": 1, "size": upstream["size"]}


def _status(link: dict | None) -> str:
    if not link or not link.get("source"):
        return "not_found" if link and link.get("not_found") else "unknown"
    if link["source"] == "local":
        return "local"
    if link.get("error"):
        return "error"
    update = link.get("update")
    if update is None:
        return "unchecked"
    return "update" if update.get("available") else "current"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _dump(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)
