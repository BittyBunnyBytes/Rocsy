#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")" && pwd)"

# Keep machine-specific service addresses in .env. Uvicorn does not load this
# file itself, so load it before resolving the launcher defaults below.
if [[ -f "$project_dir/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$project_dir/.env"
  set +a
fi

cd "$project_dir"

host="${H3_HOST:-0.0.0.0}"
port="${H3_PORT:-8000}"
tunnel_name="${H3_CLOUDFLARE_TUNNEL:-rocsy}"
tunnel_url="${H3_CLOUDFLARE_ORIGIN:-http://127.0.0.1:${port}}"

if [[ "${H3_CLOUDFLARE_ENABLED:-1}" != "1" ]]; then
  exec "$project_dir/.venv/bin/uvicorn" app:app --app-dir "$project_dir" --host "$host" --port "$port" --timeout-graceful-shutdown 10
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is required when H3_CLOUDFLARE_ENABLED=1" >&2
  exit 1
fi

server_pid=""
tunnel_pid=""

cleanup() {
  trap - EXIT INT TERM
  [[ -n "$tunnel_pid" ]] && kill "$tunnel_pid" 2>/dev/null || true
  [[ -n "$server_pid" ]] && kill "$server_pid" 2>/dev/null || true
  wait "$tunnel_pid" "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"$project_dir/.venv/bin/uvicorn" app:app --app-dir "$project_dir" --host "$host" --port "$port" --timeout-graceful-shutdown 10 &
server_pid=$!

cloudflared tunnel --url "$tunnel_url" run "$tunnel_name" &
tunnel_pid=$!

wait -n "$server_pid" "$tunnel_pid"
status=$?
exit "$status"
