"""Site-wide themes: one palette per theme, mapped onto every studio's own CSS
variable names, served as /theme.css and injected into each studio's HTML by
the proxy (see proxy.py). The choice is stored server-side so phone and
desktop match.

Every studio styles itself through :root custom properties, so overriding
those re-skins it without touching the studio. Studios also have some
hard-coded dark colours, which is why every theme here is a dark theme."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

# Palette keys: bg, panel (cards/sidebars), soft (inputs, insets), raised
# (buttons, hovers), border, text, muted, faint, accent, accent2 (gradient
# end), violet, green, amber, red.
THEMES: dict[str, dict[str, Any]] = {
    "default": {"name": "Studio default", "note": "Each studio keeps its own built-in look", "palette": None,
                "swatch": ["#0f1418", "#182128", "#79add3", "#7fbd54"]},
    "bunnybytes": {"name": "Rocsy", "note": "One shared slate-blue look everywhere", "palette": {
        "bg": "#0f1418", "panel": "#182128", "soft": "#12191e", "raised": "#1f2a32", "border": "#33414c",
        "text": "#c7d0d6", "muted": "#7f8c96", "faint": "#5c6a73", "accent": "#79add3", "accent2": "#5b8fd4",
        "violet": "#9b86c4", "green": "#7fbd54", "amber": "#e1a744", "red": "#c96a6a"}},
    "oled": {"name": "OLED Black", "note": "True black, easy on phone batteries", "palette": {
        "bg": "#000000", "panel": "#0b0b0d", "soft": "#050506", "raised": "#16161a", "border": "#26262c",
        "text": "#e6e6ea", "muted": "#8b8b95", "faint": "#5f5f68", "accent": "#8ab4f8", "accent2": "#5d8de0",
        "violet": "#b39ddb", "green": "#81c784", "amber": "#ffb74d", "red": "#ef6f6f"}},
    "nord": {"name": "Nord", "note": "Cool arctic greys and frost blue", "palette": {
        "bg": "#242933", "panel": "#2e3440", "soft": "#272c36", "raised": "#3b4252", "border": "#434c5e",
        "text": "#e5e9f0", "muted": "#a3abbb", "faint": "#7b8394", "accent": "#88c0d0", "accent2": "#5e81ac",
        "violet": "#b48ead", "green": "#a3be8c", "amber": "#ebcb8b", "red": "#bf616a"}},
    "dracula": {"name": "Dracula", "note": "Purple night with neon accents", "palette": {
        "bg": "#1e1f29", "panel": "#282a36", "soft": "#21222c", "raised": "#343746", "border": "#44475a",
        "text": "#f0f0ea", "muted": "#a4a8c4", "faint": "#7178a0", "accent": "#bd93f9", "accent2": "#ff79c6",
        "violet": "#bd93f9", "green": "#50fa7b", "amber": "#ffb86c", "red": "#ff5555"}},
    "mocha": {"name": "Catppuccin Mocha", "note": "Soft pastels on warm dark", "palette": {
        "bg": "#11111b", "panel": "#1e1e2e", "soft": "#181825", "raised": "#313244", "border": "#45475a",
        "text": "#cdd6f4", "muted": "#a6adc8", "faint": "#7f849c", "accent": "#89b4fa", "accent2": "#cba6f7",
        "violet": "#cba6f7", "green": "#a6e3a1", "amber": "#f9e2af", "red": "#f38ba8"}},
    "forest": {"name": "Forest", "note": "Deep greens with a moss accent", "palette": {
        "bg": "#0c130f", "panel": "#142019", "soft": "#101a14", "raised": "#1c2b22", "border": "#2c4033",
        "text": "#d6e4d9", "muted": "#8fa596", "faint": "#637a6b", "accent": "#7fd3a0", "accent2": "#3fa36b",
        "violet": "#a7a0e0", "green": "#8fd46b", "amber": "#e6c069", "red": "#e07a6f"}},
    "rose": {"name": "Rose", "note": "Plum dark with a pink accent", "palette": {
        "bg": "#140e13", "panel": "#1f151d", "soft": "#191118", "raised": "#2a1d27", "border": "#3f2c3a",
        "text": "#eedbe6", "muted": "#ad91a3", "faint": "#7f6575", "accent": "#f28dbb", "accent2": "#c76bd6",
        "violet": "#c79bf0", "green": "#9fd38a", "amber": "#f0b871", "red": "#ef6f7f"}},
    "ember": {"name": "Ember", "note": "Charcoal with a warm orange glow", "palette": {
        "bg": "#121010", "panel": "#1c1918", "soft": "#171413", "raised": "#28221f", "border": "#3d342f",
        "text": "#ece2da", "muted": "#a8988b", "faint": "#7a6c61", "accent": "#f2a65a", "accent2": "#e0603f",
        "violet": "#b99be0", "green": "#9ccc6e", "amber": "#f2c45a", "red": "#e5675a"}},
}
for _theme in THEMES.values():
    if _theme["palette"]:
        _p = _theme["palette"]
        _theme["swatch"] = [_p["bg"], _p["panel"], _p["accent"], _p["green"]]

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
DATA_SAVER_MODES = ("auto", "phone", "off")
# Home page modules (Settings -> Modules). Hiding one only takes it off the
# home page; its URL keeps working. Settings itself can't be hidden. "tag" = a pill next to the
# name (home cards carry their own "experimental" flag in templates/home.html).
MODULES: dict[str, dict[str, str]] = {
    "Agent": {"group": "Tools", "note": "Master Agent (agentic mode) and the home prompt box"},
    "Vid": {"group": "Create", "label": "Video", "note": "Video generation (H3 Machine)"},
    "Krea": {"group": "Create", "label": "Image", "note": "Images with Krea 2, Lustify and Qwen Image"},
    "Mus": {"group": "Create", "label": "Music", "note": "Music generation"},
    "Voice": {"group": "Create", "note": "Local text to speech"},
    "Code": {"group": "Create", "label": "Vibe Coding", "note": "Coding projects", "tag": "Experimental"},
    "Trainer": {"group": "Create", "note": "LoRA training"},
    "Queue": {"group": "Tools", "note": "Shared GPU queue"},
    "Models": {"group": "Tools", "note": "Download models and LoRAs, hardware fit, updates"},
    "REA": {"group": "Tools", "note": "Reverse Engineer Anything: look inside programs with Ghidra", "tag": "Experimental"},
    "Llama": {"group": "Tools", "note": "Local LLM inference"},
    "Connections": {"group": "Tools", "note": "Websites, Telegram and tunnels"},
    "Hermes": {"group": "Tools", "note": "Remote agent desktop and chat"},
    "Cache": {"group": "Tools", "note": "RAG library for the Master Agent (pinned files, groups, trigger terms) and the 7-day upload cache"},
}
DEFAULT_HIDDEN: list[str] = []
GROUPS = ("Create", "Tools")
DATASET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")  # Trainer's dataset folder names
DEFAULTS = {"theme": "default", "accent": "", "hidden": DEFAULT_HIDDEN, "order": list(MODULES), "groups": {},
            "scanner_block": True, "data_saver": "auto", "trainer_art_exclude": []}


def _rgba(hex_color: str, alpha: float) -> str:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, {alpha})"


def _mix(a: str, b: str, t: float) -> str:
    """a blended toward b by t (0..1)."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


