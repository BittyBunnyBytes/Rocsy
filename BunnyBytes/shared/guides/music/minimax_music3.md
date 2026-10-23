# MiniMax Music 3 — prompting guide

MiniMax Music 3 takes a caption (the studio's Music direction) and lyrics with
section tags. It follows how the song develops over time, not only a global style.

## Quick syntax
- Caption in three parts: Global Metadata (genre, subgenre, BPM, key, emotional progression, scenario, production) · Vocal Details (gender, timbre, performance, harmonies, effects) · Arrangement (instruments section by section, groove, bass, drums, textures, space).
- "The more specific, the closer the result" — a short sentence works, the structured caption controls more.
- Lyric tags, one per line: [Intro] [Verse] [Pre-Chorus] [Chorus] [Post-Chorus] [Bridge] [Instrumental] [Solo] [Outro].
- Tags carry structure; the lyric words carry mood. Never put lyrics in the caption.
- Instrumental songs stay instrumental: say so in the caption and leave lyrics empty.
- Songs up to about 300 s; tags guide but don't guarantee structure, key or tempo.

## Details for Hermes
Official caption-rewriter rules (MiniMax-AI/MiniMax-Music3, skills/music-caption-rewriter):
- Output exactly three headed parts in this order: Global Metadata, Vocal Details (or the lead instrument for instrumentals), Arrangement (a section-by-section timeline where instruments enter, change and exit coherently).
- About 250–450 English words by default (keep it under ~3,500 characters for the studio field).
- Preserve explicit user requirements and exclusions first; never reverse vocal gender, required instruments or tempo limits; never add vocals to an instrumental request.
- Don't invent exact BPM, key or vocalist identity when a broader description is enough.
- Use the lyrics only to infer emotion and intensity — never quote, paraphrase or summarize lyric lines in the caption.
- Only bracketed tags are directives; a section tag may change that section's arrangement without changing the global genre.
- No titles, track IDs or reasoning in the caption.

Sources: github.com/MiniMax-AI/MiniMax-Music3 (README, skills/music-caption-rewriter/SKILL.md); docs.comfy.org/tutorials/audio/minimax/minimax-music-3; demo: minimax-ai.github.io/music3-demo.
