"""Checks for the Master Agent's prompt-ledger clips, run when a clip is saved (captured from a reply or `$R prompt save`),
so problems Vid would refuse at handover show up while the agent is still writing.

The dialogue part is a COPY of Video/backend/agentic.py (dialogue_problems, fill_speaker_ids, normalize_dialogue and their
helpers): keep it in step with Vid, the same way ledger.py is. On top of that come the checks Vid makes when it parses a
plan (header, six Reference sections, refs= vs mode, <Picture N> labels vs refs=) and two the agent needs that Vid can't
see: speaker IDs given only in subject_definitions ("<Subject 3> is Trump-Tuco ... (S4: ...)") and one speaker table
across the whole ledger.

check_clip() returns (prompt with the safe automatic fixes, fixes, problems). The fixes are the ones Vid's normalize_plan
makes anyway (speaker IDs it can tie to a name, end punctuation, a full Reference prompt under mode=text).
"""
from __future__ import annotations

import re
from typing import Any

# --- copied from Video/backend/agentic.py ------------------------------------------------------------------------
SPEECH_VERB_QUOTE = re.compile(r'(?i)\b(?:says?|said|asks?|asked|shouts?|shouted|yells?|yelled|whispers?|whispered|replies|replied|'
                               r'exclaims?|exclaimed|mutters?|muttered|adds?|answers?|answered|complains?|sings?|sang)\b[^<"\n]{0,40}["“]')
NO_SPEECH = re.compile(r'(?i)\b(?:no one|nobody|no-one)\s+(?:speaks|talks|says)|\bno (?:dialogue|dialog|speech|talking|spoken|voices?|lines)\b'
                       r'|\bwithout (?:speaking|dialogue|talking|a word)|\b(?:is|are|remains?|stays?) (?:silent|wordless)')
SPEAKER_ID = re.compile(r'\(S\d+(?:\s*,\s*S\d+)*\)')
NAMED_SPEAKER = re.compile(r"(<Subject \d+>|\b[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)?)\s*(\(S\d+\))")
NOT_NAMES = {'The', 'A', 'An', 'He', 'She', 'They', 'It', 'His', 'Her', 'Their', 'Then', 'And', 'As', 'When', 'While'}
ON_SCREEN_TEXT = re.compile(r'(?i)\b(?:reads?|reading|shows?|spells?|labell?ed|lettering|letters|written|text|sign|caption|title)\b')
SENTENCE_BREAK = re.compile(r'</d>|[.!?]["”\']?\s+|\n|\[Shot \d+\]\s*', re.I)
REFERENCE_SECTIONS = ('subject_definitions:', 'summary:', 'retention_analysis:',
                      'detailed_description:', 'overall_soundscape:', 'non_diegetic_music:')


def _norm_words(text: str) -> str:
    return ' '.join(re.sub(r'[^\w\s]', ' ', re.sub(r'^\s*\[[^\]]*\]', '', text).lower()).split())


def _sentence_start(text: str, position: int) -> int:
    start = 0
    for match in SENTENCE_BREAK.finditer(text, 0, position):
        start = match.end()
    return start


def _has_speaker(text: str, position: int) -> bool:
    return bool(SPEAKER_ID.search(text, min(_sentence_start(text, position), max(0, position - 300)), position))


def _quoted_lines(text: str) -> list[tuple[re.Match[str], str]]:
    found = []
    outside = re.sub(r'(?is)<d>.*?</d>', lambda m: ' ' * len(m.group(0)), text)
    for match in SPEECH_VERB_QUOTE.finditer(outside):
        if ON_SCREEN_TEXT.search(match.group(0)):
            continue
        close = re.search(r'["”]', outside[match.end():match.end() + 400])
        found.append((match, outside[match.end():match.end() + close.start()] if close else ''))
    return found


def speaker_names(text: str) -> dict[str, str]:
    names: dict[str, str | None] = {}
    for name, speaker in NAMED_SPEAKER.findall(text):
        candidates = [name] + ([name.split()[-1]] if ' ' in name and not name.startswith('<') else [])
        for candidate in candidates:
            if candidate.split()[0] in NOT_NAMES:
                continue
            names[candidate] = speaker if names.get(candidate, speaker) == speaker else None
    return {name: speaker for name, speaker in names.items() if speaker}


