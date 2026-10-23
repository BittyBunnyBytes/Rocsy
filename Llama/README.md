# Llama

A dockerized [llama-swap](https://github.com/mostlygeek/llama-swap) instance:
a small proxy that starts/stops the actual `llama-server` process on demand
based on the `model` field in each OpenAI-compatible request. This is the
"pick a model" surface for llama.cpp on this host -- picking one is an API
call or a click in the web UI, not a container rebuild.

The GPU workload itself is NOT containerized -- this image only runs
llama-swap. It execs the host's own custom RDNA-boost ROCm build of
llama.cpp (`~/AI/llama.cpp-rdna-boost`, see
`BunnyBytes/hostctl/build-rocm-flash-attention.sh`) via a read-only bind mount at the
exact same absolute path (its `RUNPATH` is baked in absolute, not relative),
plus a read-only bind mount of `/opt/rocm` so the container's ROCm userspace
runtime always matches this host's kernel driver exactly. `/dev/kfd` and
`/dev/dri` are passed through for compute access. This matches BunnyBytes' existing "GPU workloads stay bare-metal"
convention (see the top-level docker-compose.yml's header comment) while
still giving llama-swap's process manager / model picker a normal container.

## Which llama.cpp build runs

By default the built-in RDNA-boost build above. Models → Manager → **Core Backend** can
download newer llama.cpp releases, convert them to RDNA Boost (only releases the patch
series applies to cleanly) and build them into `~/AI/llama.cpp-builds/builds/<id>/build`
(mounted read-only at the same path). "Use this build" points
`~/AI/llama.cpp-builds/active` at one; `llama-launch` follows that link on every model
load, so switching never needs a restart. `LLAMA_SERVER_BIN` in `.env` still wins.

## Adding or changing models

Edit `config.yaml` (bind-mounted into the container, not baked into the
image) and `docker compose restart llama` -- no rebuild needed. Each entry
under `models:` needs a `cmd:` that execs `${llama_launch}` with its default
GPU-layer policy (`all` or `auto`) and fit margin (`none` or MiB), followed by
`--model /path/to.gguf` and `--port ${PORT}`; copy an
existing entry as a template. GGUF files under
`$ROCSY_COMFY_ROOT/models/llm` are already bind-mounted read-only at their
real host path -- anything you drop there is usable without a compose change.
Other model folders go in `BunnyBytes/docker-compose.override.yml`.

Only one model runs at a time (llama-swap's default "group" swap engine
unloads whatever was previously loaded before starting the next one).
`globalTTL: 1800` also
auto-unloads an idle model after 30 minutes so VRAM comes back for ComfyUI
or anything else.

## GPU placement and offloading

The default uses `ROCm0` alone. Multi-GPU is a per-model toggle: open the
model in the llama-swap UI → **Parameters** → **Multi-GPU** (off by default).
When it's on, llama-launch loads that model on `LLAMA_MULTI_GPU_DEVICES`
(default `ROCm0,ROCm1`) the next time it starts. `LLAMA_GPU_DEVICES` in
`BunnyBytes/.env` sets the single-GPU default for every model. GPU 0 must
remain first, and `--main-gpu 0` keeps it as the primary device.

Optional `.env` controls:

| Variable | Effect |
|---|---|
| `LLAMA_SPLIT_MODE` | `layer` (default), `row`, `tensor`, or `none` (GPU 0 only) |
| `LLAMA_TENSOR_SPLIT` | Relative shares in device order, such as `3,1`; blank lets llama.cpp choose |
| `LLAMA_GPU_LAYERS` | Exact number, `all`, or `auto`; blank retains each model's default (`all` for smaller models, `auto` for Flash) |
| `LLAMA_FIT_TARGET` | Per-GPU free-memory margin in MiB, such as `4096,2048`; blank retains the model default (Flash: 2560 or 4096 MiB) |
| `LLAMA_CPU_MOE_LAYERS` | Number of initial MoE expert layers to keep on CPU; blank uses llama.cpp's default |
| `LLAMA_KV_OFFLOAD` | `1` (default) keeps KV cache offloading on, `0` keeps KV cache on CPU |

For example, turning Multi-GPU on with `LLAMA_TENSOR_SPLIT=3,1` and
`LLAMA_GPU_LAYERS=40` uses both GPUs with GPU 0 assigned the larger share and
at most 40 model layers in VRAM. Changing `.env` settings requires recreating
the container; Parameters-tab changes apply on the model's next load.

**`qwen-27b` shares a GPU with the native Qwen supervisor already running on
`127.0.0.1:8081`** (same model + flags, see `qwen_supervisor.py`) -- don't
load both at once, a 24GB card can't hold two ~16GB models plus context.

## Reaching it

| From | URL | Auth |
|---|---|---|
| This host | `http://127.0.0.1:8400/v1/...` | `Authorization: Bearer $LLAMA_API_KEY` |
| Anywhere (incl. Hermes on another machine) | `https://rocsy.example.com/Llama/v1/...` | same bearer key |
| Model-picker UI | `https://rocsy.example.com/Llama/ui/` | a logged-in site session, or the bearer key |

`LLAMA_API_KEY` is set once in `BunnyBytes/.env` and is checked twice: by
llama-swap itself (`apiKeys:` in `config.yaml`) and by the gateway (as an
alternative to a logged-in browser session on `/Llama/*`, since a headless
Hermes agent can't do the cookie-login flow). Both must always match.

The public path only works because the gateway strips the `/Llama` prefix
before forwarding (`strip_prefix=True` in `gateway/proxy.py`) -- llama-swap,
unlike this repo's own FastAPI apps, has no concept of a mount prefix.

Hermes is already configured with `llama-local`, `llama`, `llama-gemma12`,
`llama-gemma31`, and `llama-qwen` model aliases in `~/.hermes/config.yaml`
(see that file's Model Aliases section) -- `hermes model` or `/model` to
switch, or edit an alias's `model:` field to point at a different
`config.yaml` entry here.

## Ports

- `8400` -- llama-swap's own listen port (`LLAMA_PORT` in `.env`)
- `9100+` -- ephemeral ports llama-swap assigns to whatever `llama-server`
  it's currently running (`startPort` in `config.yaml`)

### Strata model

`strata-qwen-flash-next` delegates start/stop to the authenticated host helper through
`strata-swap-bridge`, then proxies Strata's OpenAI-compatible server on loopback port 9101.
Install a Strata model in Models → Manager → Core Backend → Inference Engines before
loading this llama-swap entry. Its browser UI is available from the model's Strata UI tab.
