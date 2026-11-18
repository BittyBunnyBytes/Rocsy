"""Project setup chats: the user talks a project into shape with the Master Agent.

A setup chat (session["setup"]) targets a new project ("project": "") or an existing one. Each reply may
carry a draft block; drafts merge into session["setup"]["draft"]. Nothing touches the project until the user
presses the chat's Create / Apply button (POST /api/sessions/{sid}/setup/apply in app.py).

The block uses ::LINES, not a ``` fence, so documents can hold code fences:

    ::PROJECT
    name: Neon Noir Shorts
    icon: film
    color: purple
    plugins: prompt_ledger, Shot list
    remove: Old notes.md
    ::INSTRUCTIONS
    Every chat ...
    ::DOCUMENT Style guide.md
    # Style guide ...
    ::PLUGIN Shot list
    icon: 🎬
    description: Keeps the shot list
    capture: shots
    feed_back: yes
    button: yes
    ---
    Write each shot as ...
    ::ENDPROJECT
"""
from __future__ import annotations

import json
import re
from typing import Any

BLOCK = re.compile(r"(?ms)^[ \t>*_]*::PROJECT\b[^\n]*\n(.*?)(^[ \t>*_]*::ENDPROJECT\b|\Z)")
FENCE_LINE = re.compile(r"^\s*(```|~~~)")
SECTION = re.compile(r"^[ \t]*::(INSTRUCTIONS|DOCUMENT|PLUGIN)\b[ \t]*(.*)$")
FIELD = re.compile(r"^\s*([a-z_]+)\s*:\s*(.*)$")
MAX_DOCS = 20
MAX_DOC_CHARS = 200_000
MAX_NEW_PLUGINS = 8
YES = {"yes", "y", "true", "on", "1"}


def _doc_name(name: str) -> str:
    name = re.sub(r"\s*[:/\\|]\s*", " - ", name.strip().strip("*_`\"'"))        # "Characters: Miso" → "Characters - Miso"
    name = re.sub(r"[^\w.()&,'!+ -]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")[:100]
    if not name:
        return ""
    return name if "." in name[-6:] else name + ".md"


def key(name: str) -> str:
    """Names match loosely: "strip_scripts", "Strip scripts" and "strip-scripts" are the same plugin."""
    return re.sub(r"[\s_-]+", " ", str(name)).strip().casefold()


def _label(name: str) -> str:
    """A plugin name the model wrote like an id (strip_scripts) reads as a name (Strip scripts)."""
    name = re.sub(r"\s+", " ", name).strip().strip("*_`\"'")[:60]
    if re.fullmatch(r"[a-z0-9]+(?:[_-][a-z0-9]+)+", name):
        name = re.sub(r"[_-]+", " ", name).capitalize()
    return name


def _capture(value: str) -> str:
    """The block name only: models write "strip: ```strip Title blocks are saved" or "```lyrics"."""
    m = re.match(r"[\s`]*([A-Za-z0-9][A-Za-z0-9_-]{0,23})", value or "")
    word = m[1].lower() if m else ""
    return "" if word in ("none", "no", "optional", "empty") else word


def _fields(lines: list[str]) -> dict[str, str]:
    out = {}
    for line in lines:
        if m := FIELD.match(line):
            out[m[1]] = m[2].strip()
    return out


