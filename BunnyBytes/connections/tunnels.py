"""Manage locally configured cloudflared processes without Docker access."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from cloudflare import read_config, read_status

UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
HOSTNAME = re.compile(r"^(?:\*\.)?[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$" )
MAX_TUNNELS = 8
MAX_ROUTES = 20


class TunnelError(ValueError):
    pass


def validate_routes(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value or len(value) > MAX_ROUTES:
        raise TunnelError(f"Provide 1 to {MAX_ROUTES} hostname routes")
    result = []
    seen = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise TunnelError("Every route needs a hostname and origin")
        hostname = str(entry.get("hostname") or "").strip().lower()
        origin = str(entry.get("origin") or "").strip()
        if not HOSTNAME.fullmatch(hostname) or "." not in hostname:
            raise TunnelError(f"Invalid hostname: {hostname[:100]}")
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise TunnelError(f"Use an HTTP or HTTPS origin without credentials or query parameters for {hostname}")
        if hostname in seen:
            raise TunnelError(f"Duplicate hostname: {hostname}")
        seen.add(hostname)
        result.append({"hostname": hostname, "origin": origin.rstrip("/")})
    return result


def validate_credential(raw: bytes) -> tuple[str, dict]:
    if len(raw) > 64 * 1024:
        raise TunnelError("Tunnel credential is too large")
    try:
        data = json.loads(raw)
        tunnel_id = str(uuid.UUID(str(data["TunnelID"])))
        if not data.get("AccountTag") or not data.get("TunnelSecret"):
            raise ValueError("missing fields")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise TunnelError("Credential JSON needs AccountTag, TunnelID and TunnelSecret") from exc
    return tunnel_id, data


def _write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    path.chmod(0o600)


class TunnelManager:
    def __init__(self, data_dir: Path, existing_config: Path, existing_credential: Path, binary: str = "cloudflared"):
        self.data_dir = data_dir
        self.registry_path = data_dir / "tunnels.json"
        self.existing_config = existing_config
        self.existing_credential = existing_credential
        self.binary = binary
        self.processes: dict[str, subprocess.Popen] = {}
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.records = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            records = json.loads(self.registry_path.read_text())
            if isinstance(records, dict):
                return records
        except (OSError, ValueError):
            pass
        existing = read_config(self.existing_config)
        if not existing["configured"]:
            return {}
        tunnel_id = existing["tunnel_id"]
        records = {tunnel_id: {
            "id": tunnel_id, "name": "byte", "routes": existing["routes"],
            "credential": str(self.existing_credential), "enabled": False,
            "managed": False, "metrics_port": 20241,
        }}
        self.records = records
        self._save()
        return records

    def _save(self) -> None:
        _write_private(self.registry_path, (json.dumps(self.records, indent=2, sort_keys=True) + "\n").encode())

    def _config_path(self, tunnel_id: str) -> Path:
        return self.data_dir / "tunnels" / f"{tunnel_id}.yml"

    def _write_config(self, record: dict) -> Path:
        config = {"tunnel": record["id"], "credentials-file": record["credential"],
                  "ingress": [{"hostname": route["hostname"], "service": route["origin"]} for route in record["routes"]]
                  + [{"service": "http_status:404"}]}
        path = self._config_path(record["id"])
        candidate = path.with_suffix(".candidate.yml")
        _write_private(candidate, yaml.safe_dump(config, sort_keys=False).encode())
        try:
            validation = subprocess.run([self.binary, "--config", str(candidate), "tunnel", "ingress", "validate"],
                                        capture_output=True, timeout=10, check=False)
            if validation.returncode:
                raise TunnelError("Cloudflared rejected the ingress routes: " +
                                  validation.stderr.decode(errors="replace").strip()[-300:])
            candidate.replace(path)
        finally:
            candidate.unlink(missing_ok=True)
        return path

    def _process_running(self, tunnel_id: str) -> bool:
        process = self.processes.get(tunnel_id)
        return process is not None and process.poll() is None

    def _start(self, record: dict) -> None:
        if self._process_running(record["id"]):
            return
        config = self._write_config(record)
        log_path = self.data_dir / "tunnels" / f"{record['id']}.log"
        environment = {**os.environ, "TUNNEL_METRICS": f"127.0.0.1:{record['metrics_port']}"}
        with log_path.open("a", buffering=1) as logfile:
            self.processes[record["id"]] = subprocess.Popen(
                [self.binary, "--config", str(config), "tunnel", "run"],
                stdout=logfile, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=environment, close_fds=True,
            )

    async def _stop(self, tunnel_id: str) -> None:
        process = self.processes.pop(tunnel_id, None)
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=12)
        except asyncio.TimeoutError:
            process.kill()
            await asyncio.to_thread(process.wait)

    async def supervise(self) -> None:
        while True:
            async with self.lock:
                for record in self.records.values():
                    if record.get("managed") and record.get("enabled") and not self._process_running(record["id"]):
                        try:
                            self._start(record)
                        except OSError:
                            pass
            await asyncio.sleep(10)

    async def shutdown(self) -> None:
        async with self.lock:
            for tunnel_id in list(self.processes):
                await self._stop(tunnel_id)

    def list_status(self) -> list[dict]:
        result = []
        for record in self.records.values():
            managed = bool(record.get("managed"))
            config = self._config_path(record["id"]) if managed else self.existing_config
            metrics = read_status(config, f"http://127.0.0.1:{record['metrics_port']}")
            result.append({
                "id": record["id"], "name": record["name"], "routes": record["routes"],
                "managed": managed, "enabled": bool(record.get("enabled")),
                "process_running": self._process_running(record["id"]) if managed else None,
                "metrics_port": record["metrics_port"],
                "connected": metrics["connected"], "active_connections": metrics["active_connections"],
                "total_requests": metrics["total_requests"], "request_errors": metrics["request_errors"],
                "error": metrics["error"],
            })
        return result

    async def add(self, name: str, routes: object, credential: bytes) -> dict:
        clean_name = name.strip()[:80]
        if not clean_name:
            raise TunnelError("Give the tunnel a name")
        clean_routes = validate_routes(routes)
        tunnel_id, data = validate_credential(credential)
        async with self.lock:
            if len(self.records) >= MAX_TUNNELS:
                raise TunnelError(f"At most {MAX_TUNNELS} tunnels are supported")
            if tunnel_id in self.records:
                raise TunnelError("This tunnel is already configured")
            port = next((p for p in range(20241, 20241 + MAX_TUNNELS) if all(r["metrics_port"] != p for r in self.records.values())), None)
            if port is None:
                raise TunnelError("No metrics port is available")
            cred_path = self.data_dir / "tunnels" / f"{tunnel_id}.json"
            _write_private(cred_path, (json.dumps(data) + "\n").encode())
            record = {"id": tunnel_id, "name": clean_name, "routes": clean_routes,
                      "credential": str(cred_path), "enabled": True, "managed": True, "metrics_port": port}
            self._write_config(record)
            self.records[tunnel_id] = record
            self._save()
            self._start(record)
            return record

    async def update_routes(self, tunnel_id: str, routes: object) -> None:
        clean = validate_routes(routes)
        async with self.lock:
            record = self.records.get(tunnel_id)
            if not record or not record.get("managed"):
                raise TunnelError("This tunnel is not managed by Connections")
            previous = record["routes"]
            record["routes"] = clean
            try:
                self._write_config(record)
            except (TunnelError, OSError):
                record["routes"] = previous
                raise
            self._save()
            try:
                if record["enabled"]:
                    await self._stop(tunnel_id)
                    self._start(record)
            except OSError:
                record["routes"] = previous
                self._write_config(record)
                self._save()
                self._start(record)
                raise

    async def set_enabled(self, tunnel_id: str, enabled: bool) -> None:
        async with self.lock:
            record = self.records.get(tunnel_id)
            if not record or not record.get("managed"):
                raise TunnelError("This tunnel is not managed by Connections")
            record["enabled"] = enabled
            self._save()
            if enabled:
                self._start(record)
            else:
                await self._stop(tunnel_id)

    async def restart(self, tunnel_id: str) -> None:
        async with self.lock:
            record = self.records.get(tunnel_id)
            if not record or not record.get("managed"):
                raise TunnelError("This tunnel is not managed by Connections")
            await self._stop(tunnel_id)
            record["enabled"] = True
            self._save()
            self._start(record)

    async def adopt_existing(self, tunnel_id: str) -> None:
        async with self.lock:
            record = self.records.get(tunnel_id)
            if not record or record.get("managed") or not self.existing_credential.is_file():
                raise TunnelError("Existing tunnel is unavailable for management")
            record["managed"] = True
            record["enabled"] = True
            self._write_config(record)
            self._save()
            self._start(record)
