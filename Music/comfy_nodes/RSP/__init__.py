"""RSP (Rocsy Suno Parity): Suno-style covers on YuE2 that keep the covered song's singer.

Module scope stays light: nothing heavy is imported while ComfyUI builds its node list.
"""
from .rsp.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
