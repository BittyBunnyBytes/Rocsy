"""Inspect public Civitai/Hugging Face LoRA links and stream a selected file safely."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from html.parser import HTMLParser
import json
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import httpx

from . import custom_loras
from .comfy import ServiceError

def _public_address(host: str) -> str:
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ServiceError(f"Could not resolve web host {host}") from exc
    ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
    if not ips or any(not ip.is_global for ip in ips):
        raise ServiceError("LoRA links must resolve to a public Internet host")
    return str(ips[0])


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)


def _plain(value: str) -> str:
    parser = _Text()
    parser.feed(value or '')
    return ' '.join(''.join(parser.parts).split())


_CIVITAI_HOSTS = {"civitai.com", "www.civitai.com", "civitai.red", "civitai.green"}
_HF_HOSTS = {"huggingface.co", "www.huggingface.co"}


def source_kind(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in {None, 443}:
        raise ValueError("Use an HTTPS Civitai or Hugging Face link")
    if parts.hostname in _CIVITAI_HOSTS:
        return "civitai"
    if parts.hostname in _HF_HOSTS:
        return "huggingface"
    raise ValueError("Use a Civitai or Hugging Face model link")


async def _stream(url: str, provider: str, token: str, limit: int, destination: Path | None = None,
                  progress: Callable[[int, int | None], None] | None = None) -> tuple[bytes | None, str, str, int]:
    """Pin a public IP for each HTTPS redirect; keep bearer tokens on their provider host."""
    for _ in range(9):
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password or parts.port not in {None, 80, 443}:
            raise ServiceError("LoRA download redirected to an invalid URL")
        host = parts.hostname
        ip = await asyncio.to_thread(_public_address, host)
        pinned = httpx.URL(url).copy_with(host=ip)
        headers = {"Host": host, "User-Agent": "Krea-LoRA-Gallery/1.0"}
        if token and host == ("civitai.com" if provider == "civitai" else "huggingface.co"):
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=12), trust_env=False,
                                     follow_redirects=False) as client:
            async with client.stream("GET", pinned, headers=headers, extensions={"sni_hostname": host}) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise ServiceError("LoRA download redirected without a destination")
                    url = urljoin(url, location)
                    continue
                if response.status_code in {401, 403}:
                    raise ServiceError("This LoRA requires access. Supply a Civitai or Hugging Face token with download permission, and accept any gated model terms.")
                response.raise_for_status()
                length_text = response.headers.get("content-length", "")
                length = int(length_text) if length_text.isdigit() else None
                if length and length > limit:
                    raise ServiceError("LoRA file exceeds the 6 GiB limit" if destination else "Model metadata is too large")
                amount = 0
                chunks: list[bytes] = []
                stream = destination.open("wb") if destination else None
                try:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        amount += len(chunk)
                        if amount > limit:
                            raise ServiceError("LoRA file exceeds the 6 GiB limit" if destination else "Model metadata is too large")
                        if stream:
                            stream.write(chunk)
                            if progress:
                                progress(amount, length)
                        else:
                            chunks.append(chunk)
                finally:
                    if stream:
                        stream.close()
                return (None if destination else b"".join(chunks)), response.headers.get("content-type", ""), url, amount
    raise ServiceError("LoRA download redirected too many times")


async def _json(url: str, provider: str, token: str) -> dict[str, Any]:
    data, _, _, _ = await _stream(url, provider, token, 4_000_000)
    try:
        value = json.loads(data or b"")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ServiceError("Model site did not return readable metadata") from exc
    if not isinstance(value, dict):
        raise ServiceError("Model site returned invalid metadata")
    return value


def _compatibility(value: str, model: str) -> bool:
    text = value.lower()
    if not text:
        return True
    krea = 'krea' in text or 'lustify' in text
    qwen = 'qwen' in text
    moss = 'moss' in text
    anima = 'anima' in text
    if krea or qwen or moss or anima:
        return {'krea': krea, 'qwen': qwen, 'moss': moss, 'anima': anima}[model]
    return True


async def inspect(url: str, model: str, token: str) -> dict[str, Any]:
    provider = source_kind(url)
    if model not in {"krea", "qwen", "moss", "anima"}:
        raise ValueError("Choose Krea 2, Qwen-Image 2.1, MOSS-Image, or Anima")
    if provider == "civitai":
        return await _civitai(url, model, token)
    return await _huggingface(url, model, token)


async def _civitai(url: str, model: str, token: str) -> dict[str, Any]:
    parts = urlsplit(url)
    model_match = re.fullmatch(r"/models/(\d+)(?:/[^/]*)?/?", parts.path)
    download_match = re.fullmatch(r"/api/download/models/(\d+)/?", parts.path)
    version_id = (parse_qs(parts.query).get("modelVersionId") or [None])[0]
    if version_id and not re.fullmatch(r"\d+", version_id):
        raise ValueError("Invalid Civitai model version")
    if download_match:
        version_id = download_match.group(1)
    if not model_match and not download_match:
        raise ValueError("Use a Civitai model page or model download link")
    model_id = model_match.group(1) if model_match else None
    if version_id:
        version = await _json(f"https://civitai.com/api/v1/model-versions/{version_id}", "civitai", token)
        if model_id and str(version.get("modelId")) != model_id:
            raise ValueError("The Civitai version does not belong to this model")
        model_id = str(version.get("modelId") or model_id or "")
        versions = [version]
    else:
        versions = []
    if not model_id or not re.fullmatch(r"\d+", model_id):
        raise ServiceError("Civitai did not identify the model")
    item = await _json(f"https://civitai.com/api/v1/models/{model_id}", "civitai", token)
    if str(item.get("type", "")).casefold() not in {"lora", "locon", "lycoris"}:
        raise ServiceError("That Civitai page is not a LoRA")
    if not versions:
        versions = item.get("modelVersions") or []
    candidates = []
    for version in versions[:30]:
        if not isinstance(version, dict):
            continue
        base = str(version.get("baseModel") or "")
        if not _compatibility(base, model):
            continue
        for file in (version.get("files") or [])[:20]:
            if not isinstance(file, dict) or not str(file.get("name", "")).lower().endswith(".safetensors"):
                continue
            size = int(float(file.get("sizeKB") or 0) * 1024)
            if size > custom_loras.MAX_LORA_BYTES:
                continue
            file_url = str(file.get("downloadUrl") or version.get("downloadUrl") or "")
            if urlsplit(file_url).hostname not in _CIVITAI_HOSTS or urlsplit(file_url).scheme != "https":
                continue
            candidate = {"id": f"{version.get('id')}:{file.get('id')}", "filename": str(file["name"]),
                         "version": str(version.get("name") or ""), "base_model": base, "size_bytes": size,
                         "triggers": version.get("trainedWords") or [], "download_url": file_url,
                         "sha256": str((file.get("hashes") or {}).get("SHA256") or "").lower()}
            candidates.append(candidate)
    if not candidates:
        raise ServiceError("No compatible .safetensors LoRA file was listed for the selected model")
    if len(candidates) > 100:
        candidates = candidates[:100]
    # Use the chosen version's page after Hermes selects a candidate.
    return {"provider": "civitai", "source_url": f"https://civitai.com/models/{model_id}",
            "title": str(item.get("name") or ""), "creator": str((item.get("creator") or {}).get("username") or ""),
            "context": _plain(str(item.get("description") or ""))[:16000],
            "candidates": candidates, "model_id": model_id, "versions": versions}


async def _huggingface(url: str, model: str, token: str) -> dict[str, Any]:
    parts = [unquote(value) for value in urlsplit(url).path.strip("/").split("/")]
    if len(parts) < 2 or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", value) for value in parts[:2]):
        raise ValueError("Use a Hugging Face model repository or file link")
    owner, repo = parts[:2]
    revision, requested_file, tree_prefix = "main", "", ""
    if len(parts) > 2:
        if len(parts) < 4 or parts[2] not in {"blob", "resolve", "tree"}:
            raise ValueError("Use a Hugging Face repository, tree, or file link")
        revision = parts[3]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", revision):
            raise ValueError("Unsupported Hugging Face revision")
        if parts[2] == "tree":
            tree_prefix = "/".join(parts[4:]).strip("/")
            if ".." in parts[4:]:
                raise ValueError("Invalid Hugging Face folder path")
        else:
            if len(parts) < 5:
                raise ValueError("Link directly to a .safetensors file")
            requested_file = "/".join(parts[4:])
            if not requested_file.lower().endswith(".safetensors") or ".." in parts[4:]:
                raise ValueError("Link directly to a .safetensors file")
    repo_id = f"{owner}/{repo}"
    info_url = f"https://huggingface.co/api/models/{quote(repo_id, safe='/')}"
    if revision != "main":
        info_url += f"/revision/{quote(revision)}"
    info = await _json(info_url + "?blobs=true", "huggingface", token)
    files = info.get("siblings") or []
    candidates = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("rfilename") or "")
        if not name.lower().endswith(".safetensors") or (requested_file and name != requested_file) or (tree_prefix and not name.startswith(tree_prefix + "/")):
            continue
        size = int(item.get("size") or (item.get("lfs") or {}).get("size") or 0)
        if size > custom_loras.MAX_LORA_BYTES:
            continue
        candidates.append({"id": name, "filename": name, "version": revision, "base_model": "",
                           "size_bytes": size, "triggers": [], "sha256": str((item.get("lfs") or {}).get("sha256") or "").lower(),
                           "download_url": f"https://huggingface.co/{quote(repo_id, safe='/')}/resolve/{quote(revision)}/{quote(name, safe='/')}"})
    if not candidates:
        raise ServiceError("No .safetensors LoRA file was found at that Hugging Face link")
    if len(candidates) > 100:
        raise ServiceError("This repository has too many LoRA files; link directly to one .safetensors file")
    readme = ""
    try:
        data, _, _, _ = await _stream(f"https://huggingface.co/{quote(repo_id, safe='/')}/resolve/{quote(revision)}/README.md",
                                      "huggingface", token, 80_000)
        readme = (data or b"").decode("utf-8", errors="replace")[:16000]
    except (ServiceError, httpx.HTTPError):
        pass
    title = str((info.get("cardData") or {}).get("model_name") or repo.replace("-", " "))
    return {"provider": "huggingface", "source_url": f"https://huggingface.co/{repo_id}",
            "title": title, "creator": owner, "context": readme, "candidates": candidates,
            "repo_id": repo_id, "tags": info.get("tags") or []}


async def download(candidate: dict[str, Any], provider: str, token: str, target: Path,
                   progress: Callable[[int, int | None], None]) -> None:
    url = str(candidate["download_url"])
    if provider == "civitai" and urlsplit(url).hostname not in _CIVITAI_HOSTS:
        raise ServiceError("Civitai supplied an invalid download URL")
    if provider == "huggingface" and urlsplit(url).hostname not in _HF_HOSTS:
        raise ServiceError("Hugging Face supplied an invalid download URL")
    await _stream(url, provider, token, custom_loras.MAX_LORA_BYTES, target, progress)
    expected = candidate.get("sha256", "")
    if re.fullmatch(r"[0-9a-f]{64}", expected):
        def digest() -> str:
            hasher = hashlib.sha256()
            with target.open("rb") as stream:
                while data := stream.read(8 * 1024 * 1024):
                    hasher.update(data)
            return hasher.hexdigest()
        if await asyncio.to_thread(digest) != expected:
            raise ServiceError("Downloaded LoRA failed its SHA-256 check")
    await asyncio.to_thread(custom_loras.validate_file, target)
