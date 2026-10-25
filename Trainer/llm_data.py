"""Text datasets for LLM LoRA training: .jsonl / .json files of conversations or plain text.

Stdlib only: the web app uses it to count, check, import and edit records, and llm_train.py (in the
LLM trainer's own venv) uses it to read them, so both always agree on what a file contains.

Every record becomes either {"messages": [{"role", "content", ...}, ...]} (chat, optionally with
"tools") or {"text": str} (raw text, trained on in full). Messages keep the optional fields chat
templates understand: "reasoning_content" (thinking; also read from "thinking"/"reasoning"),
"tool_calls", "tool_call_id" and "name". Accepted shapes:
  {"messages": [{"role": "user", "content": "..."}, ...]}           OpenAI / chat-template format
  {"conversations": [{"from": "human", "value": "..."}, ...]}         ShareGPT
  {"prompt": "...", "completion": "..."} (also question/answer, query/response, input/output;
      prompt and completion may be message lists, TRL's conversational form)
  {"prompt": ..., "chosen": ..., "rejected": ...}                     preference pairs: trains on "chosen"
  {"instruction": "...", "input": "...", "output": "..."}            Alpaca
  {"text": "..."}                                                     plain text
An optional "system" field is put in front as a system message.

Other files (.txt/.md plain text, .csv/.tsv tables, .parquet) are converted to .jsonl on import
(import_records), so the dataset folder only ever holds the formats above.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Iterator

TEXT_EXTS = {".jsonl", ".json"}
IMPORT_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".parquet", ".jsonl", ".json"}
MAX_BYTES = 2 * 1024**3
EDIT_MAX_BYTES = 64 * 1024**2     # files the record editor loads whole
CHARS_PER_TOKEN = 3.6             # rough average for English with modern BPE tokenizers (estimates only)
TURN_TOKENS = 6                   # chat-template markers per message

_ROLES = {"human": "user", "user": "user", "gpt": "assistant", "assistant": "assistant", "model": "assistant",
          "bot": "assistant", "system": "system", "tool": "tool", "function": "tool", "ipython": "tool",
          "developer": "system"}
_PROMPT_KEYS = ("prompt", "question", "query", "user")
_REPLY_KEYS = ("completion", "response", "answer", "chosen", "assistant")
_THINK_KEYS = ("reasoning_content", "thinking", "reasoning")


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTS and not path.name.startswith(".")


def _str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):  # OpenAI content parts: keep the text parts
        return "".join(p.get("text", "") for p in value if isinstance(p, dict) and p.get("type", "text") == "text")
    return ""


def _jsonish(value: Any) -> Any:
    """Table cells hold lists as JSON text ('[{"role": ...}]'): decode those, leave the rest."""
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _message(t: Any) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(t, dict):
        return None, "a message is not an object"
    role = _ROLES.get(str(t.get("role") or t.get("from") or "").lower())
    if not role:
        return None, f"unknown role {t.get('role') or t.get('from')!r}"
    msg: dict[str, Any] = {"role": role, "content": _str(t.get("content") if "content" in t else t.get("value"))}
    think = next((_str(t[k]) for k in _THINK_KEYS if _str(t.get(k)).strip()), "")
    if think and role == "assistant":
        msg["reasoning_content"] = think
    calls = _jsonish(t.get("tool_calls"))
    if isinstance(calls, list) and calls and role == "assistant":
        msg["tool_calls"] = calls
    for k in ("tool_call_id", "name"):
        if isinstance(t.get(k), str) and t[k]:
            msg[k] = t[k]
    return msg, ""


def _messages(turns: Any) -> tuple[list[dict[str, Any]] | None, str]:
    if isinstance(turns, str):
        return [{"role": "user", "content": turns}], ""
    if not isinstance(turns, list):
        return None, "messages are not a list"
    out = []
    for t in turns:
        m, why = _message(t)
        if m is None:
            return None, why
        out.append(m)
    return out, ""


def _learnable(m: dict[str, Any]) -> bool:
    return m["role"] == "assistant" and bool(m["content"].strip() or m.get("tool_calls") or m.get("reasoning_content"))


def _chat(messages: list[dict[str, Any]], system: str, record: dict[str, Any], fmt: str) -> tuple[dict[str, Any] | None, str]:
    if system and not any(m["role"] == "system" for m in messages):
        messages.insert(0, {"role": "system", "content": system})
    if not any(_learnable(m) for m in messages):
        return None, "no assistant reply to learn from"
    out: dict[str, Any] = {"messages": messages}
    tools = _jsonish(record.get("tools"))
    if isinstance(tools, list) and tools:
        out["tools"] = tools
    return out, fmt


def normalize(record: Any) -> tuple[dict[str, Any] | None, str]:
    """(normalized record, format name) or (None, why it was skipped)."""
    if not isinstance(record, dict):
        return None, "not a JSON object"
    record = {(k.lower() if isinstance(k, str) else k): v for k, v in record.items()}
    system = _str(record.get("system") or record.get("system_prompt")).strip()
    for key, fmt in (("messages", "chat"), ("conversations", "sharegpt"), ("conversation", "sharegpt")):
        turns = _jsonish(record.get(key))
        if isinstance(turns, list):
            msgs, why = _messages(turns)
            return (None, why) if msgs is None else _chat(msgs, system, record, fmt)
    if "instruction" in record and "output" in record:
        prompt = _str(record["instruction"]).strip()
        if _str(record.get("input")).strip():
            prompt += "\n\n" + _str(record["input"]).strip()
        if not _str(record["output"]).strip():
            return None, "empty output"
        msgs = [{"role": "user", "content": prompt}, {"role": "assistant", "content": _str(record["output"])}]
        return _chat(msgs, system, record, "alpaca")
    prompt_key = next((k for k in (*_PROMPT_KEYS, "input") if k in record), None)
    reply_key = next((k for k in (*_REPLY_KEYS, "output") if k in record), None)
    if prompt_key and reply_key:
        prompt, reply = _jsonish(record[prompt_key]), _jsonish(record[reply_key])
        head, why = _messages(prompt)
        if head is None:
            return None, why
        if isinstance(reply, list):          # conversational completion: a list of assistant/tool turns
            tail, why = _messages(reply)
            if tail is None:
                return None, why
        else:
            tail = [{"role": "assistant", "content": _str(reply)}]
            for k in _THINK_KEYS:
                if _str(record.get(k)).strip():
                    tail[0]["reasoning_content"] = _str(record[k])
                    break
        if not any(_learnable(m) for m in tail):
            return None, f"empty {reply_key}"
        fmt = "preference (trains on chosen)" if reply_key == "chosen" else "prompt-completion"
        return _chat(head + tail, system, record, fmt)
    if isinstance(record.get("text"), str):
        return ({"text": record["text"]}, "text") if record["text"].strip() else (None, "empty text")
    return None, "unknown shape (expected messages, conversations, prompt/completion, instruction/output or text)"


# ---------------------------------------------------------------------------------------------- reading

def _raw_records(path: Path) -> Iterator[tuple[int, Any]]:
    if path.suffix.lower() == ".jsonl":
        with path.open(encoding="utf-8-sig", errors="replace") as fh:
            n = 0
            for line in fh:
                if line.strip():
                    n += 1
                    try:
                        yield n, json.loads(line)
                    except ValueError:
                        yield n, ValueError("invalid JSON")
        return
    data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    if isinstance(data, dict):  # {"data": [...]} / {"train": [...]} wrappers, or one record
        data = next((data[k] for k in ("data", "train", "records", "examples", "rows") if isinstance(data.get(k), list)), [data])
    for n, item in enumerate(data if isinstance(data, list) else [data], 1):
        yield n, item


def read(path: Path) -> Iterator[dict[str, Any]]:
    """Normalized records of one file; unusable ones are skipped."""
    for _, raw in _raw_records(path):
        if not isinstance(raw, Exception):
            rec, _ = normalize(raw)
            if rec:
                yield rec


def est_tokens(rec: dict[str, Any]) -> int:
    """Rough token count (the real one needs the base model's tokenizer; llm_train.py logs that)."""
    if "text" in rec:
        return int(len(rec["text"]) / CHARS_PER_TOKEN) + 1
    chars = sum(len(m["content"]) + len(m.get("reasoning_content", "")) + (len(json.dumps(m["tool_calls"])) if "tool_calls" in m else 0)
                for m in rec["messages"])
    chars += len(json.dumps(rec["tools"])) if rec.get("tools") else 0
    return int(chars / CHARS_PER_TOKEN) + TURN_TOKENS * len(rec["messages"])


def order_problem(rec: dict[str, Any]) -> str:
    """Turn-order issues many chat templates refuse or that teach odd habits ('' = fine)."""
    msgs = [m for m in rec.get("messages", []) if m["role"] != "system"]
    if not msgs:
        return ""
    if any(m["role"] == "system" for m in rec["messages"][1:]):
        return "a system message after the first turn"
    if msgs[0]["role"] != "user":
        return "starts with an assistant turn"
    if msgs[-1]["role"] != "assistant":
        return f"ends with a {msgs[-1]['role']} turn (nothing is learned after the last reply)"
    for a, b in zip(msgs, msgs[1:]):
        if a["role"] == b["role"] == "user":
            return "two user turns in a row"
    return ""


def _key(rec: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def inspect(path: Path, sample: int = 2, max_length: int = 2048) -> dict[str, Any]:
    """Record counts, formats, size estimates and the first few problems of a file, for the UI and
    the training check."""
    out: dict[str, Any] = {"file": path.name, "size_mb": round(path.stat().st_size / 1048576, 2), "records": 0,
                           "skipped": 0, "formats": {}, "problems": [], "error": "", "sample": [],
                           "editable": path.suffix.lower() == ".jsonl" and path.stat().st_size <= EDIT_MAX_BYTES,
                           "tokens": 0, "longest": 0, "over_limit": 0, "max_length": max_length, "duplicates": 0,
                           "order_warnings": 0, "order_example": "", "thinking": 0, "tool_calls": 0, "turns": 0}
    if path.stat().st_size > MAX_BYTES:
        out["error"] = "file is larger than 2 GB"
        return out
    seen: set[str] = set()
    try:
        for n, raw in _raw_records(path):
            rec, why = (None, str(raw)) if isinstance(raw, Exception) else normalize(raw)
            if rec is None:
                out["skipped"] += 1
                if len(out["problems"]) < 5:
                    out["problems"].append(f"record {n}: {why}")
                continue
            out["records"] += 1
            out["formats"][why] = out["formats"].get(why, 0) + 1
            if len(out["sample"]) < sample:
                out["sample"].append(rec)
            t = est_tokens(rec)
            out["tokens"] += t
            out["longest"] = max(out["longest"], t)
            out["over_limit"] += "messages" in rec and t > max_length   # plain text is split into windows instead
            k = _key(rec)
            out["duplicates"] += k in seen
            seen.add(k)
            if "messages" in rec:
                out["turns"] += sum(m["role"] == "assistant" for m in rec["messages"])
                out["thinking"] += any(m.get("reasoning_content") or "<think>" in m["content"] for m in rec["messages"])
                out["tool_calls"] += any(m.get("tool_calls") for m in rec["messages"]) or bool(rec.get("tools"))
                problem = order_problem(rec)
                if problem:
                    out["order_warnings"] += 1
                    out["order_example"] = out["order_example"] or f"record {n}: {problem}"
    except (OSError, ValueError) as exc:
        out["error"] = f"could not read it: {exc}"[:200]
    return out


# ---------------------------------------------------------------------------------------------- editing

def load_lines(path: Path) -> list[Any]:
    """Every non-blank line of a .jsonl: the decoded value, or the raw line text if it isn't JSON."""
    if path.stat().st_size > EDIT_MAX_BYTES:
        raise ValueError("the file is too large to edit here (over 64 MB)")
    out = []
    with path.open(encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    out.append(line.rstrip("\n"))
    return out


def write_lines(path: Path, records: Iterable[Any]) -> int:
    """Atomically (re)write a .jsonl; raw (undecodable) lines are kept as they were."""
    tmp = path.with_name(f".{path.name}.tmp")
    n = 0
    with tmp.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")
            n += 1
    os.replace(tmp, path)
    return n


def clean_for_save(rec: Any) -> dict[str, Any]:
    """What the editor writes: the normalized record (so every line ends up in one format), or a
    ValueError saying why the trainer would skip it."""
    norm, why = normalize(rec)
    if norm is None:
        raise ValueError(why)
    return norm


# ---------------------------------------------------------------------------------------------- importing

def split_plain(text: str, mode: str = "paragraphs", target_chars: int = 6000) -> list[dict[str, str]]:
    """Plain text -> {"text"} records. "whole" = one record (the trainer cuts it into max_length
    windows); "paragraphs" = consecutive paragraphs grouped up to ~target_chars, so every record
    starts at a paragraph instead of mid-sentence; "blank3" = split only at 2+ blank lines
    (a separator many people put between separate documents in one file)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    if mode == "whole":
        return [{"text": text}]
    if mode == "blank3":
        return [{"text": p.strip()} for p in re.split(r"\n\s*\n\s*\n+", text) if p.strip()]
    out, buf, size = [], [], 0
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if buf and size + len(para) > target_chars:
            out.append({"text": "\n\n".join(buf)})
            buf, size = [], 0
        buf.append(para)
        size += len(para) + 2
    if buf:
        out.append({"text": "\n\n".join(buf)})
    return out


def _table_rows(path: Path) -> Iterator[dict[str, Any]]:
    ext = path.suffix.lower()
    if ext == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:  # only without pyarrow installed
            raise ValueError("reading .parquet needs pyarrow (rebuild the Trainer)") from exc
        for batch in pq.ParquetFile(path).iter_batches(batch_size=2000):
            yield from batch.to_pylist()
        return
    raw = path.read_bytes().decode("utf-8-sig", errors="replace")
    csv.field_size_limit(64 * 1024**2)
    yield from csv.DictReader(io.StringIO(raw), delimiter="\t" if ext == ".tsv" else ",")


def import_records(path: Path, mode: str = "paragraphs") -> tuple[list[Any], dict[str, Any]]:
    """Records to store as .jsonl for an uploaded file, and a report {records, skipped, formats, problems}."""
    ext = path.suffix.lower()
    report: dict[str, Any] = {"records": 0, "skipped": 0, "formats": {}, "problems": []}
    if ext in (".txt", ".md", ".markdown"):
        recs = split_plain(path.read_bytes().decode("utf-8-sig", errors="replace"), mode)
        report.update(records=len(recs), formats={"text": len(recs)} if recs else {})
        return recs, report
    rows: Iterable[tuple[int, Any]] = enumerate(_table_rows(path), 1) if ext in (".csv", ".tsv", ".parquet") else _raw_records(path)
    out = []
    for n, raw in rows:
        rec, why = (None, str(raw)) if isinstance(raw, Exception) else normalize(raw)
        if rec is None:
            report["skipped"] += 1
            if len(report["problems"]) < 5:
                report["problems"].append(f"row {n}: {why}")
            continue
        out.append(rec)
        report["formats"][why] = report["formats"].get(why, 0) + 1
    report["records"] = len(out)
    return out, report
