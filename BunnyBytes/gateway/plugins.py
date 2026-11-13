"""Custom services (plugins). Every folder in ROCSY_ROOT/core-backend-extras/plugins/ that holds a
plugin.json becomes a Rocsy service: the gateway proxies /<path>/ to its
upstream behind the Rocsy login, themes it, gives it the shared top bar and an
icon, and (optionally) a home card. plugins/README.md documents the manifest.

Plugins are this machine's own: the folder is git-ignored and left out of
compiled installers, so nothing in here may name a particular plugin."""
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import plugin_updates

log = logging.getLogger("bunnybytes.plugins")

PLUGIN_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,23}$")
PLUGIN_PATH = re.compile(r"^/[A-Za-z][A-Za-z0-9_-]{0,31}$")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
# Icons are drawn into Rocsy's own pages, so only plain SVG shapes get through.
_ICON_TAG = re.compile(r"<\s*/?\s*([A-Za-z]+)")
_ICON_TAGS = {"path", "circle", "rect", "ellipse", "line", "polyline", "polygon", "g"}
_ICON_BAD = re.compile(r"\bon[a-z]+\s*=|javascript:|href|url\(", re.I)
DEFAULT_ICON = '<rect x="4" y="4" width="16" height="16" rx="3.5"/><path d="M9 12h6M12 9v6"/>'
DEFAULT_COLOR = "#a3b0bd"
GROUPS = ("Create", "Tools")
# A plugin may ship its own icon picture (plugin.json "icon_file") instead of SVG shapes.
ICON_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}\.(png|svg|webp|jpe?g)$", re.I)
ICON_TYPES = {"png": "image/png", "svg": "image/svg+xml", "webp": "image/webp", "jpg": "image/jpeg", "jpeg": "image/jpeg"}
ICON_MAX = 512 * 1024
# Home-card backdrop: nothing, Rocsy's newest Image / Video output, whichever is newer, or the
# plugin's own pictures from home.background_url (JSON {"pictures": [...], "slideshow": bool}).
BACKGROUNDS = ("none", "image", "video", "recent", "plugin")
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*$")


def sniff_icon(data: bytes) -> str | None:
    """The picture type from the bytes themselves (never trust a file name alone)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:2048].lstrip().lower()
    if (head.startswith(b"<svg") or head.startswith(b"<?xml")) and b"<svg" in data[:4096].lower():
        return "svg"
    return None


@dataclass
class Plugin:
    id: str
    name: str
    path: str
    upstream: str
    folder: str
    strip_prefix: bool = False
    themed: bool = True
    icon: str = DEFAULT_ICON
    color: str = DEFAULT_COLOR
    health: str = ""
    home: dict | None = None
    gpu_queue: bool = False
    note: str = ""
    container: str = ""
    icon_file: str = ""
    icon_version: str = ""
    version: str = ""
    updates: dict | None = None
    # An app that only works at the site root (absolute /api, /scripts... URLs, no base-path
    # setting): the gateway adds `path` to its fetches, module imports and redirects and serves
    # root-path requests made from its pages (see gateway app.py root_app_shim / root fallback).
    root_app: bool = False

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "path": self.path, "upstream": self.upstream, "folder": self.folder,
                "home": bool(self.home), "group": (self.home or {}).get("group", ""), "gpu_queue": self.gpu_queue,
                "health": self.health, "note": self.note, "color": self.color, "icon": self.icon,
                "container": self.container, "strip_prefix": self.strip_prefix, "themed": self.themed,
                "icon_url": self.icon_url, "background": (self.home or {}).get("background", "none"),
                "version": self.version, "updates": bool(self.updates), "root_app": self.root_app}

    @property
    def icon_url(self) -> str:
        """The plugin's own icon picture, versioned by content; "" when it draws SVG shapes."""
        return f"/api/plugins/{self.folder}/icon?v={self.icon_version}" if self.icon_file else ""


def _clean_icon(icon: object) -> str:
    if not isinstance(icon, str) or not icon.strip():
        return DEFAULT_ICON
    tags = {tag.lower() for tag in _ICON_TAG.findall(icon)}
    if not tags or not tags <= _ICON_TAGS or _ICON_BAD.search(icon):
        raise ValueError("icon may only hold <path>, <circle>, <rect>, <ellipse>, <line>, <polyline>, <polygon> and <g>")
    return icon.strip()


