"""Makes the studios' hard-coded colours themeable.

Every studio stylesheet the gateway proxies goes through `rewrite_css`: each
colour literal that sits close to one of the studios' shared palette roles
(background, panel, border, text, accent, ...) becomes
`var(--t-<role>, <original>)`. /theme.css defines the --t-* tokens for a theme
and leaves them unset for "Studio default", so the fallback keeps every studio
pixel-identical until a theme is picked. The output does not depend on the
chosen theme, so CDN/browser caching of the rewritten files is safe.

Colours far from every role (photo overlays, the rainbow "legendary"
gradient, pure white on buttons) are left alone."""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache

# Bump when the mapping changes: it's part of the ?bbt= cache key the proxy
# adds to studio stylesheet URLs.
REWRITE_VERSION = "4"

# Native colours of the studios (H3 Machine, Krea, Mus, Trainer, Queue),
# grouped by the role they play. Taken from their :root blocks + the most
# common hard-coded literals.
ROLES: dict[str, list[str]] = {
    "bg": ["#0a0f13", "#0f1418", "#0c1115", "#101619", "#081018", "#070b0e", "#0b1014", "#04070a", "#05080a"],
    "panel": ["#111920", "#182128", "#151f25", "#192329", "#141d23", "#151e25", "#17212a", "#131b22"],
    "soft": ["#0d1419", "#12191e", "#10171c", "#131c21", "#0e151a"],
    "raised": ["#1b2730", "#22313b", "#19242b", "#1f2a33", "#202c35", "#263540"],
    "border": ["#25323b", "#354550", "#33414c", "#2c3b43", "#32444d", "#2d3b43", "#243139", "#2a3842", "#3a4b57"],
    "text": ["#d5dde2", "#c7d0d6", "#e3eae8", "#e0e8ec", "#e2e9e8", "#eaf4fb", "#dfe7ec", "#cfd8de", "#f1f5f8"],
    "muted": ["#86949e", "#7f8c96", "#92a19f", "#97a6ae", "#91a19f", "#9aa7b0", "#a7b3bb"],
    "faint": ["#5c6a73", "#6f807e", "#6d7e7e", "#56636c", "#66747d"],
    "accent": ["#7cb3dc", "#79add3", "#8cc3ea", "#6fa8d6", "#9fd0f2", "#a3e5c5", "#5fa8a0"],
    "accent2": ["#5b8fd4", "#4f84c8", "#3f78bf"],
    "violet": ["#a58cf0", "#9b86c4", "#b6a0e1", "#b58cf0", "#8f78d8"],
    "green": ["#82c45a", "#7fbd54", "#91ca7c", "#6fb34a", "#9ad47a", "#4cc38a"],
    "amber": ["#e5ac48", "#e1a744", "#f0bf71", "#e4b460", "#f3b562", "#d99a2b"],
    "red": ["#d86f6f", "#a85b5b", "#e58a7d", "#dd8585", "#c95b5b", "#ef6f6f", "#b84c4c"],
}

_SAMPLES = [(role, tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))) for role, colors in ROLES.items() for hex_ in colors]

_COLOR = re.compile(
    r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b"
    r"|rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(?:,\s*[\d.]+%?\s*)?\)"
    r"|rgba?\(\s*\d{1,3}\s+\d{1,3}\s+\d{1,3}\s*(?:/\s*[\d.]+%?\s*)?\)")
# Never touch colours inside url(...) (SVG data URIs), @font-face, or a value that already uses var().
_SKIP = re.compile(r"url\([^)]*\)|@font-face\s*\{[^}]*\}", re.S)


def _parse(literal: str) -> tuple[tuple[int, int, int], float] | None:
    if literal.startswith("#"):
        hex_ = literal[1:]
        if len(hex_) in (3, 4):
            hex_ = "".join(ch * 2 for ch in hex_)
        rgb = tuple(int(hex_[i:i + 2], 16) for i in (0, 2, 4))
        alpha = int(hex_[6:8], 16) / 255 if len(hex_) == 8 else 1.0
        return rgb, alpha  # type: ignore[return-value]
    numbers = re.findall(r"[\d.]+%?", literal[literal.index("(") + 1:])
    if len(numbers) < 3:
        return None
    rgb = tuple(min(255, int(float(n))) for n in numbers[:3])
    alpha = 1.0
    if len(numbers) > 3:
        alpha = float(numbers[3].rstrip("%")) / (100 if numbers[3].endswith("%") else 1)
    return rgb, alpha  # type: ignore[return-value]


def _chroma(rgb: tuple[int, int, int]) -> int:
    return max(rgb) - min(rgb)


