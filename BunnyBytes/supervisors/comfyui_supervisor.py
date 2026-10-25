#!/usr/bin/env python3
"""Host-side lifecycle manager for the ComfyUI server, mirroring
../../qwen38_install/supervisor/qwen_supervisor.py for ComfyUI.

Why this exists: the studio containers have no way to launch a GPU process on
the host, so a render would fail whenever ComfyUI was not already running. This
supervisor lets them ask a host process (reachable via network_mode: host) to
start ComfyUI on demand.

Unlike qwen_supervisor.py, ComfyUI is NOT torn down between attempts or
cycles - it's shared, long-lived infrastructure used concurrently by
Video, Music and Krea (see BunnyBytes/docker-compose.yml).
So /start here is "ensure running, start if not" and is idempotent; there is
no per-attempt /stop dance, and this supervisor's own shutdown does NOT stop
ComfyUI (the child is started with start_new_session=True precisely so it
survives independently of this process). /stop still exists for manual/admin
use only - callers should not invoke it as part of a normal run.

This process is meant to run continuously (see comfyui-supervisor.service) -
it is cheap and idle when ComfyUI is already up or not yet needed.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The ComfyUI checkout: ROCSY_COMFY_ROOT (set by the unit from BunnyBytes/.env), else the
# "ComfyUI" folder next to this supervisor/ directory (a ComfyUI Desktop install keeps its
# standalone env there). .venv/bin/python is a symlink into standalone-env/bin/python3.
HERE = os.path.dirname(os.path.abspath(__file__))
COMFY_DIR = os.environ.get("ROCSY_COMFY_ROOT") or os.path.join(os.path.dirname(HERE), "ComfyUI")
COMFY_PYTHON = os.path.join(COMFY_DIR, ".venv", "bin", "python")

SUPERVISOR_HOST = os.environ.get("COMFYUI_SUPERVISOR_HOST", "127.0.0.1")
SUPERVISOR_PORT = int(os.environ.get("COMFYUI_SUPERVISOR_PORT", "8083"))

# Matches BunnyBytes/.env's COMFY_URL. Flags below match the user's own
# working ComfyUI Desktop launch config (confirmed 2026-09-22: an LTX 2.5
# render that reliably GPU-page-faulted through this supervisor's old
# --high-ram flag set completed cleanly in 1m27s under these args instead).
# --high-ram and --cache-ram are mutually exclusive (see `main.py --help`);
# --disable-smart-memory forces aggressive VRAM->RAM offload instead of
# ComfyUI's default opportunistic VRAM residency, which is likely what
# avoids the fault when combined with --enable-dynamic-vram/--async-offload.
COMFY_PORT = int(os.environ.get("COMFYUI_PORT", "8188"))
_BASE_ARGS = ["--listen", "127.0.0.1", "--port", str(COMFY_PORT), "--preview-method", "none", "--disable-metadata"]
COMFY_ARGS_LTX = [
    *_BASE_ARGS,
    "--enable-dynamic-vram", "--disable-smart-memory", "--async-offload", "2",
    "--cache-ram", "8", "16", "--use-flash-attention",
]
# Video's MiniMax H3 Reference mode loads a Qwen3-VL-32B text encoder
# (comfy_extras/nodes_minimax_h3.py) that the LTX profile's --enable-dynamic-vram
# staging tries to pre-allocate as one ~27GB buffer, which doesn't fit a 24GB
# card outright (confirmed 2026-09-22: "Model MiniMaxH3TEModel_ prepared for
# dynamic VRAM loading. 25882MB Staged" immediately preceded a CUDA OOM,
# with PyTorch's own allocator holding under 1GB at the time - not
# fragmentation, the staging buffer itself is just larger than the card).
# This profile instead matches Video's own bare-metal/local-mode launch
# command (AI/Video/.env's H3_COMFY_COMMAND), which uses ComfyUI's
# classic per-layer smart-memory offloading instead of a single staging
# buffer, and is what that command was apparently tuned to avoid this with.
COMFY_ARGS_H3 = [
    *_BASE_ARGS,
    "--disable-pinned-memory", "--cache-none", "--disable-async-offload", "--use-flash-attention",
]
COMFY_ARGS_H3_SPLIT = [*COMFY_ARGS_H3, "--disable-dynamic-vram"]
# Krea/Qwen share one ComfyUI process and switch between differently quantized
# Krea 2, Krea 2 finetune, and Qwen model families.  Do not retain model state
# across prompts: the default smart cache can reuse incompatible Krea 2 state
# after a checkpoint switch.  On ROCm this poisoned the FP8 Lustify finetune
# with NaNs and SaveImage emitted an all-black PNG.  The cache-free profile was
# verified with the same checkpoint immediately before the resident-cache
# optimization was introduced.  This costs a few seconds of reload time but
# keeps checkpoint switches deterministic for both app paths.
COMFY_ARGS_KREA = [*_BASE_ARGS, "--cache-none", "--disable-async-offload", "--use-flash-attention"]
COMFY_ARGS_QWEN = [*_BASE_ARGS, "--cache-none", "--disable-async-offload", "--use-flash-attention"]
# Also benchmarked for these two and rejected: --use-pytorch-cross-attention (slower,
# ~24 GB VRAM peaks), --async-offload 2 (~24 GB peaks), --fast (no speed gain), Comfy
# Kitchen attention (no gain). Same flags today, so switching krea <-> qwen never restarts.
COMFY_PROFILES = {"ltx": COMFY_ARGS_LTX, "h3": COMFY_ARGS_H3, "h3_multi": COMFY_ARGS_H3, "h3_split": COMFY_ARGS_H3_SPLIT,
                  "h3_parallel": COMFY_ARGS_H3, "krea": COMFY_ARGS_KREA, "qwen": COMFY_ARGS_QWEN}
DEFAULT_PROFILE = "ltx"
PROFILE_CONFIG = os.path.join(HERE, "profiles.json")
PROFILE_DEFAULT_EXTRAS = {name: list(args[len(_BASE_ARGS):]) for name, args in COMFY_PROFILES.items()}


def _load_profile_extras() -> dict[str, list[str]]:
    values = {name: list(args) for name, args in PROFILE_DEFAULT_EXTRAS.items()}
    try:
        with open(PROFILE_CONFIG, encoding="utf-8") as handle:
            saved = json.load(handle)
        for name, args in saved.items():
            if name in values and isinstance(args, list) and all(isinstance(arg, str) for arg in args):
                values[name] = args
    except (OSError, ValueError, TypeError):
        pass
    return values


PROFILE_EXTRAS = _load_profile_extras()
COMFY_PROFILES = {name: [*_BASE_ARGS, *args] for name, args in PROFILE_EXTRAS.items()}


def profile_settings() -> dict:
    return {"default_profile": DEFAULT_PROFILE, "gemm_tuning": gemm_settings(), "profiles": [
        {"name": name, "extra_args": shlex.join(PROFILE_EXTRAS[name]),
         "default_extra_args": shlex.join(PROFILE_DEFAULT_EXTRAS[name]),
         "gpus": _profile_gpus(name)}
        for name in COMFY_PROFILES
    ]}


def save_profile_settings(body: dict) -> dict:
    if isinstance(body, dict) and "gemm_tuning" in body:
        save_gemm_mode(body["gemm_tuning"])
        if "profiles" not in body:
            return profile_settings()
    supplied = body.get("profiles") if isinstance(body, dict) else None
    if not isinstance(supplied, dict) or set(supplied) - set(COMFY_PROFILES):
        raise ValueError("profiles must contain only known profile names")
    updated = {name: list(args) for name, args in PROFILE_EXTRAS.items()}
    for name, command in supplied.items():
        if not isinstance(command, str):
            raise ValueError(f"{name} arguments must be text")
        args = shlex.split(command)
        if len(args) > 64 or any(len(arg) > 512 or "\x00" in arg for arg in args):
            raise ValueError(f"{name} has too many or invalid arguments")
        updated[name] = args
    temp = PROFILE_CONFIG + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(updated, handle, indent=2)
        handle.write("\n")
    os.replace(temp, PROFILE_CONFIG)
    PROFILE_EXTRAS.clear()
    PROFILE_EXTRAS.update(updated)
    COMFY_PROFILES.clear()
    COMFY_PROFILES.update({name: [*_BASE_ARGS, *args] for name, args in updated.items()})
    return profile_settings()

# Model loading priority (Rocsy Settings -> Services, through bunny-hostctl): the CPU/disk priority the
# ComfyUI started here runs at, from its next start. Only normal or lower: a user service may not raise
# its own priority. nice/ionice exec into python, so adoption still finds "main.py" in the cmdline.
PRIORITY_CONFIG = os.path.join(HERE, "priority.json")
PRIORITY_PREFIXES = {"background": ["nice", "-n", "19", "ionice", "-c", "3"],
                     "low": ["nice", "-n", "10", "ionice", "-c", "2", "-n", "7"], "normal": []}


def _load_priority() -> str:
    try:
        with open(PRIORITY_CONFIG, encoding="utf-8") as handle:
            level = json.load(handle).get("level")
    except (OSError, ValueError, AttributeError):
        level = None
    return level if level in PRIORITY_PREFIXES else "normal"


PRIORITY = _load_priority()


def priority_settings() -> dict:
    return {"level": PRIORITY, "levels": list(PRIORITY_PREFIXES)}


def save_priority(body: dict) -> dict:
    global PRIORITY
    level = body.get("level") if isinstance(body, dict) else None
    if level not in PRIORITY_PREFIXES:
        raise ValueError(f"level must be one of {', '.join(PRIORITY_PREFIXES)}")
    temp = PRIORITY_CONFIG + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump({"level": level}, handle)
        handle.write("\n")
    os.replace(temp, PRIORITY_CONFIG)
    PRIORITY = level
    return priority_settings()


def _priority_prefix() -> list[str]:
    prefix = PRIORITY_PREFIXES[PRIORITY]
    if prefix and not (shutil.which("nice") and shutil.which("ionice")):
        log.warning("nice/ionice not found - starting ComfyUI at normal priority instead of %s", PRIORITY)
        return []
    return prefix


# GEMM tuning (Rocsy Settings -> Services -> ComfyUI profiles): PyTorch TunableOp benchmarks the
# hipBLASLt/rocBLAS kernels for every new matmul shape and keeps the fastest in a CSV next to this
# supervisor. "tune" learns new shapes (the first render of a new size is slower), "use" only reads
# what was learned, "auto" renders with what was learned, records new shapes and tunes them while
# ComfyUI is idle (renders never wait), "off" is stock. Applies from ComfyUI's next start. ComfyUI is
# stopped by signal, so custom_nodes/bunny_tunableop_flush saves the results while it runs (TunableOp
# only writes at exit) and does auto's idle tuning.
GEMM_CONFIG = os.path.join(HERE, "gemm.json")
GEMM_DIR = os.path.join(HERE, "tunableop")
GEMM_MODES = ("off", "auto", "tune", "use")


def _load_gemm_mode() -> str:
    try:
        with open(GEMM_CONFIG, encoding="utf-8") as handle:
            mode = json.load(handle).get("mode")
    except (OSError, ValueError, AttributeError):
        mode = None
    return mode if mode in GEMM_MODES else "off"


GEMM_MODE = _load_gemm_mode()


def _gemm_keys(kind: str) -> set[str]:
    """op,params of every shape in the results (kind "results") or recorded-untuned ("untuned") files."""
    keys: set[str] = set()
    files = sorted(name for name in os.listdir(GEMM_DIR) if name.startswith(f"tunableop_{kind}") and name.endswith(".csv")) \
        if os.path.isdir(GEMM_DIR) else []
    for name in files:
        try:
            with open(os.path.join(GEMM_DIR, name), encoding="utf-8") as handle:
                keys.update(",".join(line.strip().split(",", 2)[:2]) for line in handle if line.startswith(("Gemm", "ScaledGemm")))
        except OSError:
            pass
    return keys


def _gemm_skipped() -> set[str]:
    """Shapes the ComfyUI side gave up on (no result possible); written by bunny_tunableop_flush."""
    keys: set[str] = set()
    for name in (os.listdir(GEMM_DIR) if os.path.isdir(GEMM_DIR) else []):
        if name.startswith("tunableop_skipped") and name.endswith(".csv"):
            try:
                with open(os.path.join(GEMM_DIR, name), encoding="utf-8") as handle:
                    keys.update(line.strip() for line in handle if line.strip())
            except OSError:
                pass
    return keys


def gemm_settings() -> dict:
    tuned = _gemm_keys("results")
    return {"mode": GEMM_MODE, "modes": list(GEMM_MODES), "tuned_shapes": len(tuned),
            "pending_shapes": len(_gemm_keys("untuned") - tuned - _gemm_skipped())}


def save_gemm_mode(mode: object) -> None:
    global GEMM_MODE
    if mode not in GEMM_MODES:
        raise ValueError(f"gemm_tuning must be one of {', '.join(GEMM_MODES)}")
    temp = GEMM_CONFIG + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump({"mode": mode}, handle)
        handle.write("\n")
    os.replace(temp, GEMM_CONFIG)
    GEMM_MODE = mode


def _gemm_env() -> dict[str, str]:
    if GEMM_MODE == "off":
        return {}
    os.makedirs(GEMM_DIR, exist_ok=True)
    env = {"PYTORCH_TUNABLEOP_ENABLED": "1", "PYTORCH_TUNABLEOP_FILENAME": os.path.join(GEMM_DIR, "tunableop_results%d.csv"),
           "BUNNY_TUNABLEOP_MODE": GEMM_MODE}
    if GEMM_MODE == "auto":
        # No TUNING/RECORD_UNTUNED env here: env vars override torch's runtime switches, and the flush
        # node turns live tuning off and only switches it on while it tunes the recorded shapes.
        env["PYTORCH_TUNABLEOP_UNTUNED_FILENAME"] = os.path.join(GEMM_DIR, "tunableop_untuned.csv")  # torch inserts the GPU ordinal itself
        env["BUNNY_LOCK_URL"] = os.environ.get("BUNNY_LOCK_URL", "http://127.0.0.1:8600")
    else:
        env["PYTORCH_TUNABLEOP_TUNING"] = "1" if GEMM_MODE == "tune" else "0"
    return env


# Which GPUs each profile's ComfyUI can see (ROCR_VISIBLE_DEVICES, so the
# other card doesn't exist for that process at all). ROCm enumerates by PCI
# bus: 0 = RX 7900 XTX (0a:00.0, main card, drives the desktop), 1 = RX 7800 XT
# (0d:00.0). Only Video's multi-GPU settings (Conditioning / Conditioning
# and VAEs on GPU 1 -> h3_multi, Split weights -> h3_split, Parallel split ->
# h3_parallel) get both cards; everything else runs on the XTX alone.
PRIMARY_GPUS = os.environ.get("COMFYUI_PRIMARY_GPUS", "0")
MULTI_GPUS = os.environ.get("COMFYUI_MULTI_GPUS", "0,1")
MULTI_GPU_PROFILES = {"h3_multi", "h3_split", "h3_parallel"}
# Inherited GPU selectors would re-filter/re-number what ROCR_VISIBLE_DEVICES exposes.
_GPU_SELECTOR_VARS = ("ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES", "GPU_DEVICE_ORDINAL")


def _profile_gpus(profile: str) -> str:
    return MULTI_GPUS if profile in MULTI_GPU_PROFILES else PRIMARY_GPUS


_FLASH: dict = {}


def _launch_args(args: list[str]) -> list[str]:
    """ComfyUI exits at start-up on --use-flash-attention without the flash-attn package, which a
    fresh install only gets from Models -> Core Backend's build: drop the flag until it's there."""
    if "--use-flash-attention" not in args:
        return args
    # Only "found" is remembered: a missing one is looked for again at every launch (a second),
    # so a finished build is used on ComfyUI's next start without restarting this supervisor.
    if not _FLASH.get("found"):
        try:
            found = subprocess.run([COMFY_PYTHON, "-c", "import importlib.util as u; raise SystemExit(not u.find_spec('flash_attn'))"],
                                   capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            found = True  # can't tell: keep the profile as it is
        _FLASH["found"] = found
        if not found:
            log.warning("flash-attn isn't installed in %s; starting ComfyUI without --use-flash-attention "
                        "(build it in Models -> Manager -> Core Backend)", COMFY_PYTHON)
    return args if _FLASH["found"] else [a for a in args if a != "--use-flash-attention"]


def _launch_key(profile: str | None) -> tuple[list[str], str] | None:
    """What actually distinguishes two profiles' processes: flags + visible GPUs."""
    return (COMFY_PROFILES[profile], _profile_gpus(profile)) if profile in COMFY_PROFILES else None

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s comfyui-supervisor: %(message)s"
)
log = logging.getLogger("comfyui-supervisor")


