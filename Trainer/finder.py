"""Clip finder: Hermes searches video sites for source footage for a dataset.

A find job takes the shared GPU queue (Hermes' own model runs on the llama service), frees
ComfyUI's VRAM, then starts a real Hermes agent run through the Hermes API server
(/v1/runs). Hermes drives the search with the clip-finder skill, whose script calls the tool
endpoints below:

  search   known sites (YouTube, Dailymotion, archive.org, Bilibili) through yt-dlp / public
           APIs; ANY other site through search discovery (OpenSearch, search forms, common
           search URL patterns) or a remembered "site recipe"
  page     open any web page: title, text, numbered links (video pages marked), forms;
           --render loads it in headless Chromium for JavaScript sites
  submit   fill in and submit a form found by `page`
  learn    remember how a site's search works (/data/site_recipes.json)
  inspect  yt-dlp metadata of a video without downloading it
  propose  add a candidate source video (shown in the Datasets tab, one click to download)

Every tool call and every Hermes event (tool calls, reasoning, replies) lands in the job log,
which the Datasets tab shows in the Hermes terminal pane. A Hermes chat session can also run
a find job itself (`find.py start`), in which case no extra Hermes run is started.
"""
from __future__ import annotations

import asyncio
import calendar
import contextvars
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import urllib.parse
from contextlib import suppress
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

HERMES_URL = os.getenv("HERMES_API_URL", "http://127.0.0.1:8642").rstrip("/")
HERMES_KEY = os.getenv("HERMES_API_KEY", "")
LLAMA_URL = os.getenv("LLAMA_URL", "http://127.0.0.1:8400").rstrip("/")
LLAMA_KEY = os.getenv("LLAMA_API_KEY", "")
COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
HERMES_LOG = Path(os.getenv("HERMES_LOG", "/hermes-logs/agent.log"))
SCRIPT = "/opt/data/skills/media/clip-finder/scripts/find.py"   # path inside the Hermes container
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
ACTIVE = {"waiting_for_gpu", "preparing", "running"}
REASONING = ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
LOCAL_PROVIDERS = ("custom", "llama-cpp", "lmstudio", "ollama")   # these run on this machine's GPU
# Finder / link-fetch default (user's choice 2026-10-07): Qwen3.8 27B Uncensored, medium thinking.
# The provider is looked up in Hermes' inventory (the local one that lists this model).
FINDER_MODEL = os.getenv("FINDER_MODEL", "qwen-27b")
FINDER_REASONING = os.getenv("FINDER_REASONING", "medium")
KNOWN_SITES = {"youtube": "YouTube", "dailymotion": "Dailymotion", "archive": "Internet Archive", "bilibili": "Bilibili"}
SITE_ALIASES = {"yt": "youtube", "youtube.com": "youtube", "www.youtube.com": "youtube", "m.youtube.com": "youtube",
                "youtu.be": "youtube", "dailymotion.com": "dailymotion", "www.dailymotion.com": "dailymotion",
                "archive.org": "archive", "www.archive.org": "archive", "internet archive": "archive",
                "bilibili.com": "bilibili", "www.bilibili.com": "bilibili"}
VIDEO_PATH = re.compile(r"/(watch|video|videos|v|embed|clip|clips|movie|movies|details|view_video|play|player|media|film|episode|shorts|reel)s?(/|\?|$|[-_.=])|\.(mp4|webm|mov|m3u8)(\?|$)", re.I)
SEARCH_PATTERNS = ["/search?q={q}", "/search?query={q}", "/search?search_query={q}", "/?s={q}", "/search/{q}",
                   "/results?search_query={q}", "/search?k={q}", "/search?keyword={q}", "/search?term={q}"]

DEFAULT_FIND_PROMPT = """You are scouting the web for SOURCE VIDEOS for a LoRA training dataset in the BunnyBytes Trainer (job {job_id}, dataset "{dataset}"). The user watches your work live in the Trainer's Hermes terminal.

Looking for: {query}
{concept}Sites: {sites}
{duration}Goal: propose the {count} best source videos (never more than {max_count}).

Use the clip-finder helper for EVERYTHING on the web (no curl, no other scraping, never download videos yourself):
F="python3 {script}"
  $F search {job_id} SITE "query" [--n 10]      search a site. SITE = youtube, dailymotion, archive, bilibili, or ANY domain (example.com): for sites it doesn't know the helper finds the site's search box itself
  $F page {job_id} URL [--render] [--find WORD] open any page: title, text, numbered links (★ = looks like a video page), forms
  $F submit {job_id} URL --form N --set name=value [--set ...] [--render]   fill in and submit a form listed by `page`
  $F click {job_id} URL "button or link text"   click it in the browser (age gates, cookie walls, "load more", tabs); the search keeps its cookies afterwards
  $F learn {job_id} DOMAIN "https://DOMAIN/search?q={{q}}" ["note"]   remember a site's search URL once you worked it out
  $F inspect {job_id} URL                       title, length, resolution, description, chapters of a video (no download)
  $F propose {job_id} URL --score 1-10 --reason "why it is good" [--start M:SS --end M:SS]
  $F status {job_id}                             what you already proposed
  $F say {job_id} "one short line telling the user what you are doing"

How to work:
1. `say` your plan in one line. Search each site with 2-4 different phrasings (add words like footage, compilation, raw, 4k, no music, full scene when it helps).
2. A site you don't know: `search` it first. If that finds nothing, `page` its homepage (add --render when the page comes back nearly empty: JavaScript site), find its search form or search/category links, `submit` the form or open the search URL, and follow pagination links. When you find a search URL that works, `learn` it so next time is one call.
   If `page` says AGE GATE / CONSENT WALL (or shows almost no links plus buttons like "I am 18", "Enter", "Accept"), `click` that button once on that page; after that every page, search, inspect and download of this job carries the accepted cookies.
3. `inspect` promising results before proposing (when yt-dlp can't read a site, inspect plays the page in the browser and catches the stream itself: extractor "browser"; an inspect error is real, not rate limiting, so don't retry the same URL). Good sources: the subject is clearly visible for long stretches, 720p or better, little on-screen text or watermark, not a slideshow / talking head / reaction / lyric video / static image with music. When chapters or the description point at the good part, give --start/--end (only a hint for trimming: the user downloads whole videos).
4. Propose each good video RIGHT AFTER you inspect it: never collect a pool first. The user reviews, downloads and rejects proposals while you keep searching, and you can run out of helper calls. Score honestly (10 = perfect source). Search results and inspect tell you when a video was already downloaded or rejected by the user: skip those. Every helper reply ends with how many proposals you still need and how many helper calls you have left.
5. Stop when you have {count} good proposals or after about {max_steps} helper calls, whichever comes first. Finish with a 2-3 sentence summary of what you found and what was missing.
"""


DEFAULT_FETCH_PROMPT = """The user pasted a link into the BunnyBytes Trainer (Datasets → Download & trim) to get that video into the dataset "{dataset}" (job {job_id}). The plain downloader can't handle this site, so you fetch it. The user watches your work live in the Trainer's Hermes terminal.

Link: {url}

Use the clip-finder helper for everything (no curl, never download yourself):
F="python3 {script}"
  $F inspect {job_id} URL                    what video is at a URL (falls back to playing the page in the browser and catching the stream)
  $F page {job_id} URL [--render]            open the page: text, numbered links (★ = video page), BUTTONS, forms
  $F click {job_id} URL "button text"        click through an age gate / cookie wall / "play" (cookies are kept for the whole job)
  $F propose {job_id} VIDEO_URL --score 10 --reason "the linked video"   THIS DOWNLOADS IT (the whole video)
  $F say {job_id} "one short line telling the user what you are doing"

Steps:
1. `inspect` the link. If it is one video, `propose` exactly that URL and finish.
2. If inspect fails or finds nothing: `page` the link (add --render if it comes back nearly empty). If it shows an AGE GATE / CONSENT WALL, `click` the button that lets you in once, then `inspect` the link again.
3. If the page holds several videos (post, gallery, playlist), pick the one the link is about (the main / first one, or the one matching the page title), `inspect` it, `propose` it.
4. Propose exactly ONE video. Don't search for other videos. Don't retry an identical failing command more than once. If it really can't be downloaded, say why in one sentence.
Finish with one short sentence.
"""


class FindRequest(BaseModel):
    query: str = Field(min_length=2, max_length=400)
    concept: str = Field("", max_length=300)
    fetch_url: str = Field("", max_length=2000)    # "get THIS video" (Download & trim with a non-YouTube link)
    sites: list[str] = Field(default_factory=lambda: ["youtube"], max_length=12)
    count: int = Field(8, ge=1, le=30)
    min_seconds: int = Field(0, ge=0, le=36000)
    max_seconds: int = Field(0, ge=0, le=36000)
    auto_download: bool = False
    max_steps: int = Field(40, ge=5, le=200)
    unload_after: bool = True
    model: str = Field(FINDER_MODEL, max_length=200)   # "" = Hermes' own default model
    provider: str = Field("", max_length=100)          # "" with a model = the local provider that has it
    reasoning: Literal["default", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] = FINDER_REASONING  # type: ignore[assignment]
    source: Literal["trainer", "hermes"] = "trainer"


class SearchBody(BaseModel):
    site: str = Field(min_length=2, max_length=300)
    query: str = Field(min_length=1, max_length=300)
    n: int = Field(10, ge=1, le=30)


class PageBody(BaseModel):
    url: str = Field(min_length=4, max_length=2000)
    render: bool = False
    find: str = Field("", max_length=100)


class SubmitBody(BaseModel):
    url: str = Field(min_length=4, max_length=2000)
    form: int = Field(0, ge=0, le=50)
    fields: dict[str, str] = Field(default_factory=dict)
    render: bool = False


class LearnBody(BaseModel):
    domain: str = Field(min_length=3, max_length=200)
    template: str = Field(min_length=8, max_length=500)
    note: str = Field("", max_length=300)


class UrlBody(BaseModel):
    url: str = Field(min_length=4, max_length=2000)


