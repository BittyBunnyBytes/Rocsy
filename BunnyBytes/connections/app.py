"""Connections: web control and supervision for the existing Telegram watcher."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, suppress
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from cloudflare import read_status as read_cloudflare_status
from tunnels import TunnelError, TunnelManager, validate_routes
from cloudflare_api import HEX32, create_local_tunnel, create_dns_routes
import posts as post_schedule

PREFIX = os.getenv("CONNECTIONS_MOUNT_PREFIX", "/Connections").rstrip("/")
DATA = Path(os.getenv("CONNECTIONS_DATA_DIR", "/data"))
SCRIPT = Path(os.getenv("CONNECTIONS_SCRIPT", "/app/telegram_comfyui_output.py"))
CONFIG = Path(os.getenv("CONNECTIONS_CONFIG", "/data/telegram-config.json"))
LEGACY_CONFIG = Path(os.getenv("CONNECTIONS_LEGACY_CONFIG", "/run/telegram/config.json"))
_COMFY_OUTPUT = Path(os.path.expanduser(os.getenv("ROCSY_COMFY_ROOT", "~/ComfyUI"))) / "output"
OUTPUT = Path(os.getenv("CONNECTIONS_OUTPUT_DIR") or _COMFY_OUTPUT)
H3_OUTPUT = Path(os.getenv("CONNECTIONS_H3_OUTPUT_DIR") or _COMFY_OUTPUT)
COMFY_URL = os.getenv("CONNECTIONS_COMFY_URL", "http://127.0.0.1:8189")
PORT = int(os.getenv("CONNECTIONS_PORT", "8800"))
STATE = DATA / "telegram-state.json"
H3_STATE = DATA / "telegram-state-h3.json"
LOG = DATA / "watcher.log"
SETTINGS = DATA / "settings.json"
CLOUDFLARE_CONFIG = Path(os.getenv("CONNECTIONS_CLOUDFLARE_CONFIG", "/run/cloudflared/config.yml"))
CLOUDFLARE_METRICS = os.getenv("CONNECTIONS_CLOUDFLARE_METRICS", "http://127.0.0.1:20241")
CLOUDFLARE_CREDENTIAL = Path(os.getenv("CONNECTIONS_CLOUDFLARE_CREDENTIAL", "/run/cloudflared/byte.json"))
HOSTCTL_URL = os.getenv("HOSTCTL_URL", "http://127.0.0.1:8899").rstrip("/")
HOSTCTL_SECRET = os.getenv("HOSTCTL_SECRET", "")
# Website accounts the Master Agent uses (any site). <id>.json = the login (owner-only); Hermes reads
# them at /opt/data/rocsy-accounts. A scouting run writes sites/<id>.json (what the site needs: login,
# fields, file rules, limits, steps) and scripts/<id>_upload.* (the automation later runs use).
ACCOUNTS = Path(os.getenv("CONNECTIONS_ACCOUNTS_DIR", "/accounts"))
HERMES_ACCOUNTS = os.getenv("CONNECTIONS_ACCOUNTS_HERMES_DIR", "/opt/data/rocsy-accounts")
MAX_MEDIA = 48 * 1024 * 1024        # under the gateway's 50 MB request limit
AGENT_URL = os.getenv("CONNECTIONS_AGENT_URL", "http://127.0.0.1:8900").rstrip("/")
BROWSERS = ("headed", "headless")
ACCOUNT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def p(path: str) -> str:
    return PREFIX + path


if not CONFIG.exists() and LEGACY_CONFIG.is_file():
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LEGACY_CONFIG, CONFIG)
    os.chmod(CONFIG, 0o600)


class Watcher:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.task: asyncio.Task | None = None
        self.enabled = os.getenv("CONNECTIONS_AUTOSTART", "true").lower() in {"1", "true", "yes"}
        self.last_error = ""
        try:
            saved = json.loads(SETTINGS.read_text())
            self.enabled = bool(saved["enabled"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def configured(self) -> bool:
        if not CONFIG.is_file():
            return False
        try:
            data = json.loads(CONFIG.read_text())
            return bool(data.get("bot_token") and data.get("chat_id"))
        except (OSError, ValueError, TypeError):
            return False

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def status(self) -> dict:
        try:
            with LOG.open(errors="replace") as stream:
                lines = list(deque(stream, maxlen=30))
        except OSError:
            lines = []
        return {
            "enabled": self.enabled,
            "running": self.running(),
            "configured": self.configured(),
            "outputs_available": OUTPUT.is_dir() and H3_OUTPUT.is_dir(),
            "pid": self.process.pid if self.running() else None,
            "last_error": self.last_error,
            "recent_log": lines,
        }

    def save_enabled(self) -> None:
        DATA.mkdir(parents=True, exist_ok=True)
        temp = SETTINGS.with_suffix(".tmp")
        temp.write_text(json.dumps({"enabled": self.enabled}) + "\n")
        temp.replace(SETTINGS)

    def start(self) -> None:
        if self.running():
            return
        if not self.configured():
            self.last_error = "Telegram bot configuration is missing."
            return
        if not OUTPUT.is_dir() or not H3_OUTPUT.is_dir():
            self.last_error = "A ComfyUI output directory is unavailable."
            return
        DATA.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(SCRIPT), "--config", str(CONFIG), "--state", str(STATE),
                   "--h3-state", str(H3_STATE), "--output-dir", str(OUTPUT), "--h3-output-dir", str(H3_OUTPUT),
                   "--comfy-url", COMFY_URL, "--h3-comfy-url", COMFY_URL]
        try:
            with LOG.open("a", buffering=1) as logfile:
                self.process = subprocess.Popen(command, stdout=logfile, stderr=subprocess.STDOUT,
                                                stdin=subprocess.DEVNULL, close_fds=True)
        except OSError as exc:
            self.last_error = f"Could not start watcher: {exc}"
            self.process = None
            return
        self.last_error = ""

    async def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            self.process = None
            return
        process.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=10)
        except asyncio.TimeoutError:
            process.kill()
            await asyncio.to_thread(process.wait)
        self.process = None

    async def supervise(self) -> None:
        while True:
            if self.enabled:
                if not self.running():
                    if self.process is not None:
                        self.last_error = f"Watcher exited with code {self.process.returncode}; restarting."
                    self.start()
            elif self.running():
                await self.stop()
            await asyncio.sleep(10)


watcher = Watcher()
tunnels = TunnelManager(DATA, CLOUDFLARE_CONFIG, CLOUDFLARE_CREDENTIAL)
posts = post_schedule.PostStore(DATA, ACCOUNTS, HERMES_ACCOUNTS)
batches = post_schedule.BatchStore(DATA)
post_lock = asyncio.Lock()        # the scheduler and the page's edits read-modify-write posts.json


def read_telegram_config() -> dict:
    try:
        data = json.loads(CONFIG.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_telegram_config(data: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=CONFIG.parent, prefix=".telegram-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.chmod(temp, 0o600)
        os.replace(temp, CONFIG)
    except Exception:
        with suppress(OSError):
            os.unlink(temp)
        raise


@asynccontextmanager
async def lifespan(_: FastAPI):
    with suppress(Exception):
        await asyncio.to_thread(migrate_accounts)
    watcher.task = asyncio.create_task(watcher.supervise())
    tunnels.task = asyncio.create_task(tunnels.supervise())
    post_task = asyncio.create_task(post_scheduler())
    try:
        yield
    finally:
        watcher.task.cancel()
        tunnels.task.cancel()
        post_task.cancel()
        with suppress(asyncio.CancelledError):
            await post_task
        with suppress(asyncio.CancelledError):
            await watcher.task
        with suppress(asyncio.CancelledError):
            await tunnels.task
        await tunnels.shutdown()
        await watcher.stop()


app = FastAPI(title="Connections", version="1.0.0", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get(PREFIX)
async def root():
    return RedirectResponse(p("/"), status_code=307)


@app.get(p("/"), response_class=HTMLResponse)
async def home():
    return HTMLResponse(page_html(), headers={"Cache-Control": "no-store"})


@app.get(p("/app.js"))
async def page_script():
    return FileResponse(STATIC / "connections.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get(p("/app.css"))
async def page_style():
    return FileResponse(STATIC / "connections.css", media_type="text/css", headers={"Cache-Control": "no-cache"})


@app.get(p("/api/status"))
async def status():
    return watcher.status()


@app.get(p("/api/telegram/config"))
async def telegram_config():
    data = read_telegram_config()
    token = str(data.get("bot_token") or "")
    return {"configured": bool(token and data.get("chat_id")), "chat_id": str(data.get("chat_id") or ""),
            "token_hint": ("••••" + token[-4:]) if token else ""}


@app.put(p("/api/telegram/config"))
async def save_telegram_config(request: Request, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON object required")
    current = read_telegram_config()
    token = str(body.get("bot_token") or "").strip() or str(current.get("bot_token") or "")
    chat_id = str(body.get("chat_id") or "").strip()
    if not token or ":" not in token or any(char.isspace() for char in token):
        raise HTTPException(status_code=422, detail="Enter a valid BotFather bot token")
    if not chat_id or len(chat_id) > 80 or any(char.isspace() for char in chat_id):
        raise HTTPException(status_code=422, detail="Enter a valid Telegram chat ID")
    await watcher.stop()
    write_telegram_config({**current, "bot_token": token, "chat_id": chat_id})
    watcher.last_error = ""
    if watcher.enabled:
        watcher.start()
    return {"configured": True, "chat_id": chat_id, "token_hint": "••••" + token[-4:]}


def hermes_link_request(method: str, model: str = "", action: str = "") -> dict:
    if not HOSTCTL_SECRET:
        raise HTTPException(status_code=503, detail="Host connection helper is unavailable")
    data = json.dumps({"model": model}).encode() if method == "POST" else None
    request = urllib.request.Request(HOSTCTL_URL + "/connections/hermes" + ("/disconnect" if action == "disconnect" else ""), data=data, method=method,
                                     headers={"X-Hostctl-Secret": HOSTCTL_SECRET, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("error", "Hermes connection failed")
        except (ValueError, OSError):
            detail = "Hermes connection failed"
        raise HTTPException(status_code=exc.code, detail=detail) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Host connection helper is unavailable") from exc


@app.get(p("/api/hermes/status"))
async def hermes_link_status():
    return await asyncio.to_thread(hermes_link_request, "GET")


@app.post(p("/api/hermes/connect"))
async def hermes_link_connect(request: Request, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("model"), str):
        raise HTTPException(status_code=422, detail="Choose a llama-swap model")
    return await asyncio.to_thread(hermes_link_request, "POST", body["model"])


@app.post(p("/api/hermes/disconnect"))
async def hermes_link_disconnect(x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    return await asyncio.to_thread(hermes_link_request, "POST", "", "disconnect")


@app.get(p("/api/cloudflare"))
async def cloudflare_status():
    existing = next((item for item in tunnels.records.values() if item.get("name") == "byte"), None)
    config = tunnels._config_path(existing["id"]) if existing and existing.get("managed") else CLOUDFLARE_CONFIG
    return await asyncio.to_thread(read_cloudflare_status, config, CLOUDFLARE_METRICS)


@app.get(p("/api/tunnels"))
async def list_tunnels():
    return {"tunnels": await asyncio.to_thread(tunnels.list_status)}


def parse_routes_json(raw: str) -> list[dict[str, str]]:
    try:
        return validate_routes(json.loads(raw))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(p("/api/tunnels/import"), status_code=201)
async def import_tunnel(name: str = Form(...), routes: str = Form(...), credential: UploadFile = File(...),
                        x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    clean_routes = parse_routes_json(routes)
    raw = await credential.read(64 * 1024 + 1)
    await credential.close()
    try:
        record = await tunnels.add(name, clean_routes, raw)
        return {"id": record["id"], "dns": "Configure each hostname as a proxied CNAME to <tunnel-id>.cfargotunnel.com"}
    except TunnelError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(p("/api/tunnels/create"), status_code=201)
async def create_tunnel(request: Request, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON object required")
    clean_routes = parse_routes_json(json.dumps(body.get("routes")))
    name = str(body.get("name") or "").strip()[:80]
    token = str(body.get("api_token") or "")
    account_id = str(body.get("account_id") or "")
    zone_id = str(body.get("zone_id") or "")
    if not name:
        raise HTTPException(status_code=422, detail="Give the tunnel a name")
    if not HEX32.fullmatch(account_id) or not HEX32.fullmatch(zone_id):
        raise HTTPException(status_code=422, detail="Account ID and zone ID must be 32 hexadecimal characters")
    if len(tunnels.records) >= 8:
        raise HTTPException(status_code=409, detail="Tunnel limit reached")
    try:
        tunnel_id, credential = await asyncio.to_thread(create_local_tunnel, token, account_id, name)
        record = await tunnels.add(name, clean_routes, credential)
        dns_errors = await asyncio.to_thread(create_dns_routes, token, zone_id, tunnel_id, clean_routes)
        return {"id": record["id"], "dns_errors": dns_errors}
    except TunnelError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put(p("/api/tunnels/{tunnel_id}/routes"))
async def edit_tunnel_routes(tunnel_id: str, request: Request, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    body = await request.json()
    try:
        clean_routes = validate_routes(body.get("routes") if isinstance(body, dict) else None)
        await tunnels.update_routes(tunnel_id, clean_routes)
        return {"ok": True}
    except TunnelError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(p("/api/tunnels/{tunnel_id}/{action}"))
async def control_tunnel(tunnel_id: str, action: str, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    try:
        if action == "start":
            await tunnels.set_enabled(tunnel_id, True)
        elif action == "stop":
            await tunnels.set_enabled(tunnel_id, False)
        elif action == "restart":
            await tunnels.restart(tunnel_id)
        else:
            raise HTTPException(status_code=404, detail="Unknown tunnel action")
        return {"ok": True}
    except TunnelError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except Exception:
        with suppress(OSError):
            os.unlink(temp)
        raise


# A website (websites/<site>.json: name, url, notes, scout) is worked out once: its guide (sites/<site>.json)
# and scripts (scripts/<site>_upload.*, scripts/<site>_actions/) are shared by every account on it.
# An account (<id>.json) is one login on a website (site = <site>).
WEBSITES = ACCOUNTS / "websites"
AUTOMATION = ACCOUNTS / "automation.json"     # the agent reads it too: run / write saved site scripts
RUNNING = ("starting", "waiting_for_gpu", "thinking", "rendering")


def read_account(account_id: str) -> dict:
    return read_json(ACCOUNTS / f"{account_id}.json")


def write_account(account_id: str, data: dict) -> None:
    write_json(ACCOUNTS / f"{account_id}.json", data)


def read_website(site: str) -> dict:
    return read_json(WEBSITES / f"{site}.json")


def write_website(site: str, data: dict) -> None:
    write_json(WEBSITES / f"{site}.json", data)


def site_of(account_id: str, data: dict | None = None) -> str:
    site = str((data if data is not None else read_account(account_id)).get("site") or "")
    return site if ACCOUNT_ID.fullmatch(site) else account_id


def full_account(account_id: str) -> dict:
    """The login with its website's name and url (what the post prompts use)."""
    data = read_account(account_id)
    if not data:
        return {}
    w = read_website(site_of(account_id, data))
    return {**data, "url": w.get("url") or data.get("url") or "", "site_name": w.get("name") or ""}


