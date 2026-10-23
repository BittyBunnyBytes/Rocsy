# BunnyBytes internal capability framework

This service gives every app and agent in the Docker stack a stable place for
reusable operations and a catalog of the stack services. It starts with media
operations already needed across the studios: probe, trim, crop, extract audio,
and thumbnail. Add new capabilities
as versioned `/v1/...` routes and list them in `CAPABILITIES` in `app.py`.

The merged LoRA gallery metadata for Video and Krea Image is exposed at
`GET /v1/lora-gallery/h3`, `/v1/lora-gallery/krea`, and
`/v1/lora-gallery/all` (the latter returns both keyed catalogs). The framework
reads built-in indexes from read-only mounts and merges each studio's uploaded
LoRAs and edited metadata from its live API. `GET /v1/lora-gallery/{studio}/state`
returns model catalogs, aliases, and managed LoRAs together. Studio backends
proxy the catalog to their browsers, preserving the internal-secret boundary;
each studio serves its own preview media and applies only compatible LoRAs.
Built-in entries remain available while a studio is restarting.

## Shared studio UI

`ui/h3-studio.css` and `ui/h3-studio.js` hold the common Video look and
mobile Create/Generations navigation used by Music and Krea Image. On desktop,
the generation panel scrolls within the viewport, with 12-card batches loaded
as the list nears its end; phones scroll with the page. Compose
mounts these files read-only into each studio's static directory. The studios
load their own CSS first, then the shared layer, so their model and media
controls stay app-specific. Update the common shell, colors, buttons, form
fields, cards, gallery layout, or mobile tabs in these framework files to update
both studios. Each studio retains its own 12-card page limit and Show more
button as a keyboard-accessible fallback.

### References card

`window.BunnyReferences` in `ui/h3-studio.js` (styles in `ui/h3-studio.css`,
scoped to `body.h3-studio-ui`) is Video's References card as a shared
component: `render(list, items)` draws H3's rows (thumbnail, token chip, meta
line, studio extras, remove), `renderTotals`, `bindList` (insert token / view /
remove), `bindDropZone` and `insertToken`. The studio keeps its own state and
uploads. Image (Krea) uses it for a `details.card.card-inputs#reference-panel`
placed directly under the prompt, the same spot as H3. Load `h3-studio.js`
before the studio's module script so the component exists at startup.

Video also mounts `ui/h3-studio.js` for the shared LoRA gallery media
loader. H3 and Krea Image use it to fetch card previews near the viewport,
load the selected detail preview immediately, and draw repeated play icons
from one inline SVG symbol. This keeps the gallery responsive without eagerly
requesting every thumbnail when it opens.
H3 also mounts the shared CSS for the phone gallery layout. On phones the
gallery keeps search visible, expands filters and upload tools from Options,
and places editing controls before long descriptions. Both studios use the
same touch sizes and scrolling rules for preview, detail, and category views.

The service binds `127.0.0.1:8700` through the stack's host network. It is **not**
proxied by the public gateway or Cloudflare. Every capability route requires
`X-Queue-Internal-Secret`, using the existing `QUEUE_INTERNAL_SECRET` from
`BunnyBytes/.env`. `/health` is the only unauthenticated route. The service
never accepts host file paths; callers upload media and receive a result, so
containers do not need shared writable volumes or path translations.

## Discover and call

From a studio container that has `QUEUE_INTERNAL_SECRET`, or from the host
with that secret, call:

```bash
curl -H "X-Queue-Internal-Secret: $QUEUE_INTERNAL_SECRET" \
  http://127.0.0.1:8700/v1/capabilities

curl -f -H "X-Queue-Internal-Secret: $QUEUE_INTERNAL_SECRET" \
  -F 'file=@clip.mp4' -F 'start=2' -F 'end=7' \
  http://127.0.0.1:8700/v1/media/trim -o trimmed.mp4

curl -f -H "X-Queue-Internal-Secret: $QUEUE_INTERNAL_SECRET" \
  -F 'file=@frame.png' -F 'x=0.1' -F 'y=0.1' -F 'width=0.8' -F 'height=0.8' \
  http://127.0.0.1:8700/v1/media/crop -o cropped.png
```

The capability response also lists the internal URLs and main entrypoints of
Vid, Mus, Krea, Trainer, and the GPU queue. Those services retain their
existing APIs and service-specific policies.

All media routes use multipart form uploads. `probe` returns JSON; the other
routes return the resulting file. Trim times and thumbnail time use seconds.
Crop coordinates use fractions from 0 to 1. Invalid media or parameters return
HTTP 422; oversized uploads return 413; wrong secret returns 403. Video output
is H.264/AAC MP4, audio output AAC M4A, cropped images PNG, thumbnails JPEG.
The maximum upload is 250 MiB by default (`FRAMEWORK_MAX_UPLOAD_MB`); at most
2 operations run concurrently (`FRAMEWORK_MAX_PARALLEL`).

