from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import wave
import base64
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config_store import OUTPUTS_DIR, ROOT, get_voice
from .script_parser import Segment, parse_script


INDEX_ROOT = ROOT / "models" / "legacy" / "IndexTTS2"
FISH_S2_ROOT = ROOT / "models" / "legacy" / "FishAudioS2"
DRAMA3_ROOT = ROOT / "models" / "legacy" / "FishDrama3"
INDEX_PYTHON = INDEX_ROOT / ".venv/bin/python"
FISH_S2_PYTHON = FISH_S2_ROOT / ".venv/bin/python"
INDEX_MODEL = INDEX_ROOT / "checkpoints"
FISH_S2_MODEL = FISH_S2_ROOT / "checkpoints/s2-pro"
DRAMA3_MODEL = DRAMA3_ROOT / "checkpoints"
DRAMA3_GGUF = "drama-3-backbone-8bit-experts-Q3_K_S.gguf"
DRAMA3_SERVER_BIN = DRAMA3_ROOT / "audio.cpp/build/linux-hip-release/bin/audiocpp_server"

ENGINE_NAMES = {
    "indextts2": "IndexTTS2",
    "fish_s2": "Fish Audio S2 Pro",
    "fish_drama3": "Fish Audio Drama 3",
}
GENERATION_LOCK = threading.Lock()
# sitecustomize.py in here applies ROCSY_SDPA_BACKEND inside the engines' own Python processes.
SDPA_SHIM = Path(__file__).resolve().parent / "sdpa_shim"


def _attention_env(settings: dict) -> dict:
    backend = str(settings.get("attention", "sdpa")).removeprefix("sdpa_")
    if backend not in ("flash", "efficient", "math"):
        return {}
    path = os.pathsep.join(filter(None, (str(SDPA_SHIM), os.environ.get("PYTHONPATH", ""))))
    return {"ROCSY_SDPA_BACKEND": backend, "PYTHONPATH": path}


class _FishS2Server:
    """Own the bundled Fish S2 API process so its checkpoints stay resident."""

    key = "fish_s2"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._port: int | None = None
        self._runtime_key: tuple | None = None
        self._logs: list[str] = []

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _read_logs(self, process: subprocess.Popen) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            line = line.rstrip()
            if line:
                with self._lock:
                    self._logs = (self._logs + [line])[-80:]

    def _request(self, path: str, payload: dict | None = None, timeout: float = 30) -> bytes:
        if self._port is None:
            raise RuntimeError("Fish S2 server is not running")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"http://127.0.0.1:{self._port}{path}", data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"} if data else {},
            method="POST" if data else "GET",
        )
        with urlopen(request, timeout=timeout) as response:
            return response.read()

    def _stop_locked(self) -> bool:
        process = self._process
        self._process = None
        self._port = None
        self._runtime_key = None
        if process is None or process.poll() is not None:
            return False
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=8)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return True

    def stop(self) -> bool:
        with self._lock:
            return self._stop_locked()

    def status(self) -> dict:
        with self._lock:
            active = self._process is not None and self._process.poll() is None
            return {self.key: active}

    def ensure(self, device: str, settings: dict, log: Callable[[str], None]) -> None:
        runtime_key = (device, bool(settings.get("half", False)), bool(settings.get("compile", False)),
                       str(settings.get("attention", "sdpa")))
        with self._lock:
            if self._process is not None and self._process.poll() is None and self._runtime_key == runtime_key:
                return
            self._stop_locked()
            self._logs = []
            self._port = self._free_port()
            command = [
                str(FISH_S2_PYTHON), "tools/api_server.py",
                "--listen", f"127.0.0.1:{self._port}",
                "--device", device,
                "--llama-checkpoint-path", str(FISH_S2_MODEL),
                "--decoder-checkpoint-path", str(FISH_S2_MODEL / "codec.pth"),
            ]
            if runtime_key[1]:
                command.append("--half")
            if runtime_key[2]:
                command.append("--compile")
            self._process = subprocess.Popen(
                command, cwd=FISH_S2_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", start_new_session=True,
                env={**os.environ, **_attention_env(settings)},
            )
            self._runtime_key = runtime_key
            threading.Thread(target=self._read_logs, args=(self._process,), daemon=True).start()

        log("Starting persistent Fish S2 server; loading model once into VRAM")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            with self._lock:
                process = self._process
                logs = self._logs[-12:]
            if process is None or process.poll() is not None:
                self.stop()
                raise RuntimeError("Fish S2 server stopped during startup:\n" + "\n".join(logs))
            try:
                self._request("/v1/health", timeout=2)
                log("Fish S2 is loaded and ready; later segments reuse this model")
                return
            except (HTTPError, URLError, TimeoutError, OSError):
                time.sleep(0.5)
        self.stop()
        raise RuntimeError("Fish S2 server did not become ready within three minutes")

    def synthesize(self, segment: Segment, voice: dict, output: Path, settings: dict,
                   log: Callable[[str], None]) -> None:
        text = f"[{segment.emotion}] {segment.text}" if segment.emotion else segment.text
        reference = base64.b64encode(Path(voice["audio_path"]).read_bytes()).decode("ascii")
        max_tokens = int(settings.get("max_new_tokens", 0))
        payload = {
            "text": text,
            "format": "wav",
            "streaming": False,
            "references": [{"audio": reference, "text": voice.get("transcript", "")}],
            "use_memory_cache": "on",
            "seed": int(settings.get("seed", 42)),
            "temperature": float(settings.get("temperature", 0.9)),
            "top_p": float(settings.get("top_p", 0.9)),
            "chunk_length": max(100, min(1000, int(settings.get("chunk_length", 300)))),
        }
        if max_tokens > 0:
            payload["max_new_tokens"] = max_tokens
        log("Generating with the resident Fish S2 model")
        try:
            audio = self._request("/v1/tts", payload, timeout=900)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[-2000:]
            raise RuntimeError(f"Fish S2 server request failed ({exc.code}): {detail}") from exc
        if len(audio) <= 44:
            raise RuntimeError("Fish S2 server returned no audio")
        output.write_bytes(audio)


