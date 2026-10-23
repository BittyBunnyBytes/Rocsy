"""Per-model rocBLAS backend calibration for llama.cpp.

This measures rocBLAS automatic selection against Tensile and hipBLASLt preference.
It does not create a hipBLASLt per-GEMM solution table. Quantized llama.cpp
kernels that bypass rocBLAS are unaffected. A selected override is applied by
llama-launch only while the GGUF, binary, and hipBLASLt library match.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path

import llama_optimize as opt

ROOT = opt.OVERRIDES.parent / "hipblaslt-tuning"
_lock = threading.Lock()
_thread: threading.Thread | None = None
MIN_GAIN = 0.03
MAX_REGRESSION = 0.02
MAX_BASELINE_DRIFT = 0.05


def _paths(model: str) -> tuple[Path, Path]:
    if not opt.ID.fullmatch(model):
        raise ValueError("Invalid model id")
    return ROOT / f"{model}.json", opt.OVERRIDES / f"{model}.hipblaslt"


def _signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{int(stat.st_mtime)}"


def _runtime_library(binary: Path) -> Path:
    rocm_file = binary.resolve().parent.parent / "rocm-path"
    root = Path(rocm_file.read_text().strip()) if rocm_file.is_file() else Path("/opt/rocm")
    lib = (root / "lib/libhipblaslt.so").resolve()
    if not lib.is_file():
        raise RuntimeError("hipBLASLt library is unavailable for this llama.cpp build")
    return lib


def _context(model: str) -> tuple[dict, Path, Path, Path]:
    _entry, _words, defaults = opt.config_model(model)
    settings = opt.read_settings(model, defaults)
    if settings.get("--backend", "rocm") != "rocm":
        raise ValueError("hipBLASLt tuning requires this model's ROCm backend")
    gguf = Path(settings.get("--model") or "").resolve()
    if not gguf.is_file():
        raise ValueError("The configured GGUF is missing")
    binary = (opt.build_dir(settings) / "bin/llama-server").resolve()
    if not binary.is_file():
        raise ValueError("The selected llama.cpp build is missing")
    return settings, gguf, binary, _runtime_library(binary)


def _fingerprint(gguf: Path, binary: Path, library: Path) -> dict:
    return {"gguf": str(gguf), "gguf_stat": _signature(gguf),
            "binary": str(binary), "binary_stat": _signature(binary),
            "library": str(library), "library_stat": _signature(library)}


def _write_record(model: str, data: dict) -> None:
    record, _ = _paths(model)
    ROOT.mkdir(parents=True, exist_ok=True)
    opt.record(record, data)


def _write_override(model: str, data: dict) -> None:
    _, override = _paths(model)
    mode = data.get("winner")
    if mode not in ("0", "1"):
        override.unlink(missing_ok=True)
        return
    fp = data["fingerprint"]
    # Exact lines are checked by llama-launch before exporting the setting.
    value = "\n".join([mode, fp["gguf"], fp["gguf_stat"], fp["binary"],
                       fp["binary_stat"], fp["library"], fp["library_stat"]]) + "\n"
    opt.OVERRIDES.mkdir(parents=True, exist_ok=True)
    opt.atomic(override, value.encode())


def _idle() -> bool:
    import llama_benchmark
    if ((llama_benchmark._thread and llama_benchmark._thread.is_alive()) or
            (opt._thread and opt._thread.is_alive())):
        return False
    top = subprocess.run(["docker", "top", "llama", "-eo", "pid,comm"],
                         capture_output=True, text=True, timeout=15)
    return top.returncode == 0 and "llama-server" not in top.stdout and "strata" not in top.stdout


def status(model: str) -> dict:
    record, override = _paths(model)
    try:
        data = json.loads(record.read_text())
    except (OSError, ValueError):
        return {"state": "idle", "model": model}
    if data.get("state") == "running" and (_thread is None or not _thread.is_alive()):
        data = {**data, "state": "failed", "error": "Tuning stopped unexpectedly"}
    if data.get("state") == "complete":
        try:
            _settings, gguf, binary, lib = _context(model)
            data["current"] = data.get("fingerprint") == _fingerprint(gguf, binary, lib)
        except (OSError, ValueError, RuntimeError):
            data["current"] = False
        data["applied"] = bool(data["current"] and data.get("winner") in ("0", "1") and override.is_file())
    return data


def _choose(scores: dict[str, dict[str, float]]) -> str:
    first, last = scores["auto_first"], scores["auto_last"]
    if any(abs(first[k] / last[k] - 1) > MAX_BASELINE_DRIFT for k in ("pp", "tg")):
        return "auto"
    baseline = {k: math.sqrt(first[k] * last[k]) for k in ("pp", "tg")}
    candidates = []
    for mode in ("0", "1"):
        ratio = {k: scores[mode][k] / baseline[k] for k in ("pp", "tg")}
        combined = math.sqrt(ratio["pp"] * ratio["tg"])
        if min(ratio.values()) >= 1 - MAX_REGRESSION and combined >= 1 + MIN_GAIN:
            candidates.append((combined, mode))
    return max(candidates)[1] if candidates else "auto"


def _execute(model: str, data: dict) -> None:
    global _thread
    try:
        settings, gguf, binary, library = _context(model)
        if not _idle():
            raise RuntimeError("Unload the running model before tuning")
        bench = opt.benchmark_binary(settings)
        fp = _fingerprint(gguf, binary, library)
        data.update(fingerprint=fp, scores={}, build=str(bench))
        _write_record(model, data)
        for label, mode in (("auto_first", None), ("1", "1"), ("0", "0"), ("auto_last", None)):
            if not _idle():
                raise RuntimeError("A model loaded during tuning; no setting was saved")
            # Remove an inherited override for the automatic control run.
            env = {"ROCBLAS_USE_HIPBLASLT": mode}
            data["phase"] = label
            _write_record(model, data)
            data["scores"][label] = opt.bench(bench, str(gguf), settings, env)
            _write_record(model, data)
        if fp != _fingerprint(gguf, binary, library):
            raise RuntimeError("Model, build, or hipBLASLt library changed during tuning")
        data["winner"] = _choose(data["scores"])
        _write_override(model, data)
        data.update(state="complete", phase="done", finished=time.time())
    except Exception as exc:
        data.update(state="failed", error=str(exc)[:500], finished=time.time())
    finally:
        _write_record(model, data)
        with _lock:
            _thread = None


def delete(model: str) -> dict:
    global _thread
    record, override = _paths(model)
    with _lock:
        if _thread and _thread.is_alive() and status(model).get("state") == "running":
            raise RuntimeError("Wait for this tuning run to finish before deleting it")
        override.unlink(missing_ok=True)
        record.unlink(missing_ok=True)
    return {"state": "idle", "model": model, "message": "hipBLASLt tuning data deleted"}


def handle(method: str, path: str, body: dict) -> tuple[int, dict]:
    global _thread
    if method == "GET" and path.startswith("/llama-hipblaslt/status/"):
        try:
            return 200, status(path.rsplit("/", 1)[-1])
        except ValueError as exc:
            return 422, {"error": str(exc)}
    if method != "POST" or path not in ("/llama-hipblaslt/start", "/llama-hipblaslt/delete"):
        return 404, {"error": "not found"}
    model = str(body.get("model") or "")
    try:
        if path.endswith("/delete"):
            return 200, delete(model)
        settings, _gguf, _binary, _lib = _context(model)
        opt.benchmark_binary(settings)  # fail before queueing if llama-bench is absent
        with _lock:
            if _thread and _thread.is_alive():
                return 409, {"error": "A hipBLASLt tuning run is already active"}
            if not _idle():
                return 409, {"error": "Unload the running model before tuning"}
            data = {"model": model, "state": "running", "phase": "queued", "started": time.time()}
            _write_record(model, data)
            _thread = threading.Thread(target=_execute, args=(model, data), daemon=True)
            _thread.start()
        return 202, data
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return 422, {"error": str(exc)[:500]}
