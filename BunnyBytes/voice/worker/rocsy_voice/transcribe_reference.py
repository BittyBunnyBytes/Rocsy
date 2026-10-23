#!/usr/bin/env python3
"""Transcribe one short reference clip with the local Whisper turbo checkpoint."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import torch
import whisper


ROOT = Path(__file__).resolve().parent.parent.parent
MODEL_DIR = ROOT / "models" / "legacy" / "WhisperLargeV3Turbo"
CHECKPOINT = MODEL_DIR / "large-v3-turbo.pt"


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: transcribe_reference.py AUDIO")
    if not CHECKPOINT.is_file():
        raise SystemExit(f"Missing transcription model: {CHECKPOINT}")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = whisper.load_model(str(CHECKPOINT), device=device, download_root=str(MODEL_DIR))
    result = model.transcribe(
        sys.argv[1],
        task="transcribe",
        fp16=device == "cuda",
        temperature=0,
        beam_size=5,
        condition_on_previous_text=False,
        verbose=False,
    )
    print(json.dumps({"text": result.get("text", "").strip(), "language": result.get("language", ""), "device": device}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
