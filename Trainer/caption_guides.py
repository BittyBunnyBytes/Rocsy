"""Per-model captioning guidance for auto-tagging.

Each guide says how the model being trained wants its captions written (booru tags or
sentences, what to include, how long), following the model's own prompting conventions.
The tagger appends the chosen guide to its system prompt; "auto" picks one from the dataset.
"""
from __future__ import annotations

import re
from typing import Any

TAGS, NATURAL = "tags", "natural"

GUIDES: list[dict[str, Any]] = [
    # ---- video models ----
    {"id": "h3", "label": "MiniMax H3", "group": "Video", "format": NATURAL, "families": ["minimax_h3"],
     "text": "Captions are natural sentences in MiniMax H3's own prompt order: subject (look, clothing), then the action "
             "in the order it happens with precise verbs and directions, then the setting and lighting, then the camera "
             "(shot size, static/pan/track/zoom/handheld) and the look (live action, 3D, 2D animation). 25-60 words."},
    {"id": "ltx", "label": "LTX 2.5", "group": "Video", "format": NATURAL, "families": ["ltx25"],
     "text": "LTX prompts are one flowing, chronological paragraph. Start with the main action, then specific movements and "
             "gestures, the subject's appearance, the background, the camera angle and movement, and the lighting and colours. "
             "Write it like a cinematographer describing the shot. 40-100 words."},
    {"id": "wan", "label": "Wan 2.1 / 2.2", "group": "Video", "format": NATURAL, "families": ["aitk:wan"],
     "text": "Wan prompts follow subject + scene + motion + camera + look: who or what, where, what moves and how, then "
             "camera language (close-up, low angle, tracking shot, dolly in) and the visual style or lighting "
             "(soft daylight, neon, film grain, 3D cartoon). 30-80 words."},
    # ---- natural-language image models ----
    {"id": "qwen", "label": "Qwen Image 2.1 / Qwen Image", "group": "Image", "format": NATURAL,
     "families": ["qwen_image_21", "aitk:qwen_image"],
     "text": "Qwen Image is trained on long, detailed descriptions read by a vision-language encoder. Describe the subject, "
             "then where things are in the frame (left, right, foreground, background), materials, colours, lighting, "
             "and the medium (photo, illustration, 3D render). Copy any visible text exactly, inside double quotes, "
             "and say where it is (a sign above the door reads \"OPEN LATE\"). 50-120 words."},
    {"id": "krea2", "label": "Krea 2", "group": "Image", "format": NATURAL, "families": ["krea2"],
     "text": "Krea 2 favours photographic, art-directed descriptions: subject and pose, composition and framing, lighting "
             "(direction, quality, colour), lens and film look when clear (shallow depth of field, wide angle, 35mm grain), "
             "colour palette and mood. Plain prose, 30-80 words."},
    {"id": "flux", "label": "Flux (Flux.1 / Flux.2 Klein / Chroma)", "group": "Image", "format": NATURAL,
     "families": ["flux1", "klein9b", "aitk:flux", "aitk:chroma", "aitk:flex"],
     "text": "Flux reads natural language through a large text encoder: put the subject first, then concrete details "
             "(clothing, materials, colours, expression), the setting, lighting and the medium. No tag lists and no quality "
             "words such as masterpiece or 8k. 30-80 words."},
    {"id": "moss", "label": "MOSS-Image", "group": "Image", "format": NATURAL, "families": ["moss_image"],
     # OpenMOSS / Fizgig docs/MOSS_IMAGE.md: trained on long, detailed captions (short/medium/long variants),
     # Qwen3.5-9B encoder reads up to 4,096 tokens, renders English and Chinese text well.
     "text": "MOSS-Image was trained on long, detailed captions and its Qwen3.5-9B encoder reads up to 4,096 tokens, "
             "so describe the picture fully: the subject and its details, pose or action, where everything sits in the frame "
             "(foreground, background, left, right), materials, colours, lighting, mood and the medium (photo, illustration, "
             "3D render, poster). It renders text well: copy every visible word exactly as written, English or Chinese, inside "
             "double quotes, and say where it is and how it looks (font, colour). 60-150 words, in clear sentences."},
    {"id": "natural", "label": "Other modern models (SD3, Lumina, HunyuanImage, HiDream)", "group": "Image",
     "format": NATURAL, "families": ["sd3", "lumina2", "hunyuan_image21", "aitk:sd35", "aitk:lumina", "aitk:hidream"],
     "text": "These models read natural language. Write one clear descriptive paragraph: subject, details, action or pose, "
             "setting, lighting, medium. No tag lists and no quality words. 30-80 words."},
    # ---- tag-based image models ----
    {"id": "anima", "label": "Anima", "group": "Image", "format": TAGS, "families": ["anima"],
     # circlestone-labs/Anima model card: Danbooru-style tags (+ natural language), lowercase with spaces,
     # order [quality/meta/year/safety] [1girl/1boy/1other] [character] [series] [@artist] [general tags].
     "text": "Anima is trained on Danbooru-style tags. Write tags in lowercase with spaces instead of underscores; when "
             "Danbooru and Gelbooru name a tag differently, use the Gelbooru name. Order, after the trigger word: one safety "
             "tag (safe, sensitive, nsfw or explicit), the subject count (1girl, 1boy, 1other, 2girls, solo), the character "
             "and series names only if you are certain (written in lowercase like any tag), an artist "
             "only if certain and always written with @ in front (@artist name), then the general tags: identity traits "
             "(species, hair, eyes, body, markings), clothing, pose and action, expression, framing (portrait, upper body, "
             "full body, from above), setting and background, medium (anime screenshot, sketch, 3d). Do not add quality, "
             "score or year tags: they are prompt-time controls, not image content. The model was trained with tag dropout, "
             "so 20-45 accurate tags beat an exhaustive list."},
    {"id": "sdxl", "label": "SDXL / Pony / Illustrious", "group": "Image", "format": TAGS, "families": ["sdxl"],
     "text": "SDXL fine-tunes such as Pony and Illustrious are trained on Danbooru tags (e621 tags for furry and anthro content, "
             "e.g. anthro, digitigrade, fur). Order: subject count, character and series if certain, identity traits, "
             "clothing, pose, expression, framing, background, style or medium. No score_9, masterpiece or other quality tags."},
    {"id": "sd15", "label": "SD 1.5", "group": "Image", "format": TAGS, "families": ["sd15"],
     "text": "SD 1.5's CLIP encoder reads only about 75 tokens, so keep it to the 15-30 most important tags, most important "
             "first: subject count, identity traits, clothing, pose, framing, background. No quality tags."},
    {"id": "booru", "label": "Generic booru tags", "group": "Image", "format": TAGS, "families": [],
     "text": "Use Danbooru-style tags, most important first: subject count, identity traits, clothing, pose and action, "
             "expression, framing, background, style or medium."},
]
BY_ID = {g["id"]: g for g in GUIDES}

