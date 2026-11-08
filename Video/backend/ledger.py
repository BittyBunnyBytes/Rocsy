"""Long-haul Agentic bookkeeping (50 connected clips, a 5-minute film, ...).

Every Agentic session keeps a prompt ledger: one entry per clip, numbered across the whole session (run 2's clip 1
is overall clip 11 when run 1 kept 10), moving through three states that are mirrored to folders next to the
session's prompts.md:

    prompts/in-progress/clip-NNN.md   drafted by Hermes, not yet past the plan checks and review
    prompts/written/clip-NNN.md       validated and reviewed: queued, rendering, or rendered but not yet final
    prompts/verified/clip-NNN.md      its final render was kept: locked, never rewritten or re-planned

plus progress.md (one line per clip) and, for a long request, outline.md (one line per planned clip).

A long request gets an outline once, then is made in batches: each run writes only its next BATCH_CLIPS clips from
the outline while the planner sees finished clips as one line each (plus the last one in full, for continuity),
so a 50-clip film costs each planning pass about the same context as a 10-clip one, and a pause, a resume or a
new run picks up at the first clip the ledger hasn't verified.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

BATCH_CLIPS = 10            # clips one long-haul run writes and renders
LONG_CLIPS = 12             # more clips than this is a long-haul request
LONG_SECONDS = 150          # ... or a longer finished video than this
OUTLINE_MAX = 200           # clips an outline may hold
STATUSES = ("in-progress", "written", "verified")
CONTEXT_LIMIT = 40_000      # characters of ledger context given to the planner
PENDING_FULL = 6            # unfinished clips given to the planner in full

_COUNT = re.compile(r"(?i)\b(\d{1,3})\s+(?:(?:continuous|connected|consecutive|separate|short|long|more|new|different)\s+)*"
                    r"(?:video\s+)?(?:clips|videos|shots|scenes|parts|segments|episodes|chapters)\b")
_VIDEO_WORDS = r"(?:video|film|movie|short|story|episode|cut|clip|animation|series|montage|music video|reel|trailer|documentary|vlog|sequence|edit)"
_LENGTH = r"(\d+(?:\.\d+)?)\s*(?:-\s*)?(?:{unit})\b"
# "a 5 minute video", "5-minute long film", "make it 5 minutes long", "a video (about) 5 minutes long", "runtime 300 seconds"
_DURATION_FORMS = [r"{length}\s*(?:-\s*)?(?:long\s+)?" + _VIDEO_WORDS, r"{length}\s+(?:long|in length|total|in total|runtime)",
                   _VIDEO_WORDS + r"[^.\n]{{0,40}}?\b(?:of|lasting|running|about|around|roughly|approximately|for)\s+(?:about\s+)?{length}",
                   r"(?:runtime|length|duration)\s*(?:of|:|is|=)?\s*(?:about\s+)?{length}"]
_MINUTES = [re.compile("(?i)" + form.format(length=_LENGTH.format(unit="minutes?|mins?"))) for form in _DURATION_FORMS]
_SECONDS = [re.compile("(?i)" + form.format(length=_LENGTH.format(unit="seconds?|secs?"))) for form in _DURATION_FORMS]
_TARGET = re.compile(r"(?i)target finished video length: about (\d+) seconds")


def long_haul_target(*texts: str) -> dict[str, int | None] | None:
    """{clips, seconds} when the request asks for more than one ordinary plan can hold, else None."""
    text = "\n".join(t or "" for t in texts)
    clips = max((int(m.group(1)) for m in _COUNT.finditer(text)), default=0)
    if re.search(r"(?m)^\s*::VIDEO\b", text):
        return None                      # the user wrote the clips themselves: plan it as written, not as an outline
    seconds = max([float(m.group(1)) * 60 for rx in _MINUTES for m in rx.finditer(text)]
                  + [float(m.group(1)) for rx in _SECONDS for m in rx.finditer(text)]
                  + [float(m.group(1)) for m in _TARGET.finditer(text)], default=0.0)
    if clips > LONG_CLIPS or seconds > LONG_SECONDS:
        return {"clips": min(clips, OUTLINE_MAX) or None, "seconds": round(seconds) or None}
    return None


def summary(prompt: str, limit: int = 220) -> str:
    """One line a planner can continue from: the summary section, else the first shot, with dialogue kept as words."""
    text = prompt or ""
    found = re.search(r"(?ms)^\s*summary:\s*(.+?)(?=^\s*[a-z_]+:\s*$|^\s*[a-z]+_[a-z_]+:|\Z)", text)
    if found:
        text = found.group(1)
    else:
        shot = re.search(r"\[Shot 1\]\s*(.+)", text, re.S)
        text = shot.group(1) if shot else re.sub(r"(?m)^\s*[a-z]+(?:_[a-z]+)+:\s*", "", text)
    text = re.sub(r"<d>\s*(?:\[[^\]]*\])?\s*(.*?)</d>", r"“\1”", text, flags=re.S)
    text = re.sub(r"\[(?:reference generation|video editing|keyframe completion|video continuation)[^\]]*\]\s*", "", text)
    # Continuations all open alike ("The target video continues from the exact final frame of the previous clip,
    # showing ..."): drop that lead-in so the line says what happens in this clip.
    text = re.sub(r"(?is)^\s*(?:\[Shot 1\]\s*)?(?:the target video|this clip|the clip|it)\s+(?:continues|picks up|carries on)\s+"
                  r"(?:directly\s+|seamlessly\s+)?from\s+(?:the\s+)?(?:exact\s+)?(?:final|last|ending)\s+frame[^,.;:]*[,.;:]\s*"
                  r"(?:(?:showing|where|with|as|in which)\s+)?", "", text)
    text = re.sub(r"(?is)^\s*continuing from the exact (?:pose|frame)[^,]*,\s*", "", text)
    text = text[:1].upper() + text[1:] if text else text
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def ledger(session: dict[str, Any]) -> dict[str, Any]:
    return session.setdefault("ledger", {"clips": {}, "outline": [], "target": None, "batch": BATCH_CLIPS})


def entries(session: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(ledger(session)["clips"].values(), key=lambda entry: entry["n"])


def reached(entry: dict[str, Any]) -> bool:
    """Verified, or verified once and now being edited: either way the film has reached this clip."""
    return entry.get("status") == "verified" or bool(entry.get("was_verified"))


def verified_count(session: dict[str, Any]) -> int:
    """Clips 1..N the film has reached with no gap: where the next batch starts. A finished clip that is being
    edited still counts, so a new batch never re-plans over it."""
    clips, n = ledger(session)["clips"], 0
    while reached(clips.get(str(n + 1), {})):
        n += 1
    return n


def add_direction(session: dict[str, Any], from_clip: int, text: str) -> dict[str, Any]:
    """A lasting change the user asked for ("from now on the cat wears the red hat"): every clip from from_clip on."""
    item = {"from": int(from_clip), "text": text.strip()[:2000], "at": time.time()}
    ledger(session).setdefault("directions", []).append(item)
    return item


def directions_for(session: dict[str, Any], first: int, last: int | None = None) -> list[dict[str, Any]]:
    return [d for d in ledger(session).get("directions") or [] if last is None or d["from"] <= last]


def directions_text(session: dict[str, Any], first: int, last: int | None = None) -> str:
    items = directions_for(session, first, last)
    if not items:
        return ""
    return ("STANDING DIRECTIONS from the user (they override the outline and earlier prompts, for every clip from the "
            "number given on; the newest wins where two disagree):\n"
            + "\n".join(f"  from clip {d['from']}: {d['text']}" for d in items))


def put(session: dict[str, Any], n: int, status: str, prompt: str, **fields: Any) -> dict[str, Any]:
    """Record clip n's prompt and state. A verified clip stays verified unless its prompt really changes."""
    entry = ledger(session)["clips"].setdefault(str(n), {"n": n})
    if entry.get("status") == "verified" and status != "verified" and entry.get("prompt") == prompt:
        return entry
    if entry.get("status") == "verified" and status != "verified":
        entry["was_verified"] = True        # being edited: still part of the finished film until re-verified
    if status == "verified":
        entry.pop("was_verified", None)
    entry.pop("rendered", None)          # a new prompt or a verification: no longer "rendered, awaiting review"
    entry.update(status=status, prompt=prompt, summary=summary(prompt), updated=time.time(),
                 **{key: value for key, value in fields.items() if value is not None})
    return entry


