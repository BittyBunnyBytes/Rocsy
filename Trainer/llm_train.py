#!/usr/bin/env python3
"""LoRA training for chat/text LLMs (PEFT on ROCm PyTorch), run by the Trainer in the LLM trainer's venv.

Reads the per-job train set (.jsonl/.json, see llm_data.py), trains a LoRA on a Hugging Face base model
(bf16, or 4-/8-bit QLoRA through bitsandbytes when the weights would not fit), saves a checkpoint per
epoch and the final PEFT adapter, then converts it to a GGUF LoRA with llama.cpp's convert_lora_to_gguf.py
so llama-server can load it with --lora on top of the same model's GGUF.

Log lines follow the formats progress.py already parses (a "steps:" tqdm-style bar, "epoch k/E done — avr_loss L",
"saved ....safetensors").
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_data  # noqa: E402

TOKENIZER_FILES = ["*.json", "*.model", "*.txt", "*.jinja", "*.tiktoken", "tokenizer*"]
# Attention + MLP projections of Llama/Qwen/Gemma/Mistral-style decoders; vision/audio towers are left alone.
TARGET_RE = (r"^(?!.*(vision|visual|audio|image|mm_proj|multi_modal|embed_vision|embed_audio)).*"
             r"\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)$")
EXCLUDE_RE = r".*(vision|visual|audio|image|mm_proj|multi_modal|embed_vision|embed_audio).*"


def log(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    log(f"[llm] ERROR: {msg}")
    sys.exit(2)


def clock(sec: float) -> str:
    sec = int(max(0, sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def resolve_base(base: str, tokenizer_only: bool) -> Path:
    local = Path(base)
    if local.is_dir():
        if not (local / "config.json").is_file():
            fail(f"{base} has no config.json: pick a Hugging Face model folder (config.json + *.safetensors).")
        return local
    if not re.fullmatch(r"[A-Za-z0-9][\w.-]*/[\w.-]+", base):
        fail(f"{base!r} is neither a model folder nor a Hugging Face repo id like Qwen/Qwen3-4B.")
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import GatedRepoError, RepositoryNotFoundError
    log(f"[llm] downloading {base} from Hugging Face{' (tokenizer only)' if tokenizer_only else ''}; "
        "files already in the cache are reused")
    try:
        path = snapshot_download(base, allow_patterns=TOKENIZER_FILES if tokenizer_only else TOKENIZER_FILES + ["*.safetensors", "*.py"])
    except GatedRepoError:
        fail(f"{base} is gated: accept its licence on huggingface.co and set HF_TOKEN for the Trainer, or pick an ungated copy.")
    except RepositoryNotFoundError:
        fail(f"{base} was not found on Hugging Face.")
    return Path(path)


def weights_gb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.glob("*.safetensors")) / 1024**3


def render_chat(tok):
    if tok.chat_template:
        return lambda msgs, gen=False, tools=None: tok.apply_chat_template(msgs, tools=tools, tokenize=False, add_generation_prompt=gen)
    eos = tok.eos_token or ""

    def simple(msgs, gen=False, tools=None):  # no chat template (a base model): a plain, consistent layout
        def body(m):
            think = f"<think>\n{m['reasoning_content']}\n</think>\n" if m.get("reasoning_content") else ""
            calls = "".join(f"\n{json.dumps(c, ensure_ascii=False)}" for c in m.get("tool_calls", []))
            return think + m["content"] + calls
        text = "".join(f"### {m['role'].title()}\n{body(m)}{eos if m['role'] == 'assistant' else ''}\n\n" for m in msgs)
        return text + ("### Assistant\n" if gen else "")
    return simple


def end_of_turn(render) -> str:
    """What the template writes after an assistant reply ("<|im_end|>\\n", "<end_of_turn>\\n"…): the common
    tail of two one-turn chats whose replies differ in their last letter. '' if there is none or it fails."""
    cache = getattr(render, "_eot", None)
    if cache is None:
        try:
            a, b = (render([{"role": "user", "content": "Hi"}, {"role": "assistant", "content": reply}]) for reply in ("Alpha", "Bravo"))
            cache = os.path.commonprefix([a[::-1], b[::-1]])[::-1]
        except Exception:
            cache = ""
        try:
            render._eot = cache
        except AttributeError:
            pass
    return cache


def reply_spans(render, msgs, full: str) -> list[tuple[int, int]] | None:
    """Character spans of the assistant replies in the rendered chat, each with its end-of-turn marker."""
    spans = []
    for i, m in enumerate(msgs):
        if m["role"] != "assistant":
            continue
        upto = render(msgs[:i + 1])
        if not full.startswith(upto):
            break
        # The generation prompt may add bits the history never shows (an empty thinking block): the reply
        # starts where the prompt and the rendered history part ways.
        spans.append((len(os.path.commonprefix([render(msgs[:i], True), upto])), len(upto)))
    else:
        return spans
    # Templates that render a turn differently once later turns follow (Qwen3 only gives the LAST reply an
    # empty <think> block, drops old thinking…): each reply starts where the history-so-far plus generation
    # prompt stops matching, and runs to the template's own end-of-turn marker. Tool-call-only replies
    # (no text to search for) are found this way too.
    eot = end_of_turn(render)
    if eot:
        spans, cursor = [], 0
        for i, m in enumerate(msgs):
            if m["role"] != "assistant":
                continue
            start = max(cursor, len(os.path.commonprefix([render(msgs[:i], True), full])))
            end = full.find(eot, start)
            if end < 0:
                break
            cursor = end + len(eot)
            spans.append((start, cursor))
        else:
            return spans
    # No recognisable end marker: find each reply's text in order.
    spans, cursor = [], 0
    for m in msgs:
        text = m["content"].strip()
        if m["role"] != "assistant" or not text:
            continue
        pos = full.find(text, cursor)
        if pos < 0:
            return None
        end = pos + len(text)
        newline = full.find("\n", end, end + 24)
        end = newline + 1 if newline >= 0 else end
        spans.append((pos, end))
        cursor = end
    return spans


def build_examples(tok, files: list[Path], max_len: int) -> tuple[list[tuple[list[int], list[int]]], dict[str, int]]:
    """(input_ids, labels) pairs. Chats learn only the assistant replies (the chat template's own
    end-of-turn included, so the LoRA learns to stop); plain text learns everything, in max_len windows."""
    stats = {"chats": 0, "texts": 0, "truncated": 0, "whole_sequence": 0, "dropped": 0, "tokens": 0}
    base_render = render_chat(tok)
    if not tok.chat_template:
        log("[llm] the base model has no chat template: chats use a plain '### User / ### Assistant' layout")
    template = tok.chat_template if isinstance(tok.chat_template, str) else ""
    warned: set[str] = set()
    eos = tok.eos_token_id
    out: list[tuple[list[int], list[int]]] = []
    for path in files:
        for rec in llm_data.read(path):
            # Templates silently drop fields they don't know: say so once instead of training without them.
            msgs_ = rec.get("messages", [])
            if template and "reasoning_content" not in template and "reasoning" not in warned and any(m.get("reasoning_content") for m in msgs_):
                warned.add("reasoning")
                log("[llm] note: this model's chat template ignores 'reasoning_content', so thinking in the data is NOT trained; "
                    "put it inside the reply as <think>…</think> if the model uses that format")
            if template and "tools" not in template and "tools" not in warned and (rec.get("tools") or any(m.get("tool_calls") for m in msgs_)):
                warned.add("tools")
                log("[llm] note: this model's chat template has no tool support; tool definitions/calls in the data may be dropped")
            render = (lambda m, gen=False, _t=rec.get("tools"): base_render(m, gen, _t))
            render._eot = end_of_turn(base_render)      # computed once (cached on base_render)
            if "text" in rec:
                ids = tok(rec["text"], add_special_tokens=True)["input_ids"] + ([eos] if eos is not None else [])
                for i in range(0, len(ids), max_len):
                    chunk = ids[i:i + max_len]
                    if len(chunk) > 8:
                        out.append((chunk, list(chunk)))
                stats["texts"] += 1
                continue
            msgs = rec["messages"]
            try:
                full = render(msgs)
            except Exception as exc:  # e.g. a template that refuses a system role or two user turns in a row
                stats["dropped"] += 1
                if stats["dropped"] <= 3:
                    log(f"[llm] skipped a chat the template refused: {str(exc)[:160]}")
                continue
            enc = tok(full, add_special_tokens=False, return_offsets_mapping=True)
            ids, offsets = enc["input_ids"], enc["offset_mapping"]
            labels = [-100] * len(ids)
            spans = reply_spans(render, msgs, full)
            if spans is not None:
                for t, (a, b) in enumerate(offsets):
                    if any(s <= a < e for s, e in spans):
                        labels[t] = ids[t]
            else:
                labels = list(ids)
                stats["whole_sequence"] += 1
            if len(ids) > max_len:
                ids, labels = ids[:max_len], labels[:max_len]
                stats["truncated"] += 1
            if all(x == -100 for x in labels):
                stats["dropped"] += 1
                continue
            out.append((ids, labels))
            stats["chats"] += 1
    stats["tokens"] = sum(len(i) for i, _ in out)
    return out, stats


def load_model(path: Path, quant: str):
    import torch
    from transformers import AutoConfig, AutoModelForCausalLM, BitsAndBytesConfig
    kwargs = {"dtype": torch.bfloat16, "device_map": {"": 0}, "attn_implementation": "sdpa", "low_cpu_mem_usage": True}
    if quant == "4bit":
        kwargs["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                                           bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
    elif quant == "8bit":
        kwargs["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True)
    config = AutoConfig.from_pretrained(path)
    if multimodal(config):
        # Natively multimodal checkpoints (Qwen3.5, Gemma 4…): load them whole so the LoRA's tensor names match the
        # checkpoint (and llama.cpp's converter); only the text layers get the LoRA.
        from transformers import AutoModelForImageTextToText
        log(f"[llm] {config.architectures} is multimodal: loading it whole, training only the text layers")
        return AutoModelForImageTextToText.from_pretrained(path, **kwargs)
    return AutoModelForCausalLM.from_pretrained(path, **kwargs)


def multimodal(config) -> bool:
    return hasattr(config, "text_config") and (hasattr(config, "vision_config") or hasattr(config, "audio_config"))


def add_lora(model, rank: int, alpha: int):
    from peft import LoraConfig, get_peft_model
    # No prepare_model_for_kbit_training: it upcasts every non-quantised weight (embeddings, lm_head, the vision
    # tower) to fp32, several GB on 250k-token vocabularies. bf16 + gradient checkpointing is enough for LoRA.
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model.config.use_cache = False
    common = {"r": rank, "lora_alpha": alpha, "lora_dropout": 0.05, "bias": "none", "task_type": "CAUSAL_LM"}
    try:
        return get_peft_model(model, LoraConfig(target_modules=TARGET_RE, **common))
    except ValueError:  # an architecture without q_proj/…/down_proj names: every linear layer instead
        log("[llm] no q/k/v/o/gate/up/down projections found: targeting all linear layers of the text model")
        return get_peft_model(model, LoraConfig(target_modules="all-linear", exclude_modules=EXCLUDE_RE, **common))


def save_lora(model, target: Path) -> None:
    from peft import get_peft_model_state_dict
    from safetensors.torch import save_file
    state = {k: v.detach().to("cpu").contiguous() for k, v in get_peft_model_state_dict(model).items()}
    save_file(state, str(target), metadata={"format": "pt"})
    log(f"saved {target}")


def convert_gguf(peft_dir: Path, base: Path, target: Path, llama_cpp: Path, outtype: str) -> bool:
    script = llama_cpp / "convert_lora_to_gguf.py"
    if not script.is_file():
        log(f"[llm] llama.cpp's convert_lora_to_gguf.py was not found in {llama_cpp}: skipped the GGUF copy")
        return False
    log(f"[llm] converting to a GGUF LoRA for llama.cpp ({outtype})")
    # The converter only needs the base config; given the whole folder it opens every weight shard too.
    config_only = target.parent / ".gguf-base"
    config_only.mkdir(exist_ok=True)
    shutil.copy(base / "config.json", config_only / "config.json")
    proc = subprocess.run([sys.executable, str(script), str(peft_dir), "--base", str(config_only), "--outfile", str(target),
                           "--outtype", outtype], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    shutil.rmtree(config_only, ignore_errors=True)
    tail = "\n".join(proc.stdout.strip().splitlines()[-15:])
    if proc.returncode or not target.is_file():
        log(f"{tail}\n[llm] GGUF conversion failed (exit {proc.returncode}); the PEFT adapter is still saved and usable "
            "with transformers/vLLM. llama.cpp may not support this architecture's LoRA yet.")
        return False
    log(f"saved GGUF LoRA: {target} ({target.stat().st_size / 1048576:.1f} MB)")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True, help="Hugging Face repo id or local model folder")
    ap.add_argument("--data", required=True, type=Path, help="folder of .jsonl/.json files")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--name", required=True)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=0, help="LoRA alpha (0 = 2 x rank)")
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--max_length", type=int, default=2048)
    ap.add_argument("--quant", choices=["auto", "4bit", "8bit", "none"], default="auto")
    ap.add_argument("--vram_gb", type=float, default=24.0, help="GPU memory the auto quant choice plans for")
    ap.add_argument("--grad_accum", type=int, default=0, help="examples per optimizer step (0 = auto)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--llama_cpp", type=Path, default=None, help="llama.cpp checkout with convert_lora_to_gguf.py")
    ap.add_argument("--gguf_type", choices=["f16", "bf16", "q8_0", "f32"], default="f16")
    ap.add_argument("--dry_run", action="store_true", help="read the data and tokenizer only (no GPU)")
    a = ap.parse_args()

    files = sorted((p for p in a.data.iterdir() if llm_data.is_text_file(p)), key=lambda p: p.name.casefold())
    if not files:
        fail("the train set has no .jsonl/.json files")
    base = resolve_base(a.base, tokenizer_only=a.dry_run)
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(base)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    examples, st = build_examples(tok, files, a.max_length)
    log(f"[llm] {len(examples)} training examples from {len(files)} file(s): {st['chats']} chat(s), {st['texts']} text(s), "
        f"{st['tokens']:,} tokens" + (f"; {st['truncated']} cut to {a.max_length} tokens" if st["truncated"] else "")
        + (f"; {st['dropped']} skipped" if st["dropped"] else ""))
    if st["whole_sequence"]:
        log(f"[llm] note: {st['whole_sequence']} chat(s) train on the whole conversation (the chat template rewrites earlier turns, "
            "so replies could not be isolated)")
    if not examples:
        fail("no usable examples after tokenizing")
    if a.dry_run:
        log("[llm] dry run OK")
        for n, (ids, labels) in enumerate(examples[:3], 1):
            learned = tok.decode([t for t, l in zip(ids, labels) if l != -100])[:400].replace("\n", "⏎")
            log(f"[llm] example {n}: {len(ids)} tokens, {sum(x != -100 for x in labels)} learned: {learned}")
        return

    gb = weights_gb(base)
    quant = a.quant
    if quant == "auto":
        # bf16 weights + LoRA + activations must leave room for the desktop on the same card.
        quant = "none" if gb <= a.vram_gb * 0.45 else "4bit"
        log(f"[llm] base weights are {gb:.1f} GB: training {'in bf16' if quant == 'none' else 'with a 4-bit (QLoRA) base'}")
    import torch
    if not (torch.cuda.is_available() and torch.version.hip):
        fail("no ROCm GPU is visible to PyTorch; CPU training is disabled")
    if os.getenv("LLM_SDPA", "math") == "math":
        # The fused SDPA kernels of some ROCm wheels fail on RDNA3; the math path always works.
        torch.backends.cuda.enable_flash_sdp(False)
        torch.backends.cuda.enable_mem_efficient_sdp(False)
    random.seed(a.seed)
    torch.manual_seed(a.seed)
    log(f"[llm] loading {a.base} ({gb:.1f} GB) on {torch.cuda.get_device_name(0)}")
    model = load_model(base, quant)
    model = add_lora(model, a.rank, a.alpha or a.rank * 2)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    log(f"[llm] LoRA rank {a.rank}, alpha {a.alpha or a.rank * 2}: {trainable / 1e6:.1f} M trainable parameters")

    accum = a.grad_accum or max(1, min(8, len(examples) // 8))
    per_epoch = math.ceil(len(examples) / accum)
    total = per_epoch * a.epochs
    warmup = max(1, round(total * 0.05))
    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=a.lr, weight_decay=0.0, betas=(0.9, 0.999))
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: (s + 1) / warmup if s < warmup else
                                              0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * (s - warmup) / max(1, total - warmup))))
    log(f"training: {len(examples)} examples, {a.epochs} epochs, {accum} per step, {total} steps, lr {a.lr:g}, cosine with {warmup} warmup")
    a.out.mkdir(parents=True, exist_ok=True)
    every = max(1, total // 2000)
    model.train()
    step, start, bad_run = 0, time.time(), 0
    for epoch in range(1, a.epochs + 1):
        order = list(range(len(examples)))
        random.shuffle(order)
        ep_loss, ep_n = 0.0, 0
        for chunk_start in range(0, len(order), accum):
            chunk = order[chunk_start:chunk_start + accum]
            step_loss, ok = 0.0, True
            for idx in chunk:
                ids, labels = examples[idx]
                x = torch.tensor([ids], device="cuda")
                y = torch.tensor([labels], device="cuda")
                loss = model(input_ids=x, labels=y).loss
                if not torch.isfinite(loss):
                    ok = False
                    break
                (loss / len(chunk)).backward()
                step_loss += loss.item() / len(chunk)
            if ok:
                torch.nn.utils.clip_grad_norm_(params, 1.0)
                opt.step()
                bad_run = 0
                ep_loss += step_loss
                ep_n += 1
            else:
                bad_run += 1
                log(f"[llm] step {step + 1}: non-finite loss, skipped")
                if bad_run >= 10:
                    fail("10 steps in a row had a NaN/Inf loss: training diverged. Try a lower learning rate.")
            opt.zero_grad(set_to_none=True)
            sched.step()
            step += 1
            if step % every == 0 or step == total:
                el = time.time() - start
                rate = el / step
                filled = int(20 * step / total)
                avg = ep_loss / ep_n if ep_n else 0.0
                log(f"steps: {int(100 * step / total):3d}%|{'#' * filled}{' ' * (20 - filled)}| {step}/{total} "
                    f"[{clock(el)}<{clock(rate * (total - step))}, {rate:.2f}s/it, avr_loss={avg:.4f}]")
        log(f"epoch {epoch}/{a.epochs} done — avr_loss {ep_loss / max(1, ep_n):.4f}")
        if epoch < a.epochs:
            save_lora(model, a.out / f"{a.name}-{epoch:06d}.safetensors")

    peft_dir = a.out / "peft"
    model.save_pretrained(peft_dir)
    tok.save_pretrained(peft_dir)
    final = a.out / f"{a.name}.safetensors"
    save_lora(model, final)
    log(f"saved final LoRA: {final}")
    (a.out / f"{a.name}.json").write_text(json.dumps({
        "base_model": a.base, "rank": a.rank, "alpha": a.alpha or a.rank * 2, "epochs": a.epochs, "lr": a.lr,
        "quant": quant, "max_length": a.max_length, "examples": len(examples)}, indent=2), encoding="utf-8")
    del model, opt
    torch.cuda.empty_cache()
    if a.llama_cpp:
        convert_gguf(peft_dir, base, a.out / f"{a.name}.gguf", a.llama_cpp, a.gguf_type)


if __name__ == "__main__":
    main()
