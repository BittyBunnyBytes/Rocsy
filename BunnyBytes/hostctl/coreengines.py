"""Optional Strata and ROCmFPX engines managed alongside the core llama.cpp builds."""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import loadpriority
from paths import COMFY_ROOT, LLAMA_CONFIG, LLAMA_OVERRIDES, ROOT as ROCSY_ROOT

HOME = Path.home()
ROOT = Path(os.getenv("CORE_ENGINES_ROOT", ROCSY_ROOT / "core-engines"))
STRATA = ROOT / "Strata"
FPX = ROOT / "ROCmFPX"
STATE = ROOT / "state.json"
LOG = ROOT / "install.log"
STRATA_URL = "https://github.com/Niko1221/Strata.git"
FPX_URL = "https://github.com/charlie12345/ROCmFPX.git"
STRATA_PORT = 9101
INSTALL_UNIT = "core-strata-install"
SERVE_UNIT = "core-strata-serve"
FPX_UNIT = "core-rocmfpx-build"
TUNE_UNIT = "core-strata-hipblaslt-tune"
AGENT_STOP_URL = f"http://127.0.0.1:{os.getenv('AGENT_PORT', '8900')}/Agent/api/internal/stop-model"
ARCH_RE = re.compile(r"gfx[0-9a-f]+")
FPX_ID_RE = re.compile(r"rocmfpx-[0-9a-f]{8}-gfx[0-9a-f]+")
# VRAM the GPU image encoder keeps beside the engine: part of --vram-reserve-mib while vision is on.
VISION_VRAM_MIB = 1536


class Refused(Exception):
    pass


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(data: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, STATE)


def unit_active(unit: str) -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit + ".service"],
                          capture_output=True).returncode == 0


def health() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{STRATA_PORT}/health", timeout=1) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def arch() -> str:
    nodes = Path("/sys/class/kfd/kfd/topology/nodes")
    for props in sorted(nodes.glob("*/properties")):
        try:
            match = ARCH_RE.search(props.read_text())
            if match:
                return match.group()
        except OSError:
            pass
    for props in sorted(nodes.glob("*/properties")):
        try:
            match = re.search(r"^gfx_target_version\s+(\d+)", props.read_text(), re.M)
            if match and int(match.group(1)):
                return "gfx" + str(int(match.group(1)) // 100)
        except OSError:
            pass
    result = subprocess.run(["rocm-smi", "--showproductname"], capture_output=True, text=True, timeout=20)
    match = ARCH_RE.search(result.stdout)
    return match.group() if match else ""


def hipblas_version() -> str:
    roots = (Path("/opt/rocm/include/hipblaslt/hipblaslt-version.h"),
             Path("/opt/rocm/include/hipblaslt/hipblaslt-version.hpp"))
    for path in roots:
        try:
            content = path.read_text()
        except OSError:
            continue
        for name in ("HIPBLASLT_VERSION", "HIPBLASLT_VERSION_MAJOR"):
            match = re.search(r"^\s*#\s*define\s+" + name + r"\s+(\d+)", content, re.M)
            if match and name == "HIPBLASLT_VERSION":
                return match.group(1)
        major = re.search(r"HIPBLASLT_VERSION_MAJOR\s+(\d+)", content)
        minor = re.search(r"HIPBLASLT_VERSION_MINOR\s+(\d+)", content)
        patch = re.search(r"HIPBLASLT_VERSION_PATCH\s+(\d+)", content)
        if major and minor and patch:
            return str(int(major.group(1)) * 100000 + int(minor.group(1)) * 100 + int(patch.group(1)))
    return ""


def tuning() -> dict:
    gpu, version = arch(), hipblas_version()
    name = f"{gpu}-hipblaslt-{version}.txt" if gpu and version else ""
    path = STRATA / "tools/hip" / name if name else None
    return {"arch": gpu, "version": version, "file": str(path) if path else "",
            "available": bool(path and path.is_file()), "source": STRATA_URL,
            "running": unit_active(TUNE_UNIT), "job": read_state().get("tuning", {}),
            "note": "Strata accepts a hipBLASLt table only when GPU architecture and library version both match."}


def view() -> dict:
    state = read_state()
    scripts = sorted(STRATA.glob("run-*.sh")) if STRATA.is_dir() else []
    build = state.get("rocmfpx", {})
    if build.get("phase") not in ("ready", "failed") and not unit_active(FPX_UNIT) and build:
        build = {**build, "phase": "failed", "error": "Build stopped unexpectedly"}
    return {"strata": {"source": STRATA_URL, "path": str(STRATA), "downloaded": (STRATA / ".git").is_dir(),
                       "installed": bool(scripts), "running": health(), "installing": unit_active(INSTALL_UNIT),
                       "serving": unit_active(SERVE_UNIT), "models": [p.stem[4:] for p in scripts],
                       "port": STRATA_PORT, "job": state.get("strata", {})},
            "rocmfpx": {"source": FPX_URL, "path": str(FPX), "arch": arch(), "job": build,
                         "building": unit_active(FPX_UNIT)}, "hipblaslt": tuning()}


def wait_unit_gone(unit: str, timeout: float = 150) -> None:
    """Waits until a unit has finished stopping: is-active already says no while it is still "deactivating", and
    systemd-run refuses the same name until it is gone (a llama-swap swap starts the next size during the stop)."""
    end = time.time() + timeout
    while time.time() < end:
        state = subprocess.run(["systemctl", "--user", "show", "-p", "ActiveState", "--value", unit + ".service"],
                               capture_output=True, text=True).stdout.strip()
        if state in ("", "inactive"):
            return
        if state == "failed":
            subprocess.run(["systemctl", "--user", "reset-failed", unit + ".service"], capture_output=True)
            return
        time.sleep(0.5)


def launch(unit: str, *args: str, props: tuple[str, ...] = (),
           priority: tuple[str, ...] = ("Nice=19", "IOSchedulingClass=idle")) -> None:
    extra = [arg for prop in (*priority, *props) for arg in ("-p", prop)]
    result = subprocess.run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}",
                             *extra, sys.executable,
                             str(Path(__file__).resolve()), *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:300])


