from __future__ import annotations

import asyncio
import contextlib
import hmac
import hashlib
import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.background import BackgroundTask

import auth
from auth import (
    check_llama_api_key,
    check_password,
    has_password,
    make_session_cookie,
    password_revision,
    rate_limiter,
    session_payload,
    set_password,
    verify_session_cookie,
    COOKIE_MAX_AGE_SECONDS,
    MAX_ATTEMPTS,
)
from config import settings
from proxy import close_shared_client, proxy_request, shared_client
from security_headers import SecurityHeadersMiddleware
import home as home_data
import llama_params
import restyle
import themes
import favicons
import topbar
import cache_service
import connections
import code_projects
import code_agent
import code_history
import datasaver
import honeypot
from notify import CATALOG as NOTIFY_CATALOG, NotificationHub, watch_services

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gateway")

BASE_DIR = Path(__file__).resolve().parent

# docs_url/redoc_url/openapi_url disabled: FastAPI serves these completely
# unauthenticated by default (no _has_valid_session check applies to them,
# since they're framework-registered routes, not proxy routes) - anyone on
# the internet could otherwise browse the full route/parameter map without
# logging in first. Confirmed reachable pre-fix at /docs, /redoc, /openapi.json.
@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    watcher = asyncio.create_task(watch_services(notifications, UPSTREAMS))
    try:
        yield
    finally:
        watcher.cancel()
        connection_tracker.flush()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await close_shared_client()


app = FastAPI(title="Rocsy Gateway", version="1.2.0", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)
connection_tracker = connections.ConnectionTracker(BASE_DIR / "data" / "connections.json", settings.cookie_secret,
                                                   COOKIE_MAX_AGE_SECONDS)
auth.is_session_revoked = connection_tracker.is_revoked
decoy = honeypot.Honeypot(BASE_DIR / "data" / "honeypot.json", check_password)
# Assets, health checks and service-to-gateway calls aren't people connecting.
_UNTRACKED = ("/static/", "/health", "/theme.css", "/app-icon/", "/notify-sw", "/internal/", "/favicon")


def _visitor_ip(headers: dict[str, str], client_host: str) -> tuple[str, bool]:
    """(ip, local): the visitor's real address for the Connections list. Through
    the tunnel the socket peer is cloudflared, so Cloudflare's header is used."""
    if headers.get("cf-ray") and headers.get("cf-connecting-ip"):
        return headers["cf-connecting-ip"], False
    return client_host or "unknown", True


class ConnectionsMiddleware:
    """Records each visitor (anonymised) and signed-in browser for Settings ->
    Security -> Connections, refuses revoked visitors, and gives older session
    cookies a session id so they can be revoked individually."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith(_UNTRACKED):
            return await self.inner(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        ip, local = _visitor_ip(headers, (scope.get("client") or ("", 0))[0])
        cookie = Request(scope).cookies.get(settings.cookie_name)
        payload = session_payload(cookie) if cookie else None
        sid = payload.get("sid") if payload else None
        try:
            connection_tracker.record(ip, headers.get("cf-ipcountry", ""), local, headers.get("user-agent", ""),
                                      sid if isinstance(sid, str) else None, password_revision(),
                                      path=scope["path"], block_scanners=site_settings.load()["scanner_block"],
                                      fetch_site=headers.get("sec-fetch-site", ""))
        except Exception:  # never let bookkeeping take the site down
            log.exception("Connections tracking failed")
        if connection_tracker.is_blocked(ip):
            response = Response("Your access to Rocsy has been revoked.", status_code=403,
                                media_type="text/plain", headers={"Cache-Control": "no-store"})
            return await response(scope, receive, send)
        if payload is None or sid:
            return await self.inner(scope, receive, send)
        new_cookie = _session_cookie_header(make_session_cookie())

        async def send_with_cookie(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(b"set-cookie", new_cookie.encode("latin-1"))]
            await send(message)

        return await self.inner(scope, receive, send_with_cookie)


_DOT_SEGMENT = re.compile(r"/\.(?!well-known(?:/|$))")


class HoneypotMiddleware:
    """Serves the decoy .env (honeypot.py), 404s every other dotfile path before any
    route or upstream app sees it, and bans whoever presents the decoy's cookie
    secret or API key. The decoy password is caught in POST /login."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.inner(scope, receive, send)
        path = scope["path"]
        if scope["type"] == "http" and honeypot.DECOY_PATH.search(path):
            if scope["method"] not in ("GET", "HEAD"):
                response = Response(status_code=405)
            else:
                response = Response(decoy.env_file(), media_type="text/plain",
                                    headers={"Cache-Control": "no-store"})
            return await response(scope, receive, send)
        if _DOT_SEGMENT.search(path):
            return await _refuse(scope, receive, send, 404, "Not Found")
        request = Request(scope) if scope["type"] == "http" else WebSocket(scope, receive, send)
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if decoy.is_decoy_cookie(request.cookies.get(settings.cookie_name)):
            _ban_decoy_user(scope, "honeypot: forged session cookie")
            return await _refuse(scope, receive, send, 403, "Your access to Rocsy has been revoked.")
        if scheme.lower() == "bearer" and token and decoy.is_decoy_key(token):
            _ban_decoy_user(scope, "honeypot: decoy API key")
            return await _refuse(scope, receive, send, 403, "Your access to Rocsy has been revoked.")
        return await self.inner(scope, receive, send)


async def _refuse(scope, receive, send, status: int, text: str) -> None:
    if scope["type"] == "websocket":
        return await send({"type": "websocket.close", "code": 1008})
    response = Response(text, status_code=status, media_type="text/plain", headers={"Cache-Control": "no-store"})
    await response(scope, receive, send)


def _ban_decoy_user(scope, reason: str) -> None:
    """Only tunnel visitors are banned: blocking a local address would lock the owner
    out of this machine (local use still gets refused, just not banned)."""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    ip, local = _visitor_ip(headers, (scope.get("client") or ("", 0))[0])
    log.warning("Honeypot triggered by %s (%s)%s", ip, reason, "" if not local else " - local, not banned")
    if not local:
        connection_tracker.ban(ip, reason)


# Inner to ConnectionsMiddleware: a banned visitor's next request stops at its 403.
app.add_middleware(HoneypotMiddleware)
app.add_middleware(ConnectionsMiddleware)
# Added last = outermost, so responses ConnectionsMiddleware makes itself
# (the revoked-visitor 403) get the security headers too.
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


_asset_hashes: dict[str, tuple[tuple[int, int], str]] = {}


def _asset_version(name: str) -> str:
    """Content hash for a static file's ?v= - Cloudflare caches /static for
    hours, so the URL has to change whenever the file does. Re-hashed only
    when the file's mtime/size change: this runs on every themed page."""
    path = BASE_DIR / "static" / name
    try:
        stat = path.stat()
        version = (stat.st_mtime_ns, stat.st_size)
        cached = _asset_hashes.get(name)
        if cached is None or cached[0] != version:
            cached = _asset_hashes[name] = (version, hashlib.sha256(path.read_bytes()).hexdigest()[:10])
        return cached[1]
    except OSError:
        return app.version


templates.env.globals["asset_v"] = _asset_version
templates.env.globals["theme_href"] = lambda app_name="home": theme_href(app_name)
site_settings = themes.SettingsStore(BASE_DIR / "data" / "settings.json")
notifications = NotificationHub(BASE_DIR / "data")
upload_cache = cache_service.UploadCache(settings.storage_user_uploads_dir, BASE_DIR / "data" / "upload-cache.json",
                                        reference_db_path=Path("/storage/h3-data/h3-machine.sqlite3"))
# Our own studios get the site theme injected and their CSS colours rewritten
# to theme tokens (restyle.py); Llama and Hermes get token overrides only.
THEMED_APPS = themes.STUDIO_APPS


def theme_href(app_name: str = "home") -> str:
    """/theme.css for one app, versioned by the saved choice (and this code):
    Cloudflare gives it a 4 h browser cache, so a change needs a new URL."""
    values = site_settings.load()
    key = f"{values['theme']}|{values['accent']}|{app_name}|{restyle.REWRITE_VERSION}|{_asset_version('../themes.py')}"
    return f"/theme.css?app={app_name}&v={hashlib.sha256(key.encode()).hexdigest()[:10]}"


def theme_link(app_name: str) -> str:
    return f'<link rel="stylesheet" href="{theme_href(app_name)}" data-bunny-theme>'


def data_saver_script() -> str:
    """Mobile data saver for this device (static/js/data-saver.js). Not deferred:
    it has to see each <video> before the browser starts loading it."""
    mode = site_settings.load()["data_saver"]
    return f'<script src="/static/js/data-saver.js?v={_asset_version("js/data-saver.js")}" data-mode="{mode}"></script>'


def rocsy_bar_link() -> str:
    return f'<link rel="stylesheet" href="/static/css/rocsy-bar.css?v={_asset_version("css/rocsy-bar.css")}">'


_HOSTNAME = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?$")


def tunnel_url(request: Request) -> str:
    """The public URL when this page came through the Cloudflare tunnel (cloudflared
    adds cf-ray / cf-connecting-ip; local visits have neither), else ""."""
    if not (request.headers.get("cf-ray") or request.headers.get("cf-connecting-ip")):
        return ""
    host = request.headers.get("host", "").lower()
    return f"https://{host}" if _HOSTNAME.match(host) else ""


templates.env.globals["tunnel_url"] = tunnel_url
templates.env.globals["app_icon_link"] = lambda app_name: Markup(favicons.link(app_name))
templates.env.globals["rocsy_crumb"] = lambda app_name: Markup(topbar.crumb(app_name))
templates.env.globals["data_saver_script"] = lambda: Markup(data_saver_script())

UPSTREAMS = {
    "Vid": settings.vid_upstream,
    "Mus": settings.mus_upstream,
    "Krea": settings.krea_upstream,
    "Trainer": settings.trainer_upstream,
    "Queue": settings.queue_upstream,
    "Llama": settings.llama_upstream,
    "Hermes": settings.hermes_upstream,
    "Connections": settings.connections_upstream,
    "Agent": settings.agent_upstream,
    "Voice": settings.voice_upstream,
    "Models": settings.models_upstream,
    "REA": settings.rea_upstream,
}


