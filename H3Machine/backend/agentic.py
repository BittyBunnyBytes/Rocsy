"""Validated Hermes clip plans for H3 Reference-mode video continuation."""
from __future__ import annotations

import math
import re
from typing import Any

from .comfy import ServiceError
from .models import MINIMAX_H3_LORA_SLOTS, WAN_LORAS, WAN_MAX_SECONDS

WAN = "wan_2_2"

CONTINUATION_FRAME = "{{CONTINUATION_FRAME}}"
CONTINUATION_VIDEO = "{{CONTINUATION_VIDEO}}"
PLAN_GUIDE = f'''For multiple videos or a requested total longer than 15 seconds, return a clip plan instead of one prompt. Use this exact syntax, without fences or commentary:
::VIDEO id=1 action=NEW mode=text duration=10 refs=- exclude=- first=- last=-
A complete MiniMax H3 prompt for this clip.
::END
::VIDEO id=2 action=EXTEND from=1 mode=reference duration=10 refs=- exclude=- frame=previous
A complete six-section H3 full-reference prompt for the next clip. Use {CONTINUATION_FRAME} for the selected source clip's uploaded final-frame image. No generated video is attached as a video reference.
::END
Each ::VIDEO is one generation of 5-15 seconds. NEW starts a separate video. EXTEND must use mode=reference, frame=previous, and from=<earlier clip>; it is never Keyframe mode. Use from=1 on clip 3 to continue clip 1 across a cutaway in clip 2. In a resumed session use from=session:<completed clip number> to continue that exact final clip from any prior run; the context lists these numbers. from=0 remains shorthand for the latest completed clip. Never use a staging preview or failed attempt as a source. At execution, Agentic extracts and uploads ONLY the selected source clip's final frame as an image reference, then replaces {CONTINUATION_FRAME} with the actual <Picture N> label. The generated source video is never attached as a video reference; from= is used only to select and extract its frame. For a clean extension, use refs=- so the generated final frame is the only visual reference. Reuse original references only when the user explicitly needs them: refs=inherit keeps them, refs=inherit,image2 adds a named attached reference, refs=image2 uses only that named reference, and refs=- uses neither. exclude=image2 removes that attached reference even if inherited. Only name attached aliases in refs/exclude; never invent assets. Reserve one H3 image slot for the automatically uploaded continuation frame: at most 8 other images. Any user-selected video references keep the normal H3 limits, but never use a generated source video as a reference.
Every EXTEND prompt must use the six full-reference sections in order. In subject_definitions identify {CONTINUATION_FRAME} as the selected previous clip's exact final frame and the starting visual reference for the new clip. Do not introduce a <Video N> label for a generated source clip. In summary use exactly [reference generation]. The frame is an image reference for pose, identity, composition, scene and motion direction; do not claim a source video is attached or that a hard keyframe is enforced. In retention_analysis give {CONTINUATION_FRAME} its own line, '{CONTINUATION_FRAME}: fully_preserved - ...', with a detailed entry: describe what is actually happening in that frame and preserve exact subject identity, pose, paw placement, gaze, scene geometry, props, lighting, camera position, motion direction, and selected original references. Hermes may add context for what should happen next, but must not invent unseen past action. Do not ask to repeat earlier action or restart from a different pose. Prefer refs=- unless an original asset is specifically needed in addition to the final frame. In detailed_description begin [Shot 1] from the frame by name, e.g. '[Shot 1] Continuing from the exact pose and composition of {CONTINUATION_FRAME}, ...', then describe only new forward action after that instant. No establishing reset, replay of the previous clip, turn back toward camera, or repeated opening action. The source video's audio is never copied into the output; if the continuation-tail audio option is enabled, an audio-only extract may be attached for voice timbre and ambience. Describe new words and sound events only. For a NEW reference clip, use normal H3 Reference syntax and only the selected attached references. If a NEW clip selects any refs, its header MUST say mode=reference, never mode=text. If the user starts in Reference mode with uploaded assets, choose the needed aliases explicitly for the first clip so refs=inherit carries them into continuations. For a NEW text clip, use base-generation syntax. NEW keyframe clips may use first=First and last=Last if those images are attached. Preserve dialogue, timing, and continuity. Keep plans within 24 clips. If the input is already a ::VIDEO plan, review and revise that plan using the guide, keeping the requested number and order of clips unless the user asks otherwise. Return the full plan. When a PRIOR AGENTIC SESSION is supplied, output only NEW clips for this run; do not repeat any previously rendered clip. The prior session's last completed video is source 0, so use action=EXTEND from=0 mode=reference frame=previous to continue it. refs=inherit on from=0 or from=session:<number> keeps that source clip's selected original references. For one video of at most 15 seconds, Enhance may return just one refined prompt; Agentic always returns a ::VIDEO plan. Each clip may use at most 3 video references totalling 15 s, 9 images and 3 audio. EDITING ATTACHED FOOTAGE: when the references include segments of a source video and the user asks to change, replace or restyle something in it, make ONE NEW mode=reference clip per segment that contains the change, in time order, with duration equal to that segment's length (5-15 s), refs=<that segment>,<the replacement image>; in summary use [video editing] (plus + audio reuse to keep its soundtrack), name the segment as the source <Video N> being edited and keep its timing, camera and action, and change only what the user asked. Skip segments where the edited subject does not appear, and do not use two overlapping segments. References the user tagged in their prompt (<Picture 2>, <Video 1>, ...) keep the meaning the user gave them: use each one as it is in the clips their text puts it in, never trimmed, cropped, re-described as something else or swapped for downloaded media there; you may leave one out of a clip that does not need it (refs=/exclude=) or switch to different media in later clips. CLIP LENGTH AND MODE ARE YOURS TO CHOOSE: give each clip the duration its action and dialogue need (about 2.5 spoken words per second plus time for the action, 5-15 s) and the mode that fits it (text, reference with attached assets, keyframe, or a reference continuation), regardless of the Studio form's duration and mode, unless the user's request states lengths or a mode. DIALOGUE FORMAT (H3 voices gibberish otherwise; the service rejects plans that break it): (1) every spoken word goes inside <d>[Language] words.</d>, ending with . ? or ! before </d>. (2) Give each speaker one ID for the whole plan, (S1), (S2) and so on, and write it right after the speaker's name or <Subject N> in the same sentence as EVERY line they speak, continuations included: 'Peter (S2) says: <d>[English] Wait for me!</d> He laughs. Peter (S2) adds: <d>[English] Seriously.</d>' - never 'He continues: <d>...' without the ID. (3) Never put speech in quotation marks. To time an effect or reaction to a line, refer to it without quoting it again, e.g. 'as he says the line, huge letters slam across the frame'. Quotes are only for visible on-screen text (a sign reads "OPEN"). (4) A clip where nobody talks says 'No one speaks.' in its description. Keep the user's dialogue word for word except obvious typos, and keep the speaker visible on screen while speaking. If a generation reports an error, describe the reported failure and let the service retry the same clip; never propose, inspect, or edit application code. Before answering, check each clip: a valid header line, its ::END line, the required sections in order, a speaker ID beside every <d> line, and no quoted speech.'''
FIELD = re.compile(r'([a-z]+)=([^\s]+)', re.I)
PLAN_GUIDE += ('\nWhen an attached picture defines a named character, every NEW clip showing that character must '
               'use mode=reference, select the same picture in refs=, and use the six Reference sections. '
               'A text-only NEW clip loses that character\'s visual identity even if its name appears in the prompt.')
