"""Turn a trainer job's raw console log into structured progress for the job cards.

Fizgig runs (Krea 2 / MiniMax H3) go through three phases in one log: latent caching,
text caching and training. Training prints a tqdm bar (``step/total [elapsed<eta, s/it,
avr_loss=...]``) plus an ``epoch k/E done — avr_loss L`` line per epoch. Other backends
fall back to the last tqdm-style ``n/total`` counter in the log.
"""
from __future__ import annotations

import re
from typing import Any

_TRAIN_BAR = re.compile(
    r"minimax-h3:\s*\d+%\|[^|]*\|\s*(\d+)/(\d+)\s*\[([^\]<]*)<([^,\]]*),\s*([\d.]+)\s*(s/it|it/s)(?:,\s*avr_loss=([\d.]+))?"
)
_GENERIC_BAR = re.compile(r"(\d+)/(\d+)\s*\[([^\]<]*)<([^,\]]*),\s*([\d.]+)\s*(s/it|it/s)(?:[^\]]*?(?:avr_loss|loss)[=:]\s*([\d.]+))?")
# sd-scripts' training bar is labelled "steps:"; its caching bars come first and are not.
_STEPS_BAR = re.compile(r"steps:\s*\d+%\|[^|]*\|\s*" + _GENERIC_BAR.pattern)
_EPOCH_DONE = re.compile(r"epoch (\d+)/(\d+) done\s*[—-]+\s*avr_loss ([\d.]+)")
_SAVED = re.compile(r"saved (?:final LoRA: )?(\S+\.safetensors)")
_FOUND = re.compile(r"found (\d+) images?, (\d+) clip\(s\) and (\d+) audio")
_ITEMS = re.compile(r"training: (\d+) items, (\d+) epochs")
_STILLS = re.compile(r"(\d+) clip still\(s\) added as photo items")
_CACHE_TICK = re.compile(r"(?:^|[\r\n])(\d+)it \[")


def _seconds(text: str) -> int | None:
    parts = text.strip().split(":")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


