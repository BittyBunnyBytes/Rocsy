"""VS Code colour themes for Settings -> Appearance -> Theme.

A VS Code theme is a JSON(C) file with a `colors` map (workbench colours) and
`tokenColors` (syntax). Rocsy needs only the 14-colour palette themes.py uses
(bg, panel, soft, raised, border, text, muted, faint, accent, accent2, violet,
green, amber, red), so each palette role is read from the workbench keys that
play the same part in VS Code, with syntax and terminal colours as fallbacks.

Sources: a theme .json/.jsonc, a .vsix (an extension; it may contain several
themes), or an extension ID / Marketplace or Open VSX link, downloaded from
Open VSX first and the VS Code Marketplace second."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import posixpath
import re
import zipfile
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

PALETTE_KEYS = ("bg", "panel", "soft", "raised", "border", "text", "muted", "faint",
                "accent", "accent2", "violet", "green", "amber", "red")
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_VSIX_BYTES = 60 * 1024 * 1024
MAX_THEMES_PER_IMPORT = 24
EXTENSION_ID = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]{0,99})\.([A-Za-z0-9][A-Za-z0-9._-]{0,99})$")
_COLOR = re.compile(r"^#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


class ThemeError(ValueError):
    """A message for the person importing the theme."""


# ---------------------------------------------------------------- JSONC

def loads_jsonc(text: str) -> Any:
    """JSON with comments and trailing commas (how VS Code themes are written)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(ch)
            i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(cleaned.lstrip("﻿"))


# ---------------------------------------------------------------- colours

def _rgb(hex6: str) -> tuple[int, int, int]:
    return int(hex6[1:3], 16), int(hex6[3:5], 16), int(hex6[5:7], 16)


def _hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v))):02x}" for v in rgb)


def mix(a: str, b: str, t: float) -> str:
    return _hex(x + (y - x) * t for x, y in zip(_rgb(a), _rgb(b)))


def color(value: Any, over: str) -> str | None:
    """#rgb / #rgba / #rrggbb / #rrggbbaa as #rrggbb, alpha blended over `over`."""
    if not isinstance(value, str) or not _COLOR.match(value.strip()):
        return None
    digits = value.strip()[1:]
    if len(digits) in (3, 4):
        digits = "".join(c * 2 for c in digits)
    rgb = "#" + digits[:6].lower()
    if len(digits) == 8:
        alpha = int(digits[6:8], 16) / 255
        if alpha < 0.05:
            return None  # fully transparent: VS Code treats it as "not set"
        rgb = mix(over, rgb, alpha)
    return rgb


def luminance(hex6: str) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in _rgb(hex6))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# ---------------------------------------------------------------- mapping

def _token(token_colors: Any, prefixes: tuple[str, ...], bg: str) -> str | None:
    """The foreground of the first tokenColors rule whose scope starts with one of `prefixes`."""
    if not isinstance(token_colors, list):
        return None
    for prefix in prefixes:
        for rule in token_colors:
            if not isinstance(rule, dict):
                continue
            scopes = rule.get("scope")
            if isinstance(scopes, str):
                scopes = [s.strip() for s in scopes.split(",")]
            if not isinstance(scopes, list):
                continue
            if any(isinstance(s, str) and s.split(" ")[0].startswith(prefix) for s in scopes):
                found = color((rule.get("settings") or {}).get("foreground"), bg)
                if found:
                    return found
    return None


