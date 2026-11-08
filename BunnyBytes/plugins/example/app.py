"""Example Rocsy plugin: a tiny web app with no dependencies beyond Python itself.

Copy this folder (core-backend-extras/plugins/example) to start your own plugin. It shows
everything a plugin can hook into:

  /Example/                 the app's page. The Rocsy gateway serves it behind the login and
                            adds the Rocsy theme and top bar (it looks for <header class="topbar">).
  /Example/api/health       plugin.json "health": Core Backend -> Plugins shows "Answering".
  /Example/api/home-art     plugin.json home.background "plugin": pictures for the home card's
                            backdrop, as {"pictures": [...], "slideshow": true}. They must be
                            served under the plugin's own path (/Example/...).
  /Example/art/<n>.svg      the pictures themselves (generated here, so nothing has to be stored).
  /Example/api/info         a small JSON endpoint the page polls.
  /Example/api/version      plugin.json updates.current_url: the version this copy runs.
  /Example/api/latest       plugin.json updates.url: the newest release, as {"version", "url", "notes"}.
                            A real plugin points updates at its GitHub repo or its own release feed.

The app serves under its own prefix (plugin.json "strip_prefix": false), binds to 127.0.0.1
only, and is reached by Rocsy through the gateway at http://<rocsy>/Example/.
"""
import json
import os
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.getenv("EXAMPLE_HOST", "127.0.0.1")
PORT = int(os.getenv("EXAMPLE_PORT", "8130"))
PREFIX = os.getenv("EXAMPLE_PREFIX", "/Example").rstrip("/")
VERSION = "1.0.0"
# The "newest release" the example advertises, so Core Backend shows how an available update looks.
LATEST = {"version": "1.1.0", "url": "https://github.com/", "published": "2026-11-05",
          "notes": "Example release. Point plugin.json \"updates\" at your GitHub repo or release feed."}
STARTED = time.time()
VISITS = {"count": 0}

# Colour pairs for the generated backdrop pictures.
ART = [("#0f3d4c", "#4fcbdc"), ("#2a1d4f", "#8f7cf2"), ("#3f1f2a", "#e8735f"),
       ("#1f3a24", "#84c96c"), ("#3d331a", "#e9c84f"), ("#1b2b45", "#5eaaf0")]


