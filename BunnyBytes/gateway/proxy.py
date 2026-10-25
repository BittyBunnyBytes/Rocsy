from __future__ import annotations

from html import escape as html_escape
import logging
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import AsyncIterator, Callable

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse, Response

import datasaver
import reasoning_adapter
import restyle
from config import settings

log = logging.getLogger("gateway.proxy")

# Hop-by-hop headers per RFC 7230 section 6.1 -- must never be forwarded
# verbatim in either direction of a proxy.
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

# Normal requests: reasonably tight connect timeout, moderate read timeout.
# SSE streams need a long-lived (effectively unbounded) read timeout since the
# upstream may go quiet between events for a long time without the connection
# being dead.
#
# 2026-09-21: bumped read 30.0 -> 180.0. A non-streaming /Llama/v1/chat/completions
# call (no SSE, so this is the timeout that applies) with a reasoning model and a
# few hundred+ output tokens routinely runs past 30s -- the model itself was fine,
# this proxy was cutting it off mid-generation and returning a 504 before any
# response body ever arrived. 180s covers a slow reasoning + long completion
# without making a truly hung upstream take unreasonably long to be reported.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=5.0)
SSE_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=None)
MODEL_LOAD_TIMEOUT = httpx.Timeout(connect=5.0, read=600.0, write=30.0, pool=5.0)

# Heuristic for routes that are SSE even before we've seen the upstream's
# response headers, so we can pick the long-read-timeout client up front.
_SSE_PATH_HINTS = ("/events",)

# One pooled client for every proxied request (keep-alive to the upstreams
# instead of a new client + TCP connect per request). Its cookie jar accepts
# nothing: a shared jar would keep one visitor's upstream Set-Cookie and send
# it along with other visitors' requests. No connection cap - SSE streams hold
# theirs open indefinitely, as they did with per-request clients.
_client: httpx.AsyncClient | None = None


def shared_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=32),
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        )
    return _client


async def close_shared_client() -> None:
    if _client is not None:
        await _client.aclose()


def _filter_headers(headers: httpx.Headers | dict, extra_drop: set[str] = frozenset()) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() not in extra_drop
    }


def _set_header_ci(headers: dict[str, str], key: str, value: str) -> None:
    """Sets a header by name, replacing any existing entry that matches
    case-insensitively -- a plain dict is case-sensitive, so without this a
    lowercase key copied from upstream (e.g. "cache-control") and a
    differently-cased key set here (e.g. "Cache-Control") would both survive
    and be emitted as two separate response headers."""
    for existing in list(headers):
        if existing.lower() == key.lower():
            del headers[existing]
    headers[key] = value


def _header_ci(headers: dict[str, str], key: str, default: str = "") -> str:
    for existing, value in headers.items():
        if existing.lower() == key.lower():
            return value
    return default


async def _sized_request_stream(request: Request, max_bytes: int) -> AsyncIterator[bytes]:
    """Re-streams the incoming request body, aborting if it exceeds max_bytes
    even when no (or a lying) Content-Length header was provided -- a
    belt-and-suspenders check alongside security_headers.py's Content-Length
    based rejection, which chunked-transfer uploads could otherwise bypass."""
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise _BodyTooLarge()
        yield chunk


class _BodyTooLarge(Exception):
    pass


def _upstream_broke() -> JSONResponse:
    return JSONResponse({"error": "upstream_unavailable", "message": "The upstream app closed the connection mid-reply."},
                        status_code=502)


async def _read_body(upstream_response: httpx.Response) -> bytes | None:
    """The whole (decoded) upstream body, or None if the upstream dropped the
    connection or stalled partway -- the caller answers 502 instead of a 500."""
    try:
        return await upstream_response.aread()
    except httpx.HTTPError as exc:
        log.warning("Upstream reply for %s broke off: %s", upstream_response.request.url.path, exc)
        return None
    finally:
        await upstream_response.aclose()


class _UpstreamStream(StreamingResponse):
    """Once streaming has started a 502 can't be sent any more: an upstream that
    breaks off mid-body just ends the connection (the browser sees it as cut
    short), logged in one line instead of an unhandled-error traceback."""

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except httpx.HTTPError as exc:
            log.warning("Upstream stream for %s broke off: %s", scope.get("path"), exc)


