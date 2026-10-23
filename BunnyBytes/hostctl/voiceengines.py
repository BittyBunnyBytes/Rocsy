"""Explicit Core Backend installers for the Voice service's engines and add-ons."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from paths import ROOT as ROCSY_ROOT

VOICE = Path(os.getenv("CORE_VOICE_DIR", ROCSY_ROOT / "BunnyBytes/voice"))
STATE = Path(os.getenv("CORE_VOICE_STATE", ROCSY_ROOT / "core-backend-extras/voice"))
LEGACY = VOICE / "models/legacy"
QWEN = VOICE / "models/qwen/Qwen3-TTS-12Hz-1.7B-Base"
QWEN_DESIGN = VOICE / "models/qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
LOCK = threading.Lock()
PYTHON311 = shutil.which("python3.11") or str(Path.home() / ".local/bin/python3.11")
WORKER_READY = VOICE / ".worker-venv/.rocsy-ready"
DEEPSPEED_VERSION = "0.17.1"  # IndexTTS2 v2.0.0 optional dependency
# Qwen3-TTS optional FlashAttention 2, built for the Voice image's own torch and mounted into it.
FLASH_DIR = Path(os.getenv("CORE_VOICE_FLASH_DIR", STATE / "flash-attn"))
FLASH_SCRIPT = Path(__file__).resolve().with_name("build-voice-flash-attention.sh")
VOICE_STATE_URL = os.getenv("CORE_VOICE_STATE_URL", "http://127.0.0.1:8950/Voice/api/state")
ADDONS = ("indextts2-deepspeed", "qwen3-flash")
DRAMA3_GGUF = "drama-3-backbone-8bit-experts-Q3_K_S.gguf"
DRAMA3_SERVER = LEGACY / "FishDrama3/audio.cpp/build/linux-hip-release/bin/audiocpp_server"
UV = shutil.which("uv") or str(Path.home() / ".hermes/bin/uv")
SPECS = {
    "qwen3": {"name": "Qwen3-TTS 1.7B Base", "repo": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
              "model": QWEN, "files": ("config.json", "model.safetensors", "speech_tokenizer/config.json")},
    # Voice creation for Add character: designs a reference clip from a text description,
    # which Qwen3-TTS Base (or any engine) then clones. Same Python backend as Base.
    "qwen3_design": {"name": "Qwen3-TTS 1.7B VoiceDesign", "repo": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
                     "model": QWEN_DESIGN, "files": ("config.json", "model.safetensors", "speech_tokenizer/config.json"),
                     "creates_voices": True},
    "indextts2": {"name": "IndexTTS2", "repo": "IndexTeam/IndexTTS-2",
                  "model": LEGACY / "IndexTTS2/checkpoints", "files": ("gpt.pth", "s2mel.pth"),
                  "source": LEGACY / "IndexTTS2", "git": "https://github.com/index-tts/index-tts.git",
                  "ref": "v2.0.0", "python": "python3.11", "torch": "2.8.0", "rocm": "rocm6.4"},
    "fish_s2": {"name": "Fish Audio S2 Pro", "repo": "fishaudio/s2-pro",
                "model": LEGACY / "FishAudioS2/checkpoints/s2-pro", "files": ("codec.pth", "model.safetensors.index.json"),
                "source": LEGACY / "FishAudioS2", "git": "https://github.com/fishaudio/fish-speech.git",
                "ref": "befe4001745417f8c42131739d862b8a6fdbd15a", "python": "python3.11", "torch": "2.8.0", "rocm": "rocm6.4"},
    # Drama 3 runs in audio.cpp's native fish_drama3 port (HIP), not PyTorch: the official FP8
    # checkpoint is 41.5 GB and NVIDIA-only. Q3_K experts + Q8_0 backbone (18.9 GiB) fit the
    # 24 GB card with every expert layer on the GPU.
    "fish_drama3": {"name": "Fish Audio Drama 3", "repo": "Jmica/Drama-3-GGUF",
                    "model": LEGACY / "FishDrama3/checkpoints", "files": (DRAMA3_GGUF,),
                    "source": LEGACY / "FishDrama3/audio.cpp", "git": "https://github.com/0xShug0/audio.cpp.git",
                    "ref": "0df48c7c6d79cdd8995a75531449b953c7f6bb7f", "native": True},
}


def _read(engine: str) -> dict:
    try:
        return json.loads((STATE / f"{engine}.json").read_text())
    except (OSError, ValueError):
        return {}


def _write(engine: str, **fields) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    data = {**_read(engine), **fields}
    (STATE / f"{engine}.json").write_text(json.dumps(data))


def _active(engine: str) -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", f"core-voice-{engine}.service"],
                          capture_output=True).returncode == 0


def _ready(engine: str) -> tuple[bool, bool]:
    spec = SPECS[engine]
    model = spec["model"]
    weights = all((model / name).is_file() and (model / name).stat().st_size > 0 for name in spec["files"])
    if engine in ("qwen3", "qwen3_design"):
        # Its Python/ROCm backend is part of the Voice image, built by Compose.
        return weights, (VOICE / "Dockerfile").is_file()
    source = spec["source"]
    if spec.get("native"):
        return weights, DRAMA3_SERVER.is_file() and (VOICE / ".worker-venv/bin/python").is_file() and WORKER_READY.is_file()
    backend = ((source / ".venv/bin/python").is_file() and (source / "pyproject.toml").is_file()
               and (VOICE / ".worker-venv/bin/python").is_file() and WORKER_READY.is_file())
    return weights, backend


def _deepspeed_status() -> dict:
    python = LEGACY / "IndexTTS2/.venv/bin/python"
    version = ""
    if python.is_file():
        try:
            result = subprocess.run([str(python), "-c",
                                     "from importlib.metadata import version; print(version('deepspeed'))"],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                version = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    meta = _read("indextts2-deepspeed")
    return {"installed": version == DEEPSPEED_VERSION, "version": version,
            "required_version": DEEPSPEED_VERSION, "running": _active("indextts2-deepspeed"),
            "phase": meta.get("phase", ""), "error": meta.get("error", "")}


def _flash_status() -> dict:
    site = FLASH_DIR / "site"
    meta = _read("qwen3-flash")
    try:
        torch = (site / ".rocsy-torch").read_text().strip()
    except OSError:
        torch = ""
    return {"installed": any(site.glob("flash_attn_2_cuda*.so")), "torch": torch,
            "running": _active("qwen3-flash"), "phase": meta.get("phase", ""), "error": meta.get("error", "")}


def status() -> dict:
    engines = []
    for engine, spec in SPECS.items():
        weights, backend = _ready(engine)
        meta = _read(engine)
        engines.append({"id": engine, "name": spec["name"], "repo": spec["repo"],
                        "weights": weights, "backend": backend, "installed": weights and backend,
                        "running": _active(engine), "phase": meta.get("phase", ""),
                        "error": meta.get("error", ""), "path": str(spec["model"]),
                        "creates_voices": bool(spec.get("creates_voices")),
                        **({"deepspeed": _deepspeed_status()} if engine == "indextts2" else {}),
                        **({"flash": _flash_status()} if engine == "qwen3" else {})})
    return {"engines": engines}


def _command(args: list[str], log, *, cwd: Path | None = None, env_extra: dict | None = None) -> None:
    print("$ " + " ".join(args), file=log, flush=True)
    result = subprocess.run(args, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "HF_HUB_DISABLE_TELEMETRY": "1",
                                 **(env_extra or {})})
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0]}; see log")


def _backend(engine: str, log) -> None:
    spec = SPECS[engine]
    source = spec["source"]
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        staging = source.with_name(f".{source.name}.install-{os.getpid()}")
        try:
            _command(["git", "clone", "--filter=blob:none", spec["git"], str(staging)], log)
            _command(["git", "checkout", spec["ref"]], log, cwd=staging)
            staging.rename(source)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    if not (source / "pyproject.toml").is_file():
        raise RuntimeError(f"Existing {source} lacks pyproject.toml; refusing to overwrite it")
    python = source / ".venv/bin/python"
    if python.is_file():
        module = "indextts" if engine == "indextts2" else "fish_speech"
        valid = subprocess.run([str(python), "-c", f"import torch, {module}; assert torch.version.hip"],
                               capture_output=True, timeout=30)
        if valid.returncode == 0:
            return
    else:
        _write(engine, phase="installing backend")
        _command([PYTHON311, "-m", "venv", str(source / ".venv")], log)
    _write(engine, phase="installing backend")
    _command([str(python), "-m", "pip", "install", "--upgrade", "pip"], log)
    index = f"https://download.pytorch.org/whl/{spec['rocm']}"
    _command([str(python), "-m", "pip", "install", f"torch=={spec['torch']}",
              f"torchaudio=={spec['torch']}", "--index-url", index], log)
    _command([str(python), "-m", "pip", "install", "-e", str(source)], log)
    if engine == "indextts2":
        _command([str(python), "-m", "pip", "install", "fastapi", "uvicorn", "python-multipart"], log)
    # An interrupted pip install must not be reported as a complete backend.
    _command([str(python), "-c", "import torch; assert torch.version.hip; import huggingface_hub"], log)


def _native_backend(engine: str, log) -> None:
    """Build audio.cpp's server for the RDNA3 card with only the Drama 3 family compiled in."""
    spec = SPECS[engine]
    source = spec["source"]
    if DRAMA3_SERVER.is_file():
        return
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        staging = source.with_name(f".{source.name}.install-{os.getpid()}")
        try:
            _command(["git", "clone", "--filter=blob:none", spec["git"], str(staging)], log)
            _command(["git", "checkout", spec["ref"]], log, cwd=staging)
            staging.rename(source)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    if not (source / "scripts/build_linux.sh").is_file():
        raise RuntimeError(f"Existing {source} is not an audio.cpp checkout; refusing to overwrite it")
    _write(engine, phase="building audio.cpp (HIP)")
    rocm = os.getenv("ROCM_PATH", "/opt/rocm")
    _command(["bash", "scripts/build_linux.sh", "--backend", "hip", "--gpu-targets", os.getenv("GPU_ARCH", "gfx1100"),
              "--build-type", "Release", "--build-dir", "build/linux-hip-release", "--model-set", "custom",
              "--models", "fish_drama3", "--target", "audiocpp_server"], log, cwd=source,
             env_extra={"ROCM_PATH": rocm, "PATH": f"{rocm}/bin:{os.environ.get('PATH', '')}"})
    if not DRAMA3_SERVER.is_file():
        raise RuntimeError("audio.cpp build finished without audiocpp_server")


