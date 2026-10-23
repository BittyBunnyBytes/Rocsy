"""Attach selected model-only LoRAs to a ComfyUI MODEL output."""
from __future__ import annotations

from typing import Any

from .models import GenerationRequest, lora_family, lora_slots_for


def model_with_loras(graph: dict[str, Any], source: str, request: GenerationRequest) -> list[Any]:
    previous: list[Any] = [source, 0]
    slots = lora_slots_for(lora_family(request.model))
    for index, slot in enumerate(request.loras):
        if not slot.enabled:
            continue
        node_id = str(1000 + index)
        graph[node_id] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": previous, "lora_name": slots[index], "strength_model": slot.strength},
        }
        previous = [node_id, 0]
    return previous
