# Fish Audio S2 Pro — script guide

Multilingual (80+ languages) voice cloning with inline cues in [square brackets].
Cues are free-form natural language, not a fixed list.

## Quick syntax
- Sentence-level emotion goes at the start of the sentence: "[happy] What a beautiful day!"
- Tones and sound effects can go anywhere: "This is [emphasis] really important."
- Stack up to 3 cues: "[sad][whispering] I miss you so much."
- Intensity in words: [slightly sad], [very excited], [extremely angry].
- Pauses: [break] (short), [long-break] (long). Effects read best with matching text: "[laughing] Ha, ha, ha."
- Square brackets only — (parentheses) are the old S1 syntax and are not converted.

## Details for Hermes
- Basic emotions: happy, sad, angry, excited, calm, nervous, confident, surprised, satisfied, delighted, scared, worried, upset, frustrated, depressed, empathetic, embarrassed, disgusted, moved, proud, relaxed, grateful, curious, sarcastic.
- Advanced: disdainful, unhappy, anxious, hysterical, indifferent, uncertain, doubtful, confused, disappointed, regretful, guilty, ashamed, jealous, envious, hopeful, optimistic, pessimistic, nostalgic, lonely, bored, contemptuous, sympathetic, compassionate, determined, resigned.
- Tones: [in a hurry tone], [shouting], [screaming], [whispering], [soft tone], [emphasis].
- Effects: laughing, chuckling, sobbing, crying loudly, sighing, groaning, panting, gasping, yawning, snoring, clear throat; special: [audience laughing], [background laughter], [crowd laughing].
- Free-form also works: [whisper in small voice], [professional broadcast tone], [pitch up].
- Don't overuse cues in short text, don't mix conflicting emotions, and keep a cue next to the sentence it controls.
- Only add cues when the request asks for a delivery; otherwise plain text.

Sources: docs.fish.audio (Emotions / Emotion reference, Models overview); huggingface.co/fishaudio S2 Pro card.
