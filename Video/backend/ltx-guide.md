# LTX 2.5: prompting guide

LTX 2.5 renders video and its audio together from one prompt. It reads a single
paragraph of natural English prose. It has no fields, no section labels and no
shot markup.

## Quick syntax
- One continuous paragraph, written in the order things happen. No headings, lists, Markdown or code fences.
- Optional visual style first: `Style: <style>, ...` (cinematic-realistic is the model's default; leave it out when unsure).
- Start straight with the scene and the action. Not "The scene opens with...", and no titles.
- Present-progressive verbs (is walking, is speaking), with temporal connectors (as, then, while) to keep the order.
- Speech goes in quotes with the voice described: `The man says in an excited voice: 'You won't believe what I just saw!'`. Name the language when it isn't English.
- Sound is part of the prompt: ambience, effects and music, placed where they happen in the action.
- No timestamps and no cuts unless the user asks for them: one continuous shot.

## Details for Hermes
Structure each prompt like a cinematographer's shot description:
1. The main action, in one sentence.
2. Specific movements and gestures.
3. Exact appearance of characters and objects (gender, clothing, hair, expression). Don't add characters the user didn't ask for.
4. Background and environment.
5. Camera angle and movement. Only add camera motion the user asked for; otherwise describe the framing.
6. Lighting and colour.
7. Any changes or sudden events.
8. The audio layer: be concrete ("soft footsteps on tile"), never vague ("ambient sound is present").

Rules:
- Keep everything the user asked for: style, visuals, motion, camera, audio. Fill vague prompts with concrete detail (lighting, textures, materials, setting).
- Keep the user's dialogue word for word (fix only typos). Don't invent dialogue unless the user mentions talking, singing or a conversation.
- Plain, restrained wording: "red dress", not "vibrant red dress"; "soft overhead light", not "blinding light"; subtle modifiers for small features ("subtle freckles").
- Only what can be seen or heard: no smell, taste or touch.
- For POV or FPV shots, don't describe the subject whose point of view it is.
- If the user's prompt is already detailed, chronological prose, don't make big changes. Add the audio description if it's missing.
- Length: about 150-250 words. A long paragraph is fine, but keep it to one shot.

## Rocsy modes
- **Text**: the prompt alone.
- **Keyframe**: the first image (and the last, when one is attached) is the picture the video starts (ends) on. Describe the image first, then the motion from it. Where the image and the text disagree, follow the image. Keep `@First` / `@Last` exactly as written.
- **Reference** (MSR): attached pictures and audio are named `<Picture N>` and `<Audio N>`. Keep every such token exactly as the user wrote it and never invent new ones; Rocsy turns them into the labels the model was trained on. Say what each one gives the video (a character's look, a place, a voice), e.g. "the woman from <Picture 1> ...".
- LoRA trigger words stay exactly as written.

Sources: the system prompts Lightricks ships with ComfyUI-LTXVideo (system_prompts/gemma_t2v_system_prompt.txt and gemma_i2v_system_prompt.txt, the LTX-2 Gemma enhancer; prompt_enhancer_utils.py) and docs.ltx.io/open-source-model/usage-guides/prompting-guide.