def fill_speaker_ids(prompt: str, names: dict[str, str], only_speaker: str | None = None) -> tuple[str, int]:
    fixed = 0
    position = 0
    while (match := re.compile(r'(?i)<d>').search(prompt, position)):
        position = match.end()
        start = _sentence_start(prompt, match.start())
        if SPEAKER_ID.search(prompt, start, match.start()):
            continue
        sentence = prompt[start:match.start()]
        insert, speaker = None, None
        pronoun = re.search(r'\b(?:[Hh]e|[Ss]he|[Tt]hey)\b', sentence)
        hits = [(hit.end(), speaker) for name, speaker in names.items()
                for hit in [re.search(r'(?<![\w<])' + re.escape(name) + r'(?![\w])(?!\s*\(S)', sentence)] if hit]
        if hits and (not pronoun or min(hits)[0] < pronoun.start()):
            insert, speaker = min(hits)
            insert += start
        elif pronoun and only_speaker and not hits:
            insert, speaker = start + pronoun.end(), only_speaker
        if insert is not None:
            prompt = prompt[:insert] + f' {speaker}' + prompt[insert:]
            position += len(speaker) + 1
            fixed += 1
    return prompt, fixed


def normalize_dialogue(text: str) -> tuple[str, int]:
    fixed = 0

    def finish(match: re.Match[str]) -> str:
        nonlocal fixed
        body = match.group(1).rstrip()
        if re.match(r'\[[A-Za-z ]+\]\s*\S', body) and not re.search(r'[.?!]["”\']?$', body):
            fixed += 1
            return f'<d>{body}.</d>'
        return match.group(0)

    return re.sub(r'(?is)<d>(.*?)</d>', finish, text), fixed


def dialogue_problems(prompt: str, index: int) -> list[str]:
    problems = []
    opened, closed = len(re.findall(r'(?i)<d>', prompt)), len(re.findall(r'(?i)</d>', prompt))
    if opened != closed:
        problems.append(f'Clip {index}: every <d> needs its closing </d>')
    for match in re.finditer(r'(?is)<d>(.*?)</d>', prompt):
        body = match.group(1).strip()
        if not re.match(r'\[[A-Za-z ]+\]\s*\S', body):
            problems.append(f'Clip {index}: each dialogue line must be <d>[Language] words</d>, e.g. <d>[English] Wait for me!</d>; '
                            f'this one is <d>{body[:60]}</d>')
        elif not re.search(r'[.?!]["”\']?$', body):
            problems.append(f'Clip {index}: end each spoken line with . ? or ! before </d> ("{body[:60]}")')
        if not _has_speaker(prompt, match.start()):
            problems.append(f"Clip {index}: put the speaker's ID in the sentence that introduces this line, e.g. "
                            f"'Peter (S2) says: <d>[English] ...</d>' or 'Peter (S2) continues: ...' (line: {body[:60]})")
    spoken = [_norm_words(body) for body in re.findall(r'(?is)<d>(.*?)</d>', prompt)]
    for match, words in _quoted_lines(prompt):
        if not (normalized := _norm_words(words)) or not any(normalized in line for line in spoken):
            problems.append(f'Clip {index}: spoken words must be inside <d>[Language] ...</d>, not quoted ("{match.group(0)[:40]}{words[:40]}")')
    if not opened and not NO_SPEECH.search(prompt) and 'audio reuse' not in prompt.lower():
        problems.append(f"Clip {index} has no dialogue: say so explicitly (e.g. 'No one speaks.') so H3 doesn't invent "
                        "gibberish voices; otherwise write the lines as <d>[Language] ...</d> with the speaker's (Sx)")
    return problems
# --- end of the copy -------------------------------------------------------------------------------------------------

PROMPT_LIMIT = 20000         # Vid's GenerationRequest.prompt max_length
WORDS_PER_SECOND = 2.5       # Vid's planning guide: about 2.5 spoken words per second plus time for the action
_FIELD = re.compile(r'(\w+)=(\S+)')
# "<Subject 3> is Trump-Tuco, the heavy tanned man ... (S4: rasping growl)" / "... (S4)" on the subject's own line
_DEFINITION = re.compile(r"(<Subject \d+>)\s+is\s+(?:the\s+|a\s+|an\s+)?([A-Z][\w'’-]*(?:[ -][A-Z][\w'’-]*)?)?([^\n]*)")
_DEF_ID = re.compile(r'\((S\d+)\b[^)]*\)')


def header_fields(prompt: str) -> dict[str, str]:
    first = prompt.lstrip().split('\n', 1)[0]
    return dict(_FIELD.findall(first)) if first.startswith('::VIDEO') else {}


def definition_speakers(prompt: str) -> dict[str, str]:
    """{"<Subject 3>": "(S4)", "Trump-Tuco": "(S4)"} from the subject_definitions section, where the agent tends to give
    the speaker ID ("(S4: rasping growl)") instead of beside each line."""
    if 'subject_definitions:' not in prompt:
        return {}
    section = prompt.split('subject_definitions:', 1)[1].split('summary:', 1)[0]
    out: dict[str, str] = {}
    for part in re.split(r'(?=<Subject \d+>\s+is\b)', section):
        m = _DEFINITION.match(part.strip())
        if not m:
            continue
        ident = _DEF_ID.search(part)
        if not ident:
            continue
        speaker = f'({ident.group(1)})'
        out[m.group(1)] = speaker
        name = (m.group(2) or '').strip()
        if name and name.split()[0] not in NOT_NAMES and len(name) > 1:
            out[name] = speaker
    return out


