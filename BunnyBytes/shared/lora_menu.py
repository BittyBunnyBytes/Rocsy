"""The planner's LoRA menu, shared by the framework (Image Agentic) and Video (H3 / Wan Agentic).

A flat dump of every installed LoRA with its full scraped blurb grows with the gallery (55 H3 LoRAs were
~17K characters per plan) and buries the few that fit. This builds a compact menu instead:

- descriptions are cleaned of licence / marketing / changelog boilerplate and cut at a word boundary;
- default fields (strength 1.0, no triggers, no description) are left out, and the group is a heading;
- a short list fits in full; a long one gets a BEST MATCHES section (ranked by word overlap with the
  request, with full details) followed by a one-line-per-group INDEX of everything else (number, name
  and first trigger), so any LoRA can still be chosen by number.

Entries are dicts: number (1-based, what the planner writes), label, group, triggers (list), strength,
description, and optionally base ("trained for") and enabled (already on: shown, never offered).
"""
from __future__ import annotations

import math
import re
from typing import Any

FULL_LIST_UP_TO = 24   # at most this many entries: every one gets full details
BEST_MATCHES = 12      # longer lists: this many ranked entries get full details
DESCRIPTION_CHARS = 120
MATCH_FLOOR = 0.25      # a best match scores at least this share of the top one (the rest stay in the index)

_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("""a an and are as at be but by can for from has have in into is it its of on or so that the their them
this to too was were will with without you your use used using very more most some any all not no only also just
lora loras model models video videos clip clips image images style prompt prompts trained training trigger triggers
word words make made like one two get best good high quality new version""".split())
# Sentences that never help pick a LoRA (licence, monetisation, changelogs, support asks, links).
_BOILERPLATE = re.compile(
    r"money|sell|paid|merge it|licen[cs]e|commercial|patreon|ko-?fi|buy me|donat|support me|discord|follow me|"
    r"https?://|www\.|changelog|update[sd]?:|epoch|steps?\b.*\btrain|dataset of|"
    r"training (process|set|data)|thanks|credit|warning|runninghub|coins|registration|sign up|your browser", re.I)


def words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.casefold()) if len(w) > 2 and w not in _STOP}


def clean_description(text: str, label: str = "", limit: int = DESCRIPTION_CHARS) -> str:
    """The useful part of a scraped blurb: no boilerplate sentences, no repeated name, cut at a word."""
    text = " ".join(str(text or "").split())
    if label and text.casefold().startswith(label.casefold()):
        text = text[len(label):].lstrip(" -:–—|,.")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s and not _BOILERPLATE.search(s)]
    text = " ".join(sentences)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"
    return text


def _terms(entry: dict[str, Any]) -> tuple[set[str], set[str]]:
    strong = words(" ".join([entry["label"], entry.get("group") or "", *entry.get("triggers", [])]))
    return strong, words(entry.get("description") or "") - strong


def _rank(entries: list[dict[str, Any]], query: set[str]) -> list[tuple[float, dict[str, Any]]]:
    """Word overlap with the request; a word in the name/group/triggers counts 3x one in the description,
    and rarer words count more (a 'fight' LoRA beats the tenth 'anime' one for 'anime fight scene')."""
    terms = [_terms(e) for e in entries]
    df: dict[str, int] = {}
    for strong, weak in terms:
        for w in (strong | weak) & query:
            df[w] = df.get(w, 0) + 1
    idf = {w: math.log(1 + len(entries) / n) for w, n in df.items()}
    scored = [(sum(3 * idf[w] for w in strong & query) + sum(idf[w] for w in weak & query), i, e)
              for i, (e, (strong, weak)) in enumerate(zip(entries, terms))]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [(score, e) for score, _, e in scored]


def _detail(entry: dict[str, Any], show_group: bool) -> str:
    head = f"{entry['number']}. {entry['label']}" + (f" [{entry['group']}]" if show_group and entry.get("group") else "")
    parts = []
    if entry.get("base"):
        parts.append(f"for {entry['base']}")
    if entry.get("triggers"):
        parts.append("triggers: " + " | ".join(entry["triggers"]))
    try:
        strength = float(entry.get("strength") or 1.0)
    except (TypeError, ValueError):
        strength = 1.0
    if abs(strength - 1.0) > 1e-9:
        parts.append(f"strength {strength:g}")
    description = clean_description(entry.get("description") or "", entry["label"])
    if description:
        parts.append(description)
    return head + (" — " + "; ".join(parts) if parts else "")


def _index_item(entry: dict[str, Any]) -> str:
    first = (entry.get("triggers") or [""])[0]
    hint = f" ({first})" if first and len(first) <= 32 else ""
    return f"{entry['number']} {entry['label']}{hint}"


def _grouped(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault(entry.get("group") or "Other", []).append(entry)
    return dict(sorted(groups.items(), key=lambda kv: (kv[0] == "Other", kv[0].casefold())))


def render(entries: list[dict[str, Any]], query: str = "") -> str:
    """The numbered menu (no heading or rules: the caller adds its own pick syntax)."""
    entries = [e for e in entries if e.get("label")]
    on = [e for e in entries if e.get("enabled")]
    offered = [e for e in entries if not e.get("enabled")]
    lines = []
    if on:
        lines.append("ALREADY ON (never choose): " + ", ".join(f"{e['number']} {e['label']}" for e in on))
    if len(offered) <= FULL_LIST_UP_TO:
        for group, members in _grouped(offered).items():
            lines.append(f"{group}:")
            lines.extend("  " + _detail(e, False) for e in members)
        return "\n".join(lines)
    ranked = _rank(offered, words(query))[:BEST_MATCHES]
    floor = ranked[0][0] * MATCH_FLOOR if ranked else 0
    best = [e for score, e in ranked if score > 0 and score >= floor]
    if best:
        lines.append(f"BEST MATCHES FOR THIS REQUEST (by keyword, strongest first; {len(offered)} offered in all):")
        lines.extend("  " + _detail(e, True) for e in best)
    else:
        lines.append(f"No LoRA shares keywords with this request ({len(offered)} offered); pick from the index only "
                     "if one clearly fits.")
    shown = {id(e) for e in best}
    rest = [e for e in offered if id(e) not in shown]
    if rest:
        lines.append("INDEX of the others by group (number name (first trigger)); any number here is valid too:")
        for group, members in _grouped(rest).items():
            lines.append(f"  {group}: " + " · ".join(_index_item(e) for e in members))
    return "\n".join(lines)
