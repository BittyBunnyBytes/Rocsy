"""Runs in RSP's own venv, with vendor/seed-vc as the working directory. Never imported by ComfyUI.

GPU only: Seed-VC quietly falls back to CPU, so a missing GPU is an error here.
"""
import argparse
import os
import sys

SEED_VC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor", "seed-vc")
HUB_FILES = (
    ("Plachta/Seed-VC", "DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth"),
    ("Plachta/Seed-VC", "config_dit_mel_seed_uvit_whisper_base_f0_44k.yml"),
    ("lj1995/VoiceConversionWebUI", "rmvpe.pt"),
    ("funasr/campplus", "campplus_cn_common.bin"),
)
SNAPSHOTS = ("nvidia/bigvgan_v2_44khz_128band_512x", "openai/whisper-small")


def prefetch() -> None:
    """Downloads only; no torch, no GPU. Same cache folders Seed-VC reads."""
    from huggingface_hub import hf_hub_download, snapshot_download

    for repo, name in HUB_FILES:
        print(hf_hub_download(repo_id=repo, filename=name, cache_dir="./checkpoints"), flush=True)
    for repo in SNAPSHOTS:
        print(snapshot_download(repo_id=repo, cache_dir="./checkpoints/hf_cache",
                                allow_patterns=["*.json", "*.txt", "*.bin", "*.pt", "*.safetensors", "*.model"],
                                ignore_patterns=["bigvgan_discriminator_optimizer.pt", "*.h5", "*.msgpack"]), flush=True)


def convert(args: argparse.Namespace) -> None:
    import numpy as np
    import torch

    if not torch.cuda.is_available():
        raise SystemExit("RSP: no GPU visible to Seed-VC, and it does not run on CPU here.")

    sys.path.insert(0, SEED_VC)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import seedvc_fast

    wave, rate = seedvc_fast.convert(args.source, args.target, args.steps, args.cfg_rate, args.semitones, args.auto_f0,
                                     args.match_range)
    with open(args.output, "wb") as handle:     # np.save(path) would add a second .npy to some names
        np.save(handle, wave.astype(np.float32))
    with open(args.output + ".rate", "w") as handle:
        handle.write(str(rate))


if __name__ == "__main__":
    os.chdir(SEED_VC)
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefetch", action="store_true")
    parser.add_argument("--source")
    parser.add_argument("--target")
    parser.add_argument("--output")
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--cfg-rate", type=float, default=0.7)
    parser.add_argument("--semitones", type=int, default=0)
    parser.add_argument("--auto-f0", action="store_true")
    parser.add_argument("--match-range", action="store_true")
    parsed = parser.parse_args()
    prefetch() if parsed.prefetch else convert(parsed)
