"""Guarded per-model llama.cpp tuning for Rocsy (hostctl side).

The model's overrides are never edited during benchmarks. A raw baseline is
persisted before any GPU work; an exception restores it. A stale in-progress
record is restored at hostctl startup. Only flags measured by llama-bench are
eligible for an automatic change.
"""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

from paths import ROOT as ROCSY_ROOT
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml

HOME = Path.home()
CONFIG = Path(os.getenv("ROC_LLAMACONFIG", ROCSY_ROOT / "Llama/config.yaml"))
OVERRIDES = Path(os.getenv("ROC_LLAMAOVERRIDES", ROCSY_ROOT / "Llama/overrides"))
RUNS = Path(os.getenv("ROC_OPT_RUNS", ROCSY_ROOT / "Llama/optimization-runs"))
BUILDS = Path(os.getenv("ROC_LLAMA_BUILDS", ROCSY_ROOT / "llama.cpp-builds"))
BUILTIN = ROCSY_ROOT / "llama.cpp-rdna-boost/build-rocm-gfx1100-gfx1101-rdna-boost"
ID = re.compile(r"^[A-Za-z0-9._-]+$")
FLAGS = ("--batch-size", "--ubatch-size", "--threads", "--flash-attn")
_lock = threading.Lock()
_thread: threading.Thread | None = None


