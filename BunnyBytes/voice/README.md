# Rocsy Voice

The gateway serves Voice at `/Voice/`. The Docker service on port 8950 owns
characters, generation jobs and the shared GPU queue. Qwen3-TTS runs in that
container. IndexTTS2, Fish Audio S2 Pro and Fish Audio Drama 3 run in the Rocsy
Voice worker on host loopback port 7860. IndexTTS2 and S2 Pro use separate ROCm
Python environments under `voice/models/legacy/`; Drama 3 runs in a HIP build of
audio.cpp (`models/legacy/FishDrama3/audio.cpp`, `fish_drama3` port) with the
Jmica/Drama-3-GGUF Q3_K_S package, every expert layer on the GPU. The worker API uses its own small
`voice/.worker-venv` environment so Fish engines can run without IndexTTS2. The worker is authenticated with
`QUEUE_INTERNAL_SECRET` from `BunnyBytes/.env`.

Install or repair an engine in Models → Manager → Core Backend → Voice engines.
Each action downloads that engine's checkpoint and, for the host engines, its
separate ROCm Python backend. Qwen3-TTS uses the Voice container backend; its
Core Backend action downloads the checkpoint and builds/starts that container.
Existing checkpoints and working environments are reused. Installation logs
remain in `core-backend-extras/voice/`. IndexTTS2 has a separate DeepSpeed
0.17.1 install action in Core Backend. It uses the engine's existing ROCm PyTorch
and leaves DeepSpeed disabled in generation settings until selected. Model files are local and are never
downloaded on startup. The worker's
`/health` response reports each engine's actual checkpoint readiness. The
Voice tab shows an engine as Ready only when the worker and its checkpoint are
available. Qwen readiness is checked separately in the container.

Characters live in `voice/data/voices/` and are managed by Rocsy's Voice API.
The gallery, reference samples, portraits, new character saving and deletion
all use that directory. The worker reads these profiles when generating speech.
Existing character IDs were copied from the old voice store during migration.

Whisper transcription also runs in the worker from the local
`voice/models/legacy/WhisperLargeV3Turbo` checkpoint. The container holds the
shared GPU queue token for both generation and transcription. Music removal
continues through ComfyUI's `BunnyRemoveMusic` node; cleaned clips and uploaded
recordings stay in `voice/data/refs` until saved or pruned after six hours.

Add character → Create a voice designs a new voice from a text description
with Qwen3-TTS VoiceDesign (`voice/models/qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign`,
optional, installed from the same Core Backend card). Each preview speaks the
sample line in the GPU queue (`POST /Voice/api/reference/design`), loading the
model per take and unloading it afterwards. The kept take is saved like a
cleaned clip, with the sample line as its exact transcript. `/api/state`
lists engines that can create voices under `creators`.

Voice → Settings → Attention is per engine. Qwen3-TTS (also used by Create a
voice) takes SDPA Auto/Flash/Memory-efficient/Math, Eager, or FlashAttention 2.
FlashAttention 2 is optional: Core Backend → Voice engines → "Qwen3-TTS ·
FlashAttention 2" runs `hostctl/build-voice-flash-attention.sh`. That script
compiles ROCm/flash-attention (tridao) in a throw-away container started from the
Voice image, so it matches the image's own Python and torch. ComfyUI's build
can't be shared: it is for a different Python and torch. The result goes to
`core-backend-extras/voice/flash-attn/site`, mounted read-only on the Voice
PYTHONPATH, and Voice restarts once idle. Rebuild after the Voice image's torch
changes. IndexTTS2 and Fish S2 get the SDPA choices through
`worker/rocsy_voice/sdpa_shim/sitecustomize.py` (`ROCSY_SDPA_BACKEND`). Drama 3
always uses ggml FlashAttention.

Install the Rocsy worker unit with `./install-units.sh`, then run
`systemctl --user enable --now rocsy-voice-worker`. Build and restart the Voice
container with `docker compose build voice && docker compose up -d --no-deps voice`.
Check `/Voice/api/state` to see the four engines and their readiness.

IndexTTS2 script emotion cues use Rocsy syntax: `[EMO=happy]words[/]` or
`[EMO=text,TEXT=warm and calm]words[/]`. The Voice parser removes the cue
and passes its description as IndexTTS2 emotion text; the words remain the
spoken script. `[EMO=off]` disables the saved emotion prompt within its span.
The upstream model receives separate speech and emotion text, not bracket tags.
