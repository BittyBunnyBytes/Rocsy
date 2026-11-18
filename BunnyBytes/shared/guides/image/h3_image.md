# MiniMax H3 Image — prompting guide

MiniMax H3 Image is a distilled still-image model (6 steps, no CFG, no negative prompt). It reads long, literal
English captions very well, renders exact on-image text, and edits or combines up to 10 reference pictures.
This guide is condensed from MiniMax's own prompt-expander instructions (the H3 Image "PE" prompts in ComfyUI's
official templates).

## Quick syntax
- Write one fluent English paragraph describing the finished image: medium and style first, then every subject with position, pose, materials and colours, then setting, lighting and camera.
- Exact on-image text goes in "double quotes" with its font feel, colour and position; it is rendered literally.
- References are <Picture 1>, <Picture 2>… in slot order; <Picture 1> is the picture being edited.
- Edits: name each kept element as <Subject N> from its <Picture N>, say it is fully or partially preserved, then describe only what changes or is new.
- There is no negative prompt (CFG 1): state what you want, positively.
- Never write a ratio, resolution or pixel count; the studio sets the size (or follows <Picture 1>).

## Details for Hermes

### Text to image (no references)
- Preserve every hard fact in the request exactly: quoted text, names, counts, numbers, formulas, left/right/top/bottom order, style and medium.
- Required content must be visible, not implied. If a label or text is requested, put it on a surface and say where it sits.
- Decide what kind of image it is:
  - A scene or photograph: prioritize subject identity, pose, contact, scale, environment, lighting, and lens or medium.
  - A designed artifact (poster, card, infographic, UI, chart, diagram, packaging, cover, multi-panel): plan the layout in reading order before writing. Use slots such as header/title, subtitle, hero visual, panels, cards, legend, footer. Give each slot its position, its literal text, what it shows, and its style. Never write vague fillers like "icons", "details" or "annotations" without saying what they show.
- People and action:
  - Fix the scene layout first, then choose one camera.
  - State each body's orientation relative to the camera (front-facing, back-facing, left/right profile, three-quarter) and its frame direction (leftward, toward the camera…).
  - Keep gaze, lean, limbs and support consistent with one frozen moment.
  - For hands, grips, hugs, held tools: frame close enough to read the contact, and say which hand touches what.
- Respect the subject count: don't add extra main figures. Background people stay small.
- Use world knowledge only as visible evidence: materials, tools, labels, units, clothing, architecture. No invisible lore.
- Style comes after content. Replace mood adjectives with visible cues (light direction, palette, texture, lens).
- The official example captions are long and concrete. The official poster example is about 600 words: every object gets colour, material, position and lighting. Richness comes from precise visible detail, not from repetition.

### Editing and combining references (<Picture N>)
- Write one continuous paragraph in three parts:
  1. **Definitions.** Name each source element that stays visible or lends a property, as `<Subject N>`, with its `<Picture N>` and a few identifying cues. For example: "The woman in a red coat in <Picture 1> is <Subject 1>." Use the same Subject number for the same person across pictures.
  2. **Overview.** One sentence starting "The target image…" giving the main change. Then one relationship clause per Subject, each in its own sentence:
     - "<Subject N> is fully preserved." Nothing else, and never describe that Subject again.
     - Partially preserved: what stays and what changes.
     - Attribute transfer: which property moves to which carrier. For example: "<Subject 1> supplies the exact colour transferred to <Subject 2>".
     - Weak reference: a borrowed style, palette or composition the new design may vary.
  3. **Changes only.** Describe vividly only the changed and new content: new objects, new background, new text (in full, exact), and how they integrate. Integration covers scale, contact, shadows, lighting, perspective and occlusion. Don't re-describe preserved Subjects; use their bare `<Subject N>` token as an anchor.
- Copy readable source text exactly once, in Part 1. Write changed text in full in Part 3. Never guess illegible text.
- Only use <Picture N> numbers for references that are actually enabled; list them individually (never "the reference images").
- Content that is brand new has no Subject number. A removed element: mention it once with its <Picture N>, then describe what fills the space.

### Size and quality
- H3 Image works best at large sizes (about 4 to 20 MP; the studio default is 2048×2048, up to about 16.9 MP). Small renders look weak. The size is the studio's setting, not the prompt's.
- With "Output follows Reference 1" the picture keeps <Picture 1>'s shape. Describe the result for that frame.

Sources: ComfyUI workflow templates image_minimax_h3_image_t2i / image_minimax_h3_image_edit (MiniMax H3 Image PE system prompts); huggingface.co/MiniMaxAI/MiniMax-H3-Image-PE; huggingface.co/Comfy-Org/MiniMax-H3-Image.
