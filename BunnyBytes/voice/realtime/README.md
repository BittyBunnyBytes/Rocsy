# Real-time voice conversion

Speak into your microphone and come out in a Rocsy voice, live, for games, Discord or any voice chat.
It uses Seed-VC's small real-time model (seed-uvit-tat-xlsr-tiny) with RSP's existing Seed-VC
environment. Any voice works without training: the Voice studio's characters and Music's custom
singing voices are listed automatically.

- `rt_server.py`: the server (systemd user unit `rocsy-voice-realtime`, `127.0.0.1:8953`).
- `rocsy_vc_client.py`: a ready-made client: microphone → Rocsy → a virtual microphone for games.

## Speed (RX 7900 XTX, 48 kHz)

| block | diffusion steps | GPU time per block | total delay | likeness* |
|---|---|---|---|---|
| 300 ms | 10 | 150 ms | ~530 ms | 0.80 |
| 200 ms | 10 | 150 ms | ~430 ms | 0.78 |
| **200 ms** | **6** (default) | **100 ms** | **~380 ms** | 0.77 |
| 150 ms | 6 | 100 ms | ~340 ms | 0.76 |

*speaker similarity to the target voice (CAM++); the original speaker scored 0.46 against it.
The default uses half the GPU time it has, which leaves room for a game running on the same card.
About 1 GB of VRAM. The model loads in ~1 s and stays loaded for 60 s after the last stream
(`RT_VC_KEEP_LOADED_S`).

## GPU queue

A stream holds one continuous turn in Rocsy's GPU queue (app `VoiceRT`, "Real-time voice
conversion") from the first connection until 60 s after the last one closes. With the queue's
default one-job-at-a-time setting, renders and LLM work wait while you talk, and a stream waits
(`{"type": "waiting"}`) while a render runs. Turn on Queue → Queue settings → **Run jobs together
when they fit** to let it run next to other work; its VRAM is set to 1.5 GB there (Queue → VRAM).

## Using it in a game

Easiest, from the browser on this computer: Voice → Live voice → Output → **My microphone**, press
Start, and choose **Rocsy Voice** as the microphone in the game / Discord / Signal (optionally "Make
Rocsy Voice my default microphone while I'm live", which switches back when you stop or close the tab;
hostctl/voicemic.py, a 30 s heartbeat). Without a browser:

### Command-line client (Linux)

```bash
pip install sounddevice numpy websockets       # once
python3 rocsy_vc_client.py --list-voices
python3 rocsy_vc_client.py --voice Bitty --virtual-mic -v
```

`--virtual-mic` creates a microphone called **Rocsy Voice** (PipeWire / PulseAudio); choose it as
the input device in the game or voice chat. It disappears when the client stops. The converted
voice plays only into that microphone, never your speakers: if the client can't move its audio
there, it stays silent. Your own mic stays your system default; nothing else changes.

Windows: install a virtual audio cable (VB-Audio Cable), run with `--output "CABLE Input"` and pick
"CABLE Output" as the game's microphone.

Other options: `--block-ms`, `--steps` (see the table), `--silence-db` (below this level counts as
silence and isn't converted, default -50), `--input` (which microphone), `--input-file` /
`--output-file` (test without a microphone).

## From another computer

Through the Rocsy site: `wss://<your site>/Voice/v1/realtime`, with a key made on the Llama API page
→ Keys for other apps, with **Real-time voice conversion** ticked (untick **Chat** for a key that
can only do voice). Send it as `Authorization: Bearer rk-...`, or `?key=rk-...` when the WebSocket
library can't set headers. Each stream counts as one use against the key's per-minute / per-day
limits. Logged-in Rocsy pages can open the same URL with their session.

```bash
python3 rocsy_vc_client.py --url wss://<your site>/Voice/v1/realtime --key rk-... --voice Bitty --virtual-mic
```

## API

`GET /v1/realtime/voices` (site: `/Voice/v1/realtime/voices`) →
`{"voices": [{"id": "voice:bitty-f06369c2", "name": "Bitty", "kind": "voice", "seconds": 6.64}, ...]}`

`WS /v1/realtime` (site: `/Voice/v1/realtime`):

1. Send JSON settings first (or the same keys as query parameters):
   `{"voice": "Bitty", "sample_rate": 48000, "block_ms": 200, "diffusion_steps": 6,
   "silence_db": -50, "cfg_rate": 0.7, "prompt_seconds": 3}`. Only `voice` is required (a name
   or id). Sample rates: 16000, 22050, 24000, 32000, 44100, 48000.
2. Wait for `{"type": "ready", "sample_rate", "block_samples", "latency_ms", "voice"}`
   (`{"type": "waiting", "position"}` may come first while the GPU queue is busy).
3. Send binary frames of 16-bit little-endian mono PCM at your sample rate, any size, at the pace
   you capture them. Converted audio comes back in the same format, `block_samples` at a time.
   If you send faster than it converts, audio older than a second is dropped.
4. Optional mid-stream JSON: `{"type": "voice", "voice": "..."}` switches voice;
   `{"type": "settings", "diffusion_steps": 8, ...}` changes steps / cfg / silence / prompt length.
   The server sends `{"type": "stats", "infer_ms", "realtime_factor", "latency_ms"}` every few
   seconds and `{"type": "error", "message", "fatal"?}` on problems (then closes, unless
   `"fatal": false`).

At most 2 streams at once (`RT_VC_MAX_SESSIONS`).

### Publishing the converted voice (Live voice → 📡 API)

A stream can also publish its converted voice on a channel: `"publish": "my-channel"` in the first
message, or `{"type": "publish", "channel": "my-channel" | null}` later (the client: `--publish
my-channel`; the Live voice panel: the 📡 API toggle). Any number of programs then receive it:

`WS /v1/realtime/listen?channel=my-channel` (site: `/Voice/v1/realtime/listen`, same login / key as
the stream) → `{"type": "hello", "sample_rate", "format", "block_samples", "voice"}` while someone
publishes (else `{"type": "waiting"}`), then binary 16-bit mono PCM blocks, `{"type": "voice"}` when
the voice changes and `{"type": "end"}` when the publisher stops (the listener stays connected for the
next one). A listener that can't keep up skips blocks; it never slows the speaker down. The
publisher gets `{"type": "listeners", "count"}`. `GET /v1/realtime/channels` lists live channels.
One publisher per channel.

## Setup on a new install

The model files (~2.2 GB: DiT_uvit_tat_xlsr_ema.pth, CosyVoice hift.pt, facebook/wav2vec2-xls-r-300m)
go into RSP's Seed-VC cache (`models/RSP/seed-vc`); the server never downloads anything itself.
`install-units.sh` installs and the unit's `ExecStart` uses `RSP/.venv/bin/python`.
