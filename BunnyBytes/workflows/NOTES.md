# YuE2 workflow conversion notes

Source (frontend/visual-editor export):
`$ROCSY_COMFY_ROOT/user/default/workflows/Audio/YuE 2/YuE 2 - INT8.json`

Converter: `~/AI/BunnyBytes/workflows/convert_yue2_workflow.py`

Validated output (API-format prompt dict, ready for `POST /prompt`):
`~/AI/BunnyBytes/workflows/YuE2-INT8.json`

## Verification result

Validated by POSTing the converted prompt to ComfyUI's `/prompt`: `HTTP 200`
with empty `node_errors` (the queued validation job was deleted before it
rendered). The first attempt surfaced the `LoadAudioFromYouTube` empty-URL quirk
described below, which the converter now handles.

## Node-id map (for `Music/backend/generation.py`)

Per-request parameters a caller will want to override, and the exact node
id / input key to write them into in `YuE2-INT8.json`:

| Purpose | Node id | class_type | Input key | Default in template |
|---|---|---|---|---|
| **Style / genre prompt text** | `22` (YuE2GenerateMusic) **and** `23` (YuE2GenerateABC) | — | `inputs.style` | long descriptive string — write the same value to **both** nodes 22 and 23 (they must match; the ABC composer and the music generator both take style) |
| **Lyrics text** | `22` and `23` | — | `inputs.lyrics` | multi-verse `[verse]`/`[chorus]`-tagged string — write to **both** 22 and 23 |
| **Seed (music generation)** | `22` (YuE2GenerateMusic) | — | `inputs.seed` | `17` |
| **Seed (ABC composition)** | `23` (YuE2GenerateABC) | — | `inputs.seed` | `10` — independent seed from node 22; decide whether the app wants these locked together or independently randomized |
| **Seed (sampler)** | `8` (KSampler) | — | `inputs.seed` | `7` — a third, independent seed; largely irrelevant since `cfg=1` and `positive==negative` (see below), but exists |
| **Duration / length** | `22` (YuE2GenerateMusic) | — | `inputs.max_duration` (seconds, float) | `200` — this is the *requested max*; actual generated length can be shorter. The real rendered duration flows out of node 22's second output ("seconds") through link into node `5` (`EmptyYuE2LatentAudio.inputs.seconds`) — **do not hardcode/override node 5's `seconds` directly**, it's wired as a graph reference `["22", 1]` and must stay that way; only touch node 22's `max_duration`. |
| **Output / save node** | `10` (SaveAudioAdvanced) | — | `inputs.filename_prefix` (string, e.g. `"audio/YuE2_INT8"`), `inputs.format` (dict, e.g. `{"format": "flac"}` or `{"format": "mp3", "quality": "V0"}`) | `flac` under `output/audio/` |
| **ABC mode** (full vs melody-only) | `22` and `23` | — | `inputs.mode` (`"full"` or `"melody"`) | `"full"` on both |
| **Reference audio — use at all** | `43` (LCBooleanValue "USE REFERENCE") | — | `inputs.value` (bool) | `true` — master switch; when `false`, SheetSage2 never runs and ABC is self-composed only |
| **Reference audio — source** | `42` (LCBooleanValue "USE YOUTUBE") | — | `inputs.value` (bool) | `false` = local file (node 40), `true` = YouTube (node 41) |
| **Reference audio — local file** | `40` (LoadAudio) | — | `inputs.audio` (filename already present in ComfyUI's input dir) | `"Ref Music.mp3"` |
| **Reference audio — YouTube URL** | `41` (LoadAudioFromYouTube) | — | `inputs.url`, `inputs.start_time`, `inputs.duration`, `inputs.force_redownload` | `url` defaults to a **placeholder** (see Quirks below) — must be overwritten with a real `http(s)://` URL whenever `USE_YOUTUBE`/`USE_REFERENCE` are both true, or left as-is (harmless, never fetched) otherwise |
| **Reference strength (0=loose, 1=strict)** | `61` — **not in the output file**, already inlined as literal `0.9` into nodes `62`/`63`/`64`'s `a` input | — | n/a — see "Dropped nodes" below | `0.9` in the source; if the Music app wants this tunable per-request, it must write the *derived* values directly into `62.inputs.a`, `63.inputs.a`, `64.inputs.a` (all three, same value), since node 61 (the primitive dial) no longer exists as a node to update |
| **Model checkpoint** | `15` (CheckpointLoaderSimple) | — | `inputs.ckpt_name` | `"yue2_3b_int8_convrot.safetensors"` — unlikely to change per-request |
| **SheetSage2 audio-encoder model** | `46` (AudioEncoderLoader) | — | `inputs.audio_encoder_name` | `"sheetsage2_bf16.safetensors"` |
| **Reference transcription mode** | `47` (SheetSage2AudioToABC) | — | `inputs.mode` (`"full"`/`"melody"`) | `"full"` |
| **Sampler settings** | `8` (KSampler) | — | `steps`, `cfg`, `sampler_name`, `scheduler`, `denoise` | `32`, `1`, `"dpm_2"`, `"sgm_uniform"`, `1` — `cfg=1` means classifier-free guidance is effectively disabled (matches `positive`/`negative` both pointing at the same YuE2GenerateMusic conditioning output — this is intentional in the source graph, not a conversion artifact) |
| **VAE tiled-decode tuning** | `17` (VAEDecodeAudioTiled) | — | `tile_size`, `overlap` | `1920`, `128` — lower-VRAM knobs, unlikely to be per-request |

Graph-internal wiring the app must **never** touch directly (these are node
references, `["<id>", slot]`, not literal values — editing the source node's
value is correct; editing the target's reference would break the graph):
`5.seconds` (← `22`), `8.model/positive/negative/latent_image`,
`10.audio`, `17.samples/vae`, `22.clip/abc/temperature/top_p/repetition_penalty`,
`23.clip`, `47.audio_encoder/audio`, `48.on_false/on_true/switch`,
`45.on_false/on_true/switch`, `62/63/64.a` (once wired — see reference-strength
note above for how to override the *value* that flows into these).

## Dropped nodes (frontend-only, no API-format effect)

| Source id | type | Why dropped |
|---|---|---|
| `12` | PrimitiveNode ("STYLE — edit this") | Frontend value source, no execute(). Literal string inlined directly into `22.style` and `23.style`. |
| `13` | PrimitiveNode ("LYRICS — edit this") | Same pattern; inlined into `22.lyrics` and `23.lyrics`. |
| `61` | PrimitiveFloat ("REFERENCE STRENGTH") | Same pattern; inlined into `62.a`, `63.a`, `64.a` (all three, value `0.9`). |
| `14` | PreviewAny ("Preview: Composed ABC") | Frontend display-only node, but **not a dead end** — it also forwarded YuE2GenerateABC's `abc` output into `48.on_false` (ComfySwitchNode). Handled as a *bypass*: the link resolver walks straight through it, so `48.on_false` in the final output correctly points at `["23", 0]` (YuE2GenerateABC) rather than at the (now-nonexistent) PreviewAny node. Confirmed correct by inspecting the resolved output. |
| `53` | PreviewAny ("Preview: ABC Actually Used") | Frontend display-only, and a true dead end (its output has zero downstream links in the source graph) — dropped with no bypass needed. |
| `25`, `60`, `65` | Note | Pure comments, no inputs/outputs, no execution effect. |

## Widget-order assumptions

**None needed, for any node type.** This particular workflow export includes
a `widgets_values_named` dict (name → value) on every node, in addition to
the positional `widgets_values` array the frontend uses internally. The
converter reads widget values by name (via each input slot's own declared
`widget.name` from the node's own `inputs` list), not by position — so no
guessing of declared-schema order was required even for node types whose
Python source wasn't read in depth. Source was still read directly for the
following to confirm names/behavior and catch non-obvious cases (documented
inline in the script's docstring and below):

- `YuE2GenerateABC`, `YuE2GenerateMusic`, `EmptyYuE2LatentAudio`
  (`comfy_extras/nodes_yue2.py`) — matched exactly, 11/11/2 inputs.
- `ComfySwitchNode` (`comfy_extras/nodes_logic.py`, registered as
  `SwitchNode` class but `node_id="ComfySwitchNode"`) — confirmed
  `switch`/`on_false`/`on_true` (note: schema declares `switch` first, but
  input order in the API `inputs` dict doesn't matter, only key names do).
- `SheetSage2AudioToABC`, `AudioEncoderLoader`
  (`comfy_extras/nodes_audio_encoder.py`) — matched exactly.
- `LoadAudio`, `SaveAudioAdvanced`, `VAEDecodeAudioTiled`
  (`comfy_extras/nodes_audio.py`) — matched. Found and handled two
  frontend-only companion-widget types that would otherwise have leaked
  bogus input keys: `AUDIO_UI` (inline waveform player, e.g. `LoadAudio`'s
  `audioUI` slot) and `AUDIOUPLOAD` (`LoadAudio`'s `upload` "choose file"
  button — **note this one WAS present with a real value (`""`) in
  `widgets_values_named`, so a naive by-name lookup would have wrongly
  included it**; the converter instead skips any input slot whose
  *declared type* is `AUDIO_UI`/`AUDIOUPLOAD`, confirmed against
  `LoadAudio.execute(cls, audio)` taking only `audio`). Also found
  `SaveAudioAdvanced.inputs.format` is a `COMFY_DYNAMICCOMBO_V3` whose
  `execute()` expects a **dict** (`format.get("format")`,
  `format.get("quality")`), not the bare string the frontend widget stores
  — wrapped as `{"format": "flac"}` accordingly (confirmed correct by live
  validation).
- `LCBooleanValue` (`custom_nodes/ComfyUI_LC123_nodes/lc_boolean_switch.py`)
  — single `value: BOOLEAN` input, matched.
- `MathExpression|pysssss`
  (`custom_nodes/ComfyUI-Custom-Scripts/py/math_expression.py`) — old-style
  `INPUT_TYPES` with required `expression` + optional `a`/`b`/`c` (any
  type). `b`/`c` are unconnected in this graph (no link, no widget) and are
  correctly omitted from the API inputs rather than guessed.
- `LoadAudioFromYouTube`
  (`custom_nodes/ComfyUI-YouTube-Reference-Audio/nodes.py`) — matched
  exactly (`url` required; `start_time`/`duration`/`force_redownload`
  optional). Confirmed present and installed, not missing, per the task's
  already-confirmed facts.
- `KSampler`, `CheckpointLoaderSimple` (`nodes.py`, classic
  `INPUT_TYPES`-style core nodes) — standard, matched by inspection of the
  graph's own declared input names (didn't need to re-derive from source
  since the frontend already names every slot explicitly).

`WhisperTranscribeAudio` / `MiniCPM5FormatLyrics`
(`custom_nodes/ComfyUI-Reference-Audio-Extras/nodes.py`) — **do not appear
anywhere in this graph's `nodes` array at all** (checked: zero matches).
They belong only to the separate, out-of-scope "Transcribe & Format
Lyrics" companion workflow, exactly as the task's background section
predicted. Nothing to drop or handle for them here.

## Known quirk fixed in the template (not a mapping bug)

`LoadAudioFromYouTube` (node `41`) has a custom `VALIDATE_INPUTS` classmethod
that unconditionally rejects an empty `url`, **even though** this node sits
behind `ComfySwitchNode` (node `45`)'s lazy `on_true` input, which is gated
off by default (`USE_YOUTUBE` = node `42` = `false`). ComfyUI's static
`/prompt` validation runs every node's `VALIDATE_INPUTS` up front, regardless
of whether a lazy branch will actually execute — so the *original* frontend
workflow, saved with its default blank YouTube URL, would hit this exact
same validation error if queued as-is (verified: this is a property of the
node's own `VALIDATE_INPUTS`, not something the conversion introduced).

Fix applied in `convert_yue2_workflow.py` (`_patch_known_quirks`): when
`41.inputs.url` is empty after conversion, substitute a syntactically valid
but inert placeholder (`https://youtu.be/placeholder-unused-reference`).
It is never fetched with the default switch state (`USE_YOUTUBE=false`), and
the whole point of exposing this input to `Music/backend/generation.py` is
that it gets overwritten with a real URL whenever a caller actually
requests a YouTube reference anyway.

## Blockers

None. The converted workflow validates against a ComfyUI install with the
YuE2 node set and checkpoint.

---

# Krea 2 Turbo workflow conversion notes

Source (frontend/visual-editor export of a saved workflow, NOT
the drag-in blueprint at `$ROCSY_COMFY_ROOT/blueprints/Text to
Image (Krea-2 Turbo).json` — that blueprint is a collapsed "subgraph" node
with no upscaler and no reference-image support, and was rejected mid-task
in favor of the workflow below once that became clear):
`$ROCSY_COMFY_ROOT/user/default/workflows/Image/Krea 2/Krea2-Advanced-LoRA.json`

Validated output (API-format prompt dict, ready for `POST /prompt`):
`~/AI/Krea/workflows/Krea2-Advanced-LoRA-API.json`

No converter script was written for this one (unlike YuE2) — the source is a
plain flat 42-node graph (no subgraph flattening needed), so it was hand
-translated directly against the live edge list and validated live rather
than through a reusable script. If this workflow gets re-saved from the
ComfyUI editor with structural changes, the node-id map below needs
re-deriving from the new export the same way (dump `nodes`, cross-reference
each `inputs[].link` against every other node's `outputs[].links` to build
an edge list, don't trust position/order assumptions).

## Verification result

Validated with one live run with 2 of 4 references and the Enhancer (4×
Real-ESRGAN upscale) enabled, so every optional branch executed: `HTTP 200`,
empty `node_errors`, `status_str: "success"`, and an upscaled image written to
ComfyUI's output folder. All apps read ComfyUI's output from the shared
`$ROCSY_COMFY_SHARED/output` directory; set the ComfyUI URL in each app's `.env`.

## Node-id map (for `Krea/backend/generation.py`)

Per-request parameters and the exact node id / input key written into
`Krea2-Advanced-LoRA-API.json`:

| Purpose | Node id | class_type | Input key | Template default |
|---|---|---|---|---|
| **Prompt text** | `5` | CLIPTextEncode | `inputs.text` | empty string |
| **Width / height** | `7` | EmptyLatentImage | `inputs.width` / `inputs.height` | `1024`, `1024` — must stay multiples of 32 per the node's own title ("Resolution (multiples of 32)") |
| **Seed** | `8` | KSampler | `inputs.seed` | `572143895013248`, UI `control_after_generate: "randomize"` — the app generates a fresh random seed per request rather than reusing this literal (same pattern as Music/YuE2) |
| **Enhancer (4× upscale) toggle** | `19` | PrimitiveBoolean | `inputs.value` | `False` |
| **Reference master toggle** ("Use reference images") | `15` | PrimitiveBoolean | `inputs.value` | `False` — if `False`, the entire reference chain (nodes 12–14, 21–32, 33) is bypassed via node `16`'s lazy `on_false`, and none of it needs valid inputs to *execute*, but ComfyUI's static `/prompt` validation still checks every node eagerly (same quirk as YuE2's `LoadAudioFromYouTube`) — so all 4 `LoadImage` nodes must always hold a real, present filename even when disabled |
| **Reference slot N enable** (N=1..4) | `35`/`37`/`39`/`41` | PrimitiveBoolean | `inputs.value` | `True, False, False, False` (only slot 1 on by default) |
| **Reference slot N image** | `12`/`21`/`25`/`29` | LoadImage | `inputs.image` | `"Krea2_Advanced_LoRA_00006_.png"` (a real file already present in ComfyUI's input dir — confirmed at `$ROCSY_COMFY_SHARED/input/`). Only overwrite this when the app has a real uploaded filename for that slot (via `POST /api/upload` → `ComfyService.upload_image`, which forwards to ComfyUI's own `/upload/image`); otherwise leave the template's placeholder in place so static validation never fails on an unused slot |
| **Reference slot N strength** | `34`/`23`/`27`/`31` | LatentMultiply | `inputs.multiplier` | `1.0, 0.75, 0.6, 0.45` |
| **LoRA slot N** (N=1..3, fixed to the 3 LoRAs actually installed) | `4` | `Power Lora Loader (rgthree)` | `inputs.lora_1` / `lora_2` / `lora_3`, each `{"on": bool, "lora": "<filename>", "strength": float, "strengthTwo": null}` | slot 1 `KNP_000003000.safetensors` **on**, strength `0.8`; slot 2 `nicegirls_krea2.safetensors` off; slot 3 `lenovo_krea2.safetensors` off |
| **Output / save node** | `10` | SaveImage | `inputs.filename_prefix` | `"Krea2/Krea2_Advanced_LoRA"` — the app appends a timestamp per request so repeated generations don't collide, keeping everything under a `Krea2/` subfolder |
| **Model checkpoint / text encoder / VAE** | `1` / `2` / `3` | UNETLoader / CLIPLoader / VAELoader | `unet_name` / `clip_name`+`type`+`device` / `vae_name` | `krea2_turbo_int8_convrot.safetensors`, `qwen3vl_4b_fp8_scaled.safetensors`+`krea2`+`default`, `qwen_image_vae.safetensors` — **not** exposed per-request |
| **Sampler settings** | `8` | KSampler | `steps`, `cfg`, `sampler_name`, `scheduler`, `denoise` | `8`, `1`, `"euler"`, `"simple"`, `1` — turbo defaults, not exposed per-request |
| **Upscale model** | `17` | UpscaleModelLoader | `inputs.model_name` | `"RealESRGAN_x4plus.pth"` — not exposed per-request (a second option, `4x_foolhardy_Remacri.pth`, is also installed but unused here) |

Graph-internal wiring the app must **never** touch directly (node references,
not literal values): `4.model`/`4.clip` (← `1`, `2`); `5.clip` (← `4[1]`,
the *CLIP* output slot — `4` returns `(MODEL, CLIP)` in that order, confirmed
against `RgthreePowerLoraLoader.RETURN_TYPES` in
`custom_nodes/rgthree-comfy/py/power_lora_loader.py`); `6.conditioning` (←
`5`); `8.model/positive/negative/latent_image`; `9.samples/vae`; `10.images`
(← `20`, the Enhancer toggle output — never point this at `9` directly, that
would silently skip the enhancer); `13/22/26/30.pixels/vae`; `14/24/28/32
.conditioning/latent` (each stage's `conditioning` input chains from the
*previous* stage's `ComfySwitchNode` output — `14`←`5`, `24`←`36`, `28`←`38`,
`32`←`40` — not all from `5` directly, this is a running accumulator, not 4
independent branches); `16/36/38/40/42` (each `ComfySwitchNode`'s
`on_false`/`on_true` pair); `33.conditioning` (← `42`, the *last* stage's
switch); `18.upscale_model/image`; `20.on_false/on_true` (← `9`/`18`).

## Quirk found and fixed: `DynamicCombo` API format (COMFY_DYNAMICCOMBO_V3)

This only affected the *rejected* plain-blueprint conversion attempt (its
`TextGenerate` LLM prompt-enhancement node), not the final workflow above,
but is recorded here since it'll bite again anywhere a `DynamicCombo`-typed
input (`comfy_api/latest/_io.py`, class `DynamicCombo`) shows up in a future
conversion. Unlike `SaveAudioAdvanced.format` in the YuE2 conversion (a
flat nested dict, `{"format": "flac"}`), `DynamicCombo`'s
`_expand_schema_for_dynamic` does **not** accept a nested dict — the
selected option's own sub-fields must be flattened as separate **dotted
top-level keys** in `inputs`: `"sampling_mode": "on"`,
`"sampling_mode.temperature": 0.7`, `"sampling_mode.top_k": 64`, etc. (the
dotted names are visible directly in the node's own declared `inputs` list
in any frontend export). A nested-dict attempt fails at *execution* time,
not static validation — `node_errors` comes back empty, the prompt is
accepted, and it only errors once the node actually runs:
`TypeError: TextGenerate.execute() missing 1 required positional argument:
'sampling_mode'`. Confirmed live against `127.0.0.1:8188`.

## Blockers

None. Conversion is complete and validated end-to-end (references + LoRA +
enhancer exercised together in one live run).
