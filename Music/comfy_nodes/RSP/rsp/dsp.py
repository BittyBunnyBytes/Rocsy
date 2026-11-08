"""Audio arithmetic for covers. ComfyUI AUDIO = {"waveform": [batch, channels, samples], "sample_rate": int}."""
from __future__ import annotations

import math

import torch
import torchaudio


class NoSinging(ValueError):
    pass


def _fit(wave: torch.Tensor, samples: int) -> torch.Tensor:
    """Cut or pad with silence so wave has exactly this many samples."""
    if wave.shape[-1] >= samples:
        return wave[..., :samples]
    return torch.nn.functional.pad(wave, (0, samples - wave.shape[-1]))


def _channels(wave: torch.Tensor, channels: int) -> torch.Tensor:
    if wave.shape[1] == channels:
        return wave
    return wave.mean(dim=1, keepdim=True).expand(-1, channels, -1)


def match(audio: dict, like: dict) -> torch.Tensor:
    """audio's waveform in like's sample rate, channels and length."""
    wave = audio["waveform"].float()
    if int(audio["sample_rate"]) != int(like["sample_rate"]):
        wave = torchaudio.functional.resample(wave, int(audio["sample_rate"]), int(like["sample_rate"]))
    wave = _channels(wave, like["waveform"].shape[1])
    return _fit(wave, like["waveform"].shape[-1])


def band_of(mix: dict, vocals: dict) -> dict:
    """The band = the mix minus its separated voice, so nothing the separator left is lost."""
    return {"waveform": mix["waveform"].float() - match(vocals, mix), "sample_rate": mix["sample_rate"]}


def clean_voice(original: dict, vocals: dict, floor_db: float = -25.0) -> dict:
    """The original voice recording when the separator took out next to nothing (it was already a clean vocal),
    so its own sound isn't smeared by separation; otherwise the separated vocals."""
    sample = original["waveform"].float()
    removed = _rms(sample - match(vocals, original)) / _rms(sample).clamp_min(1e-8)
    return original if float(removed) < 10.0 ** (floor_db / 20.0) else vocals


def _rms(wave: torch.Tensor) -> torch.Tensor:
    return wave.pow(2).mean().sqrt()


def _active_rms(wave: torch.Tensor, sample_rate: int) -> float:
    """Loudness of the sung parts: silences between phrases don't count."""
    mono = wave.mean(dim=(0, 1))
    frame = max(1, int(sample_rate * 0.05))
    if mono.numel() < frame:
        return float(_rms(mono))
    frames = mono[: mono.numel() // frame * frame].view(-1, frame).pow(2).mean(dim=1).sqrt()
    peak = float(torch.quantile(frames, 0.95))
    active = frames[frames > peak * 0.1]
    return float(active.pow(2).mean().sqrt()) if active.numel() else 0.0


def mix_cover(band: dict, converted: dict, original_vocals: dict, voice_strength: float = 100.0, gain_db: float = 0.0) -> dict:
    """Band + vocal, the converted voice at the level YuE2 sang at. Lowered as a whole if it would clip."""
    rate = int(band["sample_rate"])
    original = match(original_vocals, band)
    voice = match(converted, band)
    level = _active_rms(voice, rate)
    if level > 1e-6:
        voice = voice * (_active_rms(original, rate) / level)
    share = max(0.0, min(100.0, float(voice_strength))) / 100.0
    vocal = voice * share + original * (1.0 - share)
    out = band["waveform"].float() + vocal * 10.0 ** (float(gain_db) / 20.0)
    peak = float(out.abs().max())
    if peak > 0.99:
        out = out * (0.99 / peak)
    return {"waveform": out, "sample_rate": rate}


def pick_reference(vocals: dict, seconds: float = 25.0, min_singing: float = 3.0) -> dict:
    """The span with the most singing, loudest on a tie. Seed-VC reads at most 25 s of reference."""
    wave, rate = vocals["waveform"].float(), int(vocals["sample_rate"])
    mono = wave[0].mean(dim=0)
    second = rate
    count = mono.numel() // second
    if count < 1:
        raise NoSinging("The voice reference is under a second long.")
    energy = mono[: count * second].view(count, second).pow(2).mean(dim=1).sqrt()
    active = energy > max(float(torch.quantile(energy, 0.95)) * 0.25, 1e-4)
    if float(active.sum()) < min_singing:
        raise NoSinging(f"Heard under {min_singing:g} s of singing in the voice reference. "
                        "Pick another voice source, or turn the voice off for an instrumental.")
    span = min(count, max(1, int(seconds)))
    best = (-1.0, -1.0, 0)
    for start in range(count - span + 1):
        sung = float(active[start:start + span].sum())
        loud = float(energy[start:start + span].sum())
        best = max(best, (sung, loud, -start))
    start = -best[2]
    return {"waveform": wave[:1, :, start * second:(start + span) * second], "sample_rate": rate}


def sound_facts(audio: dict) -> dict:
    """Measurements for someone who can't hear the recording."""
    wave, rate = audio["waveform"].float(), int(audio["sample_rate"])
    mono = wave[0].mean(dim=0)
    seconds = mono.numel() / rate
    level = max(_active_rms(wave[:1], rate), 1e-6)
    # Brightness: the spectrum's centre of mass, on evenly spread 4096-sample windows.
    size = 4096
    hop = max(size, mono.numel() // 200)
    frames = mono.unfold(0, size, hop) if mono.numel() >= size else mono[None]
    power = torch.fft.rfft(frames * torch.hann_window(frames.shape[-1])).abs().pow(2).mean(dim=0)
    freqs = torch.fft.rfftfreq(frames.shape[-1], 1 / rate)
    centre = float((power * freqs).sum() / power.sum().clamp_min(1e-12))
    return {"seconds": round(seconds, 1), "loudness_db": round(20.0 * math.log10(level), 1), "brightness_hz": round(centre)}
