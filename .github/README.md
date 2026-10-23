# Rocsy

A self-hosted, password-gated AI studio for one machine with an AMD GPU (ROCm). One gateway
serves video, image, music and voice generation, LoRA training, a local LLM server and an
agent that drives all of them, and every GPU job goes through one shared queue.

> **v1.0.** Built and run daily on a single Linux box (RX 7900 XTX, ROCm). The installer sets
> it up on Arch Linux and Ubuntu; other setups are by hand. See [Known limits](#known-limits).

## What's in it

| Route | App | Source |
|---|---|---|
| `/` | Home page, Settings (themes, modules, services, notifications, Agentic) | `BunnyBytes/gateway` |
| `/Agent` | Master Agent: a Hermes agent that plans and runs jobs across the studios | `BunnyBytes/agent` |
| `/Vid` | Video (MiniMax H3, LTX 2.5) with Agentic mode and Post-Edit | `H3Machine` |
| `/Krea` | Image (Krea 2, Lustify, Qwen Image 2.1) | `Krea` |
| `/Mus` | Music (YuE2, MiniMax Music 3) | `Music` |
| `/Voice` | Text to speech with saved voices | `BunnyBytes/voice` |
| `/Trainer` | LoRA training (ROCm) | `Trainer` |
| `/Queue` | Shared GPU queue | `BunnyBytes/lock` |
| `/Models` | Model / LoRA downloads (Civitai, Hugging Face), hardware fit, llama.cpp builds | `BunnyBytes/modelhub` |
| `/REA` | Reverse Engineer Anything *(experimental)* | `BunnyBytes/rea` |
| `/Code` | Vibe Coding project library *(experimental)* | `BunnyBytes/gateway` |
| `/Llama` | llama-swap LLM server (llama.cpp, RDNA builds) | `Llama` |

Supporting services: `BunnyBytes/shared` (Agentic framework for Image/Music/Voice),
`BunnyBytes/motion` (Remotion renderer for the agent), `BunnyBytes/connections` (Telegram
output bot + Cloudflare tunnel), `BunnyBytes/hostctl` (host helper behind Settings → Services),
`BunnyBytes/supervisors` (ComfyUI supervisor).

## Layout

```
BunnyBytes/   gateway, agent, queue, models hub, voice, motion, REA, hostctl, docker-compose.yml
H3Machine/    video studio          Krea/   image studio       Music/   music studio
Trainer/      LoRA trainer          Llama/  llama-swap config + launcher
```

## Requirements

- Linux with Docker + Compose, an AMD GPU with ROCm (the stack is tuned for RDNA3)
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) on the host (port 8188), run by
  `BunnyBytes/supervisors/comfyui_supervisor.py`
- The models each studio uses (download them in `/Models`)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) checked out next to this repo as
  `hermes-rocsy/`, with `BunnyBytes/hostctl/patches/hermes/rocsy-addons.patch` applied (compose
  builds the `hermes-agent` image from it)

## Getting started

The easiest way is the pre-compiled installer attached to each [GitHub release](../../releases): extract it and
open `Install Rocsy.desktop` (or run `sh "Install Rocsy.sh"`). It sets everything below up on Arch Linux or Ubuntu. Its **What to install** section picks the optional
services (Motion; REA and the Trainer are off by default) and the core backends it sets up for you:
ComfyUI with PyTorch for ROCm and its supervisor, Hermes Agent, and yt-dlp. Anything skipped can be
installed later in Models → Manager → Core Backend. No desktop, or prefer a terminal? Run the bundle's
`sh "Install Rocsy.sh" --terminal` for the same questions in the terminal, or `--defaults` for the
default install without questions (`--help` lists the options). By hand:

1. Copy each `.env.example` to `.env` (`BunnyBytes/`, `H3Machine/`, `Krea/`, `Music/`) and fill in
   the secrets (`MASTER_PASSWORD`, `COOKIE_SECRET`, `QUEUE_INTERNAL_SECRET`, `LLAMA_API_KEY`,
   `HOSTCTL_SECRET`). `BunnyBytes/.env.example` explains how to generate each one.
2. In `BunnyBytes/.env`, set the folders (absolute paths):
   - `ROCSY_ROOT`: this checkout (the folder holding `BunnyBytes/`, `Llama/`, ...)
   - `ROCSY_COMFY_ROOT`: your ComfyUI folder
   - `ROCSY_HERMES_HOME`: optional, defaults to `~/.hermes`
3. `cp Llama/config.example.yaml Llama/config.yaml`. Optional: copy
   `BunnyBytes/docker-compose.override.example.yml` to `docker-compose.override.yml` for
   machine-specific mounts (extra model folders, an sd-scripts checkout, a hand-made tunnel).
4. `BunnyBytes/install-units.sh` installs the host helper, ComfyUI supervisor and Voice worker
   as systemd user units with your paths filled in (and writes `hostctl/hostctl.env`).
5. `cd BunnyBytes && docker compose up -d --build`, then open `http://127.0.0.1:8080` and log in
   with `MASTER_PASSWORD`.

REA and the Trainer are opt-in Compose profiles: add `rea` to `COMPOSE_PROFILES` in `BunnyBytes/.env`,
and start the Trainer with `docker compose --profile trainer up -d --build trainer` (its ROCm PyTorch
base image is a 20+ GB download).

The full first-run guide (tunnel, verification, going live) is in
[`BunnyBytes/README.md`](../BunnyBytes/README.md).

## Known limits

- No machine paths are hardcoded: `docker-compose.yml`, the host helper and the unit files
  read the folders from `BunnyBytes/.env`. Your own extra mounts belong in the git-ignored
  `docker-compose.override.yml`.
- One GPU is assumed to also drive the desktop. VRAM guards and the shared queue exist because
  running two GPU jobs at once crashed the display server.
- The llama.cpp launcher expects RDNA Boost builds under `ROCSY_ROOT` (`llama.cpp-rdna-boost/`,
  or ones built in Models → Manager → Core Backend).
- Public access goes through a Cloudflare tunnel created on the Connections page (or a
  hand-made one, see `BunnyBytes/cloudflared/README-manual-steps.md`). Rocsy has a single
  shared password; for internet exposure put Cloudflare Access in front of it as well.
- Some studios target specific model families (MiniMax H3, LTX 2.5, Wan 2.2, Krea 2,
  Qwen Image 2.1, YuE2). Download their weights in `/Models`; none ship with the repo.

## Not in this repo

Runtime data (sessions, jobs, outputs, thumbnails, downloaded LoRA galleries), model weights,
datasets, `.env` secrets, local backups and machine-local config (`Llama/config.yaml`,
`docker-compose.override.yml`, REA's folder list) are all git-ignored.

## License

Apache License 2.0, see [`LICENSE`](../LICENSE). Model weights, ComfyUI, Hermes Agent and other
third-party software you download for Rocsy keep their own licenses.
