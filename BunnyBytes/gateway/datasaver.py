"""Mobile data saver (Settings -> Appearance -> Mobile data).

static/js/data-saver.js decides per device: a phone on cellular (or any browser
asking for Save-Data) sets the rocsy_lite cookie. While it's set, big studio
images shown in <img> tags are sent as smaller WebP copies instead of full-size
PNGs. Downloads and images opened on their own still get the original:
browsers mark those with a Sec-Fetch-Dest other than "image"."""
from __future__ import annotations

import asyncio
import io
from collections import OrderedDict

MODES = ("auto", "phone", "off")
COOKIE = "rocsy_lite"

# Smaller than this is passed through: re-encoding wouldn't save much.
MIN_BYTES = 180_000
MAX_BYTES = 40_000_000
MAX_SIDE = 1280
QUALITY = 70
_SHRINKABLE = ("image/png", "image/jpeg", "image/webp", "image/bmp", "image/tiff")

# Lite copies by URL + upstream validator, capped by total size.
_cache: OrderedDict[str, bytes] = OrderedDict()
_cache_bytes = 0
_CACHE_LIMIT = 64_000_000


def wants_lite(headers, cookies) -> bool:
    return cookies.get(COOKIE) == "1" or headers.get("save-data", "").lower() == "on"


def shrinkable(request_headers, content_type: str, content_length: str) -> bool:
    if request_headers.get("sec-fetch-dest", "") != "image" or "range" in request_headers:
        return False
    if content_type.split(";")[0].strip().lower() not in _SHRINKABLE:
        return False
    return not (content_length.isdigit() and not MIN_BYTES <= int(content_length) <= MAX_BYTES)


def shrink(raw: bytes, max_side: int = MAX_SIDE, quality: int = QUALITY) -> bytes:
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(raw)) as image:
        if image.format == "JPEG":
            image.draft("RGB", (max_side, max_side))  # decode at reduced scale
        image = ImageOps.exif_transpose(image)
        # Keep transparency (Krea cut-outs): WebP has an alpha channel.
        image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
        image.thumbnail((max_side, max_side))
        out = io.BytesIO()
        image.save(out, "WEBP", quality=quality, method=4)
    return out.getvalue()


async def lite_copy(key: str, raw: bytes) -> bytes | None:
    """The lite copy, or None when it wouldn't be smaller (or isn't an image)."""
    global _cache_bytes
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key] or None
    try:
        data = await asyncio.to_thread(shrink, raw)
    except Exception:  # noqa: BLE001 - not decodable: send the original
        data = b""
    if len(data) >= len(raw) * 0.8:
        data = b""  # remembered as "keep the original"
    _cache[key] = data
    _cache_bytes += len(data)
    while (_cache_bytes > _CACHE_LIMIT or len(_cache) > 4000) and _cache:
        _cache_bytes -= len(_cache.popitem(last=False)[1])
    return data or None
