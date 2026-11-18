"""Project plugins for the Master Agent: extras a project turns on in its Plugins tab (stored on the project as
{"plugins": {"<id>": {"on": true}}}; anything not listed is off).

prompt_ledger: Video Agentic's prompt-writing bookkeeping (ledger.py, a copy of Video/backend/ledger.py) for the
Master Agent's own chats. Every clip prompt the agent writes in a reply (::VIDEO ... ::END blocks, ```h3 blocks
labelled "Clip N") or saves with `$R prompt save` goes into a per-chat ledger, numbered across the whole chat, with
in-progress / written / verified states mirrored to workspace/<chat>/prompt-ledger/prompts/<state>/clip-NNN.md plus
progress.md and outline.md. Each turn the agent gets the ledger as a compact list, so a long session that has been
compressed or restarted reads its written prompts back instead of forgetting or rewriting them.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import h3lint
import ledger

PLUGINS: dict[str, dict[str, str]] = {
    "prompt_ledger": {
        "label": "Prompt ledger",
        "description": "Video Agentic's prompt-writing system: every clip prompt the Master Agent writes is saved, numbered "
                       "across the chat (in progress → written → verified, with an outline and standing directions) and "
                       "handed back each turn, so long sessions don't forget or rewrite their prompts.",
    },
}

LEDGER_DIR = "prompt-ledger"
BLOCK_LIMIT = 9000          # characters of ledger given to the agent each turn
LAST_FULL = 4000            # the newest clip in full (continuity)

_VIDEO = re.compile(r"(?ms)^[ \t]*::VIDEO\b([^\n]*)\n(.*?)^[ \t]*::END")    # replies glue text on: "::ENDGood call"
_FENCE = re.compile(r"(?s)```h3([^\n]*)\n(.*?)```")
_CLIP_LABEL = re.compile(r"(?i)\bclip\s*#?\s*(\d{1,3})\b")


def known(pid: str) -> bool:
    return pid in PLUGINS or pid in custom_plugins()


def plugin_on(project: dict[str, Any] | None, pid: str) -> bool:
    return known(pid) and bool(((project or {}).get("plugins") or {}).get(pid, {}).get("on"))


def clean_plugins(raw: Any) -> dict[str, Any]:
    """Only known plugins (built-in or custom) that are on are stored."""
    out: dict[str, Any] = {}
    for pid, value in (raw or {}).items() if isinstance(raw, dict) else ():
        if known(pid) and (value is True or (isinstance(value, dict) and value.get("on"))):
            out[pid] = {"on": True}
    return out


def options() -> list[dict[str, Any]]:
    builtin = [{"id": pid, "icon": "📜" if pid == "prompt_ledger" else "🧩", "button": pid == "prompt_ledger", **meta}
               for pid, meta in PLUGINS.items()]
    return builtin + [{**spec, "custom": True} for spec in custom_plugins().values()]


def enabled(project: dict[str, Any] | None) -> list[str]:
    return [pid for pid in ((project or {}).get("plugins") or {}) if plugin_on(project, pid)]


# --- custom plugins: made by the user in Project → Plugins, no code ---------------------------------------------
# A custom plugin is a spec, available to every project and turned on per project like the built-in ones:
#   instructions  added to the agent's context every turn while it's on;
#   capture       a code-fence name ("lyrics"): blocks the agent writes as ```lyrics Title ... ``` are saved to
#                 the chat's collection for this plugin (same title = a new version of that item);
#   feed_back     the saved items are handed back each turn (one-liners + the newest in full);
#   button        a button next to the paperclip opens the collection.
# The agent also gets `$R plugin <id> list | show N | save --title T --text ... | delete N`.

CUSTOM_PATH = Path(os.getenv("AGENT_DATA_DIR", "/data")) / "custom-plugins.json"
CUSTOM_MAX = 40
FENCE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")
RESERVED_FENCES = {"h3", "python", "bash", "sh", "json", "js", "javascript", "text", "md", "markdown", "html", "css", "yaml", "diff"}
FEED_LIMIT = 6000           # characters of a plugin's saved items given back each turn
NEWEST_FULL = 3000
_lock = threading.RLock()     # reentrant: create/update call custom_plugins() while holding it
_custom: dict[str, dict[str, Any]] | None = None


def custom_plugins() -> dict[str, dict[str, Any]]:
    global _custom
    with _lock:
        if _custom is None:
            try:
                data = json.loads(CUSTOM_PATH.read_text())
                _custom = {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
            except (OSError, ValueError):
                _custom = {}
        return _custom


def _save_custom() -> None:
    CUSTOM_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CUSTOM_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(custom_plugins(), ensure_ascii=False, indent=1))
    tmp.replace(CUSTOM_PATH)


def _clean_spec(raw: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = dict(current or {})
    if "label" in raw or current is None:
        label = re.sub(r"\s+", " ", str(raw.get("label") or "")).strip()[:60]
        if not label:
            raise ValueError("Give the plugin a name")
        spec["label"] = label
    if "icon" in raw or current is None:
        spec["icon"] = (str(raw.get("icon") or "").strip() or "🧩")[:8]
    if "description" in raw or current is None:
        spec["description"] = str(raw.get("description") or "").strip()[:400]
    if "instructions" in raw or current is None:
        spec["instructions"] = str(raw.get("instructions") or "").replace("\r\n", "\n").strip()[:8000]
    if "capture" in raw or current is None:
        fence = str(raw.get("capture") or "").strip().lower().lstrip("`")
        if fence and (not FENCE_NAME.fullmatch(fence) or fence in RESERVED_FENCES):
            raise ValueError("The block name is 1-24 lowercase letters, digits, - or _ (and not a code language like python)")
        spec["capture"] = fence
    for flag in ("feed_back", "button"):
        if flag in raw or current is None:
            spec[flag] = bool(raw.get(flag, flag == "feed_back"))
    if not (spec["instructions"] or spec["capture"]):
        raise ValueError("Give the plugin instructions, a block to save, or both")
    for pid, other in custom_plugins().items():
        if spec["capture"] and other.get("capture") == spec["capture"] and pid != spec.get("id"):
            raise ValueError(f"The plugin {other.get('label')} already saves ```{spec['capture']} blocks")
    spec["updated"] = time.time()
    return spec


def create_custom(raw: dict[str, Any]) -> dict[str, Any]:
    if len(custom_plugins()) >= CUSTOM_MAX:
        raise ValueError(f"At most {CUSTOM_MAX} custom plugins")
    spec = _clean_spec(raw)
    spec.update(id=f"c-{secrets.token_hex(4)}", created=time.time())
    with _lock:
        custom_plugins()[spec["id"]] = spec
        _save_custom()
    return spec


def update_custom(pid: str, raw: dict[str, Any]) -> dict[str, Any]:
    current = custom_plugins().get(pid)
    if not current:
        raise KeyError(pid)
    spec = _clean_spec(raw, current)
    with _lock:
        custom_plugins()[pid] = spec
        _save_custom()
    return spec


def delete_custom(pid: str) -> bool:
    with _lock:
        if custom_plugins().pop(pid, None) is None:
            return False
        _save_custom()
    return True


def _store(s: dict[str, Any], pid: str) -> dict[str, Any]:
    data = s.setdefault("plugin_data", {}).setdefault(pid, {})
    data.setdefault("items", {})
    data.setdefault("next", 1)
    return data


def items(s: dict[str, Any], pid: str) -> list[dict[str, Any]]:
    return sorted(((s.get("plugin_data") or {}).get(pid) or {}).get("items", {}).values(), key=lambda x: x["n"])


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "item"


def _write_files(s: dict[str, Any], pid: str, workspace: Path) -> None:
    root = workspace / s["id"] / "plugins" / pid
    root.mkdir(parents=True, exist_ok=True)
    keep = set()
    for item in items(s, pid):
        name = f"{item['n']:03d}-{_slug(item['title'])}.md"
        keep.add(name)
        (root / name).write_text(f"# {item['title']}\n\n{item['text']}\n")
    for old in root.glob("*.md"):
        if old.name not in keep and old.name != "index.md":
            old.unlink(missing_ok=True)
    spec = custom_plugins().get(pid, {})
    (root / "index.md").write_text(f"# {spec.get('icon', '')} {spec.get('label', pid)}\n\n"
                                   + "\n".join(f"{i['n']}. {i['title']}" for i in items(s, pid)) + "\n")


def save_item(s: dict[str, Any], pid: str, title: str, text: str, workspace: Path, source: str) -> tuple[dict[str, Any], str]:
    """Same title (case-insensitive) = a new version of that item. Returns (item, "new"|"updated"|"same")."""
    data = _store(s, pid)
    title = re.sub(r"\s+", " ", title).strip()[:120]
    match = next((i for i in data["items"].values() if title and i["title"].casefold() == title.casefold()), None)
    if match:
        if match["text"].strip() == text.strip():
            return match, "same"
        match.update(text=text.strip(), updated=time.time(), source=source, versions=match.get("versions", 1) + 1)
        _write_files(s, pid, workspace)
        return match, "updated"
    n = data["next"]
    data["next"] += 1
    item = {"n": n, "title": title or f"Item {n}", "text": text.strip(), "created": time.time(), "updated": time.time(), "source": source}
    data["items"][str(n)] = item
    _write_files(s, pid, workspace)
    return item, "new"


def capture_custom(s: dict[str, Any], text: str, project: dict[str, Any] | None, workspace: Path) -> list[str]:
    """Blocks of enabled custom plugins in a reply (```fence Title ... ```) -> their collections. Returns notes."""
    notes = []
    for pid in enabled(project):
        spec = custom_plugins().get(pid)
        if not spec or not spec.get("capture"):
            continue
        pattern = re.compile(r"(?s)```" + re.escape(spec["capture"]) + r"(?=[ \t\n])([^\n]*)\n(.*?)```")
        for m in pattern.finditer(text or ""):
            body = m.group(2).strip()
            if not body:
                continue
            item, how = save_item(s, pid, m.group(1).strip(), body, workspace, "reply")
            if how != "same":
                notes.append(f"{spec.get('icon', '')} {spec['label']}: {'saved' if how == 'new' else 'updated'} “{item['title']}”")
    return notes


def custom_context(s: dict[str, Any], project: dict[str, Any] | None, hermes_workspace: str) -> str:
    out = ""
    for pid in enabled(project):
        spec = custom_plugins().get(pid)
        if not spec:
            continue
        out += f"\nPLUGIN “{spec['label']}” (turned on in this project; id {pid}):\n"
        if spec.get("instructions"):
            out += spec["instructions"] + "\n"
        if spec.get("capture"):
            out += (f"Blocks you write as ```{spec['capture']} Title ...``` are saved to this plugin's collection automatically "
                    "(the same title saves a new version of that item). ")
        out += (f"Commands: `$R plugin {pid} list` · `$R plugin {pid} show N|all` · `$R plugin {pid} save --title \"T\" --text \"...\"` · "
                f"`$R plugin {pid} delete N`. Files: {hermes_workspace}/{s['id']}/plugins/{pid}/\n")
        saved = items(s, pid)
        if spec.get("feed_back") and saved:
            lines = [f"  {i['n']}. {i['title']} — {(i['text'].strip().splitlines() or [''])[0][:100]}" for i in saved]
            block = f"SAVED IN “{spec['label']}” ({len(saved)}):\n" + "\n".join(lines)
            while len(block) > FEED_LIMIT and len(lines) > 1:
                lines.pop(0)
                block = f"SAVED IN “{spec['label']}” ({len(saved)}, oldest left out):\n" + "\n".join(lines)
            newest = max(saved, key=lambda i: i["updated"])
            out += block + f"\nNewest ({newest['title']}) in full:\n{newest['text'][:NEWEST_FULL]}\n"
    return out


def custom_tool(s: dict[str, Any], args: dict[str, Any], project: dict[str, Any] | None, workspace: Path) -> dict[str, Any]:
    pid = str(args.get("plugin") or "")
    spec = custom_plugins().get(pid)
    if not spec:
        names = ", ".join(f"{v['label']} ({k})" for k, v in custom_plugins().items() if plugin_on(project, k))
        return {"text": f"error: no plugin {pid!r}" + (f"; this project has: {names}" if names else "")}
    if not plugin_on(project, pid):
        return {"text": f"error: the plugin {spec['label']} is off in this project (Project → Plugins)"}
    action = str(args.get("action") or "list")
    saved = items(s, pid)
    find = lambda key: next((i for i in saved if str(i["n"]) == key or i["title"].casefold() == key.casefold()), None)
    if action == "items":                       # the page's collection view
        return {"text": "", "plugin": spec, "items": saved}
    if action == "list":
        return {"text": "\n".join(f"{i['n']}. {i['title']} — {(i['text'].splitlines() or [''])[0][:120]}" for i in saved)
                or f"{spec['label']} has nothing saved in this chat yet."}
    if action == "show":
        target = str(args.get("target") or "all")
        chosen = saved if target in ("", "all") else [x for x in (find(t.strip()) for t in target.split(",")) if x]
        if not chosen:
            return {"text": f"error: nothing called {target!r} (see `$R plugin {pid} list`)"}
        return {"text": "\n\n".join(f"-- {i['n']}. {i['title']} --\n{i['text']}" for i in chosen)[:60000]}
    if action == "save":
        text = str(args.get("text") or "")
        title = str(args.get("title") or args.get("target") or "")
        if not text.strip():
            return {"text": "error: give the content with --text \"...\" or --file PATH"}
        item, how = save_item(s, pid, title, text, workspace, "save")
        return {"text": f"{'Saved' if how == 'new' else 'Updated' if how == 'updated' else 'Unchanged:'} {item['n']}. {item['title']}"}
    if action == "delete":
        item = find(str(args.get("target") or ""))
        if not item:
            return {"text": f"error: `$R plugin {pid} delete N` (see list)"}
        _store(s, pid)["items"].pop(str(item["n"]), None)
        _write_files(s, pid, workspace)
        return {"text": f"Deleted {item['n']}. {item['title']}"}
    return {"text": "error: action is list, show, save or delete"}


# --- prompt ledger ---------------------------------------------------------------------------------------------

def _attrs(header: str) -> dict[str, str]:
    return dict(re.findall(r"(\w+)=(\S+)", header))


def _seconds(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def put(s: dict[str, Any], n: int, status: str, prompt: str, **fields: Any) -> dict[str, Any]:
    """ledger.put, with the one-line summary taken from the prompt body (not the ::VIDEO header line)."""
    entry = ledger.put(s, n, status, prompt, **fields)
    entry["summary"] = ledger.summary(re.sub(r"(?s)^\s*::VIDEO[^\n]*\n|\n::END\s*$", "", prompt))
    return entry


def _other_names(s: dict[str, Any], n: int) -> dict[str, str]:
    """Speaker names the OTHER ledger clips tie to an ID ("Walt" -> "(S2)"); <Subject N> labels are per clip, left out."""
    names: dict[str, str] = {}
    for e in ledger.entries(s):
        if e["n"] == n or not e.get("prompt"):
            continue
        for name, speaker in list(h3lint.definition_speakers(e["prompt"]).items()) + list(h3lint.speaker_names(e["prompt"]).items()):
            if not name.startswith("<"):
                names.setdefault(name, speaker)
    return names


def lint(s: dict[str, Any], n: int, prompt: str) -> tuple[str, list[str], list[str]]:
    """h3lint.check_clip with the ledger's context (other clips' speakers, the picture table). Never raises: a checker
    bug must not cost the agent its save."""
    try:
        pictures = sum(1 for alias, _ in _ref_kinds(s) if alias.startswith("image"))
        return h3lint.check_clip(prompt, n, _other_names(s, n), pictures)
    except Exception:            # noqa: BLE001
        return prompt, [], []


def lint_put(s: dict[str, Any], n: int, status: str, prompt: str, **fields: Any) -> tuple[dict[str, Any], list[str], list[str]]:
    """Check (and apply Vid's safe fixes to) a clip, then save it with its open problems on the entry."""
    fixed, fixes, problems = lint(s, n, prompt)
    entry = put(s, n, status, fixed, **fields)
    entry["problems"], entry["fixes"] = problems, fixes
    return entry, fixes, problems


def report(n: int, fixes: list[str], problems: list[str]) -> str:
    return f"clip {n}: {h3lint.summarize(fixes, problems)}" if fixes or problems else ""


_WORD_NUMBERS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                            "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
_PROMISE = re.compile(r"(?i)\b(?:full|whole|complete|entire|revised|all)\b[^.\n]{0,24}?\b(\d{1,2}|" + "|".join(_WORD_NUMBERS)
                      + r")[\s‐-—-]*clips?\b|\b(\d{1,2})[\s‐-—-]*clip\s+plan\b|\bclips\s+1\s*[-–]\s*(\d{1,2})\b")


def truncation(text: str, found: list[dict[str, Any]]) -> tuple[list[int], str]:
    """A reply cut off mid-plan: ::VIDEO headers without their ::END, or a plan announced as N clips that brought fewer.
    Returns (clip numbers that didn't come through, note) or ([], "")."""
    complete = {c["n"] for c in found}
    headers = [int(x) for x in re.findall(r"(?m)^[ \t]*::VIDEO\b[^\n]*?\bid=(\d{1,3})\b", text or "")]
    unclosed = sorted(set(h for h in headers if h not in complete))
    promised = 0
    for m in _PROMISE.finditer(text or ""):
        raw = next(g for g in m.groups() if g)
        promised = max(promised, int(raw) if raw.isdigit() else _WORD_NUMBERS.get(raw.lower(), 0))
    missing = list(unclosed)
    if complete and promised > max(complete) and len(complete) < promised and promised <= ledger.OUTLINE_MAX:
        missing += [k for k in range(max(complete) + 1, promised + 1) if k not in complete]
    missing = sorted(set(missing))
    if not missing:
        return [], ""
    why = []
    if unclosed:
        why.append(f"::VIDEO id={', id='.join(map(str, unclosed))} ha{'ve' if len(unclosed) > 1 else 's'} no ::END line")
    if promised and len(complete) < promised:
        why.append(f"the reply announced {promised} clips but only clip{'s' if len(complete) > 1 else ''} {spans(sorted(complete))} came through whole")
    return missing, ("; ".join(why) + f" — the reply was probably cut off. Clip{'s' if len(missing) > 1 else ''} {spans(missing)} "
                     "were NOT saved: post them again as complete ::VIDEO … ::END blocks (a few per reply) or `$R prompt save N --file …`.")


def open_alert(s: dict[str, Any]) -> str:
    """The cut-off warning while any of its clips is still unsaved since it was raised."""
    alert = ledger.ledger(s).get("alert") or {}
    if not alert:
        return ""
    clips = ledger.ledger(s)["clips"]
    left = [k for k in alert.get("missing", []) if (clips.get(str(k)) or {}).get("updated", 0) <= alert.get("t", 0)]
    if not left:
        ledger.ledger(s).pop("alert", None)
        return ""
    return f"CUT-OFF REPLY: clip{'s' if len(left) > 1 else ''} {spans(left)} still missing since your reply was cut off ({alert.get('text', '')[:300]})"


def found_clips(text: str) -> list[dict[str, Any]]:
    """Clip prompts in a reply: ::VIDEO blocks (by id) and ```h3 blocks labelled "Clip N" (in the fence line or just
    above it). Unnumbered h3 blocks are skipped: there is no way to know which clip they are."""
    out: list[dict[str, Any]] = []
    for m in _VIDEO.finditer(text or ""):
        attrs = _attrs(m.group(1))
        if not (attrs.get("id") or "").isdigit():
            continue
        prompt = "::VIDEO" + m.group(1).rstrip() + "\n" + m.group(2).strip() + "\n::END"
        source = attrs.get("from")
        out.append({"n": int(attrs["id"]), "prompt": prompt, "seconds": _seconds(attrs.get("duration", "")),
                    "extends": int(source) if attrs.get("action", "").upper() == "EXTEND" and (source or "").isdigit() and int(source) > 0 else None})
    for m in _FENCE.finditer(text or ""):
        label = m.group(1).strip()
        number = _CLIP_LABEL.search(label)
        if not number:
            before = (text[max(0, m.start() - 240): m.start()].strip().splitlines() or [""])[-1]
            number, label = _CLIP_LABEL.search(before), before
        body = m.group(2).strip()
        if not number or not body:
            continue
        secs = re.search(r"(?i)(\d{1,2}(?:\.\d+)?)\s*(?:s|sec|secs|seconds)\b", label)
        out.append({"n": int(number.group(1)), "prompt": body, "seconds": _seconds(secs.group(1)) if secs else None, "extends": None})
    return [c for c in out if 1 <= c["n"] <= ledger.OUTLINE_MAX]


def folder(workspace: Path, sid: str) -> Path:
    return workspace / sid / LEDGER_DIR


def save_folders(s: dict[str, Any], workspace: Path) -> None:
    root = folder(workspace, s["id"])
    root.mkdir(parents=True, exist_ok=True)
    ledger.write_folders(root, s)


def capture(s: dict[str, Any], text: str, workspace: Path) -> dict[str, Any]:
    """Saves the clip prompts in a reply as 'written', checked (h3lint) with Vid's safe fixes applied.
    Returns {"saved", "locked" (verified clips the reply changed, left alone), "reports" (per-clip fixes/problems),
    "missing"/"note" (a reply cut off mid-plan)}."""
    saved, locked, reports = [], [], []
    found = found_clips(text)
    for clip in found:
        current = ledger.ledger(s)["clips"].get(str(clip["n"])) or {}
        if current.get("prompt", "").strip() in (clip["prompt"].strip(), lint(s, clip["n"], clip["prompt"])[0].strip()):
            continue
        if current.get("status") == "verified":
            locked.append(clip["n"])        # changing a verified clip takes an explicit `$R prompt save N`
            continue
        _, fixes, problems = lint_put(s, clip["n"], "written", clip["prompt"], seconds=clip["seconds"], extends=clip["extends"], source="reply")
        saved.append(clip["n"])
        if line := report(clip["n"], fixes, problems):
            reports.append(line)
    missing, note = truncation(text, found)
    if missing:
        ledger.ledger(s)["alert"] = {"t": time.time(), "missing": missing, "text": note}
    if saved or missing:
        save_folders(s, workspace)
    return {"saved": saved, "locked": locked, "reports": reports, "missing": missing, "note": note}


def spans(numbers: list[int]) -> str:
    out, numbers = [], sorted(set(numbers))
    i = 0
    while i < len(numbers):
        j = i
        while j + 1 < len(numbers) and numbers[j + 1] == numbers[j] + 1:
            j += 1
        out.append(str(numbers[i]) if i == j else f"{numbers[i]}-{numbers[j]}")
        i = j + 1
    return ", ".join(out)


def parse_clips(value: str) -> list[int]:
    """"3", "3,5", "4-7" -> clip numbers."""
    out: list[int] = []
    for part in re.split(r"\s*,\s*", str(value or "").strip()):
        if m := re.fullmatch(r"(\d{1,3})\s*-\s*(\d{1,3})", part):
            a, b = int(m.group(1)), int(m.group(2))
            out += list(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            out.append(int(part))
    return [n for n in dict.fromkeys(out) if 1 <= n <= ledger.OUTLINE_MAX]


def _line(entry: dict[str, Any]) -> str:
    secs = entry.get("seconds")
    flag = f" ⚠{len(entry['problems'])}" if entry.get("problems") else ""
    return (f"  {entry['n']}. [{entry.get('status')}{', being edited' if entry.get('was_verified') and entry.get('status') != 'verified' else ''}]"
            + (f" ({secs:g} s)" if isinstance(secs, (int, float)) else "") + flag + f" {entry.get('summary') or ''}")


def _prompts(s: dict[str, Any]) -> dict[int, str]:
    return {e["n"]: e["prompt"] for e in ledger.entries(s) if e.get("prompt")}


def _ref_kinds(s: dict[str, Any]) -> list[tuple[str, str]]:
    """The picture table as (alias, ref): images, videos and audio numbered separately in --ref order (image1, video1,
    image2, ...), the way map_ref_names and Vid number them."""
    kinds = {a["ref"]: a.get("kind", "image") for a in s.get("attachments") or []}
    counts: dict[str, int] = {}
    out = []
    for ref in ledger.ledger(s).get("refs") or []:
        kind = {"video": "video", "audio": "audio"}.get(kinds.get(ref, "image"), "image")
        counts[kind] = counts.get(kind, 0) + 1
        out.append((f"{kind}{counts[kind]}", ref))
    return out


def picture_lines(s: dict[str, Any]) -> list[str]:
    """The picture table: image N = the Nth picture in --ref order (videoN for video pieces), with the attachment's name
    and the clips that select it."""
    names = {a["ref"]: a.get("name", "") for a in s.get("attachments") or []}
    uses = h3lint.ref_uses(_prompts(s))
    table = _ref_kinds(s)
    out = []
    for alias, ref in table:
        clips = uses.get(alias, [])
        label = {"image": "Picture", "video": "Video", "audio": "Audio"}[re.sub(r"\d+$", "", alias)]
        number = re.sub(r"\D", "", alias)
        out.append(f"  {alias} (<{label} {number}>) = {ref}" + (f" \"{names[ref]}\"" if names.get(ref) else "")
                   + (f" · clips {spans(clips)}" if clips else " · not used yet"))
    known = {alias for alias, _ in table}
    beyond = sorted((k for k in uses if k not in known), key=lambda k: (re.sub(r"\d+$", "", k), int(re.sub(r"\D", "", k) or 0)))
    if table and beyond:
        out.append(f"  ⚠ clips select {', '.join(beyond)}, which this table doesn't have")
    return out


def checks_text(s: dict[str, Any], limit: int = 3000) -> str:
    """Open problems of the ledger clips plus whole-film checks (one ID per speaker), for the turn context."""
    lines = []
    for e in ledger.entries(s):
        for problem in e.get("problems") or []:
            lines.append(f"  - {problem}" + (" (verified clip: only if the user asks)" if e.get("status") == "verified" else ""))
    _, speaker_problems = h3lint.speaker_table(_prompts(s))
    lines += [f"  - {p}" for p in speaker_problems]
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n  - … (`$R prompt check` lists them all)"
    return text


def check_all(s: dict[str, Any], numbers: list[int], workspace: Path) -> str:
    """`$R prompt check [N]`: re-check clips (after a fix, or when the picture table changed), saving Vid's safe fixes."""
    book = ledger.ledger(s)
    out = []
    for n in numbers or [e["n"] for e in ledger.entries(s)]:
        e = book["clips"].get(str(n))
        if not e or not e.get("prompt"):
            continue
        fixed, fixes, problems = lint(s, n, e["prompt"])
        if fixed.strip() != e["prompt"].strip() and e.get("status") != "verified":
            e.update(prompt=fixed, updated=time.time())
        else:
            fixes = [] if e.get("status") == "verified" else fixes
        e["problems"], e["fixes"] = problems, fixes
        out.append(report(n, fixes, problems) or f"clip {n}: OK")
    save_folders(s, workspace)
    table, speaker_problems = h3lint.speaker_table(_prompts(s))
    if table:
        out.append("speakers: " + " · ".join(f"{sp} {', '.join(names[:2])}" for sp, names in table.items()))
    out += [f"whole film: {p}" for p in speaker_problems]
    return "\n".join(out) or "The ledger has no clips to check."


def overview_text(s: dict[str, Any], limit: int | None = None) -> str:
    items = ledger.entries(s)
    book = ledger.ledger(s)
    info = ledger.overview(s)
    c = info["counts"]
    lines = [f"Clips: {info['total']} · verified {c['verified']} · written {c['written'] + c['done']} · in progress {c['writing']}"
             + (f" · not written yet {c['todo']}" if c["todo"] else "") + (f" · strip {info['strip']}" if info["strip"] else "")]
    lines += [_line(e) for e in items]
    outline = book.get("outline") or []
    if outline:
        lines.append(f"OUTLINE ({len(outline)} clips, about {round(sum(x['seconds'] for x in outline))} s):")
        lines += [f"  {i}. ({x['seconds']} s) {x['beat']}" for i, x in enumerate(outline, int(book.get('outline_start') or 0) + 1)]
    if standing := ledger.directions_text(s, 1):
        lines.append(standing)
    text = "\n".join(lines)
    if limit and len(text) > limit:      # the oldest one-liners go first
        head, rest = lines[:1], lines[1:]
        while rest and len("\n".join(head + ["  (earlier clips left out: `$R prompt list`)"] + rest)) > limit:
            rest.pop(0)
        text = "\n".join(head + ["  (earlier clips left out: `$R prompt list`)"] + rest)
    return text


def handoff(s: dict[str, Any], selection: str) -> tuple[dict[str, Any] | None, str]:
    """The ledger clips to hand to Vid's Agentic mode (`$R agentic --ledger 1-12`): (handover, error)."""
    book = ledger.ledger(s)
    written = [e for e in ledger.entries(s) if e.get("prompt")]
    if not written:
        return None, "the prompt ledger has no clips to hand over yet"
    numbers = [e["n"] for e in written] if str(selection).strip().lower() in ("", "all", "1") and len(written) <= 24 \
        else parse_clips(selection)
    if str(selection).strip().lower() == "all" and len(written) > 24:
        return None, f"the ledger has {len(written)} clips; Vid takes 24 per run: hand over 1-24, then continue with --resume"
    chosen = [book["clips"][str(n)] for n in numbers if str(n) in book["clips"] and book["clips"][str(n)].get("prompt")]
    missing = [n for n in numbers if n not in {e["n"] for e in chosen}]
    if missing:
        return None, f"not in the ledger: {spans(missing)} (see `$R prompt list`)"
    if len(chosen) > 24:
        return None, "Vid takes at most 24 clips per run: hand over a range of 24 or fewer"
    clips = [{"n": e["n"], "prompt": e["prompt"], "seconds": e.get("seconds"),
              "approved": e.get("status") == "verified"} for e in sorted(chosen, key=lambda e: e["n"])]
    outline = [{"seconds": x.get("seconds"), "beat": x.get("beat")} for x in book.get("outline") or []]
    return {"clips": clips, "outline": outline, "directions": list(book.get("directions") or []), "source": "master-agent"}, ""


_REF_FIELD = re.compile(r"\b(refs|first|last)=(\S+)")


def map_ref_names(handover: dict[str, Any], aliases_in_order: list[str]) -> list[str]:
    """Clips written with pictures numbered by --ref position (image5 = the 5th --ref) while Vid's own aliases differ
    (Vid numbers uploads across the whole session: image1-4, image6-16): rewrite their refs=/first=/last= names to the
    real aliases. Left alone when every name already is a real alias. Returns notes."""
    real = set(aliases_in_order)
    used = {name for c in handover["clips"] for m in _REF_FIELD.finditer(c["prompt"].split("\n", 1)[0])
            for name in m.group(2).split(",") if name not in ("-", "")}
    if not used or used <= real:
        return []
    by_position: dict[str, str] = {}
    for index, alias in enumerate(aliases_in_order, 1):
        kind = re.sub(r"\d+$", "", alias) or "image"
        position = sum(1 for a in aliases_in_order[:index] if re.sub(r"\d+$", "", a) == kind)
        by_position[f"{kind}{position}"] = alias
    unknown = sorted(n for n in used if n not in by_position)
    if unknown:
        raise ValueError(f"the clips name {', '.join(unknown)}, but only {len(aliases_in_order)} pictures were passed with "
                         "--ref (the clips number pictures by their --ref position)")

    def fix_header(line: str) -> str:
        return _REF_FIELD.sub(lambda m: f"{m.group(1)}=" + ",".join(by_position.get(n, n) for n in m.group(2).split(",")), line)
    label_kind = {"Picture": "image", "Video": "video", "Audio": "audio"}
    kind_label = {v: k for k, v in label_kind.items()}

    def fix_label(m: re.Match[str]) -> str:     # <Picture 5> -> <Picture 6>, one pass (never mapped twice)
        alias = by_position.get(f"{label_kind[m.group(1)]}{m.group(2)}")
        parts = re.fullmatch(r"([a-z]+)(\d+)", alias or "")
        return f"<{kind_label.get(parts.group(1), m.group(1))} {parts.group(2)}>" if parts else m.group(0)
    for clip in handover["clips"]:
        head, _, rest = clip["prompt"].partition("\n")
        rest = re.sub(r"<(Picture|Video|Audio) (\d+)>", fix_label, rest)
        clip["prompt"] = fix_header(head) + ("\n" + rest if rest else "")
    changed = sorted((k, v) for k, v in by_position.items() if k != v and k in used)
    return [f"pictures matched by --ref order: " + ", ".join(f"{k}→{v}" for k, v in changed)] if changed else []


def context_block(s: dict[str, Any], hermes_folder: str) -> str:
    """The per-turn reminder: how the ledger works, then the clips it holds."""
    rules = (f"\nPROMPT LEDGER (a plugin of this project): the clip prompts of this chat are kept here, not in your memory. "
             "Clip prompts you write in a reply are saved automatically: ::VIDEO id=N ... ::END blocks by their id, and ```h3 "
             "blocks labelled with their clip number (```h3 Clip 4 (12 s)). Number clips across the WHOLE chat (a second "
             "part continues at Clip 12, it doesn't restart at 1). Commands: `$R prompt list` · `$R prompt show 3,5-7` (full "
             "text) · `$R prompt save N --file PATH|--text \"...\" [--status in-progress|written|verified] [--seconds 12]` · "
             "`$R prompt status 3-5 verified` (when the user approves them or keeps their render) · `$R prompt delete N` · "
             "`$R prompt outline [--file PATH|--text \"1. (12 s) beat\\n2. ...\"]` · `$R prompt direction \"lasting change\" --from N`. "
             "RULES: before revising, continuing, rendering or re-posting clips, `$R prompt show` them and work from the saved "
             "text; after a compression or restart, read the ledger instead of rewriting from memory. VERIFIED clips are final: "
             "never rewrite or re-plan them unless the user asks for that clip. Standing directions apply to every clip from "
             f"their number on. Files: {hermes_folder}/prompts/<state>/clip-NNN.md, progress.md, outline.md.\n"
             "RENDERING LEDGER CLIPS: hand them to Vid's Agentic mode with `$R agentic --ledger 1-12 [--ref att1,att2,...]` "
             "(at most 24 per run; the next range continues the same video with `--resume agN`). Never paste the plan into "
             "the prompt: the clips go over as the ledger itself and are rendered exactly as saved (add `--review` only if the "
             "user wants Vid's planner to review them). Save any change with `$R prompt save` BEFORE handing clips over. If Vid "
             "refuses a handover over its prompt rules (e.g. dialogue needs the speaker's ID: `Walt (S1) says: <d>...</d>`), fix "
             "those clips with `$R prompt save` and hand them over again, or use `--review` so Vid's planner repairs them.\n"
             "CHECKS: every clip is checked against Vid's rules when it is saved (header, six Reference sections, <Picture N> "
             "labels vs refs=, a speaker ID beside every <d> line, words per second, length). Vid's safe fixes are applied by "
             "themselves (speaker IDs taken from subject_definitions, end punctuation); what is left is listed under OPEN "
             "PROBLEMS each turn and after every save. Fix them with `$R prompt save` before handing clips over; "
             "`$R prompt check [N]` re-checks. PICTURES: `$R prompt refs att1,att2,...` fixes the picture order for the whole "
             "chat (image1 = the first, image2 = the second, ...; write <Picture N> / refs=imageN by that table); "
             "`$R agentic --ledger` uses it when you leave out --ref.\n")
    if not ledger.entries(s) and not ledger.ledger(s).get("outline") and not ledger.ledger(s).get("directions") \
            and not ledger.ledger(s).get("refs"):
        return rules + "The ledger is empty so far.\n"
    text = rules + "LEDGER NOW:\n" + overview_text(s, BLOCK_LIMIT) + "\n"
    if alert := open_alert(s):
        text += alert + "\n"
    table, _ = h3lint.speaker_table(_prompts(s))
    if table:
        text += "SPEAKERS (one ID each for the whole film): " + " · ".join(f"{sp[1:-1]} {', '.join(n[:2])}" for sp, n in table.items()) + "\n"
    if pictures := picture_lines(s):
        text += "PICTURES (`$R prompt refs`):\n" + "\n".join(pictures) + "\n"
    if checks := checks_text(s):
        text += "OPEN PROBLEMS (fix with `$R prompt save` before rendering):\n" + checks + "\n"
    newest = ledger.entries(s)[-1] if ledger.entries(s) else None
    if newest and newest.get("prompt"):
        text += f"Newest clip {newest['n']} in full (continuity):\n{newest['prompt'][:LAST_FULL]}\n"
    return text


def _outline_items(text: str) -> list[dict[str, Any]]:
    items = []
    for raw in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*]\s*)?(?:\d{1,3}[.)]\s*)?", "", raw).strip()
        if not line:
            continue
        secs = 10.0
        if m := re.match(r"\(?\s*(\d{1,2}(?:\.\d+)?)\s*(?:s|sec|secs|seconds)?\s*\)?\s*[|:\-–]?\s*", line):
            if m.group(0).strip() and (")" in m.group(0) or "|" in m.group(0) or re.search(r"s\b", m.group(0))):
                secs, line = float(m.group(1)), line[m.end():].strip()
        if line:
            items.append({"seconds": secs, "beat": line})
    return items