def _client_ip(request: Request) -> str:
    # Through the tunnel the socket peer is cloudflared on 127.0.0.1, so keying
    # the login limiter on request.client.host put every internet visitor in
    # one bucket (a bot's failures locked the owner out too). Use Cloudflare's
    # address instead; the gateway binds loopback only, so nothing outside this
    # machine can forge it.
    headers = {k.lower(): v for k, v in request.headers.items()}
    return _visitor_ip(headers, request.client.host if request.client else "")[0]


def _has_valid_session(request: Request) -> bool:
    return verify_session_cookie(request.cookies.get(settings.cookie_name))


def _set_session_cookie(response: Response, request: Request) -> None:
    # Link the new session to this visitor now, so it can be revoked even
    # before the browser makes another request.
    sid = connections.new_session_id()
    headers = {k.lower(): v for k, v in request.headers.items()}
    ip, local = _visitor_ip(headers, request.client.host if request.client else "")
    connection_tracker.record(ip, headers.get("cf-ipcountry", ""), local, headers.get("user-agent", ""),
                              sid, password_revision(), count=False)
    response.set_cookie(
        key=settings.cookie_name,
        value=make_session_cookie(sid),
        max_age=COOKIE_MAX_AGE_SECONDS,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
        # No explicit domain= -- host-only cookie, per the plan.
    )


def _session_cookie_header(value: str) -> str:
    probe = Response()
    probe.set_cookie(key=settings.cookie_name, value=value, max_age=COOKIE_MAX_AGE_SECONDS,
                     secure=True, httponly=True, samesite="lax", path="/")
    return probe.headers["set-cookie"]


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=settings.cookie_name, path="/")


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error in gateway request")
    return JSONResponse({"error": "internal_error", "message": "Something went wrong."}, status_code=500)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def home(request: Request):
    if not _has_valid_session(request):
        return RedirectResponse(url=f"/login?next={quote('/')}", status_code=302)
    values = site_settings.load()
    # Render with live data so Latest and the status dots are there on first paint;
    # a slow upstream just means the page renders without it and home.js fills in.
    try:
        initial = await asyncio.wait_for(home_data.snapshot(UPSTREAMS, settings.llama_api_key, set(values["hidden"]),
                                                             art_exclude=frozenset(values["trainer_art_exclude"])), 1.0)
    except asyncio.TimeoutError:
        initial = None
    return templates.TemplateResponse(request, "home.html", {"app_version": app.version, "hidden": set(values["hidden"]),
                                                             "initial": initial, **themes.home_sections(values)})


@app.get("/theme.css")
async def theme_css(preview: str = "", accent: str = "", app: str = "home") -> Response:
    """The chosen site theme, mapped onto every studio's CSS variables. Public
    (the login page uses it too) - it's only colours. ?preview= renders an
    unsaved theme for the Settings page's live preview."""
    current = site_settings.load()
    values = themes.clean({"theme": preview, "accent": accent}) if preview else current
    return Response(themes.build_css(values["theme"], values["accent"], app), media_type="text/css",
                    headers={"Cache-Control": "no-cache"})


@app.get("/app-icon/{app_name}.svg")
async def app_icon(app_name: str) -> Response:
    """Per-service favicon (public: just a coloured glyph)."""
    body = favicons.svg(app_name)
    if body is None:
        return Response(status_code=404)
    return Response(body, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/Settings")
async def settings_page(request: Request):
    if not _has_valid_session(request):
        return RedirectResponse(url=f"/login?next={quote('/Settings')}", status_code=302)
    return templates.TemplateResponse(request, "settings.html", {"app_version": app.version, "developer_build": settings.developer_build})


@app.get("/Code")
@app.get("/Code/")
async def code_page(request: Request):
    if not _has_valid_session(request):
        return RedirectResponse(url=f"/login?next={quote('/Code/')}", status_code=302)
    return templates.TemplateResponse(request, "code.html", {})


@app.get("/Code/api/projects")
async def code_project_list(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"projects": await asyncio.to_thread(code_projects.list_projects)}


CODE_LOGIN_EXPIRED = "Your Rocsy login has expired. Reload the page to sign in again."
CODE_FORBIDDEN = "Rocsy blocked this change because it didn't come from the Vibe Coding page. Reload the page and try again."


def _code_write_guard(request: Request) -> JSONResponse | None:
    """Changes from the Vibe Coding page only: logged in, same origin."""
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized", "message": CODE_LOGIN_EXPIRED}, status_code=401)
    if request.headers.get("sec-fetch-site", "same-origin") not in {"same-origin", "none"}:
        return JSONResponse({"error": "forbidden", "message": CODE_FORBIDDEN}, status_code=403)
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        return JSONResponse({"error": "forbidden", "message": CODE_FORBIDDEN}, status_code=403)
    return None


async def _code_body(request: Request) -> dict | JSONResponse:
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_json", "message": "The page sent a malformed request. Reload it and try again."},
                            status_code=400)
    return body


@app.post("/Code/api/projects")
async def code_project_create(request: Request):
    if (denied := _code_write_guard(request)) is not None:
        return denied
    body = await _code_body(request)
    if isinstance(body, JSONResponse):
        return body
    name, idea, kind = (str(body.get(k) or "") for k in ("name", "idea", "kind"))
    if len(name) > 80 or len(idea) > 4000:
        return JSONResponse({"error": "too_long", "message": "Keep the name under 80 characters and the description under 4,000."},
                            status_code=400)
    info = await asyncio.to_thread(code_agent.create_project, name, idea, kind if kind in ("web", "python", "node", "any") else "any")
    started, agent_error = None, None
    if body.get("start") and idea.strip():
        try:
            started = await code_agent.send(settings.agent_upstream, info, idea, "goal" if body.get("goal") else "work",
                                            str(body.get("budget") or ""), [], True, _code_model(body))
        except HTTPException as exc:
            # The folder exists either way: report the agent problem instead of failing the whole create.
            agent_error = str(exc.detail)
    return {"project": info, "agent": started, "agent_error": agent_error}


def _code_model(body: dict) -> dict | None:
    model = body.get("model")
    if not isinstance(model, dict):
        return None
    return {k: str(model.get(k) or "")[:200] for k in ("model", "provider", "reasoning")}