BLOCK = re.compile(r'(?ms)^::VIDEO\s+([^\n]+)\n(.*?)^::END\s*$')

# Wan 2.2 plans: the same ::VIDEO syntax, but Wan has Text and image-to-video
# (Keyframe) only, no references and no sound. A continuation is image-to-
# video from the source clip's final frame (set as the clip's first frame).
WAN_PLAN_GUIDE = f'''For a video longer than one clip, or several videos, return a clip plan. Use this exact syntax, without fences or commentary:
::VIDEO id=1 action=NEW mode=text duration=5 refs=- first=- last=-
A complete Wan 2.2 prompt for this clip.
::END
::VIDEO id=2 action=EXTEND from=1 mode=keyframe duration=5 frame=previous
A complete Wan 2.2 prompt that continues from the final frame of clip 1.
::END
Each ::VIDEO is one Wan 2.2 generation of 2-{WAN_MAX_SECONDS:g} seconds; 5 seconds is what Wan is trained on and looks best, so prefer 5 and split longer action into continuations. Wan renders 16 fps video with NO sound: never write dialogue, speech, lyrics, music or sound effects. Show communication through action, gesture and expression; visible writing is allowed as on-screen text (a sign reads "OPEN").
Modes: text (text to video) or keyframe (image to video). Wan has no Reference mode and takes no reference images or videos, so every clip uses refs=-. NEW starts a separate shot; a NEW mode=keyframe clip uses first=First, and optionally last=Last, only when those images are listed under Available keyframes.
EXTEND continues an earlier clip: it must use mode=keyframe frame=previous from=<earlier clip id>. At execution the selected clip's final frame becomes this clip's first frame, so the new clip starts exactly where that one ended. Use from=1 on clip 3 to come back to clip 1's shot after a cutaway in clip 2. In a resumed session use from=0 for the latest completed clip, or from=session:<number> for a completed clip the context lists. An EXTEND prompt describes only what happens AFTER that frame, starting from the subject's pose and the camera's position in it; never restate an establishing shot or replay earlier action. Use EXTEND for one continuous shot that runs longer than one clip, and NEW for a cut to a different shot or scene.
Write each prompt as plain prose, about 60-120 words, for one continuous shot: the subject and its appearance, the scene, the motion as it unfolds, camera movement and framing, lighting, colour and style. Repeat the subject's key appearance details in every clip so it stays consistent across cuts. No section labels, [Shot N] markers, <d> tags, speaker IDs or <Picture N>-style tokens.
CLIP LENGTH AND MODE ARE YOURS TO CHOOSE unless the user's request states them. Keep plans within 24 clips. If the input is already a ::VIDEO plan, review and revise it using the guide, keeping the requested number and order of clips unless the user asks otherwise. When a PRIOR AGENTIC SESSION is supplied, output only NEW clips for this run; do not repeat any previously rendered clip. Agentic always returns a ::VIDEO plan, even for one clip. If a generation reports an error, describe the failure and let the service retry the same clip; never propose, inspect, or edit application code. Before answering, check each clip: a valid header line, its ::END line, refs=-, and no dialogue or sound.'''


def plan_guide(model: str) -> str:
    return WAN_PLAN_GUIDE if model == WAN else PLAN_GUIDE


def prompt_label(model: str) -> str:
    """How the review/rewrite prompts name one clip prompt."""
    return "Wan 2.2" if model == WAN else "MiniMax H3"


def is_plan(value: str) -> bool:
    return bool(re.search(r'(?m)^::VIDEO\b', value))

REFERENCE_SECTIONS = ('subject_definitions:', 'summary:', 'retention_analysis:',
                      'detailed_description:', 'overall_soundscape:', 'non_diegetic_music:')


