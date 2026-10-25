"""Connections: web control and supervision for the existing Telegram watcher."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, suppress
from collections import deque
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from cloudflare import read_status as read_cloudflare_status
from tunnels import TunnelError, TunnelManager, validate_routes
from cloudflare_api import HEX32, create_local_tunnel, create_dns_routes

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
    watcher.task = asyncio.create_task(watcher.supervise())
    tunnels.task = asyncio.create_task(tunnels.supervise())
    try:
        yield
    finally:
        watcher.task.cancel()
        tunnels.task.cancel()
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


def read_account(account_id: str) -> dict:
    try:
        data = json.loads((ACCOUNTS / f"{account_id}.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_account(account_id: str, data: dict) -> None:
    ACCOUNTS.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=ACCOUNTS, prefix=f".{account_id}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
        os.chmod(temp, 0o600)
        os.replace(temp, ACCOUNTS / f"{account_id}.json")
    except Exception:
        with suppress(OSError):
            os.unlink(temp)
        raise


def account_script(account_id: str) -> Path | None:
    return next(iter(sorted((ACCOUNTS / "scripts").glob(f"{account_id}_upload.*"))), None)


def _text(value, limit: int = 600) -> str:
    return " ".join(str(value or "").split())[:limit]


def read_guide(account_id: str) -> dict | None:
    """The site guide a scouting run wrote, trimmed to what the page shows (all plain text)."""
    path = ACCOUNTS / "sites" / f"{account_id}.json"
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


def public_account(account_id: str) -> dict:
    data, script, guide = read_account(account_id), account_script(account_id), read_guide(account_id)
    scout = data.get("scout") or {}
    scouting = bool(scout.get("session")) and (not guide or guide["updated"] < scout.get("t", 0)) \
        and agent_session_state(scout["session"]) in ("starting", "waiting_for_gpu", "thinking", "rendering")
    state = "scouting" if scouting else "automated" if script else "scouted" if guide else "new"
    return {"id": account_id, "name": data.get("name") or account_id, "url": data.get("url") or "",
            "purpose": data.get("purpose") or "", "username": str(data.get("username") or ""),
            "password_set": bool(data.get("password")), "browser": data.get("browser") if data.get("browser") in BROWSERS else "headed",
            "state": state, "guide": guide, "script": script.name if script else "",
            "script_updated": script.stat().st_mtime if script else None, "scout_session": scout.get("session") or ""}


def account_ids() -> list[str]:
    if not ACCOUNTS.is_dir():
        return []
    return sorted(p.stem for p in ACCOUNTS.glob("*.json") if ACCOUNT_ID.fullmatch(p.stem))


def account_or_404(account_id: str) -> str:
    if not ACCOUNT_ID.fullmatch(account_id) or not (ACCOUNTS / f"{account_id}.json").is_file():
        raise HTTPException(status_code=404, detail="No such account")
    return account_id


def new_account_id(name: str, url: str) -> str:
    host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower())
    base = re.sub(r"[^a-z0-9]+", "-", (name or host.split(".")[0]).lower()).strip("-")[:32] or "site"
    taken, candidate, n = set(account_ids()), base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


@app.get(p("/api/accounts"))
async def list_accounts():
    accounts = await asyncio.to_thread(lambda: [public_account(x) for x in account_ids()])
    return {"accounts": accounts, "available": ACCOUNTS.is_dir()}


@app.post(p("/api/accounts"), status_code=201)
async def add_account(request: Request, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON object required")
    data = clean_account(body, {})
    account_id = new_account_id(data["name"], data["url"])
    write_account(account_id, {**data, "created": time.time()})
    return public_account(account_id)


def clean_account(body: dict, current: dict) -> dict:
    name = _text(body.get("name") if "name" in body else current.get("name"), 60)
    url = str(body.get("url") if "url" in body else current.get("url") or "").strip()
    if url and not re.match(r"https?://", url):
        url = "https://" + url
    username = str(body.get("username") if "username" in body else current.get("username") or "").strip()
    password = str(body.get("password") or "") or str(current.get("password") or "")
    if not re.fullmatch(r"https?://[^\s/$.?#][^\s]{1,300}", url):
        raise HTTPException(status_code=422, detail="Enter the website's address, e.g. https://www.reddit.com")
    if not name:
        raise HTTPException(status_code=422, detail="Give the website a label")
    if not username or len(username) > 200 or any(char.isspace() for char in username):
        raise HTTPException(status_code=422, detail="Enter the account's username or email")
    if not password or len(password) > 500:
        raise HTTPException(status_code=422, detail="Enter the account's password")
    return {**current, "name": name, "url": url, "username": username, "password": password,
            "browser": body.get("browser") if body.get("browser") in BROWSERS else current.get("browser") or "headed",
            "purpose": _text(body.get("purpose") if "purpose" in body else current.get("purpose"), 600)}


@app.put(p("/api/accounts/{account_id}"))
async def save_account(account_id: str, request: Request, x_connections_action: str = Header(default="")):
    require_action(x_connections_action)
    account_or_404(account_id)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON object required")
    write_account(account_id, clean_account(body, read_account(account_id)))
    return public_account(account_id)


@app.delete(p("/api/accounts/{account_id}"))
async def delete_account(account_id: str, x_connections_action: str = Header(default="")):
    """Removes the login only; the site guide and script stay (adding the site again reuses them)."""
    require_action(x_connections_action)
    account_or_404(account_id)
    (ACCOUNTS / f"{account_id}.json").unlink()
    return {"deleted": account_id}


@app.post(p("/api/accounts/{account_id}/reset-script"))
async def reset_account_script(account_id: str, x_connections_action: str = Header(default="")):
    """Set the guide and script aside: the next scout or upload works the site out again."""
    require_action(x_connections_action)
    account_or_404(account_id)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path in [account_script(account_id), ACCOUNTS / "sites" / f"{account_id}.json"]:
        if path and path.is_file():
            path.rename(path.with_name(f"{path.stem}-old-{stamp}{path.suffix}"))    # out of the <id>_upload.* glob
    return public_account(account_id)


@app.post(p("/api/accounts/{account_id}/scout"))
async def scout_account(account_id: str, x_connections_action: str = Header(default="")):
    """Start a Master Agent session that works the site out: log in, find how to do what the account
    is for, write the site guide and the script. Nothing is posted."""
    require_action(x_connections_action)
    account_or_404(account_id)
    data = read_account(account_id)
    prompt = (f"Scout the website {data.get('name')} ({data.get('url')}), account id {account_id}: run `$R scout {account_id}` "
              "and follow what it returns. Don't post or change anything on the site.")
    request = urllib.request.Request(f"{AGENT_URL}/Agent/api/sessions", method="POST",
                                     data=json.dumps({"prompt": prompt}).encode(), headers={"Content-Type": "application/json"})

    def start_session() -> str:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)["id"]

    try:
        sid = await asyncio.to_thread(start_session)    # up to 20 s; don't hold the event loop
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=502, detail=f"The Master Agent isn't reachable: {exc}") from exc
    write_account(account_id, {**data, "scout": {"session": sid, "t": time.time()}})
    return await asyncio.to_thread(public_account, account_id)    # may poll the Agent for the scout state


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