def _worker_backend(log) -> None:
    """Keep the loopback API independent of any particular inference engine."""
    python = VOICE / ".worker-venv/bin/python"
    if python.is_file():
        result = subprocess.run([str(python), "-c", "import fastapi, uvicorn, multipart"],
                                capture_output=True, timeout=20)
        if result.returncode == 0:
            WORKER_READY.touch()
            return
    else:
        _command([PYTHON311, "-m", "venv", str(VOICE / ".worker-venv")], log)
    _command([str(python), "-m", "pip", "install", "fastapi==0.115.12",
              "uvicorn==0.34.2", "python-multipart"], log)
    _command([str(python), "-c", "import fastapi, uvicorn, multipart"], log)
    WORKER_READY.touch()


def _hub_python(log) -> Path:
    installer = STATE / ".venv"
    python = installer / "bin/python"
    if not python.is_file():
        _command([PYTHON311, "-m", "venv", str(installer)], log)
        _command([str(python), "-m", "pip", "install", "huggingface_hub>=0.34,<1"], log)
    return python


def install(engine: str) -> int:
    spec = SPECS[engine]
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / f"{engine}.log").open("w") as log:
        try:
            if engine not in ("qwen3", "qwen3_design"):
                _write(engine, phase="preparing backend", error="")
                (_native_backend if spec.get("native") else _backend)(engine, log)
                _write(engine, phase="installing Voice worker")
                _worker_backend(log)
            _write(engine, phase="downloading model", error="")
            model = spec["model"]
            model.mkdir(parents=True, exist_ok=True)
            weights, _ = _ready(engine)
            script = ("from huggingface_hub import snapshot_download; import sys; "
                      "snapshot_download(repo_id=sys.argv[1], local_dir=sys.argv[2], "
                      "allow_patterns=sys.argv[3:] or None)")
            if not weights:
                # Native engines ship several quant files; fetch only the one Rocsy runs.
                _command([str(_hub_python(log)), "-c", script, spec["repo"], str(model),
                          *(spec["files"] if spec.get("native") else ())], log)
            weights, backend = _ready(engine)
            if not weights or not backend:
                raise RuntimeError("Required backend or checkpoint files are missing after download")
            if engine == "qwen3":
                _write(engine, phase="starting Voice service")
                _command(["docker", "compose", "up", "-d", "--build", "--no-deps", "voice"],
                         log, cwd=VOICE.parent)
            elif engine == "qwen3_design":
                pass  # the Voice service loads it from the mounted models folder per preview
            else:
                _command(["systemctl", "--user", "restart", "rocsy-voice-worker.service"], log)
            _write(engine, phase="ready", error="", finished=time.time())
            return 0
        except Exception as exc:
            print(str(exc), file=log, flush=True)
            _write(engine, phase="failed", error=str(exc)[:300], finished=time.time())
            return 1


