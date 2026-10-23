"""Desktop/terminal hooks an installed Rocsy adds to the user's account, and their removal.

- the `rocsy` terminal command: ~/.local/bin/rocsy -> <install>/BunnyBytes/cli/rocsy
- Rocsy Desktop: ~/.local/share/applications/rocsy.desktop (`rocsy desktop`, its own app window)

Used by the bundle installer (install/update toggles) and by uninstall_worker.py. Only files
that point back at this install are ever replaced or removed: a `rocsy` command or menu entry
from something else is left alone (the installer refuses instead of overwriting it).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

DESKTOP_ID = "rocsy.desktop"


def _data_home() -> Path:
    return Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local/share")


def command_path() -> Path:
    return Path.home() / ".local/bin" / "rocsy"


def desktop_path() -> Path:
    return _data_home() / "applications" / DESKTOP_ID


def cli_script(root: Path) -> Path:
    return root / "BunnyBytes" / "cli" / "rocsy"


def _points_into_rocsy(link: Path) -> bool:
    """A symlink to some Rocsy install's cli/rocsy (this one or an older one)."""
    if not link.is_symlink():
        return False
    target = Path(os.path.realpath(link))
    return target.name == "rocsy" and target.parent.name == "cli" and (target.parents[2] / ".rocsy-install.json").is_file()


def owns_command(root: Path) -> bool:
    link = command_path()
    return link.is_symlink() and Path(os.path.realpath(link)) == cli_script(root).resolve()


def owns_desktop(root: Path) -> bool:
    return _ours(desktop_path(), root)


def conflicts(root: Path, command: bool, desktop: bool) -> str:
    """Why a toggle can't be applied (something that isn't Rocsy's is in the way), or ""."""
    link, entry = command_path(), desktop_path()
    if command and (link.exists() or link.is_symlink()) and not _points_into_rocsy(link) and not owns_command(root):
        return f"{link} already exists and isn't Rocsy's. Move it away or turn off the terminal command."
    if desktop and entry.exists() and not _rocsy_entry(entry):
        return f"{entry} already exists and isn't Rocsy's. Move it away or turn off Rocsy Desktop."
    return ""


def _ours(entry: Path, root: Path) -> bool:
    try:
        return entry.is_file() and not entry.is_symlink() and f"X-Rocsy-Install={root}\n" in entry.read_text()
    except OSError:
        return False


def _rocsy_entry(entry: Path) -> bool:
    try:
        return entry.is_file() and not entry.is_symlink() and "\nX-Rocsy-Install=" in entry.read_text()
    except OSError:
        return False


def install_command(root: Path) -> dict:
    script = cli_script(root)
    if not script.is_file():
        raise ValueError("The rocsy command was not included in the bundle.")
    script.chmod(0o755)
    if problem := conflicts(root, True, False):
        raise ValueError(problem)
    link = command_path()
    if link.exists() or link.is_symlink():
        link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(script)
    on_path = str(link.parent) in os.environ.get("PATH", "").split(os.pathsep)
    return {"path": str(link), "on_path": on_path}


def remove_command(root: Path) -> bool:
    if owns_command(root):
        command_path().unlink()
        return True
    return False


def _desktop_value(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


def install_desktop(root: Path) -> dict:
    script = cli_script(root)
    if not script.is_file():
        raise ValueError("Rocsy Desktop needs the rocsy command, which was not included in the bundle.")
    script.chmod(0o755)
    if problem := conflicts(root, False, True):
        raise ValueError(problem)
    entry = desktop_path()
    entry.parent.mkdir(parents=True, exist_ok=True)
    exe = _desktop_value(str(script))
    icon = root / "BunnyBytes/gateway/static/icons/rocsy-192.png"
    entry.write_text("[Desktop Entry]\nType=Application\nName=Rocsy\nGenericName=Rocsy Desktop\n"
                     "Comment=Open Rocsy in its own window\n"
                     f'Exec="{exe}" desktop\nTryExec={script}\n'
                     f"Icon={icon}\nTerminal=false\nStartupNotify=true\nStartupWMClass=RocsyDesktop\n"
                     "Categories=Utility;Graphics;AudioVideo;\nKeywords=AI;Agent;Image;Video;Music;\n"
                     "Actions=agent;\n"
                     f"X-Rocsy-Install={root}\n\n"
                     "[Desktop Action agent]\nName=Open the Master Agent\n"
                     f'Exec="{exe}" desktop Agent\n')
    entry.chmod(0o644)
    _refresh_menu(entry.parent)
    browser = any(shutil.which(name) for name in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome",
                                                  "brave", "brave-browser", "microsoft-edge-stable", "vivaldi-stable"))
    return {"path": str(entry), "app_window": browser}


def remove_desktop(root: Path) -> bool:
    entry = desktop_path()
    if owns_desktop(root):
        entry.unlink()
        _refresh_menu(entry.parent)
        return True
    return False


def _refresh_menu(folder: Path) -> None:
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(folder)], capture_output=True, timeout=30, check=False)


def remove_all(root: Path) -> list[str]:
    """Uninstall: drop the terminal command and the menu entry if they belong to this install."""
    removed = []
    if remove_command(root):
        removed.append(str(command_path()))
    if remove_desktop(root):
        removed.append(str(desktop_path()))
    return removed