def to_palette(data: dict[str, Any], kind: str = "") -> tuple[dict[str, str], bool]:
    """(palette, light) for one parsed VS Code theme."""
    colors = data.get("colors") if isinstance(data.get("colors"), dict) else {}
    tokens = data.get("tokenColors")
    kind = (kind or str(data.get("type") or "")).lower()
    light_hint = kind in ("light", "vs", "hclight", "hc-light")
    bg = color(colors.get("editor.background"), "#000000") or ("#ffffff" if light_hint else "#1e1e1e")
    light = light_hint if kind else luminance(bg) > 0.4

    def pick(*keys: str, base: str | None = None) -> str | None:
        for key in keys:
            found = color(colors.get(key), base or bg)
            if found:
                return found
        return None

    text = pick("editor.foreground", "foreground") or ("#333333" if light else "#d4d4d4")
    if contrast(text, bg) < 4:
        text = "#1f1f1f" if light else "#e6e6e6"

    panel = pick("sideBar.background", "activityBar.background", "editorGroupHeader.tabsBackground",
                 "panel.background") or bg
    if panel == bg:
        panel = mix(bg, text, 0.045)
    soft = pick("input.background", "editorWidget.background", "dropdown.background", "sideBarSectionHeader.background") or bg
    if soft in (bg, panel):
        soft = mix(bg, "#000000", 0.18) if not light else mix(bg, text, 0.03)
    raised = pick("button.secondaryBackground", "list.hoverBackground", "tab.inactiveBackground",
                  "editor.lineHighlightBackground", base=panel)
    if not raised or raised in (bg, panel) or contrast(raised, panel) < 1.08:
        raised = mix(panel, text, 0.08)
    border = pick("panel.border", "sideBar.border", "editorGroup.border", "editorWidget.border",
                  "input.border", "contrastBorder")
    if not border or contrast(border, bg) < 1.25:
        border = mix(bg, text, 0.18)
    muted = pick("descriptionForeground", "tab.inactiveForeground", "sideBarTitle.foreground")
    if not muted or contrast(muted, bg) < 2.5 or muted == text:
        muted = mix(text, bg, 0.38)
    faint = pick("editorLineNumber.foreground", "disabledForeground")
    if not faint or contrast(faint, bg) < 1.6:
        faint = mix(text, bg, 0.58)

    # Accent: the colour the theme uses to say "this one" (badges, primary buttons, links, focus).
    candidates = [pick(key) for key in ("activityBarBadge.background", "button.background", "textLink.foreground",
                                        "focusBorder", "progressBar.background", "tab.activeBorder",
                                        "activityBar.activeBorder")]
    candidates.append(_token(tokens, ("keyword", "storage", "entity.name.function"), bg))
    # Prefer a real colour: some themes use a white/grey badge or button.
    def saturated(c: str) -> bool:
        return max(_rgb(c)) - min(_rgb(c)) >= 40
    accent = next((c for c in candidates if c and saturated(c) and contrast(c, bg) >= 3), None) \
        or next((c for c in candidates if c and contrast(c, bg) >= 3), None)
    if not accent:
        accent = next((c for c in candidates if c), "#3794ff")
        for _ in range(10):
            if contrast(accent, bg) >= 3:
                break
            accent = mix(accent, text, 0.2)
    accent2 = mix(accent, "#000000", 0.25)

    def role(term_key: str, extra: tuple[str, ...], scopes: tuple[str, ...], fallback: str) -> str:
        found = pick(term_key, term_key.replace("ansi", "ansiBright"), *extra) or _token(tokens, scopes, bg) or fallback
        for _ in range(10):
            if contrast(found, bg) >= 2.6:
                break
            found = mix(found, text, 0.2)
        return found

    palette = {
        "bg": bg, "panel": panel, "soft": soft, "raised": raised, "border": border,
        "text": text, "muted": muted, "faint": faint, "accent": accent, "accent2": accent2,
        "violet": role("terminal.ansiMagenta", (), ("keyword", "storage"), "#c586c0" if not light else "#af00db"),
        "green": role("terminal.ansiGreen", ("gitDecoration.addedResourceForeground",), ("string",),
                      "#89d185" if not light else "#388a34"),
        "amber": role("terminal.ansiYellow", ("editorWarning.foreground", "list.warningForeground"),
                      ("constant.numeric", "constant"), "#cca700" if not light else "#bf8803"),
        "red": role("terminal.ansiRed", ("errorForeground", "editorError.foreground"), ("invalid",),
                    "#f14c4c" if not light else "#e51400"),
    }
    return palette, light


def _entry(data: dict[str, Any], name: str, kind: str = "", source: str = "") -> dict[str, Any]:
    palette, light = to_palette(data, kind)
    name = (str(name or data.get("name") or "VS Code theme").strip() or "VS Code theme")[:80]
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "theme"
    digest = hashlib.sha1(json.dumps(palette, sort_keys=True).encode()).hexdigest()[:6]
    note = "VS Code" + (f" · {source}" if source else "") + (" · light" if light else "")
    return {"id": f"vsc-{slug}-{digest}", "name": name, "note": note[:120], "palette": palette, "light": light}