@lru_cache(maxsize=4096)
def classify(rgb: tuple[int, int, int]) -> str | None:
    """Nearest palette role, or None when nothing is close. Neutrals only
    match neutral roles and colourful literals only colourful ones, so a
    grey never turns into the accent."""
    colourful = _chroma(rgb) > 48
    best, best_distance = None, 1e9
    for role, sample in _SAMPLES:
        if (_chroma(sample) > 48) != colourful:
            continue
        # Weighted RGB distance (cheap perceptual-ish).
        dr, dg, db = rgb[0] - sample[0], rgb[1] - sample[1], rgb[2] - sample[2]
        distance = (2 * dr * dr + 4 * dg * dg + 3 * db * db) ** 0.5 / 3
        if distance < best_distance:
            best, best_distance = role, distance
    return best if best_distance <= (30 if colourful else 14) else None


_TINT_ROLES = ("accent", "accent2", "violet", "green", "amber", "red")
_BASES = {"bg": (10, 15, 19), "text": (226, 233, 236)}


@lru_cache(maxsize=4096)
def tint(rgb: tuple[int, int, int]) -> tuple[str, str, int] | None:
    """A colourful literal that's a role colour blended into the background
    (dark status-badge fills) or into the text colour (pale labels):
    (role, base, percent of role) when one blend reproduces it closely."""
    if _chroma(rgb) < 24:
        return None
    best, best_error = None, 1e9
    for role, sample in _SAMPLES:
        if role not in _TINT_ROLES:
            continue
        for base, base_rgb in _BASES.items():
            # Least-squares blend amount t of sample over base.
            d = [sample[i] - base_rgb[i] for i in range(3)]
            denominator = sum(x * x for x in d) or 1
            t = sum((rgb[i] - base_rgb[i]) * d[i] for i in range(3)) / denominator
            if not 0.08 <= t <= 0.92:
                continue
            error = max(abs(base_rgb[i] + t * d[i] - rgb[i]) for i in range(3))
            if error < best_error:
                best, best_error = (role, base, round(t * 100)), error
    return best if best_error <= 12 else None


def _replace(literal: str) -> str:
    parsed = _parse(literal)
    if not parsed:
        return literal
    rgb, alpha = parsed
    native = "#%02x%02x%02x" % rgb
    role = classify(rgb)
    if role:
        color = f"var(--t-{role}, {native})"
    elif blend := tint(rgb):
        # A blend of two tokens; with no theme the fallbacks reproduce the
        # literal to within a few levels per channel.
        tint_role, base, percent = blend
        sample = min((sample for name, sample in _SAMPLES if name == tint_role),
                     key=lambda c: sum((c[i] - rgb[i]) ** 2 for i in range(3)))
        color = (f"color-mix(in srgb, var(--t-{tint_role}, {'#%02x%02x%02x' % sample}) {percent}%, "
                 f"var(--t-{base}, {'#%02x%02x%02x' % _BASES[base]}))")
    else:
        return literal
    if alpha >= 0.999:
        return color
    return f"color-mix(in srgb, {color} {round(alpha * 100, 2):g}%, transparent)"


def rewrite_css(css: str) -> str:
    out, last = [], 0
    for skip in _SKIP.finditer(css):
        out.append(_COLOR.sub(lambda m: _replace(m.group(0)), css[last:skip.start()]))
        out.append(skip.group(0))
        last = skip.end()
    out.append(_COLOR.sub(lambda m: _replace(m.group(0)), css[last:]))
    return "".join(out)


_cache: dict[str, bytes] = {}


def rewrite_cached(body: bytes) -> bytes:
    key = hashlib.sha256(body).hexdigest()
    if key not in _cache:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = rewrite_css(body.decode("utf-8", errors="replace")).encode()
    return _cache[key]


_STYLESHEET_HREF = re.compile(rb'(<link\b[^>]*rel=["\']?stylesheet["\']?[^>]*href=["\'])([^"\']+)', re.I)


def tag_stylesheets(html: bytes) -> bytes:
    """Adds ?bbt=<REWRITE_VERSION> to a studio page's own stylesheet links so
    copies cached before the rewrite (or an older mapping) aren't reused."""
    def add(match: re.Match[bytes]) -> bytes:
        href = match.group(2)
        if b"/theme.css" in href or href.startswith((b"http:", b"https:", b"//")):
            return match.group(0)
        sep = b"&" if b"?" in href else b"?"
        return match.group(1) + href + sep + b"bbt=" + REWRITE_VERSION.encode()
    return _STYLESHEET_HREF.sub(add, html)
