#!/usr/bin/env python3
"""Render the animated "Made with Rocsy" end card.

usage: make_endcard.py WIDTH HEIGHT OUT.mp4
Frames are SVG -> PNG (rsvg-convert), encoded with h264_vaapi plus a silent
AAC track so the clip concatenates cleanly onto generated videos.
"""
import math
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

FPS = 30
DUR = 4.0
BIRD = ("M256 186 C269 186 276 197 274 210 C320 182 380 162 464 156 C418 200 358 234 "
        "292 254 L308 314 L256 294 L204 314 L220 254 C154 234 94 200 48 156 C132 162 "
        "192 182 238 210 C236 197 243 186 256 186 Z")
FONT = "Adwaita Sans"


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def prog(t, start, end):
    return clamp((t - start) / (end - start))


def out_cubic(p):
    return 1 - (1 - p) ** 3


def out_back(p, s=1.6):
    p -= 1
    return 1 + (s + 1) * p ** 3 + s * p ** 2


def frame_svg(t, W, H):
    u = min(W, H) / 1080          # layout unit
    portrait = H > W
    cx = W / 2
    logo_y = H * (0.40 if portrait else 0.36)
    logo_scale = (1.6 if portrait else 1.05) * u
    made_y = logo_y + (360 if portrait else 300) * u
    name_y = made_y + (175 if portrait else 150) * u
    name_px = (180 if portrait else 150) * u
    made_px = 60 * u if portrait else 50 * u

    fade_in = out_cubic(prog(t, 0.0, 0.35))
    fade_out = 1 - prog(t, DUR - 0.45, DUR)
    master = fade_in * fade_out

    disc = out_back(prog(t, 0.15, 0.75))
    glow = out_cubic(prog(t, 0.3, 1.0)) * (0.85 + 0.15 * math.sin(max(0, t - 1.0) * 3.2))
    bp = prog(t, 0.45, 1.1)
    bird_op = out_cubic(prog(t, 0.45, 0.7))
    bird_dy = (1 - out_cubic(bp)) * 140
    bird_sx = 0.25 + 0.75 * out_back(bp, 1.2)
    bird_sy = 0.6 + 0.4 * out_cubic(bp)

    made_p = out_cubic(prog(t, 1.0, 1.45))
    letters = "Rocsy"
    spans = []
    prev_y = name_y
    for i, ch in enumerate(letters):
        lp = out_back(prog(t, 1.15 + i * 0.08, 1.55 + i * 0.08), 1.3)
        op = clamp(prog(t, 1.15 + i * 0.08, 1.35 + i * 0.08))
        y = name_y + (1 - lp) * 60 * u
        # dy keeps one text chunk so the word stays centred with normal spacing
        spans.append(f'<tspan dy="{y - prev_y:.1f}" fill-opacity="{op:.3f}">{ch}</tspan>')
        prev_y = y
    bar_p = out_cubic(prog(t, 1.75, 2.2))
    bar_w = 300 * u * bar_p
    sweep = prog(t, 2.15, 2.8)
    sweep_x = cx - 420 * u + sweep * 840 * u

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<defs>
  <radialGradient id="bg" cx="50%" cy="45%" r="75%">
    <stop offset="0" stop-color="#1e2b35"/><stop offset="1" stop-color="#070a0d"/>
  </radialGradient>
  <radialGradient id="glow" cx="50%" cy="50%" r="50%">
    <stop offset="0.55" stop-color="#e9a04f" stop-opacity="0.5"/>
    <stop offset="1" stop-color="#e9a04f" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="sweep" x1="0" x2="1" y1="0" y2="0">
    <stop offset="0" stop-color="#ffd59c" stop-opacity="0"/>
    <stop offset="0.5" stop-color="#ffd59c" stop-opacity="0.95"/>
    <stop offset="1" stop-color="#ffd59c" stop-opacity="0"/>
  </linearGradient>
  <clipPath id="nameclip">
    <text x="{cx}" y="{name_y:.1f}" text-anchor="middle" font-family="{FONT}" font-weight="800"
      font-size="{name_px:.1f}" letter-spacing="{4*u:.1f}">Rocsy</text>
  </clipPath>
</defs>
<rect width="{W}" height="{H}" fill="#000"/>
<g opacity="{master:.3f}">
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <g transform="translate({cx} {logo_y}) scale({logo_scale:.4f}) translate(-256 -268)">
    <circle cx="256" cy="268" r="200" fill="url(#glow)" opacity="{glow:.3f}"/>
    <circle cx="256" cy="268" r="{124*disc:.2f}" fill="#e9a04f"/>
    <g opacity="{bird_op:.3f}" transform="translate(256 {250+bird_dy:.2f}) scale({bird_sx:.4f} {bird_sy:.4f}) translate(-256 -250)">
      <path fill="#f2f7fb" d="{BIRD}"/>
    </g>
  </g>
  <text x="{cx}" y="{made_y + (1-made_p)*30*u:.1f}" text-anchor="middle" font-family="{FONT}"
    font-weight="600" font-size="{made_px:.1f}" letter-spacing="{14*u:.1f}" fill="#9fb0bd"
    fill-opacity="{made_p:.3f}">MADE WITH</text>
  <text x="{cx}" y="{name_y:.1f}" text-anchor="middle" font-family="{FONT}" font-weight="800"
    font-size="{name_px:.1f}" letter-spacing="{4*u:.1f}" fill="#f2f7fb">{''.join(spans)}</text>
  <g clip-path="url(#nameclip)" opacity="{1 if 0 < sweep < 1 else 0}">
    <rect x="{-110*u:.1f}" y="{-name_px:.1f}" width="{220*u:.1f}" height="{name_px*1.3:.1f}"
      fill="url(#sweep)" transform="translate({sweep_x:.1f} {name_y:.1f}) skewX(-18)"/>
  </g>
  <rect x="{cx - bar_w/2:.1f}" y="{name_y + 50*u:.1f}" width="{bar_w:.1f}" height="{10*u:.1f}"
    rx="{5*u:.1f}" fill="#e9a04f"/>
</g>
</svg>'''


def main():
    W, H, out = int(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3]).resolve()
    n = int(DUR * FPS)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        def render(i):
            svg = tmp / f"f{i:04d}.svg"
            svg.write_text(frame_svg(i / FPS, W, H))
            subprocess.run(["rsvg-convert", "-w", str(W), "-h", str(H), str(svg),
                            "-o", str(tmp / f"f{i:04d}.png")], check=True)

        with ThreadPoolExecutor(8) as ex:
            list(ex.map(render, range(n)))
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-vaapi_device", "/dev/dri/renderD128",
            "-framerate", str(FPS), "-i", str(tmp / "f%04d.png"),
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "18",
            "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart",
            str(out)], check=True)


if __name__ == "__main__":
    main()