def character_reference_problems(clips: list[dict[str, Any]], aliases: dict[str, str]) -> list[tuple[int, str]]:
    """Keep an explicitly pictured character anchored across separate scenes."""
    bindings: dict[str, tuple[str, str]] = {}
    subject_line = re.compile(r'^\s*<Subject\s+\d+>\s+is\s+([A-Z][\w\'-]*(?:\s+[A-Z][\w\'-]*){0,2})\b([^\n]*)', re.M)
    picture = re.compile(r'\b(?:from|shown in|depicted in|based on)\s+<Picture\s+(\d+)>', re.I)
    for clip in clips:
        if clip['mode'] != 'ref2v':
            continue
        definitions = clip['prompt'].split('summary:', 1)[0]
        for match in subject_line.finditer(definitions):
            pictured = picture.search(match.group(2))
            if not pictured:
                continue
            alias = 'image' + pictured.group(1)
            ref_id = aliases.get(alias)
            if ref_id and ref_id in clip['reference_ids']:
                name = match.group(1).strip()
                if len(name) > 2:
                    bindings[name.casefold()] = (name, alias)
    problems = []
    for clip in clips:
        if clip['action'] != 'NEW':
            continue
        for name, alias in bindings.values():
            if not re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', clip['prompt'], re.I):
                continue
            if aliases[alias] not in clip['reference_ids']:
                problems.append((clip['id'], f'Clip {clip["id"]} shows {name} but omits {alias}, the image that defines this character. '
                                 f'Use mode=reference, refs={alias}, and a six-section Reference prompt for this clip.'))
    return problems


def repair_reference_headers(value: str) -> tuple[str, list[str]]:
    """Correct a NEW clip's text-mode typo only when its body is a full Reference prompt."""
    corrected: list[str] = []
    required = ('subject_definitions:', 'summary:', 'retention_analysis:',
                'detailed_description:', 'overall_soundscape:', 'non_diegetic_music:')

    def replace(match: re.Match[str]) -> str:
        header, body = match.group(1), match.group(2)
        fields = dict(FIELD.findall(header))
        if (fields.get('action', '').upper() != 'NEW' or fields.get('mode', '').lower() != 'text'
                or fields.get('refs', '-') == '-' or not all(part in body for part in required)):
            return match.group(0)
        corrected.append(fields.get('id', '?'))
        return match.group(0).replace('mode=text', 'mode=reference', 1)

    return BLOCK.sub(replace, value), corrected




def bind_continuation(prompt: str, frame_token: str) -> str:
    """Bind the selected source's final-frame placeholder after the image is uploaded."""
    return prompt.replace(CONTINUATION_FRAME, frame_token)


def _check_reference_sections(prompt: str, index: int) -> None:
    names = ('subject_definitions:', 'summary:', 'retention_analysis:',
             'detailed_description:', 'overall_soundscape:', 'non_diegetic_music:')
    positions = [prompt.find(name) for name in names]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        raise ServiceError(f'Clip {index} needs all six H3 Reference sections in order')


def _check_continuation_prompt(prompt: str, index: int) -> None:
    required = (CONTINUATION_FRAME, 'subject_definitions:', 'summary:',
                'retention_analysis:', 'detailed_description:', 'overall_soundscape:', 'non_diegetic_music:')
    if not all(piece in prompt for piece in required):
        raise ServiceError(f'Clip {index} needs the final-frame placeholder and all six H3 Reference sections')
    if CONTINUATION_VIDEO in prompt:
        raise ServiceError(f'Clip {index} cannot attach a generated source video; use only its final frame')
    if not re.search(r'(?is)summary:\s*\[reference generation\]', prompt):
        raise ServiceError(f'Clip {index} needs the reference generation task tag')
    # The guide allows citing the frame inside a subject's definition ('<Subject 1> is ... {{CONTINUATION_FRAME}}');
    # that subject's label then stands for the frame in the later sections.
    labels = [re.escape(CONTINUATION_FRAME), *map(re.escape, frame_labels(prompt))]
    analysis = prompt.split('retention_analysis:', 1)[1].split('detailed_description:', 1)[0]
    if not re.search(r'(?:' + '|'.join(labels) + r')[^\n]*fully_preserved\s*-\s*\S.{45,}', analysis, re.I):
        raise ServiceError(f'Clip {index} needs a detailed fully_preserved retention line for {CONTINUATION_FRAME} '
                           f"(e.g. '{CONTINUATION_FRAME}: fully_preserved - <what the frame shows>')")
    description = prompt.split('detailed_description:', 1)[1]
    if '[Shot 1]' not in description or not re.search('|'.join(labels), description):
        raise ServiceError(f'Clip {index} must begin Shot 1 from its continuation frame: name {CONTINUATION_FRAME} in [Shot 1]')


def frame_labels(prompt: str) -> list[str]:
    """<Subject N> labels whose definition cites the continuation frame."""
    definitions = prompt.split('subject_definitions:', 1)[-1].split('summary:', 1)[0]
    return [label for label, rest in re.findall(r'(?m)^\s*(<Subject \d+>)([^\n]*)', definitions) if CONTINUATION_FRAME in rest]


def bind_frame_in_shot1(prompt: str) -> tuple[str, bool]:
    """An EXTEND description that never names the continuation frame gets it at the start of [Shot 1]
    (a description written as one unlabeled shot becomes [Shot 1])."""
    if CONTINUATION_FRAME not in prompt or 'detailed_description:' not in prompt:
        return prompt, False
    head, description = prompt.split('detailed_description:', 1)
    labels = [CONTINUATION_FRAME, *frame_labels(prompt)]
    has_shot = '[Shot 1]' in description
    if has_shot and any(label in description for label in labels):
        return prompt, False
    opening = f'[Shot 1] The shot opens on the exact composition of {CONTINUATION_FRAME}.'
    if has_shot:
        description = description.replace('[Shot 1]', opening, 1)
    elif re.search(r'\[Shot \d+\]', description):
        return prompt, False  # numbered shots without a Shot 1: Hermes has to renumber them
    else:
        description = '\n' + opening + ' ' + description.lstrip()  # one unlabeled shot
    return head + 'detailed_description:' + description, True


SPEECH_VERB_QUOTE = re.compile(r'(?i)\b(?:says?|said|asks?|asked|shouts?|shouted|yells?|yelled|whispers?|whispered|replies|replied|'
                               r'exclaims?|exclaimed|mutters?|muttered|adds?|answers?|answered|complains?|sings?|sang)\b[^<"\n]{0,40}["“]')