def _seconds(value: str) -> float | None:
    try:
        return float(str(value).rstrip('sS'))
    except ValueError:
        return None


def check_clip(prompt: str, n: int, names: dict[str, str] | None = None, pictures: int = 0) -> tuple[str, list[str], list[str]]:
    """(fixed prompt, fixes, problems) for one ledger clip. names: speaker names the rest of the ledger already tied to
    an ID; pictures: how many pictures the chat's picture table holds (0 = unknown, no range check)."""
    fixes: list[str] = []
    problems: list[str] = []
    fields = header_fields(prompt)
    head, body = (prompt.split('\n', 1) + [''])[:2] if fields else ('', prompt)
    body = re.sub(r'\n?::END\s*$', '', body)
    if fields:
        action, mode = fields.get('action', '').upper(), fields.get('mode', '').lower()
        if action not in ('NEW', 'EXTEND'):
            problems.append(f'Clip {n}: the header needs action=NEW or action=EXTEND')
        if mode not in ('text', 'reference', 'keyframe'):
            problems.append(f'Clip {n}: the header needs mode=text, mode=reference or mode=keyframe')
        secs = _seconds(fields.get('duration', ''))
        if secs is None or not 5 <= secs <= 15:
            problems.append(f'Clip {n}: duration must be 5-15 seconds (header says {fields.get("duration", "nothing")})')
        refs = [r for r in fields.get('refs', '-').split(',') if r not in ('-', '', 'inherit')]
        full_reference = all(section in body for section in REFERENCE_SECTIONS)
        if action == 'NEW' and mode == 'text' and refs:
            if full_reference:
                head = head.replace('mode=text', 'mode=reference', 1)
                mode = 'reference'
                fixes.append('mode=text → mode=reference (it selects pictures and is a full Reference prompt)')
            else:
                problems.append(f'Clip {n}: it selects refs={",".join(refs)} but is a text-mode prompt: use mode=reference '
                                'and the six Reference sections, or refs=-')
        if mode == 'reference' or action == 'EXTEND':
            positions = [body.find(section) for section in REFERENCE_SECTIONS]
            if any(p < 0 for p in positions):
                missing = [s.rstrip(':') for s, p in zip(REFERENCE_SECTIONS, positions) if p < 0]
                problems.append(f'Clip {n}: a Reference clip needs all six sections; missing {", ".join(missing)}')
            elif positions != sorted(positions):
                problems.append(f'Clip {n}: the six Reference sections must be in order: ' + ', '.join(s.rstrip(':') for s in REFERENCE_SECTIONS))
        labels = {f'image{x}' for x in re.findall(r'<Picture (\d+)>', body)} | {f'video{x}' for x in re.findall(r'<Video (\d+)>', body)} \
            | {f'audio{x}' for x in re.findall(r'<Audio (\d+)>', body)}
        for kind in ('first', 'last'):
            if fields.get(kind, '-') not in ('-', '', 'First', 'Last'):
                refs.append(fields[kind])
        unattached = sorted(labels - set(refs), key=lambda a: (re.sub(r'\d', '', a), int(re.sub(r'\D', '', a) or 0)))
        if unattached:
            problems.append(f'Clip {n}: the text names {", ".join(unattached)} (<Picture N> = imageN) but refs= does not select '
                            f'{"it" if len(unattached) == 1 else "them"}; Vid refuses that as an unattached reference')
        unused = sorted(a for a in set(refs) - labels if re.fullmatch(r'(image|video|audio)\d+', a))
        if unused and mode == 'reference':
            problems.append(f'Clip {n}: refs= selects {", ".join(unused)} but the text never names '
                            f'{"it" if len(unused) == 1 else "them"} as <Picture N>; name every selected picture or drop it')
        if pictures:
            beyond = sorted(a for a in set(refs) if (m := re.fullmatch(r'image(\d+)', a)) and int(m.group(1)) > pictures)
            if beyond:
                problems.append(f'Clip {n}: {", ".join(beyond)} is past the picture table (image1-image{pictures}, `$R prompt refs`)')
    known = dict(names or {})
    known.update(definition_speakers(body))
    speakers = set(SPEAKER_ID.findall(body)) | set(known.values())
    body, filled = fill_speaker_ids(body, known, next(iter(speakers)) if len(speakers) == 1 else None)
    if filled:
        fixes.append(f'added the speaker ID to {filled} spoken line{"s" if filled > 1 else ""}')
    body, punctuated = normalize_dialogue(body)
    if punctuated:
        fixes.append(f'end punctuation on {punctuated} spoken line{"s" if punctuated > 1 else ""}')
    problems += dialogue_problems(body, n)
    secs = _seconds(fields.get('duration', '')) if fields else None
    words = sum(len(_norm_words(x).split()) for x in re.findall(r'(?is)<d>(.*?)</d>', body))
    if secs and words > secs * WORDS_PER_SECOND + 3:
        problems.append(f'Clip {n}: {words} spoken words in {secs:g} s is more than H3 can say (about {WORDS_PER_SECOND:g} words '
                        'per second, less with action): cut lines, lengthen the clip, or split it')
    fixed = (head + '\n' + body.strip() + '\n::END') if fields else body
    if len(fixed) > PROMPT_LIMIT:
        problems.append(f'Clip {n}: {len(fixed)} characters; Vid takes at most {PROMPT_LIMIT} per clip')
    return fixed, fixes, problems


