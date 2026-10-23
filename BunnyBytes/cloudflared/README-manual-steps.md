# Adopting a hand-made Cloudflare tunnel

You normally don't need this file: create a tunnel on the **Connections** page
(`/Connections/`) and Rocsy keeps its credentials and runs `cloudflared` for you.

Use these steps only when you already have a tunnel made with the `cloudflared` CLI and
want Connections to run it. Run them on the host. Step 2 needs a browser login to your
Cloudflare account.

1. Install `cloudflared` on the host (`sudo pacman -S cloudflared`, or your distro's package).
2. Log in. This writes `~/.cloudflared/cert.pem`:
   ```
   cloudflared tunnel login
   ```
3. Find your tunnel with `cloudflared tunnel list`. Create one only if it isn't there:
   `cloudflared tunnel create <name>`. Recreating a tunnel that still exists orphans its DNS
   route.
4. Write its credentials file:
   ```
   cloudflared tunnel token --cred-file ~/.cloudflared/<name>.json <name>
   ```
5. Point your hostname at it (safe to run again):
   ```
   cloudflared tunnel route dns <name> rocsy.example.com
   ```
6. Copy the credentials into this folder (`*.json` here is git-ignored, so never commit it):
   ```
   cp ~/.cloudflared/<name>.json cloudflared/<name>.json
   cp cloudflared/config.example.yml cloudflared/config.yml   # then set tunnel: and hostname:
   ```
7. Mount both into the `connections` service. The commented `connections:` block in
   `docker-compose.override.example.yml` shows how. Then run
   `docker compose up -d connections`.
8. On `/Connections/` the tunnel shows up as an imported tunnel. Enable it there, then load
   your hostname from outside your LAN and check that login works end to end.