NO_SPEECH = re.compile(r'(?i)\b(?:no one|nobody|no-one)\s+(?:speaks|talks|says)|\bno (?:dialogue|dialog|speech|talking|spoken|voices?|lines)\b'
                       r'|\bwithout (?:speaking|dialogue|talking|a word)|\b(?:is|are|remains?|stays?) (?:silent|wordless)')
SPEAKER_ID = re.compile(r'\(S\d+(?:\s*,\s*S\d+)*\)')
# "Maya (S1)", "Maya Lin (S1)", "<Subject 2> (S1)": the name a plan gave a speaker ID.
NAMED_SPEAKER = re.compile(r"(<Subject \d+>|\b[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)?)\s*(\(S\d+\))")
NOT_NAMES = {'The', 'A', 'An', 'He', 'She', 'They', 'It', 'His', 'Her', 'Their', 'Then', 'And', 'As', 'When', 'While'}
# Where the sentence that introduces a spoken line starts: after the previous line, sentence end, newline or shot tag.
# 'He says nothing as a sign reads "OPEN"': a quote of visible text, not speech.
ON_SCREEN_TEXT = re.compile(r'(?i)\b(?:reads?|reading|shows?|spells?|labell?ed|lettering|letters|written|text|sign|caption|title)\b')
SENTENCE_BREAK = re.compile(r'</d>|[.!?]["”\']?\s+|\n|\[Shot \d+\]\s*', re.I)


def _norm_words(text: str) -> str:
    return ' '.join(re.sub(r'[^\w\s]', ' ', re.sub(r'^\s*\[[^\]]*\]', '', text).lower()).split())


def _sentence_start(text: str, position: int) -> int:
    start = 0
    for match in SENTENCE_BREAK.finditer(text, 0, position):
        start = match.end()
    return start


def _has_speaker(text: str, position: int) -> bool:
    """A speaker ID in the sentence that introduces the line, or within 300 characters before it."""
    return bool(SPEAKER_ID.search(text, min(_sentence_start(text, position), max(0, position - 300)), position))


def _quoted_lines(text: str) -> list[tuple[re.Match[str], str]]:
    """Speech-verb quotes outside <d> tags, with the quoted words."""
    found = []
    outside = re.sub(r'(?is)<d>.*?</d>', lambda m: ' ' * len(m.group(0)), text)
    for match in SPEECH_VERB_QUOTE.finditer(outside):
        if ON_SCREEN_TEXT.search(match.group(0)):
            continue
        close = re.search(r'["”]', outside[match.end():match.end() + 400])
        found.append((match, outside[match.end():match.end() + close.start()] if close else ''))
    return found


def speaker_names(text: str) -> dict[str, str]:
    """Names the plan already tied to one speaker ID (a name used with two IDs is left out)."""
    names: dict[str, str | None] = {}
    for name, speaker in NAMED_SPEAKER.findall(text):
        candidates = [name] + ([name.split()[-1]] if ' ' in name and not name.startswith('<') else [])
        for candidate in candidates:
            if candidate.split()[0] in NOT_NAMES:
                continue
            names[candidate] = speaker if names.get(candidate, speaker) == speaker else None
    return {name: speaker for name, speaker in names.items() if speaker}


def fill_speaker_ids(prompt: str, names: dict[str, str], only_speaker: str | None = None) -> tuple[str, int]:
    """Add the (Sx) a spoken line is missing when its speaker is unambiguous: the sentence names a speaker the plan
    already numbered, or it starts with he/she/they and the whole plan has exactly one speaker."""
    fixed = 0
    position = 0
    while (match := re.compile(r'(?i)<d>').search(prompt, position)):
        position = match.end()
        start = _sentence_start(prompt, match.start())
        if SPEAKER_ID.search(prompt, start, match.start()):
            continue  # the introducing sentence already names the speaker's ID
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


def quotes_to_dialogue(prompt: str, language: str | None) -> tuple[str, int]:
    """Quoted speech -> <d>[Language] ...</d> when the plan uses one language; quotes that repeat a line already in
    <d> tags (an effect timed to that line) are left alone - the check accepts them."""
    if not language:
        return prompt, 0
    spoken = [_norm_words(body) for body in re.findall(r'(?is)<d>(.*?)</d>', prompt)]
    fixed = 0
    for match, words in reversed(_quoted_lines(prompt)):
        normalized = _norm_words(words)
        if not normalized or any(normalized in line for line in spoken):
            continue
        quote_start = match.end() - 1
        quote_end = match.end() + len(words) + 1
        prompt = prompt[:quote_start] + f'<d>[{language}] {words.strip()}</d>' + prompt[quote_end:]
        fixed += 1
    return prompt, fixed


def normalize_dialogue(text: str) -> tuple[str, int]:
    """Trivial dialogue fixes that shouldn't cost a Hermes repair round: end each spoken line with punctuation."""
    fixed = 0

    def finish(match: re.Match[str]) -> str:
        nonlocal fixed
        body = match.group(1).rstrip()
        if re.match(r'\[[A-Za-z ]+\]\s*\S', body) and not re.search(r'[.?!]["”\']?$', body):
            fixed += 1
            return f'<d>{body}.</d>'
        return match.group(0)

    return re.sub(r'(?is)<d>(.*?)</d>', finish, text), fixed


