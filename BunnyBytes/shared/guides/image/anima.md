# Anima — prompting guide

Anima (circlestone-labs) is an anime/illustration model trained on Danbooru-style
tags and natural-language captions. Tags and sentences can be mixed in any order.

## Quick syntax
- Tag order: quality/meta/year/safety → 1girl/1boy/… → character → series → @artist → general tags.
- Recommended start: "masterpiece, best quality, score_7, safe, " (pick the safety tag: safe, sensitive, nsfw, explicit).
- Tags are lowercase with spaces, not underscores ("long hair", "looking at viewer"); only score_N keeps its underscore.
- Artists need the @ prefix ("@artist name") — without it the effect is very weak.
- Natural language: at least two sentences; name a character, then describe how they look.
- Weights need higher values than SDXL, e.g. (chibi:2).

## Details for Hermes
- Quality tags: human (masterpiece, best quality … worst quality) and/or aesthetic (score_9 … score_1); either, both or neither work.
- Year/period tags: "year 2025" or newest, recent, mid, early, old. Meta tags: highres, absurdres, anime screenshot, official art.
- Prefer the Gelbooru spelling when Danbooru and Gelbooru tags differ. The model was trained with tag dropout, so not every tag is needed.
- Capitalize character and series names normally in sentences. In multi-character prompts, name each character then describe that character right after.
- Aesthetic version: quality tags are optional ("masterpiece, best quality" is safe); avoid score_* tags in both prompts — they over-stylize.
- Turbo version: strong default style, less variety.
- Weak at photorealism by design; text works only for single words or very short phrases.
- Recommended negative prompt: "worst quality, low quality, score_1, score_2, score_3, artist name, blurry, jpeg artifacts, chromatic aberration" (drop the score tags for the Aesthetic version).
- Short or vague prompts can produce unwanted content: always include a safety tag and enough description. Characters must be adults for nsfw/explicit.
- No LoRA or reference panel for Anima in this studio: never use LoRA triggers or <imageN> tokens.

Sources: huggingface.co/circlestone-labs/Anima (model card).