def tool(s: dict[str, Any], args: dict[str, Any], workspace: Path, hermes_folder: str) -> str:
    """`$R prompt ACTION [TARGET] [VALUE] [--text ...] [--status ...] [--seconds ...] [--from N]`."""
    action = str(args.get("action") or "list")
    target, value = str(args.get("target") or ""), str(args.get("value") or "")
    text = str(args.get("text") or "")
    book = ledger.ledger(s)
    if action == "list":
        if not ledger.entries(s) and not book.get("outline"):
            return "The prompt ledger is empty. Write clips as ::VIDEO blocks or ```h3 Clip N blocks, or `$R prompt save N --text \"...\"`."
        return overview_text(s, 30000) + f"\nFiles: {hermes_folder}"
    if action == "show":
        numbers = parse_clips(target) or [e["n"] for e in ledger.entries(s)]
        parts = []
        for n in numbers:
            e = book["clips"].get(str(n))
            parts.append(f"-- clip {n}: not in the ledger --" if not e else
                         f"-- clip {n} [{e.get('status')}]" + (f" ({e['seconds']:g} s)" if isinstance(e.get("seconds"), (int, float)) else "")
                         + f" --\n{e.get('prompt', '')}")
        out = "\n\n".join(parts)
        return out if len(out) <= 60000 else out[:60000] + f"\n… (cut; show fewer clips or read {hermes_folder}/prompts/)"
    if action == "save":
        status = str(args.get("status") or value or "written")
        if status not in ledger.STATUSES:
            return "error: --status is in-progress, written or verified"
        if not text.strip():
            return "error: give the prompt with --text \"...\" or --file PATH"
        numbers = parse_clips(target)
        if not numbers:                         # several ::VIDEO / h3 Clip blocks in one text
            clips = found_clips(text)
            if not clips:
                cut = truncation(text, [])[1]
                return ("error: " + cut) if cut else \
                    "error: give the clip number (`$R prompt save 4 --text ...`) or ::VIDEO id=N blocks / ```h3 Clip N blocks"
            lines = []
            for c in clips:
                _, fixes, problems = lint_put(s, c["n"], status, c["prompt"], seconds=c["seconds"], extends=c["extends"], source="save")
                if line := report(c["n"], fixes, problems):
                    lines.append(line)
            save_folders(s, workspace)
            missing, note = truncation(text, clips)
            return (f"Saved clips {spans([c['n'] for c in clips])} as {status}." + "".join(f"\n{x}" for x in lines)
                    + (f"\nWARNING: {note}" if missing else "") + ("" if lines else " Checks: OK."))
        if len(numbers) > 1:
            return "error: save one clip at a time, or leave the number out and pass ::VIDEO / ```h3 Clip N blocks"
        try:
            seconds = float(args["seconds"]) if args.get("seconds") not in (None, "") else None
        except (TypeError, ValueError):
            return "error: --seconds must be a number"
        if text.lstrip().startswith("::VIDEO") and "::END" not in text:
            return f"error: the text starts a ::VIDEO block but has no ::END line (cut off?): nothing saved for clip {numbers[0]}"
        _, fixes, problems = lint_put(s, numbers[0], status, text.strip(), seconds=seconds, source="save")
        save_folders(s, workspace)
        line = report(numbers[0], fixes, problems)
        return f"Saved clip {numbers[0]} as {status}." + (f"\n{line}" if line else " Checks: OK.")
    if action == "check":
        return check_all(s, parse_clips(target), workspace)
    if action == "refs":
        refs = [r.strip().lower() for r in re.split(r"[,\s]+", target + " " + value + " " + text) if r.strip()]
        if not refs:
            lines = picture_lines(s)
            return ("PICTURES (image N = the Nth --ref):\n" + "\n".join(lines)) if lines else \
                "No picture table yet: `$R prompt refs att1,att2,att14,...` (image1 = the first, image2 = the second, ...)."
        known = {a["ref"] for a in s.get("attachments") or []}
        unknown = [r for r in refs if re.fullmatch(r"att\d+", r) and r not in known]
        if unknown:
            return f"error: no attachment {', '.join(unknown)} in this chat (`$R attachments`)"
        book["refs"] = refs[:32]
        check_all(s, [], workspace)              # picture range checks depend on the table
        return f"Picture table set ({len(book['refs'])}):\n" + "\n".join(picture_lines(s))
    if action == "status":
        numbers, status = parse_clips(target), str(args.get("status") or value)
        if not numbers or status not in ledger.STATUSES:
            return "error: `$R prompt status 3,5-7 verified` (in-progress, written or verified)"
        missing = [n for n in numbers if str(n) not in book["clips"]]
        for n in numbers:
            if str(n) in book["clips"]:
                e = book["clips"][str(n)]
                if e.get("status") == "verified" and status != "verified":
                    # ledger.put keeps a verified clip verified while its prompt is unchanged (right for
                    # Video's automatic drafts); an explicit status command really takes it back.
                    e.pop("rendered", None)
                    e.update(status=status, was_verified=True, updated=time.time())
                else:
                    put(s, n, status, e.get("prompt", ""))
        save_folders(s, workspace)
        done = [n for n in numbers if n not in missing]
        return (f"Clips {spans(done)} are now {status}." if done else "") + (f" Not in the ledger: {spans(missing)}." if missing else "")
    if action == "delete":
        numbers = parse_clips(target)
        if not numbers:
            return "error: `$R prompt delete 4` (or 4-6)"
        gone = [n for n in numbers if book["clips"].pop(str(n), None) is not None]
        save_folders(s, workspace)
        return f"Deleted clips {spans(gone)}." if gone else "Nothing to delete."
    if action == "outline":
        body = text or (target + (" " + value if value else ""))
        if not body.strip():
            outline = book.get("outline") or []
            return ("No outline yet." if not outline else
                    "\n".join(f"{i}. ({x['seconds']} s) {x['beat']}" for i, x in enumerate(outline, 1)))
        items = _outline_items(body)
        if not items:
            return "error: one beat per line, e.g. \"1. (12 s) Walt grills a motherboard at the barbecue\""
        clean = ledger.set_outline(s, items, None, max_seconds=15, min_seconds=1)
        book["outline_start"] = 0                # the Master Agent numbers clips from 1 across the chat
        save_folders(s, workspace)
        return f"Outline set: {len(clean)} clips, about {round(sum(x['seconds'] for x in clean))} s."
    if action == "direction":
        words = text or target
        if not words.strip():
            return "error: `$R prompt direction \"from now on Jesse wears the red beanie\" --from 5`"
        start = parse_clips(str(args.get("from") or value or "1")) or [1]
        item = ledger.add_direction(s, start[0], words)
        save_folders(s, workspace)
        return f"Standing direction from clip {item['from']} on: {item['text']}"
    return "error: action is list, show, save, status, delete, outline, direction, check or refs"