QUALITY_TAG = re.compile(r"^(score_\d\w*|masterpiece|best quality|high quality|highres|absurdres|ultra detailed|8k|4k|year \d{4}|newest|recent|normal quality|good quality|low quality|worst quality)$", re.I)


def for_family(family_id: str) -> str | None:
    """The guide whose `families` list matches a Trainer family id (prefix match for aitk:* recipes)."""
    for g in GUIDES:
        if any(family_id == f or (f.startswith("aitk:") and family_id.startswith(f)) for f in g["families"]):
            return g["id"]
    return None


def looks_like_tags(captions: list[str]) -> bool:
    """Most captions are short comma-separated phrases (booru style) rather than sentences."""
    votes = 0
    for c in captions:
        parts = [p.strip() for p in c.split(",") if p.strip()]
        if len(parts) >= 5 and sum(len(p.split()) for p in parts) / len(parts) <= 4 and ". " not in c:
            votes += 1
    return bool(captions) and votes * 2 > len(captions)


def resolve(target: str, has_video: bool, captions: list[str]) -> dict[str, Any]:
    """`target` is a guide id, a Trainer family id, or "auto" (videos → H3; images → tags if the
    existing captions are tags, else generic natural language)."""
    if target in BY_ID:
        return BY_ID[target]
    if target != "auto" and (gid := for_family(target)):
        return BY_ID[gid]
    if has_video:
        return BY_ID["h3"]
    return BY_ID["booru" if looks_like_tags(captions) else "natural"]


def clean_tags(text: str, trigger: str, style: str, limit: int = 60) -> str:
    """Model reply → "trigger, tag, tag, ..." (deduplicated, no quality tags, spaces not underscores)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^(tags|caption|answer)\s*:\s*", "", text, flags=re.I)
    trig = {trigger.casefold(), trigger.replace("_", " ").casefold()} if trigger else set()
    seen, out = set(trig), []
    for raw in re.split(r"[,\n]+", text):
        tag = raw.strip().strip("-*•#\"'“”").strip().rstrip(".")
        if not tag or QUALITY_TAG.match(tag) or tag.casefold() in trig:
            continue
        if len(tag) > 3:   # booru names use spaces; short emoticon tags (^_^) keep theirs
            tag = tag.replace("_", " ")
        tag = " ".join(tag.split()).lower()   # booru tags are lowercase (Anima: "use lowercase for tags")
        if QUALITY_TAG.match(tag) or len(tag) > 60 or tag.casefold() in seen:
            continue
        seen.add(tag.casefold())
        out.append(tag)
    out = out[:limit]
    if style.strip() and style.strip().casefold() not in seen:
        out.append(style.strip())
    return ", ".join(([trigger] if trigger else []) + out)