def install_deepspeed() -> int:
    """Install IndexTTS2's pinned optional dependency without replacing ROCm PyTorch."""
    STATE.mkdir(parents=True, exist_ok=True)
    python = LEGACY / "IndexTTS2/.venv/bin/python"
    with (STATE / "indextts2-deepspeed.log").open("w") as log:
        try:
            if not python.is_file():
                raise RuntimeError("Install the IndexTTS2 engine first")
            _write("indextts2-deepspeed", phase="checking ROCm", error="")
            baseline = subprocess.run([str(python), "-c", "import torch; print(torch.__version__, torch.version.hip or '')"],
                                      capture_output=True, text=True, timeout=30, check=True).stdout.strip()
            if not baseline.split() or len(baseline.split()) != 2:
                raise RuntimeError("IndexTTS2 does not have a ROCm PyTorch build")
            if not Path(UV).is_file():
                raise RuntimeError("uv is required to install into IndexTTS2's Python environment")
            _write("indextts2-deepspeed", phase="installing build dependencies")
            _command([UV, "pip", "install", "--python", str(python), "hjson", "ninja", "py-cpuinfo", "wheel"], log)
            _write("indextts2-deepspeed", phase="installing DeepSpeed")
            _command([UV, "pip", "install", "--python", str(python), "--no-deps", "--no-build-isolation",
                      f"deepspeed=={DEEPSPEED_VERSION}"], log,
                     env_extra={"DS_BUILD_OPS": "0", "DS_BUILD_AIO": "0", "DS_BUILD_SPARSE_ATTN": "0"})
            _write("indextts2-deepspeed", phase="verifying")
            check = ("import torch, deepspeed; "
                     f"assert deepspeed.__version__ == '{DEEPSPEED_VERSION}'; "
                     f"assert (torch.__version__, torch.version.hip) == {tuple(baseline.split())!r}; "
                     "print('DeepSpeed', deepspeed.__version__, 'ROCm', torch.version.hip)")
            _command([str(python), "-c", check], log)
            _write("indextts2-deepspeed", phase="ready", error="", finished=time.time())
            return 0
        except Exception as exc:
            print(str(exc), file=log, flush=True)
            _write("indextts2-deepspeed", phase="failed", error=str(exc)[:300], finished=time.time())
            return 1


