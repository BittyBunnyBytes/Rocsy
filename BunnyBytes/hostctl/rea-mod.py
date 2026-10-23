#!/usr/bin/env python3
"""REA mods on the real game, from a terminal (the Master Agent uses `$R reamod`, which also holds
the GPU slot). The user allowed Claude Code to run this without asking (2026-10-16).

  rea-mod.py status MOD
  rea-mod.py install MOD | uninstall MOD
  rea-mod.py play MOD [--seconds 90] [--screenshots 3] [--uninstall] [--no-install] [--user-file NAME]...

`play` starts the game on the desktop through Steam. The game shares the GPU with the desktop and
the local LLM, so it refuses while the GPU queue has anything in it and unloads llama first."""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = {}
for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        ENV[key.strip()] = value.strip().strip('"')
HOSTCTL = "http://127.0.0.1:" + ENV.get("HOSTCTL_PORT", "8899")
QUEUE = "http://127.0.0.1:" + ENV.get("LOCK_PORT", "8600")
LLAMA = "http://127.0.0.1:" + ENV.get("LLAMA_PORT", "8400")


def request(url, method="GET", body=None, headers=None, timeout=30):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {"text": raw.decode(errors="replace")[:200]}    # llama-swap /unload answers "OK"
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}")
        except ValueError:
            return {"error": f"HTTP {e.code}"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["status", "install", "uninstall", "play"])
    ap.add_argument("mod")
    ap.add_argument("--seconds", type=int, default=90)
    ap.add_argument("--screenshots", type=int, default=3)
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-install", dest="install", action="store_false")
    ap.add_argument("--user-file", dest="user_files", action="append", default=[],
                    help="play: create this empty file in the game's user:// folder for the modded run (a mod's one-shot autostart flag)")
    a = ap.parse_args()
    auth = {"X-Hostctl-Secret": ENV.get("HOSTCTL_SECRET", "")}
    if a.action == "status":
        result = request(f"{HOSTCTL}/rea/mods/{a.mod}/status", headers=auth)
        print(json.dumps(result, indent=1))
        sys.exit(1 if result.get("error") else 0)
    if a.action == "play":
        queue = request(f"{QUEUE}/Queue/api/queue").get("entries") or []
        if queue:
            sys.exit(f"refused: the GPU queue is busy ({len(queue)} entr{'y' if len(queue) == 1 else 'ies'}); try again when it's empty")
        request(f"{LLAMA}/unload", headers={"Authorization": f"Bearer {ENV.get('LLAMA_API_KEY', '')}"}, timeout=30)
        body = {"seconds": a.seconds, "screenshots": a.screenshots, "uninstall": a.uninstall, "install": a.install, "user_files": a.user_files}
        result = request(f"{HOSTCTL}/rea/mods/{a.mod}/play", "POST", body, auth, timeout=2 * a.seconds + 480)
    else:
        result = request(f"{HOSTCTL}/rea/mods/{a.mod}/{a.action}", "POST", {}, auth, timeout=300)
    for log in result.get("logs") or []:
        log["tail"] = log.get("tail", "")[-1500:]
    print(json.dumps(result, indent=1))
    if result.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