def _relocate_prefix(prompt: str, mode: str) -> tuple[str, bool]:
    """LoRA trigger phrases written before the first section label (a LoRA note saying 'start the prompt with ...')
    move to where H3 expects style words: right after [Shot 1] in a text clip (T2VA), or the style opening at the
    start of detailed_description, before [Shot 1], in a Reference clip."""
    if mode not in {'text', 'ref2v'}:
        return prompt, False
    original = prompt
    label = 'subject_definitions:' if mode == 'ref2v' else 'integrated_multimodal_description:'
    changed = False
    first = prompt.find(label)
    if mode == 'text' and any(name in prompt for name in ('subject_definitions:', 'detailed_description:')):
        return prompt, False  # a Reference prompt under a text header; the header check reports it
    if mode == 'text' and first < 0 and 'overall_soundscape:' in prompt and (shot := prompt.find('[Shot 1]')) >= 0:
        # Three-field text prompt missing its first label: label the [Shot 1] text.
        prompt = prompt[:shot] + 'integrated_multimodal_description: ' + prompt[shot:]
        first, changed = shot, True
    moved = []
    if first > 0 and 0 < len(prefix := prompt[:first].strip()) < 400 and '[Shot' not in prefix and '<' not in prefix:
        moved.append(prefix)
        prompt = prompt[first:]
    if mode == 'text':
        # T2VA states style after [Shot 1]: text squeezed between the label and [Shot 1] moves after it too.
        body = prompt.find(label)
        shot = prompt.find('[Shot 1]', body) if body >= 0 else -1
        between = prompt[body + len(label):shot].strip() if shot > 0 else ''
        if 0 < len(between) < 300 and '<' not in between and ':' not in between:
            moved.append(between)
            prompt = prompt[:body + len(label)] + ' ' + prompt[shot:]
    if not moved:
        return prompt, changed
    lead = ' '.join(re.sub(r'\[Shot \d+\]', '', part).strip() for part in moved).strip()
    lead = re.sub(r'(?i)^(?:start with|prompt with)\s*:\s*', '', lead).rstrip(' ,;')
    lead += '' if re.search(r'[.!?]$', lead) else ','
    if mode == 'text':
        at = prompt.find('[Shot 1]', max(0, prompt.find(label)))
        at = at + len('[Shot 1]') if at >= 0 else -1
    else:
        at = prompt.find('detailed_description:')
        at = at + len('detailed_description:') if at >= 0 else -1
    if at < 0:
        return original, False  # nowhere sensible to put it; leave the prompt as written
    gap = '\n' if mode == 'ref2v' else ' '
    prompt = prompt[:at] + gap + lead + ' ' + prompt[at:].lstrip()
    return prompt, True


def normalize_plan(text: str, model: str = "minimax_h3") -> tuple[str, list[str]]:
    """Deterministic fixes applied before validation so formatting slips don't cost Hermes repair rounds.
    Every fix is H3 syntax (sections, dialogue tags, Reference headers), so a Wan plan is left as written."""
    if model == WAN:
        return text, []
    notes: list[str] = []
    text, corrected = repair_reference_headers(text)
    if corrected:
        notes.append('Corrected NEW clip ' + ', '.join(corrected) + ' to Reference mode because its full Reference prompt selected an image')
    names = speaker_names(text)
    speakers = set(SPEAKER_ID.findall(text))
    only_speaker = next(iter(speakers)) if len(speakers) == 1 else None
    languages = set(re.findall(r'(?i)<d>\s*\[([A-Za-z ]+)\]', text))
    language = next(iter(languages)) if len(languages) == 1 else None
    counts = {'quotes': 0, 'speakers': 0, 'triggers': 0, 'frames': 0, 'headers': 0}

    def fix_block(match: re.Match[str]) -> str:
        header, body = match.group(1), match.group(2)
        fields = dict(FIELD.findall(header))
        if fields.get('action', '').upper() == 'EXTEND' and all(part in body for part in REFERENCE_SECTIONS):
            # EXTEND is always mode=reference frame=previous; fill in what the header left out.
            fixed_header = re.sub(r'\bmode=(?:text|keyframe)\b', 'mode=reference', header) if 'mode=' in header else header + ' mode=reference'
            fixed_header = re.sub(r'\bframe=-(?=\s|$)', 'frame=previous', fixed_header) if 'frame=' in fixed_header else fixed_header + ' frame=previous'
            if fixed_header != header:
                header, fields = fixed_header, dict(FIELD.findall(fixed_header))
                counts['headers'] += 1
        mode = {'text': 'text', 'reference': 'ref2v', 'keyframe': 'fl2v'}.get(fields.get('mode', '').lower(), '')
        body, moved = _relocate_prefix(body, mode)
        if fields.get('action', '').upper() == 'EXTEND':
            body, bound = bind_frame_in_shot1(body)
            counts['frames'] += bound
        body, quoted = quotes_to_dialogue(body, language)
        body, filled = fill_speaker_ids(body, names, only_speaker)
        counts['triggers'] += moved
        counts['quotes'] += quoted
        counts['speakers'] += filled
        return f'::VIDEO {header}\n{body}' + ('' if body.endswith('\n') else '\n') + '::END'

    text = BLOCK.sub(fix_block, text.strip() + '\n') if is_plan(text) else text
    text, punctuated = normalize_dialogue(text)
    if counts['triggers']:
        notes.append(f"Moved LoRA trigger/style text to the start of [Shot 1] in {counts['triggers']} clip(s)")
    if counts['headers']:
        notes.append(f"Completed {counts['headers']} EXTEND header(s) with mode=reference frame=previous")
    if counts['frames']:
        notes.append(f"Named {CONTINUATION_FRAME} at the start of [Shot 1] in {counts['frames']} continuation clip(s)")
    if counts['quotes']:
        notes.append(f"Converted {counts['quotes']} quoted line(s) to <d>[{language}] ...</d>")
    if counts['speakers']:
        notes.append(f"Added the speaker ID to {counts['speakers']} spoken line(s)")
    if punctuated:
        notes.append(f'Added end punctuation to {punctuated} spoken line(s)')
    return text, notes


def normalize_prompt(prompt: str, mode: str, context: str = '', model: str = "minimax_h3") -> str:
    """normalize_plan for one clip prompt; context (the clip's previous prompt or the plan) supplies speaker names."""
    if model == WAN:
        return prompt
    names = speaker_names(context + '\n' + prompt)
    speakers = set(SPEAKER_ID.findall(context + '\n' + prompt))
    languages = set(re.findall(r'(?i)<d>\s*\[([A-Za-z ]+)\]', context + '\n' + prompt))
    prompt, _ = _relocate_prefix(prompt, mode)
    prompt, _ = bind_frame_in_shot1(prompt)
    prompt, _ = quotes_to_dialogue(prompt, next(iter(languages)) if len(languages) == 1 else None)
    prompt, _ = fill_speaker_ids(prompt, names, next(iter(speakers)) if len(speakers) == 1 else None)
    return normalize_dialogue(prompt)[0]


