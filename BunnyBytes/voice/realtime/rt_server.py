"""Rocsy real-time voice conversion: speak into a microphone, hear yourself in a Rocsy voice.

Runs on the host with RSP's Seed-VC environment (it already has ROCm torch and Seed-VC's
dependencies) and Seed-VC's small real-time model (seed-uvit-tat-xlsr-tiny, 22.05 kHz).
The streaming loop is a headless port of Seed-VC's real-time-gui.py: a rolling input
buffer with a few seconds of left context, one diffusion pass per block, and SOLA
cross-fading between blocks. Its funasr VAD is replaced by an energy gate.

API (loopback; the gateway exposes it at /Voice/v1/realtime for logged-in pages and keys):

  GET  /health                  {"status", "loaded", "sessions"}
  GET  /v1/realtime/voices      {"voices": [{"id", "name", "kind", "seconds"}]}
  WS   /v1/realtime             the stream, see below
  WS   /v1/realtime/listen?channel=NAME   receive a published stream's converted voice
  GET  /v1/realtime/channels    {"channels": [{"channel", "live", "listeners", "sample_rate", "voice"}]}

Publishing: a stream that sets "publish": "NAME" (first message, or {"type": "publish", "channel":
"NAME" | null} later) also sends every converted block to the listeners of that channel. A listener
gets {"type": "hello", "sample_rate", "format", "voice"} when a publisher is live (else
{"type": "waiting"}), then the same binary PCM blocks, {"type": "voice"} on voice changes and
{"type": "end"} when the publisher stops (it stays connected for the next one). Slow listeners skip
blocks instead of holding the stream up. The publisher gets {"type": "listeners", "count"}.

Stream: the client's first message is JSON
  {"voice": "<voice id or name>", "sample_rate": 48000, "block_ms": 200,
   "diffusion_steps": 6, "silence_db": -50, "cfg_rate": 0.7, "prompt_seconds": 3}
(all but "voice" optional; the same keys work as query parameters). The server answers
  {"type": "ready", "sample_rate", "block_samples", "latency_ms", "voice": {...}}
(or {"type": "waiting", "position"} first while the GPU queue is busy). After that the
client sends binary frames of 16-bit little-endian mono PCM at its sample rate, any size,
and gets converted audio back in the same format, one block at a time. JSON messages
{"type": "voice", "voice": ...} and {"type": "settings", ...} change things mid-stream;
the server sends {"type": "stats", ...} every few seconds and {"type": "error", ...}
before closing on a problem.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

log = logging.getLogger("rocsy-vc")

HERE = Path(__file__).resolve().parent
ROCSY_ROOT = Path(os.environ.get("ROCSY_ROOT") or HERE.parents[2])
COMFY_ROOT = Path(os.environ.get("ROCSY_COMFY_ROOT") or Path.home() / "ComfyUI-Installs/Bunny/ComfyUI")
SEED_VC = Path(os.environ.get("RSP_SEED_VC") or COMFY_ROOT / "custom_nodes/RSP/vendor/seed-vc")
VOICE_DIRS = (("voice", ROCSY_ROOT / "BunnyBytes/voice/data/voices"), ("music", COMFY_ROOT / "models/RSP/voices"))
HOST = os.environ.get("RT_VC_HOST", "127.0.0.1")
PORT = int(os.environ.get("RT_VC_PORT", "8953"))
KEEP_LOADED_S = float(os.environ.get("RT_VC_KEEP_LOADED_S", "60"))
MAX_SESSIONS = int(os.environ.get("RT_VC_MAX_SESSIONS", "2"))
QUEUE_WAIT_S = float(os.environ.get("RT_VC_QUEUE_WAIT_S", "600"))
RATES = (16000, 22050, 24000, 32000, 44100, 48000)
AUDIO_EXTS = (".wav", ".flac", ".mp3", ".ogg", ".m4a", ".opus")

# Offline: everything was downloaded beforehand into Seed-VC's own cache folders.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_CACHE", str(SEED_VC / "checkpoints/hf_cache"))
os.environ.setdefault("MIOPEN_FIND_MODE", "FAST")   # exhaustive MIOpen search costs seconds per new shape
os.environ.setdefault("TQDM_DISABLE", "1")          # Seed-VC shows a progress bar per diffusion pass


# ------------------------------------------------------------------ voices

def list_voices() -> list[dict[str, Any]]:
    """Voice characters (Voice studio) and custom singing voices (Music), with their sample."""
    found = []
    for kind, base in VOICE_DIRS:
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            meta = next((folder / n for n in ("profile.json", "voice.json") if (folder / n).is_file()), None)
            if meta is None:
                continue
            try:
                info = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            named = info.get("file") or ""
            audio = folder / Path(named).name if named else None
            if audio is None or not audio.is_file():
                audio = next((p for p in sorted(folder.iterdir()) if p.suffix.lower() in AUDIO_EXTS), None)
            if audio is None:
                continue
            found.append({"id": f"{kind}:{info.get('id') or folder.name}", "name": info.get("name") or folder.name,
                          "kind": kind, "seconds": info.get("reference_duration"), "path": str(audio)})
    return found


def find_voice(wanted: str) -> dict[str, Any] | None:
    needle = (wanted or "").strip().casefold()
    if not needle:
        return None
    voices = list_voices()
    for voice in voices:      # exact id ("voice:bitty-f06369c2"), bare id, then name
        if needle in (voice["id"].casefold(), voice["id"].split(":", 1)[1].casefold()):
            return voice
    return next((v for v in voices if v["name"].casefold() == needle), None)


def public(voice: dict[str, Any]) -> dict[str, Any]:
    return {k: voice[k] for k in ("id", "name", "kind", "seconds")}


# ------------------------------------------------------------------ engine

class Engine:
    """Seed-VC's real-time model, loaded once and shared by every session (one block at a time)."""

    def __init__(self) -> None:
        self.models: tuple | None = None
        self.device = None
        self.gpu = asyncio.Lock()        # one block on the GPU at a time
        self.loading = asyncio.Lock()
        self.prompts: dict[tuple, tuple] = {}

    @property
    def loaded(self) -> bool:
        return self.models is not None

    def _load(self) -> None:
        cwd = os.getcwd()
        os.chdir(SEED_VC)                # Seed-VC reads configs/ and ./checkpoints relative to itself
        sys.path.insert(0, str(SEED_VC))
        try:
            import torch
            import yaml
            from hf_utils import load_custom_model_from_hf
            from modules.audio import mel_spectrogram
            from modules.campplus.DTDNN import CAMPPlus
            from modules.commons import build_model, load_checkpoint, recursive_munch
            from modules.hifigan.f0_predictor import ConvRNNF0Predictor
            from modules.hifigan.generator import HiFTGenerator
            from transformers import Wav2Vec2FeatureExtractor, Wav2Vec2Model

            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            dit_path, config_path = load_custom_model_from_hf(
                "Plachta/Seed-VC", "DiT_uvit_tat_xlsr_ema.pth", "config_dit_mel_seed_uvit_xlsr_tiny.yml")
            config = yaml.safe_load(open(config_path))
            params = recursive_munch(config["model_params"])
            params.dit_type = "DiT"
            model = build_model(params, stage="DiT")
            model, _, _, _ = load_checkpoint(model, None, dit_path, load_only_params=True,
                                             ignore_modules=[], is_distributed=False)
            for key in model:
                model[key].eval()
                model[key].to(device)
            model.cfm.estimator.setup_caches(max_batch_size=1, max_seq_length=8192)

            campplus = CAMPPlus(feat_dim=80, embedding_size=192)
            campplus.load_state_dict(torch.load(load_custom_model_from_hf(
                "funasr/campplus", "campplus_cn_common.bin", config_filename=None), map_location="cpu"))
            campplus.eval().to(device)

            hift_config = yaml.safe_load(open("configs/hifigan.yml"))
            vocoder = HiFTGenerator(**hift_config["hift"], f0_predictor=ConvRNNF0Predictor(**hift_config["f0_predictor"]))
            vocoder.load_state_dict(torch.load(load_custom_model_from_hf(
                "FunAudioLLM/CosyVoice-300M", "hift.pt", None), map_location="cpu"))
            vocoder.eval().to(device)

            name = params.speech_tokenizer.name
            extractor = Wav2Vec2FeatureExtractor.from_pretrained(name)
            wav2vec = Wav2Vec2Model.from_pretrained(name)
            wav2vec.encoder.layers = wav2vec.encoder.layers[:params.speech_tokenizer.output_layer]
            wav2vec = wav2vec.to(device).eval().half()

            def semantic(waves_16k):
                inputs = extractor([w.cpu().numpy() for w in waves_16k], return_tensors="pt",
                                   return_attention_mask=True, padding=True, sampling_rate=16000).to(device)
                with torch.no_grad():
                    return wav2vec(inputs.input_values.half()).last_hidden_state.float()

            spect = config["preprocess_params"]["spect_params"]
            mel_args = {"n_fft": spect["n_fft"], "win_size": spect["win_length"], "hop_size": spect["hop_length"],
                        "num_mels": spect["n_mels"], "sampling_rate": config["preprocess_params"]["sr"],
                        "fmin": spect.get("fmin", 0), "fmax": None if spect.get("fmax", "None") == "None" else 8000,
                        "center": False}
            self.models = (model, semantic, vocoder, campplus, lambda x: mel_spectrogram(x, **mel_args), mel_args)
            self.device = device
        finally:
            os.chdir(cwd)

    async def ensure_loaded(self) -> None:
        async with self.loading:
            if not self.loaded:
                started = time.monotonic()
                await asyncio.to_thread(self._load)
                log.info("real-time model loaded in %.1f s", time.monotonic() - started)

    def unload(self) -> None:
        if self.models is None:
            return
        self.models, self.prompts = None, {}
        import gc
        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        log.info("real-time model unloaded")

    def prompt(self, voice_path: str, prompt_seconds: float):
        """The target voice's conditioning (first prompt_seconds of its sample), cached."""
        key = (voice_path, round(prompt_seconds, 2), os.path.getmtime(voice_path))
        if key in self.prompts:
            return self.prompts[key]
        import librosa
        import torch
        import torchaudio
        model, semantic, _vocoder, campplus, to_mel, mel_args = self.models
        sr = mel_args["sampling_rate"]
        wav, _ = librosa.load(voice_path, sr=sr)
        wav = torch.from_numpy(wav[: int(sr * prompt_seconds)]).to(self.device)
        with torch.no_grad():
            wav_16k = torchaudio.functional.resample(wav, sr, 16000)
            s_ori = semantic(wav_16k.unsqueeze(0))
            feat = torchaudio.compliance.kaldi.fbank(wav_16k.unsqueeze(0), num_mel_bins=80, dither=0,
                                                     sample_frequency=16000)
            style = campplus((feat - feat.mean(dim=0, keepdim=True)).unsqueeze(0))
            mel = to_mel(wav.unsqueeze(0))
            cond = model.length_regulator(s_ori, ylens=torch.LongTensor([mel.size(2)]).to(self.device),
                                          n_quantizers=3, f0=None)[0]
        self.prompts = {k: v for k, v in self.prompts.items() if k[0] != voice_path}   # one length per voice
        self.prompts[key] = (cond, mel, style)
        return self.prompts[key]


