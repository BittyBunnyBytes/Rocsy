"""The YuE2 pack's vocal model, run in batches at half precision: 5x faster here and inaudibly different.

Its own loop does one 8 s window at a time in float32: 52 s for a 211 s song on the XTX. The same model through
~/AI/AudioSeparator (batch 4, float16 autocast) took 9.4 s for 207 s. The pack measured float16 at 44-72 dB from float32.
"""
from __future__ import annotations

import importlib
import sys

BATCH = 4


def _pack():
    import nodes as comfy_nodes

    node = comfy_nodes.NODE_CLASS_MAPPINGS.get("YuE2VocalsOnly")
    if node is None:
        raise RuntimeError("RSP needs the YuE2-ComfyUI pack.")
    return sys.modules[node.__module__].__package__


def batched_demix(model, net, audio, cancelled=None, progress=None):
    """model.demix's windows and fades, BATCH windows per call."""
    import torch

    with torch.inference_mode():
        length = audio.shape[-1]
        padded = length > 2 * model.BORDER
        if padded:
            audio = torch.nn.functional.pad(audio[None], (model.BORDER, model.BORDER), mode="reflect")[0]
        total = audio.shape[-1]
        window = model.fades(audio.device)
        summed = torch.zeros_like(audio)
        weight = torch.zeros(total, device=audio.device)
        starts = model.chunk_starts(total)
        for first in range(0, len(starts), BATCH):
            if cancelled is not None and cancelled():
                raise InterruptedError("voice separation cancelled")
            group = starts[first:first + BATCH]
            parts = []
            for start in group:
                part = audio[:, start:start + model.CHUNK]
                size = part.shape[-1]
                if size < model.CHUNK:
                    mode = "reflect" if size > model.CHUNK // 2 + 1 else "constant"
                    part = torch.nn.functional.pad(part[None], (0, model.CHUNK - size), mode=mode)[0]
                parts.append(part)
            with torch.autocast(audio.device.type, dtype=torch.float16):
                voices = net(torch.stack(parts)).float()
            for voice, start in zip(voices, group):
                size = min(model.CHUNK, total - start)
                weights = window.clone()
                if start == 0:
                    weights[:model.FADE] = 1.0
                elif start + model.CHUNK >= total:
                    weights[-model.FADE:] = 1.0
                summed[:, start:start + size] += voice[:, :size] * weights[:size]
                weight[start:start + size] += weights[:size]
            if progress is not None:
                progress((first + len(group)) / len(starts))
        vocals = torch.nan_to_num(summed / weight)
        return vocals[:, model.BORDER:-model.BORDER] if padded else vocals


def voice_of(audio: dict, options, unique_id) -> dict:
    """The pack's own voice_of (weights, device, resampling, unload, refusals) around the batched loop."""
    package = _pack()
    model = importlib.import_module(package + ".vocals.model")
    vocals_only = importlib.import_module(package + ".vocals_only")
    staged = importlib.import_module(package + ".staged")
    progress_module = importlib.import_module(package + ".progress")
    original = model.demix
    model.demix = lambda net, sound, cancelled=None, progress=None: batched_demix(model, net, sound, cancelled, progress)
    try:
        progress = progress_module.NodeProgress(unique_id, title="RSP Vocals")
        vocals = vocals_only.voice_of(audio, staged.resolve(options), unique_id, progress)
    finally:
        model.demix = original
    progress.finish("The voice of {:.0f} seconds of audio".format(vocals["waveform"].shape[-1] / float(vocals["sample_rate"])))
    return vocals