def dialogue_problems(prompt: str, index: int) -> list[str]:
    """Every spoken word in <d>[Language] ...</d> after a named speaker (Sx); a clip without lines says no one speaks.
    Loose or missing dialogue formatting is what makes H3 voice gibberish. Returns every problem, not just the first,
    so one repair round can fix them all."""
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


def _check_dialogue(prompt: str, index: int) -> None:
    if problems := dialogue_problems(prompt, index):
        raise ServiceError('; '.join(problems[:8]) + (f'; and {len(problems) - 8} more' if len(problems) > 8 else ''))


class PlanProblems(ServiceError):
    """Content problems in specific clips of a structurally valid plan: all of them, so one repair fixes everything,
    and per clip, so the repair can rewrite only those clips."""
    def __init__(self, problems: list[tuple[int, str]]):
        self.problems = problems
        self.clip_ids = sorted({index for index, _ in problems})
        shown = [message for _, message in problems[:16]]
        super().__init__(' | '.join(shown) + (f' | and {len(problems) - 16} more' if len(problems) > 16 else ''))


def plan_blocks(text: str) -> dict[int, str]:
    """{clip id: full ::VIDEO ... ::END block} for every block whose header has a numeric id."""
    blocks = {}
    for match in BLOCK.finditer(text.strip() + '\n'):
        clip_id = dict(FIELD.findall(match.group(1))).get('id', '')
        if clip_id.isdigit():
            blocks[int(clip_id)] = match.group(0).rstrip()
    return blocks


def splice_clips(plan: str, reply: str, clip_ids: list[int]) -> str | None:
    """Put the repaired blocks for clip_ids into plan. None when the reply lacks any of them."""
    fixed = plan_blocks(reply)
    if not all(clip_id in fixed for clip_id in clip_ids):
        return None
    wanted = set(clip_ids)

    def swap(match: re.Match[str]) -> str:
        clip_id = dict(FIELD.findall(match.group(1))).get('id', '')
        return fixed[int(clip_id)] if clip_id.isdigit() and int(clip_id) in wanted else match.group(0).rstrip()

    return BLOCK.sub(swap, plan.strip() + '\n').strip()


