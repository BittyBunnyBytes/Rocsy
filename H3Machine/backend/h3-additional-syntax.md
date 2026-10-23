# MiniMax H3: Additional Prompt Syntax and Rules

Companion to **MiniMax h3 Guide.md**, which covers full-reference mode. This reference collects the base-generation syntax from MiniMax's *Video Prompt Writing Guide (T2VA / I2VA / FL2VA / L2VA)* and the terms shown in the supplied prompt-editor screenshots. A palette button is often a writing shortcut, not a special model token.

## 1. Choose the prompt structure

| Task | Opening instruction | Body |
| --- | --- | --- |
| T2VA, text to audiovisual video | None | Three core fields below |
| I2VA, first frame | First-frame alignment line | Three core fields |
| FL2VA, first and last frames | Two-frame alignment line | Three core fields |
| L2VA, last frame | Last-frame alignment line | Three core fields |
| Full-reference generation or editing | Follow the separate full-reference guide | Six sections, with `detailed_description` instead of `integrated_multimodal_description` |

The base-generation fields, in order, are:

```text
integrated_multimodal_description: [Shot 1] ...
overall_soundscape: ...
non_diegetic_music: ...
```

`integrated_multimodal_description` contains the timed visual and audible events. `overall_soundscape` summarizes ambience, physical sounds and nonverbal sounds. `non_diegetic_music` describes music heard by the audience but not by the characters. Put diegetic music, speech and synchronized sound in the shot description.

### Keyframe instruction lines

The instruction comes first, followed by a blank line and then the three fields. Use the actual final shot number and duration; `S.SS` has exactly two decimal places.

```text
I2VA:
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

FL2VA:
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.

L2VA:
How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.
```

I2VA starts with the reference image's identity, composition and objects, then develops the action forward. FL2VA describes a visible path between the two frame states; the guide generally favors one continuous shot unless cuts are requested. L2VA constructs a plausible opening that converges on the supplied last image.

## 2. Shot, time and transition syntax

- Start with `[Shot 1]` and **no timestamp**. Later shots use `[Shot 2] At 00:03.500, ...`, with sequential numbers and strictly increasing cut times inside the clip's duration.
- Express normal cuts in prose: “the camera cuts to,” “the shot cuts to,” “transitions to,” “changes to,” or “switches to.” A cut should reveal a meaningful change of view, subject, state, space or time.
- `cross-dissolve`, `fade` and `wipe` are descriptive transitions, used when requested; they are not markup tags. A camera move is often clearer than a cut for a slight change of distance or angle.
- State style and initial composition at the beginning of Shot 1. Examples of style words in the palette: `Cinematic`, `live-action`, `2D-animated`, `3D CG`, `claymation`, `watercolor`, `vintage film`. These are ordinary descriptive words.
- Composition words shown in the palette include `Medium-Wide Shot`, `Medium Shot`, `Close Shot`, `Close-Up`, and `Extreme Close-Up`. They are framing descriptions, not special syntax.

## 3. Camera language

Write movement as an action inside the shot, optionally adding **amplitude** and **speed** when they matter: “The camera pushes in with small amplitude at slow speed toward the letter.” Do not stack camera labels at the end of the prompt.

| Palette term | Meaning |
| --- | --- |
| Zoom In / Zoom Out | Change focal length while camera stays put |
| Push In / Pull Out | Move camera forward / backward |
| Pan Left / Pan Right | Pivot horizontally from one position |
| Truck Left / Truck Right | Translate camera horizontally |
| Tilt Up / Tilt Down | Pivot vertically |
| Pedestal Up / Pedestal Down | Move entire camera vertically |
| Arc Shot | Move on an arc around the subject |
| Tracking Shot | Follow a moving subject |
| Static Shot | Hold camera and lens still |
| Shake Slightly / Shake Strongly | Specify intensity of camera shake |
| POV | Show a subject's point of view |
| Roll Clockwise / Roll Counterclockwise | Rotate around the lens axis |

The optional phrases are `with small amplitude`, `with large amplitude`, `at slow speed`, and `at fast speed`. Medium amplitude and normal speed are usually implicit. The screenshot abbreviations `Roll CW` and `Roll CCW` mean clockwise and counterclockwise; spell them out in prose.

## 4. Speech, lyrics and cut continuity