def _voice_busy() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(VOICE_STATE_URL, timeout=10) as response:
            jobs = json.loads(response.read(), strict=False).get("jobs", [])
    except (OSError, ValueError):
        return True  # can't tell: treat as busy, never restart over a job
    return any(job.get("state") in ("queued", "running") for job in jobs)


def install_flash() -> int:
    """Compile FlashAttention for the Voice image, then restart Voice once it is idle
    (transformers only looks for flash-attn when the service starts)."""
    STATE.mkdir(parents=True, exist_ok=True)
    log_path = STATE / "qwen3-flash.log"
    log_path.write_text("")
    _write("qwen3-flash", phase="compiling (30-90 min)", error="")
    try:
        result = subprocess.run(["bash", str(FLASH_SCRIPT), str(FLASH_DIR)],
                                env={**os.environ, "FLASH_ATTN_LOG": str(log_path),
                                     "GPU_ARCHS": os.getenv("GPU_ARCH", "gfx1100")})
        if result.returncode or not _flash_status()["installed"]:
            raise RuntimeError("FlashAttention build failed; see log")
        _write("qwen3-flash", phase="waiting for Voice to be idle")
        deadline = time.time() + 6 * 3600
        while _voice_busy():
            if time.time() > deadline:
                raise RuntimeError("Built, but Voice stayed busy; restart the Voice service to use it")
            time.sleep(30)
        with log_path.open("a") as log:
            _command(["docker", "restart", "bunny-voice"], log)
        _write("qwen3-flash", phase="ready", error="", finished=time.time())
        return 0
    except Exception as exc:
        with log_path.open("a") as log:
            print(str(exc), file=log, flush=True)
        _write("qwen3-flash", phase="failed", error=str(exc)[:300], finished=time.time())
        return 1


