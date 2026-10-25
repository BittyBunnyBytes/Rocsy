<p align="center">
  <img src="https://raw.githubusercontent.com/BittyBunnyBytes/Rocsy/main/BunnyBytes/gateway/static/icons/rocsy-v2.svg" alt="Rocsy logo" width="128" height="128">
</p>

<h1 align="center">Rocsy</h1>

<p align="center"><b>AI on AMD, made easier.</b></p>

<p align="center">
  <a href="https://github.com/BittyBunnyBytes/Rocsy/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/BittyBunnyBytes/Rocsy?style=flat-square&color=e9a04f"></a>
  <a href="https://github.com/BittyBunnyBytes/Rocsy/blob/main/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue?style=flat-square"></a>
  <img alt="Platform: Linux" src="https://img.shields.io/badge/platform-Linux-informational?style=flat-square&logo=linux&logoColor=white">
  <img alt="GPU: AMD ROCm" src="https://img.shields.io/badge/GPU-AMD%20ROCm-ED1C24?style=flat-square&logo=amd&logoColor=white">
  <img alt="Docker Compose" src="https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white">
  <img alt="Python" src="https://img.shields.io/badge/Python-3-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="ComfyUI" src="https://img.shields.io/badge/ComfyUI-backend-555?style=flat-square">
  <img alt="llama.cpp" src="https://img.shields.io/badge/llama.cpp-LLM-555?style=flat-square">
</p>

<p align="center">
  <a href="https://github.com/BittyBunnyBytes/Rocsy/releases/latest"><img alt="Download the installer" src="https://img.shields.io/badge/Download-the%20installer-e9a04f?style=for-the-badge&logo=github&logoColor=white"></a>
  &nbsp;
  <a href="https://ko-fi.com/bittybunnybytes"><img alt="Support me on Ko-fi" src="https://img.shields.io/badge/Donate-Ko--fi-FF5E5B?style=for-the-badge&logo=ko-fi&logoColor=white"></a>
</p>

<p align="center">Or install from a terminal, no manual download (Linux):</p>

```sh
curl -fsSL https://rocsy.rocks/install.sh | sh
```

<p align="center"><sub>Fetches the latest installer, checks its SHA-256 and asks the setup questions in the terminal.
It asks whether you want a password and lets you type it. Add <code>-s -- --defaults</code> after <code>sh</code> for the default
install with no questions, or <code>-s -- --no-password</code> to install without a password (trusted machines only).</sub></p>

A self-hosted, password-gated AI studio for one machine with an AMD GPU (ROCm). One gateway
serves video, image, music and voice generation, LoRA training, a local LLM server and an
agent that drives all of them, and every GPU job goes through one shared queue.

> **v1.1.** Built and run daily on a single Linux box (RX 7900 XTX, ROCm). The installer sets
> it up on most Linux distros (APT, DNF, Zypper, pacman, APK, XBPS, eopkg, Portage; tested on Arch
> Linux); immutable systems need a few manual steps. **Windows support is untested**
> (see [Windows](#windows-untested)). See [Known limits](#known-limits).

## What's in it

| Route | App | Source |
|---|---|---|
| `/` | Home page, Settings (themes, modules, services, notifications, Agentic) | `BunnyBytes/gateway` |
| `/Agent` | Master Agent: a Hermes agent that plans and runs jobs across the studios | `BunnyBytes/agent` |
| `/Video` | Video (MiniMax H3, LTX 2.5, Wan 2.2) with Agentic mode and Post-Edit | `Video` |
| `/Image` | Image (Krea 2, MOSS-Image, Qwen Image 2.1) | `Krea` |
| `/Music` | Music (YuE2, MiniMax Music 3) | `Music` |
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
Video/        video studio          Krea/   image studio       Music/   music studio
Trainer/      LoRA trainer          Llama/  llama-swap config + launcher
```

## Requirements

- Linux with Docker + Compose, an AMD GPU with ROCm (the stack is tuned for RDNA3). Windows 10/11
  through WSL2 is supported by the installer but untested (see [Windows](#windows-untested))
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
default install without questions; `--no-password` installs without a password (`--help` lists the options). By hand:

1. Copy each `.env.example` to `.env` (`BunnyBytes/`, `Video/`, `Krea/`, `Music/`) and fill in
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

## Windows (untested)

**Nothing here has been tried on a real Windows PC yet.** Expect rough edges, and include
`install.log` from the Rocsy folder when you report a problem.

Extract the installer (`tar -xf rocsy-…-installer.tar.gz` in a terminal, or Explorer on
Windows 11) and run **`Install Rocsy (Windows).cmd`**. It reads the Windows version and:

| Windows | What the installer does |
|---|---|
| 11, and 10 version 2004 (build 19041) or newer | Runs Rocsy inside **WSL2** (Ubuntu). It installs WSL2 + Ubuntu 24.04 if missing (admin rights and a restart), turns on systemd, installs Docker Engine, Compose and Buildx in the distro as root, then runs the Linux installer there. Containers that use the GPU get WSL's `/dev/dxg` instead of `/dev/kfd`/`/dev/dri` (`BunnyBytes/docker-compose.wsl.yml`, generated). Rocsy shortcuts on the Desktop and Start menu start WSL and the containers, then open Rocsy in an Edge app window; "Start with Windows" is optional. |
| 10 before version 2004, 8.1, 8, 7 | Can't run WSL2 or Docker, so Rocsy can't run there. The installer offers a shortcut to a Rocsy server elsewhere on your network instead (Firefox 115 ESR is the last Firefox for Windows 7/8/8.1). |

For the GPU, install the current AMD Windows driver, and AMD's
[ROCm for WSL](https://rocm.docs.amd.com/projects/radeon/en/latest/docs/install/wsl/install-radeon.html)
inside the Ubuntu distro, then run the installer again and pick Repair. ComfyUI's PyTorch is switched to
the ROCm-for-WSL runtime automatically when it's there. GPU tuning
and other host tools are Linux-only. Settings → Services shows the Windows version with an
"untested" note.

## Known limits

- No machine paths are hardcoded: `docker-compose.yml`, the host helper and the unit files
  read the folders from `BunnyBytes/.env`. Your own extra mounts belong in the git-ignored
  `docker-compose.override.yml`.
- One GPU is assumed to also drive the desktop. VRAM guards and the shared queue exist because
  running two GPU jobs at once crashed the display server.
- The LLM server needs a llama.cpp build from Models → Manager → Core Backend (RDNA Boost or
  stock, for any GPU target). Until you pick one with "Use this build", the launcher uses the newest
  installed build; with none installed, loading a model says so instead of failing silently.
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