def art_svg(index: int) -> str:
    dark, light = ART[index % len(ART)]
    circles = "".join(
        f'<circle cx="{(index * 97 + i * 211) % 1200}" cy="{(index * 53 + i * 149) % 700}" r="{90 + (i * 37) % 160}" '
        f'fill="{light}" opacity="{0.10 + (i % 4) * 0.06:.2f}"/>' for i in range(9))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 700" width="1200" height="700">'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{dark}"/>'
            f'<stop offset="1" stop-color="{light}" stop-opacity=".55"/></linearGradient></defs>'
            f'<rect width="1200" height="700" fill="url(#g)"/>{circles}</svg>')


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Example plugin</title>
  <style>
    :root { color-scheme: dark; --bg: #0b1115; --panel: #121a20; --border: #24313a; --text: #d8e0e5; --muted: #8796a0; --accent: #4fcbdc; }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
    header.topbar { display: flex; align-items: center; min-height: 56px; padding: 8px 18px; border-bottom: 1px solid var(--border); }
    main { max-width: 860px; margin: 0 auto; padding: 32px 18px 60px; }
    h1 { margin: 0 0 6px; font-size: 26px; }
    .lead { margin: 0 0 24px; color: var(--muted); }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 240px), 1fr)); gap: 12px; margin-bottom: 24px; }
    .card { padding: 16px; border: 1px solid var(--border); border-radius: 12px; background: var(--panel); }
    .card strong { display: block; font-size: 22px; color: var(--accent); font-variant-numeric: tabular-nums; }
    .card span { color: var(--muted); font-size: 13px; }
    ul { padding-left: 20px; } li { margin: 6px 0; }
    code { padding: 1px 6px; border-radius: 5px; background: #0d1519; font-size: 13px; }
    .art { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
    .art img { width: 100%; border-radius: 9px; border: 1px solid var(--border); display: block; }
  </style>
</head>
<body>
  <header class="topbar"></header>
  <main>
    <h1>Hello from a Rocsy plugin</h1>
    <p class="lead">This page comes from a small Python app in its own container. Rocsy puts it behind its login at
      <code>__PREFIX__/</code> and adds the theme, top bar and icon.</p>
    <div class="grid">
      <div class="card"><strong id="uptime">…</strong><span>app uptime</span></div>
      <div class="card"><strong id="visits">…</strong><span>page views since start</span></div>
      <div class="card"><strong id="version">…</strong><span>plugin version (checked for updates in Core Backend)</span></div>
    </div>
    <h2>What this example shows</h2>
    <ul>
      <li><code>plugin.json</code>: id, path, upstream, health check, colour, a picture icon (<code>icon.svg</code>) and a home card.</li>
      <li><code>compose.yml</code>: the container, on the host network but bound to 127.0.0.1 so only Rocsy can reach it.</li>
      <li><code>__PREFIX__/api/home-art</code>: the pictures behind its home card, shown as a slideshow:</li>
    </ul>
    <div class="art">__ART__</div>
    <p class="lead" style="margin-top:24px">To make your own, copy <code>core-backend-extras/plugins/example</code>, change the
      id, path and port, and include its compose.yml from <code>BunnyBytes/docker-compose.override.yml</code>.
      Settings for every plugin are in Models → Manager → Core Backend → Plugins.</p>
  </main>
  <script>
    async function tick() {
      try {
        const info = await (await fetch("__PREFIX__/api/info", {cache: "no-store"})).json();
        const s = Math.round(info.uptime);
        document.getElementById("uptime").textContent = s < 120 ? `${s} s` : s < 7200 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`;
        document.getElementById("visits").textContent = info.visits;
        document.getElementById("version").textContent = info.version;
      } catch { /* the gateway is restarting */ }
    }
    tick(); setInterval(tick, 5000);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "RocsyExamplePlugin/1.0"

    def log_message(self, fmt, *args):  # keep the container log quiet
        pass

    def send(self, status: int, body: bytes, kind: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, data, status: int = 200) -> None:
        self.send(status, json.dumps(data).encode(), "application/json")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith(PREFIX + "/") and path != PREFIX:
            return self.json({"error": "not found"}, 404)
        route = path[len(PREFIX):] or "/"
        if route == "/":
            VISITS["count"] += 1
            pictures = "".join(f'<img src="{PREFIX}/art/{i}.svg" alt="" loading="lazy">' for i in range(len(ART)))
            page = PAGE.replace("__PREFIX__", escape(PREFIX)).replace("__ART__", pictures)
            return self.send(200, page.encode(), "text/html; charset=utf-8")
        if route == "/api/health":
            return self.json({"status": "ok"})
        if route == "/api/info":
            return self.json({"uptime": time.time() - STARTED, "visits": VISITS["count"], "version": VERSION})
        if route == "/api/version":
            return self.json({"version": VERSION})
        if route == "/api/latest":
            return self.json(LATEST)
        if route == "/api/home-art":
            return self.json({"pictures": [f"{PREFIX}/art/{i}.svg" for i in range(len(ART))], "slideshow": True})
        if route.startswith("/art/") and route.endswith(".svg"):
            try:
                index = int(route[len("/art/"):-len(".svg")])
            except ValueError:
                return self.json({"error": "not found"}, 404)
            if 0 <= index < len(ART):
                return self.send(200, art_svg(index).encode(), "image/svg+xml", "public, max-age=86400")
        return self.json({"error": "not found"}, 404)


if __name__ == "__main__":
    print(f"Example plugin on http://{HOST}:{PORT}{PREFIX}/", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