def install_strata(body: dict) -> dict:
    if unit_active(INSTALL_UNIT) or unit_active(SERVE_UNIT) or health():
        raise Refused("Stop Strata before installing or changing its model")
    model = str(body.get("model") or "IQ2_XS")
    if model not in {"Q2_0", "IQ2_XS", "IQ3_XXS", "IQ3_S"}:
        raise Refused("Choose a supported Strata model preset")
    family = str(body.get("family") or "qwen")
    if family not in {"qwen", "swift", "coder"}:
        raise Refused("Choose a supported Strata model family")
    context = int(body.get("context") or 32768)
    if context < 4096 or context > 262144:
        raise Refused("Context must be between 4096 and 262144")
    gguf_dir = str(body.get("gguf_dir") or "")
    if gguf_dir:
        folder = Path(gguf_dir).resolve()
        allowed = (ROCSY_ROOT / "LLM", COMFY_ROOT / "models/llm")
        if not folder.is_dir() or not any(folder == root or root in folder.parents for root in allowed):
            raise Refused("Custom GGUF directory must be inside a managed LLM folder")
    data = read_state()
    data["strata"] = {"phase": "queued", "model": model, "family": family, "context": context, "gguf_dir": gguf_dir,
                      "started": time.time()}
    save_state(data)
    launch(INSTALL_UNIT, "strata-install")
    return {"message": "Strata download and setup started"}


def download_strata_source() -> dict:
    if (STRATA / ".git").is_dir():
        return {"message": "Strata source and its tuning tables are already downloaded"}
    if unit_active(INSTALL_UNIT):
        raise Refused("Strata source is already downloading")
    launch(INSTALL_UNIT, "strata-source")
    return {"message": "Downloading Strata source and GPU tuning tables"}