def parse(text: str, available: dict[str, str], first_id: str | None, last_id: str | None,
          resume_source: bool = False, resume_reference_ids: list[str] | None = None,
          session_sources: dict[str, dict[str, Any]] | None = None,
          allow_characters: bool = False, allow_loras: bool = False,
          known_characters: set[str] | None = None, allow_scenes: bool = False,
          known_scenes: set[str] | None = None, model: str = "minimax_h3") -> list[dict[str, Any]]:
    """Validate every clip and reference choice before queueing any generation."""
    if not is_plan(text):
        raise ServiceError('No ::VIDEO clip plan was found')
    if model == WAN:
        return _parse_wan(text, first_id, last_id, resume_source, session_sources, allow_loras)
    source = text.strip() + '\n'
    blocks = list(BLOCK.finditer(source))
    if not blocks or len(blocks) > 24 or BLOCK.sub('', source).strip():
        raise ServiceError('Clip plan must contain 1-24 complete ::VIDEO / ::END blocks')
    aliases = {key.lower(): value for key, value in available.items()}
    result: list[dict[str, Any]] = []
    seen_characters = set(known_characters or ())
    seen_scenes = set(known_scenes or ())
    problems: list[tuple[int, str]] = []
    for index, block in enumerate(blocks, 1):
        matches = FIELD.findall(block.group(1))
        fields = dict(matches)
        if len(fields) != len(matches) or FIELD.sub('', block.group(1)).strip():
            raise ServiceError(f'Clip {index} has an invalid header')
        if fields.get('id') != str(index):
            raise ServiceError(f'Clip IDs must be consecutive from 1 (clip {index})')
        action = fields.get('action', '').upper()
        mode = {'text': 'text', 'reference': 'ref2v', 'keyframe': 'fl2v'}.get(fields.get('mode', '').lower())
        if action not in {'NEW', 'EXTEND'} or not mode:
            raise ServiceError(f'Clip {index} needs action=NEW/EXTEND and mode=text/reference/keyframe')
        raw_characters = fields.get('chars', '-')
        characters = [] if raw_characters == '-' else raw_characters.lower().split(',')
        if characters and (not allow_characters or len(characters) > 3 or len(set(characters)) != len(characters)
                           or any(not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name) for name in characters)):
            raise ServiceError(f'Clip {index} has invalid experimental character names')
        if action == 'NEW' and mode == 'text' and any(name in seen_characters for name in characters):
            raise ServiceError(f'Clip {index} reuses a captured character; write a six-section Reference prompt with mode=reference')
        raw_scene = fields.get('scene', '-')
        scene = '' if raw_scene == '-' else raw_scene.lower()
        if scene and (not allow_scenes or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', scene)):
            raise ServiceError(f'Clip {index} has an invalid experimental scene name')
        if action == 'NEW' and mode == 'text' and scene in seen_scenes:
            raise ServiceError(f'Clip {index} returns to an earlier scene ({scene}); write a six-section Reference prompt '
                               'with mode=reference so the scene screenshot can be attached')
        raw_lora = fields.get('lora', '-')
        if raw_lora == '-':
            lora_slot = None
        elif not allow_loras or not raw_lora.isdigit() or not 1 <= int(raw_lora) <= len(MINIMAX_H3_LORA_SLOTS):
            raise ServiceError(f'Clip {index} selects an unavailable LoRA gallery slot')
        else:
            lora_slot = int(raw_lora) - 1
        try:
            duration = float(fields['duration'].removesuffix('s'))
        except (KeyError, ValueError):
            raise ServiceError(f'Clip {index} has no valid duration') from None
        if not math.isfinite(duration) or not 5 <= duration <= 15:
            raise ServiceError(f'Clip {index} duration must be 5-15 seconds')
        if action == 'EXTEND':
            if mode != 'ref2v' or fields.get('frame') != 'previous' or fields.get('first', '-') != '-':
                raise ServiceError(f'Clip {index} EXTEND needs mode=reference frame=previous')
            raw_parent = fields.get('from', '')
            if re.fullmatch(r'session:[1-9][0-9]*', raw_parent):
                parent = raw_parent
                if parent not in (session_sources or {}):
                    raise ServiceError(f'Clip {index} names an unavailable completed session clip: {parent}')
            else:
                try:
                    parent = int(raw_parent)
                except ValueError:
                    raise ServiceError(f'Clip {index} needs from=<earlier clip>, from=0, or from=session:<number>') from None
                if parent < 0 or parent >= index or (parent == 0 and not resume_source):
                    raise ServiceError(f'Clip {index} must extend an earlier clip or a completed session source')
        else:
            parent = None
            if fields.get('frame', '-') != '-':
                raise ServiceError(f'Clip {index} NEW cannot use frame=previous')
        def named_references(raw: str) -> list[str]:
            if raw == '-':
                return []
            names = raw.split(',')
            if any(name.lower() not in aliases for name in names):
                raise ServiceError(f'Clip {index} names an unattached reference')
            return [aliases[name.lower()] for name in names]
        raw_refs = fields.get('refs', '-')
        parts = raw_refs.split(',')
        inherit = 'inherit' in parts
        if inherit and (action != 'EXTEND' or parts.count('inherit') != 1):
            raise ServiceError(f'Clip {index} can inherit references only from an earlier clip')
        explicit = named_references(','.join(part for part in parts if part != 'inherit') or '-')
        excluded = named_references(fields.get('exclude', '-'))
        if set(explicit) & set(excluded):
            raise ServiceError(f'Clip {index} includes and excludes the same reference')
        inherited = ((session_sources or {})[parent].get('reference_ids', []) if isinstance(parent, str)
                     else result[parent - 1]['reference_ids'] if parent else (resume_reference_ids or [])) if inherit else []
        included = list(dict.fromkeys([*inherited, *explicit]))
        included = [ref for ref in included if ref not in excluded]
        if mode != 'ref2v' and included:
            raise ServiceError(f'Clip {index} lists refs outside Reference mode')
        if action == 'NEW' and mode == 'ref2v' and not included and not any(name in seen_characters for name in characters) \
                and scene not in seen_scenes:
            raise ServiceError(f'Clip {index} NEW Reference mode needs an attached reference')
        if action == 'EXTEND':
            first = last = None
        else:
            first = first_id if fields.get('first', '-') == 'First' else None
            last = last_id if fields.get('last', '-') == 'Last' else None
            if fields.get('first', '-') not in {'-', 'First'} or fields.get('last', '-') not in {'-', 'Last'}:
                raise ServiceError(f'Clip {index} names an unavailable keyframe')
            if mode == 'fl2v' and not first:
                raise ServiceError(f'Clip {index} needs an attached first frame')
        prompt = block.group(2).strip()
        if not prompt or len(prompt) > 20_000:
            raise ServiceError(f'Clip {index} needs a prompt under 20,000 characters')
        # Prompt-content problems are collected for every clip and reported together (see PlanProblems).
        def check(function: Any, *args: Any) -> None:
            try:
                function(*args)
            except ServiceError as exc:
                problems.append((index, str(exc)))
        if mode == 'ref2v':
            check(_check_reference_sections, prompt, index)
        problems.extend((index, message) for message in dialogue_problems(prompt, index))
        if action == 'EXTEND':
            check(_check_continuation_prompt, prompt, index)
        elif CONTINUATION_FRAME in prompt or CONTINUATION_VIDEO in prompt:
            problems.append((index, f'Clip {index} NEW cannot use continuation placeholders'))
        for label, number in re.findall(r'<(Picture|Video|Audio)\s+(\d+)>', prompt, re.I):
            category = {'picture': 'image', 'video': 'video', 'audio': 'audio'}[label.lower()]
            alias = f'{category}{int(number)}'
            if alias not in aliases:
                problems.append((index, f'Clip {index} names unattached <{label} {number}>'))
            elif aliases[alias] not in included:
                problems.append((index, f'Clip {index} names <{label} {number}> but does not select {alias} in refs='))
        for alias, ref_id in aliases.items():
            if ref_id not in excluded:
                continue
            visible = re.fullmatch(r'(image|video|audio)([0-9]+)', alias)
            tokens = [r'(?<!\w)' + re.escape(alias) + r'(?!\w)']
            if visible:
                kind = {'image': 'Picture', 'video': 'Video', 'audio': 'Audio'}[visible.group(1)]
                tokens.append(r'<' + kind + r'\s+' + visible.group(2) + r'>')
            if any(re.search(token, prompt, re.I) for token in tokens):
                problems.append((index, f'Clip {index} names an excluded reference ({alias}) in its prompt'))
        result.append(dict(id=index, action=action, source=parent, mode=mode, duration=duration,
                           reference_ids=included, exclude_reference_ids=excluded,
                           first_frame_id=first, last_frame_id=last, prompt=prompt,
                           characters=characters, lora_slot=lora_slot, scene=scene))
        seen_characters.update(characters)
        if scene:
            seen_scenes.add(scene)
    problems.extend(character_reference_problems(result, aliases))
    if problems:
        raise PlanProblems(problems)
    return result


WAN_SOUND = re.compile(r'(?i)<d>|\(S\d+\)|\[Shot \d+\]|<(?:Picture|Video|Audio|Subject)\s+\d+>|^\s*[a-z_]+_(?:description|definitions|analysis|soundscape|music):')


def _parse_wan(text: str, first_id: str | None, last_id: str | None, resume_source: bool,
               session_sources: dict[str, dict[str, Any]] | None, allow_loras: bool) -> list[dict[str, Any]]:
    """WAN_PLAN_GUIDE's rules. Clips come back in parse()'s shape; an EXTEND clip's first frame is
    the source's final frame, filled in when it renders (_agentic_prepare_render)."""
    source = text.strip() + '\n'
    blocks = list(BLOCK.finditer(source))
    if not blocks or len(blocks) > 24 or BLOCK.sub('', source).strip():
        raise ServiceError('Clip plan must contain 1-24 complete ::VIDEO / ::END blocks')
    result: list[dict[str, Any]] = []
    problems: list[tuple[int, str]] = []
    for index, block in enumerate(blocks, 1):
        matches = FIELD.findall(block.group(1))
        fields = dict(matches)
        if len(fields) != len(matches) or FIELD.sub('', block.group(1)).strip():
            raise ServiceError(f'Clip {index} has an invalid header')
        if fields.get('id') != str(index):
            raise ServiceError(f'Clip IDs must be consecutive from 1 (clip {index})')
        action = fields.get('action', '').upper()
        mode = {'text': 'text', 'keyframe': 'fl2v'}.get(fields.get('mode', '').lower())
        if action not in {'NEW', 'EXTEND'} or not mode:
            raise ServiceError(f'Clip {index} needs action=NEW/EXTEND and mode=text/keyframe (Wan 2.2 has no Reference mode)')
        if fields.get('refs', '-') != '-' or fields.get('exclude', '-') != '-':
            raise ServiceError(f'Clip {index}: Wan 2.2 takes no reference media; use refs=-')
        if fields.get('chars', '-') != '-' or fields.get('scene', '-') != '-':
            raise ServiceError(f'Clip {index}: chars= and scene= are MiniMax H3 Reference features; leave them out')
        raw_lora = fields.get('lora', '-')
        if raw_lora == '-':
            lora_slot = None
        elif not allow_loras or not raw_lora.isdigit() or not 1 <= int(raw_lora) <= len(WAN_LORAS):
            raise ServiceError(f'Clip {index} selects an unavailable Wan LoRA gallery entry')
        else:
            lora_slot = int(raw_lora) - 1
        try:
            duration = float(fields['duration'].removesuffix('s'))
        except (KeyError, ValueError):
            raise ServiceError(f'Clip {index} has no valid duration') from None
        if not math.isfinite(duration) or not 2 <= duration <= WAN_MAX_SECONDS:
            raise ServiceError(f'Clip {index} duration must be 2-{WAN_MAX_SECONDS:g} seconds for Wan 2.2')
        first = last = None
        if action == 'EXTEND':
            if mode != 'fl2v' or fields.get('frame') != 'previous' or fields.get('first', '-') != '-' or fields.get('last', '-') != '-':
                raise ServiceError(f'Clip {index} EXTEND needs mode=keyframe frame=previous and no first=/last=')
            raw_parent = fields.get('from', '')
            if re.fullmatch(r'session:[1-9][0-9]*', raw_parent):
                parent: int | str = raw_parent
                if parent not in (session_sources or {}):
                    raise ServiceError(f'Clip {index} names an unavailable completed session clip: {parent}')
            else:
                try:
                    parent = int(raw_parent)
                except ValueError:
                    raise ServiceError(f'Clip {index} needs from=<earlier clip>, from=0, or from=session:<number>') from None
                if parent < 0 or parent >= index or (parent == 0 and not resume_source):
                    raise ServiceError(f'Clip {index} must extend an earlier clip or a completed session source')
        else:
            parent = None
            if fields.get('frame', '-') != '-':
                raise ServiceError(f'Clip {index} NEW cannot use frame=previous')
            if fields.get('first', '-') not in {'-', 'First'} or fields.get('last', '-') not in {'-', 'Last'}:
                raise ServiceError(f'Clip {index} names an unavailable keyframe')
            first = first_id if fields.get('first', '-') == 'First' else None
            last = last_id if fields.get('last', '-') == 'Last' else None
            if (fields.get('first', '-') == 'First' and not first_id) or (fields.get('last', '-') == 'Last' and not last_id):
                raise ServiceError(f'Clip {index} names a keyframe that is not attached')
            if mode == 'fl2v' and not first:
                raise ServiceError(f'Clip {index} keyframe mode needs first=First (an attached first frame); use mode=text otherwise')
            if mode == 'text' and (first or last):
                raise ServiceError(f'Clip {index} uses first=/last= keyframes, so it needs mode=keyframe')
        prompt = block.group(2).strip().replace(CONTINUATION_FRAME, 'the first frame')
        if not prompt or len(prompt) > 20_000:
            raise ServiceError(f'Clip {index} needs a prompt under 20,000 characters')
        if WAN_SOUND.search(prompt):
            problems.append((index, f'Clip {index} uses MiniMax H3 syntax (sections, [Shot N], <d> dialogue, speaker IDs or '
                             'reference tokens). Write one plain-prose Wan 2.2 prompt with no dialogue or sound.'))
        result.append(dict(id=index, action=action, source=parent, mode=mode, duration=duration,
                           reference_ids=[], exclude_reference_ids=[], first_frame_id=first, last_frame_id=last,
                           prompt=prompt, characters=[], lora_slot=lora_slot, scene='', model=WAN))
    if problems:
        raise PlanProblems(problems)
    return result


def check_wan_prompt(prompt: str, index: int) -> None:
    """A rewritten Wan clip prompt: plain prose, no H3 syntax, dialogue or placeholders."""
    if WAN_SOUND.search(prompt) or CONTINUATION_FRAME in prompt or CONTINUATION_VIDEO in prompt:
        raise ServiceError(f'Clip {index}: write one plain-prose Wan 2.2 prompt - no section labels, [Shot N], <d> dialogue, '
                           'speaker IDs, reference tokens or {{CONTINUATION_FRAME}} placeholders, and no sound')
