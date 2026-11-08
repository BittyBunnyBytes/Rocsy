# Wan 2.2: prompting guide

Wan 2.2 renders one silent shot of a few seconds (16 fps; 5 s is what it was
trained on) from an English prose prompt. It renders no audio at all.

## Quick syntax
- One paragraph of plain English prose for one continuous shot. No headings, labels, `[Shot N]`, `<d>` tags or `<Picture N>` tokens.
- Formula: subject (and its look) + scene + motion, then the cinematic settings.
- Cinematic settings go at the front as short comma-separated terms, at most about four, e.g. `Edge lighting, medium close-up shot, daylight, left-heavy composition. A young girl ...`
- A style the user asked for comes first of all (`Anime-style thick-painted style. ...`). For 2D or illustrated styles, leave out the film-lighting terms.
- No sound, speech, music or lyrics: show communication through action, gesture and expression. Visible writing is allowed as text in the picture (a sign reads "OPEN").
- Text to video: about 60-200 words. Image to video: 100 words or less.

## Details for Hermes
Cinematic settings to choose from (pick the ones that fit; you don't need all of them):
- Time: Day time, Night time, Dawn time, Sunrise time (Day time unless the prompt says otherwise).
- Light source: Daylight, Artificial lighting, Moonlight, Practical lighting, Firelight, Fluorescent lighting, Overcast lighting, Sunny lighting. Say where the light comes from (a window, a lamp).
- Light quality: Soft lighting, Hard lighting. Angle: Top lighting, Side lighting, Underlighting, Edge lighting.
- Colour: Warm colors, Cool colors, Mixed colors.
- Shot size: Extreme wide, Wide, Medium wide, Medium, Medium close-up, Close-up, Extreme close-up shot (Medium or Wide unless asked).
- Camera angle: Over-the-shoulder, Low angle, High angle, Dutch angle, Aerial, Overhead shot. Leave the angle out when the prompt already describes a camera move.
- Composition: Center, Balanced, Right-heavy, Left-heavy, Symmetrical, Short-side composition (Center unless asked).

Rules:
- Keep the subject and the action the user gave. Fill in what they look like (appearance, expression, number, pose) and add background detail, but never add a subject the prompt doesn't have (no people in a landscape).
- Describe how the motion unfolds, step by step. With no action given, add a natural one, and some background motion (clouds drifting, leaves in the wind).
- No literary mood lines ("the scene is full of energy and life").
- A sky is a clear blue sky, so it doesn't blow out.
- Camera moves in plain words: "the camera pans up", "the camera moves from left to right", "the camera pushes forward".
- Repeat a subject's key look in every clip so it stays the same across cuts.
- LoRA trigger words stay exactly as written.

## Rocsy modes
- **Text**: the prompt alone, with the cinematic settings as above.
- **Keyframe** (image to video): the image already shows the scene. Write the motion: what the main subject does and how the camera moves, briefly. Don't re-describe what is visible in the image, and keep it to 100 words or less. Keep `@First` / `@Last` exactly as written.
- Wan has no Reference mode.

Sources: Wan-Video/Wan2.2, wan/utils/system_prompt.py (T2V_A14B_EN_SYS_PROMPT, I2V_A14B_EN_SYS_PROMPT; Apache-2.0), the official prompt extender's rewriting rules.
