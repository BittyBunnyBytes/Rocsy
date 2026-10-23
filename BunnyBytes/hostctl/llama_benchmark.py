"""Read-only llama-bench runs for the llama-swap Benchmarking tab."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path

import llama_optimize as opt

RUNS = opt.RUNS.parent / "benchmarks"
_lock = threading.Lock()
_thread: threading.Thread | None = None
_install_thread: threading.Thread | None = None
_install_state: dict = {"state": "idle"}


def status(model: str) -> dict:
    if not opt.ID.fullmatch(model):
        return {"state": "idle"}
    path = RUNS / (model + ".json")
    try:
        data = json.loads(path.read_text())
        if data.get("state") == "running" and (_thread is None or not _thread.is_alive()):
            data.update(state="failed", error="Benchmark stopped unexpectedly")
        return data
    except (OSError, ValueError):
        return {"state": "idle", "model": model}


def save(data: dict) -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    opt.record(RUNS / (data["model"] + ".json"), data)


def execute(data: dict) -> None:
    global _thread
    try:
        _model, _words, defaults = opt.config_model(data["model"])
        settings = opt.read_settings(data["model"], defaults)
        path = Path(settings.get("--model") or "")
        if not path.is_file():
            raise RuntimeError("The configured GGUF file is missing")
        top = subprocess.run(["docker", "top", "llama", "-eo", "pid,comm"],
                             capture_output=True, text=True, timeout=15)
        if top.returncode or "llama-server" in top.stdout or "strata" in top.stdout:
            raise RuntimeError("Unload the running model before benchmarking")
        binary = opt.benchmark_binary(settings)
        data["settings"] = settings
        data["build"] = str(binary)
        save(data)
        data["scores"] = opt.bench(binary, str(path), settings)
        data.update(state="complete", finished=time.time())
    except Exception as exc:
        data.update(state="failed", error=str(exc)[:500], finished=time.time())
    finally:
        save(data)
        _thread = None


def install_status(model: str = "") -> dict:
    try:
        if model:
            _model, _words, defaults = opt.config_model(model)
            settings = opt.read_settings(model, defaults)
        else:
            settings = {"--build": "active"}
        build = opt.build_dir(settings).resolve()
        binary = build / "bin/llama-bench"
        return {"model": model, "build": str(build), "installed": binary.is_file(),
                "state": _install_state["state"] if _install_state.get("build") == str(build) else "idle",
                "error": _install_state.get("error", "") if _install_state.get("build") == str(build) else "",
                "agent_connected": binary.is_file()}
    except (ValueError, OSError, KeyError) as exc:
        return {"model": model, "installed": False, "state": "failed", "error": str(exc)}


def install_binary(settings: dict) -> None:
    global _install_thread
    try:
        binary = opt.benchmark_binary(settings, install=True)
        with _lock:
            _install_state.update(state="complete", binary=str(binary), error="")
    except Exception as exc:
        with _lock:
            _install_state.update(state="failed", error=str(exc)[:500])
    finally:
        with _lock:
            _install_thread = None


def manage_install(method: str, path: str, body: dict) -> tuple[int, dict]:
    global _install_thread
    model = str(body.get("model") or "") if method == "POST" else ""
    if method == "GET":
        match = re.fullmatch(r"/llama-benchmark/install/status(?:/([A-Za-z0-9._-]+))?", path)
        if match:
            return 200, install_status(match.group(1) or "")
    if method != "POST" or path not in ("/llama-benchmark/install", "/llama-benchmark/uninstall"):
        return 404, {"error": "not found"}
    try:
        if model:
            _model, _words, defaults = opt.config_model(model)
            settings = opt.read_settings(model, defaults)
        else:
            settings = {"--build": "active"}
        build = opt.build_dir(settings).resolve()
        binary = build / "bin/llama-bench"
        if not build.is_dir():
            return 422, {"error": "The selected llama.cpp build is not installed"}
        with _lock:
            if _install_thread and _install_thread.is_alive():
                return 409, {"error": "A llama-bench installation is already running"}
            if path.endswith("/uninstall"):
                if (_thread and _thread.is_alive()) or (opt._thread and opt._thread.is_alive()):
                    return 409, {"error": "Wait for the active benchmark or Agent optimization to finish"}
                binary.unlink(missing_ok=True)
                _install_state.update(state="idle", build=str(build), error="")
                return 200, {"message": "llama-bench uninstalled from this build", **install_status(model)}
            if binary.is_file():
                return 200, {"message": "llama-bench is already installed", **install_status(model)}
            if (_thread and _thread.is_alive()) or (opt._thread and opt._thread.is_alive()):
                return 409, {"error": "Wait for the active benchmark or Agent optimization to finish"}
            _install_state.update(state="installing", build=str(build), error="")
            _install_thread = threading.Thread(target=install_binary, args=(settings,), daemon=True)
            _install_thread.start()
        return 202, {"message": "Building matching llama-bench", **install_status(model)}
    except (ValueError, OSError, KeyError) as exc:
        return 422, {"error": str(exc)[:500]}


def handle(method: str, path: str, body: dict) -> tuple[int, dict]:
    global _thread
    if path.startswith("/llama-benchmark/install") or path == "/llama-benchmark/uninstall":
        return manage_install(method, path, body)
    match = re.fullmatch(r"/llama-benchmark/status/([A-Za-z0-9._-]+)", path)
    if method == "GET" and match:
        return 200, status(match.group(1))
    if method == "POST" and path == "/llama-benchmark/start":
        model = str(body.get("model") or "")
        try:
            opt.config_model(model)
        except (ValueError, OSError) as exc:
            return 422, {"error": str(exc)}
        with _lock:
            if _thread and _thread.is_alive():
                return 409, {"error": "A benchmark is already running"}
            top = subprocess.run(["docker", "top", "llama", "-eo", "pid,comm"],
                                 capture_output=True, text=True, timeout=15)
            if top.returncode or "llama-server" in top.stdout or "strata" in top.stdout:
                return 409, {"error": "Unload the running model before benchmarking"}
            data = {"state": "running", "model": model, "started": time.time()}
            save(data)
            _thread = threading.Thread(target=execute, args=(data,), daemon=True)
            _thread.start()
        return 200, data
    return 404, {"error": "not found"}