class ProposeBody(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    title: str = Field("", max_length=300)
    reason: str = Field("", max_length=600)
    score: float = Field(5, ge=0, le=10)
    start: str | float | None = None
    end: str | float | None = None


class ClickBody(BaseModel):
    url: str = Field(min_length=4, max_length=2000)
    text: str = Field(min_length=1, max_length=120)


class SayBody(BaseModel):
    text: str = Field(min_length=1, max_length=400)


class DownloadBody(BaseModel):
    section: bool = False      # whole video by default: the user trims / auto-cuts it afterwards
    force: bool = False        # download again although the registry says it was downloaded before


class RejectBody(BaseModel):
    reason: str = Field("", max_length=300)


def to_seconds(v: Any) -> float | None:
    """'1:23', '01:02:03', '83', 83.0 -> seconds."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    parts = str(v).strip().split(":")
    try:
        total = 0.0
        for p in parts:
            total = total * 60 + float(p)
        return total
    except ValueError:
        return None


def fmt_dur(s: Any) -> str:
    if not isinstance(s, (int, float)) or s <= 0:
        return "?"
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ---- web access (public internet only) ----
# Browser session of the current find job (Playwright storage-state JSON): cookies survive
# between calls, so an age gate or cookie wall Hermes clicked through stays accepted.
SESSION: contextvars.ContextVar[Path | None] = contextvars.ContextVar("finder_session", default=None)


def session_cookies() -> list[dict[str, Any]]:
    state = SESSION.get()
    with suppress(OSError, ValueError):
        if state and state.is_file():
            return json.loads(state.read_text()).get("cookies", [])
    return []


def write_netscape_cookies(state: Path) -> Path:
    """Same cookies for yt-dlp (inspect / download), in the Netscape cookies.txt format."""
    out = state.with_suffix(".cookies.txt")
    lines = ["# Netscape HTTP Cookie File"]
    for c in json.loads(state.read_text()).get("cookies", []):
        dom = c["domain"]
        lines.append("\t".join([dom, "TRUE" if dom.startswith(".") else "FALSE", c.get("path") or "/", "TRUE" if c.get("secure") else "FALSE",
                                str(int(c["expires"])) if c.get("expires", -1) > 0 else "0", c["name"], c["value"]]))
    out.write_text("\n".join(lines) + "\n")
    return out
_host_ok: dict[str, bool] = {}


def public_host(host: str) -> bool:
    """False for localhost / LAN / link-local targets: this service runs on the host network."""
    host = (host or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return False
    if host in _host_ok:
        return _host_ok[host]
    try:
        infos = socket.getaddrinfo(host, None)
        ok = bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)
    except (OSError, ValueError):
        ok = False
    _host_ok[host] = ok
    return ok


def check_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(422, "Only http(s) web addresses.")
    if not public_host(p.hostname):
        raise HTTPException(422, f"{p.hostname} is not a public internet address.")
    return url


async def _guard(request: httpx.Request) -> None:
    if not await asyncio.to_thread(public_host, request.url.host):
        raise httpx.RequestError(f"blocked non-public address {request.url.host}", request=request)


def web_client(timeout: float = 20) -> httpx.AsyncClient:
    jar = httpx.Cookies()
    for c in session_cookies():
        with suppress(Exception):
            jar.set(c["name"], c["value"], domain=c["domain"], path=c.get("path") or "/")
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"},
                             cookies=jar, event_hooks={"request": [_guard]})


class PageParser(HTMLParser):
    """Links (with text / image / thumbnail), forms, title, meta, OpenSearch and visible text."""
    SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.title = ""
        self.meta: dict[str, str] = {}
        self.links: list[dict[str, Any]] = []
        self.forms: list[dict[str, Any]] = []
        self.opensearch: str | None = None
        self.text: list[str] = []
        self.buttons: list[str] = []
        self._button: list[str] | None = None
        self._a: dict[str, Any] | None = None
        self._form: dict[str, Any] | None = None
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "base" and a.get("href"):
            self.base = urllib.parse.urljoin(self.base, a["href"])
        elif tag == "meta":
            key = a.get("property") or a.get("name")
            if key and a.get("content") and key.lower() in {"og:title", "og:description", "description", "og:video", "og:video:url", "og:image", "og:type"}:
                self.meta[key.lower()] = a["content"][:500]
        elif tag == "link" and "search" in a.get("rel", "").lower() and "opensearch" in a.get("type", "").lower() and a.get("href"):
            self.opensearch = urllib.parse.urljoin(self.base, a["href"])
        elif tag == "a" and a.get("href") and not a["href"].startswith(("javascript:", "mailto:", "tel:", "#")):
            self._a = {"url": urllib.parse.urljoin(self.base, a["href"]).split("#")[0], "text": "", "title": a.get("title") or a.get("aria-label") or "", "thumb": None}
        elif tag == "img" and self._a is not None:
            src = a.get("data-src") or a.get("data-thumb") or a.get("src") or ""
            if src and not src.startswith("data:") and not self._a["thumb"]:
                self._a["thumb"] = urllib.parse.urljoin(self.base, src)
            if a.get("alt") and not self._a["title"]:
                self._a["title"] = a["alt"]
        elif tag == "button" or (tag == "input" and a.get("type", "").lower() in ("submit", "button") and a.get("value")):
            if tag == "button":
                self._button = [a.get("aria-label", "")]
            else:
                self.buttons.append(a["value"][:60])
        if tag == "form":
            self._form = {"action": urllib.parse.urljoin(self.base, a.get("action") or self.base), "method": (a.get("method") or "get").lower(),
                          "fields": [], "role": a.get("role", ""), "id": a.get("id") or a.get("class", "")[:40]}
            self.forms.append(self._form)
        elif tag in ("input", "textarea", "select") and self._form is not None and a.get("name"):
            t = (a.get("type") or ("text" if tag != "select" else "select")).lower()
            if t not in ("submit", "button", "image", "reset", "file", "password"):
                self._form["fields"].append({"name": a["name"], "type": t, "value": a.get("value", ""), "placeholder": a.get("placeholder", "")[:60]})

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._a is not None:
            self._a["text"] = re.sub(r"\s+", " ", self._a["text"]).strip()[:160]
            self.links.append(self._a)
            self._a = None
        elif tag == "form":
            self._form = None
        elif tag == "button" and self._button is not None:
            label = re.sub(r"\s+", " ", " ".join(self._button)).strip()[:60]
            if label and label not in self.buttons:
                self.buttons.append(label)
            self._button = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._a is not None:
            self._a["text"] += data
        if self._button is not None:
            self._button.append(data)
        if data.strip():
            self.text.append(data.strip())


_ie_classes: list[Any] | None = None


def ytdlp_supported(url: str) -> bool:
    """True when a site-specific yt-dlp extractor (not the generic one) matches the URL."""
    global _ie_classes
    if _ie_classes is None:
        with suppress(Exception):
            from yt_dlp.extractor import gen_extractor_classes
            _ie_classes = [c for c in gen_extractor_classes() if c.ie_key() not in ("Generic",) and "Search" not in c.ie_key()
                           and not getattr(c, "_SEARCH_KEY", None)]
        _ie_classes = _ie_classes or []
    for c in _ie_classes:
        with suppress(Exception):
            if c.suitable(url):
                return True
    return False


def looks_like_video(url: str, page_host: str) -> bool:
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https"):
        return False
    same_site = p.hostname and page_host and (p.hostname.split(".")[-2:] == page_host.split(".")[-2:])
    # .../free-stock-video/white-horse-chewing-1157/ : a video-ish word somewhere plus an item id at the end
    item = re.search(r"video|clip|footage|movie|film|watch|episode", p.path, re.I) and re.search(r"[-_/=]\d{3,}/?$", p.path)
    return bool((same_site and (VIDEO_PATH.search(p.path + ("?" + p.query if p.query else "")) or item)) or ytdlp_supported(url))


async def render_page(url: str, fill: tuple[int, dict[str, str]] | None = None, click: str | None = None,
                      save_state: bool = True) -> tuple[str, str, bytes | None]:
    """Load the page in headless Chromium (JavaScript sites); returns (final url, html, JPEG screenshot).
    `fill` = (form index, {field: value}): type into that form like a person and press Enter,
    so search boxes that JavaScript handles work too."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise HTTPException(501, "Headless browser not installed in the Trainer image.") from exc
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage", "--mute-audio"])
        try:
            state = SESSION.get()
            ctx = await browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900}, locale="en-US",
                                            storage_state=str(state) if state and state.is_file() else None)

            async def gate(route):
                host = urllib.parse.urlsplit(route.request.url).hostname or ""
                if route.request.resource_type in ("media", "font") or not await asyncio.to_thread(public_host, host):
                    await route.abort()
                else:
                    await route.continue_()
            await ctx.route("**/*", gate)
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            with suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=6000)
            if fill is not None:
                form = page.locator("form").nth(fill[0])
                last = None
                for name, value in fill[1].items():
                    box = form.locator(f'[name="{name}"]').first
                    if await box.count() == 0:
                        raise HTTPException(422, f"The form has no field named {name}.")
                    if not await box.is_visible():
                        # Collapsed header search etc.: use a visible box with the same name.
                        alt = page.locator(f'[name="{name}"]:visible').first
                        if await alt.count():
                            box = alt
                    if await box.is_visible():
                        await box.fill(value)
                        last = box
                    else:               # hidden field: set it directly
                        await box.evaluate("(e, v) => { e.value = v; }", value)
                if last is None:
                    raise HTTPException(422, "None of those fields can be typed into.")
                before = page.url
                await last.press("Enter")
                with suppress(Exception):
                    await page.wait_for_url(lambda u: u != before, timeout=8000)
                with suppress(Exception):
                    await page.wait_for_load_state("networkidle", timeout=6000)
            if click is not None:
                target = None
                pat = re.compile(re.escape(click), re.I)
                esc = click.replace("\\", "\\\\").replace('"', '\\"')
                for loc in (page.get_by_role("button", name=pat), page.get_by_role("link", name=pat),
                            page.locator(f'input[type=submit][value*="{esc}" i], input[type=button][value*="{esc}" i]'),
                            page.get_by_text(pat)):
                    with suppress(Exception):
                        for i in range(min(await loc.count(), 5)):
                            if await loc.nth(i).is_visible():
                                target = loc.nth(i)
                                break
                    if target is not None:
                        break
                if target is None:
                    raise HTTPException(422, f"Nothing visible to click with the text “{click}”.")
                before = page.url
                await target.click(timeout=8000)
                with suppress(Exception):
                    await page.wait_for_url(lambda u: u != before, timeout=6000)
                with suppress(Exception):
                    await page.wait_for_load_state("networkidle", timeout=6000)
            with suppress(Exception):   # lazy-loaded result grids
                await page.mouse.wheel(0, 4000)
                await page.wait_for_timeout(1200)
            with suppress(Exception):   # cookie / consent overlays would drown the page text
                await page.evaluate("""() => document.querySelectorAll('[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i],[id*=gdpr i],[class*=gdpr i],[aria-label*=cookie i],[aria-label*=consent i]')
                    .forEach((e) => { if (!e.closest('main') && e.tagName !== 'BODY' && e.tagName !== 'HTML') e.remove(); })""")
            shot = None
            with suppress(Exception):   # what the page looks like, for the Hermes timeline
                await page.evaluate("() => window.scrollTo(0, 0)")
                await page.wait_for_timeout(250)
                shot = await page.screenshot(type="jpeg", quality=70)
            html = await page.content()
            if state and save_state:
                with suppress(Exception):
                    await ctx.storage_state(path=str(state))
                    write_netscape_cookies(state)
            return page.url, html, shot
        finally:
            await browser.close()


MEDIA_URL = re.compile(r"\.(m3u8|mpd)(\?|$)|\.(mp4|webm|m4v|mov)(\?|$)", re.I)


