# workflows/ notes

> **2026-09-15:** this file was overwritten (by Claude, adding the SeedHunter section
> below) without reading it first — a mistake; the prior content, including a "Krea"
> section the main `docker-compose.yml` still references by name (see its comment
> above the `h3-machine` output volume mount), is lost and not recoverable (no git repo
> here, no backup copy found, and no snapper config covers `/home`). The load-bearing
> *fact* that section documented is still intact in that compose comment: the output
> dir must point at the real shared ComfyUI output (`$ROCSY_COMFY_SHARED/output`),
> not a local per-app folder — matching the same convention `Krea/docker-compose.yml`
> uses — but whatever additional narrative/detail the original note had is gone.
> Flagging so a human can decide if it's worth reconstructing further.

`MiniMax-H3.json` and `LTX-2.5-Final-Optimized.json` are ComfyUI-frontend-exported
files — edit them by opening/saving in ComfyUI's own web UI.

`H3-SeedHunter-Candidates.API.json` and `H3-SeedHunter-Finalize.API.json` are
**hand-authored, already-flat ComfyUI API-format JSON** (`{"<node_id>": {"class_type":
..., "inputs": {...}}}`), built directly from `backend/generation.py`'s
`build_seedhunter_candidates_workflow()` / `build_seedhunter_finalize_workflow()`.
They exist because the source SeedHunter workflow relies on a `cg-use-everywhere`
"Anything Everywhere" broadcast node whose links aren't statically resolvable in the
graph JSON, so the app's usual frontend-graph flattener (`_frontend_workflow_to_prompt`,
used for LTX) can't process it correctly — these two files replace that entirely.

**Never open or re-save these two through ComfyUI's editor** — it only understands
the frontend graph format (`nodes`/`links` arrays), not this flat API format, and
would silently corrupt their shape.
