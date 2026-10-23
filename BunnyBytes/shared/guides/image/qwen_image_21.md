# Qwen Image 2.1 — prompting guide

Qwen Image follows long, literal descriptions and renders text very well
(especially Chinese). With references on it edits and combines the inputs.

## Quick syntax
- Long, detailed descriptions work (Qwen-Image 2.x accepts very long instructions for posters, slides, infographics).
- Exact on-image text goes in "double quotes" (or Chinese quotes “ ” for Chinese text), with font, color and position.
- References: address them as <image1>, <image2>… in slot order; <image1> is the image being edited.
- Edits: name what changes and what must stay ("keep the face, hair and pose in <image1> unchanged").
- Mask edits: paint a color on <image1>, then say "change the jacket in the red area".
- The negative prompt only works when CFG is above 1 (the studio default CFG 1 skips it).

## Details for Hermes
- Text to image: one opening sentence naming medium, style, subject and background/palette, then inventory every object with color, material and position, then lighting and camera. Copy any user text character for character in its own script.
- Multi-image edits: say what each reference contributes ("put the light blue denim shirt from <image2> on the character in <image1>") and list what is preserved (facial features, hair, body shape, pose). Only use <imageN> tokens for slots that are enabled.
- Edit instructions are short and specific ("Change the rabbit's color to purple, with a flash light background").
- Official guidance warns edits are less stable without prompt rewriting, so always give a complete, explicit edit instruction.
- Never write a ratio, resolution or pixel count in the prompt; the studio sets size.
- Negative prompt (only effective with CFG > 1): low resolution, deformed limbs or fingers, oversaturated, waxy skin, missing facial detail, over-smoothed, AI look, chaotic composition, blurry or distorted text.

Sources: github.com/QwenLM/Qwen-Image; docs.comfy.org/tutorials/image/qwen/qwen-image-2-1.
