"""Vibe Coding <-> Master Agent: new projects, agent sessions per project, goals and stats.

Each project's agent sessions are remembered in data/code-projects.json (keyed by source:path, so the
project folder itself stays untouched). Everything else comes from the Agent service: a session's
events already log which model each run used ("Master Agent run started · <model> ...") and every
file the agent wrote or patched, which is what the Stats tab adds up.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

import code_history
import code_projects

META_FILE = Path(__file__).parent / "data" / "code-projects.json"
# Where each workspace is mounted inside the Hermes container (~/.hermes is /opt/data there).
HERMES_PATHS = {"hermes": "/opt/data/workspace", "rocsy": "/opt/data/rocsy-workspace",
                "vibe": "/opt/data/rocsy-workspace/" + code_projects.VIBE_DIR}
SID = re.compile(r"[0-9a-f]{32}")
RUN_START = re.compile(r"^Master Agent run started · (.+?)(?: \(([^)]*)\))? · thinking")
FILE_TOOLS = {"write_file": "writes", "patch": "edits"}
IDLE_CAP = 300          # a gap longer than this inside a run counts as 5 min (a stalled stream, a long prompt read)
RUN_ENDS = ("Waiting for ", "Stopped", "Interrupted", "Failed")
_lock = threading.Lock()
_stats_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_was_busy: dict[str, bool] = {}   # per project, to notice an agent turn ending
FETCHES = 6                      # full session reads in flight at once (they carry every event)


# ---------------- per-project memory ----------------
def _load() -> dict[str, Any]:
    try:
        data = json.loads(META_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, Any]) -> None:
    tmp = META_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(META_FILE)


def _key(info: dict[str, Any]) -> str:
    return f"{info['source']}:{info['path']}"


def meta(info: dict[str, Any]) -> dict[str, Any]:
    m = _load().get(_key(info)) or {}
    sessions = [x for x in m.get("sessions") or [] if isinstance(x, dict) and SID.fullmatch(str(x.get("id", "")))]
    # Rocsy projects inside a chat folder (rocsy-workspace/<session id>/...) belong to that session.
    first = info["path"].split("/")[0]
    if info["source"] == "rocsy" and SID.fullmatch(first) and not any(x["id"] == first for x in sessions):
        sessions.insert(0, {"id": first, "started": info.get("updated", 0), "kind": "found"})
    return {**m, "sessions": sessions}


def _update(info: dict[str, Any], change) -> dict[str, Any]:
    with _lock:
        data = _load()
        entry = data.setdefault(_key(info), {"created": time.time(), "sessions": []})
        change(entry)
        _save(data)
        return entry


def remember_session(info: dict[str, Any], sid: str, kind: str) -> None:
    def change(entry: dict[str, Any]) -> None:
        entry.setdefault("sessions", [])
        if not any(x.get("id") == sid for x in entry["sessions"]):
            entry["sessions"].append({"id": sid, "started": time.time(), "kind": kind})
    _update(info, change)


def forget_project(info: dict[str, Any]) -> None:
    with _lock:
        data = _load()
        if data.pop(_key(info), None) is not None:
            _save(data)
    code_history.forget(info)


# ---------------- new projects ----------------
def create_project(name: str, idea: str, kind: str) -> dict[str, Any]:
    name = re.sub(r"\s+", " ", name or "").strip()
    # The folder name is the project's name in the library (dashes read as spaces), so keep its case.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:60]
    if not slug:
        raise HTTPException(400, "Give the project a name (letters or numbers)")
    root = code_projects.ROOTS["vibe"]
    root.mkdir(parents=True, exist_ok=True)
    folder = root / slug
    if any(child.name.lower() == slug.lower() for child in root.iterdir()):
        raise HTTPException(409, f"A project folder named “{slug}” already exists")
    folder.mkdir()
    code_projects.forget_list()
    kinds = {"web": "A web app: index.html at the project root (it runs in the Vibe Coding live preview).",
             "python": "A Python project.", "node": "A Node.js project.", "any": "Any language or stack that fits."}
    lines = [f"# {name}", "", (idea or "").strip() or "(No description yet: ask the user what to build.)", "",
             "## Stack", kinds.get(kind, kinds["any"]), "",
             "## Progress", "The Master Agent keeps PROGRESS.md up to date: what works, what's next."]
    (folder / "README.md").write_text("\n".join(lines) + "\n")
    info = code_projects.project_info(code_projects.project_id_for("vibe", slug))
    _update(info, lambda entry: entry.update(kind=kind, idea=(idea or "")[:4000]))
    return info


# ---------------- the agent ----------------
def hermes_path(info: dict[str, Any]) -> str:
    return f"{HERMES_PATHS[info['source']]}/{info['path']}"


def brief(info: dict[str, Any]) -> str:
    m = meta(info)
    where = hermes_path(info)
    stack = {"web": "Web app: the entry point is index.html at the project root."}.get(m.get("kind", ""), "")
    return (
        f"You are vibe coding with the user on the project “{info['name']}” from the Rocsy Vibe Coding library (/Code).\n"
        f"PROJECT FOLDER: {where}\nWork only inside this folder; create and edit files there with your file tools. "
        "Before you change anything, read README.md, PROGRESS.md (if any) and the existing files to see where the project stands, "
        "then continue from there instead of starting over.\n"
        + (f"{stack}\n" if stack else "")
        + "Live preview: Vibe Coding runs index.html from the project root in a sandboxed iframe (scripts only: no cookies, "
        "localStorage or same-origin requests, no access to Rocsy), so keep web projects self-contained with relative paths "
        "and no build step, or commit the built files next to index.html.\n"
        "At the end of every turn, update PROGRESS.md in the project folder: what works now, what you changed, what's next. "
        "Don't install system packages, start long-running servers or run models on the CPU.\n"
    )


async def _agent(client: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
    try:
        return await client.request(method, url, **kw)
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "The Master Agent service took too long to answer. Try again in a moment.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Can't reach the Master Agent service. Check that the bunny-agent container is running, "
                                 "then try again.") from exc


def _error(response: httpx.Response) -> HTTPException:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, dict):
        detail = detail.get("message")
    if response.status_code >= 500:
        return HTTPException(502, f"The Master Agent service hit an error{': ' + detail if detail else ''} "
                                  f"(HTTP {response.status_code}). Try again, or check its log on the Agent page.")
    return HTTPException(response.status_code, detail or f"The Master Agent service refused this (HTTP {response.status_code}).")


def goal_command(objective: str, budget: str, milestones: list[str]) -> str:
    steps = [re.sub(r"\s+", " ", x).strip()[:400] for x in milestones if x.strip()]
    if steps:
        if not 3 <= len(steps) <= 12:
            raise HTTPException(400, "Write 3 to 12 milestones (one per line), or let the agent split the goal")
        objective += ("\n\nMILESTONES (written by the user): register exactly these, in this order and wording, with "
                      "`$R goal parts \"" + " || ".join(steps) + "\"` before any other work. Don't re-split them unless "
                      "one proves impossible; then explain why first.")
    else:
        objective += ("\n\nSplit this goal into small milestones in build order (each ONE testable outcome you can prove "
                      "by running or reading the code), then finish and prove them one at a time.")
    # A number is set right after with /goal budget N; "" keeps the Agentic default budget.
    return ("/goal infinite " if budget == "infinite" else "/goal ") + objective


async def send(base: str, info: dict[str, Any], text: str, mode: str, budget: str, milestones: list[str],
               fresh: bool, model: dict[str, str] | None, session: str = "") -> dict[str, Any]:
    """Continue the project's latest session (or the one picked in the Sessions tab, or start one)
    with a coding message or a new goal."""
    m = meta(info)
    sid = "" if fresh else next((x["id"] for x in reversed(m["sessions"]) if x.get("kind") != "found"), "")
    picked = next((x for x in m["sessions"] if x["id"] == session), None) if session and not fresh else None
    if session and not fresh and not picked:
        raise HTTPException(404, "That session doesn't belong to this project")
    if picked:
        sid = picked["id"]
    choice = {"model": model} if model and model.get("model") is not None else {}
    text = text.strip()
    # Version history: the folder as it was before this message, so the turn can be undone.
    await asyncio.to_thread(code_history.auto_snapshot, info, ("Before goal: " if mode == "goal" else "Before: ")
                            + re.sub(r"\s+", " ", text)[:120], "before")
    async with httpx.AsyncClient(timeout=30) as client:
        if mode == "goal":
            objective = (brief(info) + "\nGOAL: " + text) if not sid else (f"Project “{info['name']}” in {hermes_path(info)}. GOAL: " + text)
            body_text = goal_command(objective, budget, milestones)
        else:
            # A session found by its file changes may never have seen the project brief.
            body_text = text if sid and not (picked and picked.get("kind") == "found") else brief(info) + "\nTHE USER ASKS:\n" + text
        if sid:
            r = await _agent(client, "POST", f"{base}/Agent/api/sessions/{sid}/message",
                             json={"text": body_text, "on_busy": "queue", **choice})
            if r.status_code == 404:
                sid = ""
                body_text = (goal_command(brief(info) + "\nGOAL: " + text, budget, milestones) if mode == "goal"
                             else brief(info) + "\nTHE USER ASKS:\n" + text)
            elif r.status_code >= 300:
                raise _error(r)
        if not sid:
            r = await _agent(client, "POST", f"{base}/Agent/api/sessions", json={"prompt": body_text, **choice})
            if r.status_code >= 300:
                raise _error(r)
            sid = r.json()["id"]
            remember_session(info, sid, "goal" if mode == "goal" else "work")
            title = ("Goal: " if mode == "goal" else "") + info["name"]
            await _agent(client, "PATCH", f"{base}/Agent/api/sessions/{sid}", json={"title": title[:80]})
        if mode == "goal" and budget.isdigit():
            await _agent(client, "POST", f"{base}/Agent/api/sessions/{sid}/message",
                         json={"text": f"/goal budget {max(1, min(100, int(budget)))}"})
    return {"session": sid, "url": f"/Agent/?s={sid}"}


async def goal_action(base: str, info: dict[str, Any], action: str, part: int | None) -> dict[str, Any]:
    state = await agent_state(base, info)
    sid = (state.get("goal") or {}).get("session")
    if not sid:
        raise HTTPException(404, "This project has no goal yet")
    text = {"pause": "/goal pause", "resume": "/goal resume", "clear": "/goal clear",
            "reopen": f"/goal reopen {part + 1}" if part is not None else "/goal reopen"}.get(action)
    if not text:
        raise HTTPException(400, "Unknown goal action")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await _agent(client, "POST", f"{base}/Agent/api/sessions/{sid}/message", json={"text": text})
        if r.status_code >= 300:
            raise _error(r)
    return await agent_state(base, info)


async def _session(client: httpx.AsyncClient, base: str, sid: str, events: bool) -> dict[str, Any] | None:
    r = await _agent(client, "GET", f"{base}/Agent/api/sessions/{sid}", params={} if events else {"since": 10 ** 9})
    if r.status_code == 404:
        return None
    if r.status_code >= 300:
        raise _error(r)
    return r.json()


async def agent_state(base: str, info: dict[str, Any]) -> dict[str, Any]:
    """The project's sessions (newest first) and its goal: the newest session that has one, else the last one seen."""
    m = meta(info)
    out, goal = [], None
    # Sessions found by their file changes are listed in the Sessions tab only: their goals may be about something else.
    entries = list(reversed([x for x in m["sessions"] if x.get("kind") != "found"]))
    async with httpx.AsyncClient(timeout=20) as client:
        heads = await asyncio.gather(*(_session(client, base, entry["id"], events=False) for entry in entries))
        for entry, s in zip(entries, heads):
            if not s:
                out.append({"id": entry["id"], "missing": True, "kind": entry.get("kind", "")})
                continue
            out.append({"id": s["id"], "title": s.get("title", ""), "state": s.get("state", ""), "stage": s.get("stage", ""),
                        "busy": bool(s.get("busy")), "updated": s.get("updated"), "kind": entry.get("kind", ""),
                        "url": f"/Agent/?s={s['id']}"})
            if goal is None and s.get("goal"):
                goal = {**s["goal"], "session": s["id"], "live": True}
    if goal:
        if goal != m.get("goal"):          # polled every few seconds: write only when the goal changed
            _update(info, lambda e: e.update(goal=goal))
    elif m.get("goal"):
        goal = {**m["goal"], "live": False, "status": "cleared" if m["goal"].get("status") == "active" else m["goal"].get("status")}
    busy = any(x.get("busy") for x in out)
    if _was_busy.get(_key(info)) and not busy:
        # An agent turn just ended: keep what it made as a version.
        await asyncio.to_thread(code_history.auto_snapshot, info, "After the agent's turn", "agent")
    _was_busy[_key(info)] = busy
    return {"sessions": out, "goal": goal, "busy": busy, "hermes_path": hermes_path(info),
            "total_sessions": len(m["sessions"])}


