"""Model-specific trigger shortcuts for the Image studio's LoRA gallery."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from . import models
from .config import settings

PATH = settings.data_dir / 'lora-trigger-aliases.json'
_lock = threading.RLock()
_data: dict[str, list[dict[str, Any]]] = {'krea': [], 'qwen': [], 'moss': [], 'anima': []}
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    if PATH.is_file():
        raw = json.loads(PATH.read_text(encoding='utf-8'))
        for model in _data:
            if isinstance(raw.get(model), list):
                _data[model] = raw[model]
    _loaded = True


def _save() -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(_data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, PATH)


def get(model: str) -> list[dict[str, Any]]:
    if model not in _data:
        raise ValueError('Choose Krea 2, Qwen-Image 2.1, MOSS-Image, or Anima')
    with _lock:
        _load()
        return [dict(entry) for entry in _data[model]]


def save(model: str, filename: str, target: str, aliases: list[str]) -> list[dict[str, Any]]:
    slots = models.lora_slots_for(model) if model in _data else None
    if slots is None or filename not in slots or models.LORA_INFO.get(filename, {}).get('deleted'):
        raise ValueError('Choose an installed LoRA for the selected model')
    triggers = models.LORA_INFO.get(filename, {}).get('triggers') or []
    if target not in triggers:
        raise ValueError('Choose one of this LoRA’s trigger words')
    if not aliases or len(aliases) > 12 or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{1,39}', alias) for alias in aliases):
        raise ValueError('Use 2–40 letter, number, underscore or dash characters per alias')
    with _lock:
        _load()
        folded = {alias.casefold() for alias in aliases}
        if len(folded) != len(aliases):
            raise ValueError('Aliases must be unique')
        for entry in _data[model]:
            if entry['lora'] == filename and entry['target'] == target:
                continue
            if any(alias.casefold() in folded for alias in entry['aliases']):
                raise ValueError('An alias is already used by another LoRA')
        _data[model] = [entry for entry in _data[model] if not (entry['lora'] == filename and entry['target'] == target)]
        _data[model].append({'lora': filename, 'target': target, 'aliases': aliases})
        _save()
        return get(model)


def delete(model: str, filename: str, target: str) -> list[dict[str, Any]]:
    with _lock:
        _load()
        get(model)
        _data[model] = [entry for entry in _data[model] if not (entry['lora'] == filename and entry['target'] == target)]
        _save()
        return get(model)


def expand(prompt: str, model: str, loras: list) -> str:
    slots = models.lora_slots_for(model)
    with _lock:
        _load()
        enabled = {slots[index] for index, slot in enumerate(loras) if index < len(slots) and slot.enabled}
        replacements = {alias.casefold(): entry['target'] for entry in _data[model] if entry['lora'] in enabled for alias in entry['aliases']}
    if not replacements:
        return prompt
    pattern = re.compile(r'(?<![A-Za-z0-9_-])(?:' + '|'.join(re.escape(word) for word in sorted(replacements, key=len, reverse=True)) + r')(?![A-Za-z0-9_-])', re.I)
    return pattern.sub(lambda match: replacements[match.group().casefold()], prompt)