def account_script(site: str) -> Path | None:
    return next(iter(sorted((ACCOUNTS / "scripts").glob(f"{site}_upload.*"))), None)


def action_scripts(site: str) -> list[Path]:
    folder = ACCOUNTS / "scripts" / f"{site}_actions"
    return sorted(p for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []


def read_automation() -> dict:
    data = read_json(AUTOMATION)
    return {"use_scripts": data.get("use_scripts") is not False, "write_scripts": data.get("write_scripts") is not False}


def _text(value, limit: int = 600) -> str:
    return " ".join(str(value or "").split())[:limit]


def read_guide(site: str) -> dict | None:
    """The site guide a scouting run wrote, trimmed to what the page shows (all plain text)."""
    path = ACCOUNTS / "sites" / f"{site}.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    login = raw.get("login") if isinstance(raw.get("login"), dict) else {"how": raw.get("login")}
    files = raw.get("files") if isinstance(raw.get("files"), dict) else {"notes": raw.get("files")}
    fields = [{"name": _text(f.get("name"), 60), "required": bool(f.get("required")), "notes": _text(f.get("notes"), 200)}
              for f in raw.get("fields") or [] if isinstance(f, dict) and f.get("name")][:30]
    return {"summary": _text(raw.get("summary"), 400), "login": {k: _text(v, 300) for k, v in login.items() if v not in (None, "")},
            "fields": fields, "files": {k: _text(", ".join(v) if isinstance(v, list) else v, 200) for k, v in files.items() if v not in (None, "", [])},
            "limits": _text(raw.get("limits"), 400), "steps": [_text(x, 240) for x in raw.get("steps") or []][:20],
            "warnings": [_text(x, 240) for x in raw.get("warnings") or []][:8], "updated": path.stat().st_mtime}


def agent_session_state(sid: str) -> str:
    with suppress(Exception):
        with urllib.request.urlopen(f"{AGENT_URL}/Agent/api/sessions/{sid}?since=1000000000", timeout=3) as response:
            return str(json.load(response).get("state") or "")
    return ""


def website_state(site: str, data: dict, guide: dict | None, script: Path | None) -> str:
    scout = data.get("scout") or {}
    scouting = bool(scout.get("session")) and (not guide or guide["updated"] < scout.get("t", 0)) \
        and agent_session_state(scout["session"]) in RUNNING
    return "scouting" if scouting else "automated" if script else "scouted" if guide else "new"


def public_website(site: str, members: list[dict] | None = None) -> dict:
    data, script, guide = read_website(site), account_script(site), read_guide(site)
    if members is None:
        members = [public_account(x) for x in account_ids() if site_of(x) == site]
    return {"id": site, "name": data.get("name") or site, "url": data.get("url") or "", "notes": data.get("notes") or "",
            "state": website_state(site, data, guide, script), "guide": guide, "script": script.name if script else "",
            "script_updated": script.stat().st_mtime if script else None,
            "action_scripts": [x.name for x in action_scripts(site)],
            "scout_session": (data.get("scout") or {}).get("session") or "",
            "scout_note": (data.get("scout") or {}).get("note") or "", "last_action": last_action(data),
            "accounts": [{"id": a["id"], "name": a["name"], "login": a["username"] or a["email"]} for a in members]}


def public_account(account_id: str) -> dict:
    data = read_account(account_id)
    site = site_of(account_id, data)
    w = read_website(site)
    return {"id": account_id, "name": data.get("name") or data.get("username") or data.get("email") or account_id,
            "site": site, "site_name": w.get("name") or site, "url": w.get("url") or data.get("url") or "",
            "purpose": data.get("purpose") or "", "notes": data.get("notes") or "", "username": str(data.get("username") or ""),
            "email": str(data.get("email") or ""), "password_set": bool(data.get("password")),
            "created_by": data.get("created_by") or "",
            "browser": data.get("browser") if data.get("browser") in BROWSERS else "headed",
            "last_action": last_action(data)}


def last_action(data: dict) -> dict | None:
    run = (data.get("actions") or [None])[-1]
    if not run:
        return None
    return {"id": run["id"], "t": run["t"], "task": run["task"], "session": run["session"],
            "state": agent_session_state(run["session"]) or "unknown", "count": len(data.get("actions") or [])}


def account_ids() -> list[str]:
    if not ACCOUNTS.is_dir():
        return []
    return sorted(p.stem for p in ACCOUNTS.glob("*.json") if ACCOUNT_ID.fullmatch(p.stem))


def website_ids() -> list[str]:
    if not WEBSITES.is_dir():
        return []
    return sorted(p.stem for p in WEBSITES.glob("*.json") if ACCOUNT_ID.fullmatch(p.stem))


def account_or_404(account_id: str) -> str:
    if not ACCOUNT_ID.fullmatch(account_id) or not (ACCOUNTS / f"{account_id}.json").is_file():
        raise HTTPException(status_code=404, detail="No such account")
    return account_id


def website_or_404(site: str) -> str:
    if not ACCOUNT_ID.fullmatch(site) or not (WEBSITES / f"{site}.json").is_file():
        raise HTTPException(status_code=404, detail="No such website")
    return site


def host_of(url: str) -> str:
    return re.sub(r"^www\.", "", re.sub(r"^https?://", "", url or "").split("/")[0].split(":")[0].lower())


def new_id(name: str, url: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or host_of(url).split(".")[0]).lower()).strip("-")[:32] or "site"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def migrate_accounts() -> None:
    """Accounts from before the Websites/Accounts split get a website (one per host), and the guide and
    upload script they had move to it so every account on that site shares them."""
    if not ACCOUNTS.is_dir():
        return
    by_host = {host_of(read_website(w).get("url") or ""): w for w in website_ids()}
    for account_id in account_ids():
        data = read_account(account_id)
        if data.get("site") and (WEBSITES / f"{data['site']}.json").is_file():
            continue
        url, host = str(data.get("url") or ""), host_of(str(data.get("url") or ""))
        site = by_host.get(host)
        if not site:
            label = host.split(".")[0].capitalize() if host else account_id
            site = new_id(host.split(".")[0], url, set(website_ids()))
            write_website(site, {"name": label, "url": url, "notes": "", "created": time.time(),
                                 **({"scout": data["scout"]} if data.get("scout") else {})})
            by_host[host] = site
        if site != account_id:
            old_guide, new_guide = ACCOUNTS / "sites" / f"{account_id}.json", ACCOUNTS / "sites" / f"{site}.json"
            if old_guide.is_file() and not new_guide.exists():
                old_guide.rename(new_guide)
            script = account_script(account_id)
            if script and not account_script(site):
                script.rename(script.with_name(f"{site}_upload{script.suffix}"))
        data = {k: v for k, v in data.items() if k not in ("url", "scout")}
        write_account(account_id, {**data, "site": site})