def prune(session: dict[str, Any], run_id: str, keep: set[int]) -> None:
    """Drop this run's unfinished entries for clips its current plan no longer has (a repair or revision removed them)."""
    clips = ledger(session)["clips"]
    for key, entry in list(clips.items()):
        if entry.get("run") == run_id and entry.get("clip") not in keep and not reached(entry):
            del clips[key]


def draft(session: dict[str, Any], offset: int, blocks: dict[int, str], run_id: str) -> None:
    """A plan Hermes just wrote, before it passed the checks: its clips are in progress (verified ones untouched)."""
    prune(session, run_id, set(blocks))
    for clip_id, block in blocks.items():
        current = ledger(session)["clips"].get(str(offset + clip_id), {})
        if current.get("status") in ("verified", "written") and current.get("run") == run_id and current.get("prompt") == block:
            continue
        if current.get("status") != "verified":
            put(session, offset + clip_id, "in-progress", block, run=run_id, clip=clip_id)


def backfill(session: dict[str, Any], sources: dict[str, dict[str, Any]]) -> None:
    """Sessions from before the ledger: their kept final clips (session:1..N) are the verified clips 1..N."""
    for index, source in enumerate(sources.values(), 1):
        if ledger(session)["clips"].get(str(index), {}).get("status") != "verified":
            put(session, index, "verified", source.get("prompt") or "", render=source.get("job_id"),
                seconds=source.get("duration"))