# ---------------- every session that worked on the project ----------------
# Paths the agent touched, from its tool lines (write_file/patch/read_file PATH), terminal commands and
# the messages (the Vibe Coding brief names the folder). Kept per session until its `updated` changes.
PATH_RE = re.compile(r"/opt/data/(?:rocsy-)?workspace/[^\s'\"`),;|&<>]+")
_paths_cache: dict[str, tuple[Any, set[str]]] = {}


def session_paths(s: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for e in s.get("events") or []:
        if e.get("kind") in ("tool", "terminal", "user", "assistant", "say", "system"):
            for blob in (e.get("text"), e.get("command")):
                if blob:
                    out.update(x.rstrip(".:/") for x in PATH_RE.findall(str(blob)))
    for t in s.get("turns") or []:
        out.update(x.rstrip(".:/") for x in PATH_RE.findall(str(t.get("text") or "")))
    return out


async def discover(base: str, info: dict[str, Any]) -> None:
    """Link every Agent session that touched this project's folder (kind "found")."""
    folder = hermes_path(info).rstrip("/")
    known = {x["id"] for x in meta(info)["sessions"]}
    found: list[tuple[str, float]] = []
    gate = asyncio.Semaphore(FETCHES)

    async def read(client: httpx.AsyncClient, sid: str, updated: Any) -> None:
        async with gate:
            s = await _session(client, base, sid, events=True)
        if s:
            _paths_cache[sid] = (updated, session_paths(s))
            # Its stats are needed right after for the same session: keep them too.
            _stats_cache[sid] = (updated, {**session_stats(s, folder), "folder": folder})

    async with httpx.AsyncClient(timeout=60) as client:
        r = await _agent(client, "GET", f"{base}/Agent/api/sessions", params={"limit": 0})
        if r.status_code >= 300:
            raise _error(r)
        heads = [h for h in r.json().get("sessions") or [] if SID.fullmatch(h.get("id", "")) and h["id"] not in known]
        stale = [h for h in heads if h["id"] not in _paths_cache or _paths_cache[h["id"]][0] != h.get("updated")]
        await asyncio.gather(*(read(client, h["id"], h.get("updated")) for h in stale))
        for head in heads:
            cached = _paths_cache.get(head["id"])
            if cached and any(path == folder or path.startswith(folder + "/") for path in cached[1]):
                found.append((head["id"], float(head.get("updated") or 0)))
    if found:
        def change(entry: dict[str, Any]) -> None:
            entry.setdefault("sessions", [])
            have = {x.get("id") for x in entry["sessions"]}
            entry["sessions"].extend({"id": sid, "started": t, "kind": "found"} for sid, t in found if sid not in have)
        _update(info, change)


async def _one_stats(client: httpx.AsyncClient, base: str, sid: str, updated: Any, folder: str,
                     gate: asyncio.Semaphore | None = None) -> dict[str, Any] | None:
    cached = _stats_cache.get(sid)
    if cached and cached[0] == updated and cached[1].get("folder") == folder:
        return cached[1]
    async with gate or asyncio.Semaphore(1):
        s = await _session(client, base, sid, events=True)
    if not s:
        return None
    one = {**session_stats(s, folder), "folder": folder}
    _stats_cache[sid] = (updated, one)
    _paths_cache[sid] = (updated, session_paths(s))
    return one


async def sessions_tab(base: str, info: dict[str, Any]) -> dict[str, Any]:
    """Every session of the project, newest first, with what it did here."""
    await discover(base, info)
    m = meta(info)
    folder = hermes_path(info)
    kinds = {x["id"]: x.get("kind", "") for x in m["sessions"]}
    ids = [x["id"] for x in m["sessions"]]
    out, missing = [], []
    async with httpx.AsyncClient(timeout=60) as client:
        r = await _agent(client, "GET", f"{base}/Agent/api/sessions/summary", params={"ids": ",".join(ids[-100:])})
        if r.status_code >= 300:
            raise _error(r)
        heads = [x for x in r.json().get("sessions") or [] if not x.get("missing")]
        missing = [x["id"] for x in r.json().get("sessions") or [] if x.get("missing")]
        gate = asyncio.Semaphore(FETCHES)
        ones = await asyncio.gather(*(_one_stats(client, base, x["id"], x.get("updated"), folder, gate) for x in heads))
        for x, one in zip(heads, ones):
            one = one or {"models": {}, "messages": 0}
            ms = one["models"]
            out.append({**{k: x.get(k) for k in ("id", "title", "state", "stage", "busy", "created", "updated", "jobs", "model",
                                                  "provider", "chosen", "last_user", "last_agent", "error")},
                        "kind": kinds.get(x["id"], ""), "url": f"/Agent/?s={x['id']}",
                        "seconds": round(sum(v["seconds"] for v in ms.values())), "runs": sum(v["runs"] for v in ms.values()),
                        "changes": sum(v["writes"] + v["edits"] for v in ms.values()), "messages": one.get("messages", 0),
                        "models": sorted(ms, key=lambda n: -ms[n]["seconds"]),
                        "files": sorted({f for v in ms.values() for f in v["files"]})[:12]})
    out.sort(key=lambda x: -(x.get("updated") or 0))
    return {"sessions": out, "missing": missing}


# ---------------- stats ----------------
def session_stats(s: dict[str, Any], folder: str) -> dict[str, Any]:
    """Working time, runs and file changes per model from one session's events."""
    events = sorted(s.get("events") or [], key=lambda e: e.get("i", 0))
    models: dict[str, dict[str, Any]] = {}
    model = None
    last_t = None
    first = events[0]["t"] if events else s.get("created")
    for e in events:
        kind, text, t = e.get("kind"), str(e.get("text") or ""), float(e.get("t") or 0)
        hit = RUN_START.match(text) if kind == "system" else None
        if hit:
            name = hit.group(1).strip()
            name = "Hermes default model" if name.startswith("Hermes default") else name
            model = models.setdefault(name, {"model": name, "provider": hit.group(2) or "", "runs": 0, "seconds": 0.0,
                                             "writes": 0, "edits": 0, "tools": 0, "files": {}})
            model["runs"] += 1
            last_t = t
            continue
        if model is None or last_t is None:
            continue
        model["seconds"] += min(max(t - last_t, 0), IDLE_CAP)
        last_t = t
        if kind == "tool":
            model["tools"] += 1
            tool, _, arg = text.partition(" ")
            path = arg.strip().split(" ")[0] if arg.strip() else ""
            if tool in FILE_TOOLS and path.startswith(folder.rstrip("/") + "/"):
                model[FILE_TOOLS[tool]] += 1
                rel = path[len(folder.rstrip("/")) + 1:]
                model["files"][rel] = model["files"].get(rel, 0) + 1
        if (kind in ("ok", "error") or (kind == "system" and text.startswith(RUN_ENDS))):
            last_t = None          # the run ended: what follows (renders, the user away) isn't agent time
    return {"id": s.get("id"), "title": s.get("title", ""), "first": first, "last": events[-1]["t"] if events else s.get("updated"),
            "models": models, "messages": sum(e.get("kind") == "user" for e in events)}


async def stats(base: str, info: dict[str, Any]) -> dict[str, Any]:
    await discover(base, info)
    m = meta(info)
    folder = hermes_path(info)
    ids = [x["id"] for x in m["sessions"]]
    async with httpx.AsyncClient(timeout=60) as client:
        # One summary call per 100 sessions tells which still exist and when each changed.
        heads: list[dict[str, Any]] = []
        for i in range(0, len(ids), 100):
            r = await _agent(client, "GET", f"{base}/Agent/api/sessions/summary", params={"ids": ",".join(ids[i:i + 100])})
            if r.status_code >= 300:
                raise _error(r)
            heads += r.json().get("sessions") or []
        live = [x for x in heads if not x.get("missing")]
        gate = asyncio.Semaphore(FETCHES)
        ones = await asyncio.gather(*(_one_stats(client, base, x["id"], x.get("updated"), folder, gate) for x in live))
    per_session = [one for one in ones if one]
    missing = len(ids) - len(per_session)
    models: dict[str, dict[str, Any]] = {}
    for one in per_session:
        for name, x in one["models"].items():
            agg = models.setdefault(name, {"model": name, "provider": x["provider"], "runs": 0, "seconds": 0.0, "writes": 0,
                                           "edits": 0, "tools": 0, "sessions": 0, "files": {}})
            agg["sessions"] += 1
            for k in ("runs", "seconds", "writes", "edits", "tools"):
                agg[k] += x[k]
            for f, n in x["files"].items():
                agg["files"][f] = agg["files"].get(f, 0) + n
    seconds = sum(x["seconds"] for x in models.values())
    changes = sum(x["writes"] + x["edits"] for x in models.values())
    tools = sum(x["tools"] for x in models.values())
    rows = []
    for x in models.values():
        share_changes = (x["writes"] + x["edits"]) / changes if changes else 0
        share_time = x["seconds"] / seconds if seconds else 0
        top = sorted(x["files"].items(), key=lambda kv: -kv[1])[:6]
        rows.append({**{k: v for k, v in x.items() if k != "files"}, "seconds": round(x["seconds"]),
                     "share_changes": round(share_changes, 4), "share_time": round(share_time, 4),
                     # Contribution: file changes when there are any, otherwise working time.
                     "share": round(share_changes if changes else share_time, 4),
                     "files_touched": len(x["files"]), "top_files": [{"path": f, "changes": n} for f, n in top]})
    rows.sort(key=lambda r: (-r["share"], -r["seconds"]))
    spans = [(one["first"], one["last"]) for one in per_session if one["first"] and one["last"]]
    return {"seconds": round(seconds), "sessions": len(per_session), "missing_sessions": missing,
            "runs": sum(x["runs"] for x in models.values()), "changes": changes, "tools": tools,
            "messages": sum(one["messages"] for one in per_session),
            "first": min((a for a, _ in spans), default=None), "last": max((b for _, b in spans), default=None),
            "created": m.get("created"), "models": rows,
            "session_list": [{"id": one["id"], "title": one["title"], "first": one["first"], "last": one["last"],
                              "seconds": round(sum(x["seconds"] for x in one["models"].values())),
                              "models": sorted(one["models"], key=lambda n: -one["models"][n]["seconds"])}
                             for one in sorted(per_session, key=lambda o: -(o["last"] or 0))]}