UNAVAILABLE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{app} is not running</title>
<style>:root{{color-scheme:light dark;--bg:#f6f7f9;--fg:#1d2330;--muted:#5b6475;--card:#fff;--line:#dde1e8;--accent:#3d6ee8}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151b;--fg:#e6e9ef;--muted:#9aa3b2;--card:#1b2029;--line:#2b3240;--accent:#7aa2ff}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}}
main{{max-width:30rem;margin:16px;padding:28px;background:var(--card);border:1px solid var(--line);border-radius:14px}}
h1{{margin:0 0 8px;font-size:1.3rem}}p{{margin:0 0 14px;color:var(--muted)}}a{{color:var(--accent)}}</style></head>
<body><main><h1>{app} is not running</h1>
<p>{detail} If it isn't installed on this machine yet, set it up first. Otherwise start it from Settings &rarr; Services.</p>
<p><a href="/">Back to Rocsy</a> &middot; <a href="/Settings">Settings</a></p></main></body></html>"""


def _upstream_error(request: Request, prefix: str, status: int, code: str, message: str) -> Response:
    """JSON for API callers; a readable page when a person opened the app in the browser."""
    navigation = request.method == "GET" and (request.headers.get("sec-fetch-mode") == "navigate"
                                              or "text/html" in request.headers.get("accept", ""))
    if not navigation:
        return JSONResponse({"error": code, "message": message}, status_code=status)
    app = html_escape(prefix.strip("/") or "This app")
    detail = "It did not answer in time." if status == 504 else "Rocsy could not reach it."
    return Response(UNAVAILABLE_PAGE.format(app=app, detail=detail), status_code=status,
                    media_type="text/html", headers={"Cache-Control": "no-store"})


async def proxy_request(
    request: Request,
    upstream_base: str,
    prefix: str,
    extra_headers: dict[str, str] | None = None,
    strip_prefix: bool = False,
    head_inject: str = "",
    restyle_css: bool = False,
    adapt_reasoning: bool = False,
    html_transform: Callable[[bytes], bytes] | None = None,
    lite_images: bool = False,
) -> Response:
    """Generic streaming reverse-proxy for one upstream app.

    `prefix` (e.g. "/Video") is NOT stripped from the forwarded path by default
    -- downstream apps are built prefix-aware (root_path / APP_PREFIX) and
    expect to see the full prefixed path, per the plan's root_path finding.

    `strip_prefix=True` is for upstreams that have no concept of a mount
    prefix at all (llama-swap: a third-party binary, not one of this repo's
    prefix-aware FastAPI apps) -- the prefix is removed before forwarding so
    e.g. /Llama/v1/models reaches the upstream as /v1/models.

    `extra_headers`, when given, is merged into the forwarded request headers
    (used by lock_gate.py to inject X-Lock-Token on the generate-trigger routes).

    `head_inject`, when given, is inserted before </head> of successful HTML
    page responses (the site theme stylesheet - see themes.py). Those pages
    are small, so they're buffered and sent decoded instead of streamed.

    `restyle_css` routes the app's stylesheets through restyle.rewrite_css
    (hard-coded colours -> theme tokens) and tags its pages' stylesheet links
    with the rewrite version so stale cached copies aren't reused.

    `html_transform` runs on those same buffered pages after the injection
    (topbar.decorate: the shared Rocsy top bar).

    `adapt_reasoning` (llama-swap) buffers chat/completion request bodies so
    reasoning_adapter can swap a reasoning effort the model's template rejects
    for its closest supported one, retrying once on that rejection.

    `lite_images` (mobile data saver, see datasaver.py) sends big images shown
    in <img> tags as smaller WebP copies.
    """
    full_path = request.url.path  # already includes the prefix, e.g. /Video/api/generate
    if strip_prefix and full_path.startswith(prefix):
        full_path = full_path[len(prefix):] or "/"
    raw_query = request.scope.get("query_string", b"")  # preserve exact encoding
    target_url = httpx.URL(upstream_base + full_path, query=raw_query or None)  # no stray trailing "?"

    forward_headers = _filter_headers(request.headers, extra_drop={"host", "content-length", "cookie"})
    # The upstreams get the browser's own cookies, never the Rocsy login cookie: the
    # gateway has already checked it, and some upstreams are third-party code.
    cookies = "; ".join(part.strip() for part in "; ".join(request.headers.getlist("cookie")).split(";")
                        if part.strip() and part.split("=", 1)[0].strip() != settings.cookie_name)
    if cookies:
        forward_headers["cookie"] = cookies
    if head_inject and request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        # Page loads must come back as a full 200 so the injected (versioned)
        # theme link is current - a 304 would reuse the cached page and its old link.
        for name in ("if-none-match", "if-modified-since"):
            forward_headers.pop(next((key for key in forward_headers if key.lower() == name), name), None)
    if extra_headers:
        # Case-insensitive set, not a plain dict.update -- a caller-supplied
        # header with different casing (e.g. client sent "Authorization",
        # request.headers preserved that casing) must not survive alongside
        # ours, or httpx would forward both as distinct headers.
        for key, value in extra_headers.items():
            _set_header_ci(forward_headers, key, value)

    is_sse_hint = any(full_path.endswith(hint) for hint in _SSE_PATH_HINTS)
    is_model_load = full_path.startswith("/upstream/")
    timeout = SSE_TIMEOUT if is_sse_hint else MODEL_LOAD_TIMEOUT if is_model_load else DEFAULT_TIMEOUT

    client = shared_client()

    # ASGI can deliver a body without either wire-level length header (for
    # example over HTTP/2). Read body-capable methods even in that case.
    # Bodyless GET/HEAD requests go upstream without a chunked request body.
    has_body = (request.method not in ("GET", "HEAD") or "content-length" in request.headers
                or "transfer-encoding" in request.headers)
    body_stream = _sized_request_stream(request, settings.max_body_bytes) if has_body else None
    adapt = adapt_reasoning and request.method == "POST" and reasoning_adapter.applies(full_path)
    raw_body = sent_body = b""
    buffered_error: bytes | None = None

    async def send(content) -> httpx.Response:
        upstream_request = client.build_request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            content=content,
            timeout=timeout,
        )
        return await client.send(upstream_request, stream=True)

    try:
        if adapt:
            raw_body = b"".join([chunk async for chunk in body_stream]) if body_stream else b""
            sent_body = reasoning_adapter.adapt_body(raw_body)
            upstream_response = await send(sent_body)
            if upstream_response.status_code >= 400:
                # Rejections are small and arrive before any stream starts.
                buffered_error = await upstream_response.aread()
                retry = reasoning_adapter.retry_body(raw_body, sent_body, buffered_error)
                if retry is not None:
                    await upstream_response.aclose()
                    buffered_error = None
                    upstream_response = await send(retry)
        else:
            if "content-length" in request.headers:
                forward_headers["content-length"] = request.headers["content-length"]
            upstream_response = await send(body_stream)
    except _BodyTooLarge:
        return JSONResponse(
            {"error": "payload_too_large", "message": f"Request body exceeds the {settings.max_body_mb}MB limit."},
            status_code=413,
        )
    except httpx.TimeoutException:
        return _upstream_error(request, prefix, 504, "upstream_timeout", "The upstream app did not respond in time.")
    except httpx.HTTPError:  # refused, reset, or a malformed reply
        return _upstream_error(request, prefix, 502, "upstream_unavailable", "The upstream app is not reachable.")

    content_type = upstream_response.headers.get("content-type", "")
    is_sse = content_type.startswith("text/event-stream")

    response_headers = _filter_headers(upstream_response.headers)
    # Everything proxied here sits behind the login, so Cloudflare must never
    # keep a shared copy: it caches by file extension (a thumbnail URL ending
    # in .mp4 was served from the edge to logged-out visitors). Browsers still
    # cache "private" responses as before.
    cache_control = _header_ci(response_headers, "Cache-Control") or ""
    if not any(word in cache_control.lower() for word in ("private", "no-store")):
        # "public" is dropped, not just outvoted: "private, public" is contradictory.
        rest = ", ".join(word.strip() for word in cache_control.split(",")
                         if word.strip() and word.strip().lower() != "public")
        _set_header_ci(response_headers, "Cache-Control", f"private, {rest}" if rest else "private")
    if is_sse:
        # Never buffer SSE: force no-cache and preserve/assert the
        # X-Accel-Buffering: no hint so nothing downstream (Cloudflare, any
        # future reverse proxy) buffers either. Don't clobber an upstream value
        # that already set these correctly. Set case-insensitively so we don't
        # end up with both "cache-control" (from upstream) and "Cache-Control"
        # (set here) as two distinct response headers.
        # (Cache-Control is never empty here - "private" was just added above.)
        cache_control = _header_ci(response_headers, "Cache-Control")
        if not any(word in cache_control.lower() for word in ("no-cache", "no-store")):
            _set_header_ci(response_headers, "Cache-Control", f"{cache_control}, no-cache")
        _set_header_ci(response_headers, "X-Accel-Buffering", _header_ci(response_headers, "X-Accel-Buffering", "no"))

    if buffered_error is not None:
        # Already read (decoded) to check for an effort rejection; not retried.
        await upstream_response.aclose()
        for name in ("content-encoding", "content-length"):
            response_headers.pop(next((key for key in response_headers if key.lower() == name), name), None)
        return Response(buffered_error, status_code=upstream_response.status_code,
                        headers=response_headers, media_type=content_type or None)

    if (restyle_css and request.method == "GET" and upstream_response.status_code == 200
            and content_type.startswith("text/css")):
        css = await _read_body(upstream_response)
        if css is None:
            return _upstream_broke()
        for name in ("content-encoding", "content-length"):
            response_headers.pop(next((key for key in response_headers if key.lower() == name), name), None)
        return Response(restyle.rewrite_cached(css), status_code=200, headers=response_headers, media_type=content_type)

    if (lite_images and request.method == "GET" and upstream_response.status_code == 200
            and datasaver.shrinkable(request.headers, content_type, _header_ci(response_headers, "content-length"))):
        raw = await _read_body(upstream_response)
        if raw is None:
            return _upstream_broke()
        for name in ("content-encoding", "content-length"):
            response_headers.pop(next((key for key in response_headers if key.lower() == name), name), None)
        validator = _header_ci(response_headers, "etag") or _header_ci(response_headers, "last-modified") or str(len(raw))
        lite = await datasaver.lite_copy(f"{target_url}|{validator}", raw) if len(raw) >= datasaver.MIN_BYTES else None
        if lite is None:
            return Response(raw, status_code=200, headers=response_headers, media_type=content_type)
        # No validators: a later full-size request must never get a 304 for this copy.
        for name in ("etag", "last-modified", "content-type"):
            response_headers.pop(next((key for key in response_headers if key.lower() == name), name), None)
        _set_header_ci(response_headers, "Vary", "Cookie, Save-Data")
        response_headers["X-Rocsy-Lite"] = "1"
        return Response(lite, status_code=200, headers=response_headers, media_type="image/webp")

    if (head_inject and request.method == "GET" and upstream_response.status_code == 200
            and content_type.startswith("text/html")):
        html = await _read_body(upstream_response)  # decoded (gzip etc.)
        if html is None:
            return _upstream_broke()
        index = html.lower().find(b"</head>")
        if index >= 0:
            html = html[:index] + head_inject.encode() + html[index:]
        if restyle_css:
            html = restyle.tag_stylesheets(html)
        if html_transform:
            html = html_transform(html)
        for name in ("content-encoding", "content-length", "etag", "last-modified"):
            response_headers.pop(next((key for key in response_headers if key.lower() == name), name), None)
        return Response(html, status_code=200, headers=response_headers, media_type=content_type)

    async def body_iterator() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        finally:
            await upstream_response.aclose()

    return _UpstreamStream(
        body_iterator(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=content_type or None,
    )