STUDIO_APPS = {"Vid", "Mus", "Krea", "Trainer", "Queue", "Connections", "Agent", "Voice", "Models", "Cache", "REA"}
APPS = STUDIO_APPS | {"home", "Llama", "Hermes"}


def _studio_vars(p: dict[str, str]) -> dict[str, str]:
    """Each studio's own :root names. The proxy's rewrite (restyle.py) also
    points their definitions at --t-*; these add the in-between shades."""
    return {
        # BunnyBytes home / login / settings
        "--bb-bg": p["bg"], "--bb-panel": p["panel"], "--bb-panel-alt": p["soft"], "--bb-border": p["border"],
        "--bb-text": p["text"], "--bb-muted": p["muted"], "--bb-blue": p["accent"], "--bb-green": p["green"],
        "--bb-amber": p["amber"], "--bb-red": p["red"], "--bb-purple": p["violet"], "--bb-teal": _mix(p["accent"], p["green"], 0.5),
        # shared names (Vid, Krea, Mus, Trainer, Queue)
        "--bg": p["bg"], "--panel": p["panel"], "--border": p["border"], "--text": p["text"],
        "--muted": p["muted"], "--faint": p["faint"], "--green": p["green"], "--amber": p["amber"], "--red": p["red"],
        # Vid (H3 Machine)
        "--card": _mix(p["panel"], p["raised"], 0.4), "--field": p["soft"], "--raised": p["raised"],
        "--raised-hover": _mix(p["raised"], p["text"], 0.07), "--border-strong": _mix(p["border"], p["text"], 0.12),
        "--blue": p["accent"], "--blue-deep": p["accent2"], "--violet": p["violet"],
        "--focus": f"0 0 0 3px {_rgba(p['accent'], 0.28)}",
        "--accent-gradient": f"linear-gradient(135deg, {_mix(p['accent'], '#ffffff', 0.1)} 0%, {p['accent2']} 100%)",
        # Krea / Trainer / Mus / Queue
        "--panel-alt": p["soft"], "--panel2": p["soft"], "--purple": p["violet"],
        "--panel-soft": p["soft"], "--panel-raised": p["raised"], "--border-soft": _mix(p["border"], p["bg"], 0.3),
        "--mint": p["accent"], "--mint-dark": _mix(p["accent"], p["bg"], 0.78),
    }