# A ComfyUI that isn't answering HTTP but has been running longer than this is
# treated as busy (a long model load or render can block its event loop past
# the 2s probe), not as still booting - so it is never killed for a profile
# switch or duplicated by a second spawn.
BOOT_GRACE_SECONDS = 120


def _read_proc(pid: int, name: str) -> bytes | None:
    try:
        with open(f"/proc/{pid}/{name}", "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    """Running and not a zombie."""
    stat = _read_proc(pid, "stat")
    if not stat:
        return False
    return stat.rsplit(b")", 1)[-1].split()[0] != b"Z"


def _pid_age_seconds(pid: int) -> float | None:
    stat = _read_proc(pid, "stat")
    try:
        with open("/proc/uptime") as handle:
            uptime_seconds = float(handle.read().split()[0])
        start_ticks = int(stat.rsplit(b")", 1)[-1].split()[19])  # field 22 (starttime)
        return uptime_seconds - start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _comfy_argv(pid: int) -> list[str] | None:
    """The ComfyUI main.py arguments of `pid` if it is this checkout's ComfyUI
    on COMFY_PORT, else None."""
    raw = _read_proc(pid, "cmdline")
    if not raw:
        return None
    argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    if "main.py" not in argv:
        return None
    args = argv[argv.index("main.py") + 1:]
    port = args[args.index("--port") + 1] if "--port" in args and args.index("--port") + 1 < len(args) else "8188"
    try:
        cwd = os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None
    if cwd != os.path.realpath(COMFY_DIR) or port != str(COMFY_PORT):
        return None
    return args


def _comfy_gpus(pid: int) -> str:
    """ROCR_VISIBLE_DEVICES `pid` was started with; unset = every card = MULTI_GPUS."""
    raw = _read_proc(pid, "environ") or b""
    for entry in raw.split(b"\0"):
        if entry.startswith(b"ROCR_VISIBLE_DEVICES="):
            return entry.split(b"=", 1)[1].decode("utf-8", "replace")
    return MULTI_GPUS


def _profile_for_args(args: list[str], gpus: str) -> str | None:
    # Several profiles can share flags + GPUs (h3 / krea-like twins); prefer the
    # non-parallel one, since h3_parallel always gets its own fresh process.
    matches = [name for name in COMFY_PROFILES if _launch_key(name) == (args, gpus)]
    return next((name for name in matches if name != "h3_parallel"), matches[0] if matches else None)


def _find_running_comfy() -> tuple[int, list[str]] | None:
    """A live ComfyUI for this checkout/port that this supervisor didn't
    spawn itself (e.g. spawned by a previous supervisor process - ComfyUI
    deliberately outlives supervisor restarts)."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        args = _comfy_argv(pid)
        if args is not None and _pid_alive(pid):
            return pid, args
    return None


def _queue_busy() -> bool | None:
    """Whether ComfyUI has anything running or queued (None = unreachable)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{COMFY_PORT}/queue", timeout=3) as resp:
            queue = json.loads(resp.read().decode("utf-8"))
        return bool(queue.get("queue_running") or queue.get("queue_pending"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


class ComfyProcess:
    """Tracks at most one ComfyUI process at a time, and which memory profile
    (see COMFY_PROFILES) it was launched with. Different callers (H3
    Machine's MiniMax H3 vs. LTX 2.5) need different memory
    strategies from the SAME shared port, so /start can request a profile by
    name; a mismatch triggers a restart onto the right one - but only while
    ComfyUI is idle, since it is shared and a switch kills whatever it runs.

    The process is either one this supervisor spawned (a Popen) or one it
    adopted: ComfyUI outlives supervisor restarts on purpose, so on the next
    request a restarted supervisor finds it in /proc and takes it back over
    (profile read from its arguments) instead of losing track of it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._pid: int | None = None
        self._profile: str | None = None  # None = unknown (launched some other way, with other flags)

    def _reachable(self, timeout: float = 2.0) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{COMFY_PORT}/system_stats", timeout=timeout) as resp:
                return resp.status < 500
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _alive_locked(self) -> bool:
        """Caller must hold self._lock. Refreshes tracking: forgets a dead
        process, adopts a running ComfyUI this supervisor doesn't know about."""
        if self._process is not None and self._process.poll() is not None:
            self._process = None
            self._pid = None
            self._profile = None
        if self._process is None and self._pid is not None and not _pid_alive(self._pid):
            self._pid = None
            self._profile = None
        if self._pid is None:
            found = _find_running_comfy()
            if found:
                self._pid, args = found
                gpus = _comfy_gpus(self._pid)
                self._profile = _profile_for_args(args, gpus)
                log.info("adopted running ComfyUI pid %s (profile=%s, gpus=%s)", self._pid, self._profile or "unknown", gpus)
        return self._pid is not None

    def status(self) -> dict:
        with self._lock:
            alive = self._alive_locked()
            pid, profile, spawned = self._pid, self._profile, self._process is not None
        return {
            "spawned_by_supervisor": alive and spawned,
            "adopted": alive and not spawned,
            "pid": pid if alive else None,
            "profile": profile if alive else None,
            "gpus": _profile_gpus(profile) if alive and profile else None,
            "reachable": self._reachable(),
            "port": COMFY_PORT,
        }

    def _stop_locked(self) -> None:
        """Caller must hold self._lock. Stops the tracked ComfyUI (spawned or
        adopted - never some other process) and its process group."""
        pid = self._pid
        if pid is None or not _pid_alive(pid):
            self._process = self._pid = self._profile = None
            return
        log.info("stopping pid %s", pid)
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            group = None
        signal_target = (lambda sig: os.killpg(pid, sig)) if group == pid else (lambda sig: os.kill(pid, sig))
        for sig, wait_seconds in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
            try:
                signal_target(sig)
            except ProcessLookupError:
                break
            if self._wait_exit(pid, wait_seconds):
                break
            log.warning("pid %s did not exit within %ss after %s", pid, wait_seconds, sig.name)
        if _pid_alive(pid):
            # Keep tracking it: forgetting a live ComfyUI would let the next
            # /start spawn a second one next to it.
            log.error("pid %s is still alive after SIGKILL", pid)
            return
        self._process = self._pid = self._profile = None
        log.info("stopped pid %s", pid)

    def _wait_exit(self, pid: int, seconds: float) -> bool:
        if self._process is not None:
            try:
                self._process.wait(timeout=seconds)
                return True
            except subprocess.TimeoutExpired:
                return False
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return True
            time.sleep(0.2)
        return not _pid_alive(pid)

    def start(self, profile: str | None = None) -> dict:
        # Other apps sharing this supervisor (Music, Krea)
        # predate the profile concept and call /start with no body at all -
        # their own "wake ComfyUI up if it's offline" watchdogs fire on
        # whatever cadence they use, completely independent of any profile
        # switch in progress here. Confirmed 2026-09-22: an explicit H3
        # profile switch got silently reverted 107ms later by exactly such a
        # bare call landing mid-restart. So a request that doesn't name a
        # *recognized* profile is a "just make sure something is up" wake-up,
        # never a switch request - only an explicit, recognized profile
        # differing from what's active (or starting) may stop and replace it.
        explicit = profile in COMFY_PROFILES
        resolved = profile if explicit else DEFAULT_PROFILE
        args = COMFY_PROFILES[resolved]
        gpus = _profile_gpus(resolved)
        with self._lock:
            if self._alive_locked():
                # Profiles with identical flags and GPUs (e.g. krea and qwen) are
                # the same process: never restart just to change the name.
                wants_switch = (explicit and self._profile is not None and self._profile != resolved
                                and (_launch_key(self._profile) != _launch_key(resolved)
                                     or "h3_parallel" in (self._profile, resolved)))
                reachable = self._reachable()
                if not wants_switch:
                    if explicit and self._profile is not None and _launch_key(self._profile) == _launch_key(resolved):
                        self._profile = resolved  # same flags under another name: report the one asked for
                    status = "already_running" if reachable else "starting"
                    # debug: callers' watchdogs send these wake-ups every few seconds
                    log.debug("start requested (profile=%s, explicit=%s); ComfyUI pid %s is %s (profile=%s) - not spawning another",
                              profile, explicit, self._pid, status, self._profile or "unknown")
                    return {"status": status, "pid": self._pid, "profile": self._profile}
                # A profile switch kills everything ComfyUI is doing - and it
                # is shared, so only while nothing is running or queued. An
                # unreachable ComfyUI is only safe to restart while it is
                # still booting; after that it is probably just busy.
                age = _pid_age_seconds(self._pid)
                busy = _queue_busy() if reachable else (age is None or age > BOOT_GRACE_SECONDS)
                if busy:
                    log.info("switch to profile=%s refused: ComfyUI pid %s (profile=%s) is busy", resolved, self._pid, self._profile)
                    return {"status": "busy", "pid": self._pid, "profile": self._profile,
                            "message": f"ComfyUI is busy on the {self._profile} profile; switch to {resolved} once it is idle."}
                log.info("start requested with profile=%s but profile=%s is running (pid %s); restarting onto %s",
                         resolved, self._profile, self._pid, resolved)
                self._stop_locked()
                if self._pid is not None:
                    return {"status": "error", "message": f"Could not stop ComfyUI pid {self._pid} to switch profiles."}
            elif self._reachable():
                # Something answers on the port that isn't this checkout's ComfyUI.
                log.info("start requested; port %s answers but no ComfyUI process was found - not spawning", COMFY_PORT)
                return {"status": "already_running", "pid": None, "profile": None}

            if not os.path.isfile(COMFY_PYTHON):
                log.error("ComfyUI python not found: %s", COMFY_PYTHON)
                return {"status": "error", "message": f"ComfyUI python not found: {COMFY_PYTHON}"}

            log.info("starting ComfyUI (profile=%s, gpus=%s, priority=%s, gemm=%s): %s main.py %s (cwd=%s)",
                     resolved, gpus, PRIORITY, GEMM_MODE, COMFY_PYTHON, " ".join(args), COMFY_DIR)
            env = {k: v for k, v in os.environ.items()
                   if k not in _GPU_SELECTOR_VARS and not k.startswith(("PYTORCH_TUNABLEOP_", "BUNNY_TUNABLEOP_"))}
            env["ROCR_VISIBLE_DEVICES"] = gpus
            env.update(_gemm_env())
            process = subprocess.Popen(
                [*_priority_prefix(), COMFY_PYTHON, "main.py", *_launch_args(args)],
                cwd=COMFY_DIR,
                env=env,
                start_new_session=True,  # detached - survives this supervisor being stopped/restarted
                stdout=None,
                stderr=None,
            )
            self._process = process
            self._pid = process.pid
            self._profile = resolved
            return {"status": "starting", "pid": process.pid, "profile": resolved}

    def stop(self) -> dict:
        """Manual/admin use (and Video's idle-only NaN-recovery restart) -
        see module docstring. Stops only this checkout's ComfyUI on
        COMFY_PORT, whether this supervisor spawned it or adopted it."""
        with self._lock:
            if not self._alive_locked():
                return {"status": "already_stopped"}
            self._stop_locked()
            if self._pid is not None:
                return {"status": "error", "message": f"ComfyUI pid {self._pid} did not exit."}
            return {"status": "stopped"}


comfy = ComfyProcess()
restart_lock = threading.Lock()


def _restart_supervisor_service() -> None:
    # KillMode=process lets the detached ComfyUI process survive this unit's
    # restart; the new supervisor adopts it from /proc on its next request.
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "comfyui-supervisor.service"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode:
            log.error("supervisor systemd restart failed: %s", result.stderr.strip())
    except (OSError, subprocess.TimeoutExpired):
        log.exception("supervisor systemd restart failed")
    finally:
        restart_lock.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "ComfyUISupervisor/1.0"

    def log_message(self, fmt: str, *args) -> None:  # access log at debug: start()/stop() log what matters
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guarded(self, handler) -> None:
        try:
            handler()
        except Exception as exc:  # noqa: BLE001 - answer the caller instead of dropping the connection
            log.exception("%s %s failed", self.command, self.path)
            self._send_json(500, {"status": "error", "message": str(exc)})

    def do_GET(self) -> None:
        self._guarded(self._get)

    def do_POST(self) -> None:
        self._guarded(self._post)

    def do_PUT(self) -> None:
        self._guarded(self._put)

    def _get(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/status":
            self._send_json(200, comfy.status())
        elif self.path == "/profiles":
            self._send_json(200, profile_settings())
        elif self.path == "/priority":
            self._send_json(200, priority_settings())
        else:
            self._send_json(404, {"error": "not_found"})

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _post(self) -> None:
        if self.path == "/start":
            body = self._read_json_body()
            profile = body.get("profile") if isinstance(body, dict) else None
            self._send_json(200, comfy.start(profile))
        elif self.path == "/stop":
            self._send_json(200, comfy.stop())
        elif self.path == "/restart":
            if not restart_lock.acquire(blocking=False):
                self._send_json(409, {"status": "busy", "message": "A restart is already in progress."})
                return
            try:
                profile = comfy.status()["profile"]
                stopped = comfy.stop()
                if stopped["status"] == "error":
                    self._send_json(500, stopped)
                    restart_lock.release()
                    return
                started = comfy.start(profile)
                if started["status"] == "error":
                    self._send_json(500, started)
                    restart_lock.release()
                    return
                self._send_json(200, {"status": "restarting", "pid": started["pid"], "profile": started["profile"]})
                threading.Timer(0.5, _restart_supervisor_service).start()
            except Exception:
                restart_lock.release()
                raise
        else:
            self._send_json(404, {"error": "not_found"})

    def _put(self) -> None:
        if self.path == "/priority":
            try:
                self._send_json(200, save_priority(self._read_json_body()))
            except ValueError as exc:
                self._send_json(422, {"status": "error", "message": str(exc)})
            return
        if self.path != "/profiles":
            self._send_json(404, {"error": "not_found"})
            return
        try:
            self._send_json(200, save_profile_settings(self._read_json_body()))
        except ValueError as exc:
            self._send_json(422, {"status": "error", "message": str(exc)})


def main() -> None:
    if not os.path.isdir(COMFY_DIR):
        log.error("ComfyUI checkout not found: %s", COMFY_DIR)
        sys.exit(1)
    server = ThreadingHTTPServer((SUPERVISOR_HOST, SUPERVISOR_PORT), Handler)
    log.info("listening on http://%s:%s (manages ComfyUI at 127.0.0.1:%s)", SUPERVISOR_HOST, SUPERVISOR_PORT, COMFY_PORT)
    # No SIGTERM handler that stops ComfyUI on supervisor shutdown, unlike
    # qwen_supervisor.py - ComfyUI is shared across multiple apps and must
    # keep running independently of this supervisor's own lifecycle.
    server.serve_forever()


if __name__ == "__main__":
    main()
