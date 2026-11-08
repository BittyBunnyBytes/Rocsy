"""Update checks for plugins (Core Backend -> Plugins). plugin.json says where to look:

    "version": "1.0.0",                       the version installed here (optional), or
    "updates": {
      "github": "owner/repo",                 GitHub releases (latest release; tags if it has none)
      "url": "https://… or /Name/api/latest", any JSON feed: {"version"|"tag_name", "url"|"html_url", "notes"|"body"}
      "current_url": "/Name/api/version",     the running app reports its own version (beats "version")
      "prereleases": false                    GitHub only: count pre-releases too
    }

Results are cached for a few hours per plugin; "Check now" forces a fresh look."""
import asyncio
import json
import re
import time
from typing import Any

import httpx

GITHUB_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}$")
_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*$")
CACHE_SECONDS = 6 * 3600
FORCE_GAP = 20          # a forced check reuses a result younger than this
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_locks: dict[str, asyncio.Lock] = {}


def clean(updates: object) -> dict[str, Any] | None:
    """The plugin.json "updates" object, checked. Raises ValueError for a bad one."""
    if updates in (None, False, {}):
        return None
    if not isinstance(updates, dict):
        raise ValueError('updates must be an object like {"github": "owner/repo"}')
    github, url = str(updates.get("github") or "").strip(), str(updates.get("url") or "").strip()
    current_url = str(updates.get("current_url") or "").strip()
    if github and url:
        raise ValueError("updates: use either github or url, not both")
    if github and not GITHUB_REPO.match(github.removeprefix("https://github.com/").strip("/")):
        raise ValueError('updates.github must look like "owner/repo"')
    if url and not (url.startswith("https://") and " " not in url and len(url) < 500
                    or _PATH.match(url) and "//" not in url and ".." not in url):
        raise ValueError("updates.url must be an https:// address or a path on the plugin (/Name/api/latest)")
    if current_url and not (_PATH.match(current_url) and "//" not in current_url and ".." not in current_url):
        raise ValueError("updates.current_url must be a path on the plugin, like /Name/api/version")
    if not github and not url and not current_url:
        return None
    return {"github": github.removeprefix("https://github.com/").strip("/"), "url": url,
            "current_url": current_url, "prereleases": bool(updates.get("prereleases"))}


def _numbers(version: str) -> tuple[int, ...] | None:
    match = re.search(r"\d+(?:\.\d+)*", version)
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def newer(latest: str, current: str) -> bool | None:
    """True if latest is newer than current; None when the two can't be compared."""
    if not latest or not current:
        return None
    a, b = _numbers(latest), _numbers(current)
    if a is None or b is None:  # not numbered (e.g. a commit or a date name): different means new
        return latest.strip().lstrip("vV") != current.strip().lstrip("vV")
    width = max(len(a), len(b))
    a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
    if a != b:
        return a > b
    # Same numbers: a release beats its own pre-release (1.2.0 > 1.2.0-rc1).
    return bool(re.search(r"[-+]?(rc|beta|alpha|pre|dev)", current, re.I)) and not re.search(r"(rc|beta|alpha|pre|dev)", latest, re.I)


def _pick(data: Any, *keys: str) -> str:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
    return ""


async def _json(client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None) -> Any:
    response = await client.get(url, headers=headers or {}, follow_redirects=True)
    if response.status_code == 404:
        raise LookupError("not found (404)")
    if response.status_code == 403 and "rate limit" in response.text.lower():
        raise LookupError("GitHub's rate limit was reached; try again in an hour")
    response.raise_for_status()
    if len(response.content) > 2_000_000:
        raise LookupError("the feed is too large")
    return json.loads(response.content)


async def _latest(client: httpx.AsyncClient, updates: dict[str, Any], upstream: str) -> dict[str, str]:
    if updates["github"]:
        repo = updates["github"]
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "Rocsy-plugin-updates"}
        base = f"https://api.github.com/repos/{repo}"
        try:
            if updates["prereleases"]:
                releases = await _json(client, f"{base}/releases?per_page=10", headers)
                release = next((item for item in releases or [] if not item.get("draft")), None)
                if release is None:
                    raise LookupError("no releases")
            else:
                release = await _json(client, f"{base}/releases/latest", headers)
            return {"version": _pick(release, "tag_name", "name"), "url": _pick(release, "html_url"),
                    "notes": _pick(release, "body")[:800], "published": _pick(release, "published_at")[:10],
                    "source": f"github.com/{repo}"}
        except LookupError:
            # No releases: fall back to the newest tag.
            try:
                tags = await _json(client, f"{base}/tags?per_page=1", headers)
            except LookupError as exc:
                if "rate limit" in str(exc):
                    raise
                raise LookupError(f"github.com/{repo} wasn't found (is it public, and spelled right?)") from None
            if not tags:
                raise LookupError(f"{repo} has no releases or tags") from None
            tag = _pick(tags[0], "name")
            return {"version": tag, "url": f"https://github.com/{repo}/releases/tag/{tag}", "notes": "",
                    "published": "", "source": f"github.com/{repo} (tags)"}
    url = updates["url"]
    feed = await _json(client, url if url.startswith("https://") else f"{upstream}{url}")
    if isinstance(feed, list) and feed:
        feed = feed[0]  # a list of releases, newest first
    link = _pick(feed, "url", "html_url", "download_url")
    return {"version": _pick(feed, "version", "tag_name", "latest", "name"),
            "url": link if link.startswith(("https://", "http://", "/")) else "",
            "notes": _pick(feed, "notes", "body", "changelog")[:800], "published": _pick(feed, "published", "published_at", "date")[:10],
            "source": url if url.startswith("https://") else "the plugin"}


async def check(key: str, config: dict[str, Any], upstream: str, force: bool = False) -> dict[str, Any]:
    """Compare the newest release with the installed version. Never raises."""
    try:
        updates = clean(config.get("updates"))
    except ValueError as exc:
        return {"configured": True, "error": str(exc)}
    current = str(config.get("version") or "").strip()
    if updates is None:
        return {"configured": False, "current": current}
    signature = json.dumps([updates, current, upstream], sort_keys=True)
    cache_key = f"{key}:{signature}"
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _cache.get(cache_key)
        age = time.time() - cached[0] if cached else None
        if cached and (age < FORCE_GAP or (not force and age < CACHE_SECONDS)):
            return cached[1]
        result: dict[str, Any] = {"configured": True, "current": current, "checked_at": int(time.time())}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if updates["current_url"]:
                    try:
                        reported = _pick(await _json(client, f"{upstream}{updates['current_url']}"), "version", "tag_name")
                        if reported:
                            result["current"] = current = reported
                    except (httpx.HTTPError, LookupError, ValueError):
                        result["current_note"] = "The plugin didn't report its version; using plugin.json's"
                if updates["github"] or updates["url"]:
                    result.update(await _latest(client, updates, upstream))
                    result["latest"] = result.pop("version")
                    result["available"] = newer(result["latest"], current)
        except (httpx.HTTPError, LookupError, ValueError) as exc:
            result["error"] = f"Couldn't check: {exc}"[:300]
        _cache[cache_key] = (time.time(), result)
        return result
