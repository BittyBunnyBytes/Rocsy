# Krea 2 — prompting guide

Applies to every Krea 2 checkpoint in the studio, including finetunes such as
Lustify (Lustify is only a Krea 2 checkpoint; it uses this guide).

## Quick syntax
- Natural-language prompts are recommended (official): describe the image, don't list keywords.
- Longer, detailed prompts give the best results; short prompts still work.
- Put any words to render in "quotes" and say where they appear.
- Honor the medium you want: "photograph of", "illustration of", "3D render of", "painting of".
- One cohesive paragraph: subjects with their own attributes and actions, then setting, light, framing, style.
- LoRA trigger words go first (the hotbar inserts them). Turbo/CFG-off: no negative prompt — state what you want.

## Details for Hermes
Krea's official expansion rules (docs/expansion.txt in krea-ai/krea-2), adapted:
1. Faithfulness first: preserve all original subjects, actions, colors and spatial relationships. Do not add objects, props, characters or animals the user did not imply.
2. Practical structure: group each subject with its own attributes and actions; use grounded phrasing for poses, interactions and spatial layout.
3. Choose style, medium, framing and lighting yourself (consider two or three options, pick the one that best serves the request) but never emit planning notes in the prompt.
4. Text rendering: give the exact text and wrap the words in quotes.
5. Avoid over-specification: don't invent very specific clothing, colors, materials or scene details the request doesn't support.
6. Output one cohesive paragraph — no bullets, JSON or markdown inside the prompt.
7. If the user's prompt is already detailed, lightly polish it and keep their phrasing and direction.
8. Preserve the user's medium; never switch medium to dodge a difficulty.

Official examples range from one line ("immense rocket launch exhaust as seen from extremely close up") to full paragraphs such as: "3D rendered matte black designer toy figure, stylized round anthropomorphic shape, backward black baseball cap, oversized gold-rimmed aviator sunglasses, … smooth vinyl texture, studio lighting, solid vibrant blue background, high contrast minimal composition".

Studio notes:
- Turbo checkpoints run with CFG disabled, so negative wording has no effect; describe the desired result positively.
- References (style-reference LoRA) restyle toward the attached images; describe the new subject and scene, not the reference's content.
- When enabled LoRAs list trigger words in the studio settings, start the prompt with those exact triggers.
- Lustify (NSFW photoreal finetune): be concrete and physical about people, pose and camera; everyone depicted must be clearly adult.
- Never put resolution, aspect ratio or step counts in the prompt.

Sources: github.com/krea-ai/krea-2 (README, docs/prompting.md, docs/expansion.txt).