- Give each vocal source a stable `(S1)`, `(S2)`, etc. A nonvocal character needs no speaker ID. Already numbered speakers speaking together may use `(S1,S2)`.
- Put the speaker's identity, voice quality, action and delivery **outside** the dialogue tag. Put only the language tag and actual words inside: `The woman (S1) says: <d>[English] Wait for me!</d>`.
- Preserve user-supplied dialogue and lyrics exactly in base-generation prompts. Do not silently rewrite or translate them. In full-reference reuse, follow that guide's distinct rules for source transcription and unclear audio.
- For voiceover, use “says in an off-screen voiceover” and explicitly keep the corresponding visible character's lips closed.
- For a line crossing a shot cut, use `<scenetrans>` at the connecting points of the split line and say that the audio continues across the cut. Use `<cutoff>` when speech is cut off by the end of the video. The adjacent phrases “across the cut,” “into next shot,” “carries over,” and “remains audible” are continuity prose, not tags.
- These literal tags are already introduced in the full-reference guide; the base guide supplies additional use cases.

## 5. Visible text and sound fields

- Put the exact words of visible signs, banners, labels, neon text or subtitles in English double quotation marks, preserving their original language and punctuation: `A sign reading "营业中" glows over the doorway.` The palette's “subtitle,” “banner,” “label,” and “neon” are text categories, not tags.
- Write `overall_soundscape` as one paragraph of roughly 1–4 sentences. Do not repeat spoken lines or music already described within shots. Use `overall_soundscape: N/A` only if complete silence is explicitly requested.
- Write `non_diegetic_music` as roughly 1–3 sentences describing instruments, tempo, rhythm and changes in volume. Use `non_diegetic_music: N/A` if there is no audience-only music. Music from a radio, phone, instrument or singer within the scene belongs in the shot description.

## 6. Full-reference palette terms shown in image(3)

These terms largely belong to **MiniMax h3 Guide.md** and are included here to identify what the screenshot buttons mean. Follow that guide for their precise definitions and permitted retention relationships.

| Palette group | Literal syntax versus shortcut |
| --- | --- |
| Structure | Literal section names: `subject_definitions:`, `summary:`, `retention_analysis:`, `detailed_description:`, `overall_soundscape:`, `non_diegetic_music:`. Buttons labeled “soundscape,” “music,” or “Template” are shortcuts, not alternate field names. |
| Definitions | `<Subject N>`, `<Picture N>`, `<Video N>`, and `<Audio N>` are reference labels. “Subject is...,” “Picture = frame,” “Storyboard,” “Edit source,” “Continue from,” “Video structure,” “Voice timbre,” and “Video's audio” are prose cues for defining roles. |
| Task type | The screenshot includes `keyframe completion`, `reference generation`, `video editing`, `video continuation`, `audio reuse`, `audio reference`, and `edited version`. Select only a task type that accurately describes the request; do not concatenate the buttons as magic tokens. |
| Retention | Relationship markers such as `fully_preserved`, `partially_preserved`, `attribute_transfer`, `weak_reference`, `fully_copy`, `partially_copy`, and `reference` are literal values when allowed for that reference role. “Subject line,” “Picture line,” “Video line,” and “Audio line” are editor shortcuts. |
| Description | “Style opening,” “begins from,” “keyframe,” “ends on,” “voice from,” “off-screen,” “audio cue,” “copied ambience,” and “reused score” tell the writer what to describe. `[unclear]` is a literal transcription marker for unintelligible source speech. |

The palette's `music: N/A` means `non_diegetic_music: N/A` in the finished six-section prompt. Never replace the official field name with a button's abbreviated label.

## Quick validation

1. Choose the structure for the actual generation mode; do not put the full-reference six sections into an ordinary T2VA prompt.
2. Check that picture/video/audio labels match the attached inputs and their assigned roles.
3. Verify timestamps, sequential shots, final duration and any keyframe alignment.
4. Verify speaker IDs, language tags, visible text, audio continuity and music placement.
5. Treat camera, style, composition and most palette buttons as natural-language descriptions; reserve literal markup for documented fields, labels and tags.

## Sources

- MiniMax, [Video Prompt Writing Guide (T2VA / I2VA / FL2VA / L2VA)](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md).
- Supplied **MiniMax h3 Guide.md** (Full-Reference Mode Rewrite Output Format Guide).
- Supplied screenshots **image(1).png**, **image(2).png**, and **image(3).png** (prompt-editor palettes; labels interpreted against the guides).
