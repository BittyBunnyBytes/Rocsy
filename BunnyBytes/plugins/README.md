# Plugins: your own services in Rocsy

A plugin is any web app you run yourself (usually a container) that Rocsy puts
behind its login, at its own path, with the Rocsy theme, top bar and icon. It
can also get a home card, a row in **Settings → Services**, and turns on the
shared GPU queue.

Plugin folders live in **`core-backend-extras/plugins/`** (next to `BunnyBytes/`,
under `ROCSY_ROOT`), and you see and manage them in **Models → Manager → Core Backend
→ Plugins**: whether each one answers, Open, Start / Stop / Restart for its container,
plugins that are turned off, and any `plugin.json` that was rejected. That folder is
this machine's own: it is git-ignored and left out of compiled installers, so
private plugins never ship. Only this README and the example (`example/`) are kept
here; the installer copies the example into `core-backend-extras/plugins/example/`
when that folder doesn't exist yet.

## Start from the example

`core-backend-extras/plugins/example/` is a complete, working plugin that opens at
`/Example/`: a no-dependency Python app in a stock `python:3.12-slim` container,
with a health check, a picture icon, a home card whose backdrop is the app's own
slideshow, and update checks against its own release feed. It arrives turned off
(`"enabled": false`). To try it, set `"enabled": true` and include it from
`BunnyBytes/docker-compose.override.yml`:

```yaml
include:
  - path: ../core-backend-extras/plugins/example/compose.yml
    project_directory: .
```

To start your own, copy the folder, change `id`, `path`, the port and the container
name, and point `updates` at your repo. To drop the example, remove those `include`
lines and its folder.

## Add one

1. Make a folder there, e.g. `core-backend-extras/plugins/notes/`, with a `plugin.json`:

   ```json
   {
     "id": "Notes",
     "name": "Notes",
     "note": "My notes app",
     "path": "/Notes",
     "upstream": "http://127.0.0.1:8123",
     "health": "/Notes/api/health",
     "container": "my-notes",
     "color": "#4fcbdc",
     "icon": "<path d=\"M6 3h9l4 4v14H6z\"/><path d=\"M9 12h7M9 16h5\"/>",
     "home": {"group": "Tools", "desc": "My notes app", "status": "Notes"}
   }
   ```

2. If it runs in a container, put its Compose service in the same folder
   (`core-backend-extras/plugins/notes/compose.yml`) and include it from your
   `BunnyBytes/docker-compose.override.yml`:

   ```yaml
   include:
     - path: ../core-backend-extras/plugins/notes/compose.yml
       project_directory: .   # ${...} values and relative paths resolve from BunnyBytes/
   ```

   Use `network_mode: host` and bind the app to `127.0.0.1` so it is only
   reachable through the gateway.

3. Start it and restart the gateway:

   ```sh
   cd BunnyBytes
   docker compose up -d --build my-notes
   docker compose up -d --no-deps --force-recreate gateway
   ```

   It now opens at `/Notes/`. **Models → Manager → Core Backend → Plugins**
   shows it. (Its container also appears under **Settings → Services → Plugins**
   once the Services helper restarts.)

## plugin.json

| Key | | |
|---|---|---|
| `id` | required | 2-24 letters/digits. Must not clash with a Rocsy service (Video, Image, Agent, …). Also the app name for the GPU queue. |
| `upstream` | required | Base URL of the app, e.g. `http://127.0.0.1:8123`. |
| `path` | `/<id>` | Where it opens in Rocsy. Rocsy's own paths can't be taken. |
| `strip_prefix` | `false` | `false`: the app serves under `path` itself (it has a mount prefix). `true`: the gateway removes `path` before forwarding. |
| `root_app` | `false` | With `strip_prefix: true`, for apps that only work at the site root and have no base-path setting (absolute `/api`, `/scripts/…` URLs; e.g. SillyTavern). The gateway adds `path` to the app's root-absolute links in its pages, its fetch/XHR calls, module imports (an import map), script/stylesheet URLs set from JS and its redirects, and sends root-path requests made from its pages (pictures, fonts) back under `path`. Its pages get a looser Content-Security-Policy (inline scripts and eval), scoped to `path`. |
| `name` | `id` | Label in the top bar, home card and Services. |
| `note` | | One line shown in Settings. |
| `health` | | Path on the upstream that answers when the app is up. |
| `container` | | Container name: adds Start / Stop / Restart in **Core Backend → Plugins** and **Settings → Services** (picked up when the Services helper restarts). |
| `gpu_queue` | `false` | Informational: the app takes GPU turns. It does so with the shared client `BunnyBytes/shared/bunnylock.py` (mount it read-only into the container) under `"app": "<id>"`: turns, prepare-early notices and the Queue page's Cancel arrive over the client's stream (protocol v2, `BunnyBytes/lock/PROTOCOL.md`). The old `/api/internal/queue/proceed` and `/cancel` callbacks are gone. |
| `themed` | `true` | Inject the Rocsy theme and rewrite the app's colours. |
| `color` | grey | `#rrggbb` for the favicon and home card. |
| `icon_file` | | A picture in the plugin's folder (`.png`, `.svg`, `.webp`, `.jpg`, up to 512 KB) used **instead of** `icon` in the top bar, browser tab, home card and Core Backend. Settings → Upload picture writes `icon.<type>` and sets this. |
| `icon` | square | SVG shapes on a 24 px grid, stroked with the current colour. Only `path`, `circle`, `rect`, `ellipse`, `line`, `polyline`, `polygon` and `g`. |
| `version` | | The version installed here, e.g. `"1.0.0"`. Shown on its card and compared with `updates`. |
| `updates` | | Where Core Backend checks for a newer release (cached 6 h; **Check now** forces it): `{"github": "owner/repo"}` (latest release, or newest tag if there are none; add `"prereleases": true` to count those) **or** `{"url": "https://…/latest.json"}` / `{"url": "/Name/api/latest"}` (any JSON with `version` or `tag_name`, optional `url` and `notes`; a path is asked of the plugin). Optional `"current_url": "/Name/api/version"`: the running app reports its own version (`{"version": "…"}`), which beats `version`; `"current_key": "pkgVersion"` reads it from another field of that reply. |
| `home.background` | `"none"` | What shows behind the home card (while it's in Create): `"none"`, `"image"` (newest Rocsy Image generation), `"video"` (newest Rocsy Video generation), `"recent"` (newest of either), or `"plugin"` (the plugin's own pictures, below). |
| `home.background_url` | | With `"plugin"`: a path on the upstream answering `{"pictures": ["/Name/…", …], "slideshow": true}` (or a plain list). Only pictures under the plugin's own `path` are shown, at most 24, newest first. |
| `home.slideshow` | `false` | Crossfade through the pictures instead of showing one (`"recent"` and `"plugin"`). The plugin's reply can also ask for it. |
| `home` | `false` | `false` = reachable by URL only. An object adds a home card: `group` (`Create` or `Tools`), `desc`, `status`, `experimental`. It then shows in **Settings → Home page** like any module. |
| `enabled` | `true` | `false` turns the plugin off without deleting it. |

Folders starting with `.` or `_` are ignored, so `_draft/` is a safe place to
work on one.