def run_strata_source() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    try:
        with LOG.open("a", buffering=1) as log:
            subprocess.run(["git", "clone", "--depth=1", STRATA_URL, str(STRATA)],
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        return 0
    except Exception:
        return 1


def start_tuning() -> dict:
    info = tuning()
    if not info["arch"] or not info["version"]:
        raise Refused("GPU architecture and hipBLASLt version must be detected first")
    if not (STRATA / ".git").is_dir():
        raise Refused("Download Strata source first")
    if health():
        raise Refused("Stop Strata before calibrating hipBLASLt")
    if unit_active(TUNE_UNIT):
        raise Refused("A tuning job is already running")
    data = read_state()
    data["tuning"] = {"phase": "queued", "arch": info["arch"], "version": info["version"],
                      "started": time.time()}
    save_state(data)
    launch(TUNE_UNIT, "hipblaslt-tune")
    return {"message": f"Calibrating hipBLASLt for {info['arch']} / {info['version']}"}


def run_tuning() -> int:
    data = read_state()
    job = data.get("tuning", {})
    gpu, version = job.get("arch", ""), job.get("version", "")
    try:
        if gpu != arch() or version != hipblas_version():
            raise RuntimeError("GPU or hipBLASLt version changed; restart calibration")
        builds = sorted((p for p in STRATA.glob("build-hip*") if (p / "CMakeCache.txt").is_file()),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        if not builds:
            raise RuntimeError("Install a Strata model first so its HIP build exists")
        build = next((p for p in builds if gpu in (p / "CMakeCache.txt").read_text(errors="replace")), None)
        if build is None:
            raise RuntimeError("No Strata HIP build matches the detected GPU")
        reference = STRATA / "tools/hip/gfx1100-hipblaslt-100200.txt"
        if not reference.is_file():
            raise RuntimeError("Strata's reference GEMM shapes are missing")
        cases = []
        for line in reference.read_text().splitlines()[2:]:
            fields = line.split()
            if len(fields) >= 5 and not line.lstrip().startswith("#"):
                cases += ["--case", ",".join((fields[0], fields[4], fields[1], fields[2], fields[3]))]
        if not cases:
            raise RuntimeError("Strata's reference table has no cases")
        target = STRATA / "tools/hip" / f"{gpu}-hipblaslt-{version}.txt"
        temporary = target.with_suffix(".calibrating")
        with LOG.open("a", buffering=1) as log:
            job["phase"] = "building"
            save_state(data)
            subprocess.run(["cmake", "--build", str(build), "--target", "tune_hipblaslt"],
                           cwd=STRATA, stdout=log, stderr=subprocess.STDOUT, check=True)
            binary = build / "tune_hipblaslt"
            if not binary.is_file():
                raise RuntimeError("Strata's tune_hipblaslt binary is missing")
            job["phase"] = "calibrating"
            save_state(data)
            subprocess.run([str(binary), *cases, "--tuning-out", str(temporary)],
                           cwd=STRATA, stdout=log, stderr=subprocess.STDOUT, check=True)
        lines = temporary.read_text().splitlines()
        if len(lines) < 3 or f"STRATA_HIPBLASLT_TUNING_V1 {gpu} {version}" not in lines[1]:
            raise RuntimeError("Calibration produced a table for another GPU or hipBLASLt version")
        os.replace(temporary, target)
        job.update(phase="ready", file=str(target), finished=time.time())
        save_state(data)
        return 0
    except Exception as exc:
        job.update(phase="failed", error=str(exc)[:300], finished=time.time())
        save_state(data)
        return 1


def installed_strata_models() -> list[str]:
    """The installed sizes (run-iq3_xxs.sh -> IQ3_XXS), each its own llama-swap model."""
    return [s.stem.removeprefix("run-").upper() for s in sorted(STRATA.glob("run-*.sh"))]


def start_strata(body: dict | None = None) -> dict:
    """Start Strata with a size: body.model (llama-swap's entry for that size passes it), else the
    installed default. Only one size runs at a time (one port, one GPU): another one is stopped first."""
    if unit_active(INSTALL_UNIT):
        return {"message": "Strata is installing"}
    scripts = sorted(STRATA.glob("run-*.sh"))
    if not scripts:
        raise Refused("Install a Strata model first")
    data = read_state()
    wanted = str((body or {}).get("model") or "").upper()
    if wanted and not re.fullmatch(r"[A-Z0-9_]{2,20}", wanted):
        raise Refused("Unknown Strata model size")
    selected = (wanted or str(data.get("strata", {}).get("model") or "")).lower()
    script = next((item for item in scripts if selected and item.stem.lower() == f"run-{selected}"), None)
    if script is None and wanted:
        raise Refused(f"Strata {wanted} is not installed (installed: {', '.join(installed_strata_models())})")
    script = script or next((item for item in scripts if selected and selected in item.stem.lower()), scripts[0])
    serving = str(data.get("strata", {}).get("serving") or "")
    if health() or unit_active(SERVE_UNIT):
        if not serving or serving == script.name:
            return {"message": "Strata is already running"}
        stop_strata({"swap": True})                 # the other size: let it finish and QUIT, then start this one
    data = read_state()
    data.setdefault("strata", {})["serving"] = script.name
    save_state(data)
    wait_unit_gone(SERVE_UNIT)
    # KillMode=mixed: a stop sends SIGTERM to this runner only, which hands it to the server; the server then QUITs
    # the engine.  The default SIGTERMs every process at once, so the engine died mid-kernel and the XTX reset
    # (2026-10-17 17:30 and 17:45, Xorg went down both times).
    # Settings -> Services -> Model loading priority (Background by default, as before).
    launch(SERVE_UNIT, "strata-serve", script.name, props=("KillMode=mixed", "TimeoutStopSec=120"),
           priority=loadpriority.systemd_props("strata"))
    return {"message": "Strata is starting"}


def stop_agents_on_strata() -> int:
    """Stops the Agent sessions running on Strata; otherwise their next request makes llama-swap start it again."""
    request = urllib.request.Request(AGENT_STOP_URL, data=json.dumps({"engine": "strata"}).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-Hostctl-Secret": os.getenv("HOSTCTL_SECRET", "")})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return len(json.load(response).get("stopped") or [])
    except Exception:  # Agent service down: nothing of it is using Strata
        return 0


def wait_strata_idle(limit: float = 20) -> None:
    """Killing Strata mid-generation has reset the XTX and taken Xorg down (2026-10-17): give the cancelled
    requests a moment to finish first."""
    deadline = time.time() + limit
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{STRATA_PORT}/status", timeout=2) as response:
                status = json.load(response)
        except Exception:
            return                                # not serving: nothing to wait for
        if not status.get("busy") and not status.get("queued"):
            return
        time.sleep(0.5)


def stop_strata(body: dict | None = None) -> dict:
    # llama-swap's own unloads (swaps, TTL, the Agent freeing VRAM for renders) send swap=true: those keep the sessions.
    agents = 0
    if not (body or {}).get("swap"):
        agents = stop_agents_on_strata()
    wait_strata_idle()
    for unit in (SERVE_UNIT, INSTALL_UNIT):
        if unit_active(unit):
            subprocess.run(["systemctl", "--user", "stop", unit + ".service"], timeout=150, check=True)
    return {"message": "Strata stopped" + (f" (and {agents} Agent session{'s' * (agents != 1)} using it)" if agents else "")}


def build_fpx() -> dict:
    gpu = arch()
    if not gpu:
        raise Refused("No AMD GPU architecture was detected")
    if unit_active(FPX_UNIT):
        raise Refused("A ROCmFPX build is already running")
    data = read_state()
    data["rocmfpx"] = {"phase": "queued", "arch": gpu, "started": time.time()}
    save_state(data)
    launch(FPX_UNIT, "rocmfpx-build")
    return {"message": f"ROCmFPX download and build started for {gpu}"}


def run_strata_install() -> int:
    data = read_state()
    job = data.get("strata", {})
    ROOT.mkdir(parents=True, exist_ok=True)
    try:
        with LOG.open("a", buffering=1) as log:
            if not (STRATA / ".git").exists():
                job["phase"] = "downloading"
                save_state(data)
                subprocess.run(["git", "clone", "--depth=1", STRATA_URL, str(STRATA)],
                               stdout=log, stderr=subprocess.STDOUT, check=True)
            job["phase"] = "installing"
            save_state(data)
            cmd = ["./setup.sh", "--setup", "--family", job["family"], "--model", job["model"], "--context", str(job["context"]),
                   "--backend", "hip", "--port", str(STRATA_PORT), "--host", "127.0.0.1",
                   "--no-browser", "--yes"]
            if job.get("gguf_dir"):
                cmd += ["--gguf-dir", job["gguf_dir"]]
            log.write("Running Strata setup for " + job["model"] + "\n")
            process = subprocess.Popen(cmd, cwd=STRATA, stdout=log, stderr=subprocess.STDOUT)
            while process.poll() is None:
                if health():
                    job["phase"] = "ready"
                    job["finished"] = time.time()
                    save_state(data)
                    break
                time.sleep(3)
            code = process.wait()
            if job.get("phase") != "ready":
                raise RuntimeError(f"Strata setup exited {code}; see install log")
        return 0
    except Exception as exc:
        job.update(phase="failed", error=str(exc)[:300], finished=time.time())
        save_state(data)
        return 1


def strata_options() -> dict[str, str]:
    """The llama-swap Parameters tab's choices for the Strata model(s): <id>.args of every
    config.yaml model whose cmd is strata-swap-bridge (gateway/llama_params.py writes them)."""
    try:
        import yaml
        models = (yaml.safe_load(LLAMA_CONFIG.read_text()) or {}).get("models") or {}
    except Exception:
        return {}
    values: dict[str, str] = {}
    for model_id, entry in models.items():
        args = LLAMA_OVERRIDES / f"{model_id}.args"
        if isinstance(entry, dict) and "strata-swap-bridge" in str(entry.get("cmd", "")) and args.is_file():
            for line in args.read_text().splitlines():
                flag, _, value = line.strip().partition(" ")
                if flag == "--vision" and value.strip() in ("on", "off"):
                    values[flag] = value.strip()
    return values


def text_only_config(config: Path) -> Path:
    """A copy of a Strata run config with images off: no --vision, no "vision" section, and the GPU
    encoder's VRAM taken back out of --vram-reserve-mib. Rewritten on every start, so it follows the config."""
    cfg = json.loads(config.read_text())
    args = [arg for arg in cfg.get("args", []) if arg != "--vision"]
    vision = cfg.pop("vision", None)
    if isinstance(vision, dict) and vision.get("gpu") and "--vram-reserve-mib" in args:
        index = args.index("--vram-reserve-mib") + 1
        if index < len(args) and args[index].isdigit():
            args[index] = str(max(0, int(args[index]) - VISION_VRAM_MIB))
    cfg["args"] = args
    out = config.with_name(config.stem + ".text-only.json")
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, out)
    return out


SESSIONS = ROOT / "Strata-data" / "sessions"   # the Agent's saved conversations (POST /slots/0?action=save|restore)
SESSION_KEEP_S = 2 * 86400


def session_dir() -> Path:
    """The slot save folder, cleared of conversations nobody restored for two days."""
    SESSIONS.mkdir(parents=True, exist_ok=True)
    for old in SESSIONS.glob("*.bin"):
        try:
            if time.time() - old.stat().st_mtime > SESSION_KEEP_S:
                old.unlink()
        except OSError:
            pass
    return SESSIONS


def strata_command(script: Path) -> list[str]:
    """The run script's server command (with Vision off in Parameters on a text-only config), plus
    --slot-save-path so the Agent can park a conversation on disk before it unloads the model."""
    for line in script.read_text().splitlines():
        words = shlex.split(line)
        if words[:1] == ["exec"] and "--config" in words[:-1]:
            words = words[1:]
            if strata_options().get("--vision") == "off":
                index = words.index("--config") + 1
                words[index] = str(text_only_config(Path(words[index])))
            return words + ["--slot-save-path", str(session_dir())]
    return ["sh", str(script)]


def run_strata_serve(name: str) -> int:
    if not re.fullmatch(r"run-[A-Za-z0-9._-]+\.sh", name):
        return 2
    script = STRATA / name
    if not script.is_file():
        return 2
    env = os.environ.copy()
    info = tuning()
    if info["available"]:
        env["STRATA_HIPBLASLT_TUNING"] = info["file"]
    proc = subprocess.Popen(strata_command(script), cwd=STRATA, env=env)
    # the server's own SIGTERM path ends the engine with QUIT; dying here instead would leave it to systemd's SIGKILL
    signal.signal(signal.SIGTERM, lambda *_: proc.send_signal(signal.SIGTERM))
    return proc.wait()


def cancel_fpx() -> dict:
    if not unit_active(FPX_UNIT):
        raise Refused("No ROCmFPX build is running")
    subprocess.run(["systemctl", "--user", "stop", FPX_UNIT + ".service"], timeout=60, check=True)
    data = read_state()
    job = data.get("rocmfpx", {})
    job.update(phase="cancelled", finished=time.time())
    save_state(data)
    if job.get("id"):
        from corebackend import BUILDS, read_meta, save_json
        folder = BUILDS / job["id"]
        meta = read_meta(folder)
        meta.update(phase="cancelled", finished=time.time())
        save_json(folder / "meta.json", meta)
    return {"message": "ROCmFPX build cancelled"}


def run_fpx_build() -> int:
    data = read_state()
    job = data.get("rocmfpx", {})
    gpu = job.get("arch", "")
    try:
        if not ARCH_RE.fullmatch(gpu):
            raise RuntimeError("Invalid GPU architecture")
        from corebackend import BUILDS, save_json, write_backend_info
        ROOT.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", buffering=1) as log:
            if not (FPX / ".git").exists():
                job["phase"] = "downloading"
                save_state(data)
                subprocess.run(["git", "clone", "--depth=1", FPX_URL, str(FPX)], stdout=log,
                               stderr=subprocess.STDOUT, check=True)
            commit = subprocess.check_output(["git", "-C", str(FPX), "rev-parse", "HEAD"], text=True).strip()
            ident = f"rocmfpx-{commit[:8]}-{gpu}"
            folder = BUILDS / ident
            if (folder / "build/bin/llama-server").is_file():
                job.update(phase="ready", id=ident, finished=time.time())
                save_state(data)
                return 0
            folder.mkdir(parents=True, exist_ok=True)
            meta = {"id": ident, "kind": "rocmfpx", "release": commit[:8], "phase": "configure",
                    "path": str(folder / "build"), "gpu_targets": gpu, "created": time.time()}
            save_json(folder / "meta.json", meta)
            job.update(phase="configure", id=ident)
            save_state(data)
            cmd = ["cmake", "-S", str(FPX), "-B", str(folder / "build"), "-G", "Ninja",
                   "-DCMAKE_BUILD_TYPE=Release", "-DGGML_HIP=ON", f"-DAMDGPU_TARGETS={gpu}",
                   f"-DGPU_TARGETS={gpu}", "-DLLAMA_BUILD_SERVER=ON", "-DLLAMA_BUILD_TESTS=OFF"]
            subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=True)
            job["phase"] = "compile"
            meta["phase"] = "compile"
            save_state(data)
            save_json(folder / "meta.json", meta)
            subprocess.run(["cmake", "--build", str(folder / "build"), "--target", "llama-server",
                            "llama-bench", "llama-cli", "--parallel", str(min(os.cpu_count() or 8, 16))],
                           stdout=log, stderr=subprocess.STDOUT, check=True)
            server = folder / "build/bin/llama-server"
            if not server.is_file() or not (server.parent / "libggml-hip.so").is_file():
                raise RuntimeError("ROCmFPX GPU server was not built")
            meta.update(phase="ready", finished=time.time(), size=sum(p.stat().st_size for p in (folder / "build").rglob("*") if p.is_file()))
            save_json(folder / "meta.json", meta)
            job.update(phase="ready", finished=time.time())
            save_state(data)
            write_backend_info()
            return 0
    except Exception as exc:
        job.update(phase="failed", error=str(exc)[:300], finished=time.time())
        save_state(data)
        if job.get("id"):
            from corebackend import BUILDS, read_meta, save_json
            folder = BUILDS / job["id"]
            meta = read_meta(folder)
            meta.update(phase="failed", error=str(exc)[:300])
            save_json(folder / "meta.json", meta)
        return 1


def handle(method: str, path: str, body: dict) -> tuple[int, dict]:
    try:
        if method == "GET" and path == "/core/engines":
            return 200, view()
        if method == "GET" and path == "/core/engines/log":
            return 200, {"log": LOG.read_text()[-40000:] if LOG.exists() else ""}
        if method != "POST":
            return 404, {"error": "not found"}
        if path == "/core/engines/strata-source":
            return 200, download_strata_source()
        if path == "/core/engines/strata-install":
            return 200, install_strata(body)
        if path == "/core/engines/strata-start":
            return 200, start_strata(body)
        if path == "/core/engines/strata-stop":
            return 200, stop_strata(body)
        if path == "/core/engines/rocmfpx-build":
            return 200, build_fpx()
        if path == "/core/engines/hipblaslt-tune":
            return 200, start_tuning()
        if path == "/core/engines/rocmfpx-cancel":
            return 200, cancel_fpx()
        return 404, {"error": "not found"}
    except (Refused, ValueError) as exc:
        return 409, {"error": str(exc)}


if __name__ == "__main__":
    import dockeraccess  # the docker group in the session that installed Rocsy (see dockeraccess.py)
    dockeraccess.reexec_with_docker_group()
    if len(sys.argv) == 2 and sys.argv[1] == "hipblaslt-tune":
        sys.exit(run_tuning())
    if len(sys.argv) == 2 and sys.argv[1] == "strata-source":
        sys.exit(run_strata_source())
    if len(sys.argv) == 2 and sys.argv[1] == "strata-install":
        sys.exit(run_strata_install())
    if len(sys.argv) == 3 and sys.argv[1] == "strata-serve":
        sys.exit(run_strata_serve(sys.argv[2]))
    if len(sys.argv) == 2 and sys.argv[1] == "rocmfpx-build":
        sys.exit(run_fpx_build())
    sys.exit(2)