def speaker_table(prompts: dict[int, str]) -> tuple[dict[str, list[str]], list[str]]:
    """({"(S2)": ["Walt"], ...}, problems) over every ledger clip: H3 needs ONE ID per speaker for the whole film."""
    by_id: dict[str, dict[str, set[int]]] = {}
    for n, prompt in prompts.items():
        pairs = list(definition_speakers(prompt).items()) + [(name, sp) for name, sp in NAMED_SPEAKER.findall(prompt)]
        for name, speaker in pairs:
            if name.startswith('<Subject') or name.split()[0] in NOT_NAMES:
                continue
            speaker = re.sub(r'\s+', '', speaker)
            by_id.setdefault(speaker, {}).setdefault(name, set()).add(n)
    problems = []
    names_to_ids: dict[str, set[str]] = {}
    for speaker, names in by_id.items():
        for name in names:
            names_to_ids.setdefault(name.casefold(), set()).add(speaker)
    for name, ids in names_to_ids.items():
        if len(ids) > 1:
            where = {sp: sorted(n for nm, clips in by_id[sp].items() if nm.casefold() == name for n in clips) for sp in ids}
            problems.append(f'{name} speaks as ' + ' and '.join(f'{sp} in clip{"s" if len(c) > 1 else ""} {",".join(map(str, c))}'
                                                               for sp, c in sorted(where.items()))
                            + ': give each speaker ONE ID for the whole film')
    table = {sp: sorted(names, key=lambda x: -len(names[x])) for sp, names in sorted(by_id.items(), key=lambda kv: int(re.sub(r'\D', '', kv[0]) or 0))}
    for speaker, names in table.items():
        distinct = {x.split()[-1].casefold() for x in names}
        if len(distinct) > 1 and len(names) > 1:
            problems.append(f'{speaker} is used for different people ({", ".join(names[:4])}): one ID per speaker')
    return table, problems


def ref_uses(prompts: dict[int, str]) -> dict[str, list[int]]:
    """{"image4": [1, 3], ...}: which clips select each picture."""
    uses: dict[str, list[int]] = {}
    for n, prompt in sorted(prompts.items()):
        fields = header_fields(prompt)
        for name in fields.get('refs', '-').split(','):
            if re.fullmatch(r'(image|video|audio)\d+', name):
                uses.setdefault(name, []).append(n)
    return uses


def summarize(fixes: list[str], problems: list[str], limit: int = 3) -> str:
    out = []
    if fixes:
        out.append('fixed: ' + '; '.join(fixes))
    if problems:
        out.append(f'{len(problems)} problem{"s" if len(problems) > 1 else ""}: ' + ' | '.join(problems[:limit])
                   + (f' | and {len(problems) - limit} more' if len(problems) > limit else ''))
    return ' · '.join(out)


__all__ = ['check_clip', 'speaker_table', 'ref_uses', 'definition_speakers', 'speaker_names', 'summarize', 'header_fields']


def _selftest() -> Any:     # pragma: no cover - quick manual check
    sample = ("::VIDEO id=1 action=NEW mode=text duration=10s refs=image1,image2 first=- last=-\n"
              "subject_definitions: <Subject 1> is Walt, matching <Picture 1> (S2: flat). <Subject 2> is Jesse, matching <Picture 2> (S3).\n"
              "summary: [reference generation] x\nretention_analysis: x\ndetailed_description: [Shot 1] <Subject 2> says, "
              "<d>[English] Ay, isn't this</d> <Subject 1> says, <d>[English] Shut up!</d>\noverall_soundscape: x\nnon_diegetic_music: x\n::END")
    return check_clip(sample, 1)
