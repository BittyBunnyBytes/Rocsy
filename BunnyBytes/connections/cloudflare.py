"""Read-only Cloudflare Tunnel configuration and local connector health."""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

METRIC = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([0-9.]+)$", re.MULTILINE)


def _public_origin(value: object) -> str:
    """Show the destination without credentials, query parameters or fragments."""
    text = str(value or "")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        try:
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError:
            return "Invalid origin"
        return f"{parsed.scheme}://{parsed.hostname}{port}"
    if text.startswith("http_status:"):
        return text
    return "Other service"


def read_config(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return {"configured": False, "error": "Cloudflare tunnel config is missing", "tunnel_id": "", "routes": []}
    except (OSError, yaml.YAMLError):
        return {"configured": False, "error": "Cloudflare tunnel config could not be read", "tunnel_id": "", "routes": []}
    if not isinstance(data, dict):
        return {"configured": False, "error": "Cloudflare tunnel config is invalid", "tunnel_id": "", "routes": []}
    tunnel_id = str(data.get("tunnel") or "")
    ingress = data.get("ingress") or []
    routes = []
    if isinstance(ingress, list):
        for entry in ingress:
            if not isinstance(entry, dict):
                continue
            hostname = entry.get("hostname")
            if isinstance(hostname, str) and hostname:
                routes.append({"hostname": hostname, "origin": _public_origin(entry.get("service"))})
    return {"configured": bool(tunnel_id and routes), "error": "", "tunnel_id": tunnel_id, "routes": routes}


def metric_value(metrics: str, name: str) -> int | None:
    values = [float(value) for metric, value in METRIC.findall(metrics) if metric == name]
    return max(0, int(sum(values))) if values else None


def parse_connections(metrics: str) -> int | None:
    return metric_value(metrics, "cloudflared_tunnel_ha_connections")


def read_status(config_path: Path, metrics_url: str) -> dict:
    result = {**read_config(config_path), "connected": False, "active_connections": 0,
              "active_requests": None, "total_requests": None, "request_errors": None,
              "metrics_available": False}
    if not result["configured"]:
        return result
    base = metrics_url.rstrip("/")
    try:
        with urllib.request.urlopen(base + "/ready", timeout=2) as response:
            ready = response.status == 200
        with urllib.request.urlopen(base + "/metrics", timeout=2) as response:
            metrics = response.read(1024 * 1024).decode(errors="replace")
            count = parse_connections(metrics)
        result["active_requests"] = metric_value(metrics, "cloudflared_tunnel_concurrent_requests_per_tunnel")
        result["total_requests"] = metric_value(metrics, "cloudflared_tunnel_total_requests")
        result["request_errors"] = metric_value(metrics, "cloudflared_tunnel_request_errors")
        result["metrics_available"] = count is not None
        result["active_connections"] = count or 0
        result["connected"] = ready and count is not None and count > 0
    except (urllib.error.URLError, OSError, ValueError):
        result["error"] = "Local cloudflared metrics are unavailable"
    return result
