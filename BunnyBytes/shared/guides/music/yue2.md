# YuE2 — prompting guide

YuE2 renders a full song from a style string and section-tagged lyrics, with an
optional planned melody/chord score (the studio's Composition settings).

## Quick syntax
- Style = one comma-separated line: language, genre, vocal character, instruments, melody/feel, tempo.
  Official example: "English, warm piano pop, expressive female voice, acoustic piano, rounded bass and light drums, lyrical memorable melody, unhurried phrasing, 88 BPM".
- Lyrics: section tags on their own line — [Verse], [Chorus] (also [Bridge], [Outro]) — then the sung lines.
- Blank line between sections; short, singable lines (the official example uses 4 lines of ~7 syllables per section).
- Instrumental: say it in the style and leave lyrics empty, e.g. "Instrumental, …, no vocals, no singing, no choir, no spoken words."
- Only the words to sing go in lyrics — no notes, directions or speaker names. Tags are directions and are never sung; anything else, (parentheses) included, is sung.
- Normal sentence case. No lines in ALL CAPS: they split into arbitrary tokens and hurt the take.

## Details for Hermes
- Put genre, instruments, vocal character, language and tempo in the style (official). Keep tempo/meter consistent with any reference or score.
- Do not paste instructions to the model into the style ("make it longer", "follow the score"); it is a description of the music only.
- Repeat chorus words exactly each time the chorus returns.
- Stress: a capital in the middle of a word (recORD) breaks the tokenizer there and marks a syllable boundary; a whole word in caps does not. Use it sparingly, and only when the user asks for a stress fix: changing case changes the whole take, not just that word. In full and melody Composition modes the score decides which syllable lands on the beat, which is the stronger lever.
- Backing vocals or ad-libs in (parentheses) are sung as written, so only use them for words that should be sung.
- Set `duration` to fit the lyrics, about 1.5 sung words a second plus intros and breaks; a track can't go past 300 seconds, and a 2-minute track is roughly verse, chorus, verse, chorus.
- Official instrumental example: "Instrumental, expressive chamber string ensemble, lyrical violin melody, warm viola and cello accompaniment, G major, 96 BPM, a repeated opening phrase followed by a short gentle ending, natural concert hall acoustics, no vocals, no singing, no choir, no spoken words."
- Composition modes (studio setting): full = melody + chord plan; melody = melody plan with free accompaniment (best for covers).

Sources: pytraveler/YuE2-ComfyUI README ("Writing lyrics", "Stress, and what capital letters really do"); github.com/multimodal-art-projection/YuE (README, docs/generation.md, examples/song.json, skills/yue2-music).
