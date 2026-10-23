# IndexTTS2 — script guide

Zero-shot voice cloning; timbre comes from the character's clip. Emotion is a
separate input in IndexTTS2 (reference audio, an 8-value vector, or a text
description), not markup in the script.

## Quick syntax
- Plain spoken text only — no bracket or parenthesis tags (they would be read aloud).
- Punctuation controls pauses and intonation.
- Chinese pronunciation can be forced inline with tone-numbered pinyin, e.g. 行 → XING2 (not every syllable is supported).
- When emotion-from-text is on, the wording itself sets the emotion, so write the lines the way the character feels them.
- Spell out numbers and acronyms as spoken.

## Details for Hermes
- Keep sentences a natural length; split very long sentences.
- Emotion vector order (for reference only; not part of the script): happy, angry, sad, afraid, disgusted, melancholic, surprised, calm.
- Return only the words to be spoken.

Sources: github.com/index-tts/index-tts (README: pinyin control, emo_audio_prompt / emo_vector / use_emo_text / emo_text).
