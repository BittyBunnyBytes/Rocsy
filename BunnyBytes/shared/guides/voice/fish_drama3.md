# Fish Audio Drama 3 — script guide

Fish Audio's open-weight Drama 3 (83 languages): voice cloning from the character's
reference clip and its transcript, with delivery cues written inline in [square brackets].
Runs locally as a quantized GGUF in audio.cpp; the reference transcript is required.

## Quick syntax
- Put an event tag exactly where it should happen: "[sighs] Fine. If we miss it, it's on you. [laughs] Well, maybe that's fair."
- Cues are free-form words in square brackets ([sighs], [laughs], [whispers]); there is no fixed list.
- Long scripts are split into parts automatically; keep each cue next to the sentence it controls.
- Punctuation shapes pauses and intonation; spell out numbers, symbols and abbreviations.

## Details for Hermes
- Use cues sparingly: the local port notes that some emotion and nonverbal tags can run long, drop the next line or stall, so one cue per sentence at most and only when the request asks for a delivery.
- Follow a laugh or sigh cue with real dialogue text; never end a part on a cue alone.
- Speaker tags `<|speaker:0|>`…`<|speaker:15|>` exist in the model, but Rocsy voices each part with one character, so do not use them.
- Return only the words to be spoken, with any cues inline.

Sources: huggingface.co/fishaudio/drama-3 model card; audio.cpp docs/models/fish_drama3.md (validation limits).
