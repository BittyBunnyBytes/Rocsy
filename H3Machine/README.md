# H3 Machine

H3 Machine is a browser-based control surface for local or remote MiniMax H3 video generation. A FastAPI server owns uploads, prompt enhancement, ComfyUI workflow creation, queue monitoring, persisted job history, and safe output delivery. The frontend is semantic HTML, CSS, and vanilla JavaScript; it has no desktop GUI or filesystem assumptions.

Run `./run.sh` to start both H3 Machine and its existing Cloudflare tunnel. The
app is then available at `https://rocsy.example.com` and locally at
`http://localhost:8000`.

The launcher expects `cloudflared` to be installed and authenticated for the
named tunnel `byte`. Set `H3_CLOUDFLARE_ENABLED=0` for local-only startup, or
override `H3_CLOUDFLARE_TUNNEL` and `H3_CLOUDFLARE_ORIGIN` when using a
different tunnel or port. If either the app or tunnel exits, the launcher stops
the other process as well.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run.sh
```

Copy `.env.example` to `.env` if your launcher loads env files, or export the variables in your shell. Uvicorn itself does not automatically read `.env` unless started with `--env-file .env`.

The model selector supports MiniMax H3 and LTX 2.5. Their included workflows
are `workflows/MiniMax-H3.json` and
`workflows/LTX-2.5-Final-Optimized.json`. The LTX workflow is based on the
local `LTX25 Final Optimized.json` workflow and exposes its Low, Medium, and
Large tiled VAE decode presets plus its Stage 2 Speed/Quality choice.

Only one model family can be active or queued at a time. Switching is rejected
until the current family finishes or is cancelled, and ComfyUI's cached models
are unloaded before the next family is submitted. This prevents MiniMax H3 and
LTX 2.5 from occupying the generation pipeline together.

Uploaded images are kept under `H3_UPLOAD_DIR` and copied to
`H3_INPUT_DIR/H3MachineRefs` before a workflow is submitted. `H3_INPUT_DIR`
must be the same ComfyUI input root, or a shared/mounted equivalent visible to
ComfyUI. Generated media under `H3_OUTPUT_DIR` is served only when it is
associated with a recorded job.

Job and reference metadata is stored in SQLite at `H3_DATA_DIR/h3-machine.sqlite3`, so history survives browser refreshes and backend restarts. H3 Machine consumes ComfyUI's WebSocket progress events and persists each rendering job's percentage; live service, percentage, and generation changes are delivered to browsers with Server-Sent Events at `/api/events`.

Completed, failed, and cancelled history survives restarts. Jobs still marked as
preparing, queued, rendering, or cancelling are removed whenever H3 Machine
starts or shuts down so stale queue entries are not restored in the browser.

The browser plays a short two-tone ping when a video completes. Use the
**Alerts** button in the top bar to enable or disable completion sounds and to
grant desktop-notification permission. Existing completed jobs do not trigger
alerts when the page loads or reconnects.

## Service modes

ComfyUI and Qwen default to `external` mode. In that mode H3 Machine only checks and calls their HTTP APIs. It never sends process signals to an external service.

For a bare-metal service that H3 Machine may launch itself, set the corresponding mode to `local`:

```bash
export H3_COMFY_MODE=local
export H3_COMFY_COMMAND='/opt/ComfyUI/.venv/bin/python /opt/ComfyUI/main.py --listen 0.0.0.0 --port 8188'
export H3_QWEN_MODE=local
export H3_QWEN_COMMAND=/etc/h3-machine/qwen-command.txt
```

Only a process started by this H3 Machine process is eligible for termination. Before a render, a locally managed Qwen process is stopped to release GPU memory. Remote services are never killed. H3 Machine remains GPU-agnostic; CUDA/ROCm dependencies belong to ComfyUI and Qwen.

## Docker

```bash
cp .env.example .env
docker compose up --build -d
```

The Compose file runs H3 Machine only. It does not bundle ComfyUI or Qwen. On Linux, `host.docker.internal` is mapped to the host gateway, so the default Compose URLs can reach host-side services. For another container, set the URL to that service name on a shared Docker network. For another machine, use its reachable HTTP address.

Mounted storage:

| Host | Container | Purpose |
|---|---|---|
| `./uploads` | `/data/uploads` | Original browser uploads |
| `./input` | `/data/input` | ComfyUI input staging |
| `./Output` | `/data/output` | Generated media |
| `./data` | `/data/app` | SQLite history |
| `./logs` | `/data/logs` | Managed-service logs |
| `./workflows` | `/app/workflows` | MiniMax H3 workflow (read-only) |

When ComfyUI is outside the H3 Machine container, both processes must see compatible input and output mounts. Point their input/output directories at the same host directories or shared volumes.

## Main API

- `GET /api/health`, `/api/status`, `/api/settings/options`
- `GET|POST /api/references`, `DELETE /api/references/{id}`
- `POST /api/resolution`, `/api/generate`
- `POST /api/qwen/start`, `/api/qwen/enhance`
- `GET /api/jobs`, `/api/jobs/{id}`, `POST /api/jobs/{id}/cancel`, `DELETE /api/jobs/{id}`, `/api/events`
- `GET /api/outputs/{job_id}/stream`, `/api/outputs/{job_id}/download`

Uploads are size-limited, extension/MIME checked, image-decoded, and stored under UUID names. Generation values are validated with Pydantic allowlists and bounds. Browser clients cannot choose service URLs, workflow JSON, shell commands, filesystem paths, or output paths.

## Alias behavior

- Reference mode: each uploaded reference gets a canonical MiniMax H3 token - `<Picture N>` for images, `<Video N>` for videos, `<Audio N>` for audio - shown right on its reference card and typed directly into the prompt.
- The number in a reference's token is stable for as long as it exists - deleting `<Picture 2>` never changes what an existing `<Picture 3>` in your prompt refers to. Python re-maps each token to whatever contiguous position MiniMax H3's Ref2V node actually needs (1..N with no gaps) when the workflow is built, so the prompt text never has to change.
- Keyframe mode: `@image`, `@image1`, and `@First` target the first frame; `@image2` and `@Last` target the optional last frame. This mode has no reference pool, so it keeps the plain `@word` convention rather than canonical tokens.

The browser offers autocomplete (`<` for reference tokens, `@` for Keyframe mode's first/last words), but Python performs the authoritative translation while building the workflow.

### Agentic continuation and web tools

Agentic validates each Hermes `::VIDEO` plan before submitting video jobs. If the plan is malformed or the initial writing is empty or truncated, Hermes receives the exact error in a new GPU queue turn and keeps revising until the full plan validates or the session is cancelled. A separate Hermes plan-review turn then checks the requested clip count and order, H3 syntax, selected references, continuation sources, and intent. If it revises the plan, parsing and review repeat before any render enters the GPU queue. Both stages stream text and thinking to the same Hermes Agent log.

An Agentic `::VIDEO` plan may continue any earlier clip in that plan: `::VIDEO id=3 action=EXTEND from=1 mode=reference duration=5 refs=inherit exclude=- frame=previous` continues clip 1 even if clip 2 is a cutaway. When resuming a saved Agentic session, `from=session:1` selects the first completed final clip across prior runs; the planner receives a numbered list of available final clips. `from=0` selects the latest completed clip. Staging previews and failed attempts are never historical sources. EXTEND extracts and uploads only the selected source clip's final frame as a Reference image, binds `{{CONTINUATION_FRAME}}`, and uses `[reference generation]` with detailed retention analysis of what the source frame shows and the intended next action. The generated source video is never attached as a video reference. Each Agentic clip sends only the image, video, and audio references selected by its `refs=` field; unused private uploads are removed when a run ends or the service starts. Render references remain through the retry window, while original references needed by saved sessions remain for resume. Expired private render references are swept automatically. Agentic alias numbers ignore unrelated Keyframe and Post-Edit assets and reuse free numbers.

When staging is enabled, an optional approval gate pauses after the latest previews finish and releases the GPU queue. The striped Staging previews category has a notes field and Accept/Exclude controls. Accept applies the notes through Hermes's footage review before the final preset runs; Exclude asks Hermes to revise the staging prompts and regenerates previews with the staging preset and a new seed. The pending decision and previews survive an H3 service restart.

Hermes settings offer separate opt-in controls for public web research and agent-managed reference downloads. The planner can request bounded searches, page reads, and direct image, video, or audio downloads. It can crop images and videos with normalized `[x,y,width,height]` coordinates and trim video or audio with `[start,end]` seconds. Downloaded files use the same reference validation and H3 generation limits as manual references. Only public HTTP(S) addresses are reachable; private and local addresses are rejected. This is the H3 planner's web reader, separate from the Hermes CLI's interactive `/browser connect` connection to a live Chromium browser.

The **Final video generation preset** selector in Hermes settings applies a saved MiniMax H3 preset to every final Agentic render. Selecting **Use current Studio settings** uses the generation controls present when the run starts. Staging has its own optional preset.
