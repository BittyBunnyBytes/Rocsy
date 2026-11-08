# SPDX-License-Identifier: GPL-3.0-only
"""Seed-VC's singing conversion, adapted from vendor/seed-vc/inference.py (GPL-3.0). How it differs from upstream:

1. Each chunk diffuses 30 s of context, prompt included. The prompt is the whole 25 s reference, as upstream: it
   carries the voice's sound, and a 10 s prompt (20 s of new audio per chunk, much faster) drifted toward the source
   singer. With match_range, the vocal moves by whole octaves to the reference's range, so it stays in the song's key.
2. Silences of 10 s or more aren't diffused. Shorter ones cost more than they save: every extra piece repeats the prompt.
3. BigVGAN gets one input shape in float16. On ROCm every new convolution shape costs a one-time setup that grows
   with its size (74 s for a 20 s chunk, 8.7 s for 2.5 s; the same 2.5 s again: 1.17 s, and 0.14 s in float16).
Runs in RSP's venv only, called by seedvc_runner.py. Module scope needs numpy alone, for the tests.
"""
from __future__ import annotations

import argparse
import time

import numpy as np

SR = 44100
HOP = 512
CONTEXT_SECONDS = 30
REFERENCE_SECONDS = 25
PROMPT_SECONDS = 25
OVERLAP_FRAMES = 16
EDGE_SAMPLES = 441
VOCODER_FRAMES = 215
VOCODER_MARGIN = 16