class _Drama3Server(_FishS2Server):
    """Own audio.cpp's server for Drama 3 (native HIP build of the fish_drama3 port)."""

    key = "fish_drama3"

    def ensure(self, device: str, settings: dict, log: Callable[[str], None]) -> None:
        runtime_key = (int(settings.get("context_length", 4096)),)
        with self._lock:
            if self._process is not None and self._process.poll() is None and self._runtime_key == runtime_key:
                return
            self._stop_locked()
            self._logs = []
            self._port = self._free_port()
            config = DRAMA3_ROOT / "rocsy-server.json"
            # Every expert layer stays on the GPU (cpu_expert_layers=0): the Q3_K package is sized for it.
            config.write_text(json.dumps({
                "host": "127.0.0.1", "port": self._port, "backend": "hip", "threads": 8,
                "lazy_load": False, "max_loaded_models": 1, "idle_unload_ms": 0,
                "models": [{
                    "id": "drama-3", "family": "fish_drama3", "path": str(DRAMA3_MODEL / DRAMA3_GGUF),
                    "task": "tts", "mode": "offline",
                    "session_options": {"fish_drama3.cpu_expert_layers": 0,
                                        "fish_drama3.context_length": runtime_key[0]},
                }],
            }, indent=2))
            self._process = subprocess.Popen(
                [str(DRAMA3_SERVER_BIN), "--config", str(config)], cwd=DRAMA3_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                start_new_session=True,
            )
            self._runtime_key = runtime_key
            threading.Thread(target=self._read_logs, args=(self._process,), daemon=True).start()

        log("Starting the Drama 3 server; loading the model once into VRAM")
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            with self._lock:
                process = self._process
                logs = self._logs[-12:]
            if process is None or process.poll() is not None:
                self.stop()
                raise RuntimeError("Drama 3 server stopped during startup:\n" + "\n".join(logs))
            try:
                models = json.loads(self._request("/v1/models", timeout=2))
                if any(item.get("id") == "drama-3" for item in models.get("data", [])):
                    log("Drama 3 is loaded and ready; later segments reuse this model")
                    return
            except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                pass
            time.sleep(0.5)
        self.stop()
        raise RuntimeError("Drama 3 server did not become ready within ten minutes")

    def synthesize(self, segment: Segment, voice: dict, output: Path, settings: dict,
                   log: Callable[[str], None]) -> None:
        text = f"[{segment.emotion}] {segment.text}" if segment.emotion else segment.text
        options = {
            "temperature": float(settings.get("temperature", 1.0)),
            "top_p": float(settings.get("top_p", 0.9)),
            "seed": int(settings.get("seed", 42)),
        }
        if int(settings.get("max_new_tokens", 0)) > 0:
            options["max_tokens"] = int(settings["max_new_tokens"])
        payload = {
            "model": "drama-3", "input": text,
            "voice_ref": {"type": "path", "path": str(voice["audio_path"])},
            "reference_text": voice.get("transcript", ""),
            "options": options,
        }
        log("Generating with the resident Drama 3 model")
        try:
            audio = self._request("/v1/audio/speech", payload, timeout=900)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[-2000:]
            raise RuntimeError(f"Drama 3 server request failed ({exc.code}): {detail}") from exc
        if len(audio) <= 44 or audio[:4] != b"RIFF":
            raise RuntimeError("Drama 3 server returned no audio")
        output.write_bytes(audio)


