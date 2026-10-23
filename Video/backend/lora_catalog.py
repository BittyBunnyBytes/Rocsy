"""Persistent gallery categories and editable metadata for installed LoRAs."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import models
from .config import settings

PATH = settings.data_dir / "lora-gallery-config.json"
_lock = threading.RLock()
_data: dict[str, dict[str, Any]] = {}


def initialize() -> None:
    with _lock:
        if PATH.is_file():
            raw = json.loads(PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _data.update({key: value for key, value in raw.items() if isinstance(value, dict)})
        _sync()


def _save() -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(_data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, PATH)
    _sync()


def _slots(model: str) -> list[tuple[str, str]]:
    if model in models.LORA_LISTS:
        return models.LORA_LISTS[model]
    raise ValueError("Choose H3, LTX 2.5 or Wan 2.2")


def _base_group(filename: str) -> str:
    return str(models.CUSTOM_LORA_INFO.get(filename, {}).get("group") or models.LORA_INFO.get(filename, {}).get("group") or "Other")


def _active(model: str) -> list[str]:
    return [filename for filename, _ in _slots(model) if not models.CUSTOM_LORA_INFO.get(filename, {}).get("deleted")]


def _view(model: str) -> dict[str, Any]:
    filenames = _active(model)
    saved = _data.get(model, {})
    categories = [value for value in saved.get("categories", []) if isinstance(value, str) and value.strip() and value.casefold() != "uploaded"]
    assignments = {key: ("Other" if value.casefold() == "uploaded" else value)
                   for key, value in saved.get("assignments", {}).items() if key in filenames and isinstance(value, str)}
    for filename in filenames:
        name = assignments.get(filename) or _base_group(filename)
        if name.casefold() == "uploaded":
            name = "Other"
        if name not in categories:
            categories.append(name)
    order = [name for name in saved.get("order", []) if name in filenames]
    order.extend(name for name in filenames if name not in order)
    return {"model": model, "categories": categories, "assignments": assignments, "order": order,
            "metadata": {name: value for name, value in saved.get("metadata", {}).items() if name in filenames}}


def get(model: str) -> dict[str, Any]:
    with _lock:
        return _view(model)


def save(model: str, body: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        filenames = _active(model)
        categories = body.get("categories")
        assignments = body.get("assignments")
        order = body.get("order")
        if not isinstance(categories, list) or not categories or len(categories) > 80:
            raise ValueError("Add at least one category")
        categories = [str(name).strip() for name in categories]
        if any(not name or len(name) > 60 or name.casefold() == "uploaded" for name in categories) or len({name.casefold() for name in categories}) != len(categories):
            raise ValueError("Category names must be unique and at most 60 characters")
        if not isinstance(assignments, dict) or set(assignments) != set(filenames) or any(value not in categories for value in assignments.values()):
            raise ValueError("Assign every installed LoRA to a category")
        if not isinstance(order, list) or len(order) != len(filenames) or set(order) != set(filenames):
            raise ValueError("LoRA order must include each installed LoRA once")
        current = _data.setdefault(model, {})
        current.update(categories=categories, assignments=assignments, order=order)
        _save()
        return _view(model)


def assign(model: str, filename: str, category: str) -> dict[str, Any]:
    category = category.strip()[:60]
    if not category or category.casefold() == "uploaded":
        raise ValueError("Choose a category")
    with _lock:
        view = _view(model)
        if filename not in _active(model):
            raise ValueError("LoRA was not found")
        if category.casefold() not in {value.casefold() for value in view["categories"]}:
            view["categories"].append(category)
        else:
            category = next(value for value in view["categories"] if value.casefold() == category.casefold())
        view["assignments"][filename] = category
        current = _data.setdefault(model, {})
        current.update(categories=view["categories"], assignments=view["assignments"], order=view["order"])
        _save()
        return _view(model)


def edit(model: str, filename: str, body: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        if filename not in _active(model):
            raise ValueError("LoRA was not found")
        if str(body.get("group", "")).strip().casefold() == "uploaded":
            raise ValueError("Uploaded is not a gallery category")
        metadata: dict[str, Any] = {}
        for key, limit in (("label", 100), ("description", 10000), ("url", 2000), ("strength_note", 500)):
            value = body.get(key, "")
            if not isinstance(value, str) or len(value) > limit or (key == "label" and not value.strip()):
                raise ValueError(f"Invalid {key}")
            if key == "url" and value and (urlsplit(value).scheme not in {"http", "https"} or not urlsplit(value).netloc):
                raise ValueError("Source URL must be HTTP(S)")
            metadata[key] = value.strip() if key != "description" else value
        strength = body.get("default_strength")
        if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not 0 <= strength <= 2:
            raise ValueError("Recommended strength must be between 0 and 2")
        metadata["strength"] = float(strength)
        triggers = body.get("triggers")
        if not isinstance(triggers, list) or len(triggers) > 30 or any(not isinstance(word, str) or len(word) > 100 for word in triggers):
            raise ValueError("Invalid trigger words")
        metadata["triggers"] = [word.strip() for word in triggers if word.strip()]
        current = _data.setdefault(model, {})
        current.setdefault("metadata", {})[filename] = metadata
        _save()
        if body.get("group"):
            assign(model, filename, str(body["group"]))
        return _view(model)


def index(model: str) -> dict[str, dict[str, Any]]:
    view = get(model)
    return {name: {"description": info.get("description", "")} for name, info in view["metadata"].items()}


def _sync() -> None:
    models.GALLERY_LORA_OVERRIDES.clear()
    for model in models.LORA_LISTS:
        view = _view(model)
        for filename in _active(model):
            override = dict(view["metadata"].get(filename, {}))
            if filename in view["assignments"]:
                override["group"] = view["assignments"][filename]
            if override:
                models.GALLERY_LORA_OVERRIDES[filename] = override