def voiced_regions(wave: np.ndarray, frames: int, floor_db: float = -45.0, gap_seconds: float = 10.0,
                   pad_seconds: float = 0.2, shortest_seconds: float = 0.1) -> list[tuple[int, int]]:
    """[start, end) mel frames worth converting: sound above the floor, with short rests kept inside."""
    usable = min(frames, len(wave) // HOP)
    if usable <= 0:
        return []
    level = np.sqrt(np.mean(wave[:usable * HOP].reshape(usable, HOP).astype(np.float64) ** 2, axis=1))
    peak = float(level.max())
    if peak <= 0.0:
        return []
    active = np.flatnonzero(level > peak * 10.0 ** (floor_db / 20.0))
    frames_per_second = SR / HOP
    runs: list[list[int]] = []
    for index in active.tolist():
        if runs and index - runs[-1][1] <= gap_seconds * frames_per_second:
            runs[-1][1] = index + 1
        else:
            runs.append([index, index + 1])
    pad = int(pad_seconds * frames_per_second)
    shortest = shortest_seconds * frames_per_second
    regions: list[tuple[int, int]] = []
    for start, end in runs:
        if end - start < shortest:
            continue
        start, end = max(0, start - pad), min(frames, end + pad)
        if regions and start <= regions[-1][1]:
            regions[-1] = (regions[-1][0], end)
        else:
            regions.append((start, end))
    return regions


def chunks(start: int, end: int, window: int, overlap: int = OVERLAP_FRAMES) -> list[tuple[int, int]]:
    """Frame ranges covering a region, each overlapping the one before by ``overlap``."""
    out, at = [], start
    while True:
        stop = min(at + window, end)
        out.append((at, stop))
        if stop >= end:
            return out
        at = stop - overlap


def place(out: np.ndarray, wave: np.ndarray, at: int, fade: int = 0) -> None:
    """Writes wave at ``at``, its first ``fade`` samples crossfaded over what's there."""
    size = min(len(wave), len(out) - at)
    if size <= 0:
        return
    wave = wave[:size].copy()
    fade = min(fade, size)
    if fade:
        rise = np.sin(np.linspace(0.0, np.pi / 2, fade)) ** 2
        wave[:fade] = wave[:fade] * rise + out[at:at + fade] * (1.0 - rise)
    out[at:at + size] = wave


def loudest_stretch(wave: np.ndarray, seconds: int) -> np.ndarray:
    """The loudest ``seconds`` of the reference, on a 1 s grid."""
    size = seconds * SR
    if len(wave) <= size:
        return wave
    energy = np.concatenate(([0.0], np.cumsum(wave.astype(np.float64) ** 2)))
    starts = np.arange(0, len(wave) - size + 1, SR)
    return wave[int(starts[np.argmax(energy[starts + size] - energy[starts])]):][:size]


def octave_semitones(source_f0: np.ndarray, reference_f0: np.ndarray) -> int:
    """Whole octaves (as semitones) that bring the source's median sung pitch nearest the reference's."""
    sung, ref = source_f0[source_f0 > 1], reference_f0[reference_f0 > 1]
    if not sung.size or not ref.size:
        return 0
    gap = 12.0 * np.log2(float(np.median(ref)) / float(np.median(sung)))
    return 12 * int(np.round(gap / 12.0))


def vocode(vocoder_fn, mel):
    """Mel [1, bins, frames] to samples, in pieces of one fixed shape at float16 (see the top of this file)."""
    import torch

    frames = mel.size(2)
    output = np.zeros(frames * HOP, dtype=np.float32)
    step = VOCODER_FRAMES - 3 * VOCODER_MARGIN
    for start in range(0, max(1, frames - 3 * VOCODER_MARGIN), step):
        piece = mel[:, :, start:start + VOCODER_FRAMES]
        real = piece.size(2)
        if real < VOCODER_FRAMES:
            piece = torch.nn.functional.pad(piece, (0, VOCODER_FRAMES - real), mode="replicate")
        with torch.autocast(mel.device.type, dtype=torch.float16):
            wave = vocoder_fn(piece.float()).float()
        if not torch.isfinite(wave).all():
            print("[RSP] float16 vocoder overflowed on a piece, repeating it in float32", flush=True)
            wave = vocoder_fn(piece.float()).float()
        wave = wave.squeeze().cpu().numpy()
        first = 0 if start == 0 else VOCODER_MARGIN
        last = real if start + VOCODER_FRAMES >= frames else VOCODER_FRAMES - VOCODER_MARGIN
        place(output, wave[first * HOP:last * HOP], (start + first) * HOP, 0 if start == 0 else VOCODER_MARGIN * HOP)
    return output


def _words(semantic_fn, waves_16k):
    """Whisper features in upstream's 30 s windows with 5 s overlap."""
    import torch

    if waves_16k.size(-1) <= 16000 * 30:
        return semantic_fn(waves_16k)
    parts, buffer, done, overlap = [], None, 0, 5
    while done < waves_16k.size(-1):
        if buffer is None:
            chunk = waves_16k[:, done:done + 16000 * 30]
        else:
            chunk = torch.cat([buffer, waves_16k[:, done:done + 16000 * (30 - overlap)]], dim=-1)
        words = semantic_fn(chunk)
        parts.append(words if done == 0 else words[:, 50 * overlap:])
        buffer = chunk[:, -16000 * overlap:]
        done += 30 * 16000 if done == 0 else chunk.size(-1) - 16000 * overlap
    return torch.cat(parts, dim=1)


def convert(source_path: str, target_path: str, steps: int, cfg_rate: float, semitones: int, auto_f0: bool,
            match_range: bool = False) -> tuple[np.ndarray, int]:
    import librosa
    import torch
    import torchaudio
    import inference

    began = time.monotonic()
    model, semantic_fn, f0_fn, vocoder_fn, campplus, mel_fn, _ = inference.load_models(argparse.Namespace(
        f0_condition=True, checkpoint=None, config=None, fp16=True))
    device = inference.device
    loaded = time.monotonic()

    with torch.no_grad():
        source_np = librosa.load(source_path, sr=SR)[0]
        reference_np = librosa.load(target_path, sr=SR)[0][:SR * REFERENCE_SECONDS]
        prompt_np = loudest_stretch(reference_np, PROMPT_SECONDS)
        source = torch.from_numpy(source_np).unsqueeze(0).float().to(device)
        reference = torch.from_numpy(reference_np).unsqueeze(0).float().to(device)
        prompt = torch.from_numpy(np.array(prompt_np)).unsqueeze(0).float().to(device)
        to_16k = lambda wave: torchaudio.functional.resample(wave, SR, 16000)
        source_16k, prompt_16k = to_16k(source), to_16k(prompt)

        words_source = _words(semantic_fn, source_16k)
        words_prompt = semantic_fn(prompt_16k)
        mel = mel_fn(source)
        mel_prompt = mel_fn(prompt)
        fbank = torchaudio.compliance.kaldi.fbank(to_16k(reference), num_mel_bins=80, dither=0, sample_frequency=16000)
        style = campplus((fbank - fbank.mean(dim=0, keepdim=True)).unsqueeze(0))

        f0_prompt = torch.from_numpy(f0_fn(prompt_16k[0], thred=0.03)).to(device)[None]
        f0_source = torch.from_numpy(f0_fn(source_16k[0], thred=0.03)).to(device)[None]
        sung = f0_source > 1
        shifted = f0_source.clone()
        if auto_f0 and sung.any() and (f0_prompt > 1).any():
            log_source, log_prompt = torch.log(f0_source[sung] + 1e-5), torch.log(f0_prompt[f0_prompt > 1] + 1e-5)
            shifted[sung] = torch.exp(log_source - torch.median(log_source) + torch.median(log_prompt))
        if match_range and not auto_f0 and not semitones:
            semitones = octave_semitones(f0_source.cpu().numpy(), f0_prompt.cpu().numpy())
            print(f"[RSP] range match: {semitones:+d} semitones", flush=True)
        if semitones:
            shifted[sung] = inference.adjust_f0_semitones(shifted[sung], semitones)

        lengths = torch.LongTensor([mel.size(2)]).to(device)
        cond = model.length_regulator(words_source, ylens=lengths, n_quantizers=3, f0=shifted)[0]
        prompt_cond = model.length_regulator(words_prompt, ylens=torch.LongTensor([mel_prompt.size(2)]).to(device),
                                             n_quantizers=3, f0=f0_prompt)[0]
        features = time.monotonic()

        window = SR // HOP * CONTEXT_SECONDS - mel_prompt.size(2)
        regions = voiced_regions(source_np, cond.size(1))
        output = np.zeros(len(source_np), dtype=np.float32)
        done, diffusing, vocoding = 0, 0.0, 0.0
        for start, end in regions:
            region_chunks = chunks(start, end, window)
            for number, (first, last) in enumerate(region_chunks):
                chunk_began = time.monotonic()
                joined = torch.cat([prompt_cond, cond[:, first:last]], dim=1)
                with torch.autocast(device_type=device.type, dtype=torch.float16):
                    made = model.cfm.inference(joined, torch.LongTensor([joined.size(1)]).to(device), mel_prompt, style,
                                               None, steps, inference_cfg_rate=cfg_rate)[:, :, mel_prompt.size(-1):]
                torch.cuda.synchronize()    # GPU work is queued: without this the time lands on whatever reads it next
                diffused = time.monotonic()
                wave = vocode(vocoder_fn, made)
                diffusing += diffused - chunk_began
                vocoding += time.monotonic() - diffused
                if number == 0:
                    edge = min(EDGE_SAMPLES, len(wave))
                    wave[:edge] *= np.linspace(0.0, 1.0, edge, dtype=np.float32)
                if number == len(region_chunks) - 1:
                    edge = min(EDGE_SAMPLES, len(wave))
                    wave[-edge:] *= np.linspace(1.0, 0.0, edge, dtype=np.float32)
                place(output, wave, first * HOP, 0 if number == 0 else OVERLAP_FRAMES * HOP)
                done += 1
        voiced = sum(end - start for start, end in regions) * HOP / max(1, len(source_np))
    print(f"[RSP] Seed-VC {len(source_np) / SR:.1f} s: load {loaded - began:.1f} s, features {features - loaded:.1f} s, "
          f"diffusion {diffusing:.1f} s, vocoder {vocoding:.1f} s, {done} chunks of {window * HOP / SR:.0f} s, {voiced:.0%} voiced, {steps} steps", flush=True)
    return output, SR