ENGINE = Engine()


# ------------------------------------------------------------------ stream state

@dataclass
class Settings:
    sample_rate: int = 48000
    block_ms: int = 200              # 200 ms / 6 steps: ~380 ms delay, half the GPU time per block
    diffusion_steps: int = 6         # (300 / 10: ~530 ms, a little closer to the voice)
    cfg_rate: float = 0.7
    prompt_seconds: float = 3.0
    silence_db: float = -50.0
    crossfade_ms: int = 40
    extra_ce_s: float = 2.5          # content-encoder left context
    extra_dit_s: float = 0.5         # diffusion left context (ce - dit = frames the DiT skips)
    extra_right_s: float = 0.02

    @classmethod
    def parse(cls, given: dict[str, Any], base: "Settings | None" = None) -> "Settings":
        s = Settings(**vars(base)) if base else Settings()
        def num(key, kind, low, high):
            if key in given and given[key] not in (None, ""):
                value = kind(given[key])
                if not (low <= value <= high) or (isinstance(value, float) and math.isnan(value)):
                    raise ValueError(f"{key} must be between {low} and {high}")
                setattr(s, key, value)
        if "sample_rate" in given and given["sample_rate"] not in (None, ""):
            if int(given["sample_rate"]) not in RATES:
                raise ValueError(f"sample_rate must be one of {', '.join(map(str, RATES))}")
            s.sample_rate = int(given["sample_rate"])
        num("block_ms", int, 100, 1000)
        num("diffusion_steps", int, 2, 30)
        num("cfg_rate", float, 0.0, 1.0)
        num("prompt_seconds", float, 1.0, 25.0)
        num("silence_db", float, -100.0, 0.0)
        num("crossfade_ms", int, 10, 80)
        return s


