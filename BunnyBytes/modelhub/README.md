# Models (modelhub)

Gateway route `/Models/` → container `bunny-models` (port 8960, runs as uid 1000).
Browse and download models and LoRAs from **Civitai** (SFW results), **Civitai Red**
(same API on civitai.red, mature results) and **Hugging Face** into ComfyUI's
`models/` folder (mounted read-write at `/comfy-models`).

Tabs: **Models**, **LoRAs** (same browser, different kind), **Manager**
(downloads, installed files, updates, settings) and **Hardware**.

- `sources.py`: Civitai / HF clients normalized to one shape. Categories:
  Civitai by base model (video/audio sets) and type (LLM, VisionLanguage);
  HF by pipeline tag. Destination folder is suggested per file (ComfyUI-layout
  repos such as Comfy-Org "repackaged" use their own `split_files/<folder>`).
- `hardware.py`: sysfs/proc scan (GPU name/arch/VRAM, display GPU keeps 1.5 GB,
  RAM, disk) + `fit()` tiers (gpu / multi (LLM split) / nvme / offload / no) and
  ROCm caveats (NVFP4, MLX, AWQ/GPTQ, bnb, FP8 on RDNA 3). `best_variant()`
  = highest-quality quant that fits comfortably.
- `downloads.py`: queue (1-4 at once), `.part` + Range resume, SHA-256 check,
  disk-space check, HTML login-page guard (gated Civitai files need the API key).
  Writes `<file>.sha256` sidecars (same format as ComfyUI LoRA Manager).
- `library.py`: installed files, linked by download / LoRA Manager metadata /
  SHA-256 on Civitai / HF repo + file match / pasted link. Update check never
  installs: a newer Civitai version counts only on the same base and the same
  "line" (its name keeps every word of the installed one); parallel variants
  are listed as info. Updates are applied from the Manager, by default with the
  same filename so studio pickers keep working. The previous model/LoRA (and
  old shards) is removed only after every new file matches its published
  SHA-256 and post-download registration succeeds. A failed/incomplete update,
  a source without SHA-256, or a locally changed old file keeps the old version;
  same-name updates keep it as `<name>.old` until it can be removed manually.
- Daily background check (Manager → Settings) notifies via the gateway hub
  (`Models` service in Settings → Notifications).

Data: `modelhub/data/` (settings.json 600 with keys, library.json, hashes.json,
downloads.json, thumbs/). Busy guard for Services: `/Models/api/state` `active`.
Downloads land on disk only; Video/Image LoRA pickers still need their allowlists.

Later additions (2026-10-09):
- LoRAs tab: **Base model** selector, installed bases first (`/api/bases`: Civitai base of
  linked model files, else a file-name guess in `library.BASE_PATTERNS`). Civitai filters by
  `baseModels`; HF by `base_model:adapter:<repo>` tags (`sources.HF_BASE_REPOS`, one request
  per repo). `ALSO_WORKS_ON` adds compatible bases (LTX 2.3 LoRAs on LTX 2.5). Remembered per browser.
- Back button: tabs and the model drawer are history entries; each tab keeps its results and
  scroll position; the top bar is sticky.
- Fit labels say what happens: Fits in VRAM (+ GB left) / Fits in VRAM (tight) / Split across
  GPUs / Needs RAM offload (diffusion) / Needs CPU offload (LLM) / Too big for VRAM + RAM /
  NVIDIA-only, Apple-only, Not GGUF formats / Loads with base model (LoRAs).
- Qwen3.8 Flash Next GGUF fit also estimates the quantized PLE NGram table. If the
  models folder is on local NVMe, an otherwise oversized variant can show
  **NGram on NVMe** when its remaining weights fit GPU and RAM. This is an estimate;
  GGUF tensor quantization can differ from the filename. Downloaded models with
  this tier get llama.cpp `--load-mode mmap --lazy-mode on-direct`. The estimate
  does not count speculative decoding NGram caches as offloadable weights.
- Video previews autoplay muted, looped, no controls, only while on screen.

Integrations (2026-10-09, `integrations.py`):
- **LoRAs → studio galleries.** A LoRA whose base is MiniMax H3 / LTX 2.5 / LTX 2.3 goes to
  `loras/H3MachineCustom` and is registered in Video's gallery; Krea 2 / Qwen 2.1 go to
  `loras/KreaCustom` and Krea's gallery. Registration = `POST <studio>/api/internal/lora-gallery/register`
  (QUEUE_INTERNAL_SECRET; `custom_loras.register()` in both apps, no copy) with name, triggers,
  description, creator, version, source URL, author-stated strength, a matching existing category and
  preview media (cached by the studio). Civitai metadata comes from the API; Hugging Face from the card
  (`instance_prompt`, widget outputs) and README (trigger lines, embedded images/videos, text).
  Choosing another folder in the drawer skips registration.