FISH_S2_SERVER = _FishS2Server()
DRAMA3_SERVER = _Drama3Server()
atexit.register(FISH_S2_SERVER.stop)
atexit.register(DRAMA3_SERVER.stop)


def loaded_model_status() -> dict:
    """Return models intentionally kept alive by BunnyVoice."""
    return {**FISH_S2_SERVER.status(), **DRAMA3_SERVER.status()}


def unload_models() -> dict:
    """Release all persistent model processes. Safe to call repeatedly."""
    return {"unloaded": [server.key for server in (FISH_S2_SERVER, DRAMA3_SERVER) if server.stop()]}


def _split_text(text: str, max_chars: int, min_chars: int) -> list[str]:
    """Split long model inputs at natural boundaries without dropping text."""
    if len(text) <= max_chars:
        return [text]
    units = [part.strip() for part in re.split(r"(?<=[.!?…])\s+|\n+", text) if part.strip()]
    pieces: list[str] = []
    for unit in units:
        if len(unit) <= max_chars:
            pieces.append(unit)
            continue
        words = unit.split()
        chunk = ""
        for word in words:
            candidate = f"{chunk} {word}".strip()
            if chunk and len(candidate) > max_chars:
                pieces.append(chunk)
                chunk = word
            else:
                chunk = candidate
        if chunk:
            pieces.append(chunk)

    packed: list[str] = []
    for piece in pieces:
        candidate = f"{packed[-1]} {piece}" if packed else piece
        if packed and len(candidate) <= max_chars:
            packed[-1] = candidate
        else:
            packed.append(piece)
    if len(packed) > 1 and len(packed[-1]) < min_chars:
        combined = f"{packed[-2]} {packed[-1]}"
        if len(combined) <= int(max_chars * 1.25):
            packed[-2:] = [combined]
    return packed or [text]


def _prepare_segments(segments: list[Segment], settings: dict) -> list[Segment]:
    if not settings.get("auto_split", True):
        return [segment for segment in segments if segment.kind != "pause" or segment.seconds > 0]
    max_chars = max(80, min(2000, int(settings.get("max_chars", 280))))
    min_chars = max(0, min(max_chars, int(settings.get("min_chars", 80))))
    prepared: list[Segment] = []
    for segment in segments:
        if segment.kind == "pause":
            if segment.seconds > 0:
                prepared.append(segment)
            continue
        for text in _split_text(segment.text, max_chars, min_chars):
            prepared.append(Segment(text=text, voice=segment.voice, emotion=segment.emotion))
    return prepared


def model_file_statuses() -> list[dict]:
    """Return model readiness without importing PyTorch in the engine subprocesses."""
    specs = [
        ("indextts2", INDEX_PYTHON, INDEX_MODEL, ["gpt.pth", "s2mel.pth"]),
        ("fish_s2", FISH_S2_PYTHON, FISH_S2_MODEL, ["codec.pth", "model.safetensors.index.json"]),
        ("fish_drama3", DRAMA3_SERVER_BIN, DRAMA3_MODEL, [DRAMA3_GGUF]),
    ]
    return [
        {
            "id": key,
            "name": ENGINE_NAMES[key],
            "ready": python.is_file() and model_dir.is_dir()
            and all((model_dir / name).is_file() for name in required),
            "gpu": False,
            "gpu_pending": python.is_file(),
        }
        for key, python, model_dir, required in specs
    ]


