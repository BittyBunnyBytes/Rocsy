from __future__ import annotations

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cache_service import MAX_UPLOAD_BYTES
from config import settings

# Routes allowed a bigger body than MAX_BODY_MB (plus room for the multipart wrapper).
BODY_LIMITS = {"/Cache/api/files/upload": MAX_UPLOAD_BYTES + 1024 * 1024}

# Restrictive CSP: default-src 'self' only, no 'unsafe-inline' for scripts.
# Per the plan's design decision, any per-request config (e.g. APP_PREFIX) must
# be passed to frontends via data-* attributes read by external JS files, not
# inline <script> blocks -- that's what keeps this policy tight. style-src
# allows 'unsafe-inline' only because the existing dark-theme frontends
# (H3 Machine et al.) use some inline style attributes; scripts do not get the
# same allowance.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class SecurityHeadersMiddleware:
    """Adds the standard hardening headers to every response, and enforces a
    request body size cap on proxied requests before they reach the proxy.

    Pure ASGI (not BaseHTTPMiddleware): only the response-start message is
    touched, so streamed bodies (videos, SSE) pass through without an extra
    task and memory stream per response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        content_length = Headers(scope=scope).get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = None
            limit = BODY_LIMITS.get(path, settings.max_body_bytes)
            if declared is not None and declared > limit:
                response: Response = JSONResponse(
                    {
                        "error": "payload_too_large",
                        "message": f"Request body exceeds the {limit // (1024 * 1024)}MB limit.",
                    },
                    status_code=413,
                )
                self._apply_headers(response.headers, path)
                return await response(scope, receive, send)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                self._apply_headers(MutableHeaders(scope=message), path)
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _apply_headers(headers: MutableHeaders, path: str = "") -> None:
        for key, value in SECURITY_HEADERS.items():
            headers[key] = value
        if path == "/api/developer/installer-ui":
            headers["X-Frame-Options"] = "SAMEORIGIN"
            headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
            )
        if path.startswith("/Code/preview/"):
            # Static project pages run in an opaque origin, even when opened directly.
            headers["X-Frame-Options"] = "SAMEORIGIN"
            # ES modules are fetched from the iframe's opaque (null) origin.
            # The scoped, signed preview URL authorizes only this project.
            headers["Access-Control-Allow-Origin"] = "null"
            headers["Content-Security-Policy"] = (
                "sandbox allow-scripts; default-src 'self' data: blob:; "
                "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "img-src 'self' data: blob:; media-src 'self' blob:; "
                "font-src 'self' https://fonts.gstatic.com; connect-src 'none'; "
                "form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
            )
        if path == "/Strata" or path.startswith("/Strata/"):
            headers["X-Frame-Options"] = "SAMEORIGIN"
            headers["Content-Security-Policy"] = (
                "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "connect-src 'self' ws: wss:; frame-ancestors 'self'; base-uri 'self'"
            )
        if path == "/Hermes" or path.startswith("/Hermes/"):
            # Hermes injects a small per-process session-token/base-path bootstrap into
            # index.html and uses blob workers for terminal/editor features. Keep this
            # relaxation scoped to the already-authenticated Hermes surface.
            headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "media-src 'self' blob:; connect-src 'self' ws: wss:; "
                "worker-src 'self' blob:; font-src 'self' data:; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
