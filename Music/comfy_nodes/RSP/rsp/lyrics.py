"""Whisper's timed lines laid under a score's section tags. Pure text, no torch."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_TAG = re.compile(r"^%\s*([A-Za-z][\w -]*)\s*$")
_REST_BAR = re.compile(r"^(?:\"[^\"]*\")?z\d*$")


def score_sections(score: str) -> list[dict]:
    """[{tag, bars, sung}] in order, from the '% verse' comments and the vocal lines under them."""
    found: list[dict] = []
    voice = ""
    for raw in (score or "").splitlines():
        line = raw.strip()
        tag = _TAG.match(line)
        if tag:
            found.append({"tag": tag.group(1).strip(), "bars": 0, "sung": False})
        elif line.startswith("V:"):
            voice = line[2:].strip().split(" ")[0]
        elif found and voice == "Vocal" and "|" in line:
            bars = [bar.strip() for bar in line.split("|") if bar.strip()]
            found[-1]["bars"] += len(bars)
            found[-1]["sung"] = found[-1]["sung"] or any(not _REST_BAR.match(bar) for bar in bars)
    return [section for section in found if section["bars"]]


def clean_lines(segments: list[dict]) -> list[dict]:
    """Whisper's sure lines. no_speech_prob alone runs high on sung vocals, so like Whisper's own silence
    check, a line is only dropped when it's also unsure of the words."""
    return [segment for segment in segments if (segment.get("text") or "").strip()
            and not (float(segment.get("no_speech_prob") or 0.0) >= 0.6 and float(segment.get("avg_logprob") or 0.0) <= -1.0)]


def lay_words(score: str, segments: list[dict], seconds: float) -> str:
    """Each line goes in the section its middle falls in. Bars are equal length, so bars give the times."""
    lines = clean_lines(segments)
    sections = score_sections(score)
    total = sum(section["bars"] for section in sections)
    if not sections or not total or seconds <= 0:
        return "[Verse]\n" + "\n".join(line["text"].strip() for line in lines) if lines else ""
    per_bar = seconds / total
    start = 0.0
    spans = []
    for section in sections:
        end = start + section["bars"] * per_bar
        spans.append((start, end, section["tag"], section["sung"], []))
        start = end
    for line in lines:
        middle = (float(line.get("start") or 0.0) + float(line.get("end") or 0.0)) / 2.0
        span = next((span for span in spans if span[0] <= middle < span[1]), spans[-1])
        if not span[3]:     # heard over a section the score has no singing in: the nearest sung one
            sung = [other for other in spans if other[3]] or spans
            span = min(sung, key=lambda other: abs((other[0] + other[1]) / 2.0 - middle))
        span[4].append(line["text"].strip())
    return "\n\n".join(f"[{tag.title()}]\n" + "\n".join(words) for _, _, tag, _, words in spans if words)


_SECTION = re.compile(r"^\[[^\]]*\]\s*(.*)$")
MAX_FILL_WORD = 0.6     # seconds a word Whisper missed may take


def sung_lines(lyrics: str) -> list[list[str]]:
    """The words of each sung line, split the way the Music player splits them: section tags aren't sung."""
    lines = []
    for raw in (lyrics or "").splitlines():
        line = raw.strip()
        tag = _SECTION.match(line)
        if tag:
            line = tag.group(1)
        if line.split():
            lines.append(line.split())
    return lines


def _plain(word: str) -> str:
    return "".join(ch for ch in word.lower() if ch.isalnum())


def align_words(lyrics: str, segments: list[dict], seconds: float) -> list[list[list[float]]]:
    """[start, end] for every written word, per line. Whisper's timed words anchor the words it heard; the
    ones it missed sit beside their neighbours, so they can't stretch across an instrumental break."""
    lines = sung_lines(lyrics)
    sheet = [(number, word) for number, line in enumerate(lines) for word in line]
    heard = [word for segment in clean_lines(segments) for word in segment.get("words") or []
             if _plain(str(word.get("word") or ""))]
    matcher = SequenceMatcher(None, [_plain(word) for _, word in sheet], [_plain(str(word["word"])) for word in heard], autojunk=False)
    times: list[list[float] | None] = [None] * len(sheet)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            word = heard[block.b + offset]
            times[block.a + offset] = [float(word["start"]), float(word["end"])]
    matched = sum(item is not None for item in times)
    if matched < max(3, 0.15 * len(sheet)):
        raise RuntimeError(f"Whisper matched only {matched} of {len(sheet)} written words, too few to time the lyrics.")
    index = 0
    while index < len(sheet):
        if times[index] is not None:
            index += 1
            continue
        stop = index
        while stop < len(sheet) and times[stop] is None:
            stop += 1
        before = times[index - 1][1] if index else 0.0
        after = times[stop][0] if stop < len(sheet) else max(seconds, before)
        count = stop - index
        length = min(max(after - before, 0.0), count * MAX_FILL_WORD)
        # Runs on from a word of its own line, otherwise leads into the next word.
        follows = index > 0 and (sheet[index - 1][0] == sheet[index][0] or stop == len(sheet))
        begin = before if follows else after - length
        for step in range(count):
            times[index + step] = [begin + length * step / count, begin + length * (step + 1) / count]
        index = stop
    cursor = 0.0
    result: list[list[list[float]]] = [[] for _ in lines]
    for (number, _), (start, end) in zip(sheet, times):
        start = max(start, cursor)
        end = max(end, start)
        cursor = start
        result[number].append([round(start, 2), round(end, 2)])
    return result
