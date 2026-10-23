# YuE2 — prompting guide

YuE2 renders a full song from a style string and section-tagged lyrics, with an
optional planned melody/chord score (the studio's Composition settings).

## Quick syntax
- Style = one comma-separated line: language, genre, vocal character, instruments, melody/feel, tempo.
  Official example: "English, warm piano pop, expressive female voice, acoustic piano, rounded bass and light drums, lyrical memorable melody, unhurried phrasing, 88 BPM".
- Lyrics: section tags on their own line — [Verse], [Chorus] (also [Bridge], [Outro]) — then the sung lines.
- Blank line between sections; short, singable lines (the official example uses 4 lines of ~7 syllables per section).
- Instrumental: say it in the style and leave lyrics empty, e.g. "Instrumental, …, no vocals, no singing, no choir, no spoken words."
- Only the words to sing go in lyrics — no notes, directions or speaker names.

## Details for Hermes
- Put genre, instruments, vocal character, language and tempo in the style (official). Keep tempo/meter consistent with any reference or score.
- Do not paste instructions to the model into the style ("make it longer", "follow the score"); it is a description of the music only.
- Repeat chorus words exactly each time the chorus returns.
- Fit the lyric amount to the track length setting; a 2-minute track is roughly verse, chorus, verse, chorus.
- Official instrumental example: "Instrumental, expressive chamber string ensemble, lyrical violin melody, warm viola and cello accompaniment, G major, 96 BPM, a repeated opening phrase followed by a short gentle ending, natural concert hall acoustics, no vocals, no singing, no choir, no spoken words."
- Composition modes (studio setting): full = melody + chord plan; melody = melody plan with free accompaniment (best for covers).

Sources: github.com/multimodal-art-projection/YuE (README, docs/generation.md, examples/song.json, skills/yue2-music).
