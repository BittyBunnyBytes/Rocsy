---
name: h3-prompting
description: Write or fix MiniMax H3 video prompts in the official format - Text (T2VA), Keyframe (I2VA / FL2VA / L2VA) and Reference / full-reference mode (ref2va, six sections with <Subject N>/<Picture N>/<Video N>/<Audio N> labels, task-type tags, retention markers). Use whenever the user asks to write, rewrite, convert, check or debug a prompt for H3, Video, or MiniMax H3, including character swaps, video edits, continuations, voice/audio references.
---

# MiniMax H3 prompting

H3 was trained on rewritten prompts in a fixed format. Prompts that follow it
render far more reliably than free prose; invented tags, invented retention
markers, and ALL-CAPS "do NOT show X" instructions are just noise and can pull
the wrong reference into the shot.

Pick the mode first:

| Video mode | Format | Read |
| --- | --- | --- |
| Text | T2VA: 3 fields | `references/t2va-i2va-fl2va-l2va-guide.md` |
| Keyframe (first / first+last / last) | I2VA / FL2VA / L2VA: an instruction line, a blank line, then the 3 fields | same guide, §2.1 and §3 |
| Reference | Full-reference: 6 sections | `references/full-reference-guide.md` (builds on the base guide for shots, camera, speech) |

Read the relevant guide before writing. The notes below are the rules that are
easiest to get wrong.

## Text / Keyframe (3 fields)

```
integrated_multimodal_description: [Shot 1] <style sentence> ... [Shot 2] At 00:05.000, the camera cuts to ...
overall_soundscape: ...
non_diegetic_music: ... | N/A
```

- `[Shot 1]` has no timestamp; later shots start `[Shot N] At MM:SS.mmm,`, strictly increasing and inside the duration.
- Camera: move type + optional `with small|large amplitude` + `at slow|fast speed`.
- Speakers get `(S1)`, `(S2)`... in order of first vocal event and keep it for the whole prompt. Lines: `<d>[English] exact words.</d>`. `<scenetrans>` at both ends of a line crossing a cut, `<cutoff>` when the video ends mid-line. Voiceovers add "while their lips remain completely closed."
- Visible text goes verbatim in straight double quotes.

## Reference mode (6 sections, in this order)

```
subject_definitions:
summary:
retention_analysis:
detailed_description:
overall_soundscape:
non_diegetic_music:
```

**Labels.** `<Picture N>`, `<Video N>`, `<Audio N>` are the attached references,
numbered per category in attachment order (Video shows the exact tokens on
each reference card and in the hotbar). `<Subject N>` is content *you* define
from them (a person, dog, scene, costume, motion) and is numbered by you. A
label keeps one meaning in every section.

- Something used only to define a character or scene: cite the picture inside
  the `<Subject N>` line; no separate `<Picture N>` line.
- A standalone `<Picture N>` line only when the image is an actual frame
  (first / key / last frame) or a storyboard.
- `<Video N>` only for whole-video roles: the edit source, the continuation
  source, or camera / cut / rhythm structure. People or objects taken from a
  video are still `<Subject N>`.
- A reference video's own soundtrack is its own `<Audio N>` (Video passes
  it unless muted); `<Video N>` / `<Audio N>` indices are independent.
- An audio bound to a speaker reuses that speaker's ID: `<Audio 1> is the
  voice-timbre reference for <Subject 1> (S1).`

**summary** starts with a task tag from exactly this set, combined with ` + `,
no repeats:
`keyframe completion`, `reference generation`, `video editing`,
`video continuation`, `audio reuse`, `audio reference`.
E.g. `[reference generation + audio reference]`, `[video editing + audio reuse]`.
Editing tasks continue with `The target video is an edited version of <Video 1>.`
No new labels in the summary. `[video editing + video continuation]` and
`[audio reuse + audio reference]` are rejected by the H3 Prompt IDE schema
(ComfyUI-H3-Prompt-IDE); pick the one that is true.

**retention_analysis**: one line per label, no `(Sx)` here.
- Visual (`Subject`/`Picture`/`Video`): `fully_preserved`, `partially_preserved`, `attribute_transfer`, `weak_reference`.
- Audio: `fully_copy`, `partially_copy`, `reference`, `weak_reference`.
- Forms: `<Subject 1> (appears in [Shot 1], [Shot 3]): fully_preserved - ...`,
  `<Picture 2> ([Shot 1] first frame): fully_preserved - ...`,
  `<Video 1> (cut and pacing structure): weak_reference - ...`,
  `<Audio 1>: reference - ...`.

**detailed_description**: one or two style sentences *before* `[Shot 1]`, then
shots as in the base guide. 350-500 words for generation (edits scale with the
source). Describe each shot concretely: composition, subject look and position,
environment, lighting, action, camera, sound, and where each label takes effect.
Re-describe a subject's key features at its first appearance. Frame anchors:
"the shot begins from <Picture 1>", "the shot's keyframe corresponds to
<Picture 2>", "the shot ends on <Picture 3>". A speaking subject is
`<Subject N> (Sx)`; off-screen keeps the same form plus `off-screen`. Reused
dialogue or lyrics go verbatim with `[unclear]` for unintelligible spans; when
only timbre is referenced, write new lines, not the reference's words.

**overall_soundscape / non_diegetic_music**: ambience and effects vs.
audience-only score. Say where an `<Audio N>` is copied or referenced in
whichever section matches its layer. Never repeat dialogue here. `N/A` when
there is no score.

## Proven recipe: swap a character into a video

Inserting a picture's character into a reference clip (e.g. Video 2026-09-26):
- `[video editing + reference generation + audio reuse] The target video is an edited version of <Video 1>. ...`
- `<Video 1>` fully_preserved, `<Picture 1>` attribute_transfer (its character replaces the person in the video), the clip's `<Audio 1>` fully_copy.
- Describe the video's actual scene concretely in every shot, with the new character in it.
- Output aspect = the video's aspect; duration = the trimmed clip length.

## Checklist before handing over a prompt

1. Six sections (Reference) or three fields (Text/Keyframe), keys exactly as spelled, in order.
2. Every label used later is defined in `subject_definitions`; label numbers match the attached references.
3. Task tag(s) and retention markers only from the fixed sets.
4. Timestamps increasing and within the duration; `[Shot 1]` has none.
5. Speaker IDs assigned once in vocal order and reused; none in retention_analysis.
6. All sections in English except words inside `<d>` and visible text.

## Length: 15 seconds max per prompt (Video)

One H3 prompt renders one clip of at most 15 seconds (5-15 s). For a longer video (30 seconds, a minute,
"a 40 s ad"), write several prompts: Clip 1, Clip 2, ... each 15 s or less, adding up to the requested
length (e.g. 40 s = 15 + 15 + 10). Every clip is its own complete prompt: shot timestamps restart at
00:00 in each, all inside that clip's own length. Keep characters, wardrobe, setting, style and voices
identical across clips, and make each clip pick up where the previous one ended (the same action, place
and speaker; a line of dialogue that runs past the cut ends with `<cutoff>` and continues in the next
clip). Label each block with its clip number and length, then give the total.