def _parse(folder: Path, data: dict, taken_ids: set[str], taken_paths: set[str]) -> Plugin:
    plugin_id = str(data.get("id") or "")
    if not PLUGIN_ID.match(plugin_id):
        raise ValueError("id must be 2-24 letters/digits, starting with a letter")
    if plugin_id.lower() in {name.lower() for name in taken_ids}:
        raise ValueError(f"id {plugin_id!r} is already used by Rocsy or another plugin")
    path = str(data.get("path") or f"/{plugin_id}").rstrip("/")
    if not PLUGIN_PATH.match(path):
        raise ValueError("path must look like /Name (letters, digits, - and _)")
    if path.lower() in {taken.lower() for taken in taken_paths}:
        raise ValueError(f"path {path} is already used by Rocsy or another plugin")
    upstream = str(data.get("upstream") or "").rstrip("/")
    parts = urlsplit(upstream)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/") or parts.query:
        raise ValueError("upstream must be a base URL like http://127.0.0.1:8010")
    color = str(data.get("color") or DEFAULT_COLOR)
    if not HEX.match(color):
        raise ValueError("color must be a #rrggbb hex colour")
    health = str(data.get("health") or "")
    if health and not health.startswith("/"):
        raise ValueError("health must be a path on the upstream, like /Name/api/health")
    home = data.get("home")
    if home:
        home = home if isinstance(home, dict) else {}
        group = home.get("group", "Tools")
        if group not in GROUPS:
            raise ValueError(f"home.group must be one of {', '.join(GROUPS)}")
        background = str(home.get("background") or "none")
        if background not in BACKGROUNDS:
            raise ValueError(f"home.background must be one of {', '.join(BACKGROUNDS)}")
        background_url = str(home.get("background_url") or "")
        if background == "plugin" and not background_url:
            raise ValueError('home.background "plugin" needs home.background_url (a path on the upstream)')
        if background_url and (not _SAFE_PATH.match(background_url) or "//" in background_url or ".." in background_url):
            raise ValueError("home.background_url must be a path on the upstream, like /Name/api/home-art")
        home = {"group": group, "desc": str(home.get("desc") or data.get("note") or ""),
                "status": str(home.get("status") or home.get("desc") or data.get("note") or ""),
                "experimental": bool(home.get("experimental")), "background": background,
                "background_url": background_url, "slideshow": bool(home.get("slideshow"))}
    version = data.get("version")
    if version not in (None, "") and not (isinstance(version, (str, int, float)) and len(str(version)) <= 40):
        raise ValueError("version must be a short text like 1.2.0")
    updates = plugin_updates.clean(data.get("updates"))
    root_app = data.get("root_app", False)
    if not isinstance(root_app, bool):
        raise ValueError("root_app must be true or false")
    if root_app and not data.get("strip_prefix"):
        raise ValueError("root_app needs strip_prefix: true (the app is served from its own root)")
    icon_file, icon_version = str(data.get("icon_file") or ""), ""
    if icon_file:
        if not ICON_FILE.match(icon_file):
            raise ValueError("icon_file must be a .png, .svg, .webp or .jpg file name in the plugin's folder")
        picture = folder / icon_file
        try:
            blob = picture.read_bytes()
        except OSError:
            raise ValueError(f"icon_file {icon_file} isn't in the plugin's folder") from None
        if len(blob) > ICON_MAX:
            raise ValueError("icon_file is over 512 KB")
        if sniff_icon(blob) is None:
            raise ValueError("icon_file isn't a PNG, SVG, WebP or JPEG picture")
        icon_version = hashlib.sha256(blob).hexdigest()[:10]
    return Plugin(id=plugin_id, name=str(data.get("name") or plugin_id)[:40], path=path, upstream=upstream,
                  folder=folder.name, strip_prefix=bool(data.get("strip_prefix")), themed=data.get("themed") is not False,
                  icon=_clean_icon(data.get("icon")), color=color.lower(), health=health, home=home or None,
                  gpu_queue=bool(data.get("gpu_queue")), note=str(data.get("note") or "")[:200],
                  container=str(data.get("container") or "")[:63], icon_file=icon_file, icon_version=icon_version,
                  version=str(version or ""), updates=updates, root_app=root_app)


def load(root: Path, reserved_ids: set[str], reserved_paths: set[str]) -> tuple[list[Plugin], list[dict]]:
    """Every valid plugin under root, plus one {folder, error} per broken one.
    A folder without plugin.json (or with "enabled": false) is skipped quietly."""
    plugins: list[Plugin] = []
    errors: list[dict] = []
    if not root.is_dir():
        return plugins, errors
    taken_ids, taken_paths = set(reserved_ids), set(reserved_paths)
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        manifest = folder / "plugin.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("plugin.json must be a JSON object")
            if data.get("enabled") is False:
                continue
            plugin = _parse(folder, data, taken_ids, taken_paths)
        except (OSError, ValueError) as exc:
            log.warning("Plugin %s skipped: %s", folder.name, exc)
            errors.append({"folder": folder.name, "error": str(exc)})
            continue
        taken_ids.add(plugin.id)
        taken_paths.add(plugin.path)
        plugins.append(plugin)
        log.info("Plugin %s: %s -> %s", plugin.id, plugin.path, plugin.upstream)
    return plugins, errors


FOLDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def snapshot(root: Path) -> dict[str, str]:
    """Every plugin.json as text, to tell when one changed since the gateway started."""
    found: dict[str, str] = {}
    if root.is_dir():
        for manifest in sorted(root.glob("*/plugin.json")):
            if not manifest.parent.name.startswith((".", "_")):
                try:
                    found[manifest.parent.name] = manifest.read_text(encoding="utf-8")
                except OSError:
                    found[manifest.parent.name] = ""
    return found


def read_config(root: Path, folder: str) -> dict:
    if not FOLDER.match(folder) or folder.startswith((".", "_")):
        raise ValueError("unknown plugin folder")
    manifest = root / folder / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("plugin.json must be a JSON object")
    return data


def validate(root: Path, folder: str, data: dict, reserved_ids: set[str], reserved_paths: set[str]) -> Plugin:
    """Check an edited plugin.json the same way load() will, against Rocsy and the other plugins."""
    if not isinstance(data, dict):
        raise ValueError("plugin.json must be a JSON object")
    if "enabled" in data and not isinstance(data["enabled"], bool):
        raise ValueError("enabled must be true or false")
    container = data.get("container")
    if container not in (None, "") and not (isinstance(container, str)
                                            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", container)):
        raise ValueError("container must be a container name (letters, digits, _ . -)")
    taken_ids, taken_paths = set(reserved_ids), set(reserved_paths)
    if data.get("enabled") is not False:
        others = {k: v for k, v in snapshot(root).items() if k != folder}
        for text in others.values():
            try:
                other = json.loads(text)
            except ValueError:
                continue
            if isinstance(other, dict) and other.get("enabled") is not False and other.get("id"):
                taken_ids.add(str(other["id"]))
                taken_paths.add(str(other.get("path") or f"/{other['id']}").rstrip("/"))
    return _parse(root / folder, data, taken_ids, taken_paths)


def disabled(root: Path) -> list[dict]:
    """Plugins switched off with "enabled": false, for Core Backend -> Plugins (never routed)."""
    found: list[dict] = []
    if not root.is_dir():
        return found
    for manifest in sorted(root.glob("*/plugin.json")):
        if manifest.parent.name.startswith((".", "_")):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("enabled") is False:
            color = str(data.get("color") or "")
            try:
                icon = _clean_icon(data.get("icon"))
            except ValueError:
                icon = DEFAULT_ICON
            found.append({"folder": manifest.parent.name, "id": str(data.get("id") or "")[:24],
                          "name": str(data.get("name") or data.get("id") or manifest.parent.name)[:40],
                          "note": str(data.get("note") or "")[:200], "path": str(data.get("path") or "")[:33],
                          "color": color.lower() if HEX.match(color) else DEFAULT_COLOR, "icon": icon,
                          "icon_url": (f"/api/plugins/{manifest.parent.name}/icon"
                                       if ICON_FILE.match(str(data.get("icon_file") or "")) else "")})
    return found


def home_card(plugin: Plugin) -> dict:
    """The card data templates/home.html renders for built-in modules."""
    icon = (f'<g stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
            f'{plugin.icon}</g>')
    home = plugin.home or {}
    return {"href": f"{plugin.path}/", "color": plugin.color, "icon": icon, "icon_img": plugin.icon_url,
            "name": plugin.name, "live": False,
            "experimental": home.get("experimental", False), "desc": home.get("desc", ""), "status": home.get("status", ""),
            "plugin": True}


def icon_picture(root: Path, folder: str, name: str = "") -> tuple[bytes, str] | None:
    """The icon picture named in a plugin's plugin.json (or, for the Settings dialog's preview
    of a fresh upload, the named picture in its folder), read from disk now."""
    if not name:
        try:
            name = str(read_config(root, folder).get("icon_file") or "")
        except (OSError, ValueError):
            return None
    if not FOLDER.match(folder) or folder.startswith((".", "_")):
        return None
    if not ICON_FILE.match(name):
        return None
    try:
        blob = (root / folder / name).read_bytes()
    except OSError:
        return None
    kind = sniff_icon(blob)
    return (blob, ICON_TYPES[kind]) if kind and len(blob) <= ICON_MAX else None


def art_pictures(plugin_path: str, reply: object) -> tuple[list[str], bool]:
    """A plugin's backdrop reply, reduced to pictures it serves under its own Rocsy path."""
    pictures = reply.get("pictures") if isinstance(reply, dict) else reply if isinstance(reply, list) else []
    slideshow = bool(reply.get("slideshow")) if isinstance(reply, dict) else False
    keep = [src for src in pictures or [] if isinstance(src, str) and src.startswith(plugin_path + "/")
            and _SAFE_PATH.match(src) and "//" not in src and ".." not in src and len(src) < 600]
    return keep[:24], slideshow