def start_flash() -> tuple[int, dict]:
    with LOCK:
        if any(_active(name) for name in (*SPECS, *ADDONS)):
            return 409, {"error": "Another voice engine install is running"}
        if _ready("qwen3") != (True, True):
            return 409, {"error": "Install Qwen3-TTS first"}
        _write("qwen3-flash", phase="queued", error="", started=time.time())
        result = subprocess.run(["systemd-run", "--user", "--collect", "--quiet",
                                 "--unit=core-voice-qwen3-flash", "-p", "Nice=19",
                                 "-p", "IOSchedulingClass=idle", sys.executable,
                                 str(Path(__file__).resolve()), "qwen3-flash"],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            error = (result.stderr or result.stdout).strip()[:300]
            _write("qwen3-flash", phase="failed", error=error)
            return 503, {"error": error}
    return 200, {"message": "Qwen3-TTS FlashAttention build started"}


def start_deepspeed() -> tuple[int, dict]:
    with LOCK:
        if any(_active(name) for name in (*SPECS, *ADDONS)):
            return 409, {"error": "Another voice engine install is running"}
        if _ready("indextts2") != (True, True):
            return 409, {"error": "Install the IndexTTS2 engine first"}
        if _deepspeed_status()["installed"]:
            return 409, {"error": "DeepSpeed is already installed for IndexTTS2"}
        _write("indextts2-deepspeed", phase="queued", error="", started=time.time())
        result = subprocess.run(["systemd-run", "--user", "--collect", "--quiet",
                                 "--unit=core-voice-indextts2-deepspeed", "-p", "Nice=19",
                                 "-p", "IOSchedulingClass=idle", sys.executable,
                                 str(Path(__file__).resolve()), "indextts2-deepspeed"],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            error = (result.stderr or result.stdout).strip()[:300]
            _write("indextts2-deepspeed", phase="failed", error=error)
            return 503, {"error": error}
    return 200, {"message": "IndexTTS2 DeepSpeed install started"}


def handle(method: str, path: str) -> tuple[int, dict]:
    if method == "GET" and path == "/core/voice":
        return 200, status()
    if method == "GET" and path == "/core/voice/indextts2/deepspeed/log":
        log = STATE / "indextts2-deepspeed.log"
        return 200, {"log": log.read_text(errors="replace")[-64000:] if log.is_file() else ""}
    if method == "POST" and path == "/core/voice/indextts2/deepspeed":
        return start_deepspeed()
    if method == "GET" and path == "/core/voice/qwen3/flash/log":
        log = STATE / "qwen3-flash.log"
        return 200, {"log": log.read_text(errors="replace")[-64000:] if log.is_file() else ""}
    if method == "POST" and path == "/core/voice/qwen3/flash":
        return start_flash()
    parts = path.split("/")
    if len(parts) != 5 or parts[:3] != ["", "core", "voice"] or parts[3] not in SPECS:
        return 404, {"error": "Unknown voice engine"}
    engine, action = parts[3:]
    if method == "GET" and action == "log":
        log = STATE / f"{engine}.log"
        return 200, {"log": log.read_text(errors="replace")[-64000:] if log.is_file() else ""}
    if method != "POST" or action != "install":
        return 404, {"error": "Unknown voice engine action"}
    with LOCK:
        if any(_active(name) for name in (*SPECS, *ADDONS)):
            return 409, {"error": "Another voice engine install is running"}
        if _ready(engine) == (True, True):
            return 409, {"error": "This voice engine is already installed"}
        _write(engine, phase="queued", error="", started=time.time())
        result = subprocess.run(["systemd-run", "--user", "--collect", "--quiet",
                                 f"--unit=core-voice-{engine}", "-p", "Nice=19",
                                 "-p", "IOSchedulingClass=idle", sys.executable,
                                 str(Path(__file__).resolve()), engine], capture_output=True, text=True, timeout=30)
        if result.returncode:
            error = (result.stderr or result.stdout).strip()[:300]
            _write(engine, phase="failed", error=error)
            return 503, {"error": error}
    return 200, {"message": f"{SPECS[engine]['name']} install started"}


if __name__ == "__main__":
    import dockeraccess  # the docker group in the session that installed Rocsy (see dockeraccess.py)
    dockeraccess.reexec_with_docker_group()
    if len(sys.argv) != 2:
        sys.exit("Unknown voice engine")
    if sys.argv[1] == "indextts2-deepspeed":
        sys.exit(install_deepspeed())
    if sys.argv[1] == "qwen3-flash":
        sys.exit(install_flash())
    if sys.argv[1] not in SPECS:
        sys.exit("Unknown voice engine")
    sys.exit(install(sys.argv[1]))
