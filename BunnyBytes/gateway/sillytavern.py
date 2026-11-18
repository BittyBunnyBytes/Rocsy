"""SillyTavern at /SillyTavern: one of Rocsy's own optional services (compose profile "sillytavern").

The container is `sillytavern` in docker-compose.yml (code and Rocsy add-ons in ROOT/SillyTavern).
SillyTavern has no base-path setting, so it is served like a root_app plugin (app.py
_register_plugin: path shim, root fallback, static cache), but it is built in: it isn't listed under
Core Backend -> Plugins and a plugin can't take its name or path. It is registered only when its
profile is on (COMPOSE_PROFILES, passed in as ROCSY_PROFILES); Core Backend -> SillyTavern switches it.
"""
from __future__ import annotations

import os
from pathlib import Path

import plugins

ID = "SillyTavern"
PATH = "/SillyTavern"
PROFILE = "sillytavern"

MANIFEST = {
    "id": ID,
    "name": "SillyTavern",
    "note": "SillyTavern chat frontend (official image), behind the Rocsy login",
    "path": PATH,
    "upstream": os.getenv("SILLYTAVERN_UPSTREAM", "http://127.0.0.1:8140"),
    "strip_prefix": True,
    "root_app": True,
    "themed": False,
    "health": "/version",
    "container": "rocsy-sillytavern",
    "color": "#e9a04f",
    "icon": '<path d="M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H11l-4 3v-3H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z"/>'
            '<path d="M8 10h8M8 13h5"/>',
    "home": {
        "group": "Tools",
        "desc": "Chat with characters through your own LLMs",
        "status": "SillyTavern",
        "experimental": True,
        # Character pictures from the Rocsy Addons server plugin (empty until that add-on is installed).
        "background": "plugin",
        "background_url": "/api/plugins/rocsy/home-art?prefix=/SillyTavern",
    },
    "gpu_queue": True,
    "static_cache": {
        "seconds": 86400,
        "paths": ["/scripts/", "/css/", "/lib/", "/lib.js", "/script.js", "/style.css", "/webfonts/", "/img/",
                  "/locales/", "/favicon"],
        "exclude": ["/scripts/extensions/third-party/"],
        "version_url": "/version",
        "version_key": "pkgVersion",
    },
}


def enabled() -> bool:
    profiles = os.getenv("ROCSY_PROFILES", os.getenv("COMPOSE_PROFILES", ""))
    return PROFILE in {name.strip() for name in profiles.split(",")}


def service() -> plugins.Plugin:
    return plugins._parse(Path("sillytavern"), MANIFEST, set(), set())
