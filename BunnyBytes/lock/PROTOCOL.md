# bunny-lock protocol v2

bunny-lock gives the GPU to one job at a time, or to several when *Run jobs
together when they fit* is on (Queue page → Queue settings; see "Concurrent
jobs" below). Apps talk to it through
`BunnyBytes/shared/bunnylock.py` (mounted into every app container); this page
describes what that client does, for anyone writing another one.

All internal routes are loopback only and need `X-Queue-Internal-Secret`.

## The lease stream

Each app **process** opens one stream and keeps it open:

    GET /v2/stream?app=<App>&instance=<random id chosen at process start>

Server-sent events arrive on it:

| event | data | meaning |
|---|---|---|
| `hello` | `{instance}` | connected |
| `grant` | `{entry_id, job_id, token, others}` | your job holds the GPU now; `ack` it, or `release` it if you don't want it. `others`: apps of the jobs already on the GPU — when non-empty, don't free VRAM or unload models (`BunnyLock.shared_with(token)`, `Turn.others`) |
| `prepare` | `{entry_id, job_id}` | your job is next: do its CPU-side setup now (Settings → Services → GPU queue → *Prepare the next job early*) |
| `cancel` | `{request_id, entry_id, job_id}` | the Queue page's Cancel; answer with `POST /v2/reply` |
| `adopt` | `{entry_id, job_id, active, token}` | the app restarted: an entry of its previous process; answer with `POST /v2/adopt` |

A comment line arrives every second. When a process's stream closes, the same
`instance` can simply reconnect. Otherwise its job holding the GPU keeps the turn
for 30 s (`LOCK_ACTIVE_GRACE_SECONDS`), then the GPU moves on; its waiting entries
are kept for 10 minutes (`LOCK_ADOPT_GRACE_SECONDS`). After a queue restart,
processes get 15 s extra to reconnect.

**Restarts.** A new instance of the same app is offered the old process's entries
as `adopt` events and answers each with `POST /v2/adopt`: keep (the entry stays in
place, an active one keeps its turn) or drop. No answer within 30 s means drop.
Studios save queued jobs with `PendingStore`; `requeue_lost()` queues again any
saved job the queue no longer has.

A grant that arrives while the process is reconnecting is re-sent on reconnect.

## Calls

| POST | body | answer |
|---|---|---|
| `/v2/enqueue` | `{app, instance, job_id, label, is_continuous?, priority?, group?, uses_llm?, model?, config?, vram_mb?}` | `{entry_id, state, position}`; the grant comes over the stream |
| `/v2/ack` | `{token}` | the job started (measures the handoff gap) |
| `/v2/heartbeat` | `{token, instance?}` | extends the 90 s lease; `instance` re-binds the turn to a restarted process |
| `/v2/release` | `{token, llm_settle?}` | 404 if the token isn't the active one |
| `/v2/dequeue` | `{entry_id}` | removes a waiting entry (never an active one) |
| `/v2/prioritize` | `{entry_id}` | front of the line; locked entries keep their slots |
| `/v2/progress` | `{token, job}` or `{token, percent, stage?, remaining_seconds?}` | progress bar on the Queue page |
| `/v2/shutdown_at` | `{entry_id, shutdown_at}` | shown on the entry's card |
| `/v2/adopt` | `{entry_id, keep}` | answer to an `adopt` offer |
| `/v2/reply` | `{request_id, status, body}` | answer to a `cancel`; 404 = "not mine any more", the entry is dropped |

`uses_llm: false` marks a job that never needs the LLM. After a release with
`llm_settle: true` (an LLM turn that just unloaded its model), the queue keeps
llama-swap's models unloaded while such a job holds the GPU, until nothing has
reloaded for 8 s or 150 s pass (`LOCK_LLM_SETTLE_SECONDS`).

The lease (90 s, `LOCK_TTL_SECONDS`) is only the fallback for a process that
keeps its stream open but stops working; the client heartbeats held turns every 20 s.

## Concurrent jobs

The queue reads the card's VRAM every second (amdgpu sysfs,
`mem_info_vram_used`; `LOCK_VRAM_DEVICE` picks the device directory) and
records a **peak per job type**: `app · model · config` from the enqueue
(`model`/`config` optional; an LLM turn without a model is `app · LLM`). Each
running job is charged the reading minus the floor (lowest reading ever: the
desktop) minus the other running jobs' planned VRAM, so a job that uses more
than planned raises its peak while it still runs. Peaks never go down on their
own (`data/vram.json`; the Queue page can forget one or set a value instead).
The planned VRAM of a job: the page's value, else its peak raised by the app's
`vram_mb` guess.

With the setting on, the job at the head of the line also starts while others
run when: every job involved has a planned VRAM; no other job of the same app
runs; at most one `uses_llm: true` job runs (they share llama-swap); and
`max(floor + planned of the running jobs, live reading) + its planned VRAM +
reserve <= card size`. Strictly in order: a job that doesn't fit waits and
nothing behind it overtakes. A job type never measured runs alone first. The
LLM guard only unloads the model while every running job has `uses_llm: false`.

Image, Music and Video all render through the one ComfyUI, which executes one
prompt at a time: running them together keeps their models loaded side by side
rather than rendering in parallel.

## Other routes

- `GET /health` – status, version and connected apps.
- `GET /metrics` – Prometheus text: handoff gap, grant→ack, wait and lease
  histograms; grants, expired leases, dead processes, LLM-guard unloads, prepares.
- Browser routes under `/Queue` (through the gateway): the page, `GET /api/queue`
  (with ETag), `GET /api/queue/events` (SSE), reorder, lock, cancel,
  `GET/PUT /api/settings` (`prepare_early`, `concurrent`, `vram_reserve_mb`,
  `vram_total_mb`, `vram_overrides: {key: MiB | null}`), `GET /api/vram`,
  `POST /api/vram/forget {key}`, ComfyUI restart.

## Plugins

A plugin that wants GPU turns copies `bunnylock.py` (or mounts
`BunnyBytes/shared/bunnylock.py`) and uses its own app id. No `plugin.json`
setting is needed: the queue accepts any app that knows the secret.