- **GGUF LLMs → llama-swap.** A block between `# >>> rocsy-models:<id>` / `# <<<` markers is appended
  to `~/AI/Llama/config.yaml` (edited in place: other containers bind the file itself; backup
  `config.yaml.bak-models-*`). Models bigger than VRAM get `auto 2560` + `--fit on`. llama is
  restarted through hostctl once `/running` is empty (`LlamaRestarter`).
- **Manager → Installed**: everything in models/ by kind with "used by" tags; Uninstall deletes the
  file, shards and sidecars, removes gallery entries through the studio, removes Models-made
  llama-swap entries, and asks before breaking a built-in picker slot or a hand-written llama-swap entry.

Last used / "Unused for a week" (2026-10-09, `usage.py`): the models drive is noatime, so usage
comes from the job graphs embedded in ComfyUI outputs (PNG text chunk / MP4 "prompt" tag, last 60
days), Krea's job list (its PNGs carry no graph; model->file table in usage.py, backfill only),
ComfyUI /history every 2 min, and llama-swap /running every minute (LLMs: tracked from 2026-10-09).
A file is "unused" if not used in the window, added before it, and tracked at least that long.
Jobs whose outputs were deleted before tracking started don't count, so check before uninstalling.

Core Backend (2026-10-10, Manager → **Core Backend**, next to Settings): the llama.cpp engine behind
llama-swap. The work runs on the host in `hostctl/corebackend.py` (routes `/core/llamacpp*` on hostctl);
this app only proxies `/api/core/llamacpp*` and announces finished builds (`core-build` notification).
- Mirror `~/AI/llama.cpp-builds/repo.git`: upstream ggml-org/llama.cpp (shallow, release tags `bNNNN`)
  + the RDNA Boost fork (nasone32/llama.cpp-RDNA3-7900xtx-opt). Every fork tip seen is pinned under
  `refs/rocsy/boost/` (state.json), so a force-push can't lose it. Re-checked every 6 h and on demand.
- RDNA Boost = upstream base (merge-base) + its patch series. A release is **compatible** only if the
  whole series replays onto it with no conflict (`git replay`, in memory); nothing is merged by hand or
  by AI. Incompatible releases show the first conflicting patch and files; the build API refuses them.
- Build = `systemd-run --user --unit=core-llama-build` (Nice 19, idle IO), worktree → cmake (same
  options as the built-in build, gfx1100;gfx1101) → llama-server/llama-cli → ldd + `--version` with
  every GPU hidden. The worktree is removed afterwards; `builds/<id>/build` holds the binaries.
- **Use this build** points `~/AI/llama.cpp-builds/active` at it; llama-launch follows the link on each
  model load (loaded models are untouched). Removing the link = the built-in build.
- 2026-10-10 additions: **All versions** lists every llama.cpp release (b1046+, local tags + `ls-remote`).
  A non-compatible release installs only as a **stock** build (`builds/bNNNN-stock`, kind "stock",
  no patches ever applied), marked "No RDNA Boost" everywhere; switching to one warns and names
  llama-swap models using RDNA Boost-only options (`--lazy-mode on-direct`, `--moe-expert-cache`,
  `draft-mtp-adaptive`) that won't start on it. llama-launch accepts `builds/b*-stock` too.
- **Older installs**: `build-*` folders in ~/AI/llama.cpp-rdna-boost and ~/AI/llama.cpp (never the
  source trees). Delete is refused when the folder holds a model-like file (.gguf/.safetensors/...,
  big .bin), has a mount inside, a process runs from it, llama-swap config/.env point into it, or it
  is in use (the built-in only once another build is active); launch scripts referencing it need a
  second confirm. Symlinks are removed as links, never followed. The llama container now mounts the
  whole ~/AI/llama.cpp-rdna-boost clone, so deleting a build never breaks a recreate.
