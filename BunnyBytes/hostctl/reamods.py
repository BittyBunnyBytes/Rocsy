"""Install / uninstall REA mods into a game folder on the host.

A mod project (built on the REA page) has an install/ tree in the agent's workspace that mirrors
the game folder. The REA container only sees games read-only, so the page and authorized agents
use this host helper to install, play-test, and uninstall mods. Every file is tracked per game:
which mods own it, whether it existed
before (left alone on uninstall) and the backup of anything it overwrote (restored on uninstall).
Files shared by several mods (the BepInEx loader) are removed only with the last mod."""
from __future__ import annotations

import contextlib
import filecmp
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from paths import HERMES_HOME, ROOT as ROCSY_ROOT

import reafolders

HOME = Path.home()
WORKSPACE = Path(os.getenv("REA_MODS_WORKSPACE", HERMES_HOME / "rocsy-workspace/rea-mods"))
STATE_DIR = Path(os.getenv("CORE_EXTRAS_STATE", ROCSY_ROOT / "core-backend-extras")) / "rea-mod-installs"
LOCK = threading.Lock()
MAX_FILES = 5000


class Refused(Exception):
    pass


def project(mod_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", mod_id or ""):
        raise Refused("Unknown mod")
    try:
        return json.loads((WORKSPACE / mod_id / "project.json").read_text())
    except (OSError, ValueError) as exc:
        raise Refused("Unknown mod") from exc


def host_game_path(proj: dict) -> Path:
    """The container path /folders/<slug>/... as a real host folder the user mounted."""
    match = re.fullmatch(r"/folders/([^/]+)(/.*)?", str(proj.get("gamePath", "")))
    folder = next((f for f in reafolders.load() if match and f["slug"] == match.group(1)), None)
    if not folder:
        raise Refused("The game folder is not one of REA's folders any more")
    base = Path(folder["path"]).resolve()
    game = Path(os.path.realpath(base / (match.group(2) or "/").lstrip("/")))
    if game != base and base not in game.parents:
        raise Refused("The game path leaves its folder")
    if not game.is_dir():
        raise Refused("The game folder doesn't exist")
    return game


def _state_file(game: Path) -> Path:
    return STATE_DIR / (hashlib.sha256(str(game).encode()).hexdigest()[:16] + ".json")


def _load_state(game: Path) -> dict:
    try:
        return json.loads(_state_file(game).read_text())
    except (OSError, ValueError):
        return {"game": str(game), "files": {}, "dirs": {}, "mods": {}}


def _save_state(game: Path, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _state_file(game).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, _state_file(game))


def game_running(game: Path) -> list[str]:
    """Processes whose program, working folder or command line is inside the game folder (Proton too)."""
    hits = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            places = [os.path.realpath(proc / "exe"), os.path.realpath(proc / "cwd")]
            places += (proc / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        if any(p == str(game) or p.startswith(str(game) + "/") for p in places if p):
            try:
                hits.append((proc / "comm").read_text().strip())
            except OSError:
                hits.append(proc.name)
    return sorted(set(hits))


def _tree(source: Path) -> list[str]:
    files = []
    for root, dirs, names in os.walk(source, followlinks=False):
        for name in names:
            full = Path(root) / name
            if full.is_symlink() or not full.is_file():
                raise Refused(f"install/ contains something that isn't a plain file: {full.relative_to(source)}")
            rel = str(full.relative_to(source))
            if ".." in Path(rel).parts:
                raise Refused("Bad path in install/")
            files.append(rel)
            if len(files) > MAX_FILES:
                raise Refused("install/ has too many files")
    return sorted(files)


def launch_hint(proj: dict) -> str:
    engine = proj.get("engine") or "unity-mono"
    if engine == "unreal":
        return ('Windows game (Proton): in Steam open the game\'s Properties → Launch Options and set\n'
                'WINEDLLOVERRIDES="dwmapi=n,b" %command%\nso UE4SS (dwmapi.dll) loads. The UE4SS window opens with the game.')
    if engine not in ("unity-mono", "unity-il2cpp"):
        return "No launch option needed: start the game as usual."
    if str(proj.get("platform", "")).startswith("win"):
        return ('Windows game (Proton): in Steam open the game\'s Properties → Launch Options and set\n'
                'WINEDLLOVERRIDES="winhttp=n,b" %command%\nso the BepInEx loader (winhttp.dll) runs. '
                'Outside Steam, launch it with that variable set.')
    return ('Linux game: in Steam open the game\'s Properties → Launch Options and set\n'
            f'{LINUX_LAUNCH_OPTION}\n(it starts BepInEx when a mod is installed and the plain game after Uninstall).\n'
            'Outside Steam, start the game with sh ./run_bepinex.sh from its folder.')


# Steam Launch Option for Linux BepInEx games: run_bepinex.sh when it's there, else the game as is.
LINUX_LAUNCH_OPTION = '''sh -c 'test -f run_bepinex.sh && exec sh run_bepinex.sh "$@"; exec "$@"' -- %command%'''


def launch_command(proj: dict, game: Path, root: Path, appid: str) -> tuple[list[str], dict] | None:
    """The recommended Launch Option run directly (cmd, extra env), so Play doesn't depend on what is saved
    in Steam. Native Linux BepInEx games only: Proton ones still need Steam to start them. Runs inside
    Steam's flatpak when that's the Steam in use, so the game finds the running client."""
    if not loader_marker(proj):
        return None
    exe = next((f.name for f in sorted(game.glob("*.x86_64")) if f.is_file()), None)
    if not exe:
        return None
    # LINUX_LAUNCH_OPTION with %command% = the game's binary, from the game folder like Steam does
    inner = ["sh", "-c", 'cd "$1" && shift && { test -f run_bepinex.sh && exec sh run_bepinex.sh "$@"; exec "$@"; }',
             "--", str(game), f"./{exe}"]
    env = {"SteamAppId": appid, "SteamGameId": appid}
    if "com.valvesoftware.Steam" in str(root):
        return ["flatpak", "run", *[f"--env={k}={v}" for k, v in env.items()], "--command=sh",
                "com.valvesoftware.Steam", *inner[1:]], {}
    return inner, env


def _start(proj: dict, game: Path, root: Path, appid: str) -> bool:
    """Start the game: the recommended launch command when there is one (True), else through Steam."""
    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0"), "XAUTHORITY": os.environ.get("XAUTHORITY", str(HOME / ".Xauthority"))}
    direct = launch_command(proj, game, root, appid)
    if direct:
        cmd, extra = direct
        env.update(extra)
    else:
        url = f"steam://rungameid/{appid}"
        cmd = ["flatpak", "run", "com.valvesoftware.Steam", url] if "com.valvesoftware.Steam" in str(root) else [shutil.which("steam") or "steam", url]
    subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return bool(direct)


def loader_marker(proj: dict) -> str | None:
    """Library that shows up in the game's /proc/PID/maps once the mod loader is injected (only checked
    where that's reliable: native Linux Unity games, where Doorstop is LD_PRELOADed by run_bepinex.sh)."""
    if proj.get("engine") in ("unity-mono", "unity-il2cpp") and str(proj.get("platform", "")).startswith("linux"):
        return "libdoorstop"
    return None


def steam_launch_options(root: Path, appid: str) -> str | None:
    """The game's Launch Options from Steam's localconfig.vdf ("" = none set), None if unknown. Steam saves
    that file a little after a change, so a value set seconds ago can still read as missing."""
    try:
        configs = sorted(root.glob("userdata/*/config/localconfig.vdf"), key=lambda f: f.stat().st_mtime, reverse=True)
        text = configs[0].read_text(errors="replace") if configs else ""
    except OSError:
        return None
    for m in re.finditer(r'\n(\t+)"%s"\s*\n\1\{' % re.escape(appid), text):
        indent = m.group(1)
        end = text.find("\n" + indent + "}", m.end())
        block = text[m.end():end if end > 0 else None]
        if not re.search(r'\n%s\t"(LastPlayed|Playtime|LaunchOptions)"' % indent, block):
            continue                                         # other per-app blocks (cloud, badges...)
        opt = re.search(r'\n%s\t"LaunchOptions"\s+"((?:[^"\\]|\\.)*)"' % indent, block)
        return re.sub(r'\\(.)', r'\1', opt.group(1)) if opt else ""
    return None


def _maps(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/maps").read_text(errors="replace")
    except OSError:
        return ""


def loader_state(game: Path, marker: str, timeout: float) -> bool | None:
    """True once a game process has the loader mapped, False if the game's own binary has been running
    for 5 s without it, None if no game process showed up in time."""
    deadline, seen = time.time() + timeout, None
    while time.time() < deadline:
        for pid in _pids(game):
            maps = _maps(pid)
            if marker in maps:
                return True
            if seen is None and str(game) + "/" in maps:     # the game binary itself (not the sh wrapper)
                seen = time.time()
        if seen and time.time() - seen > 5:
            return False
        time.sleep(1)
    return None


def stop_game(game: Path) -> None:
    for sig in (15, 15, 9):                                  # TERM, TERM, then KILL
        for pid in _pids(game):
            with contextlib.suppress(OSError):
                os.kill(pid, sig)
        for _ in range(10):
            if not _pids(game):
                return
            time.sleep(1)


def status(mod_id: str) -> dict:
    proj = project(mod_id)
    try:
        game = host_game_path(proj)
    except Refused as exc:
        return {"installed": False, "error": str(exc), "launch": launch_hint(proj)}
    state = _load_state(game)
    info = state["mods"].get(mod_id)
    hint, option_ok = launch_hint(proj), None
    if loader_marker(proj):
        with contextlib.suppress(Refused):
            root, appid = steam_app(game)
            opts = steam_launch_options(root, appid)
            if opts is not None:
                option_ok = "run_bepinex.sh" in opts
            if launch_command(proj, game, root, appid):
                hint = ("▶ Play and play tests start the game with the recommended launch options themselves. "
                        "To start it with the mod from Steam's library too:\n" + hint)
            elif option_ok is False:
                hint = ("⚠ Steam's Launch Options for this game don't start BepInEx yet, so ▶ Play would start the "
                        "unmodded game.\n\n" + hint)
    return {"installed": bool(info), "installed_at": (info or {}).get("at"), "files": (info or {}).get("files", 0),
            "game": str(game), "others": [m for m in state["mods"] if m != mod_id], "running": game_running(game),
            "launch": hint, "launch_option_ok": option_ok}


def install(mod_id: str) -> dict:
    with LOCK:
        proj = project(mod_id)
        game = host_game_path(proj)
        source = WORKSPACE / mod_id / "install"
        if not source.is_dir() or not any(source.iterdir()):
            raise Refused("Build the mod (or prepare the loader) first")
        running = game_running(game)
        if running:
            raise Refused(f"Close the game first ({', '.join(running)})")
        files = _tree(source)
        if proj.get("engine") == "godot":
            if proj.get("steps", {}).get("build", {}).get("state") != "done":
                raise Refused("Build the current Godot mod successfully before installing")
            artifact = str(proj.get("pckRel", ""))
            if artifact not in files:
                raise Refused(f"Godot build is missing {artifact}")
            built = source / artifact
            with built.open("rb") as handle:
                head = handle.read(4)
                handle.seek(-4, os.SEEK_END)
                tail = handle.read(4)
            if artifact.lower().endswith(".exe") and (head[:2] != b"MZ" or tail != b"GDPC"):
                raise Refused("Godot build is a raw PCK mislabeled as an executable; rebuild it before installing")
            if artifact.lower().endswith(".pck") and head != b"GDPC":
                raise Refused("Godot build is not a valid PCK; rebuild it before installing")
        state = _load_state(game)
        backups = _state_file(game).with_suffix("") / "backup"
        for rel in files:
            src, dst = source / rel, game / rel
            entry = state["files"].get(rel)
            if entry is None:
                entry = {"owners": [], "created": not dst.exists(), "backup": None}
                if dst.exists() and not filecmp.cmp(src, dst, shallow=False):
                    backup = backups / rel
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, backup)
                    entry["backup"] = str(backup)
                state["files"][rel] = entry
            # Track folders we create so uninstall can remove them when empty.
            for parent in reversed(Path(rel).parents[:-1]):
                folder = game / parent
                if not folder.exists():
                    folder.mkdir()
                    state["dirs"][str(parent)] = True
            if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
                shutil.copy2(src, dst)
            if rel.endswith(".sh"):
                dst.chmod(dst.stat().st_mode | 0o111)
            if mod_id not in entry["owners"]:
                entry["owners"].append(mod_id)
        state["mods"][mod_id] = {"name": proj.get("name"), "at": time.time(), "files": len(files)}
        state["gamePath"] = proj.get("gamePath", "")   # REA builds read originals from backup/ by this path
        _save_state(game, state)
        (WORKSPACE / mod_id / ".installed").write_text(str(game))
        return {**status(mod_id), "message": f"Installed {proj.get('name')} into {game}"}


def _installed_states() -> list[dict]:
    states = []
    for file in sorted(STATE_DIR.glob("*.json")):
        try:
            state = json.loads(file.read_text())
        except (OSError, ValueError):
            continue
        if state.get("mods") and state.get("game"):
            states.append(state)
    return states


def installed() -> dict:
    """Every mod currently installed in any game folder, for the REA page's Installed list."""
    mods = []
    for state in _installed_states():
        game = Path(state["game"])
        running = game_running(game) if game.is_dir() else []
        for mod_id, info in state["mods"].items():
            proj = None
            with contextlib.suppress(Refused):
                proj = project(mod_id)
            mods.append({"id": mod_id, "name": (proj or {}).get("name") or info.get("name") or mod_id,
                         "installed_at": info.get("at"), "files": info.get("files", 0), "game": str(game),
                         "gameName": (proj or {}).get("gameName") or game.name, "gamePath": state.get("gamePath", ""),
                         "project": proj is not None, "missing": not game.is_dir(), "running": running})
    mods.sort(key=lambda m: -(m["installed_at"] or 0))
    return {"mods": mods}


def _installed_game(mod_id: str) -> Path:
    """The game folder a mod is installed in, even if its REA project was deleted since."""
    try:
        return host_game_path(project(mod_id))
    except Refused:
        if not re.fullmatch(r"[0-9a-f]{12}", mod_id or ""):
            raise
        for state in _installed_states():
            if mod_id in state["mods"] and Path(state["game"]).is_dir() and _state_file(Path(state["game"])).is_file():
                return Path(state["game"])
        raise


def uninstall(mod_id: str) -> dict:
    with LOCK:
        game = _installed_game(mod_id)
        proj = None
        with contextlib.suppress(Refused):
            proj = project(mod_id)
        running = game_running(game)
        if running:
            raise Refused(f"Close the game first ({', '.join(running)})")
        state = _load_state(game)
        if mod_id not in state["mods"]:
            (WORKSPACE / mod_id / ".installed").unlink(missing_ok=True)
            raise Refused("This mod isn't installed")
        for rel, entry in list(state["files"].items()):
            if mod_id not in entry["owners"]:
                continue
            entry["owners"].remove(mod_id)
            if entry["owners"]:
                continue
            target = game / rel
            if entry.get("backup") and Path(entry["backup"]).is_file():
                shutil.copy2(entry["backup"], target)
                Path(entry["backup"]).unlink()
            elif entry.get("created"):
                target.unlink(missing_ok=True)
            del state["files"][rel]
        # Remove folders we created, deepest first, only when empty (BepInEx writes logs/config there).
        for rel in sorted(state["dirs"], key=lambda p: p.count("/"), reverse=True):
            if not any(f == rel or f.startswith(rel + "/") for f in state["files"]):
                try:
                    (game / rel).rmdir()
                    del state["dirs"][rel]
                except OSError:
                    pass
        name = (proj or {}).get("name") or state["mods"][mod_id].get("name") or mod_id
        del state["mods"][mod_id]
        _save_state(game, state)
        (WORKSPACE / mod_id / ".installed").unlink(missing_ok=True)
        left = [d for d in state["dirs"]]
        note = f" BepInEx's own logs/config stay in: {', '.join(left)}." if left and not state["mods"] else ""
        message = f"Removed {name} from {game}.{note}"
        if proj is None:
            return {"installed": False, "game": str(game), "message": message}
        return {**status(mod_id), "message": message}


# ---- play test on the real game ---------------------------------------------------------------
# The REA container can only run games headless and without Steam (GodotSteam games quit there).
# A play test installs the build, starts the game through the user's Steam on the desktop, keeps
# it running for a while, takes screenshots, closes it and returns what the game logged. The
# Agent runs it inside its GPU slot (the game shares the GPU with the desktop and the local LLM).
PLAY_LOCK = threading.Lock()
STEAM_ROOTS = [HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam", HOME / ".local/share/Steam", HOME / ".steam/steam"]
FLATPAK_STEAM_HOME = HOME / ".var/app/com.valvesoftware.Steam"
ERROR_LINE = re.compile(r"SCRIPT ERROR|^ERROR:|Exception|NullReference|\[Error|\[Fatal|Traceback|Unhandled|segfault|crash", re.I)


def steam_app(game: Path) -> tuple[Path, str]:
    """(Steam root, app id) for a game installed in a Steam library."""
    apps = game.parent.parent if game.parent.name == "common" else None
    for manifest in (apps.glob("appmanifest_*.acf") if apps else []):
        try:
            text = manifest.read_text(errors="replace")
        except OSError:
            continue
        if re.search(r'"installdir"\s+"%s"' % re.escape(game.name), text, re.I):
            appid = re.search(r'"appid"\s+"(\d+)"', text)
            root = next((r for r in STEAM_ROOTS if r.exists() and (apps == r / "steamapps" or str(apps).startswith(str(r)))), apps.parent)
            if appid:
                return root, appid.group(1)
    raise Refused("This game isn't in a Steam library, so it can't be started for a play test")


def _pids(game: Path) -> list[int]:
    out = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            places = [os.path.realpath(proc / "exe"), os.path.realpath(proc / "cwd")]
            places += (proc / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        if any(p == str(game) or p.startswith(str(game) + "/") for p in places if p):
            out.append(int(proc.name))
    return out


def _log_candidates(game: Path, root: Path, appid: str) -> list[Path]:
    prefix = root / "steamapps/compatdata" / appid / "pfx/drive_c/users/steamuser"
    globs = [(prefix / "AppData/Roaming/Godot/app_userdata", "*/logs/*.log"), (HOME / ".local/share/godot/app_userdata", "*/logs/*.log"),
             (prefix / "AppData/LocalLow", "*/*/Player.log"), (HOME / ".config/unity3d", "*/*/Player.log"),
             (FLATPAK_STEAM_HOME / ".config/unity3d", "*/*/Player.log"),
             (FLATPAK_STEAM_HOME / ".local/share/godot/app_userdata", "*/logs/*.log"),
             (game, "BepInEx/LogOutput.log"), (game, "*/Saved/Logs/*.log"), (game, "**/UE4SS.log"), (game, "*.log"),
             (prefix / "AppData/Local", "*/Saved/Logs/*.log")]
    found = []
    for base, pattern in globs:
        if base.is_dir():
            found += [f for f in list(base.glob(pattern))[:40] if f.is_file()
                      and not re.fullmatch(r"godot\d{4}-\d\d-\d\dT.*\.log", f.name)]
    return found


def _log_marker(path: Path) -> tuple[int, int, bytes]:
    """Remember enough of a log to distinguish an append from a rewritten file."""
    with path.open("rb") as handle:
        stat = os.fstat(handle.fileno())
        handle.seek(max(0, stat.st_size - 4096))
        return stat.st_ino, stat.st_size, handle.read()


def _fresh_log(path: Path, since: float) -> bool:
    """A log the game rewrote at this launch. Godot starts every launch with its banner and rotates the
    previous log to godotYYYY-MM-DDT….log; its startup lines are identical every time, so the old log
    is a byte prefix of the new one and looks like an append (one Godot 3 game's baseline kept 7 of
    53 lines and called 9 normal game errors "new")."""
    try:
        with path.open("rb") as handle:
            if handle.read(14) == b"Godot Engine v":
                return True
    except OSError:
        return False
    return path.name == "godot.log" and any(re.fullmatch(r"godot\d{4}-\d\d-\d\dT.*\.log", f.name) and f.stat().st_mtime >= since
               for f in path.parent.glob("godot*.log"))


def _log_offset(path: Path, marker: tuple[int, int, bytes] | None, since: float = 0.0) -> int:
    if marker is None or (since and _fresh_log(path, since)):
        return 0
    old_inode, old_size, old_tail = marker
    with path.open("rb") as handle:
        stat = os.fstat(handle.fileno())
        if stat.st_ino != old_inode or stat.st_size < old_size:
            return 0
        handle.seek(old_size - len(old_tail))
        return old_size if handle.read(len(old_tail)) == old_tail else 0


def _screenshot(path: Path) -> bool:
    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0"), "XAUTHORITY": os.environ.get("XAUTHORITY", str(HOME / ".Xauthority"))}
    try:
        # JPEG, not PNG: ~10x smaller, so the agent's vision check uploads and reads it fast.
        return subprocess.run(["import", "-window", "root", "-resize", "1280x1280>", "-quality", "85", str(path)], env=env, timeout=20,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0 and path.is_file()
    except (OSError, subprocess.SubprocessError):
        return False


def _normalized_error(line: str) -> str:
    line = re.sub(r"^\s*\[?\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\]?\s*", "", line)
    line = re.sub(r"0x[0-9a-f]+", "0x?", line, flags=re.I)
    line = re.sub(r"\b(id|instance|Object)\s*[#:]?\s*\d+", r"\1 ?", line, flags=re.I)
    line = re.sub(r"@(\w+)@\d+", r"@\1@?", line)                 # Godot auto-names: @player@1247
    return re.sub(r"\s+", " ", line).strip()[:300]


# ---- Play test diagnosis -----------------------------------------------------------------------
# The raw error list of a play test is mostly the game's own noise, capped and repeated (a Godot 3 game
# 2026-10-16: 60 lines, 2 of them the mod's bug, no source line, no baseline because another mod was
# installed). The local agent model went round in circles on it. The diagnosis groups errors with
# their "at:" line and count, splits what happened before and after the mod's own output, checks the
# game's earlier clean runs, explains known Godot errors and points at the mod lines behind them.
# What a Godot engine error means (never what to change: the agent works that out) and which mod
# source lines are involved in it. An empty meaning marks asset / scene-setup noise.
GODOT_HINTS = [
    (r"up vector can't be zero|look_at\(\) failed",
     "look_at() was called while the direction from the node to its target is parallel to the up vector "
     "(e.g. the node sits straight below/above the target, or both are at the same place).",
     r"look_at\("),
    (r'!is_inside_tree\(\)',
     "Something read a node's global transform (global_transform, look_at, to_global...) while that node was not in the scene tree.",
     r"look_at\(|global_transform|global_translation|to_global\("),
    (r'Node not found: "[^"]+" \(relative to "/root/',
     "A script or scene looked up this node path under that parent and it doesn't exist there.", None),
    (r"Invalid (get|set) index|Invalid call|Nonexistent function|null instance|Attempt to call function",
     "GDScript runtime error; the script file:line is in the log around it.", None),
    (r"Skin bind .* no bone|couldn't resolve track|ViewportTexture: Path to node is invalid", "", None),
]


def _mod_sources(mod_id: str) -> dict[str, list[str]]:
    src = WORKSPACE / mod_id / "src/files"
    out = {}
    for f in sorted(src.rglob("*.gd"))[:200] if src.is_dir() else []:
        try:
            out["res://" + str(f.relative_to(src))] = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
    cs = WORKSPACE / mod_id / "src"                          # Unity (BepInEx) mods: src/*.cs
    for f in sorted(f for f in cs.rglob("*.cs") if "obj" not in f.relative_to(cs).parts)[:200] if cs.is_dir() else []:
        try:
            out["src/" + str(f.relative_to(cs))] = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
    return out


def _source_lines(sources: dict[str, list[str]], pattern: str, limit: int = 8) -> list[str]:
    hits = []
    for path, lines in sources.items():
        for n, line in enumerate(lines, 1):
            if re.search(pattern, line.split("#", 1)[0]):
                hits.append(f"{path}:{n}: {line.strip()[:140]}")
    return hits[:limit]


def _family(line: str) -> str:
    """Group key: one entry per kind of error (Skin bind #2 'shoulderl', #3 'arml'… → one), but node paths
    in double quotes stay distinct because they say which scene an error came from."""
    line = _normalized_error(line)
    # Several missing children under one parent (tail, tail_fox, old... on each player) are one problem.
    line = re.sub(r'Node not found: "[^"]*" \(relative to', 'Node not found: "…" (relative to', line)
    return re.sub(r"#\d+|\b\d+\b", "#", re.sub(r"'[^']*'", "'…'", line))


def _known_clean_errors(appid: str) -> set[str]:
    """Error keys from every earlier clean (unmodded) run of this game, from any mod project."""
    known = set()
    for res in sorted(WORKSPACE.glob("*/playtests/*/result.json"))[-200:]:
        try:
            r = json.loads(res.read_text())
        except (OSError, ValueError):
            continue
        if r.get("appid") != appid or (r.get("baseline") or {}).get("status") != "ready":
            continue
        for copy in (res.parent / "baseline").glob("*.log"):
            with contextlib.suppress(OSError):
                known |= {_family(l) for l in copy.read_text(errors="replace").splitlines() if ERROR_LINE.search(l)}
    return known


def _error_groups(text: str, markers: list[str]) -> tuple[list[dict], int | None]:
    """Unique errors in log order with count, first line, the engine's "at:" line and the last normal
    line printed before it; plus the line where the mod's own output first appears."""
    lines = text.splitlines()
    mod_line = next((i for i, l in enumerate(lines) if any(m in l for m in markers)), None)
    groups, by_key, context = [], {}, ""
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("at:"):
            continue
        if not ERROR_LINE.search(line):
            context = line[:160]
            continue
        key = _family(line)
        g = by_key.get(key)
        if not g:
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            g = by_key[key] = {"key": key, "line": line[:300], "first": i + 1, "count": 0, "context": context,
                               "at": nxt[:300] if nxt.startswith("at:") else "", "before_mod": False, "after_mod": False}
            groups.append(g)
        g["count"] += 1
        missing = re.search(r'Node not found: "([^"]*)"', line)
        if missing and missing.group(1) not in g.setdefault("missing", []) and len(g["missing"]) < 12:
            g["missing"].append(missing.group(1))
        if mod_line is not None:
            g["before_mod" if i < mod_line else "after_mod"] = True
    return groups, (mod_line + 1 if mod_line is not None else None)


def _diagnose(mod_id: str, result: dict, appid: str) -> dict:
    sources = _mod_sources(mod_id)
    markers = sorted({m.group(1)[:40] for lines in sources.values() for l in lines
                      for m in re.finditer(r'(?:print|Debug\.Log\w*|Logger\.Log\w*|Console\.WriteLine)\(\s*\$?"([^"%{]{8,})', l)})[:30]
    markers += sorted({f"Loading [{m.group(1)}" for lines in sources.values() for l in lines        # BepInEx chainloader line
                       for m in re.finditer(r'BepInPlugin\(\s*"[^"]*"\s*,\s*"([^"]{3,})"', l)})
    text = ""
    for log in result.get("logs") or []:
        with contextlib.suppress(OSError):
            text += Path(log["copy"]).read_text(errors="replace") + "\n"
    groups, mod_line = _error_groups(text, markers)
    clean = _known_clean_errors(appid)
    if (result.get("baseline") or {}).get("status") == "ready":
        clean |= {_family(l) for l in result["baseline"].get("errors") or []}
    godot = re.search(r"Godot Engine v(\d)\.(\d+)", text)
    suspects = [g for g in groups if g["key"] not in clean and not g["before_mod"]]

    def tag(g: dict) -> str:
        if g["key"] in clean:
            return "also in a clean game run (game noise)"
        if g["before_mod"]:
            return "already before your mod's code ran (likely game noise)"
        return "NEW after your mod's code ran" if mod_line is not None else "not in any clean run"

    hinted = []
    for g in suspects:
        for pattern, hint, src in GODOT_HINTS:
            if re.search(pattern, g["line"]):
                hinted.append((g, hint, _source_lines(sources, src) if src else []))
                break

    # Short version (goes into the agent's resume message): most likely causes first.
    short = []
    for g, hint, lines in [h for h in hinted if h[1]][:4]:
        short.append(f"{g['line'][:160]} ×{g['count']}" + (f" (missing: {', '.join(g['missing'])})" if len(g.get("missing", [])) > 1 else "")
                     + f" (log line {g['first']}, after \"{g['context'][:60]}\"; {g['at'][4:120]}). Means: {hint}"
                     + (f" Mod lines involved: {' | '.join(lines[:4])}" if lines else ""))
    if result.get("exited_early"):
        tail = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("at:")][-3:]
        left = max(0, int(result.get("requested_seconds") or 0) - int(result.get("ran_seconds") or 0))
        short.append(f"The game process ended {result.get('ran_seconds')} s in, {left} s before the test would have closed it (a crash, "
                     "a quit() call, or the user closing the window). Crash output isn't captured under Proton, so the last log lines "
                     f"are the best clue: {' / '.join(t[:100] for t in tail)}")
    uf = result.get("user_files") or {}
    if uf and not uf.get("consumed"):
        names = ", ".join(sorted({Path(p).name for p in uf.get("placed", [])}))
        short.append(f"user://{names} was placed before launch and was still there afterwards: no mod code read or deleted it, so "
                     "nothing opened the mod's scene automatically and the screenshots show only what the game did by itself.")
    if result.get("loader") is False:
        short.insert(0, "The game started WITHOUT the mod loader (BepInEx/Doorstop was not injected), so the mod never ran and the "
                        "test was stopped. Steam's Launch Options for the game must start run_bepinex.sh; that's the user's "
                        "setting (REA page → Play & refine shows it), not something a code change fixes.")
    elif mod_line is None and sources:
        short.append("None of your mod's log lines (print/Debug.Log/Logger) appear in the log: its code never ran in this test.")
    if godot and "SCRIPT ERROR" not in text:
        short.append(f"This is an exported (release) {godot.group(0)} build: GDScript runtime errors (an unknown property, a call on null) "
                     "are NOT written to the log; such a line silently does nothing. A missing error is not proof the code works.")
    assets = [g for g in suspects if any(re.search(p, g["line"]) and not h for p, h, _ in GODOT_HINTS)]
    short.append(f"{len(groups)} kind(s) of error: {len(suspects)} new since the mod's code ran ({len(assets)} of them asset/animation "
                 f"warnings), {len(groups) - len(suspects)} also before the mod ran or in a clean game run. "
                 "Full report: diagnosis.md in the play test folder")

    # Full report next to the logs.
    out = [f"# Play test diagnosis — run {result.get('run')}",
           f"Engine: {godot.group(0) if godot else 'unknown'} · ran {result.get('ran_seconds')} s of {result.get('requested_seconds')} s"
           + (" · EXITED BY ITSELF" if result.get("exited_early") else ""),
           f"Mod output first seen at log line {mod_line}." if mod_line else
           "The mod's own log output (print/Debug.Log/Logger) never appeared in the log.",
           "\n## Most likely causes\n", *[f"- {s}" for s in short[:-1]],
           "\n## Errors not in a clean run and not before the mod ran\n"]
    out += [f"- line {g['first']} ×{g['count']} [{tag(g)}]: {g['line']}"
            + (f"\n  missing: {', '.join(g['missing'])}" if len(g.get("missing", [])) > 1 else "")
            + f"\n  {g['at']}\n  after: {g['context']}" for g in suspects if g not in assets]
    out.append("\n## New asset / animation warnings (players, skins, animation tracks)\n")
    out += [f"- line {g['first']} ×{g['count']}: {g['line'][:200]}" for g in assets]
    out.append("\n## Game noise\n")
    out += [f"- line {g['first']} ×{g['count']} [{tag(g)}]: {g['line'][:200]}" for g in groups if g not in suspects]
    report = Path(result["folder"]) / "diagnosis.md"
    with contextlib.suppress(OSError):
        report.write_text("\n".join(out) + "\n")
    return {"text": "\n".join(f"• {s}" for s in short)[:3000], "file": str(report), "unique_errors": len(groups),
            "suspect_errors": [g["line"] for g in suspects][:20], "mod_output_line": mod_line}


def _compare_errors(errors: list[str], baseline: list[str]) -> tuple[list[str], list[str]]:
    known = {_normalized_error(line) for line in baseline}
    new, shared, seen = [], [], set()
    for line in errors:
        key = _normalized_error(line)
        if key in seen:
            continue
        seen.add(key)
        (shared if key in known else new).append(line)
    return new, shared


def _user_dirs(game: Path, root: Path, appid: str) -> list[Path]:
    """Where the game's user:// lives (Godot app_userdata/<project>), found from its logs folder."""
    dirs = []
    for log in _log_candidates(game, root, appid):
        if log.parent.name == "logs" and log.parent.parent.parent.name == "app_userdata" and log.parent.parent not in dirs:
            dirs.append(log.parent.parent)
    return dirs


def _place_user_files(game: Path, root: Path, appid: str, names: list[str]) -> list[Path]:
    """Create empty one-shot files in user:// (e.g. a mod's autostart flag that opens its scene) so an
    unattended play test reaches content behind menus. Removed after the run if the mod didn't."""
    if not names:
        return []
    dirs = _user_dirs(game, root, appid)
    if not dirs:
        raise Refused("user_files: no user:// folder found (only Godot games with an earlier launch log are supported)")
    placed = []
    for name in names:
        for folder in dirs:
            target = folder / name
            target.write_text("rocsy play test\n")
            placed.append(target)
    return placed


def _shot_times(seconds: int, shots: int) -> list[float]:
    """Seconds after the game started: 3 shots = 15 s in, half way and just before the game is closed
    (the agent's play tests); other counts spread evenly. Shots closer than 3 s merge."""
    if shots <= 0:
        return []
    if shots == 3:
        times = [min(15, seconds - 3), seconds / 2, seconds - 3]
    else:
        times = [seconds * (i + 1) / (shots + 1) for i in range(shots)]
    out: list[float] = []
    for at in sorted(max(1, t) for t in times):
        if not out or at - out[-1] >= 3:
            out.append(at)
    return out


def _capture_game(mod_id: str, game: Path, root: Path, appid: str, out: Path, seconds: int, shots: int,
                  marker: str | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    if game_running(game):
        raise Refused("The game is already running; close it before a play test")
    try:
        before = {f: _log_marker(f) for f in _log_candidates(game, root, appid)}
        start = time.time()
        _start(project(mod_id), game, root, appid)
        started_at = None
        while time.time() - start < 150:                     # Steam may need to start first
            if _pids(game):
                started_at = time.time()
                break
            time.sleep(2)
        pictures, exited_early, loader = [], False, None
        if started_at and marker:
            loader = loader_state(game, marker, 30)
            if loader is False:
                seconds = 0                                  # mod can't run: don't sit through the test
        if started_at:
            marks = [started_at + at for at in _shot_times(seconds, shots)]
            while time.time() - started_at < seconds:
                if not _pids(game):
                    exited_early = True
                    break
                if marks and time.time() >= marks[0]:
                    marks.pop(0)
                    at = round(time.time() - started_at)
                    shot = out / f"screen-{len(pictures) + 1}-{at}s.jpg"
                    if _screenshot(shot):
                        pictures.append(str(shot))
                time.sleep(1)
            ran = round(time.time() - started_at)
            stop_game(game)
        else:
            ran = 0
        time.sleep(2)
        logs, errors = [], []
        for f in _log_candidates(game, root, appid):
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime < start - 5:
                continue
            offset = _log_offset(f, before.get(f), start - 5)
            with f.open("rb") as handle:
                handle.seek(max(offset, st.st_size - 2 * 1024 * 1024))
                text = handle.read().decode(errors="replace")
            copy = out / (re.sub(r"[^\w.-]+", "_", f.name) or "game.log")
            copy.write_text(text)
            logs.append({"path": str(f), "copy": str(copy), "bytes": len(text), "tail": text[-4000:]})
            errors += [line.strip()[:300] for line in text.splitlines() if ERROR_LINE.search(line)][:60]
        result = {"folder": str(out), "appid": appid, "started": bool(started_at), "ran_seconds": ran,
                  "exited_early": exited_early, "still_running": bool(_pids(game)), "screenshots": pictures, "logs": logs,
                  "errors": errors[:60], "installed": status(mod_id).get("installed", False)}
        if marker:
            result["loader"] = loader
        if loader is False:
            result["note"] = ("The game started WITHOUT the mod loader, so the test was stopped. "
                              + launch_hint(project(mod_id)))
        if not started_at:
            result["note"] = "The game didn't start within 150 s (is Steam logged in? is the game installed?)."
        return result
    except Exception:
        if _pids(game):
            for pid in _pids(game):
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
        raise


def play(mod_id: str, body: dict | None = None) -> dict:
    body = body or {}
    seconds = max(15, min(600, int(body.get("seconds") or 90)))
    shots = max(0, min(6, int(body.get("screenshots", 3))))
    user_files = body.get("user_files") or []
    if not isinstance(user_files, list) or len(user_files) > 4 or not all(isinstance(n, str) and re.fullmatch(r"[\w.-]{1,64}", n) and not n.startswith(".") for n in user_files):
        raise Refused("user_files: up to 4 plain file names (letters, digits, . _ -)")
    if not PLAY_LOCK.acquire(blocking=False):
        raise Refused("A play test is already running")
    try:
        proj = project(mod_id)
        game = host_game_path(proj)
        if game_running(game):
            raise Refused(f"The game is already running ({', '.join(game_running(game))}): close it first")
        root, appid = steam_app(game)
        before = status(mod_id)
        clean_after = bool(body.get("uninstall")) and not before.get("others") and (before.get("installed") or body.get("install", True))
        clean_before = body.get("install", True) and not before.get("installed") and not before.get("others") and not clean_after
        run_seconds = max(seconds, 45) if clean_before or clean_after else seconds
        run = time.strftime("%Y%m%dT%H%M%S")
        out = WORKSPACE / mod_id / "playtests" / run
        out.mkdir(parents=True, exist_ok=True)
        baseline = {"status": "unavailable", "reason": "No clean game run was available for comparison."}

        def record_baseline() -> dict:
            # Both launches need time to pass Steam startup and reach a comparable scene.
            clean = _capture_game(mod_id, game, root, appid, out / "baseline", run_seconds, 0)
            (out / "baseline" / "result.json").write_text(json.dumps(clean, indent=1))
            lines = sum(log["tail"].count("\n") if log["bytes"] <= 4000 else 999 for log in clean["logs"])
            has_output = lines >= 20
            ready = clean["started"] and not clean["exited_early"] and not clean["still_running"] and has_output
            return {"status": "ready" if ready else "unavailable", "ran_seconds": clean["ran_seconds"],
                    "errors": clean["errors"], "folder": clean["folder"], "log_lines": lines,
                    "reason": "" if ready else ("The clean game did not reach a comparable run or produce a game log." if not has_output or not clean["started"]
                                                else "The clean game exited or kept running unexpectedly.")
                              + (f" Its log had only {lines} line(s)." if clean["started"] and not has_output else "")}

        if clean_before:
            baseline = record_baseline()
            if game_running(game):
                raise Refused("The clean game is still running; refused to install the mod")
        if body.get("install", True) and not before.get("installed"):
            install(mod_id)
        placed = _place_user_files(game, root, appid, user_files)
        try:
            result = _capture_game(mod_id, game, root, appid, out, run_seconds, shots, loader_marker(proj))
        finally:
            leftover = [str(f) for f in placed if f.exists()]
            for f in placed:
                f.unlink(missing_ok=True)
        if placed:
            result["user_files"] = {"placed": [str(f) for f in placed], "consumed": not leftover, "left_over": leftover}
        result["run"] = run
        result["requested_seconds"] = seconds
        if body.get("uninstall") and result["installed"] and not result["still_running"]:
            result["uninstall"] = uninstall(mod_id).get("message", "")
        if clean_after and result.get("uninstall"):
            baseline = record_baseline()
        if not result["installed"]:
            baseline = {"status": "clean_only", "reason": "No mod was installed for this run."}
        elif before.get("others"):
            baseline = {"status": "unavailable", "reason": "Other mods were installed, so a clean baseline could not be measured."}
        elif before.get("installed") and not clean_after:
            baseline = {"status": "unavailable", "reason": "The mod was already installed; a clean baseline requires uninstalling it."}
        result["baseline"] = baseline
        mod_log_available = any(log["bytes"] > 0 for log in result["logs"])
        if baseline["status"] == "ready" and mod_log_available:
            result["comparison_status"] = "compared"
            result["new_errors"], result["baseline_errors"] = _compare_errors(result["errors"], baseline["errors"])
            result["unclassified_errors"] = []
        else:
            result["comparison_status"] = "inconclusive" if baseline["status"] == "ready" else "unclassified"
            if baseline["status"] == "ready" and not mod_log_available:
                result["comparison_note"] = "The modded launch produced no game log; it may not have reached the same scene as the clean game."
            result["new_errors"], result["baseline_errors"] = [], []
            result["unclassified_errors"] = result["errors"]
        if result.get("started"):
            try:
                result["diagnosis"] = _diagnose(mod_id, result, appid)
            except Exception as exc:  # noqa: BLE001  (a report bug must not lose the play test)
                result["diagnosis"] = {"text": f"(diagnosis failed: {exc})"}
        (out / "result.json").write_text(json.dumps(result, indent=1))
        return result
    finally:
        PLAY_LOCK.release()


# ---- Play button on the REA page ---------------------------------------------------------------
# Starts the game through the user's Steam on the desktop with whatever is installed and leaves it
# running for the user to play (the timed, self-closing play() above is the agent's play test).
def launch(mod_id: str) -> dict:
    if PLAY_LOCK.locked():
        raise Refused("A play test is running; wait for it to finish")
    proj = project(mod_id)
    game = host_game_path(proj)
    running = game_running(game)
    if running:
        raise Refused(f"The game is already running ({', '.join(running)})")
    root, appid = steam_app(game)
    direct = _start(proj, game, root, appid)
    installed = status(mod_id).get("installed", False)
    name = proj.get("gameName") or game.name
    marker = loader_marker(proj)
    if not installed or not marker:
        return {"launched": True, "appid": appid, "installed": installed,
                "message": f"Starting {name} through Steam" + ("" if installed else " (the mod is NOT installed: this is the unmodded game)")}
    loaded = loader_state(game, marker, 150)                 # Steam may need to start first
    how = " with the recommended launch options" if direct else ""
    if loaded is False:
        stop_game(game)
        raise Refused(f"{name} started WITHOUT the mod loader (BepInEx), so it was closed again. "
                      + ("It was started with the recommended launch command, so check BepInEx/LogOutput.log and run_bepinex.sh."
                         if direct else launch_hint(proj)))
    return {"launched": True, "appid": appid, "installed": True, "loader": loaded,
            "message": f"{name} is running with the mod loaded{how}" if loaded else
                       f"Asked Steam to start {name}, but it hadn't started after 150 s (is Steam logged in?)"}


def handle(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    if path == "/rea/mods/installed":
        return (200, installed()) if method == "GET" else (405, {"error": "method not allowed"})
    match = re.fullmatch(r"/rea/mods/([0-9a-f]{12})/(status|install|uninstall|play|launch)", path)
    if not match:
        return 404, {"error": "not found"}
    mod_id, action = match.groups()
    try:
        if method == "GET" and action == "status":
            return 200, status(mod_id)
        if method == "POST" and action == "install":
            return 200, install(mod_id)
        if method == "POST" and action == "uninstall":
            return 200, uninstall(mod_id)
        if method == "POST" and action == "play":
            return 200, play(mod_id, body)
        if method == "POST" and action == "launch":
            return 200, launch(mod_id)
        return 405, {"error": "method not allowed"}
    except Refused as exc:
        return 409, {"error": str(exc)}
    except OSError as exc:
        return 500, {"error": f"{exc.strerror or exc}: {exc.filename or ''}"[:300]}
