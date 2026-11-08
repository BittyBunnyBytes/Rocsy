"""The info dialog's Stats tab: where a generation's wall time went.

Ported from Image's job_stats (itself from Video's job_details). app.py records, per ComfyUI prompt
of a job (Music submits one), every
node's wall time from the websocket "executing" transitions plus its progress
ticks into jobs.timings = {runs: [{prompt_id, label, submitted, started,
finished, nodes: {id: [class, title]}, cached, segments: [{node, start, end,
ticks: [[seconds into the node, value, max]]}]}]}. This groups them into
stages and categories for the workflow map.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

_CATEGORY_LABELS = {
    "wait": "Waiting", "load": "Load models & inputs", "compose": "Compose score", "encode": "Encode",
    "sample": "Sampling", "decode": "Decode", "output": "Save & finish", "other": "Other nodes",
}


def _category(class_type: str) -> str:
    name = class_type.lower()
    if "sampler" in name:
        return "sample"
    if "decode" in name:
        return "decode"
    if name.startswith("yue2generate") or "toabc" in name:  # YuE 2's ABC score + song conditioning, SheetSage reference
        return "compose"
    if "load" in name:  # before "encode": AudioEncoderLoader is a loader
        return "load"
    if "encode" in name or "conditioning" in name:
        return "encode"
    if "save" in name or "preview" in name:
        return "output"
    return "other"


def _node_info(nodes: dict[str, list[str]], node_id: str) -> list[str]:
    """[class, title]; a node expanded at run time (group / subgraph) has an id like "87:3" or "87.3"."""
    return nodes.get(node_id) or nodes.get(re.split(r"[.:]", node_id)[0]) or ["", ""]


def _epoch(timestamp: str | None) -> float | None:
    try:
        return datetime.fromisoformat(timestamp).timestamp() if timestamp else None
    except ValueError:
        return None


def _step_seconds(ticks: list[list[Any]], total: float) -> list[float]:
    """Seconds per progress tick (sampler steps, decode tiles); the first one
    includes everything before it, e.g. moving the model onto the GPU."""
    times = [float(tick[0]) for tick in ticks if tick and isinstance(tick[0], (int, float))]
    if len(times) < 2:
        return []
    return ([round(end - start, 3) for start, end in zip([0.0, *times], times)]
            + ([round(total - times[-1], 3)] if total - times[-1] >= 0.5 else []))


def job_stats(item: dict[str, Any], live: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        timings = live or json.loads(item.get("timings") or "{}")
    except (TypeError, ValueError):
        timings = {}
    runs = [run for run in (timings.get("runs") or []) if run.get("segments")]
    if not runs:
        message = ("Waiting for ComfyUI to start this generation." if item.get("state") in {"waiting_for_gpu", "preparing", "queued"}
                   else "Stage timings are recorded for generations started after the Stats update.")
        return {"available": False, "message": message}
    origin = _epoch(item.get("created_at")) or runs[0]["submitted"]
    stages: list[dict[str, Any]] = []

    def add(label: str, category: str, start: float, end: float, **extra: Any) -> None:
        if end - start >= 0.05:
            stages.append({"label": label, "category": category, "start": round(start - origin, 3),
                           "seconds": round(end - start, 3), **extra})

    cursor = origin
    for number, run in enumerate(runs, 1):
        prefix = f"{run.get('label') or f'Run {number}'} · " if len(runs) > 1 else ""
        first = run["segments"][0]["start"]
        add(f"{prefix}Waiting for the GPU" if number == 1 else f"{prefix}Waiting before this run", "wait", cursor, run["submitted"],
            detail="Queued behind other GPU work (shared lock), plus any ComfyUI profile switch" if number == 1
            else "Time between ComfyUI prompts")
        add(f"{prefix}ComfyUI queue & setup", "wait", max(cursor, run["submitted"]), first,
            detail=f"Validating the prompt; {len(run.get('cached') or [])} cached node(s) skipped")
        for segment in run["segments"]:
            class_type, title = _node_info(run.get("nodes") or {}, segment["node"])
            seconds = segment["end"] - segment["start"]
            add(prefix + (title or class_type or f"Node {segment['node']}"), _category(class_type), segment["start"], segment["end"],
                node=segment["node"], class_type=class_type, running=bool(segment.get("running")),
                steps=_step_seconds(segment.get("ticks") or [], seconds))
        cursor = run.get("finished") or run["segments"][-1]["end"]
    completed = _epoch(item.get("completed_at"))
    if completed and not live:
        add("Saving & finishing", "output", cursor, completed, detail="Collecting the audio from ComfyUI and updating the job")
    end = (completed if not live else None) or max(stage["start"] + stage["seconds"] for stage in stages) + origin
    total = round(end - origin, 3)
    categories: dict[str, float] = {}
    for stage in stages:
        categories[stage["category"]] = categories.get(stage["category"], 0.0) + stage["seconds"]
    return {
        "available": True, "running": bool(live), "total": total,
        "stages": stages,
        "categories": [{"key": key, "label": _CATEGORY_LABELS[key], "seconds": round(seconds, 3)}
                       for key, seconds in sorted(categories.items(), key=lambda pair: -pair[1])],
        "nodes_run": sum(len(run["segments"]) for run in runs),
        "nodes_cached": sum(len(run.get("cached") or []) for run in runs),
    }
