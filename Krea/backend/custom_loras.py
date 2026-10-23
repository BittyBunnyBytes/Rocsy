"""Persistent, user-managed LoRAs. Filenames and Krea and Qwen slot positions stay stable."""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import threading
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import models
from .config import settings

MAX_LORA_BYTES = 6 * 1024**3
REGISTRY = settings.data_dir / 'custom-loras.json'
MEDIA_ROOT = settings.data_dir / 'custom-lora-media'
MODEL_ROOT = Path(os.getenv('KREA_CUSTOM_LORA_DIR', '/models/loras/KreaCustom'))
_lock = threading.RLock()
_entries: list[dict[str, Any]] = []
_initialized = False


class InstallCancelled(Exception):
    """A linked download was cancelled while its model file was being copied."""


def _copy_file(source: Path, destination: Path, cancel_check: Callable[[], bool] | None) -> None:
    if not cancel_check:
        shutil.copyfile(source, destination)
        return
    with source.open('rb') as original, destination.open('wb') as target:
        while chunk := original.read(8 * 1024 * 1024):
            if cancel_check():
                raise InstallCancelled()
            target.write(chunk)
        if cancel_check():
            raise InstallCancelled()


def _persist() -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    temporary = REGISTRY.with_suffix('.tmp')
    temporary.write_text(json.dumps(_entries, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, REGISTRY)


def _sync() -> None:
    for entry in _entries:
        filename = entry['filename']
        slots = models.lora_slots_for(entry['model'])
        position = next((n for n, name in enumerate(slots) if name == filename), None)
        if position is None:
            slots.append(filename)
        else:
            slots[position] = filename
        models.LORA_INFO[filename] = {
            'label': entry['label'], 'group': entry['group'], 'url': entry.get('url', ''),
            'source': entry.get('source', ''),
            'strength': entry.get('default_strength', 1.0), 'strength_note': entry.get('strength_note', ''),
            'triggers': entry.get('triggers', []), 'custom_id': entry['id'], 'deleted': entry.get('deleted', False),
        }


def initialize() -> None:
    global _initialized, _entries
    with _lock:
        if _initialized:
            return
        if REGISTRY.is_file():
            data = json.loads(REGISTRY.read_text(encoding='utf-8'))
            if not isinstance(data, list):
                raise ValueError('Custom LoRA registry must be a list')
            _entries = data
            if any(entry.get('group') == 'Uploaded' for entry in _entries):
                for entry in _entries:
                    if entry.get('group') == 'Uploaded':
                        entry['group'] = 'Other'
                _persist()
        _sync()
        _initialized = True


def entries(include_deleted: bool = False) -> list[dict[str, Any]]:
    initialize()
    with _lock:
        return [dict(item) for item in _entries if include_deleted or not item.get('deleted')]


def get(item_id: str, include_deleted: bool = False) -> dict[str, Any] | None:
    initialize()
    with _lock:
        return next((dict(item) for item in _entries if item['id'] == item_id and (include_deleted or not item.get('deleted'))), None)


def get_by_filename(filename: str) -> dict[str, Any] | None:
    initialize()
    with _lock:
        return next((dict(item) for item in _entries if item['filename'] == filename and not item.get('deleted')), None)


def validate_file(path: Path) -> None:
    size = path.stat().st_size
    if size < 16 or size > MAX_LORA_BYTES:
        raise ValueError('LoRA file must be a nonempty safetensors file under 6 GiB')
    with path.open('rb') as stream:
        length = struct.unpack('<Q', stream.read(8))[0]
        if length < 2 or length > min(16 * 1024 * 1024, size - 8):
            raise ValueError('Invalid safetensors header')
        try:
            header = json.loads(stream.read(length))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError('Invalid safetensors header') from exc
    if not isinstance(header, dict) or not any(key != '__metadata__' for key in header):
        raise ValueError('LoRA file has no tensor weights')


def _storage_name(entry: dict[str, Any]) -> str:
    """Older registry entries used their ID as the on-disk filename."""
    name = entry.get('storage_name') or f"{entry['id']}.safetensors"
    return name if name == Path(name).name and name.endswith('.safetensors') else f"{entry['id']}.safetensors"


def _named_storage(value: str, item_id: str) -> str:
    stem = value.replace('\\', '/').rsplit('/', 1)[-1]
    if stem.lower().endswith('.safetensors'):
        stem = stem[:-12]
    stem = unicodedata.normalize('NFKD', stem).encode('ascii', 'ignore').decode('ascii')
    stem = re.sub(r'[^A-Za-z0-9._-]+', '_', stem).strip('._-')[:80].strip('._-') or 'LoRA'
    occupied = {entry['filename'].rsplit('/', 1)[-1].casefold() for entry in _entries}
    name = f'{stem}.safetensors'
    counter = 2
    while name.casefold() in occupied or (MODEL_ROOT / name).exists():
        name = f'{stem}-{counter}.safetensors'
        counter += 1
        if counter > 10000:
            return f'{stem}-{item_id[:8]}.safetensors'
    return name


def install(path: Path, model: str, label: str, *, source: str = 'upload',
            source_job_id: str = '', triggers: list[str] | None = None,
            suggested_filename: str = '', cancel_check: Callable[[], bool] | None = None) -> dict[str, Any]:
    initialize()
    if model not in {'krea', 'qwen', 'moss', 'anima'}:
        raise ValueError('Choose Krea 2, Qwen-Image 2.1, MOSS-Image, or Anima')
    label = label.strip()[:100]
    if not label:
        raise ValueError('Give the LoRA a name')
    validate_file(path)
    with _lock:
        prior = next((entry for entry in _entries if source_job_id and entry.get('source_job_id') == source_job_id and entry['model'] == model), None)
        if prior:
            if prior.get('deleted'):
                MODEL_ROOT.mkdir(parents=True, exist_ok=True)
                target = MODEL_ROOT / _storage_name(prior)
                temporary = MODEL_ROOT / f"{prior['id']}.part"
                try:
                    _copy_file(path, temporary, cancel_check)
                except InstallCancelled:
                    temporary.unlink(missing_ok=True)
                    raise
                os.replace(temporary, target)
                prior['deleted'] = False
                _persist()
                _sync()
            return dict(prior)
        item_id = uuid.uuid4().hex
        MODEL_ROOT.mkdir(parents=True, exist_ok=True)
        storage_name = _named_storage(suggested_filename or label, item_id) if source == 'link' else f'{item_id}.safetensors'
        filename = f'KreaCustom/{storage_name}'
        target = MODEL_ROOT / storage_name
        temporary = MODEL_ROOT / f'{item_id}.part'
        try:
            _copy_file(path, temporary, cancel_check)
            os.replace(temporary, target)
            entry = {
                'id': item_id, 'filename': filename, 'model': model, 'label': label,
                'storage_name': storage_name,
                'group': 'Trainer made' if source == 'trainer' else 'Other',
                'source': source, 'source_job_id': source_job_id, 'default_strength': 1.0,
                'strength_note': 'Default starting strength; adjust after testing',
                'triggers': (triggers or [])[:12], 'description': '', 'creator': '',
                'version': '',
                'base_model': {'krea': 'Krea 2', 'qwen': 'Qwen-Image 2.1', 'moss': 'MOSS-Image', 'anima': 'Anima'}[model],
                'url': '', 'previews': [], 'deleted': False,
            }
            _entries.append(entry)
            _persist()
            _sync()
            return dict(entry)
        except Exception:
            temporary.unlink(missing_ok=True)
            if not any(item['id'] == item_id for item in _entries):
                target.unlink(missing_ok=True)
            raise


def register(storage_name: str, model: str, label: str, *, source: str = 'models', source_job_id: str = '',
             triggers: list[str] | None = None) -> dict[str, Any]:
    """Add a LoRA the Rocsy Models service downloaded (or the Trainer copied) into
    MODEL_ROOT (no copy). Registering the same file again returns its existing entry."""
    initialize()
    if model not in {'krea', 'qwen', 'moss', 'anima'}:
        raise ValueError('Choose Krea 2, Qwen-Image 2.1, MOSS-Image, or Anima')
    if storage_name != Path(storage_name).name or not storage_name.endswith('.safetensors') or storage_name.startswith('.'):
        raise ValueError('Bad LoRA file name')
    validate_file(MODEL_ROOT / storage_name)
    label = label.strip()[:100] or storage_name[:-12]
    with _lock:
        prior = next((entry for entry in _entries if _storage_name(entry) == storage_name and entry['model'] == model), None)
        if prior:
            prior['deleted'] = False
            _persist()
            _sync()
            return dict(prior)
        item_id = uuid.uuid4().hex
        entry = {
            'id': item_id, 'filename': f'KreaCustom/{storage_name}', 'model': model, 'label': label,
            'storage_name': storage_name, 'group': 'Trainer made' if source == 'trainer' else 'Other',
            'source': source, 'source_job_id': source_job_id,
            'default_strength': 1.0, 'strength_note': 'Default starting strength; adjust after testing',
            'triggers': (triggers or [])[:12], 'description': '', 'creator': '', 'version': '',
            'base_model': {'krea': 'Krea 2', 'qwen': 'Qwen-Image 2.1', 'moss': 'MOSS-Image', 'anima': 'Anima'}[model],
            'url': '', 'previews': [], 'deleted': False,
        }
        _entries.append(entry)
        _persist()
        _sync()
        return dict(entry)


def update(item_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    initialize()
    with _lock:
        entry = next((item for item in _entries if item['id'] == item_id and not item.get('deleted')), None)
        if not entry:
            raise KeyError(item_id)
        revised = dict(entry)
        for key in ('label', 'description', 'creator', 'version', 'base_model', 'url', 'strength_note'):
            if key in changes:
                value = changes[key]
                if not isinstance(value, str):
                    raise ValueError(f'{key} must be text')
                if key == 'url' and value.strip() and urlsplit(value.strip()).scheme not in {'http', 'https'}:
                    raise ValueError('Source URL must be HTTP(S)')
                revised[key] = value.strip()[:(10000 if key == 'description' else 500)]
        if not revised['label']:
            raise ValueError('Give the LoRA a name')
        if 'default_strength' in changes:
            value = float(changes['default_strength'])
            if not 0 <= value <= 2:
                raise ValueError('Recommended strength must be between 0 and 2')
            revised['default_strength'] = value
        if 'triggers' in changes:
            value = changes['triggers']
            if not isinstance(value, list) or any(not isinstance(word, str) for word in value):
                raise ValueError('Triggers must be a list of words')
            revised['triggers'] = [word.strip()[:100] for word in value[:12] if word.strip()]
        if 'previews' in changes:
            value = changes['previews']
            if not isinstance(value, list) or any(not isinstance(item, dict) or
                    not str(item.get('poster', '')).startswith(f'api/lora-gallery/media/{item_id}/')
                    or (item.get('video') and not str(item['video']).startswith(f'api/lora-gallery/media/{item_id}/'))
                    for item in value):
                raise ValueError('Invalid preview paths')
            revised['previews'] = value[:6]
        entry.update(revised)
        _persist()
        _sync()
        return dict(entry)


def delete(item_id: str) -> dict[str, Any]:
    initialize()
    with _lock:
        entry = next((item for item in _entries if item['id'] == item_id and not item.get('deleted')), None)
        if not entry:
            raise KeyError(item_id)
        entry['deleted'] = True
        _persist()
        _sync()
        (MODEL_ROOT / _storage_name(entry)).unlink(missing_ok=True)
        shutil.rmtree(MEDIA_ROOT / item_id, ignore_errors=True)
        return dict(entry)


def gallery_index() -> dict[str, dict[str, Any]]:
    return {item['filename']: {
        'name': item['label'], 'creator': item.get('creator', ''), 'version': item.get('version', ''),
        'base_model': item.get('base_model', ''),
        'description': item.get('description', ''), 'trained_words': item.get('triggers', []),
        'url': item.get('url', ''), 'previews': item.get('previews', []),
    } for item in entries()}
