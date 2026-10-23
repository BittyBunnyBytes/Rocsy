# Rocsy stack (`BunnyBytes/`)

`docker-compose.yml` in this folder runs the whole Rocsy stack: one password-gated gateway on
`127.0.0.1:8080` in front of every studio and service. Every container uses host networking
and binds to loopback only. Public access, if you want it, goes through a Cloudflare tunnel.
The overview and the route table are in the [repo README](../.github/README.md).

## Before first start

1. **Docker** with the Compose and Buildx plugins, the daemon running, your account in the
   `docker` group. (The installer does all of this for you on Arch and Ubuntu.)
2. **`.env`:** `cp .env.example .env`, then fill in `MASTER_PASSWORD`, `COOKIE_SECRET`,
   `QUEUE_INTERNAL_SECRET`, `LLAMA_API_KEY` and `HOSTCTL_SECRET` (the comments in the file show
   how to generate each one), plus the folders `ROCSY_ROOT` and `ROCSY_COMFY_ROOT`. Compose
   refuses to start while a required value is empty. The studios (`../H3Machine`, `../Krea`,
   `../Music`) each have their own `.env.example` too.
3. **Llama config:** `cp ../Llama/config.example.yaml ../Llama/config.yaml`.
4. **Host services:** `./install-units.sh` installs `bunny-hostctl` (the host helper behind
   Settings → Services, port 8899), the ComfyUI supervisor and the Rocsy Voice worker as
   systemd user units. It also writes `hostctl/hostctl.env` from `HOSTCTL_SECRET`. Start them
   with the command it prints.
5. **ComfyUI** runs natively on the host, not in Docker. The studios reach it at `COMFY_URL`
   (default `http://127.0.0.1:8189`), and the supervisor switches it between the per-studio
   profiles.
6. **Hermes:** clone [Hermes Agent](https://github.com/NousResearch/hermes-agent) next to this
   repo as `../hermes-rocsy`, then apply `hostctl/patches/hermes/rocsy-addons.patch`.

## Bring the stack up

```
docker compose up -d --build
```

Then open `http://127.0.0.1:8080` and log in with `MASTER_PASSWORD`.

Two services are opt-in Compose profiles:
- **REA** (experimental): add `rea` to `COMPOSE_PROFILES` in `.env`, or install it from Models →
  Manager → Core Backend, which does that for you.
- **Trainer**: its `rocm/pytorch` base image is a download of 20+ GB, and training competes with the
  studios for VRAM. Start it with `docker compose --profile trainer up -d --build trainer`.

Machine-specific additions go in `docker-compose.override.yml` (git-ignored; start from
`docker-compose.override.example.yml`). Examples: extra GGUF folders, an sd-scripts checkout
for the Trainer, a hand-made Cloudflare tunnel. REA's game folders are written into the same file by the REA page.

## Verify locally

- `curl -sI http://127.0.0.1:8080/` redirects to `/login` when you have no cookie.
- After you log in, `/Vid`, `/Krea`, `/Mus`, `/Voice`, `/Agent` and `/Models` each load with
  no 404s in the browser console.
- Two GPU jobs from different studios queue one after the other: check `/Queue`.
- `ss -tlnp`: every Rocsy port is bound to `127.0.0.1`, never `0.0.0.0`.

## Going public (optional)

Create a tunnel on the **Connections** page (`/Connections/`). It stores the credentials
under `connections/data/` and runs `cloudflared` for you. To adopt a tunnel you made by hand
with `cloudflared tunnel create`, see `cloudflared/README-manual-steps.md` and the
`connections:` example in `docker-compose.override.example.yml`. Set `ROCSY_PUBLIC_URL` in
`.env` to the public address (browser push notifications use it as their contact).

Before you expose it, set a long passphrase in Settings → Security. Consider putting Cloudflare
Access in front of the hostname as well.

## Services in this folder

| Folder | Service |
|---|---|
| `gateway/` | login, home page, Settings, reverse proxy for every app, installer compiler |
| `lock/` | shared GPU queue (`/Queue`); every GPU job takes a slot here |
| `agent/` | Master Agent (`/Agent`), Hermes runs that drive the studios |
| `shared/` | framework service on `127.0.0.1:8700`: Agentic mode and media operations for the studios ([README](shared/README.md)) |
| `modelhub/` | Models hub (`/Models`): downloads, hardware fit, Core Backend builds |
| `voice/` | Rocsy Voice (`/Voice`) + the host-side GPU worker |
| `motion/` | Remotion renderer the agent uses for edits |
| `rea/` | Reverse Engineer Anything (`/REA`) |
| `connections/` | Telegram output bot + Cloudflare tunnels (`/Connections`, [README](connections/README.md)) |
| `hostctl/` | host helper for Settings → Services (systemd user unit, not a container) |
| `supervisors/` | ComfyUI supervisor (systemd user unit) |
| `cli/` | the `rocsy` terminal command |

## Operations

- **Stop everything:** `docker compose down`
- **Rebuild one app** after an edit: `docker compose up -d --build <service>`. Wait until
  `/Queue` is idle first, because a rebuild drops any running job.
- **Rotate the password:** use Settings → Security, or change `MASTER_PASSWORD` and run
  `docker compose up -d gateway`. Rotate `COOKIE_SECRET` as well to sign everyone out.
- **Data:** every app keeps its state in bind-mounted `data/`, `logs/` and `Output/` folders
  on the host, so you can back them up directly. They are git-ignored.
