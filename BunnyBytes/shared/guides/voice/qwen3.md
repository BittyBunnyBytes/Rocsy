# Qwen3-TTS 1.7B Base — script guide

The Base model clones the character's voice from a short reference clip
(3-second clone). It reads the script literally; it has no instruction or emotion control.

## Quick syntax
- Write only the words to be spoken — no tags, stage directions or speaker labels (Base has no instruct control).
- 10 languages: Chinese, English, Japanese, Korean, German, French, Russian, Portuguese, Spanish, Italian. Match the Language selector.
- Punctuation shapes delivery: commas and full stops for pauses, "?" and "!" for intonation, "…" to trail off.
- Spell out numbers, dates, symbols and abbreviations the way they should sound.
- Characters with an exact transcript of their reference clip clone better.

## Details for Hermes
- Convey emotion only through wording and punctuation.
- Split very long passages into short paragraphs and sentences.
- Return only the script.

Sources: github.com/QwenLM/Qwen3-TTS (README: Base model, languages, ref_text / x_vector_only_mode).