def set_outline(session: dict[str, Any], items: list[dict[str, Any]], target: dict[str, Any] | None,
                max_seconds: float = 15, min_seconds: float = 5) -> list[dict[str, Any]]:
    clean = []
    for item in items[:OUTLINE_MAX]:
        if not isinstance(item, dict):
            continue
        beat = re.sub(r"\s+", " ", str(item.get("beat") or item.get("text") or "")).strip()
        if not beat:
            continue
        try:
            seconds = float(item.get("seconds") or 10)
        except (TypeError, ValueError):
            seconds = 10.0
        clean.append({"seconds": round(min(max(seconds, min_seconds), max_seconds)), "beat": beat[:400]})
    book = ledger(session)
    book["outline"], book["target"] = clean, target
    book["outline_start"] = verified_count(session)     # clips the session already had: the outline comes after them
    return clean


def outline_total(session: dict[str, Any]) -> int:
    """The overall clip number the outline ends at (0 without an outline)."""
    book = ledger(session)
    return int(book.get("outline_start") or 0) + len(book.get("outline") or []) if book.get("outline") else 0


def batch(session: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    """(first overall clip number, the outline lines this run writes); ([] when the outline is done or absent)."""
    book = ledger(session)
    done = verified_count(session)
    start = done - int(book.get("outline_start") or 0)
    return done + 1, (book.get("outline") or [])[max(start, 0): max(start, 0) + int(book.get("batch") or BATCH_CLIPS)]


def batch_note(session: dict[str, Any]) -> str:
    """What the planner writes this run, for a session with an outline. '' when there is no outline left."""
    book = ledger(session)
    outline = book.get("outline") or []
    first, part = batch(session)
    if not part:
        return ""
    total, seconds = outline_total(session), sum(item["seconds"] for item in outline)
    last = first + len(part) - 1
    lines = [f"LONG-HAUL REQUEST, MADE IN BATCHES: the whole video is {total} clips (about {round(seconds)} s), outlined "
             f"below. Clips 1-{first - 1} are finished (see the PROGRESS LEDGER)." if first > 1 else
             f"LONG-HAUL REQUEST, MADE IN BATCHES: the whole video is {total} clips (about {round(seconds)} s).",
             f"Write ONLY this batch: overall clips {first}-{last}, as ::VIDEO ids 1-{len(part)} in this order, one clip "
             "per outline line below, each about the seconds given."
             + (" Clip 1 of this batch continues the last finished clip (action=EXTEND from=0) unless its outline line "
                "is a cut to a new shot or scene." if first > 1 else "")
             + " The service starts the next batch by itself when this one is finished, so do not write later outline "
             "lines, do not wrap the story up early, and never rewrite or repeat a finished clip.",
             "This batch's outline:"]
    lines += [f"  {first + index}. ({item['seconds']} s) {item['beat']}" for index, item in enumerate(part)]
    if standing := directions_text(session, first, last):
        lines.append(standing)
    if last < total:
        lines.append(f"Next batch starts with (context only, do NOT write it): {last + 1}. "
                     f"{outline[last - int(book.get('outline_start') or 0)]['beat']}")
    return "\n".join(lines)


def planner_context(session: dict[str, Any]) -> str:
    """The ledger as compact planner context: finished clips one line each, the last one in full, unfinished clips
    in full (the next run's starting point). '' for a session with nothing recorded."""
    items = entries(session)
    if not items:
        return ""
    done = verified_count(session)
    verified = [entry for entry in items if entry["n"] <= done]
    pending = [entry for entry in items if entry["n"] > done and entry.get("prompt")]
    lines = ["PROGRESS LEDGER (the session's prompt folders: verified = final and locked; written = validated, not "
             "finished; in-progress = a draft that had not passed the checks):"]
    if verified:
        lines.append(f"VERIFIED clips 1-{done} are FINAL: never rewrite, repeat, re-plan or re-render them. Continue after clip {done}.")
        lines += [f"  {entry['n']}." + (f" ({entry['seconds']:g} s)" if isinstance(entry.get("seconds"), (int, float)) else "")
                  + (" [being edited]" if entry.get("status") != "verified" else "")
                  + f" {entry.get('summary') or ''}" for entry in verified]
        lines += [f"Last verified clip {done} in full (the next clip continues from its final frame unless it cuts away):",
                  verified[-1].get("prompt", "")[:8000]]
    if pending:
        lines.append(f"UNFINISHED clips from an earlier run ({len(pending)}), reuse them as the starting point for these clip numbers:")
        for entry in pending[:PENDING_FULL]:
            lines += [f"-- clip {entry['n']} ({entry.get('status')}) --", entry.get("prompt", "")[:6000]]
        if len(pending) > PENDING_FULL:
            lines.append("  ...and " + ", ".join(f"{entry['n']}: {entry.get('summary') or ''}" for entry in pending[PENDING_FULL:])[:3000])
    if standing := directions_text(session, done + 1):
        lines.append(standing)
    text = "\n".join(lines)
    if len(text) > CONTEXT_LIMIT:      # very long films: the oldest one-liners go first, the tail stays whole
        head, rest = lines[:2], lines[2:]
        while rest and len("\n".join(head + ["  (earlier finished clips omitted)"] + rest)) > CONTEXT_LIMIT:
            rest.pop(0)
        text = "\n".join(head + ["  (earlier finished clips omitted)"] + rest)
    return text


def _clip_file(entry: dict[str, Any]) -> str:
    meta = [f"# Clip {entry['n']} — {entry.get('status')}", ""]
    for key, label in (("render", "Render"), ("run", "Run"), ("clip", "Clip in its run"), ("seconds", "Seconds"),
                       ("extends", "Continues clip")):
        if entry.get(key) not in (None, ""):
            meta.append(f"- {label}: {entry[key]}")
    if entry.get("updated"):
        meta.append(f"- Updated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry['updated']))}")
    return "\n".join(meta + ["", entry.get("prompt") or "", ""])


def write_folders(folder: Path, session: dict[str, Any]) -> None:
    """Mirror the ledger: prompts/<status>/clip-NNN.md (each clip in exactly one folder), progress.md, outline.md."""
    root = folder / "prompts"
    for status in STATUSES:
        (root / status).mkdir(parents=True, exist_ok=True)
    wanted = {(entry.get("status"), f"clip-{entry['n']:03d}.md"): _clip_file(entry)
              for entry in entries(session) if entry.get("status") in STATUSES}
    for status in STATUSES:
        for existing in (root / status).glob("clip-*.md"):
            if (status, existing.name) not in wanted:
                existing.unlink(missing_ok=True)
    for (status, name), content in wanted.items():
        target = root / status / name
        if not target.is_file() or target.read_text() != content:
            target.write_text(content)
    book = ledger(session)
    outline = book.get("outline") or []
    done = verified_count(session)
    start = int(book.get("outline_start") or 0)
    counts = {status: sum(entry.get("status") == status for entry in entries(session)) for status in STATUSES}
    lines = [f"# Progress — Agentic session {session.get('id')}", "",
             f"Verified {counts['verified']} · written {counts['written']} · in progress {counts['in-progress']}"
             + (f" · outline {done}/{start + len(outline)} clips" if outline else ""), ""]
    lines += [f"- {entry['n']:>3}. [{entry.get('status')}] {entry.get('summary') or ''}" for entry in entries(session)]
    (folder / "progress.md").write_text("\n".join(lines) + "\n")
    if outline:
        rows = [f"# Outline — {len(outline)} clips, about {sum(item['seconds'] for item in outline)} s", ""]
        rows += [f"{index:>3}. ({item['seconds']} s) {'✓ ' if index <= done else ''}{item['beat']}" for index, item in enumerate(outline, start + 1)]
        (folder / "outline.md").write_text("\n".join(rows) + "\n")


def dependents(session: dict[str, Any], edited: set[int]) -> list[int]:
    """Finished clips that continue (EXTEND) from an edited clip, directly or down a chain: their first frame changes."""
    out: set[int] = set()
    changed = set(edited)
    for entry in entries(session):
        source = entry.get("extends")
        if reached(entry) and entry["n"] not in edited and isinstance(source, int) and source in changed:
            out.add(entry["n"])
            changed.add(entry["n"])
    return sorted(out)


def mark_rendered(session: dict[str, Any], n: int, render_id: str) -> None:
    """A clip's final render finished (its review may still replace it): 'done', not yet verified."""
    entry = ledger(session)["clips"].get(str(n))
    if entry and entry.get("status") != "verified":
        entry["render"], entry["rendered"] = render_id, time.time()


def overview(session: dict[str, Any]) -> dict[str, Any]:
    """At a glance: each clip's state, as one letter per clip from 1 to the planned total
    (v verified, d rendered/done, w written, i writing, - not written yet), plus the counts."""
    items = {entry["n"]: entry for entry in entries(session)}
    total = max(outline_total(session), max(items, default=0))
    letters = []
    for n in range(1, total + 1):
        entry = items.get(n) or {}
        status = entry.get("status")
        letters.append("v" if status == "verified" else "d" if entry.get("rendered") or entry.get("was_verified") else
                       "w" if status == "written" else "i" if status == "in-progress" else "-")
    strip = "".join(letters)
    return {"total": total, "outline": outline_total(session), "strip": strip,
            "counts": {"verified": strip.count("v"), "done": strip.count("d"), "written": strip.count("w"),
                       "writing": strip.count("i"), "todo": strip.count("-")},
            "directions": len(ledger(session).get("directions") or [])}
