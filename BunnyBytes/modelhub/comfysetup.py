"""Manager → Core Backend → ComfyUI: what ComfyUI is missing, and where to get it.

Three lists, all read-only (installing is the caller's job):
- defaults: the files ComfyUI and ComfyUI-Manager recommend (TAESD preview decoders,
  the common VAEs, two upscalers), offered only for a folder that has no model files;
- models: files a saved workflow (ComfyUI user/default/workflows) loads that aren't
  in models/. The URL comes from the workflow itself (node properties.models, the
  format ComfyUI's templates use) or ComfyUI-Manager's model list, by file name;
- nodes: node types a saved workflow uses that ComfyUI doesn't have (/object_info),
  grouped by the custom node pack that provides them (ComfyUI-Manager's
  extension-node-map + custom-node-list, the node's own cnr_id/aux_id).

Every download/install request names an entry by key; URLs never come from the browser
(except a GitHub repo URL typed into Install from GitHub, which hostctl validates again).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

WORKFLOWS = Path(os.getenv("MODELHUB_WORKFLOWS_DIR", "/comfy-workflows"))
COMFY = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
MANAGER_RAW = "https://raw.githubusercontent.com/Comfy-Org/ComfyUI-Manager/main/"
MANAGER_FILES = ("model-list.json", "custom-node-list.json", "extension-node-map.json")
MANAGER_TTL = 24 * 3600
MODEL_EXT = re.compile(r"\.(safetensors|sft|ckpt|pt|pth|bin|gguf|onnx)$", re.I)
GITHUB_REPO = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
# Workflows kept for reference rather than use (same folders support.py skips).
SIDE_DIRS = {"archive", "imports", "api examples"}
# Frontend-only nodes: they never appear in /object_info.
VIRTUAL = {"Note", "MarkdownNote", "PrimitiveNode", "Reroute", "Group", "Comment"}

# ComfyUI's README: "download taesd_decoder.pth, taesdxl_decoder.pth, taesd3_decoder.pth and
# taef1_decoder.pth and place them in the models/vae_approx folder" for high-quality previews.
# The rest are ComfyUI-Manager model-list entries every SD/SDXL/Flux workflow example uses.
DEFAULTS = [
    {"folder": "vae_approx", "name": "taesd_decoder.pth", "title": "TAESD decoder (SD 1.x/2.x previews)"},
    {"folder": "vae_approx", "name": "taesdxl_decoder.pth", "title": "TAESDXL decoder (SDXL previews)"},
    {"folder": "vae_approx", "name": "taesd3_decoder.pth", "title": "TAESD3 decoder (SD3 previews)"},
    {"folder": "vae_approx", "name": "taef1_decoder.pth", "title": "TAEF1 decoder (Flux previews)"},
    {"folder": "vae", "name": "sdxl_vae.safetensors", "title": "SDXL VAE"},
    {"folder": "vae", "name": "vae-ft-mse-840000-ema-pruned.safetensors", "title": "SD 1.5 VAE (ft-MSE)"},
    {"folder": "vae", "name": "ae.safetensors", "title": "FLUX.1 VAE"},
    {"folder": "upscale_models", "name": "RealESRGAN_x4.pth", "title": "RealESRGAN x4"},
    {"folder": "upscale_models", "name": "4x-UltraSharp.pth", "title": "4x-UltraSharp"},
]
# Used when ComfyUI-Manager's list can't be fetched (same URLs it lists).
DEFAULT_URLS = {
    **{f"{n}_decoder.pth": f"https://github.com/madebyollin/taesd/raw/main/{n}_decoder.pth"
       for n in ("taesd", "taesdxl", "taesd3", "taef1")},
    "sdxl_vae.safetensors": "https://huggingface.co/stabilityai/sdxl-vae/resolve/main/sdxl_vae.safetensors",
    "vae-ft-mse-840000-ema-pruned.safetensors":
        "https://huggingface.co/stabilityai/sd-vae-ft-mse-original/resolve/main/vae-ft-mse-840000-ema-pruned.safetensors",
    "ae.safetensors": "https://huggingface.co/black-forest-labs/FLUX.1-schnell/resolve/main/ae.safetensors",
    "RealESRGAN_x4.pth": "https://huggingface.co/ai-forever/Real-ESRGAN/resolve/main/RealESRGAN_x4.pth",
    "4x-UltraSharp.pth": "https://huggingface.co/Kim2091/UltraSharp/resolve/main/4x-UltraSharp.pth",
}
# ComfyUI-Manager's "default" save_path, per model type.
TYPE_FOLDERS = {"checkpoint": "checkpoints", "unclip": "checkpoints", "lora": "loras", "VAE": "vae", "vae": "vae",
                "TAESD": "vae_approx", "upscale": "upscale_models", "controlnet": "controlnet",
                "T2I-Adapter": "controlnet", "clip": "text_encoders", "clip_vision": "clip_vision",
                "embedding": "embeddings", "diffusion_model": "diffusion_models", "unet": "diffusion_models",
                "gligen": "gligen", "style_model": "style_models", "photomaker": "photomaker",
                "model_patch": "model_patches", "latent_upscale": "latent_upscale_models"}
# Loader node → models/ folder, for files no workflow or list gives a folder for.
LOADER_FOLDERS = {"CheckpointLoaderSimple": "checkpoints", "CheckpointLoader": "checkpoints",
                  "UNETLoader": "diffusion_models", "UnetLoaderGGUF": "diffusion_models", "LoaderGGUF": "diffusion_models",
                  "VAELoader": "vae", "CLIPLoader": "text_encoders", "DualCLIPLoader": "text_encoders",
                  "TripleCLIPLoader": "text_encoders", "CLIPVisionLoader": "clip_vision",
                  "LoraLoader": "loras", "LoraLoaderModelOnly": "loras", "UpscaleModelLoader": "upscale_models",
                  "ControlNetLoader": "controlnet", "AudioEncoderLoader": "audio_encoders",
                  "LatentUpscaleModelLoader": "latent_upscale_models"}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def repo_key(url: str) -> str:
    """owner/name of a GitHub repo, lowercase ('' for anything else)."""
    match = GITHUB_REPO.match((url or "").strip())
    return f"{match.group(1)}/{match.group(2)}".lower() if match else ""


def hf_link(url: str) -> dict:
    """Library link for a huggingface.co/<repo>/resolve/<rev>/<file> URL, so update checks work."""
    parts = urlparse(url)
    if parts.netloc != "huggingface.co":
        return {}
    bits = unquote(parts.path).strip("/").split("/")
    if len(bits) < 5 or bits[2] != "resolve":
        return {}
    repo = "/".join(bits[:2])
    return {"source": "hf", "repo": repo, "file": "/".join(bits[4:]), "name": repo}


class Setup:
    def __init__(self, data_dir: Path, root: Path, tokens):
        self.dir = data_dir / "comfy-manager"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.tokens = tokens
        self.manager: dict = {}
        self.manager_at = 0.0
        self.heads: dict[str, dict] = {}  # url -> {size, sha256, at}
        self.last: dict = {}
        self.lock = asyncio.Lock()

    # ---- ComfyUI-Manager's lists (fetched daily, kept on disk) ----

    async def _manager_lists(self, client: httpx.AsyncClient) -> dict:
        if self.manager and time.time() - self.manager_at < MANAGER_TTL:
            return self.manager
        data = {}
        for name in MANAGER_FILES:
            path = self.dir / name
            fresh = path.is_file() and time.time() - path.stat().st_mtime < MANAGER_TTL
            if not fresh:
                try:
                    response = await client.get(MANAGER_RAW + name, timeout=60)
                    response.raise_for_status()
                    json.loads(response.content)
                    tmp = path.with_suffix(".tmp")
                    tmp.write_bytes(response.content)
                    os.replace(tmp, path)
                except (httpx.HTTPError, ValueError):
                    pass  # keep the copy from last time, if any
            try:
                data[name] = json.loads(path.read_text())
            except (OSError, ValueError):
                data[name] = {}
        self.manager, self.manager_at = data, time.time()
        return data

    # ---- custom node catalog (ComfyUI-Manager's custom-node-list) ----

    async def catalog(self) -> list[dict]:
        """Every git-installable pack in ComfyUI-Manager's list: {key, url, title, author, description}."""
        async with httpx.AsyncClient(follow_redirects=True) as client:
            manager = await self._manager_lists(client)
        out, seen = [], set()
        for entry in (manager.get("custom-node-list.json") or {}).get("custom_nodes") or []:
            if entry.get("install_type") not in (None, "git-clone"):
                continue
            urls = [entry.get("reference", "")] + list(entry.get("files") or [])
            key = next((repo_key(u) for u in urls if repo_key(u)), "")
            if not key or key in seen:
                continue
            seen.add(key)
            match = next(GITHUB_REPO.match(u.strip()) for u in urls if repo_key(u) == key)
            out.append({"key": key, "url": f"https://github.com/{match.group(1)}/{match.group(2)}",
                        "title": str(entry.get("title") or match.group(2))[:120],
                        "author": str(entry.get("author") or match.group(1))[:80],
                        "description": re.sub(r"<[^>]+>", "", str(entry.get("description") or ""))[:300]})
        return out

    # ---- what is on disk ----

    def _installed_files(self) -> tuple[set[str], dict[str, int]]:
        """Every model file under models/ (names and relative paths) + model-file count per top folder."""
        found: set[str] = set()
        counts: dict[str, int] = {}
        root = self.root.resolve()
        seen: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            real = os.path.realpath(dirpath)
            if real in seen:  # symlink loops
                dirnames[:] = []
                continue
            seen.add(real)
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            rel_dir = os.path.relpath(dirpath, root)
            top = rel_dir.split(os.sep)[0] if rel_dir != "." else ""
            for name in filenames:
                if not MODEL_EXT.search(name):
                    continue
                found.add(name.lower())
                found.add(os.path.join(rel_dir, name).lower() if rel_dir != "." else name.lower())
                if top:
                    counts[top] = counts.get(top, 0) + 1
        return found, counts

    @staticmethod
    def _workflows() -> list[tuple[str, dict]]:
        out = []
        for path in sorted(WORKFLOWS.rglob("*.json")) if WORKFLOWS.is_dir() else []:
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if isinstance(data, dict) and isinstance(data.get("nodes"), list):
                out.append((str(path.relative_to(WORKFLOWS)), data))
        return out

    @staticmethod
    def _nodes(workflow: dict):
        """All nodes, including those inside subgraphs (subgraph instances are typed by the subgraph id)."""
        subgraphs = (workflow.get("definitions") or {}).get("subgraphs") or []
        ids = {s.get("id") for s in subgraphs}
        for nodes in [workflow.get("nodes") or []] + [s.get("nodes") or [] for s in subgraphs]:
            for node in nodes:
                if isinstance(node, dict) and node.get("type") and node["type"] not in ids:
                    yield node

    async def _object_info(self, client: httpx.AsyncClient) -> set[str] | None:
        try:
            response = await client.get(f"{COMFY}/object_info", timeout=30)
            response.raise_for_status()
            return set(response.json())
        except (httpx.HTTPError, ValueError):
            return None

    async def _head(self, client: httpx.AsyncClient, url: str) -> dict:
        """Size and (Hugging Face LFS) SHA-256 of a download, cached for a day."""
        cached = self.heads.get(url)
        if cached and time.time() - cached["at"] < MANAGER_TTL:
            return cached
        info = {"size": 0, "sha256": "", "gated": False, "dead": False, "at": time.time()}
        headers = {"User-Agent": "Rocsy-ModelHub/1.0"}
        if urlparse(url).netloc == "huggingface.co" and self.tokens().get("hf"):
            headers["Authorization"] = f"Bearer {self.tokens()['hf']}"
        try:
            response = await client.head(url, headers=headers, follow_redirects=False, timeout=15)
            if response.status_code in (401, 403):
                info["gated"] = True  # gated Hugging Face repo: needs a token with access
                self.heads[url] = info
                return info
            if response.status_code in (404, 410):
                info["dead"] = True
                self.heads[url] = info
                return info
            size = response.headers.get("x-linked-size") or ""
            etag = (response.headers.get("x-linked-etag") or "").strip('"')
            if response.status_code in (301, 302, 307, 308) and not size:
                response = await client.head(url, headers=headers, follow_redirects=True, timeout=15)
                size = response.headers.get("content-length") or ""
            elif not size:
                size = response.headers.get("content-length") or ""
            info["size"] = int(size) if size.isdigit() else 0
            info["sha256"] = etag.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", etag) else ""
        except httpx.HTTPError:
            return {**info, "at": 0}
        self.heads[url] = info
        return info

    # ---- the scan ----

    async def scan(self, packs: list[dict] | None, job: dict | None = None) -> dict:
        """packs = custom_nodes folders from the host ({dir, remote, disabled}); None = host unreachable."""
        async with self.lock:
            async with httpx.AsyncClient(headers={"User-Agent": "Rocsy-ModelHub/1.0"}) as client:
                manager, classes = await asyncio.gather(self._manager_lists(client), self._object_info(client))
                files, counts = await asyncio.to_thread(self._installed_files)
                workflows = await asyncio.to_thread(self._workflows)
                model_list = (manager.get("model-list.json") or {}).get("models") or []
                by_file: dict[str, dict] = {}
                for entry in model_list:
                    by_file.setdefault(str(entry.get("filename", "")).lower(), entry)

                defaults = self._defaults(by_file, files, counts)
                models = self._missing_models(workflows, by_file, files)
                nodes = self._missing_nodes(workflows, manager, classes, packs)
                urls = {item["url"] for item in defaults + models if item.get("url")}
                heads = dict(zip(urls, await asyncio.gather(*(self._head(client, url) for url in urls))))
            for item in defaults + models:
                head = heads.get(item.get("url")) or {}
                item["size"] = head.get("size") or item.get("size") or 0
                item["sha256"] = head.get("sha256", "")
                item["gated"] = head.get("gated", False)
                if head.get("dead"):
                    item["url"], item["dead"] = "", True  # the listed link is gone: search instead
            self.last = {"defaults": defaults, "models": models, **nodes,
                         "workflows": len(workflows), "scanned_at": time.time(),
                         "manager_list": bool(model_list)}
            return self.last

    def _defaults(self, by_file: dict, files: set[str], counts: dict[str, int]) -> list[dict]:
        out = []
        for spec in DEFAULTS:
            if counts.get(spec["folder"]):
                continue  # the folder already has something: nothing is "missing"
            entry = by_file.get(spec["name"].lower()) or {}
            url = entry.get("url") or DEFAULT_URLS[spec["name"]]
            out.append({"key": f"default:{spec['name']}", "name": spec["name"], "title": spec["title"],
                        "dest": spec["folder"], "url": url, "size": 0,
                        "note": entry.get("description", "")[:200], "installed": spec["name"].lower() in files})
        return [item for item in out if not item["installed"]]

    def _missing_models(self, workflows: list, by_file: dict, files: set[str]) -> list[dict]:
        wanted: dict[str, dict] = {}
        for rel, workflow in workflows:
            side = bool({p.lower() for p in Path(rel).parts[:-1]} & SIDE_DIRS)
            for node in self._nodes(workflow):
                props = node.get("properties") or {}
                listed = {str(m.get("name", "")).lower(): m for m in props.get("models") or [] if isinstance(m, dict)}
                values = node.get("widgets_values")
                values = list(values.values()) if isinstance(values, dict) else values if isinstance(values, list) else []
                names = [v for v in values if isinstance(v, str) and MODEL_EXT.search(v)]
                names += [m.get("name", "") for m in listed.values() if m.get("name")]
                for raw in names:
                    path = raw.replace("\\", "/")
                    base = path.rsplit("/", 1)[-1]
                    if not base or base.lower() in files or path.lower() in files:
                        continue
                    item = wanted.setdefault(base.lower(), {"key": f"model:{base.lower()}", "name": base,
                                                            "url": "", "dest": "", "workflows": [], "side": True,
                                                            "loader": node["type"]})
                    meta = listed.get(base.lower()) or listed.get(path.lower())
                    if meta and str(meta.get("url", "")).startswith("https://") and not item["url"]:
                        item["url"] = meta["url"]
                        item["dest"] = str(meta.get("directory") or "").strip("/")
                    if rel not in item["workflows"]:
                        item["workflows"].append(rel)
                    item["side"] = item["side"] and side
        for item in wanted.values():
            entry = by_file.get(item["name"].lower())
            if entry and not item["url"]:
                item["url"] = entry.get("url", "")
                save = str(entry.get("save_path") or "default")
                item["dest"] = TYPE_FOLDERS.get(entry.get("type", ""), "") if save == "default" else save
                item["size"] = 0
            if not item["dest"]:
                item["dest"] = LOADER_FOLDERS.get(item["loader"], "")
            if not item["url"].startswith("https://") or not item["dest"]:
                item["url"] = ""
            item["search"] = re.sub(r"[-_.]+", " ", MODEL_EXT.sub("", item["name"])).strip()
        return sorted(wanted.values(), key=lambda i: (i["side"], not i["url"], i["name"].lower()))

    def _missing_nodes(self, workflows: list, manager: dict, classes: set[str] | None,
                       packs: list[dict] | None) -> dict:
        if classes is None:
            return {"nodes": [], "unknown_nodes": [], "broken_nodes": [], "nodes_error":
                    "ComfyUI isn't running, so its nodes can't be checked. Start it (Settings → Services) and scan again."}
        node_map = manager.get("extension-node-map.json") or {}
        node_list = (manager.get("custom-node-list.json") or {}).get("custom_nodes") or []
        repo_info: dict[str, dict] = {}
        by_cnr: dict[str, str] = {}
        for entry in node_list:
            urls = [entry.get("reference", "")] + list(entry.get("files") or [])
            key = next((repo_key(u) for u in urls if repo_key(u)), "")
            if not key or entry.get("install_type") not in (None, "git-clone"):
                continue
            repo_info.setdefault(key, {"title": entry.get("title", ""), "description": entry.get("description", ""),
                                       "author": entry.get("author", ""), "url": f"https://github.com/{key}"})
            if entry.get("id"):
                by_cnr.setdefault(str(entry["id"]).lower(), key)
        provides: dict[str, list[str]] = {}
        patterns: list[tuple[re.Pattern, str]] = []
        pack_classes: dict[str, set[str]] = {}
        for url, value in node_map.items():
            key = repo_key(url)
            if not key or not isinstance(value, list) or not value:
                continue
            pack_classes[key] = set(value[0])
            for name in value[0]:
                provides.setdefault(name, []).append(key)
            pattern = (value[1] if len(value) > 1 and isinstance(value[1], dict) else {}).get("nodename_pattern")
            if pattern:
                try:
                    patterns.append((re.compile(pattern), key))
                except re.error:
                    pass

        installed: set[str] = set()
        installed_names: set[str] = set()
        for pack in packs or []:
            if pack.get("disabled"):
                continue
            if repo_key(pack.get("remote", "")):
                installed.add(repo_key(pack["remote"]))
            installed_names.add(_norm(pack.get("dir", "")))

        def is_installed(key: str, cnr: str = "") -> bool:
            return key in installed or _norm(key.split("/")[-1]) in installed_names or (cnr and _norm(cnr) in installed_names)

        missing: dict[str, dict] = {}
        broken: dict[str, dict] = {}
        unknown: dict[str, dict] = {}
        for rel, workflow in workflows:
            side = bool({p.lower() for p in Path(rel).parts[:-1]} & SIDE_DIRS)
            for node in self._nodes(workflow):
                kind = node["type"]
                if kind in classes or kind in VIRTUAL:
                    continue
                props = node.get("properties") or {}
                cnr = str(props.get("cnr_id") or "").lower()
                if cnr == "comfy-core":
                    continue  # a core frontend node
                aux = repo_key(f"https://github.com/{props['aux_id']}") if props.get("aux_id") else ""
                candidates = provides.get(kind, [])
                key = (aux if aux else by_cnr.get(cnr, "") if cnr else "") or \
                      next((c for c in candidates if is_installed(c)), "") or (candidates[0] if candidates else "") or \
                      next((k for p, k in patterns if p.search(kind)), "")
                if not key and cnr and _norm(cnr) in installed_names:
                    continue  # from an installed pack the lists don't know: most likely frontend-only
                if not key:
                    entry = unknown.setdefault(kind, {"type": kind, "pack": cnr, "workflows": [], "side": True})
                elif is_installed(key, cnr):
                    # Installed pack without this node: real only if the pack's Python declares it
                    # (rgthree labels, KJNodes Set/Get etc. are frontend-only).
                    if kind not in pack_classes.get(key, set()):
                        continue
                    entry = broken.setdefault(key, {"key": key, "url": f"https://github.com/{key}",
                                                    "title": (repo_info.get(key) or {}).get("title") or key.split("/")[-1],
                                                    "types": [], "workflows": [], "side": True})
                else:
                    info = repo_info.get(key) or {}
                    entry = missing.setdefault(key, {"key": key, "url": f"https://github.com/{key}",
                                                     "title": info.get("title") or key.split("/")[-1],
                                                     "description": info.get("description", "")[:300],
                                                     "author": info.get("author", "") or key.split("/")[0],
                                                     "listed": bool(info), "types": [], "workflows": [], "side": True})
                if "types" in entry and kind not in entry["types"]:
                    entry["types"].append(kind)
                if rel not in entry["workflows"]:
                    entry["workflows"].append(rel)
                entry["side"] = entry["side"] and side
        order = lambda i: (i["side"], (i.get("title") or i.get("type", "")).lower())  # noqa: E731
        return {"nodes": sorted(missing.values(), key=order), "broken_nodes": sorted(broken.values(), key=order),
                "unknown_nodes": sorted(unknown.values(), key=order), "nodes_error": ""}
