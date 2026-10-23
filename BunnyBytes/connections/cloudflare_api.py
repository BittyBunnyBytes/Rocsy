"""Small Cloudflare API adapter for creating a local tunnel and DNS routes."""
from __future__ import annotations

import base64
import json
import re
import secrets
import urllib.error
import urllib.request
import uuid

from tunnels import TunnelError

HEX32 = re.compile(r"^[a-fA-F0-9]{32}$")
API = "https://api.cloudflare.com/client/v4"


def _call(token: str, method: str, path: str, body: dict) -> dict:
    request = urllib.request.Request(API + path, data=json.dumps(body).encode(), method=method,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read())
            message = "; ".join(str(error.get("message", "")) for error in data.get("errors", []))
        except (ValueError, TypeError):
            message = "Cloudflare returned an error"
        raise TunnelError(message[:300] or f"Cloudflare returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise TunnelError(f"Could not reach Cloudflare: {exc.reason}") from None
    if not data.get("success"):
        message = "; ".join(str(error.get("message", "")) for error in data.get("errors", []))
        raise TunnelError(message[:300] or "Cloudflare rejected the request")
    return data.get("result") or {}


def create_local_tunnel(token: str, account_id: str, name: str) -> tuple[str, bytes]:
    if not token.strip() or not HEX32.fullmatch(account_id):
        raise TunnelError("Cloudflare API token and 32-character account ID are required")
    secret = base64.b64encode(secrets.token_bytes(32)).decode()
    result = _call(token, "POST", f"/accounts/{account_id}/cfd_tunnel",
                   {"name": name, "config_src": "local", "tunnel_secret": secret})
    try:
        tunnel_id = str(uuid.UUID(str(result["id"])))
        account_tag = str(result.get("account_tag") or account_id)
    except (KeyError, ValueError, TypeError) as exc:
        raise TunnelError("Cloudflare created a tunnel but returned no usable ID") from exc
    credentials = {"AccountTag": account_tag, "TunnelID": tunnel_id, "TunnelSecret": secret}
    return tunnel_id, (json.dumps(credentials) + "\n").encode()


def create_dns_routes(token: str, zone_id: str, tunnel_id: str, routes: list[dict]) -> list[str]:
    if not HEX32.fullmatch(zone_id):
        raise TunnelError("A 32-character zone ID is required for DNS records")
    errors = []
    for route in routes:
        try:
            _call(token, "POST", f"/zones/{zone_id}/dns_records",
                  {"type": "CNAME", "name": route["hostname"],
                   "content": f"{tunnel_id}.cfargotunnel.com", "proxied": True, "ttl": 1})
        except TunnelError as exc:
            errors.append(f"{route['hostname']}: {exc}")
    return errors