# ---------------------------------------------------------------- sources

def from_json(raw: bytes | str, name: str = "") -> list[dict[str, Any]]:
    if isinstance(raw, bytes):
        if len(raw) > MAX_JSON_BYTES:
            raise ThemeError("That theme file is too big")
        raw = raw.decode("utf-8-sig", errors="replace")
    try:
        data = loads_jsonc(raw)
    except ValueError:
        raise ThemeError("That isn't a VS Code theme (the JSON could not be read)") from None
    if not isinstance(data, dict):
        raise ThemeError("That isn't a VS Code theme")
    if isinstance(data.get("contributes"), dict):
        raise ThemeError("That is an extension's package.json; upload the .vsix, or use its extension ID")
    if not isinstance(data.get("colors"), dict) and not isinstance(data.get("tokenColors"), list):
        raise ThemeError("That JSON has no VS Code theme colours (`colors` / `tokenColors`)")
    return [_entry(data, data.get("name") or name)]


def _merged(archive: zipfile.ZipFile, path: str, depth: int = 0) -> dict[str, Any]:
    """A theme file from the .vsix with its `include` chain merged underneath it."""
    try:
        info = archive.getinfo(path)
    except KeyError:
        raise ThemeError(f"The extension lists a theme file it doesn't contain ({posixpath.basename(path)})") from None
    if info.file_size > MAX_JSON_BYTES:
        raise ThemeError("A theme file in that extension is too big")
    data = loads_jsonc(archive.read(info).decode("utf-8-sig", errors="replace"))
    if not isinstance(data, dict):
        return {}
    include = data.get("include")
    if isinstance(include, str) and include.endswith((".json", ".jsonc")) and depth < 5:
        base = _merged(archive, posixpath.normpath(posixpath.join(posixpath.dirname(path), include)), depth + 1)
        colors = {**(base.get("colors") or {}), **(data.get("colors") or {})}
        tokens = [*(base.get("tokenColors") if isinstance(base.get("tokenColors"), list) else []),
                  *(data.get("tokenColors") if isinstance(data.get("tokenColors"), list) else [])]
        data = {**base, **data, "colors": colors, "tokenColors": tokens}
    return data


def from_vsix(raw: bytes) -> list[dict[str, Any]]:
    if len(raw) > MAX_VSIX_BYTES:
        raise ThemeError("That extension is too big")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ThemeError("That isn't a .vsix (could not open the archive)") from None
    with archive:
        try:
            package = loads_jsonc(archive.read("extension/package.json").decode("utf-8-sig", errors="replace"))
        except (KeyError, ValueError):
            raise ThemeError("That .vsix has no readable extension/package.json") from None
        contributed = ((package.get("contributes") or {}).get("themes") or []) if isinstance(package, dict) else []
        if not contributed:
            raise ThemeError("That extension has no colour themes (it may be an icon theme or another kind of extension)")
        source = str(package.get("displayName") or package.get("name") or "")[:60]
        if source.startswith("%"):
            source = str(package.get("name") or "")[:60]
        found = []
        for item in contributed[:MAX_THEMES_PER_IMPORT]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            path = posixpath.normpath(posixpath.join("extension", item["path"]))
            if not path.startswith("extension/"):
                continue
            try:
                data = _merged(archive, path)
            except ValueError:
                continue
            label = str(item.get("label") or item.get("id") or data.get("name") or source)
            if label.startswith("%"):
                label = str(data.get("name") or item.get("id") or source)
            found.append(_entry(data, label, str(item.get("uiTheme") or ""), source))
        if not found:
            raise ThemeError("No theme in that extension could be read")
        return found


def from_upload(raw: bytes, filename: str = "") -> list[dict[str, Any]]:
    if raw[:2] == b"PK" or filename.lower().endswith(".vsix"):
        return from_vsix(raw)
    stem = posixpath.basename(filename).rsplit(".", 1)[0]
    stem = re.sub(r"[-_]?color[-_]?theme$", "", stem, flags=re.I).replace("-", " ").replace("_", " ").strip()
    return from_json(raw, stem.title() if stem else "")