def _names(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def parse(text: str) -> dict[str, Any] | None:
    """The last draft block in a reply, as a partial draft; None if there is none."""
    text = (text or "").replace("\r", "")
    found = list(BLOCK.finditer(text))
    if not found:
        return None
    m = found[-1]
    lines = m[1].split("\n")
    if not m[2]:
        # No ::ENDPROJECT (the model forgot it): read to the end, but if the block sits in a code fence, stop at
        # the fence that closes it rather than swallowing the prose after it. Fences inside documents pair up.
        before = text[:m.start()].rstrip("\n").rsplit("\n", 1)[-1]
        if FENCE_LINE.match(before):
            inside, cut = False, len(lines)
            for i, line in enumerate(lines):
                if not FENCE_LINE.match(line):
                    continue
                bare = line.strip() in ("```", "~~~")
                if inside:
                    inside = not bare            # a bare fence closes the document's code block
                elif bare:
                    cut = i                      # the fence around the whole block
                    break
                else:
                    inside = True                # ```lang opens a code block inside a document
            lines = lines[:cut]
    sections: list[tuple[str, str, list[str]]] = [("HEAD", "", [])]
    for line in lines:
        if m := SECTION.match(line.lstrip(">*_ \t")):          # also **::DOCUMENT x** from a model that bolds it
            sections.append((m[1], m[2].strip().rstrip("*_").strip(), []))
        else:
            sections[-1][2].append(line)
    part: dict[str, Any] = {"documents": {}, "new_plugins": {}}
    for kind, arg, body in sections:
        if kind == "HEAD":
            head = _fields(body)
            for key in ("name", "icon", "color"):
                if head.get(key):
                    part[key] = head[key]
            if "plugins" in head:
                part["plugins"] = _names(head["plugins"])
            if head.get("remove"):
                part["remove"] = _names(head["remove"])
        elif kind == "INSTRUCTIONS":
            part["instructions"] = "\n".join(body).strip()[:8000]
        elif kind == "DOCUMENT":
            name = _doc_name(arg)
            if name:
                part["documents"][name] = "\n".join(body).strip("\n")[:MAX_DOC_CHARS]
        elif kind == "PLUGIN":
            label = _label(arg)
            if not label:
                continue
            split = next((i for i, l in enumerate(body) if l.strip() == "---"), None)
            head = _fields(body if split is None else body[:split])
            instructions = "" if split is None else "\n".join(body[split + 1:]).strip()
            if split is None and "instructions" in head:
                instructions = head["instructions"]
            part["new_plugins"][label] = {
                "label": label, "icon": head.get("icon", "")[:8], "description": head.get("description", "")[:400],
                "instructions": instructions[:8000], "capture": _capture(head.get("capture", "")),
                "feed_back": head.get("feed_back", "yes").lower() in YES, "button": head.get("button", "yes").lower() in YES}
    return part


def merge(draft: dict[str, Any] | None, part: dict[str, Any], icons: tuple[str, ...], colors: tuple[str, ...]) -> dict[str, Any]:
    """Fields given replace; documents and plugins merge by name; `remove` drops draft items or marks project
    sources (by name or srcN) for deletion."""
    d = {"name": "", "icon": "", "color": "", "instructions": None, "plugins": None,
         "documents": {}, "new_plugins": {}, "remove": [], **(draft or {})}
    d["documents"], d["new_plugins"], d["remove"] = dict(d["documents"]), dict(d["new_plugins"]), list(d["remove"])
    if part.get("name"):
        d["name"] = re.sub(r"\s+", " ", part["name"]).strip().strip("\"'“”")[:80]
    if part.get("icon") in icons:
        d["icon"] = part["icon"]
    if part.get("color") in colors:
        d["color"] = part["color"]
    if part.get("instructions") is not None:
        d["instructions"] = part["instructions"]
    if part.get("plugins") is not None:
        d["plugins"] = part["plugins"]
    for name, text in part.get("documents", {}).items():
        if name in d["documents"] or len(d["documents"]) < MAX_DOCS:
            d["documents"][name] = text
            d["remove"] = [x for x in d["remove"] if x != name]
    for label, spec in part.get("new_plugins", {}).items():
        same = next((old for old in d["new_plugins"] if key(old) == key(label)), None)
        if same is not None:
            del d["new_plugins"][same]
        if same is not None or len(d["new_plugins"]) < MAX_NEW_PLUGINS:
            d["new_plugins"][label] = spec
    for name in part.get("remove", []):
        same = next((old for old in d["new_plugins"] if key(old) == key(name)), None)
        if same is not None:
            del d["new_plugins"][same]
            continue
        if name not in d["documents"] and _doc_name(name) in d["documents"]:
            name = _doc_name(name)
        d["documents"].pop(name, None)
        if name not in d["remove"]:          # it may already be a project source from an earlier apply
            d["remove"].append(name)
    before = {k: v for k, v in (draft or {}).items() if k != "version"}
    if draft and before == {k: v for k, v in d.items() if k != "version"}:
        return draft                         # nothing new (the same block seen twice): keep the version
    d["version"] = int(d.get("version") or 0) + 1
    return d


def write_files(d: dict[str, Any], folder) -> None:
    """The draft's documents and plugins as files in the chat's workspace, so the agent can re-read and edit
    them after its context was compressed."""
    folder.mkdir(parents=True, exist_ok=True)
    keep = {"index.md"}
    for name, text in d.get("documents", {}).items():
        keep.add(name)
        (folder / name).write_text(text)
    for label, spec in d.get("new_plugins", {}).items():
        fname = "plugin-" + re.sub(r"[^\w-]+", "-", label).strip("-")[:60] + ".md"
        keep.add(fname)
        (folder / fname).write_text(f"::PLUGIN {label}\nicon: {spec['icon']}\ndescription: {spec['description']}\n"
                                    f"capture: {spec['capture']}\nfeed_back: {'yes' if spec['feed_back'] else 'no'}\n"
                                    f"button: {'yes' if spec['button'] else 'no'}\n---\n{spec['instructions']}\n")
    (folder / "index.md").write_text(f"name: {d.get('name')}\nicon: {d.get('icon')}\ncolor: {d.get('color')}\n"
                                     f"plugins: {', '.join(d['plugins']) if d.get('plugins') is not None else '(unchanged)'}\n"
                                     f"remove: {', '.join(d.get('remove') or [])}\n\n::INSTRUCTIONS\n"
                                     f"{d['instructions'] if d.get('instructions') is not None else '(unchanged)'}\n")
    for old in folder.iterdir():
        if old.is_file() and old.name not in keep:
            old.unlink(missing_ok=True)


def summary(d: dict[str, Any]) -> str:
    bits = []
    if d.get("name"):
        bits.append(json.dumps(d["name"], ensure_ascii=False))
    if d.get("instructions"):
        bits.append("instructions")
    if d.get("documents"):
        n = len(d["documents"])
        bits.append(f"{n} document{'s' if n > 1 else ''}")
    if d.get("new_plugins"):
        n = len(d["new_plugins"])
        bits.append(f"{n} new plugin{'s' if n > 1 else ''}")
    if d.get("plugins") is not None:
        bits.append(f"{len(d['plugins'])} plugin{'s' if len(d['plugins']) != 1 else ''} on")
    if d.get("remove"):
        bits.append(f"remove {len(d['remove'])}")
    return ", ".join(bits) or "empty"


def prompt_block(project: dict[str, Any] | None, plugin_options: list[dict[str, Any]],
                 icons: tuple[str, ...], colors: tuple[str, ...]) -> str:
    """System-prompt part of a setup chat: stable between turns unless the project changes."""
    if project:
        sources = "".join(f"\n  - {a.get('ref')} \"{a.get('name')}\" · file {a.get('hermes_path')}"
                          for a in project.get("sources") or []) or " none"
        on = [pid for pid, v in (project.get("plugins") or {}).items() if isinstance(v, dict) and v.get("on")]
        target = (f"You are updating the existing project {json.dumps(project['name'], ensure_ascii=False)}. "
                  f"Now: icon {project.get('icon')}, colour {project.get('color')}; plugins on: {', '.join(on) or 'none'}; "
                  f"sources (read one with your file tools before you rewrite it; a ::DOCUMENT with the same name replaces it):{sources}\nIts instructions now:\n{(project.get('instructions') or '').strip() or '(none)'}\n")
    else:
        target = "You are setting up a NEW project; it doesn't exist yet.\n"
    plugin_lines = "\n".join(f"- {o['id']}: {o.get('label')} — {(o.get('description') or '')[:160]}" for o in plugin_options)
    return f"""
PROJECT SETUP CHAT. The user pressed "Set up with the agent": this chat exists to shape a Rocsy project with them.
A project groups chats and gives every chat in it shared instructions, source documents (files the agent reads before
answering) and plugins (extra instructions, plus blocks the agent writes that are saved and handed back every turn).
{target}
Talk with the user: ask about the goal, style, rules, recurring outputs and reference material, a few questions at a
time, and suggest things they haven't thought of. Write real, useful content: full instructions, complete documents
(style guides, character sheets, glossaries, templates, checklists ...) and plugins for things the chats should keep
track of. Don't use tools or studios to build the project, and don't render anything: nothing is created until the
user presses the "Create project" / "Apply changes" button under this chat. You may read files to look at
existing material.

Whenever the plan changes, end your reply with a draft block. Lines starting with :: must stand alone:
::PROJECT
name: <project name>
icon: <one of {', '.join(icons)}>
color: <one of {', '.join(colors)}>
plugins: <comma list of plugins that should be ON (the full list): ids from the list below, or ::PLUGIN names>
remove: <comma list of names to drop: draft documents/plugins, or existing sources by name or srcN>
::INSTRUCTIONS
<the instructions every chat in the project follows (the whole text)>
::DOCUMENT <file name, e.g. Style guide.md>
<the complete document in Markdown>
::PLUGIN <plugin display name, e.g. Strip archive>
icon: <one emoji>
description: <one line>
capture: <one lowercase word, or leave the line out>
feed_back: yes
button: yes
---
<the plugin's instructions to the agent>
::ENDPROJECT
A plugin's capture word (say lyrics) makes every ```lyrics Title block the agent writes in a project chat get saved.
Every part is optional and merges into the draft: give only what changed (a document or plugin with the same name
replaces the old one). The user sees the draft in a panel, so don't repeat it in prose; say briefly what changed.
Existing plugins you can turn on:
{plugin_lines or '(none)'}
"""


def context(d: dict[str, Any] | None, applied: int | None, target_name: str, folder: str = "") -> str:
    """Per-turn note: the draft so far (names only; the agent wrote the text itself)."""
    if not d:
        return "\nPROJECT DRAFT: nothing yet.\n"
    lines = [f"\nPROJECT DRAFT v{d.get('version')} ({'applied' if applied == d.get('version') else 'not applied yet'}"
             f"{' to ' + json.dumps(target_name, ensure_ascii=False) if target_name else ''}): {summary(d)}"]
    if d.get("name"):
        lines.append(f"name {d['name']}, icon {d.get('icon') or '-'}, colour {d.get('color') or '-'}")
    for name, text in d.get("documents", {}).items():
        lines.append(f"- document {name} ({len(text)} chars)")
    for label in d.get("new_plugins", {}):
        lines.append(f"- new plugin {label}")
    if d.get("plugins") is not None:
        lines.append("plugins on: " + (", ".join(d["plugins"]) or "none"))
    if d.get("remove"):
        lines.append("remove: " + ", ".join(d["remove"]))
    if folder:
        lines.append(f"The whole draft is in {folder}/ (index.md, each document, plugin-*.md): read a file before you "
                     "revise it instead of writing it from memory.")
    return "\n".join(lines) + "\n"
