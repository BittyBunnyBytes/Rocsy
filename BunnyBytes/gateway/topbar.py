"""One shared top bar for every service: the proxy finds each studio's own
header, tags it `rb-bar` and puts the same "Rocsy / <App>" crumb at its start
(the Rocsy part links home). static/css/rocsy-bar.css then gives all of those
headers one height, background, tab and status-chip style, and hides each
app's old brand / home link. Done here so the studios need no rebuild."""
import re
from html import escape

ROCSY_MARK = (
    '<svg viewBox="40 140 432 260" width="30" height="19" aria-hidden="true">'
    '<circle class="rb-mark-sun" cx="256" cy="268" r="124"/>'
    '<path class="rb-mark-bird" d="M256 186 C269 186 276 197 274 210 C320 182 380 162 464 156 C418 200 358 234 '
    '292 254 L308 314 L256 294 L204 314 L220 254 C154 234 94 200 48 156 C132 162 192 182 238 210 C236 197 243 186 '
    '256 186 Z"/></svg>'
)

# label, icon path(s) on a 24px grid (stroked with currentColor)
APPS: dict[str, tuple[str, str]] = {
    "Code": ("Vibe Coding", '<path d="M8 7L3 12l5 5M16 7l5 5-5 5M14 4l-4 16"/>'),
    "Vid": ("Video", '<rect x="3" y="6" width="13" height="12" rx="2.5"/><path d="M16 10.5l5-3v9l-5-3"/>'),
    "Krea": ("Image", '<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="9" cy="10" r="1.8"/>'
                      '<path d="M20.5 16l-5-5L6 20"/>'),
    "Mus": ("Music", '<path d="M9 18V6l11-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>'),
    "Voice": ("Voice", '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>'),
    "Agent": ("Master Agent", '<path d="M11 3l1.8 4.7 4.7 1.8-4.7 1.8L11 16l-1.8-4.7L4.5 9.5l4.7-1.8z"/>'
                              '<path d="M18.5 14.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>'),
    "Trainer": ("Trainer", '<path d="M9.5 3h5M10.5 3v6.2L5.2 18.4A1.7 1.7 0 0 0 6.7 21h10.6a1.7 1.7 0 0 0 1.5-2.6L13.5 9.2V3"/>'
                           '<path d="M7.5 15h9"/>'),
    "Queue": ("GPU Queue", '<path d="M12 3.5l8.5 4.5-8.5 4.5L3.5 8z"/><path d="M3.5 12.5l8.5 4.5 8.5-4.5"/>'
                           '<path d="M3.5 16.5l8.5 4.5 8.5-4.5"/>'),
    "REA": ("REA", '<circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5.5 5.5"/><path d="M8.5 8.5l-1.5 2 1.5 2M12.5 8.5l1.5 2-1.5 2"/>'),
    "Models": ("Models", '<path d="M12 3v11"/><path d="M7.5 9.5L12 14l4.5-4.5"/><path d="M4 15.5V19a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3.5"/>'),
    "Cache": ("RAG & Cache", '<ellipse cx="10" cy="5.5" rx="6.5" ry="2.5"/><path d="M3.5 5.5v12c0 1.4 2.9 2.5 6.5 2.5M16.5 5.5V10M3.5 11.5c0 1.4 2.9 2.5 6.5 2.5"/><path d="M18 13l1 2.4 2.4 1-2.4 1-1 2.4-1-2.4-2.4-1 2.4-1z"/>'),
    "Connections": ("Connections", '<path d="M21 3L3 10.5l7 2.5 2.5 7z"/><path d="M10 13l11-10"/>'),
    "Settings": ("Settings", '<circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="6.6"/>'
                             '<path d="M12 2.8v2.4M12 18.8v2.4M21.2 12h-2.4M5.2 12H2.8M18.5 5.5l-1.7 1.7M7.2 16.8l-1.7 1.7'
                             'M18.5 18.5l-1.7-1.7M7.2 7.2L5.5 5.5"/>'),
    "Llama API": ("Llama API", '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M9 9h6v6H9zM9 1.5V4M15 1.5V4'
                               'M9 20v2.5M15 20v2.5M1.5 9H4M1.5 15H4M20 9h2.5M20 15h2.5"/>'),
}


def crumb(app: str) -> str:
    """The "Rocsy / <App>" breadcrumb; Rocsy is the way back to the home page."""
    label, icon = APPS.get(app, (app, ""))
    icon_html = ('<span class="rb-app-icon"><svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" '
                 f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{icon}</svg></span>'
                 if icon else "")
    return (
        '<div class="rb-crumb">'
        f'<a class="rb-home" href="/" title="Back to the Rocsy home page" aria-label="Rocsy home">{ROCSY_MARK}'
        '<span class="rb-home-name">Rocsy</span></a>'
        '<span class="rb-sep" aria-hidden="true">/</span>'
        f'<span class="rb-app">{icon_html}'
        f'<span class="rb-app-name">{escape(label)}</span></span>'
        '</div>'
    )


# The first element that is the app's top bar: class topbar (most studios) or
# a-top (Agent), else the page's first <header> (Connections).
_BAR = re.compile(rb'<(?:header|div)\b[^>]*\bclass="(?:[^"]*\s)?(?:topbar|a-top)(?:\s[^"]*)?"[^>]*>', re.I)
_HEADER = re.compile(rb"<header\b[^>]*>", re.I)
_CLASS = re.compile(rb'\bclass="', re.I)


def decorate(html: bytes, app: str) -> bytes:
    """Tag the app's top bar and insert the crumb as its first child. Pages
    without a bar (embeds, error pages) come back unchanged."""
    if app not in APPS or b"rb-bar" in html:
        return html
    body = html.lower().find(b"<body")
    match = _BAR.search(html, max(body, 0)) or _HEADER.search(html, max(body, 0))
    if body < 0 or not match:
        return html
    tag = match.group(0)
    attrs = f' data-rb-app="{app}"'.encode()
    if _CLASS.search(tag):
        new_tag = _CLASS.sub(b'class="rb-bar ', tag, count=1)
    else:
        new_tag = tag[:-1] + b' class="rb-bar">'
    new_tag = new_tag[:-1] + attrs + b">"
    return html[:match.start()] + new_tag + crumb(app).encode() + html[match.end():]