def extension_id(ref: str) -> tuple[str, str]:
    """publisher.name from an ID, a Marketplace link or an Open VSX link."""
    ref = ref.strip()
    if ref.startswith(("http://", "https://")):
        url = urlsplit(ref)
        host = url.hostname or ""
        if host.endswith("marketplace.visualstudio.com"):
            ref = (parse_qs(url.query).get("itemName") or [""])[0]
        elif host.endswith("open-vsx.org"):
            parts = [p for p in url.path.split("/") if p]
            if len(parts) >= 3 and parts[0] in ("extension", "api"):
                ref = f"{parts[1]}.{parts[2]}"
        else:
            raise ThemeError("Use an extension ID (publisher.name) or a VS Code Marketplace / Open VSX link")
    match = EXTENSION_ID.match(ref)
    if not match:
        raise ThemeError("That doesn't look like an extension ID (publisher.name, e.g. dracula-theme.theme-dracula)")
    return match.group(1), match.group(2)


async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    async with client.stream("GET", url, follow_redirects=True) as response:
        response.raise_for_status()
        chunks, size = [], 0
        async for chunk in response.aiter_raw():
            size += len(chunk)
            if size > MAX_VSIX_BYTES:
                raise ThemeError("That extension is too big")
            chunks.append(chunk)
    data = b"".join(chunks)
    # The Marketplace sends the .vsix gzip-compressed, with or without saying so.
    encoding = response.headers.get("content-encoding", "")
    if data[:2] == b"\x1f\x8b" or "gzip" in encoding:
        try:
            data = gzip.decompress(data)
        except OSError:
            pass
    return data


async def fetch_extension(client: httpx.AsyncClient, ref: str) -> list[dict[str, Any]]:
    publisher, name = extension_id(ref)
    errors = []
    try:
        meta = await client.get(f"https://open-vsx.org/api/{publisher}/{name}", follow_redirects=True)
        if meta.status_code == 200:
            download = ((meta.json().get("files") or {}).get("download") or "")
            if download.startswith("https://"):
                return from_vsix(await _download(client, download))
        errors.append(f"Open VSX: {meta.status_code}")
    except ThemeError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        errors.append(f"Open VSX: {type(exc).__name__}")
    try:
        url = (f"https://marketplace.visualstudio.com/_apis/public/gallery/publishers/{publisher}"
               f"/vsextensions/{name}/latest/vspackage")
        return from_vsix(await _download(client, url))
    except ThemeError:
        raise
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ThemeError(f"No extension called {publisher}.{name} on Open VSX or the VS Code Marketplace") from None
        errors.append(f"Marketplace: {exc.response.status_code}")
    except httpx.HTTPError as exc:
        errors.append(f"Marketplace: {type(exc).__name__}")
    raise ThemeError("Couldn't download that extension (" + "; ".join(errors) + ")")


def clean_custom(value: Any, limit: int = 40) -> dict[str, dict[str, Any]]:
    """Saved imported themes, validated (settings.json is read on every page)."""
    if not isinstance(value, dict):
        return {}
    kept: dict[str, dict[str, Any]] = {}
    for theme_id, theme in value.items():
        if len(kept) >= limit:
            break
        if not (isinstance(theme_id, str) and re.fullmatch(r"vsc-[a-z0-9-]{1,48}-[0-9a-f]{6}", theme_id)
                and isinstance(theme, dict) and isinstance(theme.get("palette"), dict)):
            continue
        palette = theme["palette"]
        if not all(isinstance(palette.get(k), str) and re.fullmatch(r"#[0-9a-f]{6}", palette[k]) for k in PALETTE_KEYS):
            continue
        kept[theme_id] = {"name": str(theme.get("name") or "VS Code theme")[:80],
                          "note": str(theme.get("note") or "VS Code")[:120],
                          "palette": {k: palette[k] for k in PALETTE_KEYS}, "light": theme.get("light") is True}
    return kept