@app.get("/Code/api/projects/{project_id}/agent")
async def code_project_agent(request: Request, project_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    info = await asyncio.to_thread(code_projects.project_info, project_id)
    return await code_agent.agent_state(settings.agent_upstream, info)


@app.post("/Code/api/projects/{project_id}/agent")
async def code_project_agent_send(request: Request, project_id: str):
    """Call the Master Agent on this project: continue coding, or start a goal it splits into milestones."""
    if (denied := _code_write_guard(request)) is not None:
        return denied
    body = await _code_body(request)
    if isinstance(body, JSONResponse):
        return body
    text = str(body.get("text") or "").strip()
    mode = "goal" if body.get("mode") == "goal" else "work"
    milestones = [str(x) for x in body.get("milestones") or [] if isinstance(x, str)][:12]
    if not text or len(text) > 12000:
        return JSONResponse({"error": "Write what the agent should do (up to 12,000 characters)"}, status_code=400)
    budget = str(body.get("budget") or "")
    if budget not in ("", "infinite") and not budget.isdigit():
        return JSONResponse({"error": "invalid_budget", "message": "Pick a turn budget from the list."}, status_code=400)
    info = await asyncio.to_thread(code_projects.project_info, project_id)
    session = str(body.get("session") or "")
    return await code_agent.send(settings.agent_upstream, info, text, mode, budget, milestones,
                                 bool(body.get("fresh")), _code_model(body), session if code_agent.SID.fullmatch(session) else "")


@app.get("/Code/api/projects/{project_id}/sessions")
async def code_project_sessions(request: Request, project_id: str):
    """Sessions tab: every Master Agent session of the project, also ones found by the files they touched."""
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    info = await asyncio.to_thread(code_projects.project_info, project_id)
    return await code_agent.sessions_tab(settings.agent_upstream, info)


@app.post("/Code/api/projects/{project_id}/goal")
async def code_project_goal(request: Request, project_id: str):
    if (denied := _code_write_guard(request)) is not None:
        return denied
    body = await _code_body(request)
    if isinstance(body, JSONResponse):
        return body
    part = body.get("part")
    info = await asyncio.to_thread(code_projects.project_info, project_id)
    return await code_agent.goal_action(settings.agent_upstream, info, str(body.get("action") or ""),
                                        part if isinstance(part, int) and part >= 0 else None)


@app.get("/Code/api/projects/{project_id}/stats")
async def code_project_stats(request: Request, project_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    info = await asyncio.to_thread(code_projects.project_info, project_id)
    return await code_agent.stats(settings.agent_upstream, info)


@app.get("/Code/api/projects/{project_id}/history")
async def code_project_history(request: Request, project_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized", "message": CODE_LOGIN_EXPIRED}, status_code=401)
    info = await asyncio.to_thread(code_projects.project_info, project_id)
    return await asyncio.to_thread(code_history.state, info)


@app.post("/Code/api/projects/{project_id}/history")
async def code_project_history_action(request: Request, project_id: str):
    """Version history: turn it on/off, save a version now, or restore one (the Undo button restores the last
    version taken before an agent message)."""
    if (denied := _code_write_guard(request)) is not None:
        return denied
    body = await _code_body(request)
    if isinstance(body, JSONResponse):
        return body
    action = body.get("action")
    info = await asyncio.to_thread(code_projects.project_info, project_id)
    if action in ("enable", "disable"):
        return await asyncio.to_thread(code_history.set_enabled, info, action == "enable")
    if action == "save":
        return await asyncio.to_thread(code_history.save, info, str(body.get("label") or "").strip()[:160])
    if action == "restore":
        version = str(body.get("version") or "")
        if not re.fullmatch(r"[0-9a-f]{1,16}", version):
            return JSONResponse({"error": "invalid_version", "message": "Pick a version from the list."}, status_code=400)
        if (await code_agent.agent_state(settings.agent_upstream, info))["busy"]:
            return JSONResponse({"error": "agent_busy", "message": "The Master Agent is working on this project right now. "
                                 "Wait for its turn to end (or stop it on the Agent page), then restore."}, status_code=409)
        return await asyncio.to_thread(code_history.restore, info, version)
    return JSONResponse({"error": "unknown_action", "message": "Unknown history action."}, status_code=400)


@app.delete("/Code/api/projects/{project_id}")
async def code_project_delete(request: Request, project_id: str):
    if (denied := _code_write_guard(request)) is not None:
        return denied
    body = await _code_body(request)
    if isinstance(body, JSONResponse):
        return body
    if not isinstance(body.get("name"), str):
        return JSONResponse({"error": "missing_project_name", "message": "Type the project name to confirm the delete."},
                            status_code=400)
    info = await asyncio.to_thread(code_projects.project_info, project_id)
    result = await asyncio.to_thread(code_projects.delete_project, project_id, body["name"])
    await asyncio.to_thread(code_agent.forget_project, info)
    return result


@app.get("/Code/api/projects/{project_id}/files")
async def code_project_files(request: Request, project_id: str, path: str = ""):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await asyncio.to_thread(code_projects.list_files, project_id, path)


@app.get("/Code/api/projects/{project_id}/file")
async def code_project_file(request: Request, project_id: str, path: str, download: bool = False):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    file = await asyncio.to_thread(code_projects.project_path, project_id, path)
    if not file.is_file():
        return JSONResponse({"error": "not_a_file", "message": "That's a folder, not a file."}, status_code=400)
    if not download and file.suffix.lower() in code_projects.TEXT_EXT and file.stat().st_size > code_projects.MAX_TEXT_BYTES:
        return JSONResponse({"error": "file_too_large", "message": "Download this file to inspect it."}, status_code=413)
    if file.suffix.lower() in code_projects.TEXT_EXT:
        media_type = "text/plain; charset=utf-8"
    elif file.suffix.lower() in code_projects.MEDIA_EXT:
        media_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    else:
        media_type = "application/octet-stream"
        download = True
    return FileResponse(file, media_type=media_type, filename=file.name if download else None,
                        content_disposition_type="attachment" if download else "inline",
                        headers={"Cache-Control": "no-store"})


@app.get("/Code/api/projects/{project_id}/download")
async def code_project_download(request: Request, project_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    path, name = await asyncio.to_thread(code_projects.make_zip, project_id)
    return FileResponse(path, media_type="application/zip", filename=name,
                        background=BackgroundTask(os.unlink, path), headers={"Cache-Control": "no-store"})


@app.get("/Code/api/projects/{project_id}/preview-url")
async def code_project_preview_url(request: Request, project_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    token = await asyncio.to_thread(code_projects.make_preview_token, project_id, settings.cookie_secret)
    return {"url": f"/Code/preview/{token}/index.html"}


@app.get("/Code/preview/{token}/{path:path}")
async def code_project_preview(token: str, path: str):
    project_id = await asyncio.to_thread(code_projects.verify_preview_token, token, settings.cookie_secret)
    file = await asyncio.to_thread(code_projects.project_path, project_id, path)
    if not file.is_file() or file.suffix.lower() not in code_projects.PREVIEW_EXT:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return FileResponse(file, media_type=mimetypes.guess_type(file.name)[0] or "application/octet-stream",
                        headers={"Cache-Control": "no-store"})


@app.get("/Cache")
async def cache_page(request: Request):
    if not _has_valid_session(request):
        return RedirectResponse(url=f"/login?next={quote('/Cache')}", status_code=302)
    return templates.TemplateResponse(request, "cache.html", {"app_version": app.version})


@app.get("/Cache/")
async def cache_page_slash(request: Request):
    return await cache_page(request)


@app.get("/Cache/api/files")
async def cache_files(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(upload_cache.list), headers={"Cache-Control": "no-store"})


@app.get("/Cache/api/groups")
async def cache_groups(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(upload_cache.groups), headers={"Cache-Control": "no-store"})


@app.post("/Cache/api/groups")
async def create_cache_group(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    return JSONResponse(await asyncio.to_thread(upload_cache.save_group, None, str(body.get("name", "")), body.get("files"), body.get("rag_terms"), body.get("note")),
                        status_code=201, headers={"Cache-Control": "no-store"})


@app.put("/Cache/api/groups/{group_id}")
async def update_cache_group(request: Request, group_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    return JSONResponse(await asyncio.to_thread(upload_cache.save_group, group_id, str(body.get("name", "")), body.get("files"), body.get("rag_terms"), body.get("note")),
                        headers={"Cache-Control": "no-store"})


@app.delete("/Cache/api/groups/{group_id}")
async def delete_cache_group(request: Request, group_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    await asyncio.to_thread(upload_cache.delete_group, group_id)
    return JSONResponse({"deleted": group_id}, headers={"Cache-Control": "no-store"})


@app.get("/Cache/api/files/zip")
async def cache_files_zip(request: Request, ids: str = ""):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    path = await asyncio.to_thread(upload_cache.make_zip, [i for i in ids.split(",") if i])
    return FileResponse(path, media_type="application/zip", filename=time.strftime("rocsy-cache-%Y%m%d-%H%M%S.zip"),
                        background=BackgroundTask(os.unlink, path), headers={"Cache-Control": "no-store"})


@app.get("/Cache/api/files/{file_id}/content")
async def cache_file_content(request: Request, file_id: str, download: bool = False):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    path = await asyncio.to_thread(upload_cache.content_path, file_id)
    if download:
        return FileResponse(path, filename=path.name, content_disposition_type="attachment",
                            headers={"Cache-Control": "private, max-age=60"})
    return FileResponse(path, headers={"Cache-Control": "private, max-age=60"})


@app.get("/Cache/api/files/{file_id}/thumb")
async def cache_file_thumb(request: Request, file_id: str, v: str = ""):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    thumb = await asyncio.to_thread(upload_cache.thumbnail_path, file_id)
    if thumb is None:
        return FileResponse(await asyncio.to_thread(upload_cache.content_path, file_id), headers={"Cache-Control": "private, max-age=60"})
    # ?v= is the file's mtime+size, so a given URL always means the same pixels.
    cache = "private, max-age=31536000, immutable" if v else "private, max-age=60"
    return FileResponse(thumb, media_type="image/webp", headers={"Cache-Control": cache})


@app.post("/Cache/api/files/upload")
async def upload_cache_file(request: Request, upload: UploadFile = File(...)):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # Up to 250 MB copied to disk: a worker thread, not the event loop.
    return JSONResponse(await asyncio.to_thread(upload_cache.save_upload, upload), status_code=201, headers={"Cache-Control": "no-store"})


@app.post("/Cache/api/docs")
async def create_cache_doc(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    return JSONResponse(await asyncio.to_thread(upload_cache.save_doc, None, str(body.get("name", "")), body.get("text", "")),
                        status_code=201, headers={"Cache-Control": "no-store"})


@app.put("/Cache/api/docs/{file_id}")
async def update_cache_doc(request: Request, file_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    return JSONResponse(await asyncio.to_thread(upload_cache.save_doc, file_id, "", body.get("text", "")), headers={"Cache-Control": "no-store"})


@app.delete("/Cache/api/files/{file_id}")
async def delete_cache_file(request: Request, file_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(upload_cache.delete, file_id), headers={"Cache-Control": "no-store"})


@app.post("/Cache/api/files/{file_id}/pin")
async def pin_cache_file(request: Request, file_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    return JSONResponse(await asyncio.to_thread(upload_cache.pin, file_id, str(body.get("alias", "")), body.get("rag_terms"), body.get("note")), headers={"Cache-Control": "no-store"})


@app.delete("/Cache/api/files/{file_id}/pin")
async def unpin_cache_file(request: Request, file_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(upload_cache.unpin, file_id), headers={"Cache-Control": "no-store"})


@app.get("/Voice")
async def voice_page(request: Request):
    if not _has_valid_session(request):
        return RedirectResponse(url=f"/login?next={quote('/Voice')}", status_code=302)
    return RedirectResponse(url="/Voice/", status_code=302)


@app.api_route("/Voice/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_voice(request: Request, path: str):
    return await _handle_app_proxy(request, "Voice", path)


@app.get("/Models")
async def models_root(request: Request):
    return RedirectResponse(url="/Models/", status_code=302)


@app.api_route("/Models/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_models(request: Request, path: str):
    return await _handle_app_proxy(request, "Models", path)


@app.get("/REA")
async def rea_root(request: Request):
    return RedirectResponse(url="/REA/", status_code=302)


@app.api_route("/REA/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_rea(request: Request, path: str):
    return await _handle_app_proxy(request, "REA", path)


@app.get("/Voices")
async def voices_old_link() -> RedirectResponse:
    return RedirectResponse(url="/Voice", status_code=301)


@app.get("/api/settings")
async def get_site_settings(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {**site_settings.load(), "themes": themes.catalog(), "modules": themes.module_catalog()}


@app.get("/api/password")
async def password_status(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"password_set": has_password()}, headers={"Cache-Control": "no-store"})


def _current_session(request: Request) -> tuple[str, str | None]:
    """(visitor id, session id) of the browser making this request."""
    ip, _ = _visitor_ip({k.lower(): v for k, v in request.headers.items()},
                        request.client.host if request.client else "")
    payload = session_payload(request.cookies.get(settings.cookie_name)) or {}
    sid = payload.get("sid")
    return connection_tracker.visitor_id(ip), sid if isinstance(sid, str) else None


@app.get("/api/connections")
async def list_connections(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    vid, sid = _current_session(request)
    data = connection_tracker.summary(vid, sid, password_revision(), has_password())
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.post("/api/connections/clear")
async def clear_connections(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.headers.get("x-bb-action") != "1":
        return JSONResponse({"error": "Invalid request."}, status_code=400)
    count = connection_tracker.clear(_current_session(request)[0], password_revision(), has_password())
    return JSONResponse({"cleared": count,
                         "message": f"Cleared {count} connection{'' if count == 1 else 's'}."})


@app.post("/api/connections/{visitor_id}/{action}")
async def connection_action(request: Request, visitor_id: str, action: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.headers.get("x-bb-action") != "1" or action not in {"revoke", "restore"} \
            or not re.fullmatch(r"[0-9a-f]{16}", visitor_id):
        return JSONResponse({"error": "Invalid request."}, status_code=400)
    if visitor_id == _current_session(request)[0]:
        return JSONResponse({"error": "You can't revoke your own connection."}, status_code=400)
    try:
        if action == "revoke":
            count = connection_tracker.revoke(visitor_id)
            message = f"Access revoked · {count} signed-in device{'s' if count != 1 else ''} signed out."
        else:
            connection_tracker.restore(visitor_id)
            message = "Un-revoked. They can connect and sign in again with the password."
    except KeyError:
        return JSONResponse({"error": "That connection no longer exists."}, status_code=404)
    log.info("Connections: %s %s", action, visitor_id)
    return JSONResponse({"ok": True, "message": message}, headers={"Cache-Control": "no-store"})


@app.put("/api/password")
async def change_password(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"error": "JSON required."}, status_code=415)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Invalid request."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid request."}, status_code=400)
    current, new = body.get("current_password", ""), body.get("new_password")
    if not isinstance(current, str) or len(current) > 200 or not isinstance(new, str) or not 8 <= len(new) <= 200:
        return JSONResponse({"error": "Use a new password of 8 to 200 characters."}, status_code=400)
    ip = _client_ip(request)
    locked, remaining, _, _, attempts = rate_limiter.begin_attempt(ip)
    if locked:
        return JSONResponse({"error": f"Too many attempts. Try again in {max(1, int(remaining // 60) + 1)} minutes."}, status_code=429)
    if not await asyncio.to_thread(set_password, current, new):
        await asyncio.sleep(rate_limiter.delay_for(attempts))
        return JSONResponse({"error": "Incorrect current password."}, status_code=403)
    rate_limiter.record_success(ip)
    response = JSONResponse({"password_set": True}, headers={"Cache-Control": "no-store"})
    _set_session_cookie(response, request)
    return response


STORAGE_SCOPES = {
    "newest_hour": ("newest", 60 * 60),
    "newest_day": ("newest", 24 * 60 * 60),
    "newest_week": ("newest", 7 * 24 * 60 * 60),
    "older_hour": ("older", 60 * 60),
    "older_day": ("older", 24 * 60 * 60),
    "older_week": ("older", 7 * 24 * 60 * 60),
    "older_month": ("older", 30 * 24 * 60 * 60),
    "all": ("all", 0),
}

# Storage cleanup is deliberately conservative. The agent workspace also holds
# Remotion, Vibe Coding projects and REA game projects (decompiled sources whose
# PNG/OGG files are game assets), so only generated media inside a chat's own
# session folder is eligible, and never inside a project folder there.
_STORAGE_PROTECTED_DIRS = {
    ".git", "node_modules", "src", "backend", "models", "model", "checkpoints",
    "loras", "lora", "code", "config", "configs", "python", "venv", ".venv",
    "motion", code_projects.VIBE_DIR,
}
_STORAGE_MEDIA_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp", ".tif", ".tiff",
    ".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v",
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus",
}
# A folder holding one of these is a project: everything below it is kept.
_STORAGE_PROJECT_MARKERS = code_projects.MARKERS | {
    "project.godot", "Assembly-CSharp.dll", "Makefile", "CMakeLists.txt", "GOAL.md", "PLAN.md", ".git",
}
_STORAGE_PROJECT_SUFFIXES = {".csproj", ".sln", ".uproject", ".pck"}
_SESSION_DIR = re.compile(r"[0-9a-f]{32}")
_H3_STORED_NAME = re.compile(r"[0-9a-f]{32}(\.[A-Za-z0-9]+)*")


def _storage_roots() -> tuple[tuple[str, Path], ...]:
    return (("ComfyUI outputs", settings.storage_comfy_output_dir),
            ("Master Agent workspace", settings.storage_agent_workspace_dir),
            ("User uploads", settings.storage_user_uploads_dir))


def _is_project_dir(folder: Path, cache: dict[Path, bool]) -> bool:
    if folder not in cache:
        try:
            cache[folder] = any(child.name in _STORAGE_PROJECT_MARKERS or child.suffix.lower() in _STORAGE_PROJECT_SUFFIXES
                                for child in folder.iterdir())
        except OSError:
            cache[folder] = True  # unreadable: keep it
    return cache[folder]


def _cache_pinned() -> set[str]:
    """Upload paths (relative to the uploads root) pinned on the Cache page."""
    return {entry["path"] for entry in upload_cache._load().values()
            if isinstance(entry, dict) and entry.get("pinned") in (True, "true") and isinstance(entry.get("path"), str)}


def _storage_file_is_safe(label: str, path: Path, root: Path, cache: dict[Path, bool] | None = None,
                          pinned: set[str] = frozenset()) -> bool:
    """Return whether cleanup may remove this file.

    ComfyUI's mounted output root is already an output-only boundary. The
    agent workspace is mixed-use: only media a chat session made is eligible
    (<session id>/..., not in a project folder or a protected directory).
    The uploads folder is H3 Machine's reference library too: only files put
    there through the Cache page (top level, own names, not pinned) are eligible.
    """
    if label == "ComfyUI outputs":
        return True
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    if label == "User uploads":
        # Sub-folders (agentic-sessions/, .retry-cache/) and <uuid hex>.<ext> names belong to
        # H3 Machine, whose database still points at them; H3 sweeps its own unused ones.
        return (len(relative.parts) == 1 and not path.name.startswith(".")
                and not _H3_STORED_NAME.fullmatch(path.name) and relative.as_posix() not in pinned)
    if len(relative.parts) < 2 or not _SESSION_DIR.fullmatch(relative.parts[0]):
        return False  # loose workspace files, rea-*, agk-source, src, vibe-projects, ...
    if any(part.lower() in _STORAGE_PROTECTED_DIRS for part in relative.parts[:-1]):
        return False
    if path.suffix.lower() not in _STORAGE_MEDIA_SUFFIXES:
        return False
    cache = {} if cache is None else cache
    session = root / relative.parts[0]
    folder = path.parent
    while folder != session:  # the session folder itself is a chat, not a project
        if _is_project_dir(folder, cache):
            return False
        folder = folder.parent
    return True


def _storage_files() -> list[tuple[str, Path, os.stat_result]]:
    files = []
    project_dirs: dict[Path, bool] = {}
    pinned = _cache_pinned()
    for label, root in _storage_roots():
        try:
            root = root.resolve(strict=True)
        except OSError:
            continue
        if not root.is_dir():
            continue
        try:
            paths = root.rglob("*")
            for path in paths:
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    if not _storage_file_is_safe(label, path, root, project_dirs, pinned):
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                files.append((label, path, stat))
        except OSError:
            continue
    return files


def _storage_summary(include_uploads: bool = False) -> dict:
    files = _storage_files()
    by_root = {label: {"files": 0, "bytes": 0} for label, _ in _storage_roots()}
    for label, _, stat in files:
        by_root.setdefault(label, {"files": 0, "bytes": 0})
        by_root[label]["files"] += 1
        by_root[label]["bytes"] += stat.st_size
    now = time.time()
    estimates = {}
    for scope, (mode, seconds) in STORAGE_SCOPES.items():
        cutoff = now - seconds
        chosen = [stat for label, _, stat in files
                  if (include_uploads or label != "User uploads")
                  if mode == "all" or (mode == "newest" and stat.st_mtime >= cutoff)
                  or (mode == "older" and stat.st_mtime < cutoff)]
        estimates[scope] = {"files": len(chosen), "bytes": sum(stat.st_size for stat in chosen)}
    return {"roots": [{"name": label, "path": str(root), **by_root[label]} for label, root in _storage_roots()],
            "files": len(files), "bytes": sum(stat.st_size for _, _, stat in files),
            "include_uploads": include_uploads, "estimates": estimates}


async def _delete_cards_for_files(selected: set[Path]) -> tuple[int, int]:
    """Delete service history only when its published output path was selected."""
    if not selected:
        return 0, 0
    deleted = errors = 0
    root = settings.storage_comfy_output_dir.resolve()
    async with httpx.AsyncClient(timeout=8.0) as client:
        for service in ("Vid", "Krea", "Mus"):
            try:
                response = await client.get(f"{UPSTREAMS[service]}/{service}/api/jobs")
                response.raise_for_status()
                jobs = response.json()
                if not isinstance(jobs, list):
                    continue
            except Exception:
                errors += 1
                continue
            for job in jobs:
                if not isinstance(job, dict) or job.get("state") in {"waiting_for_gpu", "preparing", "queued", "rendering", "enhancing", "cancelling"}:
                    continue
                paths = job.get("output_files")
                if not isinstance(paths, list):
                    continue
                linked = set()
                for value in paths:
                    if not isinstance(value, str) or not value:
                        continue
                    path = Path(value)
                    linked.add(path.resolve() if path.is_absolute() else (root / path).resolve())
                if not linked.intersection(selected):
                    continue
                try:
                    removed = await client.delete(f"{UPSTREAMS[service]}/{service}/api/jobs/{job.get('id')}")
                    if removed.status_code == 200:
                        deleted += 1
                    elif removed.status_code != 404:
                        errors += 1
                except Exception:
                    errors += 1
    return deleted, errors


@app.get("/api/storage")
async def get_storage(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    include_uploads = request.query_params.get("include_uploads", "").lower() in {"1", "true", "yes"}
    return await asyncio.to_thread(_storage_summary, include_uploads)


@app.get("/api/uninstall/status")
async def uninstall_status(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    response = await _hostctl("GET", "/uninstall/status")
    if response.status_code != 200:
        return response
    data = json.loads(response.body)
    data["password_required"] = has_password()
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.post("/api/uninstall/start")
async def uninstall_start(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request)  # JSON only: blocks cross-site form posts
    if body is None:
        return JSONResponse({"error": "Invalid request."}, status_code=415)
    if body.get("phrase") != "UNINSTALL ROCSY" or type(body.get("delete_files")) is not bool:
        return JSONResponse({"error": "Enter the exact confirmation phrase and choose what to keep."}, status_code=400)
    password = body.get("password")
    if not isinstance(password, str) or len(password) > 200:
        return JSONResponse({"error": "Enter your Rocsy password."}, status_code=400)
    ip = _client_ip(request)
    locked, remaining, _, _, attempts = rate_limiter.begin_attempt(ip)
    if locked:
        return JSONResponse({"error": f"Too many attempts. Try again in {max(1, int(remaining // 60) + 1)} minutes."}, status_code=429)
    if not await asyncio.to_thread(check_password, password):
        await asyncio.sleep(rate_limiter.delay_for(attempts))
        return JSONResponse({"error": "Incorrect password."}, status_code=403)
    rate_limiter.record_success(ip)
    return await _hostctl("POST", "/uninstall/start", {"delete_files": body["delete_files"]})


@app.post("/api/storage/cleanup")
async def cleanup_storage(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request)  # JSON only: blocks cross-site form posts
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    scope = body.get("scope")
    if scope not in STORAGE_SCOPES:
        return JSONResponse({"error": "invalid_scope"}, status_code=400)
    include_uploads = bool(body.get("include_uploads"))
    deleted, deleted_bytes, errors, selected_files = await asyncio.to_thread(
        _delete_storage_files, *STORAGE_SCOPES[scope], include_uploads)
    deleted_cards, card_errors = await _delete_cards_for_files(selected_files)
    return {"scope": scope, "deleted": deleted, "deleted_bytes": deleted_bytes,
            "deleted_cards": deleted_cards, "errors": errors + card_errors,
            "summary": await asyncio.to_thread(_storage_summary, include_uploads)}


def _delete_storage_files(mode: str, seconds: int, include_uploads: bool) -> tuple[int, int, int, set[Path]]:
    """(deleted, deleted_bytes, errors, selected paths) for one cleanup scope."""
    cutoff = time.time() - seconds
    deleted = deleted_bytes = errors = 0
    selected_files: set[Path] = set()
    for label, path, stat in _storage_files():
        if label == "User uploads" and not include_uploads:
            continue
        if mode == "newest" and stat.st_mtime < cutoff:
            continue
        if mode == "older" and stat.st_mtime >= cutoff:
            continue
        resolved = path.resolve()
        try:
            path.unlink()
            deleted += 1
            deleted_bytes += stat.st_size
            selected_files.add(resolved)  # only removed files take their job cards with them
        except OSError:
            errors += 1
    return deleted, deleted_bytes, errors, selected_files


# Agentic logs: Master Agent chats (bunny-agent) and the Image/Music/Voice
# Agentic sessions (shared framework). Deleted through each service's own API
# so busy sessions are skipped and in-memory state stays consistent.
AGENTIC_LOG_SOURCES = (
    ("agent", "Master Agent chats"),
    ("image", "Image Agentic"),
    ("music", "Music Agentic"),
    ("voice", "Voice Agentic"),
)
_FRAMEWORK_ACTIVE = {"planning", "reviewing_plan", "queued", "rendering", "reviewing_output", "retrying"}


async def _agentic_log_rows(client: httpx.AsyncClient, source: str) -> list[dict]:
    """[{id, updated, busy}] for one source; raises on an unreachable service."""
    if source == "agent":
        response = await client.get(f"{settings.agent_upstream}/Agent/api/sessions", params={"limit": 0})
        response.raise_for_status()
        rows = response.json().get("sessions", [])
        return [{"id": r["id"], "updated": float(r.get("updated") or 0), "busy": bool(r.get("busy"))}
                for r in rows if isinstance(r, dict) and r.get("id")]
    response = await client.get(f"{settings.framework_upstream}/v1/agentic/{source}/sessions", params={"limit": 0},
                                headers={"X-Queue-Internal-Secret": settings.queue_internal_secret})
    response.raise_for_status()
    rows = response.json()
    return [{"id": r["id"], "updated": float(r.get("updated") or r.get("created") or 0), "busy": r.get("state") in _FRAMEWORK_ACTIVE}
            for r in rows if isinstance(r, dict) and r.get("id")]


def _agentic_log_matches(row: dict, mode: str, cutoff: float) -> bool:
    return mode == "all" or (mode == "newest" and row["updated"] >= cutoff) or (mode == "older" and row["updated"] < cutoff)


async def _agentic_log_summary(client: httpx.AsyncClient) -> dict:
    now = time.time()
    sources = []
    for source, name in AGENTIC_LOG_SOURCES:
        try:
            rows = await _agentic_log_rows(client, source)
        except Exception:
            sources.append({"id": source, "name": name, "available": False})
            continue
        estimates = {scope: sum(1 for r in rows if not r["busy"] and _agentic_log_matches(r, mode, now - seconds))
                     for scope, (mode, seconds) in STORAGE_SCOPES.items()}
        sources.append({"id": source, "name": name, "available": True, "sessions": len(rows),
                        "busy": sum(1 for r in rows if r["busy"]), "estimates": estimates})
    return {"sources": sources}


@app.get("/api/storage/agentic")
async def get_agentic_logs(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    async with httpx.AsyncClient(timeout=8.0) as client:
        return JSONResponse(await _agentic_log_summary(client), headers={"Cache-Control": "no-store"})


@app.post("/api/storage/agentic/cleanup")
async def cleanup_agentic_logs(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request)  # JSON only: blocks cross-site form posts
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    if body.get("scope") not in STORAGE_SCOPES:
        return JSONResponse({"error": "invalid_scope"}, status_code=400)
    known = {source for source, _ in AGENTIC_LOG_SOURCES}
    chosen = body.get("sources")
    if not isinstance(chosen, list) or not chosen or any(item not in known for item in chosen):
        return JSONResponse({"error": "Choose at least one log source."}, status_code=400)
    mode, seconds = STORAGE_SCOPES[body["scope"]]
    cutoff = time.time() - seconds
    deleted = skipped = errors = 0
    async with httpx.AsyncClient(timeout=15.0) as client:
        for source in dict.fromkeys(chosen):
            try:
                rows = [r for r in await _agentic_log_rows(client, source) if _agentic_log_matches(r, mode, cutoff)]
            except Exception:
                errors += 1
                continue
            skipped += sum(1 for r in rows if r["busy"])
            ids = [r["id"] for r in rows if not r["busy"]]
            if not ids:
                continue
            if source == "agent":
                for start in range(0, len(ids), 500):
                    try:
                        response = await client.post(f"{settings.agent_upstream}/Agent/api/sessions/delete", json={"ids": ids[start:start + 500]})
                        response.raise_for_status()
                        result = response.json()
                        deleted += len(result.get("deleted", []))
                        skipped += len(result.get("busy", []))
                    except Exception:
                        errors += 1
                continue
            # The framework deletes one session per call: a few at a time, not one by one.
            limit = asyncio.Semaphore(8)

            async def delete_one(sid: str) -> str:
                async with limit:
                    try:
                        response = await client.delete(f"{settings.framework_upstream}/v1/agentic/{source}/sessions/{sid}",
                                                       headers={"X-Queue-Internal-Secret": settings.queue_internal_secret})
                    except Exception:
                        return "error"
                return {200: "deleted", 409: "busy", 404: "gone"}.get(response.status_code, "error")

            outcomes = await asyncio.gather(*(delete_one(sid) for sid in ids))
            deleted += outcomes.count("deleted")
            skipped += outcomes.count("busy")
            errors += outcomes.count("error")
        summary = await _agentic_log_summary(client)
    return {"deleted": deleted, "skipped_busy": skipped, "errors": errors, **summary}


# Settings -> Developer (Compile Installer) lives in developer.py, a dev-only file that the public
# repository doesn't include; without it, or with developer_build off, there is no Developer tab.
if settings.developer_build:
    try:
        import developer
    except ImportError:
        developer = None
    if developer is not None:
        developer.register(app, settings_obj=settings, has_valid_session=_has_valid_session)


@app.put("/api/settings")
async def put_site_settings(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # JSON only: a cross-site form post can't send this content type without a CORS preflight.
    if not request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"error": "json_required"}, status_code=415)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    saved = site_settings.save({key: body[key] for key in ("theme", "accent", "hidden", "order", "groups", "scanner_block", "data_saver", "trainer_art_exclude") if key in body})
    return {**saved, "themes": themes.catalog(), "modules": themes.module_catalog()}


async def _json_body(request: Request) -> dict | None:
    if not request.headers.get("content-type", "").startswith("application/json"):
        return None
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


@app.get("/api/home")
async def home_status(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    values = site_settings.load()
    data = await home_data.snapshot(UPSTREAMS, settings.llama_api_key, set(values["hidden"]),
                                    art_exclude=frozenset(values["trainer_art_exclude"]))
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.get("/api/home/thumb")
async def home_thumb(request: Request, src: str = ""):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await home_data.thumbnail(UPSTREAMS, src, lite=datasaver.wants_lite(request.headers, request.cookies))
    if data is None:
        return Response(status_code=404)
    # Outputs never change under the same job id + index.
    return Response(data, media_type="image/webp", headers={"Cache-Control": "private, max-age=604800, immutable", "Vary": "Cookie, Save-Data"})


_SERVICE_ACTIONS = {"start", "stop", "restart", "boot-on", "boot-off"}


async def _hostctl(method: str, path: str, body: dict | None = None) -> JSONResponse:
    if not settings.hostctl_secret:
        return JSONResponse({"error": "The services helper isn't configured (HOSTCTL_SECRET)."}, status_code=503)
    try:
        async with httpx.AsyncClient(timeout=200.0) as client:
            response = await client.request(method, f"{settings.hostctl_url}{path}", json=body,
                                            headers={"X-Hostctl-Secret": settings.hostctl_secret})
    except httpx.HTTPError:
        return JSONResponse({"error": "The services helper (bunny-hostctl) isn't reachable."}, status_code=503)
    try:
        data = response.json()
    except ValueError:
        data = {"error": f"Helper error {response.status_code}"}
    return JSONResponse(data, status_code=response.status_code, headers={"Cache-Control": "no-store"})


@app.get("/api/services")
async def list_services(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _hostctl("GET", "/items")


@app.post("/api/services/{item_id}/{action}")
async def service_action(request: Request, item_id: str, action: str):
    """Start/stop/restart and boot toggles. JSON + a custom header, so a
    cross-site form can't trigger it; the helper re-checks the allowlist."""
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.headers.get("x-bb-action") != "1" or action not in _SERVICE_ACTIONS or not item_id.replace("-", "").isalnum():
        return JSONResponse({"error": "bad request"}, status_code=400)
    body = await _json_body(request) or {}
    return await _hostctl("POST", f"/items/{item_id}/{action}", {"force": bool(body.get("force"))})


# REA folders: host directories mounted read-only into the REA container (hostctl/reafolders.py).
@app.get("/api/rea/folders")
async def rea_folders(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _hostctl("GET", "/rea/folders")


@app.get("/api/rea/browse")
async def rea_browse(request: Request, path: str = "", hidden: int = 0):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _hostctl("GET", f"/rea/browse?path={quote(path, safe='')}&hidden={1 if hidden else 0}")


@app.post("/api/rea/folders/{action}")
async def rea_folder_action(request: Request, action: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.headers.get("x-bb-action") != "1" or action not in {"add", "remove"}:
        return JSONResponse({"error": "bad request"}, status_code=400)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    return await _hostctl("POST", f"/rea/folders/{action}",
                          {"path": str(body.get("path") or ""), "slug": str(body.get("slug") or ""), "force": bool(body.get("force"))})


def _on_host_machine(request: Request) -> bool:
    """The browser runs on this computer: a loopback peer and not the Cloudflare tunnel
    (cloudflared also connects from 127.0.0.1, but adds its headers)."""
    peer = request.client.host if request.client else ""
    tunnel = request.headers.get("cf-ray") or request.headers.get("cf-connecting-ip")
    return peer in ("127.0.0.1", "::1") and not tunnel


@app.get("/api/rea/local")
async def rea_local(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"local": _on_host_machine(request)}, headers={"Cache-Control": "no-store"})


@app.post("/api/rea/open")
async def rea_open(request: Request):
    """Open a REA file's folder in this computer's file manager. Host-machine browsers only:
    from anywhere else it would pop a window up on a screen nobody is looking at."""
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.headers.get("x-bb-action") != "1":
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not _on_host_machine(request):
        return JSONResponse({"error": "Opening folders only works from a browser on this computer"}, status_code=403)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    return await _hostctl("POST", "/rea/open", {"path": str(body.get("path") or "")})


# REA mods: copying a built mod into the real game folder (hostctl/reamods.py). User-triggered only.
@app.get("/api/rea/mods/installed")
async def rea_mods_installed(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _hostctl("GET", "/rea/mods/installed")


@app.get("/api/rea/mods/{mod_id}/status")
async def rea_mod_status(request: Request, mod_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not re.fullmatch(r"[0-9a-f]{12}", mod_id):
        return JSONResponse({"error": "bad request"}, status_code=400)
    return await _hostctl("GET", f"/rea/mods/{mod_id}/status")


@app.post("/api/rea/mods/{mod_id}/{action}")
async def rea_mod_action(request: Request, mod_id: str, action: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.headers.get("x-bb-action") != "1" or action not in {"install", "uninstall", "launch"} or not re.fullmatch(r"[0-9a-f]{12}", mod_id):
        return JSONResponse({"error": "bad request"}, status_code=400)
    return await _hostctl("POST", f"/rea/mods/{mod_id}/{action}")


@app.get("/api/comfyui/profiles")
async def get_comfyui_profiles(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _hostctl("GET", "/comfyui/profiles")


@app.put("/api/comfyui/profiles")
async def put_comfyui_profiles(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.headers.get("x-bb-action") != "1":
        return JSONResponse({"error": "bad request"}, status_code=400)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    return await _hostctl("PUT", "/comfyui/profiles", body)


@app.get("/api/load-priority")
async def get_load_priority(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _hostctl("GET", "/load-priority")


@app.put("/api/load-priority")
async def put_load_priority(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.headers.get("x-bb-action") != "1":
        return JSONResponse({"error": "bad request"}, status_code=400)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    return await _hostctl("PUT", "/load-priority", body)


@app.get("/api/gpu-tune")
async def get_gpu_tune(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await _hostctl("GET", "/gpu-tune")


@app.api_route("/api/gpu-tune", methods=["PUT"])
@app.post("/api/gpu-tune/{action}")
async def change_gpu_tune(request: Request, action: str | None = None):
    """Profile edits (PUT) and apply/step/mark/resume actions: they change the GPU's clocks, so JSON + X-BB-Action."""
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.headers.get("x-bb-action") != "1":
        return JSONResponse({"error": "bad request"}, status_code=400)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    if action is None:
        return await _hostctl("PUT", "/gpu-tune", body)
    if action not in ("apply", "resume", "step", "mark", "reset-tuning"):
        return JSONResponse({"error": "unknown action"}, status_code=404)
    return await _hostctl("POST", f"/gpu-tune/{action}", body)


@app.get("/api/notifications")
async def get_notifications(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    catalog = [{"id": key, "label": spec["label"], "note": spec["note"],
                "events": [{"id": event, "label": label} for event, (label, _) in spec["events"].items()]}
               for key, spec in NOTIFY_CATALOG.items()]
    return {"catalog": catalog, "prefs": notifications.prefs(), "devices": notifications.devices(),
            "key": await asyncio.to_thread(notifications.public_key)}


@app.put("/api/notifications")
async def put_notifications(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    return {"prefs": notifications.save_prefs(body.get("prefs") or {})}


@app.post("/api/notifications/subscribe")
async def subscribe_notifications(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    try:
        notifications.subscribe(body.get("subscription"), str(body.get("label") or "Device"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"devices": notifications.devices()}


@app.post("/api/notifications/unsubscribe")
async def unsubscribe_notifications(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    notifications.unsubscribe(str(body.get("endpoint") or ""))
    return {"devices": notifications.devices()}


@app.post("/api/notifications/test")
async def test_notification(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request) or {}
    sent = await asyncio.to_thread(notifications.broadcast, {
        "title": "Rocsy notifications are on", "body": "Finished jobs from every studio will show up like this.",
        "tag": "bb-test", "url": "/Settings#notifications", "icon": "/static/icons/rocsy-v2-192.png",
        "badge": "/static/icons/rocsy-v2-badge-96.png"}, body.get("endpoint") or None)
    return {"sent": sent}


@app.post("/internal/notify")
async def internal_notify(request: Request):
    """Events from the apps (H3 Machine, Trainer) on loopback. Cloudflare's
    tunnel also arrives on loopback, so the shared secret is what counts;
    anything that came through the tunnel is refused outright."""
    secret = settings.queue_internal_secret
    supplied = request.headers.get("x-internal-secret", "")
    if (not secret or request.headers.get("cf-connecting-ip") or request.headers.get("cf-ray")
            or not hmac.compare_digest(supplied, secret)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "json_required"}, status_code=415)
    try:
        sent = await notifications.notify(
            str(body.get("service")), str(body.get("event")), str(body.get("title") or ""),
            str(body.get("body") or ""), str(body.get("url") or ""), str(body.get("tag") or ""),
            str(body.get("icon") or ""), str(body.get("badge") or ""))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"sent": sent}


# Extensionless on purpose: Cloudflare caches *.js for hours, and a stale
# service worker would never pick up changes.
@app.get("/notify-sw")
async def notify_service_worker() -> Response:
    return Response((BASE_DIR / "static" / "notify-sw.js").read_text(), media_type="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
async def manifest() -> Response:
    data = {"name": "Rocsy", "short_name": "Rocsy", "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#0f1418", "theme_color": "#0f1418",
            "icons": [{"src": "/static/icons/rocsy-v2-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/static/icons/rocsy-v2-512.png", "sizes": "512x512", "type": "image/png"},
                      {"src": "/static/icons/rocsy-v2-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
    return Response(json.dumps(data), media_type="application/manifest+json")


def _no_store(response: Response) -> Response:
    # The login page must never be cached (by the browser or any intermediary)
    # -- a cached copy could be replayed from history/back-forward cache after
    # logout, or served stale from a shared cache.
    response.headers["Cache-Control"] = "no-store"
    return response


def _safe_next(target: str) -> str:
    """A same-site path to return to after login, else "/". Browsers read "\\"
    as "/" in URLs, so "/\\evil.com" is as off-site as "//evil.com"."""
    if (not target.startswith("/") or target.startswith("//") or "\\" in target
            or any(ord(char) < 32 or ord(char) == 127 for char in target)):
        return "/"
    return target


@app.get("/login")
async def login_get(request: Request, next: str = "/"):
    next = next[:500]
    if not has_password():
        safe_next = _safe_next(next) if not next.startswith("/login") else "/"
        return _no_store(RedirectResponse(url=safe_next, status_code=302))
    response = templates.TemplateResponse(request, "login.html", {"next": next, "error": None, "app_version": app.version, "no_password": False})
    return _no_store(response)


@app.post("/login")
async def login_post(
    request: Request,
    # max_length bounds are hygiene against oversized payloads (a bot sending
    # a megabyte "password" to waste CPU/memory repeatedly), not an attempt to
    # restrict what characters a real password may contain -- the value is
    # never interpreted as anything but an opaque string (hmac.compare_digest
    # against the configured password, never SQL/shell/template-rendered), so
    # there is no injection surface to sanitize beyond that.
    password: str = Form("", max_length=200),
    next: str = Form("/", max_length=500),
    website: str = Form("", max_length=200),
):
    ip = _client_ip(request)
    # Counted as a failure up front, before the password check awaits (see begin_attempt).
    locked, remaining, now_locked, lockout_seconds, attempt_count = rate_limiter.begin_attempt(ip)
    if locked:
        minutes = max(1, int(remaining // 60) + 1)
        response = templates.TemplateResponse(
            request,
            "login.html",
            {
                "next": next,
                "error": f"Too many failed attempts. Try again in about {minutes} minute(s).",
                "app_version": app.version,
                "no_password": not has_password(),
            },
            status_code=429,
        )
        return _no_store(response)

    # Honeypot: a real browser never fills this field in (it's off-screen and
    # not `display:none`, so most naive scripted/bot form-fillers still
    # populate it). A non-empty value skips the real password check entirely
    # -- no point spending a hmac.compare_digest call on a request that's
    # already unambiguously not a human -- but still counts against the same
    # rate limiter as a wrong-password attempt, so it can't be used to probe
    # for the real password without also tripping the lockout.
    is_bot = bool(website.strip())
    if decoy.is_decoy_password(password):
        # Only readable from the decoy /.env: a scanner, not a typo. Same reply as a
        # wrong password, but every later request from this address is refused.
        _ban_decoy_user(request.scope, "honeypot: decoy password")
        is_bot = True
    # PBKDF2 (600k rounds) takes ~0.5 s: off the event loop, or every attempt stalls all proxying.
    if is_bot or not await asyncio.to_thread(check_password, password):
        await asyncio.sleep(rate_limiter.delay_for(attempt_count))
        if now_locked:
            minutes = max(1, lockout_seconds // 60)
            error = f"Too many failed attempts. Locked out for {minutes} minute(s)."
        else:
            error = "Incorrect password."
        log.warning(
            "Failed login attempt from %s (%s, %d/%d in window)%s",
            ip, "honeypot" if is_bot else "wrong password", attempt_count, MAX_ATTEMPTS,
            " - locked out" if now_locked else "",
        )
        response = templates.TemplateResponse(
            request, "login.html", {"next": next, "error": error, "app_version": app.version, "no_password": not has_password()}, status_code=401
        )
        return _no_store(response)

    rate_limiter.record_success(ip)
    log.info("Successful login from %s", ip)
    safe_next = _safe_next(next)
    response = RedirectResponse(url=safe_next, status_code=302)
    _set_session_cookie(response, request)
    return _no_store(response)


@app.get("/logout")
async def logout(request: Request):
    # A plain link (the Log out buttons), but another site linking or embedding
    # it mustn't sign you out: browsers mark those requests cross-site.
    if request.headers.get("sec-fetch-site") == "cross-site":
        return RedirectResponse(url="/", status_code=302)
    sid = _current_session(request)[1]
    if sid:
        connection_tracker.end(sid)
    response = RedirectResponse(url="/login", status_code=302)
    _clear_session_cookie(response)
    return response


def _unauthorized_response(request: Request, original_path: str) -> Response:
    """HTML navigations get redirected to /login; /api/* sub-paths (fetch/XHR)
    get a plain 401 JSON body instead, since a redirect there would just hand
    the caller a login page it can't do anything useful with. /Llama/* is
    always API traffic (Hermes agents, curl, an OpenAI SDK) -- never a
    browser navigation -- so it always gets the JSON form too."""
    if ("/api/" in original_path or original_path.rstrip("/").endswith("/api")
            or original_path.startswith(("/Llama/", "/v1/", "/upstream/"))):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return RedirectResponse(url=f"/login?next={quote(original_path)}", status_code=302)


def _has_valid_llama_bearer(request: Request) -> bool:
    """Alternative to a logged-in session, checked only for /Llama/* --
    headless callers (a Hermes agent on this machine or another) send
    `Authorization: Bearer <LLAMA_API_KEY>` per-request instead of doing the
    cookie-login flow a browser would."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and bool(token) and check_llama_api_key(token)


async def _handle_app_proxy(request: Request, app_name: str, path: str, *, strip_prefix: bool = False) -> Response:
    upstream_base = UPSTREAMS[app_name]
    full_path = request.url.path  # e.g. /Vid/api/generate -- includes prefix, not stripped

    authorized = _has_valid_session(request) or (app_name == "Llama" and _has_valid_llama_bearer(request))
    if not authorized:
        return _unauthorized_response(request, full_path)

    # GPU access is no longer gated here: every generate-trigger route accepts
    # the request immediately and the owning app queues it itself via the
    # bunny-lock/queue coordinator (waiting_for_gpu state + proceed callback).
    # See BunnyBytes/lock/app.py.
    extra_headers = None
    if app_name == "Llama":
        # llama-swap enforces its own apiKeys check independent of this
        # gateway's auth, so a browser session-cookie login (which carries no
        # bearer token at all) would otherwise 401 at the upstream even
        # though the gateway just authorized it. Inject the real key once
        # authorized=True above -- overwriting whatever Authorization header
        # (if any) the caller sent, since a bearer caller already proved they
        # know this same value to get past _has_valid_llama_bearer.
        extra_headers = {"authorization": f"Bearer {settings.llama_api_key}"}
    return await proxy_request(
        request, upstream_base, f"/{app_name}", extra_headers=extra_headers, strip_prefix=strip_prefix,
        head_inject=theme_link(app_name) + rocsy_bar_link() + data_saver_script() if app_name in THEMED_APPS else "",
        restyle_css=app_name in THEMED_APPS,
        html_transform=lambda html: favicons.apply(
            topbar.decorate(html, app_name) if app_name in topbar.APPS else html, app_name),
        adapt_reasoning=app_name == "Llama",
        lite_images=app_name != "Llama" and datasaver.wants_lite(request.headers, request.cookies),
    )


@app.api_route("/Vid/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_vid(request: Request, path: str):
    return await _handle_app_proxy(request, "Vid", path)


@app.api_route("/Mus/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_mus(request: Request, path: str):
    return await _handle_app_proxy(request, "Mus", path)


@app.api_route("/Krea/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_krea(request: Request, path: str):
    return await _handle_app_proxy(request, "Krea", path)


@app.api_route("/Trainer/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_trainer(request: Request, path: str):
    return await _handle_app_proxy(request, "Trainer", path)


@app.api_route("/Connections/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_connections(request: Request, path: str):
    return await _handle_app_proxy(request, "Connections", path)


@app.get("/Agent")
async def agent_root(request: Request):
    return RedirectResponse(url="/Agent/" + (f"?{request.url.query}" if request.url.query else ""), status_code=302)


@app.api_route("/Agent/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_agent(request: Request, path: str):
    return await _handle_app_proxy(request, "Agent", path)


@app.api_route("/Queue/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_queue(request: Request, path: str):
    return await _handle_app_proxy(request, "Queue", path)


# Content hash of the scripts/styles injected into llama-swap's UI, passed as
# ?v= so browsers never keep running a stale copy after a gateway update.
# Recomputed per page load so hot-patched static files bust the cache without a restart.
def _llama_ui_version() -> str:
    return hashlib.sha256(b"".join(
        (BASE_DIR / "static" / name).read_bytes()
        for name in ("llama-api-nav.js", "llama-params.js", "llama-params.css")
    )).hexdigest()[:12]


async def _llama_ui_index_response(request: Request) -> Response:
    if not (_has_valid_session(request) or _has_valid_llama_bearer(request)):
        return _unauthorized_response(request, request.url.path)
    try:
        upstream = await shared_client().get(
            f"{settings.llama_upstream}/ui/",
            headers={"authorization": f"Bearer {settings.llama_api_key}"},
            timeout=10.0,
        )
    except httpx.HTTPError:
        return Response("llama-swap isn't reachable. Start it from Settings → Services.", status_code=502,
                        media_type="text/plain", headers={"Cache-Control": "no-store"})
    html = upstream.text
    version = _llama_ui_version()
    if 'data-rocsy-addons="1"' in html:
        html = html.replace('src="/static/llama-api-nav.js"',
                            f'src="/static/llama-api-nav.js?v={version}"')
        html = html.replace("</head>", f'{theme_link("Llama")}{data_saver_script()}</head>')
    else:
        html = html.replace(
            "</head>",
            f'<script defer src="/static/llama-api-nav.js?v={version}"></script>{theme_link("Llama")}{data_saver_script()}</head>',
        )
    html = favicons.apply(html.encode(), "Llama").decode()
    # no-store + versioned script URL: a reload always picks up new injected code.
    return Response(content=html, status_code=upstream.status_code, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


# The BunnyBytes launcher opens this prefixed Docker route. Keep it before
# the wildcard /Llama proxy so the API-tab bootstrap is included.
@app.get("/Llama/ui/")
async def proxy_prefixed_llama_ui_index(request: Request):
    return await _llama_ui_index_response(request)


@app.api_route("/Llama/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_llama(request: Request, path: str):
    return await _handle_app_proxy(request, "Llama", path, strip_prefix=True)


@app.get("/llama-api")
async def llama_api_setup(request: Request):
    if not _has_valid_session(request):
        return _unauthorized_response(request, request.url.path)
    return templates.TemplateResponse(request=request, name="llama_api.html")


@app.get("/llama-api/config")
async def llama_api_config(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        response = await shared_client().get(
            f"{settings.llama_upstream}/v1/models",
            headers={"authorization": f"Bearer {settings.llama_api_key}"},
            timeout=10.0,
        )
        response.raise_for_status()
        models = [item["id"] for item in response.json().get("data", [])]
    except (httpx.HTTPError, ValueError, KeyError, AttributeError):
        return JSONResponse({"error": "upstream_unavailable", "message": "llama-swap isn't reachable."}, status_code=502)
    return JSONResponse({
        "base_url": f"{str(request.base_url).rstrip('/')}/Llama/v1",
        "api_key": settings.llama_api_key,
        "models": models,
    })


@app.get("/llama-benchmark/status/{model_id}")
async def llama_benchmark_status(request: Request, model_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model_id):
        return JSONResponse({"error": "invalid_model"}, status_code=400)
    return await _hostctl("GET", f"/llama-benchmark/status/{model_id}")


@app.post("/llama-benchmark/start")
async def llama_benchmark_start(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request)
    if not body or not re.fullmatch(r"[A-Za-z0-9._-]+", str(body.get("model") or "")):
        return JSONResponse({"error": "invalid_model"}, status_code=400)
    return await _hostctl("POST", "/llama-benchmark/start", {"model": body["model"]})


@app.get("/llama-benchmark/install/status/{model_id}")
async def llama_benchmark_install_status(request: Request, model_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model_id):
        return JSONResponse({"error": "invalid_model"}, status_code=400)
    return await _hostctl("GET", f"/llama-benchmark/install/status/{model_id}")


@app.post("/llama-benchmark/{action}")
async def llama_benchmark_manage(request: Request, action: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if action not in ("install", "uninstall"):
        return JSONResponse({"error": "invalid_action"}, status_code=404)
    body = await _json_body(request)
    if not body or not re.fullmatch(r"[A-Za-z0-9._-]+", str(body.get("model") or "")):
        return JSONResponse({"error": "invalid_model"}, status_code=400)
    return await _hostctl("POST", f"/llama-benchmark/{action}", {"model": body["model"]})


@app.get("/llama-hipblaslt/status/{model_id}")
async def llama_hipblaslt_status(request: Request, model_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model_id):
        return JSONResponse({"error": "invalid_model"}, status_code=400)
    return await _hostctl("GET", f"/llama-hipblaslt/status/{model_id}")


@app.post("/llama-hipblaslt/{action}")
async def llama_hipblaslt_action(request: Request, action: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if action not in ("start", "delete"):
        return JSONResponse({"error": "invalid_action"}, status_code=404)
    body = await _json_body(request)
    if not body or not re.fullmatch(r"[A-Za-z0-9._-]+", str(body.get("model") or "")):
        return JSONResponse({"error": "invalid_model"}, status_code=400)
    return await _hostctl("POST", f"/llama-hipblaslt/{action}", {"model": body["model"]})


@app.get("/strata/status")
async def strata_status(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            reply = await client.get("http://127.0.0.1:9101/health")
            return {"running": reply.status_code == 200}
    except httpx.HTTPError:
        return {"running": False}


@app.get("/Strata")
async def strata_redirect(request: Request):
    if not _has_valid_session(request):
        return _unauthorized_response(request, request.url.path)
    return RedirectResponse("/Strata/")


@app.api_route("/Strata/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def strata_proxy(request: Request, path: str):
    if not _has_valid_session(request):
        return _unauthorized_response(request, request.url.path)
    # Strata's app uses root-relative asset and API URLs. Rewrite its HTML
    # URLs under this authenticated prefix; keep the upstream bound to loopback.
    def prefix_html(body: bytes) -> bytes:
        # (?!/) leaves protocol-relative //host URLs alone.
        body = re.sub(rb'\b(href|src)="/(?!/)', rb'\1="/Strata/', body)
        return body.replace(b'<head>', b'<head><base href="/Strata/">')
    bootstrap = """<script>(function(){const f=window.fetch.bind(window);window.fetch=function(input,init){if(typeof input==='string'&&input.startsWith('/')&&!input.startsWith('/Strata/'))input='/Strata'+input;return f(input,init)}})();</script>"""
    return await proxy_request(request, "http://127.0.0.1:9101", "/Strata", strip_prefix=True,
                               head_inject=bootstrap, html_transform=prefix_html)


@app.get("/llama-params/models")
async def llama_params_models(request: Request):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    running: set[str] = set()
    with contextlib.suppress(httpx.HTTPError, ValueError):
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.llama_upstream}/running",
                headers={"authorization": f"Bearer {settings.llama_api_key}"},
            )
            running = {item.get("model") for item in response.json().get("running", [])}
    try:
        models = await asyncio.to_thread(llama_params.models, running)
    except (OSError, yaml.YAMLError) as error:
        return JSONResponse({"error": "config_unreadable", "message": str(error)}, status_code=500)
    return {"params": llama_params.spec(), "models": models, "backend": llama_params.backend_info()}


@app.put("/llama-params/models/{model_id}")
async def llama_params_save(request: Request, model_id: str):
    if not _has_valid_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json_body(request)  # JSON only: blocks cross-site form posts
    if body is None or not isinstance(body.get("params"), dict):
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    try:
        overrides = await asyncio.to_thread(llama_params.save, model_id, body["params"])
    except llama_params.ParamError as error:
        return JSONResponse({"error": "invalid_param", "message": str(error)}, status_code=400)
    except OSError as error:
        return JSONResponse({"error": "write_failed", "message": str(error)}, status_code=500)
    return {"overrides": overrides}


@app.api_route("/Hermes/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_hermes(request: Request, path: str):
    """Authenticated Hermes dashboard proxy with prefix-aware asset/API URLs."""
    if not _has_valid_session(request):
        return _unauthorized_response(request, request.url.path)
    return await proxy_request(
        request,
        settings.hermes_upstream,
        "/Hermes",
        extra_headers={"x-forwarded-prefix": "/Hermes"},
        strip_prefix=True,
        head_inject=theme_link("Hermes") + data_saver_script()
        + f'<script defer src="/static/hermes-home.js?v={_asset_version("hermes-home.js")}"></script>',
        html_transform=lambda html: favicons.apply(html, "Hermes"),
    )


@app.websocket("/Hermes/{path:path}")
async def proxy_hermes_websocket(client: WebSocket, path: str):
    """Bridge every Hermes dashboard WebSocket through the site session gate."""
    if not verify_session_cookie(client.cookies.get(settings.cookie_name)):
        await client.close(code=4401, reason="Unauthorized")
        return
    # Browsers always send Origin on a WebSocket handshake; only our own pages may open one.
    origin = urlsplit(client.headers.get("origin", ""))
    if origin.netloc.lower() != client.headers.get("host", "").lower() or origin.scheme not in ("http", "https"):
        await client.close(code=4403, reason="Forbidden")
        return
    if not path.startswith("api/"):
        await client.close(code=4404, reason="Not found")
        return

    import websockets

    requested = [item.strip() for item in client.headers.get("sec-websocket-protocol", "").split(",") if item.strip()]
    upstream_url = (
        settings.hermes_upstream.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        + "/"
        + path
    )
    query_string = client.scope.get("query_string", b"").decode("latin-1")
    if query_string:
        upstream_url += "?" + query_string
    try:
        async with websockets.connect(
            upstream_url,
            origin=settings.hermes_upstream,
            subprotocols=requested or None,
            additional_headers={"X-Forwarded-Prefix": "/Hermes"},
            max_size=None,
        ) as upstream:
            await client.accept(subprotocol=upstream.subprotocol)

            async def browser_to_hermes() -> None:
                while True:
                    message = await client.receive()
                    if message.get("type") == "websocket.disconnect":
                        return
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])

            async def hermes_to_browser() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await client.send_bytes(message)
                    else:
                        await client.send_text(message)

            tasks = [asyncio.create_task(browser_to_hermes()), asyncio.create_task(hermes_to_browser())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("Hermes WebSocket proxy closed: %s", exc)
        try:
            await client.close(code=1011, reason="Hermes backend unavailable")
        except RuntimeError:
            pass


# Hermes' Vite entry tags are rewritten to /Hermes/assets by the dashboard
# server, but URLs embedded inside the JavaScript chunks retain Vite's build-
# time absolute /assets base. Those lazy imports are only requested when the
# user opens particular tabs, which made the failure look intermittent: the
# request hit BunnyBytes itself, returned 404, and React lost the route tree.
# Keep this authenticated because the bundles expose the dashboard surface.
@app.api_route("/assets/{path:path}", methods=["GET", "HEAD"])
async def proxy_hermes_root_assets(request: Request, path: str):
    if not _has_valid_session(request):
        return _unauthorized_response(request, request.url.path)
    return await proxy_request(
        request,
        settings.hermes_upstream,
        "",
        strip_prefix=False,
    )


# llama-swap's UI bundle is built with an absolute "/ui/..." base (its own
# bundler has no idea it's being served under /Llama), so the browser
# requests e.g. /ui/assets/index-*.js against the domain root, not
# /Llama/ui/assets/*.js. Proxied here unstripped -- straight to "/ui/..." on
# the Llama upstream, which is exactly where llama-swap itself serves it --
# rather than fixed at the HTML level, since the JS bundle's own internal
# asset/API references make the same root-relative assumption.
@app.get("/ui/")
async def proxy_llama_ui_index(request: Request):
    return await _llama_ui_index_response(request)


@app.api_route("/ui/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_llama_ui_assets(request: Request, path: str):
    return await _handle_app_proxy(request, "Llama", path, strip_prefix=False)


# Starting a model from llama-swap's UI is a GET to /upstream/<model>/.
# The bundle uses this absolute path, so it needs the same authenticated,
# unstripped forwarding as the UI and admin API routes above/below.
@app.api_route("/upstream/{path:path}", methods=["GET", "HEAD"])
async def proxy_llama_model_load(request: Request, path: str):
    return await _handle_app_proxy(request, "Llama", path, strip_prefix=False)


# llama-swap's UI also polls its OpenAI-compatible API at absolute /v1/*
# paths after a model state change. Without this, the model can load but the
# tab keeps receiving BunnyBytes 404s and displays stale state.
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "OPTIONS", "HEAD"])
async def proxy_llama_root_v1(request: Request, path: str):
    return await _handle_app_proxy(request, "Llama", path, strip_prefix=False)


# Same root-relative-path issue as /ui above: the UI's JS calls llama-swap's
# own admin API (activity feed, metrics, hardware stats, /api/events SSE) at
# absolute "/api/..." paths. Claims the top-level "/api" namespace for
# llama-swap specifically -- fine today (no other service here uses a bare
# "/api" route; each app's own /api/* lives nested under its own /App
# prefix), but reconsider this if a future service ever wants top-level
# "/api" too.
@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_llama_api(request: Request, path: str):
    return await _handle_app_proxy(request, "Llama", path, strip_prefix=False)


if __name__ == "__main__":
    uvicorn.run("app:app", host=settings.gateway_host, port=settings.gateway_port)
