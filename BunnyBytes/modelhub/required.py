"""Studios -> Generate -> "missing model files" dialog (shared/ui/missing-files.js).

A studio refuses to queue a render whose graph loads files ComfyUI doesn't list
(shared/workflow_models.missing_files) and the dialog asks here what to do about each:
- found: the same file name elsewhere on this machine (hostctl modelfind.py search):
  linked into models/<folder>/ without copying (hard link / reflink / symlink);
- download: a known source - known_files.KNOWN (official repos), the library's
  Hugging Face links, then ComfyUI-Manager's model list - checked with a HEAD request;
- neither: the dialog offers a search in Models.

Download URLs and link sources are always worked out here again on fetch; the
browser only names files.
"""
from __future__ import annotations

import re
from pathlib import Path

from comfysetup import TYPE_FOLDERS
from known_files import KNOWN

NAME = re.compile(r"^[^\\\0]{1,300}\.(safetensors|sft|gguf|ckpt|pt|pth|bin|pkl|onnx)$", re.I)
FOLDER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MODEL_EXT = re.compile(r"\.(safetensors|sft|gguf|ckpt|pt|pth|bin|pkl|onnx)$", re.I)


def clean(files) -> list[dict]:
    """[{name, folder, label}] from the browser, validated; at most 32."""
    out, seen = [], set()
    for item in files if isinstance(files, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").replace("\\", "/").strip("/")
        folder = str(item.get("folder") or "")
        if not NAME.match(name) or any(p in ("", ".", "..") for p in name.split("/")) or name.lower() in seen:
            continue
        if folder and not FOLDER.match(folder):
            folder = ""
        seen.add(name.lower())
        out.append({"name": name, "folder": folder, "label": str(item.get("label") or "")[:60]})
    return out[:32]


def source(name: str, library_links: dict, manager_by_file: dict) -> dict:
    """{url, folder, size, sha256, origin} for a file name, or {}."""
    base = name.rsplit("/", 1)[-1]
    known = KNOWN.get(base)
    if known:
        repo, path, folder, size, sha = known
        return {"url": f"https://huggingface.co/{repo}/resolve/main/{path}", "folder": folder, "size": size,
                "sha256": sha, "origin": repo}
    for rel, link in library_links.items():
        if rel.rsplit("/", 1)[-1] == base and link.get("source") == "hf" and link.get("repo") and link.get("file"):
            return {"url": f"https://huggingface.co/{link['repo']}/resolve/main/{link['file']}",
                    "folder": rel.split("/", 1)[0], "size": 0, "sha256": "", "origin": link["repo"]}
    entry = manager_by_file.get(base.lower())
    if entry and str(entry.get("url", "")).startswith("https://"):
        save = str(entry.get("save_path") or "default")
        folder = TYPE_FOLDERS.get(entry.get("type", ""), "") if save == "default" else save.strip("/")
        return {"url": entry["url"], "folder": folder if FOLDER.match(folder or "-") else "", "size": 0,
                "sha256": "", "origin": "ComfyUI-Manager list"}
    return {}


def plan(item: dict, src: dict, head: dict, found: list[dict], host_models: str) -> dict:
    """One dialog row: where it goes, what's on disk, what can be downloaded, and the suggested action."""
    folder = item["folder"] or src.get("folder", "")
    dest = f"{host_models.rstrip('/')}/{folder}/{item['name']}" if folder else ""
    want_size = src.get("size") or head.get("size") or 0
    candidates = []
    for hit in found:
        if dest and hit["path"] == dest:
            continue  # already there: ComfyUI lists it on its next look (or it's in a subfolder name mismatch)
        candidates.append({**hit, "size_ok": not want_size or hit["size"] == want_size})
    candidates.sort(key=lambda c: (not c["size_ok"], "/.cache/" in c["path"], len(c["path"])))
    download = None
    if src.get("url") and not head.get("dead"):
        download = {"size": want_size, "origin": src["origin"], "gated": bool(head.get("gated"))}
    action = ("link" if candidates and candidates[0]["size_ok"] else "download" if download
              else "link" if candidates else "none")
    search = re.sub(r"[-_.]+", " ", MODEL_EXT.sub("", item["name"].rsplit("/", 1)[-1])).strip()
    return {**item, "folder": folder, "found": candidates[:5], "download": download, "action": action,
            "search": search}


def dest_parts(name: str, folder: str) -> tuple[str, str]:
    """(downloads dest folder, file name) for a value like 'sub/x.safetensors' in folder 'loras'."""
    path = Path(name)
    parent = str(path.parent) if str(path.parent) != "." else ""
    return (f"{folder}/{parent}" if parent else folder), path.name