def _llama_vars(p: dict[str, str]) -> dict[str, str]:
    """llama-swap's UI (shadcn tokens; its own light/dark + colour themes)."""
    if "bg" not in p:  # accent only
        return {"--primary": p["accent"], "--ring": p["accent"], "--sidebar-primary": p["accent"], "--sidebar-ring": p["accent"]}
    return {
        "--background": p["bg"], "--foreground": p["text"], "--card": p["panel"], "--card-foreground": p["text"],
        "--popover": p["panel"], "--popover-foreground": p["text"], "--primary": p["accent"], "--primary-foreground": p["bg"],
        "--secondary": p["raised"], "--secondary-foreground": p["text"], "--muted": p["raised"], "--muted-foreground": p["muted"],
        "--accent": p["raised"], "--accent-foreground": p["text"], "--destructive": p["red"], "--border": p["border"],
        "--input": p["border"], "--ring": p["accent"], "--sidebar": p["soft"], "--sidebar-foreground": p["text"],
        "--sidebar-primary": p["accent"], "--sidebar-primary-foreground": p["bg"], "--sidebar-accent": p["raised"],
        "--sidebar-accent-foreground": p["text"], "--sidebar-border": p["border"], "--sidebar-ring": p["accent"],
    }


def _hermes_vars(p: dict[str, str]) -> dict[str, str]:
    """Hermes: background + one "midground" ink colour (text, borders at alpha)."""
    if "bg" not in p:
        return {}
    return {"--background": p["bg"], "--background-base": p["bg"], "--midground": p["text"], "--midground-base": p["text"]}