Service-specific rules remain in the calling app. For example, Video's
15-second reference limit and its GPU render pipeline are domain policy, not
generic trim behavior. Call this service for new general media tools instead
of adding another ffmpeg wrapper in a studio. Existing specialized pipelines
can move here when their output contracts have been verified.

## Add an operation

1. Implement a path-independent function in `operations.py` using argument
   arrays for subprocesses and writing only into the supplied scratch path.
2. Add a `/v1/...` route in `app.py` with the internal-secret dependency,
   bounded upload and concurrency handling, and scratch cleanup.
3. Add the operation to `CAPABILITIES` and cover it with a media integration
   test. Keep service-specific authentication and state in the caller.

Run tests with `python3 -m unittest discover -s tests` in this directory (needs
`ffmpeg` and `ffprobe`). Start with `docker compose up -d --build framework` from
`BunnyBytes`; inspect `docker compose logs framework` if `/health` is unavailable.

## Agentic generation sessions

Image, Music, and Voice expose **Agentic mode** beside their ordinary Generate control,
with the same controls as Video's Hermes settings. The studio proxies one guarded
route (`/api/agentic/{models|settings|guides|preview|sessions…}`) to this internal
service, so the browser never receives `QUEUE_INTERNAL_SECRET`. All UI lives in
`agentic-ui.js` / `agentic-ui.css` (mounted read-only into each studio):

- **Studio panel**: Agentic switch, Hermes guide, "Wait for my approval", recent
  sessions with inline Approve / Revise, and a gear that opens the settings tab.
- **Prompt syntax** box under the studio prompt (Image prompt, Music lyrics, Voice
  script): the "Quick syntax" bullets of the guide for the model currently loaded.
  It switches as soon as the model changes (Krea 2 — any checkpoint, finetunes included —
  / MOSS-Image / Qwen Image 2.1 / Anima; YuE2 / MiniMax Music 3; Qwen3-TTS / IndexTTS2 / Fish S2 Pro / Fish 1.5).
- **Hermes settings tab** (gear, or `#hermes`): Llama model, thinking level, context
  size (read-only; shared with H3 via `~/AI/Llama/overrides/<model>.ctx`), sampling and
  extra llama JSON; run switches (plan review, approval default, output review, model
  guide, attempts 1-3); the per-model **Model guides** editor (tabs, the loaded one
  marked, edits override the shipped file, empty/Reset returns to it); task and review
  prompts; Preview of the exact planning messages.
- **Hermes terminal**: H3's log (❯ user, ┊ ⚙ tool, ◇ thinking, ☤ answer streamed live,
  ✓/⚠/✗), session picker, Cancel, delete, and a steer box with `/` commands:
  `/status /plan /approve [notes] /revise <notes> /retry [notes] /stop /help`. Plain text
  steers the next Hermes step while running, revises a plan waiting for approval, and
  continues a finished session as a new linked session.

Model guides are `guides/<studio>/<model>.md`, written from each vendor's official
docs (sources listed at the bottom of every guide; re-check them when a model updates).
Guides are mounted at `/app/guides`, so edits apply
without a rebuild. The whole file goes into Hermes' system prompt (and the plan review)
for the model the studio has loaded; its `## Quick syntax` bullets feed the studio box.

Each session snapshots the studio's Hermes settings when it starts. The framework asks
the local llama service (streamed, H3's thinking/sampling payload) for a studio-specific
JSON plan, optionally reviews it, optionally waits for approval, submits through the
studio's regular `/api/generate`, tracks the job, checks the output, and retries with
feedback up to the configured attempts. Each Hermes call takes and releases its own turn
in the shared GPU queue and unloads llama afterwards. Image review sends the picture to
Hermes (vision model); Music verifies the file has audio, a plausible duration and is not
silent; Voice checks the studio returned audio. Voice jobs can be cancelled only while
queued (HTTP 409 otherwise). Exact user lyrics are locked into Music requests.

The authenticated internal API (`X-Queue-Internal-Secret`):

- `POST /v1/agentic/{image|music|voice}/sessions` `{"request","settings","guide","approve_plan"?,…}`
  (Music also `locked_lyrics`; all accept `resume_session_id`, `planner_model`, `planner_instructions`)
- `GET /v1/agentic/{studio}/sessions` (slim rows) and `GET …/sessions/{id}` (with `log`)
- `POST …/sessions/{id}/cancel`, `POST …/sessions/{id}/plan` `{"action":"approve|revise","notes"}`,
  `POST …/sessions/{id}/steer` `{"text"}`, `DELETE …/sessions/{id}` (finished only)
- `GET|PUT /v1/agentic/{studio}/settings`, `GET /v1/agentic/{studio}/guides`,
  `POST /v1/agentic/{studio}/preview`, `GET /v1/agentic/models`

`agentic.sessions` is listed in `/v1/capabilities`. Sessions (with their log) and
settings persist in the `framework-agentic-data` volume. In-flight runs are marked
`interrupted` after a framework restart; completed results remain browseable.
`FRAMEWORK_AGENTIC_MODEL` sets the default Hermes model (matches Video).
