from __future__ import annotations

import random
import re
from dataclasses import dataclass


VOCAL_TAGS = {
    "laugh": ("laughing joyfully", "ha ha ha"),
    "laughing": ("laughing joyfully, amused, giggling", "hahaha, hehe"),
    "laughter": ("laughing heartily, laughing out loud", "hahaha, ha ha ha"),
    "laughs": ("laughing, amused", "haha, heh"),
    "giggling": ("giggling lightly", "hehehe"),
    "crying": ("crying softly, sobbing", "oh... oh no..."),
    "sobbing": ("sobbing intensely", "ohh... ugh..."),
    "sigh": ("sighing deeply", "aahhh..."),
    "sighing": ("sighing slowly", "hmm... aahhh..."),
    "gasp": ("gasping in surprise", "ah!"),
    "whimper": ("whimpering softly, fearful", "uhh... oh..."),
    "moan": ("groaning softly, sighing deeply", "mmm... ahhh..."),
    "moaning": ("moaning", "mmm... ohh..."),
    "growl": ("growling angrily", "grrr..."),
    "scream": ("screaming in terror", "AHHH!"),
    "whisper": ("whispering quietly", "shhh..."),
    "whispering": ("whispering quietly", "shhh... listen..."),
}


@dataclass
class Segment:
    kind: str = "speech"
    text: str = ""
    voice: str = ""
    emotion: str = ""
    seconds: float = 0.0


TOKEN_RE = re.compile(
    r"\[[^\]]*\]|"
    r"<([a-zA-Z]+)(?::(\d+))?>(.*?)</\1>|<([a-zA-Z]+)(?::(\d+))?\s*/?>",
    re.IGNORECASE | re.DOTALL,
)


def _attrs(body: str) -> dict[str, str]:
    result = {}
    for part in re.split(r",\s*(?=[A-Za-z_]+\s*=)", body):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        result[key.strip().upper()] = value.strip().strip('"\'')
    return result


def parse_script(
    script: str,
    default_voice: str = "",
    default_emotion: str = "",
    preserve_unknown_brackets: bool = False,
) -> list[Segment]:
    state = {"voice": default_voice, "emotion": default_emotion}
    stack: list[dict] = []
    segments: list[Segment] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "".join(buffer).strip()
        buffer.clear()
        if text:
            segments.append(Segment(text=text, voice=state["voice"], emotion=state["emotion"]))

    cursor = 0
    for match in TOKEN_RE.finditer(script):
        buffer.append(script[cursor:match.start()])
        token = match.group(0)
        cursor = match.end()
        if token.startswith("["):
            inner = token[1:-1].strip()
            upper = inner.upper()
            if upper == "NEW":
                flush()
            elif upper in ("/", "//"):
                flush()
                if upper == "//":
                    stack.clear()
                    state = {"voice": default_voice, "emotion": default_emotion}
                else:
                    state = stack.pop() if stack else {"voice": default_voice, "emotion": default_emotion}
            elif re.fullmatch(r"PAUSE(?:\s*=\s*.*)?", inner, re.IGNORECASE):
                flush()
                value = inner.split("=", 1)[1] if "=" in inner else "0.5"
                value = value.lower().rstrip("s ")
                try:
                    seconds = max(0.0, min(30.0, float(value)))
                except ValueError:
                    seconds = 0.5
                segments.append(Segment(kind="pause", seconds=seconds))
            else:
                attrs = _attrs(inner)
                if "VOICE" in attrs or "EMO" in attrs:
                    flush()
                    stack.append(dict(state))
                    if "VOICE" in attrs:
                        state["voice"] = attrs["VOICE"]
                    if "EMO" in attrs:
                        mode = attrs["EMO"].casefold()
                        if mode in ("none", "off"):
                            state["emotion"] = ""
                        elif mode == "text":
                            state["emotion"] = attrs.get("TEXT", "")
                        else:
                            state["emotion"] = attrs.get("TEXT", attrs["EMO"])
                elif preserve_unknown_brackets:
                    # Fish Audio S2 accepts natural-language performance cues in
                    # brackets. Keep anything that is not BunnyVoice control
                    # syntax verbatim so it reaches the model as authored.
                    buffer.append(token)
        else:
            flush()
            name = (match.group(1) or match.group(4) or "").casefold()
            count = int(match.group(2) or match.group(5) or 1)
            inner_text = match.group(3)
            emotion, fallback = VOCAL_TAGS.get(name, (name, name))
            for _ in range(max(1, min(8, count))):
                segments.append(Segment(
                    text=(inner_text or fallback).strip(),
                    voice=state["voice"],
                    emotion=emotion,
                ))
    buffer.append(script[cursor:])
    flush()
    return segments