def atomic(path: Path, data: bytes | None) -> None:
    if data is None:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_name("." + path.name + ".optimizer-tmp")
    with tmp.open("wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def record(path: Path, value: dict) -> None:
    atomic(path, (json.dumps(value, indent=2) + "\n").encode())


def config_model(model_id: str) -> tuple[dict, list[str], dict[str, str]]:
    if not ID.fullmatch(model_id):
        raise ValueError("Invalid model id")
    model = (yaml.safe_load(CONFIG.read_text()) or {}).get("models", {}).get(model_id)
    if not isinstance(model, dict):
        raise ValueError("Unknown model id")
    words = shlex.split(str(model.get("cmd") or ""))
    if len(words) < 5 or words[0] != "${llama_launch}":
        raise ValueError("This model does not use llama-launch")
    defaults = {words[i]: words[i+1] for i in range(3, len(words)-1) if words[i].startswith("--")}
    defaults["--gpu-layers"] = words[1]
    if words[2] != "none":
        defaults["--fit-target"] = words[2]
    return model, words, defaults


def raw_paths(model_id: str) -> tuple[Path, Path]:
    return OVERRIDES / (model_id + ".args"), OVERRIDES / (model_id + ".ctx")


def read_settings(model_id: str, defaults: dict[str, str]) -> dict[str, str]:
    args, _ = raw_paths(model_id)
    values = defaults.copy()
    if args.exists():
        for line in args.read_text().splitlines():
            key, _, val = line.partition(" ")
            if key.startswith("--") and val.strip():
                values[key] = val.strip()
    return values


def build_dir(settings: dict[str, str]) -> Path:
    choice = settings.get("--build", "active")
    if choice == "builtin":
        return BUILTIN
    if choice == "active":
        return BUILDS / "active" if (BUILDS / "active").exists() else BUILTIN
    if not re.fullmatch(r"(?:b[0-9]+-(?:boost-[0-9a-f]+|stock)(?:-[a-z0-9-]+)?|rocmfpx-[0-9a-f]{8}-gfx[0-9a-f]+)", choice):
        raise ValueError("Invalid llama.cpp build")
    return BUILDS / "builds" / choice / "build"


def benchmark_binary(settings: dict[str, str], *, install: bool = False) -> Path:
    build = build_dir(settings)
    binary = build / "bin/llama-bench"
    if binary.is_file():
        return binary
    if not install:
        raise RuntimeError("llama-bench is not installed for this build. Install it in llama-swap → Benchmarking or Core Backend → Inference Engines.")
    source = build.resolve().parent / "src"
    if not source.is_dir() and build.resolve() != BUILTIN:
        metadata = json.loads((build.resolve().parent / "meta.json").read_text())
        commit = str(metadata.get("commit") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise RuntimeError("Build metadata has no source commit for llama-bench")
        restored = subprocess.run(["git", "-C", str(BUILDS / "repo.git"), "worktree", "add",
                                   "--detach", str(source), commit], capture_output=True, text=True, timeout=120)
        if restored.returncode:
            raise RuntimeError("Could not restore matching llama.cpp source: " + restored.stderr[-400:])
    # Builds made for serving commonly omit the bench executable. Build the
    # matching target; never substitute a different revision's executable.
    result = subprocess.run(["cmake", "--build", str(build), "--target", "llama-bench", "-j", "4"],
                            capture_output=True, text=True, timeout=1800)
    if result.returncode or not binary.is_file():
        raise RuntimeError("Matching llama-bench is unavailable: " + (result.stderr or result.stdout)[-400:])
    return binary


def hardware() -> dict:
    info = {"cpu_count": os.cpu_count(), "mem_kib": None, "gpus": [], "rocm": ""}
    try:
        info["cpu"] = next(x.split(":", 1)[1].strip() for x in Path("/proc/cpuinfo").read_text().splitlines()
                           if x.startswith("model name"))
        info["mem_kib"] = int(next(x.split()[1] for x in Path("/proc/meminfo").read_text().splitlines()
                                   if x.startswith("MemTotal:")))
    except (OSError, StopIteration, ValueError):
        pass
    try:
        result = subprocess.run(["lspci", "-nn"], capture_output=True, text=True, timeout=10)
        info["gpus"] = [x.strip() for x in result.stdout.splitlines() if "VGA" in x or "Display" in x or "3D controller" in x]
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        result = subprocess.run(["rocm-smi", "--showmeminfo", "vram"], capture_output=True, text=True, timeout=15)
        info["rocm"] = result.stdout[-3000:] if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        pass
    return info


def ensure_idle() -> None:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        result = subprocess.run(["docker", "top", "llama", "-eo", "pid,comm"], capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and "llama-server" not in result.stdout:
            return
        time.sleep(5)
    raise RuntimeError("The llama server stayed loaded; benchmark did not start")


def ensure_agent_alive(run: dict) -> None:
    session = run.get("session")
    if not session:
        return
    try:
        with urlopen(f"http://127.0.0.1:8900/Agent/api/sessions/{session}", timeout=5) as response:
            state = json.load(response).get("state")
    except Exception as exc:
        raise RuntimeError("Rocsy Agent became unreachable during optimization") from exc
    if state in ("failed", "interrupted", "stopped"):
        raise RuntimeError(f"Rocsy Agent session {state} during optimization")


def bench(binary: Path, model_file: str, settings: dict[str, str],
          env_overrides: dict[str, str | None] | None = None) -> dict[str, float]:
    def val(flag: str, fallback: str) -> str:
        return settings.get(flag, fallback)
    backend = "Vulkan" if val("--backend", "rocm") == "vulkan" else "ROCm"
    devices = backend + "0/" + backend + "1" if val("--multi-gpu", "off") == "on" else backend + "0"
    args = [str(binary), "-m", model_file, "-p", "512", "-n", "128", "-r", "2", "-o", "json",
            "-b", val("--batch-size", "2048"), "-ub", val("--ubatch-size", "512"),
            "-t", val("--threads", str(min(os.cpu_count() or 8, 8))),
            "-ngl", "-1" if val("--gpu-layers", "all") in ("all", "auto") else val("--gpu-layers", "all"),
            "-fa", val("--flash-attn", "on"),
            "-ctk", val("--cache-type-k", "f16"), "-ctv", val("--cache-type-v", "f16"),
            "-ncmoe", val("--n-cpu-moe", "0"),
            "-dev", devices]
    if settings.get("--fit-target"):
        args += ["-fitt", settings["--fit-target"]]
    env = os.environ.copy()
    runtime = binary.resolve().parent.parent / "rocm-path"
    if runtime.is_file():
        root = runtime.read_text().strip()
        if not root.startswith(str(ROCSY_ROOT / "rocm-versions/") ) or ".." in root:
            raise RuntimeError("Invalid ROCm runtime path for selected llama.cpp build")
        env.update(ROCM_PATH=root, HIP_PATH=root, LD_LIBRARY_PATH=root + "/lib:" + root + "/lib/llvm/lib")
    if env_overrides:
        for key, value in env_overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    result = subprocess.run(args, capture_output=True, text=True, timeout=900, env=env)
    if result.returncode:
        raise RuntimeError("llama-bench failed: " + result.stderr[-600:])
    try:
        rows = json.loads(result.stdout)
        scores = {"pp": 0.0, "tg": 0.0}
        for row in rows:
            key = "pp" if int(row.get("n_prompt") or 0) else "tg"
            scores[key] = float(row["avg_ts"])
        if min(scores.values()) <= 0 or any(not math.isfinite(x) for x in scores.values()):
            raise ValueError("Missing benchmark results")
        return scores
    except (ValueError, TypeError, KeyError) as error:
        raise RuntimeError("Could not read llama-bench JSON") from error


def candidates(base: dict[str, str], count: int) -> list[dict[str, str]]:
    # Small, controlled search. Any user-provided research can narrow the
    # sequence; only benchmarkable load flags are touched.
    choices = []
    batch = int(base.get("--batch-size", "2048"))
    ubatch = int(base.get("--ubatch-size", "512"))
    for b, ub in ((1024, 512), (2048, 512), (4096, 1024), (4096, 2048)):
        if b != batch or ub != ubatch:
            choices.append({"--batch-size": str(b), "--ubatch-size": str(ub)})
    threads = int(base.get("--threads", str(min(count, 8))))
    for t in sorted({max(1, count // 2), min(count, 8)}):
        if t != threads and t <= 64:
            choices.append({"--threads": str(t)})
    if base.get("--flash-attn", "on") != "auto":
        choices.append({"--flash-attn": "auto"})
    return choices[:7]


def validate_trials(raw) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 5:
        raise ValueError("Research trials must be a list of at most five settings")
    limits = {"--batch-size": (32, 8192), "--ubatch-size": (32, 4096), "--threads": (1, 64)}
    out = []
    for item in raw:
        if not isinstance(item, dict) or not item or set(item) - set(FLAGS):
            raise ValueError("Research trials may change batch, micro-batch, threads or flash attention only")
        cleaned = {}
        for flag, value in item.items():
            if flag == "--flash-attn":
                if value not in ("on", "off", "auto"):
                    raise ValueError("Invalid flash attention choice")
                cleaned[flag] = value
            else:
                number = int(value)
                low, high = limits[flag]
                if not low <= number <= high:
                    raise ValueError(f"{flag} must be between {low} and {high}")
                cleaned[flag] = str(number)
        out.append(cleaned)
    return out


def restore(run: dict) -> None:
    for suffix in ("args", "ctx"):
        path = OVERRIDES / f"{run['model']}.{suffix}"
        snapshot = RUNS / run["id"] / (suffix + ".baseline")
        atomic(path, snapshot.read_bytes() if snapshot.exists() else None)


def update(run: dict) -> None:
    record(RUNS / run["id"] / "status.json", run)


def progress(run: dict, message: str) -> None:
    """Show benchmark progress without holding up the GPU if chat is unavailable."""
    session = run.get("session")
    if not session:
        return
    try:
        request = Request(f"http://127.0.0.1:8900/Agent/api/tool/{session}/benchmark",
                          data=json.dumps({"text": message, "run": run["id"]}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=3):
            pass
    except Exception:
        pass


def score_line(scores: dict[str, float]) -> str:
    return f"prompt {scores['pp']:.1f} tok/s · generation {scores['tg']:.1f} tok/s"


def settings_line(settings: dict[str, str]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(settings.items())) or "saved settings"


def inform(run: dict) -> None:
    session = run.get("session")
    if not session:
        return
    if run["state"] == "failed":
        message = (f"Llama optimization failed for {run['model']}. "
                   f"Baseline settings {'were restored' if run.get('restored') else 'could not be restored'}: "
                   f"{run.get('error', '')}")
    else:
        baseline = run.get("baseline")
        baseline_text = f" Baseline: {score_line(baseline)}." if baseline else ""
        winner = next((x for x in run.get("results", []) if x.get("settings") == run.get("applied")
                       and x.get("scores") and x.get("settings")), None)
        message = (f"Llama optimization finished for {run['model']}: "
                   + (f"applied {settings_line(run['applied'])} ({run['gain']}% combined gain)."
                      if run.get("applied") else "baseline retained; no clear benchmark improvement.")
                   + baseline_text + (f" Selected: {score_line(winner['scores'])}." if winner else ""))
    for _ in range(60):
        try:
            request = Request(f"http://127.0.0.1:8900/Agent/api/tool/{session}/benchmark",
                              data=json.dumps({"text": message, "run": run["id"]}).encode(),
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=5):
                return
        except Exception:
            time.sleep(10)


def execute(run: dict) -> None:
    global _thread
    try:
        model, words, defaults = config_model(run["model"])
        base = read_settings(run["model"], defaults)
        model_file = base.get("--model")
        if not model_file or not Path(model_file).is_file():
            raise RuntimeError("Configured GGUF file is missing")
        run["hardware"] = hardware()
        run["baseline_settings"] = base
        run["state"] = "preparing"
        update(run)
        binary = benchmark_binary(base)
        ensure_idle()
        ensure_agent_alive(run)
        run["state"] = "benchmarking"
        update(run)
        progress(run, f"Llama benchmark started for {run['model']}. Measuring the saved settings first: "
                 f"{settings_line({flag: base[flag] for flag in FLAGS if flag in base})}.")
        baseline = bench(binary, model_file, base)
        run["baseline"] = baseline
        run["results"] = [{"settings": {}, "scores": baseline}]
        update(run)
        progress(run, f"Baseline benchmark: {score_line(baseline)}.")
        trials = run.get("trials") or []
        seen = set()
        planned = []
        for change in (trials + candidates(base, os.cpu_count() or 8))[:10]:
            signature = tuple(sorted(change.items()))
            if signature not in seen:
                seen.add(signature)
                planned.append(change)
        for index, change in enumerate(planned, 1):
            ensure_agent_alive(run)
            settings = {**base, **change}
            label = f"Trial {index}/{len(planned)} ({settings_line(change)})"
            if int(settings.get("--ubatch-size", "512")) > int(settings.get("--batch-size", "2048")):
                run["results"].append({"settings": change, "error": "micro-batch exceeds batch"})
                update(run)
                progress(run, f"{label}: skipped; micro-batch exceeds batch.")
                continue
            progress(run, f"{label}: benchmarking now.")
            try:
                score = bench(binary, model_file, settings)
                run["results"].append({"settings": change, "scores": score})
                outcome = f"{score_line(score)} (prompt {(score['pp'] / baseline['pp'] - 1):+.1%}, generation {(score['tg'] / baseline['tg'] - 1):+.1%} vs baseline)."
            except Exception as exc:
                run["results"].append({"settings": change, "error": str(exc)[:500]})
                outcome = f"failed: {str(exc)[:300]}"
            update(run)
            progress(run, f"{label}: {outcome}")
        # Both prompt and generation must improve enough to exceed noise;
        # retain the user's baseline if the tradeoff is unclear.
        eligible = [x for x in run["results"][1:] if "scores" in x and
                    x["scores"]["pp"] >= baseline["pp"] * 0.98 and
                    x["scores"]["tg"] >= baseline["tg"] * 0.98]
        eligible.sort(key=lambda x: math.sqrt(x["scores"]["pp"] * x["scores"]["tg"]), reverse=True)
        winner = eligible[0] if eligible else None
        gain = (math.sqrt(winner["scores"]["pp"] * winner["scores"]["tg"] /
                          (baseline["pp"] * baseline["tg"])) - 1) if winner else 0
        if winner and gain >= 0.03:
            ensure_agent_alive(run)
            args_file, _ = raw_paths(run["model"])
            lines = args_file.read_text().splitlines() if args_file.exists() else []
            for flag, value in winner["settings"].items():
                lines = [line for line in lines if not line.startswith(flag + " ")]
                lines.append(flag + " " + value)
            # Recheck external edits; never overwrite changes made while testing.
            snapshot = RUNS / run["id"] / "args.baseline"
            original = snapshot.read_bytes() if snapshot.exists() else None
            if (args_file.read_bytes() if args_file.exists() else None) != original:
                raise RuntimeError("Settings changed during benchmark; no optimizer settings applied")
            run["mutation_pending"] = True
            update(run)
            atomic(args_file, ("\n".join(lines) + "\n").encode())
            run["applied"] = winner["settings"]
        else:
            run["applied"] = {}
        run["gain"] = round(gain * 100, 2)
        run["state"] = "complete"
        run["mutation_pending"] = False
        update(run)
    except BaseException as exc:
        if run.get("mutation_pending"):
            try:
                restore(run)
                run["restored"] = True
            except Exception as restore_error:
                run["restore_error"] = str(restore_error)[:500]
        else:
            run["restored"] = True  # overrides were never changed
        run["state"] = "failed"
        run["error"] = str(exc)[:600]
        update(run)
    finally:
        _thread = None
        inform(run)


def recover() -> None:
    if not RUNS.exists():
        return
    for status in RUNS.glob("*/status.json"):
        try:
            run = json.loads(status.read_text())
            if run.get("state") in ("preparing", "benchmarking", "running"):
                if run.get("mutation_pending"):
                    restore(run)
                run.update(state="failed", restored=True, error="Optimizer stopped before finishing; baseline restored")
                update(run)
                threading.Thread(target=inform, args=(run,), daemon=True).start()
        except (OSError, ValueError, KeyError):
            pass


def handle(method: str, path: str, body: dict) -> tuple[int, dict]:
    global _thread
    if path == "/llama-optimize/models" and method == "GET":
        config = yaml.safe_load(CONFIG.read_text()) or {}
        return 200, {"models": [{"id": key, "name": value.get("name", key)} for key, value in
                                config.get("models", {}).items() if isinstance(value, dict)]}
    if path == "/llama-optimize/status" and method == "GET":
        runs = sorted(RUNS.glob("*/status.json"), key=lambda p: p.stat().st_mtime, reverse=True) if RUNS.exists() else []
        return 200, json.loads(runs[0].read_text()) if runs else {"state": "idle"}
    if path.startswith("/llama-optimize/status/") and method == "GET":
        run_id = path.removeprefix("/llama-optimize/status/")
        if not ID.fullmatch(run_id):
            return 422, {"error": "Invalid optimization run id"}
        status = RUNS / run_id / "status.json"
        return (200, json.loads(status.read_text())) if status.is_file() else (404, {"error": "Unknown optimization run"})
    if path == "/llama-optimize/start" and method == "POST":
        with _lock:
            if _thread and _thread.is_alive():
                return 409, {"error": "A model optimization is already running"}
            model_id = str(body.get("model") or "")
            session = str(body.get("session") or "")
            if not re.fullmatch(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", session):
                return 422, {"error": "A Rocsy session id is required"}
            sources = body.get("sources") or []
            if not isinstance(sources, list) or not sources or len(sources) > 5 or not all(
                    isinstance(x, str) and urlparse(x).scheme == "https" and urlparse(x).netloc for x in sources):
                return 422, {"error": "Provide 1-5 HTTPS research sources before benchmarking"}
            trials = validate_trials(body.get("trials"))
            config_model(model_id)
            run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + model_id
            folder = RUNS / run_id
            folder.mkdir(parents=True, exist_ok=False)
            for suffix, file in zip(("args", "ctx"), raw_paths(model_id)):
                if file.exists():
                    atomic(folder / (suffix + ".baseline"), file.read_bytes())
            run = {"id": run_id, "model": model_id, "session": session, "sources": sources, "trials": trials,
                   "state": "running", "started": time.time()}
            update(run)
            _thread = threading.Thread(target=execute, args=(run,), daemon=True)
            _thread.start()
            return 202, run
    return 404, {"error": "Unknown optimizer endpoint"}
