"""Faster token loop and acoustic attention for the YuE2 pack, without editing the pack.

Installed by the cover node. On ROCm a CPU read per token is expensive: the pack read every token back and copied the
repetition window to the card each step. Here sampling stays on the card and tokens are read every READBACK steps, the way
ComfyUI's own YuE2 does. Same RNG calls and arithmetic, so a seed gives the same tokens. The acoustic stage's attention
goes through flash-attn. Anything else (no card, FP8, no CUDA graph) uses the pack's original code.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import time

READBACK = 16
_installed = False


def _vendor():
    import nodes as comfy_nodes

    node = comfy_nodes.NODE_CLASS_MAPPINGS.get("YuE2VocalsOnly")
    if node is None:
        raise RuntimeError("RSP needs the YuE2-ComfyUI pack.")
    return sys.modules[node.__module__].__package__ + ".vendor.yue2"


def fast_attention(original):
    import torch
    from flash_attn import flash_attn_func

    def attention(q, k, v, *, causal=False, backend="sdpa", query_chunk_size=None):
        if query_chunk_size is not None or backend != "sdpa" or q.device.type != "cuda" or q.dtype not in (torch.bfloat16, torch.float16):
            return original(q, k, v, causal=causal, backend=backend, query_chunk_size=query_chunk_size)
        groups = q.shape[1] // k.shape[1]
        if groups > 1:
            k, v = k.repeat_interleave(groups, dim=1), v.repeat_interleave(groups, dim=1)
        return flash_attn_func(q[None], k[None], v[None], causal=causal)[0]

    return attention


def fast_generate(sampling_module, cuda_graph, original):
    import torch

    @torch.inference_mode()
    def generate_tokens(model, prefix, sampling, seed, phase, negative=None, cfg_scale=1.0,
                        legacy_off=False, cancelled=None, on_token=None, use_cuda_graph=True):
        device = next(model.parameters()).device
        if (not use_cuda_graph or device.type != "cuda" or getattr(model, "_yue2_fp8_originals", {})
                or len(prefix) + sampling.max_tokens > sampling_module.CONTEXT
                or (negative is not None and len(negative) + sampling.max_tokens > sampling_module.CONTEXT)
                or (cfg_scale != 1 and negative is None)):
            return original(model, prefix, sampling, seed, phase, negative=negative, cfg_scale=cfg_scale, legacy_off=legacy_off,
                            cancelled=cancelled, on_token=on_token, use_cuda_graph=use_cuda_graph)
        if cancelled is not None and cancelled():
            raise InterruptedError("Cancelled before prefill")
        generator = torch.Generator(device=device).manual_seed(seed)
        sampling_module.synchronize(device)
        start = time.perf_counter()
        graph = cuda_graph.GraphAR(model, [prefix] if cfg_scale == 1 else [prefix, negative], sampling.max_tokens, fuse_projections=True)
        try:
            logits = graph.prefill()
            conditional, unconditional = logits[:1], logits[1:] if cfg_scale != 1 else None
            sampling_module.synchronize(device)
            prefill_seconds = time.perf_counter() - start
            # EOD can't be sampled in either phase, so as padding it never changes the penalty.
            recent = torch.full((sampling.penalty_window,), sampling_module.EOD, dtype=torch.long, device=device)
            made = torch.empty(sampling.max_tokens, dtype=torch.long, device=device)
            end = sampling_module.ABC_END if phase == "abc" else sampling_module.MUSIC_END
            history, first, eos, read = [], None, False, 0
            for step in range(sampling.max_tokens):
                if cancelled is not None and cancelled():
                    raise InterruptedError(f"Cancelled during {phase}")
                guided = conditional if cfg_scale == 1.0 else unconditional + cfg_scale * (conditional - unconditional)
                scores = sampling_module.distribution(guided, sampling, recent, step, phase, legacy_off)
                if sampling.temperature == 0:
                    next_id = scores.argmax(-1, keepdim=True)
                else:
                    next_id = torch.multinomial(scores.softmax(-1), 1, generator=generator)
                made[step] = next_id[0, 0]
                recent[step % sampling.penalty_window] = next_id[0, 0]
                if (step + 1) % READBACK == 0 or step + 1 == sampling.max_tokens:
                    for token in made[read:step + 1].tolist():
                        if first is None:
                            first = time.perf_counter() - start
                        if on_token is not None:
                            on_token(phase, token)
                        if token == end:
                            eos = True
                            break
                        history.append(token)
                    read = step + 1
                    if eos:
                        break
                if step + 1 < sampling.max_tokens:
                    branches = graph.step(next_id)
                    conditional, unconditional = branches[:1], branches[1:] if cfg_scale != 1 else None
            sampling_module.synchronize(device)
            seconds = time.perf_counter() - start
            count = len(history) + int(eos)
            return history, {"seconds": seconds, "prefill_seconds": prefill_seconds, "ttft_seconds": first, "output_tokens": count,
                             "content_tokens": len(history), "output_tps": count / seconds, "prefix_tokens": len(prefix),
                             "cfg_branches": 1 if cfg_scale == 1 else 2, "execution": "cuda_graph",
                             "attention": graph.attention_backend}, not eos
        finally:
            graph.close()

    return generate_tokens


def install() -> None:
    """Once per process; the pack looks both functions up by name when it runs."""
    global _installed
    if _installed:
        return
    vendor = _vendor()
    sampling = importlib.import_module(vendor + ".sampling")
    cuda_graph = importlib.import_module(vendor + ".cuda_graph")
    sampling.generate_tokens = fast_generate(sampling, cuda_graph, sampling.generate_tokens)
    if importlib.util.find_spec("flash_attn"):
        nar = importlib.import_module(vendor + ".nar")
        nar.attention = fast_attention(nar.attention)
    _installed = True
    print("[RSP] faster YuE2 token loop and acoustic attention installed")