async def sniff_video(url: str) -> dict[str, Any] | None:
    """For sites yt-dlp can't read: play the page in the browser (with the job's cookies) and
    catch the stream the player requests (HLS/DASH playlist or a plain video file)."""
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage", "--mute-audio", "--autoplay-policy=no-user-gesture-required"])
        try:
            state = SESSION.get()
            ctx = await browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900}, locale="en-US",
                                            storage_state=str(state) if state and state.is_file() else None)

            async def gate(route):
                host = urllib.parse.urlsplit(route.request.url).hostname or ""
                if not await asyncio.to_thread(public_host, host):
                    await route.abort()
                else:
                    await route.continue_()
            await ctx.route("**/*", gate)
            page = await ctx.new_page()
            seen: list[str] = []
            page.on("request", lambda r: seen.append(r.url) if MEDIA_URL.search(r.url.split("#")[0]) or r.resource_type == "media" else None)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            with suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=6000)
            with suppress(Exception):       # players that wait for a click / autoplay
                await page.evaluate("() => document.querySelectorAll('video').forEach(v => { v.muted = true; v.play().catch(() => {}); })")
                await page.wait_for_timeout(2500)
            info = await page.evaluate("""() => {
                const v = document.querySelector('video'), m = (p) => document.querySelector(`meta[property="${p}"],meta[name="${p}"]`)?.content || null;
                return {title: m('og:title') || document.title, description: m('og:description') || m('description'), thumb: m('og:image'),
                        duration: v && isFinite(v.duration) ? v.duration : null, height: v ? v.videoHeight || null : null,
                        src: v && /^https?:/.test(v.currentSrc || '') ? v.currentSrc : null};
            }""")
        finally:
            await browser.close()
    streams = [u for u in seen if u.startswith("http")]
    if info.get("src"):
        streams.insert(0, info["src"])
    if not streams:
        return None
    # A master playlist beats variant playlists and segments; then manifests, then files.
    rank = lambda u: (0 if re.search(r"(index|master|playlist|manifest)[^/]*\.(m3u8|mpd)", u, re.I) else 1 if re.search(r"\.(m3u8|mpd)", u, re.I) else 2)
    return {**info, "stream": sorted(dict.fromkeys(streams), key=rank)[0]}


async def fetch_page(url: str, render: bool = False, data: dict[str, str] | None = None) -> dict[str, Any]:
    url = check_url(url)
    rendered = False
    shot = None
    if render and data is None:
        final, html, shot = await render_page(url)
        rendered = True
    else:
        async with web_client() as c:
            r = await (c.post(url, data=data) if data is not None else c.get(url))
        if r.status_code in (401, 403, 429, 503) and data is None:
            # Bot walls (Cloudflare & co.) often let a real browser through.
            with suppress(HTTPException):
                return await fetch_page(url, render=True)
        if r.status_code >= 400:
            raise HTTPException(502, f"{urllib.parse.urlsplit(url).hostname} answered HTTP {r.status_code}.")
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype:
            raise HTTPException(422, f"Not a web page ({ctype or 'unknown type'}); use inspect for video links.")
        final, html = str(r.url), r.text[:3_000_000]
    return {**parse_page(final, html, rendered), "shot": shot}


def parse_page(final: str, html: str, rendered: bool) -> dict[str, Any]:
    p = PageParser(final)
    with suppress(Exception):
        p.feed(html)
    host = urllib.parse.urlsplit(final).hostname or ""
    seen, links = set(), []
    for ln in p.links:
        if ln["url"] in seen or not ln["url"].startswith("http"):
            continue
        seen.add(ln["url"])
        ln["video"] = looks_like_video(ln["url"], host)
        ln["label"] = ln["text"] or ln["title"] or ""
        links.append(ln)
    text = re.sub(r"\s+", " ", " ".join(p.text)).strip()
    return {"url": final, "host": host, "title": re.sub(r"\s+", " ", p.title).strip()[:200], "meta": p.meta, "text": text,
            "links": links, "forms": p.forms, "opensearch": p.opensearch, "rendered": rendered, "buttons": p.buttons[:25],
            "gate": bool(re.search(r"18\+|over 18|are you 18|i am 18|i'm 18|age verification|verify (your )?age|adult content|"
                                   r"enter the site|mature content|accept (all )?cookies|consent", text[:4000], re.I)) and len(links) < 40,
            "thin": len(text) < 400 or len(links) < 8}


def search_forms(page: dict[str, Any]) -> list[tuple[int, dict[str, Any], str]]:
    """(index, form, name of its text field) for forms that look like a site search."""
    out = []
    for i, f in enumerate(page["forms"]):
        text_fields = [x for x in f["fields"] if x["type"] in ("text", "search")]
        if not text_fields:
            continue
        hint = " ".join([f["action"], f["role"], f["id"], *[x["name"] + " " + x["placeholder"] for x in text_fields]]).lower()
        named = next((x for x in text_fields if x["type"] == "search" or re.search(r"^(q|query|s|k|kw|keyword|keywords|search|search_query|term|text|w)$", x["name"], re.I)), None)
        if named or "search" in hint or "find" in hint:
            out.append((i, f, (named or text_fields[0])["name"]))
    out.sort(key=lambda t: 0 if "search" in (t[1]["action"] + t[1]["role"]).lower() else 1)
    return out


def form_url(form: dict[str, Any], values: dict[str, str]) -> tuple[str, dict[str, str]]:
    data = {f["name"]: f["value"] for f in form["fields"] if f["type"] not in ("checkbox", "radio") or f["value"]}
    data.update(values)
    if form["method"] == "post":
        return form["action"], data
    split = urllib.parse.urlsplit(form["action"])
    return urllib.parse.urlunsplit((split.scheme, split.netloc, split.path, urllib.parse.urlencode(data), "")), {}


