"""Per-service favicons: each service's top-bar glyph (topbar.APPS) drawn in
dark on its own coloured tile, so browser tabs tell the services apart. The
proxy swaps each studio's own <link rel="icon"> for this one; home and login
keep the Rocsy roc."""
import hashlib
import re

import topbar

# Tile colour per service (the glyph is drawn in the dark Rocsy ink on top).
COLORS: dict[str, str] = {
    "Vid": "#e8735f",
    "Krea": "#b484f2",
    "Mus": "#4fc7a3",
    "Voice": "#f08cb6",
    "Agent": "#e9a04f",
    "Trainer": "#84c96c",
    "Queue": "#5eaaf0",
    "Models": "#e9c84f",
    "REA": "#e57f8f",
    "Connections": "#4fcbdc",
    "Settings": "#a3b0bd",
    "Llama": "#d9a173",
    "Hermes": "#8f9cf2",
}

# Apps without a top-bar entry of their own.
_GLYPHS = {
    "Llama": topbar.APPS["Llama API"][1],
    "Hermes": '<path d="M4 5.5h16a1.5 1.5 0 0 1 1.5 1.5v9a1.5 1.5 0 0 1-1.5 1.5H10l-4.5 3.5V17.5H4A1.5 1.5 0 0 1 '
              '2.5 16V7A1.5 1.5 0 0 1 4 5.5z"/><path d="M7.5 11.5h.01M12 11.5h.01M16.5 11.5h.01"/>',
}
INK = "#141c22"


def glyph(app: str) -> str:
    return _GLYPHS.get(app) or topbar.APPS.get(app, ("", ""))[1]


def svg(app: str) -> str | None:
    if app not in COLORS or not glyph(app):
        return None
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 24 24">'
        f'<rect width="24" height="24" rx="5.5" fill="{COLORS[app]}"/>'
        f'<g transform="translate(2.4 2.4) scale(.8)" fill="none" stroke="{INK}" stroke-width="2.4" '
        f'stroke-linecap="round" stroke-linejoin="round">{glyph(app)}</g></svg>'
    )


def href(app: str) -> str:
    """Versioned by content: Cloudflare caches .svg for 4 h."""
    version = hashlib.sha256((svg(app) or "").encode()).hexdigest()[:10]
    return f"/app-icon/{app}.svg?v={version}"


def link(app: str) -> str:
    return f'<link rel="icon" type="image/svg+xml" href="{href(app)}" data-rb-icon>' if app in COLORS else ""


# The app's own icons (rel="icon" / "shortcut icon"; apple-touch-icon is left alone).
_OWN_ICON = re.compile(rb'<link\b(?=[^>]*\brel=["\']?(?:shortcut\s+)?icon["\'\s>])[^>]*>\s*', re.I)


def apply(html: bytes, app: str) -> bytes:
    """Replace the page's favicon links with this app's icon."""
    if app not in COLORS or b"data-rb-icon" in html:
        return html
    head_end = html.lower().find(b"</head>")
    if head_end < 0:
        return html
    head = _OWN_ICON.sub(b"", html[:head_end])
    return head + link(app).encode() + html[head_end:]