class Stream:
    """One client's rolling buffers (Seed-VC real-time-gui.py start_vc + audio_callback)."""

    def __init__(self, settings: Settings, voice: dict[str, Any]) -> None:
        import torch
        import torchaudio.transforms as tat
        self.s, self.voice = settings, voice
        dev = ENGINE.device
        sr = settings.sample_rate
        self.zc = sr // 50
        frames = lambda seconds: int(round(seconds * sr / self.zc)) * self.zc
        self.block = frames(settings.block_ms / 1000)
        self.block_16k = 320 * self.block // self.zc
        self.crossfade = frames(settings.crossfade_ms / 1000)
        self.sola_buffer_len = min(self.crossfade, 4 * self.zc)
        self.sola_search = self.zc
        self.extra = frames(settings.extra_ce_s)
        self.extra_right = frames(settings.extra_right_s)
        total = self.extra + self.crossfade + self.sola_search + self.block + self.extra_right
        self.input = torch.zeros(total, device=dev)
        self.input_16k = torch.zeros(320 * total // self.zc, device=dev)
        self.sola_buffer = torch.zeros(self.sola_buffer_len, device=dev)
        self.skip_head = self.extra // self.zc
        self.skip_tail = self.extra_right // self.zc
        self.return_length = (self.block + self.sola_buffer_len + self.sola_search) // self.zc
        fade = torch.sin(0.5 * math.pi * torch.linspace(0, 1, self.sola_buffer_len, device=dev)) ** 2
        self.fade_in, self.fade_out = fade, 1 - fade
        self.to_16k = tat.Resample(sr, 16000).to(dev)
        model_sr = ENGINE.models[-1]["sampling_rate"]
        self.from_model = tat.Resample(model_sr, sr).to(dev) if model_sr != sr else None
        self.ones = torch.ones(1, 1, self.sola_buffer_len, device=dev)
        self.pending = bytearray()
        self.quiet_blocks = 99           # blocks since the last loud one (hangover keeps word tails)
        self.hangover = max(1, round(600 / settings.block_ms))
        self.infer_ms: list[float] = []

    @property
    def latency_ms(self) -> int:
        return round((self.block + self.crossfade + self.sola_search + self.extra_right) * 1000 / self.s.sample_rate)

    def process(self, block_pcm: bytes) -> bytes:
        """One block in (int16 mono), one block out. Runs in a worker thread."""
        import numpy as np
        import torch
        import torch.nn.functional as F
        x = np.frombuffer(block_pcm, dtype="<i2").astype(np.float32) / 32768.0
        n = x.shape[0]
        rms = float(np.sqrt(np.mean(x * x) + 1e-12))
        loud = 20 * math.log10(rms) > self.s.silence_db
        self.quiet_blocks = 0 if loud else self.quiet_blocks + 1
        speaking = self.quiet_blocks <= self.hangover

        self.input[:-n] = self.input[n:].clone()
        self.input[-n:] = torch.from_numpy(x).to(self.input.device)
        tail_16k = 320 * (n // self.zc + 1)
        self.input_16k[:-self.block_16k] = self.input_16k[self.block_16k:].clone()
        self.input_16k[-tail_16k:] = self.to_16k(self.input[-n - 2 * self.zc:])[320:][-tail_16k:]

        if speaking:
            started = time.perf_counter()
            out = self._infer()
            if self.from_model is not None:
                out = self.from_model(out)
            self.infer_ms.append((time.perf_counter() - started) * 1000)
        else:
            out = torch.zeros(self.block + self.sola_buffer_len + self.sola_search, device=self.input.device)

        # SOLA: find where the new audio lines up best with the tail of the last block, then cross-fade.
        head = out[None, None, : self.sola_buffer_len + self.sola_search]
        num = F.conv1d(head, self.sola_buffer[None, None, :])
        den = torch.sqrt(F.conv1d(head ** 2, self.ones) + 1e-8)
        corr = num[0, 0] / den[0, 0]
        offset = int(torch.argmax(corr).item()) if corr.numel() > 1 else 0
        out = out[offset:]
        out[: self.sola_buffer_len] *= self.fade_in
        out[: self.sola_buffer_len] += self.sola_buffer * self.fade_out
        self.sola_buffer[:] = out[self.block: self.block + self.sola_buffer_len]
        pcm = (out[: self.block].clamp(-1, 1) * 32767).to(torch.int16).cpu().numpy()
        return pcm.astype("<i2").tobytes()

    def _infer(self):
        import torch
        model, semantic, vocoder, _campplus, _to_mel, mel_args = ENGINE.models
        sr, hop = mel_args["sampling_rate"], mel_args["hop_size"]
        cond_prompt, mel_prompt, style = ENGINE.prompt(self.voice["path"], self.s.prompt_seconds)
        with torch.no_grad():
            s_alt = semantic(self.input_16k.unsqueeze(0))
            skip = int((self.s.extra_ce_s - self.s.extra_dit_s) * 50)
            s_alt = s_alt[:, skip:]
            target = torch.LongTensor([(self.skip_head + self.return_length + self.skip_tail - skip) / 50 * sr // hop]).to(s_alt.device)
            cond = model.length_regulator(s_alt, ylens=target, n_quantizers=3, f0=None)[0]
            both = torch.cat([cond_prompt, cond], dim=1)
            mel = model.cfm.inference(both, torch.LongTensor([both.size(1)]).to(both.device), mel_prompt, style, None,
                                      n_timesteps=self.s.diffusion_steps, inference_cfg_rate=self.s.cfg_rate)
            wave = vocoder(mel[:, :, mel_prompt.size(-1):]).squeeze()
        out_len = self.return_length * sr // 50
        tail = self.skip_tail * sr // 50
        return wave[-out_len - tail: -tail] if tail else wave[-out_len:]


# ------------------------------------------------------------------ GPU queue

class Turn:
    """Holds one continuous GPU-queue turn while any stream is open (Rocsy's shared GPU queue)."""

    def __init__(self) -> None:
        self.lock = None
        self.cm = None
        self.users = 0
        self.guard = asyncio.Lock()
        self.release_task: asyncio.Task | None = None

    def _client(self):
        if self.lock is None:
            sys.path.insert(0, str(ROCSY_ROOT / "BunnyBytes/shared"))
            from bunnylock import BunnyLock
            self.lock = BunnyLock("VoiceRT")
            self.lock.start()
        return self.lock

    async def acquire(self, on_wait) -> None:
        async with self.guard:
            if self.release_task:
                self.release_task.cancel()
                self.release_task = None
            self.users += 1
            if self.cm is not None:
                return
            if not os.environ.get("QUEUE_INTERNAL_SECRET"):
                log.warning("QUEUE_INTERNAL_SECRET is not set: running without the GPU queue")
                return
            cm = self._client().lease(f"realtime-{uuid.uuid4().hex[:12]}", "Real-time voice conversion",
                                      is_continuous=True, uses_llm=False, model="seed-vc xlsr-tiny",
                                      on_wait=on_wait, timeout=QUEUE_WAIT_S)
            try:
                await cm.__aenter__()
            except BaseException:
                self.users -= 1
                raise
            self.cm = cm

    async def release(self) -> None:
        async with self.guard:
            self.users = max(0, self.users - 1)
            if self.users == 0 and self.release_task is None:
                self.release_task = asyncio.create_task(self._release_later())

    async def _release_later(self) -> None:
        # A reconnect within KEEP_LOADED_S keeps the model and the turn; after that both go.
        try:
            await asyncio.sleep(KEEP_LOADED_S)
        except asyncio.CancelledError:
            return
        async with self.guard:
            if self.users:
                return
            self.release_task = None
            async with ENGINE.gpu:
                ENGINE.unload()
            if self.cm is not None:
                cm, self.cm = self.cm, None
                await cm.__aexit__(None, None, None)


TURN = Turn()
SESSIONS: set[str] = set()
CHANNEL_NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")


class Channel:
    """A published stream and the listeners following it."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.publisher: str | None = None          # session id
        self.publisher_ws: web.WebSocketResponse | None = None
        self.info: dict[str, Any] = {}             # sample_rate, format, voice
        self.listeners: dict[int, asyncio.Queue] = {}

    def public(self) -> dict[str, Any]:
        return {"channel": self.name, "live": self.publisher is not None, "listeners": len(self.listeners),
                "sample_rate": self.info.get("sample_rate"), "voice": self.info.get("voice")}

    def push(self, item: Any) -> None:
        for queue in self.listeners.values():
            if queue.full():              # a slow listener skips audio rather than holding the stream up
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(item)

    async def tell_publisher(self) -> None:
        if self.publisher_ws is not None and not self.publisher_ws.closed:
            try:
                await self.publisher_ws.send_json({"type": "listeners", "channel": self.name, "count": len(self.listeners)})
            except ConnectionError:
                pass


CHANNELS: dict[str, Channel] = {}


def channel(name: str) -> Channel:
    if name not in CHANNELS:
        CHANNELS[name] = Channel(name)
    return CHANNELS[name]


def forget_channel(ch: Channel) -> None:
    if ch.publisher is None and not ch.listeners:
        CHANNELS.pop(ch.name, None)


async def start_publishing(name: str, sid: str, ws: web.WebSocketResponse, info: dict[str, Any]) -> Channel:
    if not CHANNEL_NAME.fullmatch(name or ""):
        raise ValueError("A channel name is 1-40 letters, digits, - or _")
    ch = channel(name)
    if ch.publisher not in (None, sid):
        raise ValueError(f"Channel {name} is already being published by another stream")
    ch.publisher, ch.publisher_ws, ch.info = sid, ws, info
    ch.push({"type": "hello", **info})
    await ch.tell_publisher()
    log.info("session %s publishes on channel %s", sid, name)
    return ch


def stop_publishing(ch: Channel | None, sid: str) -> None:
    if ch is None or ch.publisher != sid:
        return
    ch.publisher, ch.publisher_ws = None, None
    ch.push({"type": "end"})
    forget_channel(ch)


# ------------------------------------------------------------------ HTTP / WebSocket

def gpu_memory_mb() -> int | None:
    """What this process really holds on the GPU (the queue's card-wide reading includes games etc.)."""
    if not ENGINE.loaded:
        return 0
    import torch
    return round(torch.cuda.max_memory_reserved() / 2**20) if torch.cuda.is_available() else None


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "loaded": ENGINE.loaded, "sessions": len(SESSIONS),
                              "gpu_mb": gpu_memory_mb()})


async def channels(_request: web.Request) -> web.Response:
    return web.json_response({"channels": [ch.public() for ch in CHANNELS.values()]})


async def listen(request: web.Request) -> web.StreamResponse:
    """Receive the converted voice of whichever stream publishes on ?channel=NAME."""
    name = request.query.get("channel", "")
    ws = web.WebSocketResponse(max_msg_size=64 * 1024, heartbeat=20)
    await ws.prepare(request)
    if not CHANNEL_NAME.fullmatch(name):
        await ws.send_json({"type": "error", "message": "?channel= is 1-40 letters, digits, - or _"})
        await ws.close()
        return ws
    ch = channel(name)
    queue: asyncio.Queue = asyncio.Queue(maxsize=25)
    key = id(ws)
    ch.listeners[key] = queue
    await ws.send_json({"type": "hello", **ch.info} if ch.publisher else
                       {"type": "waiting", "channel": name, "message": "Nobody is publishing on this channel yet"})
    await ch.tell_publisher()

    async def drain_incoming() -> None:      # notice the listener closing
        async for _message in ws:
            pass

    reader = asyncio.create_task(drain_incoming())
    try:
        while not ws.closed:
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
            if getter not in done:
                getter.cancel()
                break
            item = getter.result()
            if isinstance(item, bytes):
                await ws.send_bytes(item)
            else:
                await ws.send_json(item)
    except ConnectionError:
        pass
    finally:
        reader.cancel()
        ch.listeners.pop(key, None)
        await ch.tell_publisher()
        forget_channel(ch)
    return ws


async def voices(_request: web.Request) -> web.Response:
    return web.json_response({"voices": [public(v) for v in list_voices()]})


async def realtime(request: web.Request) -> web.StreamResponse:
    ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024, heartbeat=20)
    await ws.prepare(request)
    sid = uuid.uuid4().hex[:8]

    async def fail(message: str) -> web.WebSocketResponse:
        await ws.send_json({"type": "error", "message": message})
        await ws.close()
        return ws

    # First message: the settings (query parameters work too, for clients that can't send one first).
    given: dict[str, Any] = dict(request.query)
    try:
        first = await ws.receive(timeout=15)
    except asyncio.TimeoutError:
        return await fail("Send the settings as the first message")
    if first.type == WSMsgType.TEXT:
        try:
            given.update(json.loads(first.data))
        except ValueError:
            return await fail("The first message must be JSON settings")
    elif first.type != WSMsgType.BINARY:
        return ws
    try:
        settings = Settings.parse(given)
    except (TypeError, ValueError) as exc:
        return await fail(str(exc))
    voice = find_voice(str(given.get("voice") or ""))
    if voice is None:
        return await fail("Unknown voice; GET /v1/realtime/voices lists them")
    if len(SESSIONS) >= MAX_SESSIONS:
        return await fail(f"{MAX_SESSIONS} real-time sessions are already running")

    SESSIONS.add(sid)
    acquired = False
    published: Channel | None = None
    try:
        async def on_wait(entry: dict[str, Any]) -> None:
            if entry.get("state") != "active":
                await ws.send_json({"type": "waiting", "position": entry.get("position"),
                                    "message": "Waiting for the GPU (another job is using it)"})
        try:
            await TURN.acquire(on_wait)
            acquired = True
        except asyncio.TimeoutError:
            return await fail("The GPU stayed busy; try again later")
        except Exception as exc:  # noqa: BLE001 - queue unreachable, cancelled from the Queue page...
            return await fail(f"Could not get a GPU turn: {exc}")
        await ENGINE.ensure_loaded()
        async with ENGINE.gpu:
            stream = await asyncio.to_thread(Stream, settings, voice)
            await asyncio.to_thread(ENGINE.prompt, voice["path"], settings.prompt_seconds)
            # One throwaway pass: the GPU's first run of each new block size compiles kernels for
            # seconds (4.8 s measured); without this the start of the first sentence came out late.
            await asyncio.to_thread(stream._infer)
        await ws.send_json({"type": "ready", "sample_rate": settings.sample_rate, "block_samples": stream.block,
                            "latency_ms": stream.latency_ms, "voice": public(voice), "format": "pcm_s16le mono"})
        log.info("session %s: %s at %d Hz, %d ms blocks", sid, voice["name"], settings.sample_rate, settings.block_ms)
        pub_info = lambda: {"sample_rate": settings.sample_rate, "format": "pcm_s16le mono",
                            "block_samples": stream.block, "voice": public(stream.voice)}
        if given.get("publish"):
            try:
                published = await start_publishing(str(given["publish"]), sid, ws, pub_info())
            except ValueError as exc:
                await ws.send_json({"type": "error", "message": str(exc), "fatal": False})
        if first.type == WSMsgType.BINARY:
            stream.pending.extend(first.data)
        last_stats = time.monotonic()
        block_bytes = stream.block * 2
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                stream.pending.extend(message.data)
                # Behind by more than a second: drop the oldest audio rather than fall further behind.
                limit = block_bytes * max(2, round(1000 / settings.block_ms))
                if len(stream.pending) > limit:
                    del stream.pending[: len(stream.pending) - limit]
                while len(stream.pending) >= block_bytes:
                    chunk = bytes(stream.pending[:block_bytes])
                    del stream.pending[:block_bytes]
                    async with ENGINE.gpu:
                        out = await asyncio.to_thread(stream.process, chunk)
                    await ws.send_bytes(out)
                    if published is not None:
                        published.push(out)
            elif message.type == WSMsgType.TEXT:
                try:
                    data = json.loads(message.data)
                    if data.get("type") == "voice":
                        found = find_voice(str(data.get("voice") or ""))
                        if found is None:
                            await ws.send_json({"type": "error", "message": "Unknown voice", "fatal": False})
                            continue
                        async with ENGINE.gpu:
                            await asyncio.to_thread(ENGINE.prompt, found["path"], settings.prompt_seconds)
                        stream.voice = found
                        await ws.send_json({"type": "voice", "voice": public(found)})
                        if published is not None:
                            published.info = pub_info()
                            published.push({"type": "voice", "voice": public(found)})
                    elif data.get("type") == "publish":
                        wanted = data.get("channel") or None
                        if published is not None and published.name != wanted:
                            stop_publishing(published, sid)
                            published = None
                        if wanted and published is None:
                            published = await start_publishing(str(wanted), sid, ws, pub_info())
                        await ws.send_json({"type": "publish", "channel": published.name if published else None,
                                            "listeners": len(published.listeners) if published else 0})
                    elif data.get("type") == "settings":
                        new = Settings.parse(data, settings)
                        if (new.sample_rate, new.block_ms, new.crossfade_ms) != (settings.sample_rate, settings.block_ms, settings.crossfade_ms):
                            raise ValueError("sample_rate, block_ms and crossfade_ms are fixed for a stream; reconnect to change them")
                        settings = stream.s = new
                        await ws.send_json({"type": "settings", "settings": vars(new)})
                except (TypeError, ValueError) as exc:
                    await ws.send_json({"type": "error", "message": str(exc), "fatal": False})
            if time.monotonic() - last_stats > 3 and stream.infer_ms:
                recent = stream.infer_ms[-20:]
                avg = sum(recent) / len(recent)
                await ws.send_json({"type": "stats", "infer_ms": round(avg, 1),
                                    "realtime_factor": round(avg / settings.block_ms, 2),
                                    "latency_ms": stream.latency_ms + round(avg)})
                stream.infer_ms = recent
                last_stats = time.monotonic()
    except Exception as exc:  # noqa: BLE001
        log.exception("session %s failed", sid)
        if not ws.closed:
            await fail(f"Conversion failed: {exc}")
    finally:
        SESSIONS.discard(sid)
        stop_publishing(published, sid)
        if acquired:
            await TURN.release()
        log.info("session %s closed", sid)
    return ws


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes([web.get("/health", health), web.get("/v1/realtime/voices", voices),
                    web.get("/v1/realtime", realtime), web.get("/v1/realtime/listen", listen),
                    web.get("/v1/realtime/channels", channels)])
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    web.run_app(make_app(), host=HOST, port=PORT, access_log=None)