def build_router(prefix: str, data_dir: Path, queue_request, datasets_router) -> tuple[APIRouter, Any]:
    router = APIRouter(prefix=prefix)
    jobs: dict[str, dict[str, Any]] = {}
    tasks: dict[str, asyncio.Task] = {}
    fetch_order: list[str] = []
    fetch_pump_task: asyncio.Task | None = None
    recipes_file = data_dir / "site_recipes.json"
    settings_file = data_dir / "tagging.json"
    thumb_dir = data_dir / "thumbs" / "_finds"
    browser_dir = data_dir / "browser"            # per-job browser sessions (cookies), pruned after 7 days

    models_cache: dict[str, Any] = {}

    async def hermes_models() -> dict[str, Any]:
        """Hermes' own model-picker inventory: providers that are set up and their models."""
        if models_cache.get("t", 0) > time.time() - 60:
            return models_cache["data"]
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(HERMES_URL + "/api/model/options", headers=hermes_headers())
            r.raise_for_status()
            d = r.json()
        providers = []
        for pr in d.get("providers", []):
            models = [m if isinstance(m, str) else (m.get("id") or m.get("name")) for m in pr.get("models") or []]
            models = [m for m in models if m]
            if pr.get("authenticated") and models and pr.get("slug") != "moa":
                providers.append({"slug": pr["slug"], "name": pr.get("name") or pr["slug"], "models": models,
                                  "local": is_local(pr["slug"]), "current": bool(pr.get("is_current"))})
        providers.sort(key=lambda x: (not x["current"], not x["local"], x["name"].lower()))
        data = {"providers": providers, "default": {"model": d.get("model"), "provider": d.get("provider")}, "reasoning": REASONING,
                "finder_default": {"model": FINDER_MODEL, "reasoning": FINDER_REASONING,
                                   "provider": next((pr["slug"] for pr in providers if pr["local"] and FINDER_MODEL in pr["models"]), "")}}
        models_cache.update(t=time.time(), data=data)
        return data

    def is_local(provider: str) -> bool:
        return (provider or "").split(":")[0] in LOCAL_PROVIDERS

    # ---- screenshots for the Hermes timeline ----
    shots_root = data_dir / "thumbs" / "_shots"
    shot_sem = asyncio.Semaphore(1)
    shot_tasks: set[asyncio.Task] = set()
    MAX_SHOTS = 80

    def save_shot(job: dict[str, Any], url: str, title: str, data: bytes) -> None:
        folder = shots_root / job["id"]
        folder.mkdir(parents=True, exist_ok=True)
        job["shots"] = n = job.get("shots", 0) + 1
        (folder / f"{n}.jpg").write_bytes(data)
        ev(job, "shot", title or url, shot=n, url=url)

    async def background_shot(job: dict[str, Any], url: str, title: str) -> None:
        try:
            async with shot_sem:
                _, _, data = await render_page(url, save_state=False)
            if data:
                save_shot(job, url, title, data)
        except Exception:
            pass                      # a missing screenshot never matters to the search
        finally:
            job["shots_pending"] -= 1

    def snap(job: dict[str, Any], p: dict[str, Any]) -> None:
        """Every page Hermes opens shows up as a screenshot in its timeline. Pages it already
        rendered come with one; plain-HTML fetches get one rendered in the background."""
        if p.get("shot"):
            save_shot(job, p["url"], p.get("title") or "", p["shot"])
        elif not p.get("no_bg_shot") and job.get("shots", 0) + job.setdefault("shots_pending", 0) < MAX_SHOTS:
            job["shots_pending"] += 1
            t = asyncio.create_task(background_shot(job, p["url"], p.get("title") or ""))
            shot_tasks.add(t)
            t.add_done_callback(shot_tasks.discard)

    def prune_shots() -> None:
        with suppress(OSError):
            for f in browser_dir.glob("find*"):
                if f.stat().st_mtime < time.time() - 7 * 86400:
                    f.unlink(missing_ok=True)
        with suppress(OSError):
            for d in shots_root.iterdir():
                if d.is_dir() and d.stat().st_mtime < time.time() - 7 * 86400:
                    for f in d.iterdir():
                        f.unlink(missing_ok=True)
                    d.rmdir()

    def find_prompt(key: str = "find_prompt") -> str:
        default = DEFAULT_FETCH_PROMPT if key == "fetch_prompt" else DEFAULT_FIND_PROMPT
        with suppress(OSError, ValueError):
            return json.loads(settings_file.read_text(encoding="utf-8")).get(key) or default
        return default

    def recipes() -> dict[str, Any]:
        with suppress(OSError, ValueError):
            return json.loads(recipes_file.read_text(encoding="utf-8"))
        return {}

    def save_recipe(domain: str, template: str, how: str, note: str = "") -> None:
        r = recipes()
        r[domain] = {"template": template, "how": how, "note": note, "saved": time.strftime("%Y-%m-%d %H:%M")}
        tmp = recipes_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(r, indent=1), encoding="utf-8")
        tmp.replace(recipes_file)

    def ev(job: dict[str, Any], kind: str, text: str, **extra: Any) -> None:
        """Session log for the Hermes terminal pane (kinds: user, system, tool, assistant, think, ok, warn, error)."""
        job["log"].append({"t": time.time(), "kind": kind, "text": text, **extra})
        if kind in ("tool", "assistant", "ok", "warn", "error", "say"):
            job["status_line"] = text if kind != "tool" else f"{extra.get('icon', '⚙')} {text}"
        if len(job["log"]) > 4000:
            del job["log"][:500]
            job["log_offset"] = job.get("log_offset", 0) + 500

    rejected_file = data_dir / "rejected.json"
    vkey = datasets_router.video_key

    def rejected_all() -> dict[str, dict[str, Any]]:
        with suppress(OSError, ValueError):
            return json.loads(rejected_file.read_text(encoding="utf-8"))
        return {}

    def rejected_entry(dataset: str, url: str) -> dict[str, Any] | None:
        return rejected_all().get(dataset, {}).get(vkey(url))

    def set_rejected(dataset: str, cand: dict[str, Any], reason: str | None) -> None:
        """reason None = undo. Kept per dataset so later searches never propose it again."""
        data = rejected_all()
        bucket = data.setdefault(dataset, {})
        if reason is None:
            bucket.pop(vkey(cand["url"]), None)
        else:
            bucket[vkey(cand["url"])] = {"url": cand["url"], "title": cand["title"], "reason": reason, "at": time.strftime("%Y-%m-%d %H:%M")}
        tmp = rejected_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(rejected_file)

    def active_cands(job: dict[str, Any]) -> list[dict[str, Any]]:
        return [c for c in job["candidates"] if not c.get("rejected")]

    def budget(job: dict[str, Any]) -> dict[str, Any]:
        """Footer for every helper reply: what Hermes still needs and how many calls it has left."""
        left = max(0, job["max_steps"] - job["steps"])
        n = len(active_cands(job))
        hint = ("target reached: finish with your summary" if n >= job["count"] else
                "almost out of helper calls: propose the best videos you already inspected NOW" if left <= 5 else
                "propose good videos as soon as you have inspected them")
        return {"proposed": n, "target": job["count"], "calls_left": left, "hint": hint}

    def known_flags(job: dict[str, Any], url: str) -> dict[str, Any]:
        """Already downloaded (any dataset) / rejected by the user (this dataset)?"""
        out: dict[str, Any] = {}
        prev = datasets_router.downloaded(url)
        if prev:
            out["downloaded"] = f"{prev['path']} ({prev['at']}{'' if prev.get('exists') else ', since trimmed/removed'})"
        rej = rejected_entry(job["dataset"], url)
        if rej:
            out["rejected"] = rej.get("reason") or "rejected by the user"
        return out

    def get_job(job_id: str, running: bool = False) -> dict[str, Any]:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Unknown find job.")
        if running and job["state"] not in ACTIVE:
            raise HTTPException(409, f"This find job is {job['state']}.")
        if running:
            job["steps"] += 1
        browser_dir.mkdir(parents=True, exist_ok=True)
        SESSION.set(browser_dir / f"{job['id']}.json")
        return job

    def active_download(job: dict[str, Any]) -> dict[str, Any] | None:
        for candidate in job["candidates"]:
            task = datasets_router.download_status(candidate["download"]) if candidate.get("download") else None
            if task and task["state"] in {"starting", "downloading", "merging"}:
                return task
        return None

    def fetch_blocker(job: dict[str, Any]) -> dict[str, Any] | None:
        return next((other for other in jobs.values() if other is not job and
                     ((other.get("fetch_url") and (other["state"] in ACTIVE or active_download(other))) or
                      (other["dataset"] == job["dataset"] and other["state"] in ACTIVE))), None)

    async def pump_fetches() -> None:
        nonlocal fetch_pump_task
        try:
            while fetch_order:
                job_id = fetch_order[0]
                job = jobs.get(job_id)
                if not job or (job["state"] in {"cancelled", "failed"} and not active_download(job)):
                    fetch_order.pop(0)
                    continue
                if job["state"] == "queued":
                    blocker = fetch_blocker(job)
                    if blocker:
                        job["stage"] = (f"Waiting for {blocker['fetch_url']}" if blocker.get("fetch_url")
                                        else "Waiting for the current Hermes search")
                        await asyncio.sleep(1.5)
                        continue
                    try:
                        await launch(job)
                    except Exception as exc:
                        job.update(state="failed", stage="Could not start Hermes", error=str(exc)[:300])
                        ev(job, "error", job["error"])
                        fetch_order.pop(0)
                        continue
                if job["state"] in ACTIVE or active_download(job):
                    await asyncio.sleep(1.5)
                    continue
                fetch_order.pop(0)
        finally:
            fetch_pump_task = None

    def ensure_fetch_pump() -> None:
        nonlocal fetch_pump_task
        if not fetch_pump_task or fetch_pump_task.done():
            fetch_pump_task = asyncio.create_task(pump_fetches())

    def public(job: dict[str, Any]) -> dict[str, Any]:
        cands = []
        for c in job["candidates"]:
            d = datasets_router.download_status(c["download"]) if c.get("download") else None
            cands.append({**c, "download": d})
        return {k: job[k] for k in ("id", "dataset", "query", "concept", "sites", "count", "state", "stage", "error", "created",
                                    "source", "run_id", "status_line", "steps", "max_steps", "auto_download", "searches", "summary")} \
            | {k: job.get(k) for k in ("model", "provider", "reasoning", "local", "fetch_url")} \
            | {"candidates": cands, "log_len": job.get("log_offset", 0) + len(job["log"]),
               "queue_position": fetch_order.index(job["id"]) + 1 if job["id"] in fetch_order else None}

    def site_key(site: str) -> str:
        s = site.strip().lower()
        s = SITE_ALIASES.get(s, s)
        if s in KNOWN_SITES:
            return s
        host = urllib.parse.urlsplit(s if "://" in s else "https://" + s).hostname or s
        return SITE_ALIASES.get(host, host)

    def remember(job: dict[str, Any], r: dict[str, Any]) -> None:
        job["seen"][r["url"]] = {**job["seen"].get(r["url"], {}), **{k: v for k, v in r.items() if v not in (None, "")}}

    # ---- search adapters ----
    async def yt_dlp_json(args: list[str], timeout: float = 90) -> Any:
        proc = await asyncio.create_subprocess_exec("yt-dlp", "--no-warnings", "-J", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(504, "yt-dlp took too long.")
        if proc.returncode:
            msg = " ".join(x for x in err.decode(errors="replace").splitlines() if "ERROR" in x)[-400:]
            raise HTTPException(502, msg or f"yt-dlp exited with {proc.returncode}")
        return json.loads(out)

    def thumb_of(e: dict[str, Any]) -> str | None:
        if e.get("thumbnail"):
            return e["thumbnail"]
        th = [t for t in (e.get("thumbnails") or []) if t.get("url")]
        return th[-1]["url"] if th else None

    async def search_site(site: str, query: str, n: int) -> dict[str, Any]:
        if site == "youtube":
            d = await yt_dlp_json(["--flat-playlist", f"ytsearch{n}:{query}"])
            res = [{"url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}", "title": e.get("title"), "duration": e.get("duration"),
                    "uploader": e.get("channel") or e.get("uploader"), "views": e.get("view_count"), "thumb": thumb_of(e), "site": "youtube"}
                   for e in d.get("entries") or [] if e]
            return {"how": "yt-dlp ytsearch", "results": res}
        if site == "bilibili":
            d = await yt_dlp_json(["--playlist-items", f"1:{min(n, 8)}", f"bilisearch{min(n, 8)}:{query}"], timeout=150)
            res = [{"url": e.get("webpage_url") or e.get("original_url"), "title": e.get("title"), "duration": e.get("duration"),
                    "uploader": e.get("uploader"), "views": e.get("view_count"), "thumb": thumb_of(e), "site": "bilibili"}
                   for e in d.get("entries") or [] if e]
            return {"how": "yt-dlp bilisearch", "results": res}
        if site == "dailymotion":
            async with web_client() as c:
                r = await c.get("https://api.dailymotion.com/videos", params={"search": query, "limit": n, "fields": "id,title,duration,thumbnail_360_url,owner.screenname,views_total,url"})
                r.raise_for_status()
            res = [{"url": e["url"], "title": e.get("title"), "duration": e.get("duration"), "uploader": e.get("owner.screenname"),
                    "views": e.get("views_total"), "thumb": e.get("thumbnail_360_url"), "site": "dailymotion"} for e in r.json().get("list", [])]
            return {"how": "Dailymotion API", "results": res}
        if site == "archive":
            async with web_client() as c:
                r = await c.get("https://archive.org/advancedsearch.php", params=[("q", f"(title:({query}) OR subject:({query})) AND mediatype:movies"), ("sort[]", "downloads desc"), ("fl[]", "identifier"), ("fl[]", "title"),
                                                                                    ("fl[]", "runtime"), ("fl[]", "downloads"), ("fl[]", "creator"), ("rows", str(n)), ("output", "json")])
                r.raise_for_status()
            res = [{"url": f"https://archive.org/details/{e['identifier']}", "title": e.get("title") if isinstance(e.get("title"), str) else str(e.get("title")),
                    "duration": to_seconds(e.get("runtime")) if isinstance(e.get("runtime"), (str, int, float)) else None,
                    "uploader": e.get("creator") if isinstance(e.get("creator"), str) else None, "views": e.get("downloads"),
                    "thumb": f"https://archive.org/services/img/{e['identifier']}", "site": "archive"} for e in r.json()["response"]["docs"]]
            return {"how": "archive.org advanced search", "results": res}
        return await search_any(site, query, n)

    def results_from(page: dict[str, Any], query: str, n: int, chrome: set[str] = frozenset()) -> list[dict[str, Any]]:
        """Video links on a search-result page. `chrome` = links the homepage has too (menus,
        footers, social icons): never results. Links to other sites only count when they mention the query."""
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        site = ".".join(page["host"].split(".")[-2:])

        def ok(ln: dict[str, Any]) -> bool:
            if not ln["video"] or ln["url"] in chrome or ln["url"].rstrip("/") == page["url"].rstrip("/"):
                return False
            own = (urllib.parse.urlsplit(ln["url"]).hostname or "").endswith(site)
            return own or any(w in (ln["label"] + " " + ln["url"]).lower() for w in words)
        vids = [ln for ln in page["links"] if ok(ln)]
        real = [ln for ln in vids if not re.search(r"/search|[?&](q|query|s|k)=", ln["url"], re.I)]
        vids = real or vids          # "related searches" chips only when there is nothing else
        # Prefer links whose label mentions the query; keep page order otherwise.
        vids.sort(key=lambda ln: -sum(w in (ln["label"] + " " + ln["url"]).lower() for w in words))
        merged: dict[str, dict[str, Any]] = {}
        for ln in vids:
            m = merged.setdefault(ln["url"], {"url": ln["url"], "title": "", "thumb": None, "site": page["host"]})
            if len(ln["label"]) > len(m["title"] or ""):
                m["title"] = ln["label"]
            m["thumb"] = m["thumb"] or ln.get("thumb")
        for m in merged.values():
            if not m["title"]:     # thumbnail-only cards: make a title from the URL slug
                slug = [x for x in urllib.parse.urlsplit(m["url"]).path.split("/") if x]
                m["title"] = re.sub(r"[-_]+", " ", re.sub(r"[-_]?\d{5,}$", "", slug[-1] if slug else "")).strip() or m["url"]
        return list(merged.values())[:n]

    async def search_any(domain: str, query: str, n: int) -> dict[str, Any]:
        """Search a site this service has no adapter for: remembered recipe, else discover its search."""
        domain = domain.removeprefix("www.") if domain.count(".") > 1 and domain.startswith("www.") else domain
        q = urllib.parse.quote_plus(query)
        rec = recipes().get(domain)
        tried: list[str] = []
        if rec:
            url = rec["template"].replace("{q}", q)
            page = await fetch_page(url, render=rec.get("render", False))
            if page["thin"] and not page["rendered"]:
                page = await fetch_page(url, render=True)
            res = results_from(page, query, n)
            if res:
                return {"how": f"saved recipe ({rec['how']})", "template": rec["template"], "results": res, "rendered": page["rendered"], "_page": page}
            tried.append(f"saved recipe {rec['template']} gave no video links")
        home = await fetch_page(f"https://{domain}/")
        if home["thin"]:
            with suppress(HTTPException):
                home = await fetch_page(home["url"], render=True)
        origin = f"{urllib.parse.urlsplit(home['url']).scheme}://{urllib.parse.urlsplit(home['url']).netloc}"
        chrome = {ln["url"] for ln in home["links"]}
        candidates: list[tuple[str, str, dict[str, str] | None]] = []   # (how, url or template, post data)
        if home["opensearch"]:
            with suppress(Exception):
                async with web_client(10) as c:
                    x = (await c.get(home["opensearch"])).text
                m = re.search(r'<Url[^>]*type="text/html"[^>]*template="([^"]+)"', x) or re.search(r'<Url[^>]*template="([^"]+)"[^>]*type="text/html"', x)
                if m:
                    t = m.group(1).replace("&amp;", "&")
                    candidates.append(("OpenSearch", re.sub(r"\{searchTerms\}", "{q}", re.sub(r"\{[a-zA-Z:]+\?\}", "", t)), None))
        for i, form, field in search_forms(home):
            url, data = form_url(form, {field: "{q}"})
            candidates.append((f"search form #{i}", url, data or None))
        candidates += [("URL pattern", origin + pat, None) for pat in SEARCH_PATTERNS]
        for how, template, data in candidates[:12]:
            try:
                if data is not None:
                    page = await fetch_page(template, data={k: v.replace("{q}", query) for k, v in data.items()})
                else:
                    url = template.replace("%7Bq%7D", q).replace("{q}", q)
                    page = await fetch_page(url, render=home["rendered"])
                    if page["thin"] and not page["rendered"] and how != "URL pattern":
                        page = await fetch_page(url, render=True)
            except HTTPException as exc:
                tried.append(f"{how}: {exc.detail}")
                continue
            res = results_from(page, query, n, chrome)
            # Landed on a hub whose "results" are more search pages (e.g. photo search with a
            # Videos tab): follow the first one, one level only.
            if res and data is None and all(re.search(r"search|[?&](q|query|s|k)=", r["url"], re.I) for r in res):
                with suppress(HTTPException, httpx.HTTPError):
                    sub = await fetch_page(res[0]["url"], render=page["rendered"])
                    res2 = results_from(sub, query, n, chrome | {r["url"] for r in res})
                    if res2:
                        enc_q = next((x for x in (q, urllib.parse.quote(query), query) if x in res[0]["url"]), None)
                        how, res = f"{how} → {res[0]['title'] or 'sub-search'}", res2
                        if enc_q and enc_q in sub["url"]:
                            template = sub["url"].replace(enc_q, "{q}")
            if res:
                if data is None:
                    with suppress(OSError):
                        save_recipe(domain, template.replace("%7Bq%7D", "{q}"), how, "found automatically")
                return {"how": how, "template": None if data else template, "results": res, "rendered": page["rendered"], "tried": tried,
                        "_page": page if data is None else {**page, "shot": None, "no_bg_shot": True}}
            tried.append(f"{how}: no video links")
        return {"how": "discovery failed", "results": [], "tried": tried,
                "hint": f"Open the homepage with: page https://{domain}/ --render, look for its search form / category links, then submit or follow them."}

    # ---- Hermes run ----
    def hermes_headers() -> dict[str, str]:
        return {"Authorization": f"Bearer {HERMES_KEY}"} if HERMES_KEY else {}

    def short_cmd(preview: str) -> str:
        return re.sub(r"python3 \S*find\.py", "find.py", preview or "").strip()[:400]

    async def drive_hermes(job: dict[str, Any]) -> None:
        sites = ", ".join(KNOWN_SITES.get(s, s) for s in job["sites"])
        dur = ""
        if job["min_seconds"] or job["max_seconds"]:
            dur = "Video length: " + (f"at least {fmt_dur(job['min_seconds'])}" if job["min_seconds"] else "") \
                  + (" and " if job["min_seconds"] and job["max_seconds"] else "") + (f"at most {fmt_dur(job['max_seconds'])}" if job["max_seconds"] else "") + "\n"
        values = {"job_id": job["id"], "dataset": job["dataset"], "query": job["query"], "sites": sites, "count": job["count"],
                  "max_count": job["count"] + 4, "max_steps": job["max_steps"], "script": SCRIPT, "duration": dur,
                  "concept": f"What the LoRA teaches: {job['concept']}\n" if job["concept"] else "", "url": job.get("fetch_url") or ""}
        fetch = bool(job.get("fetch_url"))
        template = find_prompt("fetch_prompt" if fetch else "find_prompt")
        try:
            prompt = template.format(**values)
        except (KeyError, IndexError, ValueError) as exc:
            ev(job, "warn", f"saved {'link fetch' if fetch else 'find'} prompt has a bad placeholder ({exc}); using the default")
            prompt = (DEFAULT_FETCH_PROMPT if fetch else DEFAULT_FIND_PROMPT).format(**values)
        if job.get("resume") and not fetch:
            kept = active_cands(job)
            rej = [c for c in job["candidates"] if c.get("rejected")]
            need = max(1, job["count"] - len(kept))
            prompt += ("\n\nCONTINUATION: this search already ran and the user reviewed your proposals.\n"
                       + ("Kept (don't propose again):\n" + "".join(f"- {c['title']} ({c['url']})\n" for c in kept) if kept else "")
                       + "REJECTED by the user (never propose these again; avoid videos like them"
                       + (", and learn from the reasons" if any(c.get("rejected") not in (True, "") for c in rej) else "") + "):\n"
                       + "".join(f"- {c['title']} ({c['url']})" + (f" — {c['rejected']}" if isinstance(c.get("rejected"), str) and c["rejected"] else "") + "\n" for c in rej)
                       + f"Find {need} more good video(s). Search with new phrasings too; propose each right after inspecting it.\n")
        if job.get("cancel"):           # stopped before Hermes was even started
            job["hermes_status"] = "cancelled"
            return
        run_body: dict[str, Any] = {"input": prompt}
        if job["model"]:
            run_body["model"] = job["model"]
            if job["provider"]:
                run_body["provider"] = job["provider"]
        if job["reasoning"] != "default":
            run_body["model_options"] = {"reasoning_effort": job["reasoning"]}
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=None)) as c:
            r = await c.post(HERMES_URL + "/v1/runs", headers=hermes_headers(), json=run_body)
            if r.status_code >= 300:
                raise RuntimeError(f"Hermes API answered {r.status_code}: {r.text[:200]}")
            job["run_id"] = r.json()["run_id"]
            job.setdefault("run_ids", []).append(job["run_id"])
            ev(job, "system", f"Hermes run {job['run_id']} started · model {job['model'] or 'Hermes default'}"
               + (f" ({job['provider']})" if job["provider"] else "") + f" · thinking {job['reasoning']}"
               + " · lines tagged hermes come from its own agent.log")
            job.update(state="running", stage="Hermes is searching")
            say: list[str] = []

            def flush_say(clean: str | None = None):
                """One log line per reply. Hermes' stream breaks the reply after its first token,
                while the matching reasoning event carries the clean text: prefer that."""
                text = clean or re.sub(r"[ \t]*\n\s*\n[ \t]*", "\n", "".join(say)).strip()
                say.clear()
                if text:
                    ev(job, "assistant", text[:1500])
            async with c.stream("GET", f"{HERMES_URL}/v1/runs/{job['run_id']}/events", headers=hermes_headers()) as s:
                async for line in s.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    with suppress(ValueError):
                        e = json.loads(line[5:].strip())
                        kind = e.get("event", "")
                        if kind == "message.delta":
                            say.append(e.get("delta") or "")
                        elif kind == "reasoning.available":
                            text = (e.get("text") or "").strip()
                            squash = lambda t: re.sub(r"\s+", "", t)
                            if text and say and squash(text) == squash("".join(say)):
                                flush_say(text)          # same words as the reply: show it once
                            elif text:
                                ev(job, "think", text[:1200])
                        elif kind == "tool.started":
                            flush_say()
                            ev(job, "tool", f"{e.get('tool')}  {short_cmd(e.get('preview') or '')}", icon="⚡", hermes_tool=True)
                        elif kind == "tool.completed":
                            if e.get("error"):
                                ev(job, "warn", f"{e.get('tool')} failed after {e.get('duration', 0):.1f}s")
                        elif kind == "approval.request":
                            ev(job, "warn", f"Hermes asked to run something that needs approval ({str(e.get('command') or '')[:120]}); denied")
                            with suppress(Exception):
                                await c.post(f"{HERMES_URL}/v1/runs/{job['run_id']}/approval", headers=hermes_headers(), json={"choice": "deny"})
                        elif kind.startswith("run."):
                            flush_say()
                            status = kind[4:]
                            if status == "completed":
                                job["summary"] = (e.get("output") or "").strip()[:3000]
                            elif status in ("failed", "cancelled", "interrupted"):
                                job["error"] = e.get("error") or status
                            job["hermes_status"] = status
                            break
            flush_say()
            if "hermes_status" not in job:   # stream closed without a terminal event: ask for the status
                st = (await c.get(f"{HERMES_URL}/v1/runs/{job['run_id']}", headers=hermes_headers())).json()
                job["hermes_status"] = st.get("status")
                job["summary"] = job["summary"] or (st.get("output") or "")[:3000]
                job["error"] = job["error"] or st.get("error")

    async def run(job_id: str, token: str | None) -> None:
        """token None = a cloud model: no GPU queue slot, no VRAM juggling."""
        job = jobs[job_id]
        if token:
            ev(job, "system", f"GPU queue slot acquired after {time.time() - job['created']:.0f}s")
        stop = asyncio.Event()

        async def heartbeat():
            while token and not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=20)
                except asyncio.TimeoutError:
                    with suppress(Exception):
                        await queue_request("/heartbeat", {"token": token}, timeout=6)
        hb = asyncio.create_task(heartbeat())
        try:
            if token:
                job.update(state="preparing", stage="Freeing ComfyUI VRAM")
                ev(job, "tool", "comfyui.free  unload_models=true free_memory=true", icon="🧹")
                async with httpx.AsyncClient(timeout=20) as c:
                    try:
                        fr = await c.post(COMFY_URL + "/free", json={"unload_models": True, "free_memory": True})
                        ev(job, "ok", f"ComfyUI VRAM released (HTTP {fr.status_code})")
                    except Exception as exc:
                        ev(job, "warn", f"ComfyUI not reachable ({type(exc).__name__}); continuing")
            job.update(state="preparing", stage="Starting Hermes (its model loads first, this can take a minute)" if token else "Starting Hermes")
            await drive_hermes(job)
            st = job.get("hermes_status")
            if job.get("cancel") or st == "cancelled":
                job.update(state="cancelled", stage="Cancelled")
                ev(job, "warn", "Cancelled")
            elif st == "completed":
                job.update(state="complete", stage="Complete")
                ev(job, "ok", f"Done: {len(active_cands(job))} video(s) proposed in {time.time() - job['created']:.0f}s, {job['steps']} helper call(s)")
            else:
                job.update(state="failed", stage="Failed", error=str(job.get("error") or st)[:400])
                ev(job, "error", f"Hermes run ended: {job['error']}")
        except asyncio.CancelledError:
            job.update(state="cancelled", stage="Cancelled")
        except Exception as exc:
            job.update(state="failed", stage="Failed", error=str(exc)[:400])
            ev(job, "error", f"job failed: {str(exc)[:300]}")
        finally:
            stop.set()
            hb.cancel()
            if token and job.get("unload_after"):
                with suppress(Exception):
                    async with httpx.AsyncClient(timeout=30) as c:
                        await c.get(LLAMA_URL + "/unload", headers={"Authorization": f"Bearer {LLAMA_KEY}"} if LLAMA_KEY else {})
                    ev(job, "tool", "llama.unload  (VRAM freed)", icon="⏏")
            if token:
                with suppress(Exception):
                    await queue_request("/release", {"token": token}, timeout=6)

    def proceed(job_id: str, token: str) -> bool:
        job = jobs.get(job_id)
        if not job or job["state"] != "waiting_for_gpu":
            return False
        tasks[job_id] = asyncio.create_task(run(job_id, token))
        return True

    def proceed_now(job_id: str) -> None:
        jobs[job_id]["state"] = "preparing"
        tasks[job_id] = asyncio.create_task(run(job_id, None))

    # ---- UI endpoints ----
    @router.post("/api/datasets/{name}/find", status_code=202)
    async def start(name: str, body: FindRequest) -> dict[str, Any]:
        datasets_router.ds_path(name)   # 404/422 for unknown or bad dataset names
        sites = []
        for s in body.sites:
            k = site_key(s)
            if k and k not in sites:
                if k not in KNOWN_SITES:
                    check_url(k)
                sites.append(k)
        if not sites:
            raise HTTPException(422, "Pick at least one site.")
        if body.source == "trainer" and not HERMES_KEY:
            raise HTTPException(503, "HERMES_API_KEY is not set for the Trainer service.")
        fetch_url = check_url(body.fetch_url) if body.fetch_url.strip() else ""
        if fetch_url:
            # "Get this one video": one proposal, downloaded straight away, few steps.
            body = body.model_copy(update={"query": f"the video at {fetch_url}", "sites": [urllib.parse.urlsplit(fetch_url).hostname or fetch_url],
                                           "count": 1, "auto_download": True, "max_steps": min(body.max_steps, 20)})
        queued_fetch = bool(fetch_url and body.source == "trainer")
        if queued_fetch:
            prior = next((jobs[job_id] for job_id in fetch_order if jobs[job_id]["dataset"] == name and
                          vkey(jobs[job_id]["fetch_url"]) == vkey(fetch_url)), None)
            if prior:
                return public(prior)
            if len(fetch_order) >= 50:
                raise HTTPException(429, "The Hermes URL queue is full (50 links).")
        busy = next((j for j in jobs.values() if j["dataset"] == name and j["state"] in ACTIVE), None)
        if busy and not queued_fetch:
            raise HTTPException(409, f"Hermes is busy with this dataset already ({'fetching ' + busy['fetch_url'] if busy.get('fetch_url') else 'a clip search'}); "
                                     "stop it or wait for it first.")
        if not fetch_url and any(jobs[job_id]["dataset"] == name for job_id in fetch_order):
            raise HTTPException(409, "Hermes URL links are queued for this dataset; wait for them or cancel them first.")
        model, provider = body.model.strip(), body.provider.strip()
        local = True
        if body.source == "trainer":
            inv = None
            with suppress(httpx.HTTPError, ValueError, KeyError):
                inv = await hermes_models()
            if inv:
                if model and not provider:
                    provider = next((pr["slug"] for pr in inv["providers"] if pr["local"] and model in pr["models"]),
                                    next((pr["slug"] for pr in inv["providers"] if model in pr["models"]), ""))
                if model and provider and not any(pr["slug"] == provider and model in pr["models"] for pr in inv["providers"]):
                    raise HTTPException(422, f"Hermes has no model {model} under {provider}.")
                local = is_local(provider or inv["default"].get("provider") or "custom")
        await asyncio.to_thread(prune_shots)
        job_id = "find" + os.urandom(14).hex()[:28]
        jobs[job_id] = job = {"id": job_id, "dataset": name, "query": body.query.strip(), "concept": body.concept.strip(), "sites": sites,
                              "count": body.count, "min_seconds": body.min_seconds, "max_seconds": body.max_seconds,
                              "auto_download": body.auto_download, "max_steps": body.max_steps, "unload_after": body.unload_after,
                              "source": body.source, "state": "queued" if queued_fetch else "waiting_for_gpu",
                              "stage": "Queued for Hermes" if queued_fetch else "Waiting for the GPU queue", "error": None,
                              "created": time.time(), "log": [], "log_offset": 0, "run_id": None, "status_line": "", "steps": 0,
                              "searches": [], "candidates": [], "seen": {}, "summary": "",
                              "model": model, "provider": provider, "reasoning": body.reasoning, "local": local, "fetch_url": fetch_url,
                              "base_max_steps": body.max_steps}
        if fetch_url:
            ev(job, "user", f"download {fetch_url} (whole video, then trim)")
        else:
            ev(job, "user", f"find {body.count} source video(s) of: {job['query']}" + (f" · teaches: {job['concept']}" if job["concept"] else "")
               + f" · sites: {', '.join(sites)}" + (" · auto-download" if body.auto_download else ""))
        if body.source == "hermes":
            job.update(state="running", stage="Hermes (chat session) is searching")
            ev(job, "system", "started by a Hermes session; it does the searching itself")
            return public(job)
        if queued_fetch:
            fetch_order.append(job_id)
            ev(job, "system", f"URL queued for Hermes · position {len(fetch_order)}")
            ensure_fetch_pump()
            return public(job)
        await launch(job)
        return public(job)

    async def launch(job: dict[str, Any]) -> None:
        """Start (or restart) the Hermes run: straight away for cloud models, else via the GPU queue."""
        if not job.get("local", True):
            ev(job, "system", f"{job['model']} is a cloud model: no GPU needed, skipping the GPU queue")
            job.update(stage="Starting Hermes")
            proceed_now(job["id"])
            return
        job.update(state="waiting_for_gpu", stage="Waiting for the GPU queue")
        ev(job, "system", "waiting for the shared GPU queue (Hermes' model runs on the GPU; renders and training go first)")
        try:
            r = await queue_request("/enqueue", {"app": "Trainer", "job_id": job["id"], "label": f"Find clips: {job['query'][:40]}"})
            r.raise_for_status()
            job["queue_entry_id"] = r.json().get("entry_id")
        except Exception as exc:
            job.update(state="failed", stage="Queue error", error=str(exc)[:300])
            raise HTTPException(503, "Could not reach the shared GPU queue.") from exc

    async def steer(job: dict[str, Any], text: str) -> bool:
        """Tell the running Hermes something mid-run (it reads it before its next step)."""
        if not job.get("run_id"):
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{HERMES_URL}/v1/runs/{job['run_id']}/steer", headers=hermes_headers(), json={"input": text})
            return r.status_code < 300
        except httpx.HTTPError:
            return False

    def schedule_resume(job: dict[str, Any]) -> None:
        """After rejections on a finished search: start a continuation run. Waits a few seconds so
        several rejections in a row become one run."""
        job["resume_at"] = time.time() + 8
        if job.get("resume_task") and not job["resume_task"].done():
            return

        async def later():
            while time.time() < job["resume_at"]:
                await asyncio.sleep(1)
            if job["state"] in ACTIVE or not any(c.get("rejected") for c in job["candidates"]):
                return
            need = max(1, job["count"] - len(active_cands(job)))
            job.update(resume=True, steps=0, max_steps=max(15, min(job.get("base_max_steps", job["max_steps"]), 10 + 6 * need)),
                       error=None, summary="", cancel=False, run_id=None)
            job.pop("hermes_status", None)
            ev(job, "user", f"rejected {sum(1 for c in job['candidates'] if c.get('rejected'))} proposal(s): find {need} more")
            with suppress(HTTPException):
                await launch(job)
        job["resume_task"] = asyncio.create_task(later())

    @router.post("/api/finds/{job_id}/candidates/{index}/reject")
    async def reject(job_id: str, index: int, body: RejectBody) -> dict[str, Any]:
        job = get_job(job_id)
        cand = next((c for c in job["candidates"] if c["i"] == index), None)
        if not cand:
            raise HTTPException(404, "Unknown candidate.")
        cand["rejected"] = body.reason.strip() or True
        set_rejected(job["dataset"], cand, body.reason.strip())
        need = max(0, job["count"] - len(active_cands(job)))
        ev(job, "warn", f"user rejected #{cand['i'] + 1} {cand['title']}" + (f" — {body.reason.strip()}" if body.reason.strip() else ""), candidate=cand["i"])
        if job.get("fetch_url") or job["source"] != "trainer":
            return public(job)
        if job["state"] in ACTIVE:
            await steer(job, f"The user REJECTED your proposal #{cand['i'] + 1} \"{cand['title']}\" ({cand['url']})"
                             + (f" because: {body.reason.strip()}" if body.reason.strip() else "")
                             + f". Never propose it again. You now need {need} more good video(s): keep searching and propose them.")
        else:
            schedule_resume(job)
        return public(job)

    @router.post("/api/finds/{job_id}/candidates/{index}/unreject")
    async def unreject(job_id: str, index: int) -> dict[str, Any]:
        job = get_job(job_id)
        cand = next((c for c in job["candidates"] if c["i"] == index), None)
        if not cand:
            raise HTTPException(404, "Unknown candidate.")
        cand.pop("rejected", None)
        set_rejected(job["dataset"], cand, None)
        ev(job, "ok", f"user restored #{cand['i'] + 1} {cand['title']}", candidate=cand["i"])
        return public(job)

    @router.get("/api/finds/models")
    async def models() -> dict[str, Any]:
        try:
            return await hermes_models()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, f"Hermes API not reachable: {exc}") from exc

    @router.get("/api/finds")
    async def list_finds(dataset: str = "") -> dict[str, Any]:
        return {"jobs": [public(j) for j in jobs.values() if not dataset or j["dataset"] == dataset][-10:],
                "sites": KNOWN_SITES, "recipes": recipes()}

    @router.get("/api/datasets/{name}/fetch-queue")
    async def fetch_queue_status(name: str) -> dict[str, Any]:
        datasets_router.ds_path(name)
        items = []
        for position, job_id in enumerate(fetch_order, 1):
            job = jobs[job_id]
            if job["dataset"] != name:
                continue
            download = active_download(job)
            items.append({"id": job_id, "url": job["fetch_url"], "state": "downloading" if download else job["state"],
                          "stage": (f"Downloading {download.get('percent', 0):.0f}%" if download else job["stage"]),
                          "position": position, "percent": download.get("percent", 0) if download else 0})
        head = jobs[fetch_order[0]] if fetch_order else None
        return {"items": items, "total": len(fetch_order),
                "active_dataset": head["dataset"] if head and head["state"] != "queued" else None}

    @router.get("/api/finds/{job_id}")
    async def get_find(job_id: str) -> dict[str, Any]:
        return public(get_job(job_id))

    def hermes_lines(job: dict[str, Any]) -> list[dict[str, Any]]:
        """This run's lines from Hermes' own agent.log (tagged [run_…]) plus its warnings/errors."""
        rids = job.get("run_ids") or ([job["run_id"]] if job.get("run_id") else [])
        if not rids or not HERMES_LOG.is_file():
            return []
        try:
            with HERMES_LOG.open("rb") as fh:
                fh.seek(max(0, HERMES_LOG.stat().st_size - 400_000))
                lines = fh.read().decode("utf-8", errors="replace").splitlines()[1:]
        except OSError:
            return []
        out = []
        for line in lines:
            if not any(f"[{rid}]" in line for rid in rids):
                continue
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d+) (\w+) \[[^\]]+\] (\S+?): (.*)", line)
            if not m:
                continue
            ts = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) + int(m.group(2)) / 1000   # Hermes logs in UTC
            out.append({"t": ts, "kind": {"ERROR": "error", "WARNING": "warn"}.get(m.group(3), "hermes"),
                        "text": f"{m.group(4)}: {m.group(5)[:500]}", "hermes": True})
        return out[-300:]

    @router.get("/api/finds/{job_id}/log")
    async def find_log(job_id: str, after: int = 0) -> dict[str, Any]:
        job = get_job(job_id)
        off = job.get("log_offset", 0)
        return {"job": public(job), "lines": job["log"][max(0, after - off):], "next": off + len(job["log"]),
                "hermes": await asyncio.to_thread(hermes_lines, job)}

    @router.post("/api/finds/{job_id}/cancel")
    async def cancel(job_id: str) -> dict[str, Any]:
        job = get_job(job_id)
        if job["state"] == "queued" and job["id"] in fetch_order:
            fetch_order.remove(job["id"])
            job.update(state="cancelled", stage="Removed from URL queue")
            ev(job, "warn", "removed from URL queue")
        elif job["state"] == "waiting_for_gpu" and job["source"] == "trainer":
            with suppress(Exception):
                await queue_request(f"/dequeue/{job.get('queue_entry_id')}")
            job.update(state="cancelled", stage="Cancelled")
        elif job["state"] in ACTIVE:
            job["cancel"] = True
            job["stage"] = "Stopping Hermes"
            if job.get("run_id"):
                with suppress(Exception):
                    async with httpx.AsyncClient(timeout=10) as c:
                        await c.post(f"{HERMES_URL}/v1/runs/{job['run_id']}/stop", headers=hermes_headers())
            else:
                job.update(state="cancelled", stage="Cancelled")
            ev(job, "warn", "cancel requested by the user")
        return public(job)

    def fetch_candidate(job: dict[str, Any], cand: dict[str, Any], section: tuple[float, float] | None, force: bool = False) -> str:
        """Start the download of a proposal: the page itself, or the stream the browser caught
        when yt-dlp can't read the page (sent with the page as Referer and the job's cookies)."""
        cookies = browser_dir / f"{job['id']}.cookies.txt"
        cookies = cookies if cookies.is_file() else None
        src = "link fetch" if job.get("fetch_url") else f"clip finder: {job['query'][:60]}"
        if cand.get("stream"):
            return datasets_router.start_download(job["dataset"], cand["stream"], section, cookies, headers={"Referer": cand["url"]},
                                                  title=cand["title"], page_url=cand["url"], force=force, source=src)["id"]
        return datasets_router.start_download(job["dataset"], cand["url"], section, cookies, page_url=cand["url"], force=force, source=src)["id"]

    @router.post("/api/finds/{job_id}/candidates/{index}/download")
    async def download(job_id: str, index: int, body: DownloadBody) -> dict[str, Any]:
        job = get_job(job_id)
        cand = next((c for c in job["candidates"] if c["i"] == index), None)
        if not cand:
            raise HTTPException(404, "Unknown candidate.")
        d = datasets_router.download_status(cand["download"]) if cand.get("download") else None
        if d and d["state"] not in ("failed",) and not (body.force and d["state"] == "duplicate"):
            return public(job)
        section = (cand["start"], cand["end"]) if body.section and cand.get("start") is not None and cand.get("end") else None
        cand["download"] = fetch_candidate(job, cand, section, force=body.force)
        ev(job, "tool", f"download  {cand['title'] or cand['url']}" + (f"  [{fmt_dur(section[0])}–{fmt_dur(section[1])}]" if section else ""), icon="⬇")
        return public(job)

    @router.get("/api/finds/{job_id}/shots/{n}")
    async def shot_image(job_id: str, n: int) -> Response:
        if not re.fullmatch(r"find[0-9a-f]{28}", job_id):
            raise HTTPException(404, "No such screenshot.")
        f = shots_root / job_id / f"{n}.jpg"
        if not f.is_file():
            raise HTTPException(404, "No such screenshot.")
        return Response(f.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "max-age=604800, immutable"})

    @router.get("/api/finds-thumb")
    async def thumb(u: str) -> Response:
        """Thumbnails from video sites, served from here (the page's CSP only allows its own origin)."""
        url = check_url(u)
        thumb_dir.mkdir(parents=True, exist_ok=True)
        f = thumb_dir / hashlib.sha1(url.encode()).hexdigest()
        if not f.is_file():
            try:
                async with web_client(15) as c:
                    r = await c.get(url)
            except httpx.HTTPError as exc:
                raise HTTPException(502, "Thumbnail not reachable.") from exc
            ctype = r.headers.get("content-type", "")
            if r.status_code >= 400 or not ctype.startswith("image/") or len(r.content) > 4_000_000:
                raise HTTPException(404, "No thumbnail.")
            f.write_bytes(r.content)
            f.with_suffix(".type").write_text(ctype)
        ctype = f.with_suffix(".type").read_text() if f.with_suffix(".type").is_file() else "image/jpeg"
        return Response(f.read_bytes(), media_type=ctype, headers={"Cache-Control": "max-age=86400"})

    # ---- tool endpoints (Hermes' clip-finder script) ----
    @router.post("/api/finds/{job_id}/search")
    async def search(job_id: str, body: SearchBody) -> dict[str, Any]:
        job = get_job(job_id, running=True)
        site = site_key(body.site)
        t0 = time.time()
        try:
            out = await search_site(site, body.query.strip(), body.n)
        except HTTPException as exc:
            ev(job, "warn", f"search {site} “{body.query}” failed: {exc.detail}")
            raise
        except httpx.HTTPError as exc:
            ev(job, "warn", f"search {site} “{body.query}” failed: {exc}")
            raise HTTPException(502, f"{site}: {exc}") from exc
        lo, hi = job["min_seconds"], job["max_seconds"]
        for r in out["results"]:
            r["fits"] = not ((lo and r.get("duration") and r["duration"] < lo) or (hi and r.get("duration") and r["duration"] > hi))
            r["proposed"] = any(vkey(c["url"]) == vkey(r["url"]) for c in job["candidates"] if not c.get("rejected"))
            r.update(known_flags(job, r["url"]))
            remember(job, r)
        job["searches"].append({"site": site, "query": body.query, "results": len(out["results"]), "how": out["how"]})
        ev(job, "tool", f"search {site}  “{body.query}” → {len(out['results'])} result(s) via {out['how']}", icon="🔎", ms=int((time.time() - t0) * 1000))
        if out.get("_page"):
            snap(job, out.pop("_page"))
        return {"site": site, **out, "budget": budget(job)}

    def page_result(job: dict[str, Any], p: dict[str, Any], find: str, t0: float, what: str = "page") -> dict[str, Any]:
        links = p["links"]
        if find:
            w = find.lower()
            links = [ln for ln in links if w in (ln["label"] + " " + ln["url"]).lower()]
        for ln in links:
            if ln["video"]:
                remember(job, {"url": ln["url"], "title": ln["label"], "thumb": ln.get("thumb"), "site": p["host"]})
        known = recipes().get(p["host"].removeprefix("www.") if p["host"].startswith("www.") and p["host"].count(".") > 1 else p["host"])
        ev(job, "tool", f"{what}  {p['url']}{' (rendered)' if p['rendered'] else ''} → {len(p['links'])} links, {sum(ln['video'] for ln in p['links'])} video-like, {len(p['forms'])} form(s)",
           icon="🌐", ms=int((time.time() - t0) * 1000))
        snap(job, p)
        return {"url": p["url"], "title": p["title"], "meta": p["meta"], "rendered": p["rendered"], "thin": p["thin"],
                "text": p["text"][:2500], "links": [{"i": i, "text": ln["label"][:120], "url": ln["url"], "video": ln["video"]} for i, ln in enumerate(links[:150])],
                "forms": [{"i": i, **f, "search": any(i == j for j, _, _ in search_forms(p))} for i, f in enumerate(p["forms"][:12])],
                "search_template": known["template"] if known else None, "buttons": p.get("buttons", []), "gate": p.get("gate", False),
                "budget": budget(job)}

    @router.post("/api/finds/{job_id}/page")
    async def page(job_id: str, body: PageBody) -> dict[str, Any]:
        job = get_job(job_id, running=True)
        t0 = time.time()
        try:
            p = await fetch_page(body.url, render=body.render)
            if p["thin"] and not p["rendered"]:
                # Nearly empty without JavaScript: look again in the browser before Hermes has to ask.
                with suppress(HTTPException):
                    p = await fetch_page(body.url, render=True)
        except httpx.HTTPError as exc:
            ev(job, "warn", f"page {body.url} failed: {exc}")
            raise HTTPException(502, str(exc)[:300]) from exc
        return page_result(job, p, body.find, t0)

    @router.post("/api/finds/{job_id}/click")
    async def click(job_id: str, body: ClickBody) -> dict[str, Any]:
        """Click a button / link by its text in the browser (age gates, cookie walls, "load more",
        tabs). The session keeps the cookies, so the gate stays accepted for later calls."""
        job = get_job(job_id, running=True)
        t0 = time.time()
        try:
            final, html, shot = await render_page(check_url(body.url), click=body.text)
        except httpx.HTTPError as exc:
            ev(job, "warn", f"click on {body.url} failed: {exc}")
            raise HTTPException(502, str(exc)[:300]) from exc
        return page_result(job, {**parse_page(final, html, True), "shot": shot}, "", t0, f"click “{body.text}” on {body.url} →")

    @router.post("/api/finds/{job_id}/submit")
    async def submit(job_id: str, body: SubmitBody) -> dict[str, Any]:
        job = get_job(job_id, running=True)
        t0 = time.time()
        try:
            src = await fetch_page(body.url, render=body.render)
            if body.form >= len(src["forms"]):
                raise HTTPException(422, f"That page has {len(src['forms'])} form(s).")
            url, data = form_url(src["forms"][body.form], body.fields)
            if body.render:
                # Type into the real form in the browser: JavaScript search boxes route themselves.
                final, html, shot = await render_page(src["url"], fill=(body.form, body.fields))
                p, url, data = {**parse_page(final, html, True), "shot": shot}, final, {}
            else:
                p = await fetch_page(url, data=data or None)
                if data:
                    p["no_bg_shot"] = True     # a POST result can't be re-opened by URL
        except httpx.HTTPError as exc:
            ev(job, "warn", f"submit on {body.url} failed: {exc}")
            raise HTTPException(502, str(exc)[:300]) from exc
        out = page_result(job, p, "", t0, f"submit form #{body.form} {json.dumps(body.fields, ensure_ascii=False)[:120]} →")
        if data:
            out["note"] = "POST form: its result URL can't be saved as a search recipe."
        else:
            out["result_url"] = url
        return out

    @router.post("/api/finds/{job_id}/learn")
    async def learn(job_id: str, body: LearnBody) -> dict[str, Any]:
        job = get_job(job_id, running=True)
        if "{q}" not in body.template:
            raise HTTPException(422, "The template needs {q} where the search words go.")
        check_url(body.template.replace("{q}", "test"))
        domain = site_key(body.domain)
        save_recipe(domain, body.template, "learned by Hermes", body.note)
        ev(job, "ok", f"learned how to search {domain}: {body.template}")
        return {"saved": domain, "template": body.template}

    async def inspect_url(url: str) -> dict[str, Any]:
        state = SESSION.get()
        cookies = ["--cookies", str(state.with_suffix(".cookies.txt"))] if state and state.with_suffix(".cookies.txt").is_file() else []
        try:
            d = await yt_dlp_json([*cookies, "--no-playlist", "--flat-playlist", "--skip-download", url], timeout=75)
        except HTTPException as exc:
            # yt-dlp doesn't know the site (or its extractor is broken): watch the player in the browser.
            sniffed = await sniff_video(url)
            if not sniffed:
                raise HTTPException(502, f"{exc.detail} — and the browser found no video stream on that page either.") from exc
            probe: dict[str, Any] = {}
            with suppress(HTTPException):
                probe = await yt_dlp_json([*cookies, "--add-header", f"Referer:{url}", "--skip-download", sniffed["stream"]], timeout=60)
            heights = [f.get("height") for f in probe.get("formats") or [] if f.get("height")]
            return {"kind": "video", "url": url, "title": sniffed.get("title"), "duration": sniffed.get("duration") or probe.get("duration"),
                    "uploader": None, "upload_date": None, "views": None, "height": max(heights) if heights else sniffed.get("height"),
                    "fps": probe.get("fps"), "extractor": "browser", "thumb": sniffed.get("thumb"), "tags": [], "categories": None,
                    "description": (sniffed.get("description") or "")[:1200], "chapters": [],
                    "stream": sniffed["stream"], "note": f"yt-dlp can't read this page ({str(exc.detail)[:120]}); the stream was found by playing it in the browser and downloads use it"}
        if d.get("_type") in ("playlist", "multi_video"):
            ents = [e for e in d.get("entries") or [] if e][:20]
            return {"kind": "playlist", "title": d.get("title"), "url": d.get("webpage_url") or url, "extractor": d.get("extractor_key"),
                    "entries": [{"url": e.get("url") or e.get("webpage_url"), "title": e.get("title"), "duration": e.get("duration")} for e in ents]}
        heights = [f.get("height") for f in d.get("formats") or [] if f.get("height")]
        return {"kind": "video", "url": d.get("webpage_url") or url, "title": d.get("title"), "duration": d.get("duration"),
                "uploader": d.get("uploader") or d.get("channel"), "upload_date": d.get("upload_date"), "views": d.get("view_count"),
                "height": max(heights) if heights else d.get("height"), "fps": d.get("fps"), "extractor": d.get("extractor_key"),
                "thumb": thumb_of(d), "tags": (d.get("tags") or [])[:15], "categories": d.get("categories"),
                "description": (d.get("description") or "")[:1200],
                "chapters": [{"title": c.get("title"), "start": c.get("start_time"), "end": c.get("end_time")} for c in (d.get("chapters") or [])[:40]]}

    @router.post("/api/finds/{job_id}/inspect")
    async def inspect(job_id: str, body: UrlBody) -> dict[str, Any]:
        job = get_job(job_id, running=True)
        url = check_url(body.url)
        t0 = time.time()
        try:
            info = await inspect_url(url)
        except HTTPException as exc:
            ev(job, "warn", f"inspect {url}: {exc.detail}")
            raise
        if info.get("stream"):
            job.setdefault("streams", {})[url] = info["stream"]
        if info["kind"] == "video":
            remember(job, {"url": url, "title": info["title"], "duration": info["duration"], "uploader": info["uploader"],
                           "thumb": info["thumb"], "height": info["height"], "views": info["views"],
                           "site": urllib.parse.urlsplit(url).hostname.removeprefix("www.") if (info["extractor"] or "Generic") == "Generic" else info["extractor"].lower()})
            if info["url"] != url:
                job["seen"][info["url"]] = job["seen"][url]
            ev(job, "tool", f"inspect  {info['title']} · {fmt_dur(info['duration'])} · {info['height'] or '?'}p · {len(info['chapters'])} chapter(s)"
               + (" · stream found in the browser" if info.get("stream") else ""), icon="🔍", ms=int((time.time() - t0) * 1000))
        else:
            ev(job, "tool", f"inspect  {info['title'] or url} → page with {len(info['entries'])} video(s)", icon="🔍", ms=int((time.time() - t0) * 1000))
        return {**info, **known_flags(job, url), "budget": budget(job)}

    @router.post("/api/finds/{job_id}/propose")
    async def propose(job_id: str, body: ProposeBody) -> dict[str, Any]:
        job = get_job(job_id, running=True)
        url = check_url(body.url)
        same = next((c for c in job["candidates"] if vkey(c["url"]) == vkey(url)), None)
        if same:
            return {"ok": False, "message": "the user rejected it: find a different video" if same.get("rejected") else "already proposed",
                    "budget": budget(job)}
        flags = known_flags(job, url)
        if flags.get("rejected"):
            ev(job, "warn", f"not proposed (rejected earlier): {body.title or url}")
            return {"ok": False, "message": f"the user rejected this video before ({flags['rejected']}): find a different one", "budget": budget(job)}
        if flags.get("downloaded"):
            ev(job, "warn", f"not proposed (already downloaded → {flags['downloaded']}): {body.title or url}")
            return {"ok": False, "message": f"already downloaded earlier to {flags['downloaded']}: find a different video", "budget": budget(job)}
        if len(active_cands(job)) >= job["count"] + 4:
            raise HTTPException(409, f"Already {len(active_cands(job))} proposals (target {job['count']}). Finish up.")
        meta = job["seen"].get(url)
        if not meta or not meta.get("duration"):
            with suppress(Exception):
                info = await inspect_url(url)
                if info.get("stream"):
                    job.setdefault("streams", {})[url] = info["stream"]
                if info["kind"] == "video":
                    meta = {**(meta or {}), "title": info["title"], "duration": info["duration"], "uploader": info["uploader"], "thumb": info["thumb"], "height": info["height"]}
        meta = meta or {}
        start, end = to_seconds(body.start), to_seconds(body.end)
        if start is not None and end is not None and end <= start:
            start = end = None
        cand = {"i": len(job["candidates"]), "url": url, "title": body.title or meta.get("title") or url, "reason": body.reason.strip(),
                "score": round(body.score, 1), "duration": meta.get("duration"), "uploader": meta.get("uploader"), "thumb": meta.get("thumb"),
                "height": meta.get("height"), "site": meta.get("site") or urllib.parse.urlsplit(url).hostname, "start": start, "end": end,
                "t": time.time(), "download": None, "stream": job.get("streams", {}).get(url)}
        job["candidates"].append(cand)
        ev(job, "ok", f"proposed #{cand['i'] + 1} ({cand['score']:g}/10) {cand['title']}" + (f" [{fmt_dur(start)}–{fmt_dur(end)}]" if start is not None and end else "")
           + (f" — {cand['reason']}" if cand["reason"] else ""), candidate=cand["i"])
        if job["auto_download"]:
            cand["download"] = fetch_candidate(job, cand, None)        # whole video; trimmed later
            ev(job, "tool", f"download  {cand['title']}", icon="⬇")
        return {"ok": True, "message": "proposed", "budget": budget(job)}

    @router.post("/api/finds/{job_id}/say")
    async def say(job_id: str, body: SayBody) -> dict[str, Any]:
        job = get_job(job_id, running=True)
        job["steps"] -= 1                 # talking to the user is free
        ev(job, "say", body.text.strip())
        return {"ok": True}

    @router.get("/api/finds/{job_id}/status")
    async def status(job_id: str) -> dict[str, Any]:
        job = get_job(job_id)
        return {"state": job["state"], "query": job["query"], "target": job["count"], "steps": job["steps"], "max_steps": job["max_steps"],
                "searches": job["searches"], "budget": budget(job),
                "proposed": [{k: c[k] for k in ("i", "url", "title", "score", "start", "end")} for c in active_cands(job)],
                "rejected": [{"url": c["url"], "title": c["title"], "reason": c.get("rejected")} for c in job["candidates"] if c.get("rejected")]}

    @router.post("/api/finds/{job_id}/finish")
    async def finish(job_id: str, body: SayBody) -> dict[str, Any]:
        """A Hermes chat session that started the job itself (source=hermes) ends it here."""
        job = get_job(job_id)
        if job["source"] != "hermes" or job["state"] not in ACTIVE:
            raise HTTPException(409, "Only a running job started by a Hermes session is finished this way.")
        job.update(state="complete", stage="Complete", summary=body.text.strip())
        ev(job, "assistant", body.text.strip())
        ev(job, "ok", f"Done: {len(job['candidates'])} video(s) proposed, {job['steps']} helper call(s)")
        return public(job)

    def active_jobs() -> list[dict[str, Any]]:
        return [{"dataset": j["dataset"], "kind": "find", "id": j["id"]} for j in jobs.values() if j["state"] in ACTIVE]
    router.active_jobs = active_jobs
    return router, proceed
