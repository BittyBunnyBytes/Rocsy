"""Prompt-guide retrieval (stdlib BM25) for the Master Agent.

guides/<topic>/*.md are split at their headings into chunks; a message is scored against them.
The H3 index does two jobs: tell whether the user wants a MiniMax H3 prompt ("script") written,
and hand the agent the guide sections that answer it, so it writes in the official format."""

from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

GUIDES = Path(__file__).with_name("guides")
CHUNK_CHARS = 1800
WORD = re.compile(r"[a-z0-9_]+")
STOP = set("a an and are as at be but by for from has have i in is it its me my of on or our so that the their them then "
           "there these they this to was we were what when which who will with you your can do does should would could "
           "about into like just want need please some any all one get make".split())

# Words that only an H3 prompt (or a question about writing one) uses.
H3_NAMES = re.compile(r"\b(h3|h3 ?machine|mini ?max|hailuo|ref2va|t2va|i2va|fl2va|l2va)\b", re.I)
H3_SYNTAX = re.compile(r"<(subject|picture|video|audio) \d+>|\b(subject_definitions|retention_analysis|integrated_multimodal_description"
                       r"|overall_soundscape|non_diegetic_music|detailed_description)\b|\[shot \d+\]|<d>\[|<scenetrans>|<cutoff>", re.I)
WRITING = re.compile(r"\b(write|writ|draft|script|prompt|rewrite|re-?word|convert|turn .{0,30} into|fix|check|debug|format|improve|"
                     r"polish|tighten|translate|shot ?list|storyboard|six[- ]section|sections?)\w*", re.I)
VIDEO_WORDS = re.compile(r"\b(video|clip|shot|scene|film|camera|keyframe|first frame|last frame|reference|character swap|"
                         r"continuation|lip ?sync|dialogue|voice)\w*", re.I)
# Normalised BM25 a writing request about video must reach to count without naming H3.
RAG_THRESHOLD = 2.3
# A question (no writing word) has to match much harder: it then gets the guide to answer from.
QUESTION_THRESHOLD = 2.9
QUESTION = re.compile(r"\?|^\s*(how|what|why|when|which|should|can|does|is|are)\b", re.I)


def tokens(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 1]


def _chunks(path: Path) -> list[dict[str, Any]]:
    """One chunk per heading section; long sections split at paragraphs."""
    out: list[dict[str, Any]] = []
    title, buf = path.stem, []

    def flush() -> None:
        body = "\n".join(buf).strip()
        while body:
            cut = body.rfind("\n\n", 0, CHUNK_CHARS) if len(body) > CHUNK_CHARS else len(body)
            part = body[:cut if cut > 200 else CHUNK_CHARS]
            out.append({"file": path.name, "title": title, "text": part.strip()})
            body = body[len(part):].strip()

    in_fence = False
    text = re.sub(r"\A---\n.*?\n---\n", "", path.read_text(errors="replace"), flags=re.S)   # skill frontmatter
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and re.match(r"#{1,4} ", line):
            flush()
            title, buf = line.lstrip("#").strip(), [line]
        else:
            buf.append(line)
    flush()
    return [c for c in out if len(c["text"]) > 40]


@lru_cache(maxsize=8)
def index(topic: str) -> dict[str, Any]:
    chunks = [c for path in sorted((GUIDES / topic).glob("*.md")) for c in _chunks(path)]
    docs = [Counter(tokens(c["title"] + " " + c["title"] + " " + c["text"])) for c in chunks]
    df = Counter(word for doc in docs for word in doc)
    avg = sum(sum(d.values()) for d in docs) / max(1, len(docs))
    return {"chunks": chunks, "docs": docs, "df": df, "avg": avg, "n": len(docs)}


def search(topic: str, query: str, k: int = 4) -> list[tuple[float, dict[str, Any]]]:
    """Top k chunks by BM25; the score is normalised by the query's length (so it can be thresholded)."""
    ix = index(topic)
    words = list(dict.fromkeys(tokens(query)))
    if not words or not ix["n"]:
        return []
    k1, b = 1.4, 0.75
    scored = []
    for chunk, doc in zip(ix["chunks"], ix["docs"]):
        length = sum(doc.values())
        score = 0.0
        for w in words:
            if w in doc:
                idf = math.log(1 + (ix["n"] - ix["df"][w] + 0.5) / (ix["df"][w] + 0.5))
                score += idf * doc[w] * (k1 + 1) / (doc[w] + k1 * (1 - b + b * length / ix["avg"]))
        if score:
            scored.append((score / math.sqrt(len(words)), chunk))
    scored.sort(key=lambda item: -item[0])
    return scored[:k]


def h3_script_intent(message: str) -> tuple[bool, float, str]:
    """(wanted, retrieval score, why). Wanted = the user asks for an H3 prompt to be written/fixed,
    not for a video to be rendered: an H3 prompt pasted in, H3 named next to a writing word, or a
    writing request about video whose words match the H3 guides strongly."""
    text = message or ""
    hits = search("h3", text, 1)
    score = round(hits[0][0], 2) if hits else 0.0
    if H3_SYNTAX.search(text):
        return True, score, "H3 prompt syntax in the message"
    writing = WRITING.search(text)
    if writing and H3_NAMES.search(text):
        return True, score, "H3/MiniMax named with a writing request"
    if writing and VIDEO_WORDS.search(text) and score >= RAG_THRESHOLD:
        return True, score, "a writing request that matches the H3 prompting guides"
    if QUESTION.search(text) and (score >= QUESTION_THRESHOLD or H3_NAMES.search(text)):
        return True, score, "a question the H3 prompting guides answer"
    return False, score, ""


def excerpts(topic: str, query: str, k: int = 4, budget: int = 7000) -> str:
    """The best k chunks; for H3 the best of the core rules (rules.md) always leads."""
    found = [chunk for _, chunk in search(topic, query, 40)]
    core = [c for c in found if c["file"] == "rules.md"]
    detail = [c for c in found if c["file"] != "rules.md"]
    # The best core rule, then the official guides' best sections, then more rules if room is left.
    picked = (core[:1] + detail[:k - 1] + core[1:])[:k] if core else found[:k]
    out, used = [], 0
    for chunk in picked:
        piece = f"--- {chunk['file']} · {chunk['title']} ---\n{chunk['text']}"
        if used + len(piece) > budget:
            break
        out.append(piece)
        used += len(piece)
    return "\n\n".join(out)
