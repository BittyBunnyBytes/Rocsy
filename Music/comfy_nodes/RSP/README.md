# RSP (Rocsy Suno Parity)

Suno-style covers on YuE2, keeping the covered song's singer. Needs the YuE2-ComfyUI pack.

**RSP Suno Cover** runs: transcribe the song, sing it with YuE2 in the new style, split that take, convert its vocal to
the kept voice with Seed-VC, mix it back over the band. It expands into YuE2's nodes plus the small RSP nodes below, so
ComfyUI caches each stage: changing only the voice settings doesn't sing again.

| Control | What it does |
|---|---|
| `style`, `lyrics` | Style of Music. Empty lyrics = recognise the song's words. |
| `audio_influence` | 70+ melody and chords, 35-69 melody, under 35 lyrics and style only |
| `style_influence` | YuE2 `cfg_scale`: 0.5 at 0%, default at 50%, 3.0 at 100% |
| `weirdness` | temperature 0.7 / 1.0 / 1.4 at 0 / 50 / 100%, with top_p, top_k and repetition penalty |
| `vocal_gender` | Added to the style text |
| `voice_source` | `source singer`, `voice audio input`, or `off` |
| `voice_strength` | 100 = kept voice only, 0 = YuE2's voice only |

The slider mappings are RSP's own (Suno doesn't publish its behaviour) and need tuning by ear.
Outputs: `cover`, `yue2_take` (the one YuE2 Edit Track can edit), `converted_vocal`, `band`, `score_abc`, `lyrics`.

Other nodes: RSP Vocals (GPU), RSP Pick Voice Reference, RSP Voice Convert (GPU), RSP Split Band, RSP Mix.

## Speed
Singing uses flash attention, samples on the card with tokens read back in batches (`rsp/fast_singing.py`, active once a cover runs), RSP Vocals runs the pack's vocal model in batches at float16, and Seed-VC uses the whole 25 s reference as its prompt
(moving the vocal by whole octaves to the voice's range, so it stays in key; a voice recording the separator barely changes
is used as recorded, `RSP Clean Voice`), skips silences of 10 s or more and runs BigVGAN at float16 in pieces of one fixed shape (`rsp/seedvc_fast.py`).

## Install
Rocsy ships RSP in `Music/comfy_nodes/RSP` (edit it there). **Models -> Manager -> Core Backend -> Music covers**
installs everything a cover needs: the YuE2-ComfyUI pack at the commit RSP was tested on, this folder copied (or
updated) into `ComfyUI/custom_nodes/RSP`, `./install.sh`, and every model file (YuE2 3B INT8, SheetSage2,
Mel-Band RoFormer, Qwen3-ASR, Whisper large-v3; the list is `BunnyBytes/hostctl/rspmodels.py`), each at a pinned
revision and checked against its SHA-256. Music asks with downloads off, so nothing large is fetched mid-render.
Restart ComfyUI once while it's idle afterwards.

By hand: `./install.sh` from `ComfyUI/custom_nodes/RSP`, then restart ComfyUI while idle. Seed-VC runs on the GPU in
its own venv (`.venv`, on top of ComfyUI's torch) and refuses to run without a GPU. Weights: `models/RSP/seed-vc`.
Tests: from ComfyUI's folder, `custom_nodes/RSP/.venv/bin/python -m pytest custom_nodes/RSP/tests`.

## Licences
RSP is GPL-3.0 (`LICENSE`): `rsp/seedvc_fast.py` adapts Seed-VC's loop, and Seed-VC (`vendor/seed-vc`, downloaded
by install.sh) is GPL-3.0. The rest of Rocsy stays Apache-2.0. YuE2 and SheetSage2 weights are CC BY-NC 4.0:
non-commercial.