- Build log is a separate panel (refreshes don't close it); it follows the end only while scrolled
  to the bottom.
- **GPU runtimes** (2026-10-10, `hostctl/coreruntime.py`, routes `/core/runtime*`): Rocsy-only ROCm versions
  (AMD TheRock tarballs for gfx110X → `~/AI/rocm-versions/<v>`) and LunarG Vulkan SDKs (`~/AI/vulkan-sdk/<v>`),
  side by side; `/opt/rocm` and pacman packages are never changed. "New builds compile against" picks the ROCm /
  Vulkan tools for the next llama.cpp build; the build records it (`build/rocm-path`, id suffix `-rocmX-Y-Z`) and
  llama-launch runs it with that ROCm's libraries, so switching ROCm = picking another build. The RADV driver
  inside the llama container updates by rebuilding only that image (`--pull --no-cache`), applied when idle.

### Inference Engines (Core Backend)

The Inference Engines area now includes the existing llama.cpp builds, ROCmFPX, and Strata.
ROCmFPX builds from `charlie12345/ROCmFPX` for the detected AMD `gfx` architecture and
appears in the llama.cpp build picker and each llama.cpp model's Parameters tab. It supports
its own GGUF quant formats and regular formats; models using RDNA Boost-only flags remain
ineligible. A build never changes the active engine until **Use this build** is selected.

Strata downloads from `Niko1221/Strata` into `~/AI/core-engines/Strata`. Its setup installs
the selected Qwen3.8 Flash Next family and quantization, or accepts an existing GGUF
directory under the managed LLM folders. Strata decides whether a custom model is
compatible. It serves on loopback port 9101; llama-swap's `strata-qwen-flash-next` model
starts/stops the host service and proxies its API. Its UI is exposed at the authenticated
`/Strata/` route and from the model's **Strata UI** tab when the service is running.

The hipBLASLt card reports the detected GPU architecture, installed library version, and
whether Strata contains an exact matching tuning table. Downloading Strata source also
gets its tables. Strata setup applies a table only for an exact architecture and version
match; otherwise it uses its documented hipBLAS fallback.

A model's **Benchmarking** tab runs the matching `llama-bench` against its saved settings
and displays prompt and generation tokens per second. It requires the llama model to be
unloaded and does not edit Parameters.

For a card/library pair without a shipped table, **Calibrate for this card** builds
Strata's `tune_hipblaslt`, measures the supplied GEMM shapes on the installed card,
validates the generated table header, and saves it under Strata's `tools/hip` directory.
Stop Strata before calibrating; the next Strata start exports the matching table path.

Supported only (2026-10-13, `support.py`): each search result gets `supported` = what can run it
here, or "". Sources: studio templates (`support.STUDIOS`: MiniMax H3, LTX 2.5/2.3, Krea 2,
MOSS-Image, Qwen Image 2.1, Anima, YuE2, MiniMax Music 3), any ComfyUI workflow saved under
`user/default/workflows/<Category>/<Family>/` (mounted ro at `/comfy-workflows`, rescanned every
60 s, Archive/Imports skipped; the folder name is matched to the base model), llama.cpp for GGUF
LLMs, and upscalers. Matched by base model (Civitai base / HF base_model tag), else by the family
name in a Hugging Face repo name. Browse toggle "Supported only" (localStorage
models.supportedOnly); the card's ✓ template badge uses the same field.

ComfyUI models & nodes (2026-10-13, `comfysetup.py` + `hostctl/comfynodes.py`): a card in Manager → Core Backend.
- **Recommended defaults** for model folders with no model files: the TAESD preview decoders ComfyUI's
  README recommends (vae_approx), SDXL / SD1.5 / FLUX VAEs, RealESRGAN x4 + 4x-UltraSharp. URLs from
  ComfyUI-Manager's model-list.json (fetched daily into data/comfy-manager/), built-in fallback.
- **Workflow model files**: every saved workflow in /comfy-workflows is read for files its nodes load
  (widget values + node `properties.models`). Missing ones get a URL from the workflow itself or
  Manager's model list by file name; HEAD gives size + HF SHA-256, gated/dead links are flagged; no link →
  "Search in Models". Downloads go through the normal queue (`after.role = comfy-setup`: no studio registration).
- **Custom nodes**: node types not in ComfyUI's /object_info, mapped to packs via the node's aux_id/cnr_id
  and Manager's extension-node-map / custom-node-list. Installed packs whose declared nodes don't load are
  listed separately (frontend-only nodes such as Set/Get or rgthree labels are ignored). Installs run on the
  host (hostctl `/core/comfynodes/install`, unit core-comfy-nodes): github.com only, never over an existing
  folder, git clone → pip install -r requirements with ComfyUI's ROCm torch/triton/numpy/rocm pinned as
  constraints → install.py; a failing pack is removed. "Restart ComfyUI" uses hostctl's busy-guarded restart.
- Items only used by workflows under Imports/Archive/API Examples are collapsed. Only defaults are preselected.

Custom nodes (2026-10-20, card in Manager → Core Backend → ComfyUI; one-line row in Manager → Updates):
- **Install**: search ComfyUI-Manager's custom-node-list (`GET /api/comfy-nodes/catalog?q=`, git-clone
  entries only) or paste any github.com repo link (`POST /api/comfy-nodes/install`). Same host installer
  and ROCm pins as the missing-node installs above.
- **Update detection**: `POST /api/comfy-nodes/check` → hostctl `/core/comfynodes/check` runs `git ls-remote`
  against each pack's branch (8 in parallel, no download; result in core-backend-extras/comfy-nodes-updates.json).
  Status per pack: behind / current / ahead / modified (tracked files edited) / diverged (own commits) /
  detached (pinned) / local (no git remote) / error. Checked every 12 h while "update checks" is on
  (Settings), with a Models notification for newly behind packs; the count is added to the Updates badge.
- **Update** (`POST /api/comfy-nodes/update {dirs}`, only packs the last check found "behind"): fetch the
  branch, reset to it, submodules, requirements + install.py with the pins; if they fail the pack goes back
  to its old commit. Packs with edits, own commits or a detached HEAD are never changed. Then "Restart
  ComfyUI". Tests: `hostctl/test_comfynodes.py` (throwaway repos, never the real ComfyUI).