def parse_progress(log: str, state: str, request: dict[str, Any]) -> dict[str, Any]:
    epochs_total = int(request.get("epochs") or 0)
    out: dict[str, Any] = {
        "phase": "queued", "phase_label": "Waiting for the GPU queue", "overall": 0.0,
        "step": 0, "steps_total": 0, "epoch": 0, "epochs_total": epochs_total,
        "steps_per_epoch": 0, "epoch_step": 0, "step_mix": "", "sec_per_step": None,
        "eta_seconds": None, "elapsed_seconds": None, "loss": None, "epochs": [],
        "cache_done": 0, "cache_total": 0,
    }
    if state in {"waiting_for_gpu"} or not log:
        if state in {"preparing", "running"}:
            out.update(phase="starting", phase_label="Starting trainer", overall=0.01)
        return out

    found = list(_FOUND.finditer(log))
    clips = images = audio = 0
    if found:
        images, clips, audio = (int(x) for x in found[0].groups())
        out["cache_total"] = images + clips + audio
    stills = _STILLS.search(log)
    parts = []
    if clips:
        parts.append(f"{clips} clip")
    if stills:
        parts.append(f"{stills.group(1)} clip-still")
    if images:
        parts.append(f"{images} photo")
    if audio:
        parts.append(f"{audio} voice")
    out["step_mix"] = " + ".join(parts)

    bars = list(_TRAIN_BAR.finditer(log))
    items = _ITEMS.search(log)
    if items:
        out["epochs_total"] = int(items.group(2))
        out["steps_per_epoch"] = int(items.group(1))

    done = {}
    for m in _EPOCH_DONE.finditer(log):
        done[int(m.group(1))] = float(m.group(3))
        out["epochs_total"] = int(m.group(2))
    saved = [m.group(1).rsplit("/", 1)[-1] for m in _SAVED.finditer(log)]

    if bars:
        last = bars[-1]
        step, total = int(last.group(1)), int(last.group(2))
        rate, unit = float(last.group(5)), last.group(6)
        out.update(step=step, steps_total=total, phase="training", phase_label="Training")
        out["sec_per_step"] = rate if unit == "s/it" else (1 / rate if rate else None)
        out["elapsed_seconds"] = _seconds(last.group(3))
        out["eta_seconds"] = _seconds(last.group(4))
        if last.group(7):
            out["loss"] = float(last.group(7))
        ep_total = out["epochs_total"] or 1
        spe = out["steps_per_epoch"] or max(1, total // ep_total)
        out["steps_per_epoch"] = spe
        out["epoch"] = min(ep_total, step // spe + 1) if step < total else ep_total
        out["epoch_step"] = step - (out["epoch"] - 1) * spe
        # Caching is ~10% of a run; training fills the remaining 90%.
        out["overall"] = 0.1 + 0.9 * (step / total if total else 0)
    elif "MiniMax H3 training:" in log or "[load] streaming the MiniMax H3 base" in log:
        out.update(phase="loading", phase_label="Loading the base model", overall=0.1)
    elif "Loading Qwen3-VL-32B text encoder" in log or "cache_text" in log:
        out.update(phase="caching_text", phase_label="Encoding captions (text encoder)", overall=0.08)
    elif found:
        ticks = [int(m.group(1)) for m in _CACHE_TICK.finditer(log)]
        out["cache_done"] = min(out["cache_total"], max(ticks) if ticks else 0)
        frac = out["cache_done"] / out["cache_total"] if out["cache_total"] else 0
        out.update(phase="caching_latents", phase_label="Encoding clips (video VAE)", overall=0.07 * frac)
    else:
        generic = list(_STEPS_BAR.finditer(log)) or list(_GENERIC_BAR.finditer(log))
        if generic:
            g = generic[-1]
            step, total = int(g.group(1)), int(g.group(2))
            rate, unit = float(g.group(5)), g.group(6)
            out.update(step=step, steps_total=total, phase="training", phase_label="Training",
                       overall=step / total if total else 0,
                       eta_seconds=_seconds(g.group(4)), elapsed_seconds=_seconds(g.group(3)))
            out["sec_per_step"] = rate if unit == "s/it" else (1 / rate if rate else None)
            if g.group(7):
                out["loss"] = float(g.group(7))
            if out["epochs_total"] and total:
                spe = max(1, total // out["epochs_total"])
                out["steps_per_epoch"] = spe
                out["epoch"] = min(out["epochs_total"], step // spe + 1) if step < total else out["epochs_total"]
                out["epoch_step"] = step - (out["epoch"] - 1) * spe
        else:
            out.update(phase="starting", phase_label="Starting trainer", overall=0.01)

    if out["phase"] == "training" or done:
        for k in range(1, (out["epochs_total"] or 0) + 1):
            if k in done:
                status, frac = "done", 1.0
            elif k == out["epoch"] and out["phase"] == "training":
                status = "running"
                frac = out["epoch_step"] / out["steps_per_epoch"] if out["steps_per_epoch"] else 0
            else:
                status, frac = "pending", 0.0
            ckpt = next((s for s in saved if s.endswith(f"-{k:06d}.safetensors")), None)
            if ckpt is None and k == out["epochs_total"] and "saved final LoRA" in log:
                ckpt = next((s for s in reversed(saved) if not re.search(r"-\d{6}\.safetensors$", s)), None)
            out["epochs"].append({"epoch": k, "status": status, "fraction": round(frac, 4),
                                  "loss": done.get(k), "checkpoint": ckpt})

    if state == "complete":
        out.update(phase="complete", phase_label="Complete", overall=1.0)
        for e in out["epochs"]:
            e.update(status="done", fraction=1.0)
    elif state in {"failed", "cancelled"}:
        out["phase_label"] = f"{state.title()} during: {out['phase_label'].lower()}"
        out["phase"] = state
    out["overall"] = round(min(1.0, out["overall"]), 4)
    return out