def _run(command: list[str], cwd: Path, log: Callable[[str], None], env_extra: dict | None = None) -> None:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.update(env_extra or {})
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    assert process.stdout is not None
    tail = []
    for line in process.stdout:
        line = line.rstrip()
        if line:
            tail.append(line)
            tail = tail[-80:]
            log(line)
    code = process.wait()
    if code:
        raise RuntimeError("\n".join(tail[-25:]) or f"Model process exited with code {code}")


def resolve_device(python: Path, requested: str) -> str:
    if requested == "cpu":
        return "cpu"
    result = subprocess.run(
        [str(python), "-c", "import torch; print(int(torch.cuda.is_available()))"],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    available = result.returncode == 0 and result.stdout.strip().endswith("1")
    if requested == "gpu" and not available:
        raise RuntimeError("The selected engine cannot see the GPU. Choose Auto or CPU.")
    return "cuda" if available else "cpu"


@lru_cache(maxsize=1)
def model_statuses() -> list[dict]:
    quick = model_file_statuses()
    specs = [(item["id"], {
        "indextts2": INDEX_PYTHON, "fish_s2": FISH_S2_PYTHON, "fish_drama3": DRAMA3_SERVER_BIN,
    }[item["id"]], item["ready"]) for item in quick]
    def probe(spec) -> dict:
        key, python, ready = spec
        gpu = key == "fish_drama3" and python.is_file()  # HIP-only build: no CPU path to probe
        if python.is_file() and key != "fish_drama3":
            try:
                gpu = resolve_device(python, "auto") == "cuda"
            except Exception:
                pass
        return {"id": key, "name": ENGINE_NAMES[key], "ready": ready, "gpu": gpu, "gpu_pending": False}

    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        return list(pool.map(probe, specs))


def _index(segment: Segment, voice: dict, output: Path, device: str, settings: dict, log) -> None:
    command = [
        str(INDEX_PYTHON), "-m", "indextts.cli_v2", "synth",
        "--text", segment.text,
        "--voice", voice["audio_path"],
        "--output", str(output),
        "--model-dir", str(INDEX_MODEL),
        "--device", device,
        "--force",
    ]
    command.append("--fp16" if device == "cuda" and settings.get("fp16", True) else "--no-fp16")
    for key, flag in (
        ("deepspeed", "deepspeed"), ("cuda_kernel", "cuda-kernel"),
        ("accel", "accel"), ("torch_compile", "torch-compile"),
    ):
        command.append(f"--{flag}" if settings.get(key, False) else f"--no-{flag}")
    if settings.get("verbose", False):
        command.append("--verbose")
    emotion = segment.emotion or settings.get("emotion", "")
    if emotion:
        command += ["--emotion-text", emotion]
        command += ["--emotion-weight", str(settings.get("emotion_weight", 1.0))]
    _run(command, INDEX_ROOT, log, _attention_env(settings))


def _index_batch(tasks: list[tuple[Segment, dict, Path]], device: str, settings: dict,
                 work: Path, log) -> None:
    """Generate every speech segment with one model load via the bundled batch CLI."""
    manifest = work / "index-batch.jsonl"
    rows = []
    for segment, voice, output in tasks:
        row = {"text": segment.text, "voice": voice["audio_path"], "output": str(output)}
        emotion = segment.emotion or settings.get("emotion", "")
        if emotion:
            row.update(emotion_text=emotion, emotion_weight=settings.get("emotion_weight", 1.0))
        rows.append(json.dumps(row, ensure_ascii=False))
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    command = [
        str(INDEX_PYTHON), "-m", "indextts.cli_v2", "batch",
        "--batch-file", str(manifest), "--model-dir", str(INDEX_MODEL),
        "--device", device, "--force",
    ]
    command.append("--fp16" if device == "cuda" and settings.get("fp16", True) else "--no-fp16")
    for key, flag in (
        ("deepspeed", "deepspeed"), ("cuda_kernel", "cuda-kernel"),
        ("accel", "accel"), ("torch_compile", "torch-compile"),
    ):
        command.append(f"--{flag}" if settings.get(key, False) else f"--no-{flag}")
    if settings.get("verbose", False):
        command.append("--verbose")
    _run(command, INDEX_ROOT, log, _attention_env(settings))


def _fish_s2(segment: Segment, voice: dict, output: Path, device: str, settings: dict, work: Path, log) -> None:
    FISH_S2_SERVER.ensure(device, settings, log)
    FISH_S2_SERVER.synthesize(segment, voice, output, settings, log)


def _fish_drama3(segment: Segment, voice: dict, output: Path, device: str, settings: dict, work: Path, log) -> None:
    DRAMA3_SERVER.ensure(device, settings, log)
    DRAMA3_SERVER.synthesize(segment, voice, output, settings, log)


def _silence(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\0\0" * int(44100 * seconds))


def _concat(parts: list[Path], output: Path, settings: dict | None = None) -> None:
    settings = settings or {}
    normalize = bool(settings.get("normalize_audio", False))
    crossfade = max(0, min(250, int(settings.get("crossfade_ms", 0)))) / 1000
    if len(parts) == 1 and not normalize:
        shutil.copy2(parts[0], output)
        return
    command = ["ffmpeg", "-y"]
    filters = []
    labels = []
    for index, part in enumerate(parts):
        command += ["-i", str(part)]
        audio_filter = "aresample=44100,aformat=sample_fmts=s16:channel_layouts=mono"
        if normalize:
            audio_filter += ",loudnorm=I=-18:TP=-1.5:LRA=11"
        filters.append(f"[{index}:a]{audio_filter}[a{index}]")
        labels.append(f"[a{index}]")
    if crossfade and len(parts) > 1:
        previous = "a0"
        for index in range(1, len(parts)):
            target = "out" if index == len(parts) - 1 else f"x{index}"
            filters.append(f"[{previous}][a{index}]acrossfade=d={crossfade:.3f}:c1=tri:c2=tri[{target}]")
            previous = target
    else:
        filters.append("".join(labels) + f"concat=n={len(parts)}:v=0:a=1[out]")
    command += ["-filter_complex", ";".join(filters), "-map", "[out]", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])


def assemble_outputs(
    sources: list[Path],
    output: Path,
    gap_ms: int = 0,
    crossfade_ms: int = 0,
    normalize_audio: bool = False,
    trims: list[tuple[float, float | None]] | None = None,
    effects: list[dict] | None = None,
) -> None:
    """Assemble already-rendered takes without loading a speech model."""
    if not sources:
        raise ValueError("Add at least one take to the timeline")
    gap_ms = max(0, min(5000, int(gap_ms)))
    with tempfile.TemporaryDirectory(prefix="bunnyvoice-suite-") as temp:
        temp_dir = Path(temp)
        trimmed_sources: list[Path] = []
        trims = trims or [(0.0, None)] * len(sources)
        effects = effects or [{} for _ in sources]
        if len(trims) != len(sources):
            raise ValueError("Every Suite source needs matching trim data")
        if len(effects) != len(sources):
            raise ValueError("Every Suite source needs matching effect data")
        for index, (source, trim, effect) in enumerate(zip(sources, trims, effects)):
            start, end = trim
            start = max(0.0, float(start))
            gain_db = max(-120.0, min(24.0, float(effect.get("gain_db", 0.0))))
            fade_in = max(0.0, float(effect.get("fade_in", 0.0)))
            fade_out = max(0.0, float(effect.get("fade_out", 0.0)))
            if start <= 0 and end is None and gain_db == 0 and not fade_in and not fade_out:
                trimmed_sources.append(source)
                continue
            trimmed = temp_dir / f"trim-{index:03d}.wav"
            command = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.4f}", "-i", str(source)]
            if end is not None:
                duration = float(end) - start
                if duration <= 0:
                    raise ValueError("A Suite clip ends before it starts")
                command += ["-t", f"{duration:.4f}"]
            filters = []
            if gain_db:
                filters.append(f"volume={gain_db:.3f}dB")
            if fade_in:
                filters.append(f"afade=t=in:st=0:d={min(fade_in, duration if end is not None else fade_in):.4f}")
            if fade_out and end is not None:
                fade_duration = min(fade_out, duration)
                filters.append(f"afade=t=out:st={max(0.0, duration - fade_duration):.4f}:d={fade_duration:.4f}")
            if filters:
                command += ["-af", ",".join(filters)]
            command += ["-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(trimmed)]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            if result.returncode or not trimmed.is_file() or trimmed.stat().st_size <= 44:
                raise RuntimeError(result.stderr[-2000:] or "A trimmed Suite clip could not be decoded")
            trimmed_sources.append(trimmed)
        parts: list[Path] = []
        gap = temp_dir / "gap.wav"
        if gap_ms:
            _silence(gap, gap_ms / 1000)
        for index, source in enumerate(trimmed_sources):
            parts.append(source)
            if gap_ms and index < len(trimmed_sources) - 1:
                parts.append(gap)
        _concat(parts, output, {
            "crossfade_ms": 0 if gap_ms else max(0, min(250, int(crossfade_ms))),
            "normalize_audio": bool(normalize_audio),
        })


def generate_script(script: str, engine: str, voice_id: str, device_request: str, config: dict,
                    update: Callable[[str, int, str], None]) -> Path:
    if engine not in ENGINE_NAMES:
        raise ValueError("Unknown engine")
    default_voice = get_voice(voice_id)
    if not default_voice:
        raise ValueError("Choose a saved voice profile first")
    settings = config.get("engines", {}).get(engine, {})
    default_emotion = settings.get("emotion", "") if engine == "indextts2" else ""
    segments = parse_script(
        script,
        default_voice["name"],
        default_emotion,
        preserve_unknown_brackets=engine in {"fish_s2", "fish_drama3"},
    )
    segment_settings = config.get("segments", {})
    segments = _prepare_segments(segments, segment_settings)
    if not segments:
        raise ValueError("The script contains no speech")
    if engine == "fish_drama3":
        device = "cuda"  # audio.cpp's HIP backend; it has no CPU mode in Rocsy
    else:
        device = resolve_device({"indextts2": INDEX_PYTHON, "fish_s2": FISH_S2_PYTHON}[engine], device_request)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    output = OUTPUTS_DIR / f"{engine}-{time.strftime('%Y%m%d-%H%M%S')}.wav"

    with GENERATION_LOCK, tempfile.TemporaryDirectory(prefix="bunnyvoice-") as temp:
        work = Path(temp)
        parts: list[Path] = []
        total = len(segments)
        speech_count = sum(segment.kind == "speech" for segment in segments)
        index_tasks: list[tuple[Segment, dict, Path]] = []
        for index, segment in enumerate(segments, 1):
            update(f"Segment {index}/{total}", int((index - 1) / total * 90), segment.text[:90])
            part = work / f"part-{index:03d}.wav"
            if segment.kind == "pause":
                _silence(part, segment.seconds)
            else:
                voice = get_voice(segment.voice) or default_voice
                if not voice.get("transcript") and engine.startswith("fish"):
                    raise ValueError(f"Voice '{voice['name']}' needs a reference transcript for Fish Audio")
                if engine == "indextts2":
                    if segment_settings.get("index_batch", True) and speech_count > 1:
                        index_tasks.append((segment, voice, part))
                    else:
                        _index(segment, voice, part, device, settings, lambda line: update("Generating", max(2, int(index / total * 85)), line))
                elif engine == "fish_s2":
                    _fish_s2(segment, voice, part, device, settings, work / f"s2-{index}", lambda line: update("Generating", max(2, int(index / total * 85)), line))
                else:
                    _fish_drama3(segment, voice, part, device, settings, work / f"drama3-{index}", lambda line: update("Generating", max(2, int(index / total * 85)), line))
            parts.append(part)
        if index_tasks:
            update("Generating batch", 8, f"Loading IndexTTS2 once for {len(index_tasks)} speech segments")
            _index_batch(index_tasks, device, settings, work, lambda line: update("Generating batch", 70, line))

        gap_ms = max(0, min(5000, int(segment_settings.get("gap_ms", 0))))
        if gap_ms and len(parts) > 1:
            spaced: list[Path] = []
            for index, part in enumerate(parts):
                spaced.append(part)
                if index < len(parts) - 1 and segments[index].kind == segments[index + 1].kind == "speech":
                    gap = work / f"gap-{index:03d}.wav"
                    _silence(gap, gap_ms / 1000)
                    spaced.append(gap)
            parts = spaced
        update("Assembling audio", 94, "Joining generated segments")
        _concat(parts, output, segment_settings)
    update("Complete", 100, output.name)
    return output
