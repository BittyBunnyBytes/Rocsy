"""Builds static/lora-gallery/: preview posters + index.json (name, creator,
description, trigger words, previews) for every LoRA in backend/models.py
LORA_INFO, from its Civitai or Hugging Face source page.

Run on the host whenever LoRAs are added (needs ffmpeg for video posters):
    python3 tools/fetch_lora_gallery.py [--force]
Another app with the same layout (backend/models.py LORA_INFO, static/), e.g. Krea:
    python3 tools/fetch_lora_gallery.py --root ~/AI/Krea

Posters and preview videos are both stored locally: the Rocsy gateway's
CSP only allows media from the site itself (media-src 'self'), so CDN video URLs
never play. Videos are re-encoded small (VIDEO_WIDTH wide, H.264 + AAC, faststart)."""
from __future__ import annotations

import argparse
import os
import html
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.models import LORA_INFO  # noqa: E402

OUT = ROOT / "static" / "lora-gallery"
MAX_PREVIEWS = 6
POSTER_WIDTH = 480
VIDEO_WIDTH = 640
UA = {"User-Agent": "H3Machine-lora-gallery/1.0"}
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".gif")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def get_json(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as response:
        return json.load(response)


def get_text(url: str) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as response:
        return response.read().decode("utf-8", "replace")


class _TextExtractor(HTMLParser):
    """Civitai descriptions are HTML; keep paragraphs, list bullets and line breaks only."""
    BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "tr"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in self.BLOCK:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def markdown_to_text(value: str) -> str:
    value = re.sub(r"^---\n.*?\n---\n", "", value, flags=re.S)  # YAML front matter
    # HTML tags go, but H3 reference tokens (<Video 1>, <Picture 1>...) are prompt text and stay.
    value = re.sub(r"<(?!\s*(?:Picture|Image|Video|Audio|Subject)\s+\d+\s*>)[^>]+>", "", value)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)  # images
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)  # links -> text
    value = re.sub(r"^#{1,6}\s*", "", value, flags=re.M)
    value = re.sub(r"^\s*[-*]\s+", "• ", value, flags=re.M)
    value = re.sub(r"^>\s*\[!(\w+)\]\s*$", lambda m: m.group(1).title() + ":", value, flags=re.M)  # GitHub callouts
    value = re.sub(r"^>\s?", "", value, flags=re.M)  # blockquote markers
    value = re.sub(r"^\|?\s*:?-{3,}.*$\n?", "", value, flags=re.M)  # table separator rows
    value = re.sub(r"^\|\s*(.*?)\s*\|\s*$", lambda m: re.sub(r"\s*\|\s*", ": ", m.group(1), count=1).replace(" | ", ", "), value, flags=re.M)
    value = re.sub(r"[*_`]{1,3}", "", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def make_poster(source: str, target: Path, is_video: bool) -> bool:
    """ffmpeg reads the (remote) image/video and writes a POSTER_WIDTH jpeg."""
    command = ["ffmpeg", "-y", "-loglevel", "error"]
    if is_video:
        command += ["-ss", "0.8"]
    command += ["-i", source, "-frames:v", "1", "-vf", f"scale='min({POSTER_WIDTH},iw)':-2", "-q:v", "4", str(target)]
    result = subprocess.run(command, capture_output=True, timeout=120)
    if result.returncode != 0 and is_video:  # clip shorter than the seek point
        command.remove("-ss"), command.remove("0.8")
        result = subprocess.run(command, capture_output=True, timeout=120)
    return result.returncode == 0 and target.exists() and target.stat().st_size > 0


VAAPI_DEVICE = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")


def make_video(source: str, target: Path) -> bool:
    """Re-encode a (remote) preview clip into a small local H.264 mp4 that every browser plays.
    Encoded on the GPU (VA-API h264_vaapi), never a CPU encoder."""
    command = ["ffmpeg", "-y", "-loglevel", "error", "-vaapi_device", VAAPI_DEVICE, "-i", source,
               "-vf", f"scale='min({VIDEO_WIDTH},iw)':-2,fps='min(30,source_fps)',format=nv12,hwupload",
               "-c:v", "h264_vaapi", "-qp", "29", "-profile:v", "high", "-c:a", "aac", "-b:a", "64k", "-ac", "2",
               "-movflags", "+faststart", str(target)]
    result = subprocess.run(command, capture_output=True, timeout=300)
    return result.returncode == 0 and target.exists() and target.stat().st_size > 0


def civitai_media_url(url: str, transform: str) -> str:
    # https://image.civitai.com/<key>/<uuid>/original=true/<id>.mp4 -> .../<transform>/<id>.mp4
    return re.sub(r"/(original=true|width=\d+[^/]*)/", f"/{transform}/", url, count=1)


def fetch_civitai(url: str) -> dict:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    model_id = re.search(r"/models/(\d+)", url).group(1)
    model = get_json(f"https://civitai.com/api/v1/models/{model_id}")
    version_id = (query.get("modelVersionId") or [None])[0]
    version = get_json(f"https://civitai.com/api/v1/model-versions/{version_id}") if version_id else (model.get("modelVersions") or [{}])[0]
    description = html_to_text(model.get("description"))
    version_notes = html_to_text(version.get("description"))
    if version_notes and version_notes not in description:
        description = f"{description}\n\nVersion notes ({version.get('name')}):\n{version_notes}".strip()
    previews = []
    for image in version.get("images") or []:
        is_video = image.get("type") == "video" or image["url"].lower().endswith(VIDEO_EXTS)
        previews.append({
            "source": civitai_media_url(image["url"], "transcode=true,width=450,optimized=true" if is_video else f"width={POSTER_WIDTH}"),
            "video": civitai_media_url(image["url"], "transcode=true,width=720,optimized=true") if is_video else None,  # re-encode source
            "nsfw": int(image.get("nsfwLevel") or 1),
        })
    return {
        "name": model.get("name") or "",
        "version": version.get("name") or "",
        "creator": (model.get("creator") or {}).get("username") or "",
        "base_model": version.get("baseModel") or "",
        "trained_words": version.get("trainedWords") or [],
        "description": description,
        "previews": previews,
    }


MEDIA_EXTS = VIDEO_EXTS + IMAGE_EXTS


def _media_path(url: str) -> str:
    return urllib.parse.urlparse(url).path.lower()


def huggingface_media(repo: str, readme: str, card: dict, siblings: list[dict]) -> list[str]:
    """Preview media for a HF model, in page order: README embeds (<video>/<source>/<img>
    tags, markdown images, bare media links, usually on cdn-uploads.huggingface.co),
    then the model card's widget outputs, then media files committed to the repo."""
    def resolve(ref: str) -> str:
        ref = html.unescape(ref.strip())
        if ref.startswith(("http://", "https://")):
            return ref.replace("/blob/", "/resolve/", 1) if "huggingface.co/" in ref else ref
        return f"https://huggingface.co/{repo}/resolve/main/{urllib.parse.quote(ref.lstrip('./'))}"

    refs = re.findall(r"""<(?:video|source|img)\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", readme, re.I)
    refs += re.findall(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)", readme)
    refs += re.findall(r"""https?://[^\s"'<>()]+""", readme)
    for item in card.get("widget") or []:
        output = (item or {}).get("output") or {}
        if output.get("url"):
            refs.append(output["url"])
    refs += [sibling["rfilename"] for sibling in siblings]
    seen, media = set(), []
    for ref in refs:
        url = resolve(ref)
        if _media_path(url).endswith(MEDIA_EXTS) and url not in seen:
            seen.add(url)
            media.append(url)
    return media


def fetch_huggingface(url: str) -> dict:
    repo = urllib.parse.urlparse(url).path.strip("/")
    info = get_json(f"https://huggingface.co/api/models/{repo}")
    nsfw = 8 if "not-for-all-audiences" in (info.get("tags") or []) else 1
    try:
        raw_readme = get_text(f"https://huggingface.co/{repo}/raw/main/README.md")
    except urllib.error.HTTPError:
        raw_readme = ""
    card = info.get("cardData") or {}
    previews = []
    for media_url in huggingface_media(repo, raw_readme, card, info.get("siblings") or []):
        is_video = _media_path(media_url).endswith(VIDEO_EXTS)
        previews.append({"source": media_url, "video": media_url if is_video else None, "nsfw": nsfw})
    return {
        "name": repo.split("/")[-1].replace("-", " "),
        "version": "",
        "creator": repo.split("/")[0],
        "base_model": card.get("base_model") or "",
        "trained_words": [],
        "description": markdown_to_text(raw_readme),
        "previews": previews,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="re-download posters that already exist")
    parser.add_argument("--only", default="", help="only LORA_INFO filenames starting with this (e.g. Wan22_)")
    parser.add_argument("--root", type=Path, help="another app's folder (its backend/models.py LORA_INFO -> its static/lora-gallery)")
    args = parser.parse_args()
    global OUT, LORA_INFO
    if args.root:
        import importlib.util
        root = args.root.expanduser().resolve()
        spec = importlib.util.spec_from_file_location("other_app_models", root / "backend" / "models.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        LORA_INFO = module.LORA_INFO
        OUT = root / "static" / "lora-gallery"
    OUT.mkdir(parents=True, exist_ok=True)
    index_path = OUT / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    failures = []
    for number, (filename, info) in enumerate(LORA_INFO.items(), start=1):
        if args.only and not filename.startswith(args.only):
            continue
        url = info.get("url") or ""
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem)
        print(f"[{number}/{len(LORA_INFO)}] {filename}", flush=True)
        try:
            if re.search(r"//civitai\.(com|red|green)/", url):  # .red/.green are Civitai's other domains; the API is on .com
                entry = fetch_civitai(url)
            elif "huggingface.co" in url:
                entry = fetch_huggingface(url)
            else:
                continue
        except Exception as error:  # noqa: BLE001 - keep the previous entry, report at the end
            failures.append(f"{filename}: {error}")
            continue
        folder = OUT / slug
        folder.mkdir(exist_ok=True)
        previews = []
        for preview in entry.pop("previews"):
            if len(previews) >= MAX_PREVIEWS:
                break
            poster = folder / f"{len(previews)}.jpg"
            if args.force or not poster.exists():
                if not make_poster(preview["source"], poster, bool(preview["video"])):
                    print(f"    poster failed: {preview['source'][:100]}")
                    poster.unlink(missing_ok=True)
                    continue
            video = None
            if preview["video"]:
                clip = folder / f"{len(previews)}.mp4"
                if args.force or not clip.exists():
                    if not make_video(preview["video"], clip):
                        print(f"    video failed: {preview['video'][:100]}")
                        clip.unlink(missing_ok=True)
                video = f"lora-gallery/{slug}/{clip.name}" if clip.exists() else None
            # Hugging Face pages carry no content rating: a LoRA filed under NSFW blurs every preview.
            nsfw = max(preview["nsfw"] or 0, 8 if info.get("group") == "NSFW" else 0)  # 8 = Civitai's "X" level
            previews.append({"poster": f"lora-gallery/{slug}/{poster.name}", "video": video, "nsfw": nsfw})
        keep = {Path(item[key]).name for item in previews for key in ("poster", "video") if item[key]}
        for stale in [*folder.glob("*.jpg"), *folder.glob("*.mp4")]:  # fewer previews than last run
            if stale.name not in keep:
                stale.unlink()
        index[filename] = {**entry, "url": url, "previews": previews}
        time.sleep(0.4)
    index = {name: index[name] for name in LORA_INFO if name in index}
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1))
    print(f"Wrote {index_path} ({len(index)} LoRAs)")
    for failure in failures:
        print("FAILED", failure)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