# ---------------- websites ----------------

@app.get(p("/api/websites"))
async def list_websites():
    def build():
        import_created()
        accounts = [public_account(x) for x in account_ids()]
        return [public_website(w, [a for a in accounts if a["site"] == w]) for w in website_ids()]
    return {"websites": await asyncio.to_thread(build), "automation": read_automation(), "available": ACCOUNTS.is_dir()}


def clean_website(body: dict, current: dict) -> dict:
    name = _text(body.get("name") if "name" in body else current.get("name"), 60)
    url = str(body.get("url") if "url" in body else current.get("url") or "").strip()
    if url and not re.match(r"https?://", url):
        url = "https://" + url
    if not re.fullmatch(r"https?://[^\s/$.?#][^\s]{1,300}", url):
        raise HTTPException(status_code=422, detail="Enter the website's address, e.g. https://www.reddit.com")
    if not name:
        raise HTTPException(status_code=422, detail="Give the website a name")
    return {**current, "name": name, "url": url,
            "notes": str(body.get("notes") if "notes" in body else current.get("notes") or "").strip()[:4000]}


async def json_body(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON object required")
    return body


@app.post(p("/api/websites"), status_code=201)
async def add_website(request: Request, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    data = clean_website(await json_body(request), {})
    if (same := next((w for w in website_ids() if host_of(read_website(w).get("url") or "") == host_of(data["url"])), None)):
        raise HTTPException(status_code=409, detail=f"{read_website(same).get('name') or same} is already added for that address")
    site = new_id(data["name"], data["url"], set(website_ids()) | set(account_ids()))
    write_website(site, {**data, "created": time.time()})
    return await asyncio.to_thread(public_website, site)


@app.put(p("/api/websites/{site}"))
async def save_website(site: str, request: Request, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    website_or_404(site)
    write_website(site, clean_website(await json_body(request), read_website(site)))
    return await asyncio.to_thread(public_website, site)


@app.delete(p("/api/websites/{site}"))
async def delete_website(site: str, x_connections_action: str = Header(default="")):
    """Only an empty website; its guide and scripts stay (adding the site again reuses them)."""
    require_action(x_connections_action)
    website_or_404(site)
    if any(site_of(x) == site for x in account_ids()):
        raise HTTPException(status_code=409, detail="Remove or move its accounts first")
    (WEBSITES / f"{site}.json").unlink()
    return {"deleted": site}


@app.post(p("/api/websites/{site}/reset-script"))
async def reset_website_script(site: str, x_connections_action: str = Header(default="")):
    """Set the guide and upload script aside: the next scout or upload works the site out again."""
    require_action(x_connections_action)
    website_or_404(site)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path in [account_script(site), ACCOUNTS / "sites" / f"{site}.json"]:
        if path and path.is_file():
            path.rename(path.with_name(f"{path.stem}-old-{stamp}{path.suffix}"))    # out of the <site>_upload.* glob
    return await asyncio.to_thread(public_website, site)


CAPTCHA_VISION_PROMPT = (
    "For any visual CAPTCHA, capture only the current challenge as an image, share that crop in the Master Agent "
    "chat, and read the same image with vision_analyze. Do not decode CSS or pixels into ASCII/text art, assume "
    "a fixed length, or reuse an answer. If you write a site script, it must call the active agent via "
    "help.handoff on each fresh CAPTCHA with a cropped selector or clip; the agent enters the visually read answer "
    "on that same page, then the script resumes. Never hard-code a CAPTCHA answer or accept one as a saved script "
    "argument. Check the site's response for errors before reporting success or saving credentials."
)


@app.post(p("/api/websites/{site}/scout"))
async def scout_website(site: str, request: Request, x_connections_action: str = Header(default="")):
    """Start a Master Agent session that works the site out with one of its accounts: log in, find how
    to do what the account is for, write the site guide and the script. Nothing is posted."""
    require_action(x_connections_action)
    website_or_404(site)
    body = (await request.json() if (await request.body()) else {}) or {}
    members = [x for x in account_ids() if site_of(x) == site]
    account_id, note = str(body.get("account") or ""), str(body.get("note") or "").strip()[:2000]
    if account_id and account_id not in members:
        raise HTTPException(status_code=422, detail="That account isn't on this website")
    data = read_website(site)
    how = (f"with account {account_id}: run `$R scout {account_id}`" if account_id
           else f"logged out (no account): run `$R scout {site} --website`")
    prompt = (f"Scout the website {data.get('name')} ({data.get('url')}) {how} and follow what it returns. "
              "Don't post or change anything on the site. " + CAPTCHA_VISION_PROMPT
              + (f" What it is for: {note}" if note else ""))
    try:
        sid = await asyncio.to_thread(start_agent_session, prompt)    # up to 20 s; don't hold the event loop
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=502, detail=f"The Master Agent isn't reachable: {exc}") from exc
    write_website(site, {**read_website(site), "scout": {"session": sid, "t": time.time(), "account": account_id, "note": note}})
    return await asyncio.to_thread(public_website, site)    # may poll the Agent for the scout state


@app.put(p("/api/automation"))
async def save_automation(request: Request, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    body = await json_body(request)
    current = read_automation()
    data = {k: bool(body[k]) if k in body else current[k] for k in ("use_scripts", "write_scripts")}
    write_json(AUTOMATION, data)
    os.chmod(AUTOMATION, 0o644)      # not secret; the agent reads it
    return data


def _source(path: Path) -> str:
    with suppress(OSError):
        return path.read_text(errors="replace")[:300_000]
    return ""


@app.get(p("/api/library"))
async def script_library():
    """Everything written for navigating each website: the guide, the upload and action scripts (with their
    code), set-aside versions, and the user's notes for the site and its accounts."""
    def build():
        accounts = [public_account(x) for x in account_ids()]
        out = []
        for site in website_ids():
            w, guide_path = read_website(site), ACCOUNTS / "sites" / f"{site}.json"
            scripts = [p for p in [account_script(site)] if p] + action_scripts(site)
            old = sorted([*(ACCOUNTS / "scripts").glob(f"{site}_upload-old-*"), *(ACCOUNTS / "sites").glob(f"{site}-old-*")]) \
                if ACCOUNTS.is_dir() else []
            out.append({"id": site, "name": w.get("name") or site, "url": w.get("url") or "", "notes": w.get("notes") or "",
                        "guide": _source(guide_path) if guide_path.is_file() else "",
                        "guide_updated": guide_path.stat().st_mtime if guide_path.is_file() else None,
                        "scripts": [{"name": x.name, "kind": "upload" if x.name.startswith(f"{site}_upload") else "action",
                                     "updated": x.stat().st_mtime, "code": _source(x)} for x in scripts],
                        "old": [{"name": x.name, "updated": x.stat().st_mtime, "code": _source(x)} for x in old],
                        "accounts": [{"name": a["name"], "purpose": a["purpose"], "notes": a["notes"]}
                                     for a in accounts if a["site"] == site and (a["purpose"] or a["notes"])]})
        return out
    return {"websites": await asyncio.to_thread(build), "automation": read_automation()}


# ---------------- accounts ----------------

@app.get(p("/api/accounts"))
async def list_accounts():
    def build():
        import_created()
        return [public_account(x) for x in account_ids()]
    return {"accounts": await asyncio.to_thread(build), "available": ACCOUNTS.is_dir()}


@app.post(p("/api/accounts"), status_code=201)
async def add_account(request: Request, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    data = clean_account(await json_body(request), {})
    account_id = new_id(data["name"] or data["username"] or data["email"].split("@")[0], "", set(account_ids()) | set(website_ids()))
    write_account(account_id, {**data, "created": time.time()})
    return await asyncio.to_thread(public_account, account_id)


def clean_account(body: dict, current: dict) -> dict:
    site = str(body.get("site") if "site" in body else current.get("site") or "")
    name = _text(body.get("name") if "name" in body else current.get("name"), 60)
    username = str(body.get("username") if "username" in body else current.get("username") or "").strip()
    email = str(body.get("email") if "email" in body else current.get("email") or "").strip()
    password = str(body.get("password") or "") or str(current.get("password") or "")
    if not ACCOUNT_ID.fullmatch(site) or not (WEBSITES / f"{site}.json").is_file():
        raise HTTPException(status_code=422, detail="Pick the website this account is on")
    if not username and not email:
        raise HTTPException(status_code=422, detail="Enter the account's username, email or both")
    if len(username) > 200 or any(char.isspace() for char in username):
        raise HTTPException(status_code=422, detail="The username can't contain spaces")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
        raise HTTPException(status_code=422, detail="Enter a valid email address, or leave it empty")
    if not password or len(password) > 500:
        raise HTTPException(status_code=422, detail="Enter the account's password")
    return {**current, "site": site, "name": name, "username": username, "email": email, "password": password,
            "browser": body.get("browser") if body.get("browser") in BROWSERS else current.get("browser") or "headed",
            "purpose": _text(body.get("purpose") if "purpose" in body else current.get("purpose"), 600),
            "notes": str(body.get("notes") if "notes" in body else current.get("notes") or "").strip()[:2000]}


@app.put(p("/api/accounts/{account_id}"))
async def save_account(account_id: str, request: Request, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    account_or_404(account_id)
    write_account(account_id, clean_account(await json_body(request), read_account(account_id)))
    return await asyncio.to_thread(public_account, account_id)


@app.get(p("/api/accounts/{account_id}/password"))
async def account_password(account_id: str):
    """The saved password, fetched only when the user clicks Show/Copy so lists never carry it."""
    account_or_404(account_id)
    return JSONResponse({"password": str(read_account(account_id).get("password") or "")},
                        headers={"Cache-Control": "no-store"})


@app.delete(p("/api/accounts/{account_id}"))
async def delete_account(account_id: str, x_connections_action: str = Header(default="")):
    """Removes the login only; the website, its guide and scripts stay."""
    require_action(x_connections_action)
    account_or_404(account_id)
    (ACCOUNTS / f"{account_id}.json").unlink()
    return {"deleted": account_id}


# Actions: a one-off task the user gives the agent, as one account ("create an email for my business") or
# logged out on a website ("sign up three new accounts"). The runs are kept in the account or website file
# (actions[]); the agent's own steps are read back from its session. Logins an action creates are appended
# by the agent's script to created/<account>.jsonl or created/site-<website>.jsonl; one naming a website
# in Connections is added to Accounts by itself (import_created).
ACTION_LOG_KINDS = ("say", "tool", "assistant", "approval", "warn", "error")
MAX_ACTIONS = 50
CREATED = ACCOUNTS / "created"
IMPORTED = CREATED / ".imported.json"     # created lines already turned into accounts (so a removed one stays removed)


def action_task(body) -> str:
    task = str((body or {}).get("task") or "").strip()[:2000] if isinstance(body, dict) else ""
    if not task:
        raise HTTPException(status_code=422, detail="Say what the agent should do on the site")
    return task


async def start_action(prompt: str) -> str:
    try:    # the user asked for this from Connections, so site actions need no extra OK
        return await asyncio.to_thread(start_agent_session, prompt, ["Uploads"])
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=502, detail=f"The Master Agent isn't reachable: {exc}") from exc


@app.post(p("/api/accounts/{account_id}/action"), status_code=201)
async def run_account_action(account_id: str, request: Request, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    account_or_404(account_id)
    task = action_task(await request.json())
    data, run_id = full_account(account_id), uuid.uuid4().hex[:8]
    sid = await start_action(f"Do an action on the website {data.get('site_name')} ({data.get('url')}) as account {account_id}: run "
                             f"`$R act {account_id} --run {run_id}` and follow what it returns. The task is: {task}. "
                             + CAPTCHA_VISION_PROMPT)
    data = read_account(account_id)
    runs = (data.get("actions") or []) + [{"id": run_id, "t": time.time(), "task": task, "session": sid}]
    write_account(account_id, {**data, "actions": runs[-MAX_ACTIONS:]})
    return {"id": run_id, "session": sid}


@app.post(p("/api/websites/{site}/action"), status_code=201)
async def run_website_action(site: str, request: Request, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    website_or_404(site)
    task = action_task(await request.json())
    data, run_id = read_website(site), uuid.uuid4().hex[:8]
    sid = await start_action(f"Do an action on the website {data.get('name')} ({data.get('url')}), logged out (no account): run "
                             f"`$R act {site} --website --run {run_id}` and follow what it returns. The task is: {task}. "
                             + CAPTCHA_VISION_PROMPT)
    data = read_website(site)
    runs = (data.get("actions") or []) + [{"id": run_id, "t": time.time(), "task": task, "session": sid}]
    write_website(site, {**data, "actions": runs[-MAX_ACTIONS:]})
    return {"id": run_id, "session": sid}


def session_log(sid: str) -> tuple[str, list[dict]]:
    with suppress(Exception):
        with urllib.request.urlopen(f"{AGENT_URL}/Agent/api/sessions/{sid}?since=0", timeout=5) as response:
            data = json.load(response)
        log = [{"t": e.get("t"), "kind": e["kind"], "text": str(e.get("text") or "")[:600]}
               for e in data.get("events") or [] if e.get("kind") in ACTION_LOG_KINDS]
        return str(data.get("state") or ""), log[-200:]
    return "unknown", []


def created_lines(owner: str) -> list[dict]:
    """The logins one account's (or website's: site-<id>) actions created, oldest first; key = file:line."""
    out = []
    with suppress(OSError):
        for n, line in enumerate((CREATED / f"{owner}.jsonl").read_text().splitlines()):
            with suppress(ValueError):
                x = json.loads(line)
                if isinstance(x, dict):
                    out.append({k: str(x.get(k) or "")[:500] for k in ("site", "username", "email", "password", "url", "note")}
                               | {"t": x.get("t"), "key": f"{owner}:{n}", "owner": owner})
    return out


def all_created() -> list[dict]:
    owners = sorted(p.stem for p in CREATED.glob("*.jsonl")) if CREATED.is_dir() else []
    return [x for owner in owners for x in created_lines(owner)]


_created_import_lock = threading.Lock()


def import_created() -> None:
    """Import each created login once, even with concurrent Accounts list requests."""
    with _created_import_lock:
        _import_created()


def _import_created() -> None:
    """Add each created login that names a website in Connections to Accounts (once)."""
    if not CREATED.is_dir():
        return
    done = set(read_json(IMPORTED).get("keys") or [])
    sites, new = set(website_ids()), []
    for x in all_created():
        if x["key"] in done or x["site"] not in sites or not x["password"] or not (x["username"] or x["email"]):
            continue
        same = [a for a in account_ids() if site_of(a) == x["site"] and
                {str(read_account(a).get(k) or "").lower() for k in ("username", "email")} & {x["username"].lower(), x["email"].lower()} - {""}]
        if not same:
            account_id = new_id(x["username"] or x["email"].split("@")[0], "", set(account_ids()) | sites)
            write_account(account_id, {"site": x["site"], "name": x["username"] or x["email"], "username": x["username"],
                                       "email": x["email"], "password": x["password"], "browser": "headed", "purpose": "",
                                       "notes": x["note"], "created": x["t"] or time.time(), "created_by": x["owner"]})
        new.append(x["key"])
    if new:
        write_json(IMPORTED, {"keys": sorted(done | set(new))})


async def action_runs(runs: list[dict]) -> list[dict]:
    runs = list(reversed(runs))
    logs = await asyncio.gather(*[asyncio.to_thread(session_log, r["session"]) for r in runs[:20]])
    return [{**r, "state": state, "log": log} for r, (state, log) in zip(runs, logs)] + [{**r, "state": "", "log": []} for r in runs[20:]]


@app.get(p("/api/accounts/{account_id}/actions"))
async def account_actions(account_id: str):
    """Every action run as the account, newest first, with the agent's steps; and the logins its actions created."""
    account_or_404(account_id)
    return {"actions": await action_runs(read_account(account_id).get("actions") or []),
            "created": (await asyncio.to_thread(created_lines, account_id))[::-1]}


@app.get(p("/api/websites/{site}/actions"))
async def website_actions(site: str):
    website_or_404(site)
    return {"actions": await action_runs(read_website(site).get("actions") or []),
            "created": (await asyncio.to_thread(created_lines, f"site-{site}"))[::-1]}


@app.get(p("/api/emails"))
async def list_emails():
    """Every email address: the ones on connected accounts, and the ones the agent's actions created."""
    def build():
        names = {w: read_website(w).get("name") or w for w in website_ids()}
        rows, seen = [], set()
        for a in account_ids():
            data = read_account(a)
            if data.get("email"):
                site = site_of(a, data)
                rows.append({"email": data["email"], "source": "agent" if data.get("created_by") else "connected",
                             "account": a, "account_name": data.get("name") or a, "site": site, "site_name": names.get(site, site),
                             "t": data.get("created"), "note": data.get("notes") or "", "password": ""})
                seen.add(data["email"].lower())
        for x in all_created():
            if x["email"] and x["email"].lower() not in seen:
                rows.append({"email": x["email"], "source": "agent", "account": "", "account_name": "",
                             "site": x["site"], "site_name": names.get(x["site"]) or host_of(x["url"]) or x["site"],
                             "t": x["t"], "note": x["note"], "password": x["password"]})
                seen.add(x["email"].lower())
        return sorted(rows, key=lambda r: -(r["t"] or 0))
    return {"emails": await asyncio.to_thread(build)}


def start_agent_session(prompt: str, cleared: list[str] | None = None) -> str:
    body = {"prompt": prompt, **({"cleared": cleared} if cleared else {})}
    request = urllib.request.Request(f"{AGENT_URL}/Agent/api/sessions", method="POST",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)["id"]


# ---------------- scheduled posts ----------------

def public_post(post: dict, names: dict[str, str]) -> dict:
    run = (post.get("runs") or [None])[-1]
    live = agent_session_state(run["session"]) if run and run.get("session") else ""
    failed = bool(run and (run.get("error") or live in ("failed", "stopped", "interrupted")))
    state = ("posting" if live in ("starting", "waiting_for_gpu", "thinking", "rendering") else
             "waiting" if post.get("awaiting_file") else
             "scheduled" if post.get("enabled") and post.get("next") else "failed" if failed else
             "skipped" if run and run.get("skipped") else
             "missed" if post.get("missed") else "paused" if post.get("next") else "done")
    accounts = post.get("accounts") or [post["account"]]
    return {**{k: post.get(k) for k in ("id", "account", "where", "title", "body", "tags", "notes", "time", "date",
                                       "repeat", "tz", "enabled", "next", "missed", "media", "batch", "batch_label", "awaiting_file", "blank")},
            "accounts": accounts, "account_mode": post.get("account_mode") or "rotate",
            "account_name": names.get(post["account"], post["account"]),
            "account_names": [names.get(a, a) for a in accounts], "summary": post_schedule.summary(post),
            "state": state, "last_run": {**run, "state": live, "account_name": names.get(run.get("account"), run.get("account") or "")} if run else None,
            "runs": len(post.get("runs") or [])}


def accounts_from(body: dict) -> tuple[list[str], str]:
    """The accounts a post (or a batch) posts from, all existing, and how it picks between them."""
    raw = body.get("accounts") if isinstance(body.get("accounts"), list) else [body.get("account")]
    accounts = list(dict.fromkeys(account_or_404(str(a or "")) for a in raw if str(a or "")))[:50]
    if not accounts:
        raise HTTPException(status_code=422, detail="Pick at least one website account")
    mode = str(body.get("account_mode") or "rotate")
    if mode not in post_schedule.ACCOUNT_MODES:
        raise HTTPException(status_code=422, detail="Account choice is rotate or random")
    return accounts, mode


def post_or_404(post_id: str) -> dict:
    post = posts.get(post_id) if re.fullmatch(r"[0-9a-f]{10}", post_id) else None
    if not post:
        raise HTTPException(status_code=404, detail="No such scheduled post")
    return post


async def send_post(post: dict) -> dict:
    """Start the Master Agent session that posts it; the run (session or error) is kept on the post."""
    if post_schedule.is_blank(post) and post.get("blank", "skip") == "skip":
        run = {"t": time.time(), "session": "", "account": "", "error": "", "skipped": "It was still blank"}
        post["runs"] = (post.get("runs") or [])[-(post_schedule.KEEP_RUNS - 1):] + [run]
        return run
    account_id = post_schedule.pick_account(post, set(account_ids()))
    account = full_account(account_id) if account_id else {}
    if not account:
        run = {"t": time.time(), "session": "", "account": "", "error": "The website account was removed"}
    else:
        prompt = post_schedule.agent_prompt(post, account_id, account, posts.media_path(post))
        try:
            run = {"t": time.time(), "session": await asyncio.to_thread(start_agent_session, prompt, ["Uploads"]),
                   "account": account_id, "error": ""}
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            run = {"t": time.time(), "session": "", "account": account_id, "error": f"The Master Agent isn't reachable: {exc}"}
    post["runs"] = (post.get("runs") or [])[-(post_schedule.KEEP_RUNS - 1):] + [run]
    return run


async def post_scheduler() -> None:
    """Send due posts. One that can't reach the Agent is retried each round until it is an hour late;
    one missed by over an hour (Connections was down) is marked missed (Post now still sends it)."""
    while True:
        await asyncio.sleep(20)
        try:
            for post_id in [x["id"] for x in posts.load()]:
                async with post_lock:
                    post, t = posts.get(post_id), time.time()
                    if not post or not post.get("enabled") or not post.get("next") or post["next"] > t:
                        continue
                    if t - post["next"] <= post_schedule.LATE:
                        run = await send_post(post)
                        if run["error"] and t - post["next"] < post_schedule.LATE - 30:
                            posts.put(post)
                            continue
                    else:
                        post["missed"] = post["next"]
                    post["next"] = post_schedule.next_run(post, t)
                    post["enabled"] = post["next"] is not None
                    posts.put(post)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"post scheduler round failed: {exc!r}", file=sys.stderr, flush=True)


def account_names() -> dict[str, str]:
    return {x: read_account(x).get("name") or x for x in account_ids()}


async def save_media(post: dict, media: UploadFile | None) -> None:
    if media is None or not media.filename:
        return
    suffix = Path(media.filename).suffix.lower()
    if suffix not in post_schedule.MEDIA_TYPES:
        raise HTTPException(status_code=422, detail="Attach an image (png, jpg, gif, webp) or a video (mp4, webm, mov)")
    posts.media.mkdir(parents=True, exist_ok=True)
    target, size = posts.media / f"{post['id']}-{int(time.time())}{suffix}", 0
    try:
        with target.open("wb") as handle:
            while chunk := await media.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_MEDIA:
                    raise HTTPException(status_code=413, detail=f"Files can be up to {MAX_MEDIA // 1024 // 1024} MB")
                handle.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        await media.close()
    posts.drop_media(post)
    post["media"] = target.name


def parse_post_form(raw: str) -> dict:
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Bad post data") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Bad post data")
    return body


@app.get(p("/api/posts"))
async def list_posts():
    def build() -> list[dict]:
        names = account_names()
        return [public_post(x, names) for x in sorted(posts.load(), key=lambda x: x.get("next") or x.get("created") or 0)]
    return {"posts": await asyncio.to_thread(build), "repeats": list(post_schedule.REPEATS)}


@app.post(p("/api/posts"), status_code=201)
async def add_post(post: str = Form(...), media: UploadFile | None = File(default=None),
                   x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    async with post_lock:
        body = parse_post_form(post)
        accounts, mode = accounts_from(body)
        try:
            item = post_schedule.new_post(accounts, body, time.time(), mode, allow_blank=bool(media and media.filename))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await save_media(item, media)
        posts.put(item)
        return await asyncio.to_thread(public_post, item, account_names())


@app.put(p("/api/posts/{post_id}"))
async def edit_post(post_id: str, post: str = Form(...), media: UploadFile | None = File(default=None),
                    x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    async with post_lock:
        item, body = post_or_404(post_id), parse_post_form(post)
        if "accounts" in body or "account" in body:
            post_schedule.set_accounts(item, *accounts_from(body))
        try:
            item = post_schedule.clean_post(body, item, time.time(), allow_blank=bool(media and media.filename))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        item.pop("missed", None)
        if body.get("remove_media"):
            posts.drop_media(item)
            item["media"] = ""
        await save_media(item, media)
        if item.get("awaiting_file"):
            if item.get("media"):
                item.pop("awaiting_file")
            else:
                item["enabled"] = False
        posts.put(item)
        return await asyncio.to_thread(public_post, item, account_names())


@app.delete(p("/api/posts/{post_id}"))
async def delete_post(post_id: str, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    async with post_lock:
        posts.delete(post_or_404(post_id)["id"])
        return {"deleted": post_id}


def post_time(post: dict) -> float:
    """When a post is (or was) due."""
    with suppress(KeyError, ValueError):
        tz = post_schedule.zone(post.get("tz", ""))
        return datetime.fromisoformat(f"{post['date']}T{post['time']}").replace(tzinfo=tz).timestamp()
    return post.get("next") or post.get("created") or 0


def unsent(post: dict) -> bool:
    return not post.get("runs") and post.get("repeat", "once") == "once" and bool(post.get("next") or post.get("awaiting_file"))


def seeded(body: dict):
    import random
    try:
        seed = int(body.get("seed") or 0)
    except (TypeError, ValueError):
        seed = 0
    return random.Random(seed) if seed else None        # the page's seed: the preview and the saved times match


def batch_summary(batch_id: str, record: dict, items: list[dict], names: dict[str, str]) -> dict:
    mine = sorted((x for x in items if x.get("batch") == batch_id), key=post_time)
    waiting = [x for x in mine if unsent(x)]
    status = ("empty" if not mine else "active" if any(x.get("enabled") for x in waiting) or any(x.get("awaiting_file") for x in waiting)
              else "paused" if waiting else "complete")
    settings = record.get("settings") or {}
    return {"id": batch_id, "label": record.get("label") or (mine[0].get("batch_label") if mine else batch_id),
            "created": record.get("created"), "settings": settings, "status": status, "posts": len(mine), "left": len(waiting),
            "blank": sum(1 for x in waiting if post_schedule.is_blank(x)),
            "next": min((x["next"] for x in waiting if x.get("enabled") and x.get("next")), default=None),
            "last_at": post_time(mine[-1]) if mine else None,
            "account_names": [names.get(a, a) for a in settings.get("accounts") or []]}


@app.get(p("/api/batches"))
async def list_batches():
    records, items, names = batches.load(), posts.load(), await asyncio.to_thread(account_names)
    for x in items:        # batches made before batches were saved
        if x.get("batch") and x["batch"] not in records:
            records[x["batch"]] = {"label": x.get("batch_label") or x["batch"], "created": x.get("created"),
                                   "settings": {"accounts": x.get("accounts") or [x["account"]], "tz": x.get("tz")}}
    out = [batch_summary(k, v, items, names) for k, v in records.items()]
    return {"batches": sorted(out, key=lambda b: b.get("created") or 0, reverse=True)}


@app.post(p("/api/posts/bulk"))
async def bulk_add_posts(request: Request, x_connections_action: str = Header(default="")):
    """Plan (preview: true) or create many one-off posts, as a new batch or (batch: id) more posts for one.
    Posts with a file wait for it (POST /api/posts/{id}/media) before they are scheduled."""
    require_action(x_connections_action)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON object required")
    accounts, mode = accounts_from(body)
    batch = str(body.get("batch") or "")
    if batch and not re.fullmatch(r"[0-9a-f]{8}", batch):
        raise HTTPException(status_code=404, detail="No such batch")
    plan = {**body, "accounts": accounts, "account_mode": mode}
    if body.get("continue"):
        plan.pop("start_date", None)
    files = [str(x)[:200] for x in body.get("files") or [] if str(x).strip()]
    async with post_lock:
        mine = [x for x in posts.load() if batch and x.get("batch") == batch]
        try:
            planned = post_schedule.bulk_posts(plan, files, time.time(), seeded(body),
                                               existing=[post_time(x) for x in mine], offset=len(mine))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if body.get("preview"):
            names = account_names()
            return {"posts": [{**{k: x[k] for k in ("account", "where", "title", "body", "at", "file")},
                               "account_name": names.get(x["account"], x["account"])} for x in planned]}
        records = batches.load()
        if batch and batch not in records and not mine:
            raise HTTPException(status_code=404, detail="No such batch")
        batch = batch or uuid.uuid4().hex[:8]
        record = records.get(batch) or {"created": time.time()}
        record.update(label=str(body.get("label") or "").strip()[:60] or record.get("label") or time.strftime("Batch %d %b %H:%M"),
                      settings=post_schedule.batch_settings(plan))
        records[batch] = record
        created, now, items = [], time.time(), posts.load()
        for x in planned:
            try:
                item = post_schedule.new_post([x["account"]], x, now, allow_blank=True,
                                              extra={"batch": batch, "batch_label": record["label"], "blank": record["settings"]["blank"]})
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"{x['title'][:40] or 'A post'}: {exc}") from exc
            if x["file"]:
                item.update(awaiting_file=x["file"], enabled=False)
            items.append(item)
            created.append({"id": item["id"], "file": x["file"]})
        posts.save(items)
        batches.save(records)
    return {"batch": batch, "label": record["label"], "posts": created}


@app.put(p("/api/batches/{batch}"))
async def edit_batch(batch: str, request: Request, x_connections_action: str = Header(default="")):
    """Rename a batch, change its settings, and optionally apply them to the posts not sent yet:
    `apply` puts the new accounts / destinations / tags / notes where a post still has the old batch
    value (or none); `retime` gives those posts new times from the new timing."""
    require_action(x_connections_action)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON object required")
    accounts, mode = accounts_from(body)
    async with post_lock:
        records, items = batches.load(), posts.load()
        mine = sorted((x for x in items if x.get("batch") == batch), key=post_time)
        if batch not in records and not mine:
            raise HTTPException(status_code=404, detail="No such batch")
        record = records.get(batch) or {"created": time.time()}
        old = record.get("settings") or {}
        new = post_schedule.batch_settings({**old, **body, "accounts": accounts, "account_mode": mode})
        label = str(body.get("label") or "").strip()[:60] or record.get("label") or batch
        waiting = [x for x in mine if unsent(x)]
        if body.get("retime") and waiting:
            plan = {**new, **{k: body.get(k) for k in ("start_date", "start_time")}}
            if body.get("continue") or not plan.get("start_date"):
                plan.pop("start_date", None)
            try:
                post_schedule.retime(waiting, plan, time.time(), seeded(body),
                                     existing=[post_time(x) for x in mine if not unsent(x) and post_time(x) > time.time() - 86400])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if body.get("apply"):
            wheres = [w.strip()[:200] for w in str(new.get("where") or "").splitlines() if w.strip()]
            old_wheres = {w.strip() for w in str(old.get("where") or "").splitlines() if w.strip()}
            import random
            rng = seeded(body) or random.SystemRandom()
            for i, x in enumerate(waiting):
                choice = rng.choice(accounts) if mode == "random" else accounts[i % len(accounts)]
                x.update(account=choice, accounts=[choice], account_mode="rotate")
                if wheres and (not x.get("where") or x["where"] in old_wheres):
                    x["where"] = wheres[i % len(wheres)]
                for key in ("tags", "notes"):
                    if not x.get(key) or x[key] == (old.get(key) or ""):
                        x[key] = str(new.get(key) or "").strip()[:1000 if key == "notes" else 300]
        for x in mine:
            x.update(batch_label=label, blank=new["blank"])
        record.update(label=label, settings=new)
        records[batch] = record
        posts.save(items)
        batches.save(records)
        return batch_summary(batch, record, items, account_names())


@app.post(p("/api/posts/{post_id}/media"))
async def add_post_media(post_id: str, media: UploadFile = File(...), x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    async with post_lock:
        item = post_or_404(post_id)
        await save_media(item, media)
        if item.pop("awaiting_file", None):
            item["next"] = post_schedule.next_run(item, time.time())
            item["enabled"] = item["next"] is not None
        posts.put(item)
        return await asyncio.to_thread(public_post, item, account_names())


@app.post(p("/api/posts/batch/{batch}/{action}"))
async def control_batch(batch: str, action: str, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    if action not in ("pause", "resume", "delete"):
        raise HTTPException(status_code=404, detail="Unknown batch action")
    async with post_lock:
        items, kept, changed = posts.load(), [], 0
        for item in items:
            if item.get("batch") != batch:
                kept.append(item)
                continue
            changed += 1
            if action == "delete":
                posts.drop_media(item)
                continue
            if not unsent(item):
                kept.append(item)
                continue
            if action == "pause":
                item["enabled"] = False
            elif not item.get("awaiting_file"):
                item["next"] = post_schedule.next_run(item, time.time())
                item["enabled"] = item["next"] is not None
            kept.append(item)
        records = batches.load()
        if not changed and batch not in records:
            raise HTTPException(status_code=404, detail="No such batch")
        posts.save(kept)
        if action == "delete" and records.pop(batch, None) is not None:
            batches.save(records)
    return {"batch": batch, "changed": changed}


@app.post(p("/api/posts/{post_id}/{action}"))
async def control_post(post_id: str, action: str, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    async with post_lock:
        item = post_or_404(post_id)
        if item.get("awaiting_file") and action != "pause":
            raise HTTPException(status_code=409, detail=f"Its file ({item['awaiting_file']}) didn't upload; edit the post and attach it")
        if action == "now":
            run = await send_post(item)
            if run["error"]:
                posts.put(item)
                raise HTTPException(status_code=502, detail=run["error"])
            item.pop("missed", None)
            if item.get("repeat") == "once":
                item.update(enabled=False, next=None)
        elif action in ("pause", "resume"):
            if action == "resume":
                item["next"] = post_schedule.next_run(item, time.time())
                if item["next"] is None:
                    raise HTTPException(status_code=409, detail="That time has passed; edit the post to pick a new one")
                item.pop("missed", None)
            item["enabled"] = action == "resume"
        else:
            raise HTTPException(status_code=404, detail="Unknown post action")
        posts.put(item)
        return await asyncio.to_thread(public_post, item, account_names())


@app.get(p("/api/posts/{post_id}/media"))
async def post_media(post_id: str):
    item = post_or_404(post_id)
    path = posts.media / item["media"] if item.get("media") else None
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="No file")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


def require_action(header: str) -> None:
    if header != "1":
        raise HTTPException(status_code=403, detail="Action header required")


@app.post(p("/api/watcher/start"))
async def start_watcher(x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    if not watcher.configured():
        raise HTTPException(status_code=409, detail="Telegram bot configuration is missing")
    if not OUTPUT.is_dir() or not H3_OUTPUT.is_dir():
        raise HTTPException(status_code=409, detail="A ComfyUI output directory is unavailable")
    watcher.enabled = True
    watcher.save_enabled()
    watcher.start()
    return watcher.status()


@app.post(p("/api/watcher/stop"))
async def stop_watcher(x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    watcher.enabled = False
    watcher.save_enabled()
    await watcher.stop()
    return watcher.status()


STATIC = Path(__file__).parent / "static"


def asset_version() -> str:
    """Cloudflare keeps .css/.js for hours: the page asks for them by content version."""
    import hashlib
    digest = hashlib.sha256()
    for name in ("connections.css", "connections.js"):
        digest.update((STATIC / name).read_bytes())
    return digest.hexdigest()[:10]


def page_html() -> str:
    return (STATIC / "index.html").read_text().replace("{{v}}", asset_version())