def build_css(theme_id: str, accent: str = "", app: str = "home") -> str:
    """The stylesheet for one theme (+ optional accent override) as one app
    sees it. Studio default with no accent overrides nothing."""
    theme = THEMES.get(theme_id) or THEMES["default"]
    app = app if app in APPS else "home"
    palette = dict(theme["palette"] or {})
    if accent and HEX.match(accent):
        palette["accent"] = accent
        palette["accent2"] = _mix(accent, "#000000", 0.25)
    head = f"/* BunnyBytes theme: {theme_id}{' + accent ' + accent if accent else ''} for {app} */\n"
    if not palette:
        return head
    if app == "Llama":
        # Beats llama-swap's own [data-theme=x].dark blocks (two classes/attributes).
        body = "\n".join(f"  {key}: {value};" for key, value in _llama_vars(palette).items())
        return head + f":root:root:root, :root:root:root.dark {{\n{body}\n}}\n"
    if app == "Hermes":
        variables = _hermes_vars(palette)
        if not variables:
            return head
        # Hermes writes its theme as an inline style on <html>; only !important beats that.
        body = "\n".join(f"  {key}: {value} !important;" for key, value in variables.items())
        return head + f":root:root:root {{\n{body}\n}}\n"
    # Studios + BunnyBytes pages: the --t-* role tokens the rewritten studio CSS reads, plus their own names.
    tokens = {f"--t-{role}": value for role, value in palette.items()}
    variables = {**tokens, **(_studio_vars(palette) if "bg" in palette else {
        "--bb-blue": palette["accent"], "--blue": palette["accent"], "--mint": palette["accent"], "--blue-deep": palette["accent2"],
        "--focus": f"0 0 0 3px {_rgba(palette['accent'], 0.28)}",
        "--accent-gradient": f"linear-gradient(135deg, {_mix(palette['accent'], '#ffffff', 0.1)} 0%, {palette['accent2']} 100%)"})}
    body = "\n".join(f"  {key}: {value};" for key, value in variables.items())
    # :root:root outranks each studio's own :root block wherever the link lands.
    return (head + f":root:root {{\n  color-scheme: dark;\n{body}\n}}\n"
            + (f"html, body {{ background-color: {palette['bg']}; }}\n" if "bg" in palette else "")
            # Krea hard-codes its Generate button / ETA gradient instead of using a variable.
            + ":root .primary-button, :root .eta-value { background-image: var(--accent-gradient); }\n"
            + f"::selection {{ background: {_rgba(palette['accent'], 0.35)}; }}\n")


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        # Read on every proxied studio page (the theme link), so parsed once
        # per file version rather than per request.
        self._cached: tuple[tuple[int, int], dict[str, Any]] | None = None

    def load(self) -> dict[str, Any]:
        try:
            stat = self.path.stat()
            version = (stat.st_mtime_ns, stat.st_size)
            if self._cached is None or self._cached[0] != version:
                self._cached = (version, clean({**DEFAULTS, **json.loads(self.path.read_text())}))
        except (OSError, ValueError):
            return copy.deepcopy(DEFAULTS)
        return copy.deepcopy(self._cached[1])

    def save(self, values: dict[str, Any]) -> dict[str, Any]:
        data = clean({**self.load(), **values})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-")
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, self.path)
        return data


def clean(values: dict[str, Any]) -> dict[str, Any]:
    theme = str(values.get("theme") or "default")
    accent = str(values.get("accent") or "")
    hidden = values.get("hidden")
    hidden = [name for name in MODULES if name in hidden] if isinstance(hidden, list) else list(DEFAULT_HIDDEN)
    # Home card order (drag and drop in Settings -> Modules): known names once, new modules appended.
    order = values.get("order")
    order = list(dict.fromkeys(name for name in order if name in MODULES)) if isinstance(order, list) else []
    # Section per module (dragged between Create and Tools); only differences from MODULES are kept.
    groups = values.get("groups")
    groups = {name: group for name, group in groups.items()
              if name in MODULES and group in GROUPS and group != MODULES[name]["group"]} if isinstance(groups, dict) else {}
    # New modules go at the front of their section, so a new service is seen.
    for name in MODULES:
        if name not in order:
            group = groups.get(name, MODULES[name]["group"])
            first = next((i for i, other in enumerate(order) if groups.get(other, MODULES[other]["group"]) == group), len(order))
            order.insert(first, name)
    return {"theme": theme if theme in THEMES else "default", "accent": accent.lower() if HEX.match(accent) else "",
            "hidden": hidden, "order": order, "groups": groups,
            # Settings -> Security -> Connections: auto-block vulnerability scanners.
            "scanner_block": values.get("scanner_block") is not False,
            # Settings -> Appearance -> Mobile data (static/js/data-saver.js decides per device).
            "data_saver": values.get("data_saver") if values.get("data_saver") in DATA_SAVER_MODES else "auto",
            # Settings -> Modules -> Trainer card pictures: datasets kept off the home page.
            "trainer_art_exclude": sorted({name for name in values.get("trainer_art_exclude") or []
                                           if isinstance(name, str) and DATASET_NAME.fullmatch(name)})}


def home_sections(values: dict[str, Any]) -> dict[str, list[str]]:
    """Visible modules per home section, in the saved order."""
    groups = values.get("groups") or {}
    return {group.lower(): [name for name in values["order"]
                            if groups.get(name, MODULES[name]["group"]) == group and name not in values["hidden"]]
            for group in GROUPS}


def module_catalog() -> list[dict[str, str]]:
    return [{"id": name, **spec} for name, spec in MODULES.items()]


def catalog() -> list[dict[str, Any]]:
    return [{"id": key, "name": theme["name"], "note": theme["note"], "swatch": theme["swatch"]}
            for key, theme in THEMES.items()]
