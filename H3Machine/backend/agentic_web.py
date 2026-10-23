"""Bounded public-web reads for opt-in Agentic research and reference acquisition."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin

import httpx

from .comfy import ServiceError

MAX_PAGE_BYTES = 350_000
MAX_ASSET_BYTES = 80 * 1024 * 1024


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0
        self.media_urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"img", "video", "audio", "source"}:
            for key in ("src", "data-src", "srcset"):
                value = values.get(key)
                if value:
                    self.media_urls.append(value.split(",", 1)[0].split(" ", 1)[0])
                    break
        if tag == "a" and (href := values.get("href")) and href.lower().split("?", 1)[0].endswith(
            (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".mov", ".mp3", ".wav", ".m4a", ".ogg")):
            self.media_urls.append(href)
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1
        elif tag in {"p", "h1", "h2", "h3", "li", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _public_address(host: str) -> str:
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ServiceError(f"Could not resolve web host {host}") from exc
    ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
    if not ips or any(not ip.is_global for ip in ips):
        raise ServiceError("Agentic web access requires a public Internet host")
    return str(ips[0])


async def safe_get(url: str, max_bytes: int = MAX_PAGE_BYTES, truncate: bool = False) -> tuple[bytes, str, str]:
    """Resolve and pin a public IP per hop; never follow a redirect to local services.
    truncate=True keeps the first max_bytes of a bigger response (web pages) instead of failing."""
    for _ in range(4):
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            raise ServiceError("Agentic web URL must be a public HTTP(S) URL")
        host = parts.hostname
        ip = await asyncio.to_thread(_public_address, host)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port not in {80, 443}:
            raise ServiceError("Agentic web access allows only standard web ports")
        host_header = host if port in {80, 443} else f"{host}:{port}"
        pinned = httpx.URL(url).copy_with(host=ip)
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=8), trust_env=False,
                                     follow_redirects=False) as client:
            async with client.stream("GET", pinned, headers={"Host": host_header, "User-Agent": "H3Machine-Agentic/1.0"},
                                     extensions={"sni_hostname": host}) as response:
                if response.is_redirect:
                    url = str(response.headers.get("location", ""))
                    url = str(httpx.URL(parts.geturl()).join(url))
                    continue
                response.raise_for_status()
                if int(response.headers.get("content-length", "0")) > max_bytes and not truncate:
                    raise ServiceError("Agentic web response exceeds its size limit")
                chunks = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        if truncate:
                            chunks.append(chunk[:max_bytes - (total - len(chunk))])
                            break
                        raise ServiceError("Agentic web response exceeds its size limit")
                    chunks.append(chunk)
                return b"".join(chunks), response.headers.get("content-type", "").split(";", 1)[0].lower(), url
    raise ServiceError("Agentic web URL redirected too many times")


async def search(query: str) -> str:
    """Return general web results and candidate public media URLs for agent choice."""
    import json
    from urllib.parse import quote, urlencode
    results = []
    try:
        data, _, _ = await safe_get("https://www.bing.com/search?format=rss&q=" + quote(query[:180]))
        root = ET.fromstring(data)
        items = root.findall("./channel/item")[:4]
        results.append("Web results:\n" + "\n".join(
            f"{item.findtext('title', '')}: {item.findtext('link', '')} — {item.findtext('description', '')[:200]}"
            for item in items))
    except (ServiceError, ET.ParseError, httpx.HTTPError):
        pass
    # Commons file search matches titles: a long descriptive query ("X white shirt 2024 photo") finds
    # nothing, so fall back to its proper nouns, then its first two words.
    words = query.split()
    names = " ".join(word for word in words if word[:1].isupper())
    attempts = list(dict.fromkeys(item for item in (query[:120], names, " ".join(words[:2])) if item))
    for attempt in attempts:
        params = {"action": "query", "generator": "search", "gsrsearch": attempt,
                  "gsrnamespace": 6, "gsrlimit": 6, "prop": "imageinfo",
                  "iiprop": "url|mime|size|extmetadata", "iiurlwidth": 960, "format": "json"}
        try:
            data, _, _ = await safe_get("https://commons.wikimedia.org/w/api.php?" + urlencode(params))
            pages = json.loads(data).get("query", {}).get("pages", {})
            media = []
            for page in pages.values():
                info = (page.get("imageinfo") or [{}])[0]
                if info.get("mime", "").startswith(("image/", "video/", "audio/")):
                    license_name = info.get("extmetadata", {}).get("LicenseShortName", {}).get("value", "unknown")
                    media.append(f"{page.get('title', '')} | {info.get('mime', '')} | license {license_name} | direct media URL: "
                                 + (info.get("thumburl") or info.get("url") or ""))
            if media:
                results.append(f"Wikimedia Commons media candidates (search: {attempt}):\n" + "\n".join(media))
                break
        except (ServiceError, ValueError, httpx.HTTPError):
            continue
    if not results:
        raise ServiceError("Web and media search providers did not return results")
    return "\n".join(results)


async def page(url: str) -> str:
    data, mime, final_url = await safe_get(url, truncate=True)  # big pages (Wikipedia) are read in part
    if mime not in {"text/html", "text/plain", "application/xhtml+xml", ""}:
        raise ServiceError("That URL is not a readable web page")
    parser = _Text()
    parser.feed(data.decode("utf-8", errors="replace"))
    media = [urljoin(final_url, value) for value in parser.media_urls[:20]
             if value and not value.startswith(("data:", "javascript:"))]
    return f"{final_url}\n" + " ".join("".join(parser.parts).split())[:7500] +            ("\nMedia URLs: " + " | ".join(media)[:1400] if media else "")
