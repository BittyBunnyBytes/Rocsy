"""Resident Chatterbox Turbo server, run by the Rocsy Voice worker inside the ChatterboxTurbo venv.

Loads the model once and keeps each voice's encoded reference (conditionals) in memory, so a
second line from the same character skips the reference encode. Loopback only; the worker picks
the port and stops the process when the GPU is needed elsewhere.

    python chatterbox_server.py --port N --device cuda --model-dir models/legacy/ChatterboxTurbo/checkpoints
"""
from __future__ import annotations

import argparse
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import soundfile as sf
import torch
from chatterbox.tts_turbo import ChatterboxTurboTTS

LOCK = threading.Lock()
CONDS: dict[tuple[str, int, float], object] = {}
MAX_CONDS = 32


def _long_enough(path: str, folder: str) -> str:
    """Turbo refuses references of 5 s or less: loop a short clip to about 6 s."""
    import numpy as np
    audio, rate = sf.read(path, always_2d=False)
    if len(audio) / rate > 5.2:
        return path
    reps = int(np.ceil(6.0 * rate / max(1, len(audio))))
    looped = Path(folder) / "reference-looped.wav"
    sf.write(looped, np.concatenate([audio] * reps), rate)
    return str(looped)


def _conditionals(model, path: str, exaggeration: float):
    import tempfile
    stat = Path(path).stat()
    key = (path, stat.st_mtime_ns, round(exaggeration, 3))
    if key not in CONDS:
        with tempfile.TemporaryDirectory(prefix="rocsy-chatterbox-") as folder:
            model.prepare_conditionals(_long_enough(path, folder), exaggeration=exaggeration)
        if len(CONDS) >= MAX_CONDS:
            CONDS.pop(next(iter(CONDS)))
        CONDS[key] = model.conds
    return CONDS[key]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--model-dir", required=True)
    args = parser.parse_args()

    model = ChatterboxTurboTTS.from_local(args.model_dir, args.device)
    # Warm up with the bundled voice (conds.pt): the first pass picks GPU kernels, so chat
    # lines don't pay for it. MIOPEN_FIND_MODE=FAST (set by the worker) keeps new lengths cheap.
    if model.conds is not None:
        with torch.inference_mode():
            for line in ("Warming up.", "Warming up the voice model with one longer sentence so the kernels are ready."):
                model.generate(line)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, b'{"ok":true}', "application/json")
            else:
                self._send(404, b"{}", "application/json")

        def do_POST(self):
            if self.path != "/tts":
                return self._send(404, b"{}", "application/json")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                with LOCK, torch.inference_mode():
                    model.conds = _conditionals(model, body["reference"], float(body.get("exaggeration", 0.0)))
                    seed = int(body.get("seed", 0))
                    if seed > 0:
                        torch.manual_seed(seed)
                    wav = model.generate(
                        body["text"],
                        temperature=float(body.get("temperature", 0.8)),
                        top_p=float(body.get("top_p", 0.95)),
                        top_k=int(body.get("top_k", 1000)),
                        repetition_penalty=float(body.get("repetition_penalty", 1.2)),
                        min_p=float(body.get("min_p", 0.0)),
                        norm_loudness=bool(body.get("norm_loudness", True)),
                    )
                buffer = io.BytesIO()
                sf.write(buffer, wav.squeeze(0).float().cpu().numpy(), model.sr, format="WAV", subtype="PCM_16")
                self._send(200, buffer.getvalue(), "audio/wav")
            except Exception as exc:  # reported to the worker, which shows it on the job
                self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"[:2000]}).encode(), "application/json")

    print(f"Chatterbox Turbo ready on 127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
