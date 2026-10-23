# Connections

Connections is the BunnyBytes service at `/Connections/`. It supervises the
existing `telegram_comfyui_output.py` watcher and exposes a login-protected
status page with Start/Pause controls, Telegram configuration, and recent
activity. The token is masked and is never returned to the browser. The
container runs as UID 1000, listens on 127.0.0.1:8800, and uses read-only
mounts for the legacy Telegram configuration, watcher script, and ComfyUI
output folders. Its writable `data/` holds the managed Telegram configuration,
sent-file fingerprints, log, and enabled setting.

The existing host user service and this container must not run concurrently.
On migration, copy `.telegram-comfyui-state.json` and
`.telegram-comfyui-state-h3.json` from the Desktop script folder into
`connections/data/telegram-state.json` and `telegram-state-h3.json` before
starting this service. Both ComfyUI output directories are mounted at their
original absolute paths, so those fingerprints stay valid. Stop and disable
`comfyui-telegram.service` before starting Connections. Never use the watcher's
`--send-existing` option during migration.

Run `docker compose up -d --build connections` from BunnyBytes. The gateway
proxies `/Connections/` after a normal site login. `GET /Connections/api/status`
returns configuration and process status without credentials. Start/Pause
buttons call the service's JSON API through the gateway; requests need the
`X-Connections-Action: 1` header. `/health` is the local container check.

## Cloudflare tunnels

Connections manages local cloudflared processes without Docker socket access.
The existing `byte` tunnel (your hostname to the local gateway) was imported
from `cloudflared/config.yml` and is now run by Connections. The former
`bunny-cloudflared` container is stopped and assigned the `legacy-tunnel`
Compose profile for rollback. Telegram credentials are editable from the
Connections page. The old host-side `.telegram-comfyui.json` is imported once;
the managed copy is stored owner-only under `connections/data/`.

The UI shows routes and live edge connections, requests, and errors. It can
start, stop, restart, and edit routes for managed tunnels. Ingress rules are
validated with `cloudflared tunnel ingress validate` before changes are
applied. Restarting or stopping a tunnel briefly disconnects public access;
local access through port 8080 remains available.

To add a tunnel, import its credential JSON and hostname routes, or use a
Cloudflare API token plus account ID and zone ID to create a locally managed
tunnel and proxied CNAME records. The API token is used for that request only
and is not saved. For imported tunnels, create proxied CNAME records pointing
to `<tunnel-id>.cfargotunnel.com` in Cloudflare DNS. If you add a hostname to
an existing tunnel, update its DNS record separately.
