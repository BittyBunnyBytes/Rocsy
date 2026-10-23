#!/usr/bin/env python3
"""
Convert the YuE2 frontend/visual-editor ComfyUI workflow export into the
flat API-format prompt dict required by ComfyUI's /prompt endpoint, then
verify the conversion by POSTing it to a running ComfyUI instance.

Source (frontend/visual-editor export -- has top-level "nodes"/"links"/
"groups" keys, NOT the flat API format):
    $ROCSY_COMFY_ROOT/user/default/workflows/
        Audio/YuE 2/YuE 2 - INT8.json

Output (validated API-format prompt dict):
    BunnyBytes/workflows/YuE2-INT8.json (next to this script)

Conversion approach
--------------------
This particular export happens to include a `widgets_values_named` dict on
every node (name -> value), in addition to the positional `widgets_values`
array the frontend actually uses internally. That means we do NOT have to
guess widget order for any node type (even ones whose Python source we
never read) -- every input slot in a node's own `inputs` list already
carries its own declared `name`, and (for widget-backed slots) a
`widget.name` we can look up directly in `widgets_values_named`. The
script cross-checked this against source for the highest-risk/native node
types (YuE2GenerateABC, YuE2GenerateMusic, EmptyYuE2LatentAudio,
ComfySwitchNode, SheetSage2AudioToABC, AudioEncoderLoader, LoadAudio,
LoadAudioFromYouTube, SaveAudioAdvanced, VAEDecodeAudioTiled, KSampler,
CheckpointLoaderSimple, LCBooleanValue, MathExpression|pysssss) and it
matches exactly -- see NOTES.md for the full trace.

Special-cased node types:
  - "Note": pure comment, no execution effect, no outputs -> dropped.
  - "PreviewAny": frontend-only display node. Some instances in this graph
    are wired inline as a "tap" (they both display AND forward a value
    downstream, e.g. node 14 forwards YuE2GenerateABC's abc output into
    ComfySwitchNode's on_false). Dropping it naively would break that
    link, so it is treated as a *bypass*: whatever consumed its output now
    resolves straight through to whatever fed its own "source" input,
    recursively. Instances with no downstream consumers (e.g. node 53) are
    simply dropped with no effect.
  - "PrimitiveNode" / "PrimitiveFloat": frontend-only value sources with no
    API-format equivalent. Dropped as nodes; their literal widget value is
    inlined directly into every downstream input slot that a link says
    they feed (resolved recursively through any chained bypass nodes).
  - Any input slot whose declared type is "AUDIO_UI" or "AUDIOUPLOAD" is a
    frontend-only companion widget (waveform player / upload button) with
    no corresponding parameter on the real node's execute() function --
    these are dropped even though `widgets_values_named` may still list a
    value for them (confirmed via source: e.g. LoadAudio.execute(self,
    audio) takes only "audio", not "upload").
  - SaveAudioAdvanced's "format" input is a COMFY_DYNAMICCOMBO_V3 -- the
    real execute() expects a dict like {"format": "flac"} or
    {"format": "mp3", "quality": "V0"}, not a bare string. Wrapped
    accordingly.

Verification loop
------------------
Once ComfyUI is reachable, POST {"prompt": <dict>, "client_id": <uuid>} to
/prompt. A 200 with empty/absent node_errors is treated as success (the
job doesn't need to finish rendering). On a 400 with node_errors naming a
specific node/input, the error is parsed and reported so the mapping can
be patched and retried, bounded to MAX_ATTEMPTS.
"""

import json
import sys
import time
import urllib.error
import urllib.request
import os
import uuid
from pathlib import Path

SOURCE_WORKFLOW = (Path(os.path.expanduser(os.getenv("ROCSY_COMFY_ROOT", "~/ComfyUI")))
                   / "user/default/workflows/Audio/YuE 2/YuE 2 - INT8.json")
OUTPUT_PATH = Path(__file__).resolve().parent / "YuE2-INT8.json"
# NOTE: The infra plan assumes a native ComfyUI on 127.0.0.1:8188. During
# this conversion, 8188 never came up (connection refused throughout), but
# a ComfyUI Desktop instance was already running natively on 127.0.0.1:8189
# -- confirmed via `ps aux` to be the *same* install
# (the Bunny ComfyUI Desktop install, comfyui_version 0.35.0,
# same comfy_extras/nodes_yue2.py) with the yue2_3b_int8_convrot.safetensors
# checkpoint present in its CheckpointLoaderSimple options. Verification was
# run against 8189 for that reason. See NOTES.md for detail; retarget this
# to 8188 (or re-run unchanged, since it's the same install) once the
# native 8188 instance described in the plan is actually up.
COMFY_BASE = "http://127.0.0.1:8189"
MAX_ATTEMPTS = 10

# Node types with no API-side effect at all -- dropped outright, no bypass.
DROP_TYPES = {"Note"}

# Node types that pass their single input straight through as their single
# output for real downstream consumers, but have no API-format equivalent
# themselves (e.g. PreviewAny). Requires exactly one input slot.
PASSTHROUGH_TYPES = {"PreviewAny"}

# Node types whose widget value should be inlined as a literal wherever a
# link says they feed a downstream input, rather than emitted as nodes.
PRIMITIVE_TYPES = {"PrimitiveNode", "PrimitiveFloat"}

# Input slot types that are frontend-only companion widgets (upload button,
# inline audio player) with no corresponding execute() parameter.
SKIP_INPUT_SLOT_TYPES = {"AUDIO_UI", "AUDIOUPLOAD"}


def load_workflow(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_indexes(workflow: dict):
    nodes_by_id = {n["id"]: n for n in workflow["nodes"]}
    # links: [link_id, origin_node_id, origin_slot, target_node_id, target_slot, type]
    links_by_id = {link[0]: link for link in workflow["links"]}
    return nodes_by_id, links_by_id


def resolve_output(nodes_by_id, links_by_id, node_id, slot, _seen=None):
    """Resolve an (origin_node_id, origin_slot) pair to either
    ("literal", value) or ("ref", node_id, slot), transparently walking
    through PRIMITIVE_TYPES (inline their value) and PASSTHROUGH_TYPES
    (walk back to whatever feeds their own input)."""
    if _seen is None:
        _seen = set()
    if node_id in _seen:
        raise ValueError(f"Cycle detected resolving node {node_id} (bypass/primitive chain)")
    _seen = _seen | {node_id}

    node = nodes_by_id[node_id]
    ntype = node["type"]

    if ntype in PRIMITIVE_TYPES:
        wv_named = node.get("widgets_values_named", {})
        if "value" in wv_named:
            value = wv_named["value"]
        elif node.get("widgets_values"):
            value = node["widgets_values"][0]
        else:
            raise ValueError(f"Primitive node {node_id} ({ntype}) has no resolvable value")
        return ("literal", value)

    if ntype in PASSTHROUGH_TYPES:
        inputs = node.get("inputs", [])
        if len(inputs) != 1:
            raise ValueError(
                f"Passthrough node {node_id} ({ntype}) expected exactly 1 input, got {len(inputs)}"
            )
        link_id = inputs[0].get("link")
        if link_id is None:
            raise ValueError(f"Passthrough node {node_id} ({ntype}) input is unconnected")
        link = links_by_id[link_id]
        origin_node_id, origin_slot = link[1], link[2]
        return resolve_output(nodes_by_id, links_by_id, origin_node_id, origin_slot, _seen)

    return ("ref", node_id, slot)


def convert(workflow: dict) -> dict:
    nodes_by_id, links_by_id = build_indexes(workflow)
    prompt = {}

    for node in workflow["nodes"]:
        node_id = node["id"]
        ntype = node["type"]

        if ntype in DROP_TYPES or ntype in PASSTHROUGH_TYPES or ntype in PRIMITIVE_TYPES:
            continue

        inputs_out = {}
        for inp in node.get("inputs", []):
            name = inp["name"]
            slot_type = inp.get("type")

            if slot_type in SKIP_INPUT_SLOT_TYPES:
                continue

            link_id = inp.get("link")
            if link_id is not None:
                link = links_by_id[link_id]
                origin_node_id, origin_slot = link[1], link[2]
                kind, *rest = resolve_output(nodes_by_id, links_by_id, origin_node_id, origin_slot)
                if kind == "literal":
                    inputs_out[name] = rest[0]
                else:
                    _, ref_node_id, ref_slot = kind, *rest  # noqa: F841
                    inputs_out[name] = [str(rest[0]), rest[1]]
                continue

            widget = inp.get("widget")
            if widget is not None:
                widget_name = widget["name"]
                wv_named = node.get("widgets_values_named", {})
                if widget_name not in wv_named:
                    # No value recorded for this widget-backed slot; leave
                    # it out rather than guess -- ComfyUI will fall back to
                    # the node's own declared default, or node_errors will
                    # name it explicitly so it can be fixed.
                    continue

                value = wv_named[widget_name]

                # SaveAudioAdvanced.format is COMFY_DYNAMICCOMBO_V3 --
                # execute() expects a dict, not a bare string.
                if slot_type == "COMFY_DYNAMICCOMBO_V3":
                    wrapped = {widget_name: value}
                    if "quality" in wv_named:
                        wrapped["quality"] = wv_named["quality"]
                    value = wrapped

                inputs_out[name] = value
                continue

            # No link, no widget: unconnected optional socket-only input
            # (e.g. MathExpression's unused "b"/"c"). Omit it.

        prompt[str(node_id)] = {
            "class_type": ntype,
            "inputs": inputs_out,
            "_meta": {"title": node.get("title", ntype)},
        }

    _patch_known_quirks(prompt)
    return prompt


def _patch_known_quirks(prompt: dict):
    """Fix issues that are NOT conversion/mapping bugs but genuine
    footguns in the saved source workflow's default state, which would
    reproduce identically if the original graph were queued as-saved in
    the ComfyUI frontend.

    LoadAudioFromYouTube.VALIDATE_INPUTS() unconditionally rejects an
    empty "url" (see custom_nodes/ComfyUI-YouTube-Reference-Audio/nodes.py)
    even though this node sits behind ComfySwitchNode's lazy on_true input,
    gated off by default (USE_YOUTUBE=false). ComfyUI's static /prompt
    validation runs VALIDATE_INPUTS on every node in the submitted graph
    regardless of whether a lazy branch will actually execute, so the
    empty default URL fails validation before execution ever starts. Since
    the branch is genuinely never executed with the default switch state,
    substituting a syntactically-valid placeholder URL here is safe (it is
    never fetched) and lets the base template validate/queue cleanly out
    of the box. The Music app's generation.py is expected to overwrite
    this with a real URL whenever a caller actually requests a YouTube
    reference.
    """
    for node_id, node in prompt.items():
        if node["class_type"] == "LoadAudioFromYouTube":
            if not (node["inputs"].get("url") or "").strip():
                node["inputs"]["url"] = "https://youtu.be/placeholder-unused-reference"


def wait_for_comfyui(max_wait_seconds=0, poll_interval=15) -> bool:
    """Return True once /system_stats responds. If max_wait_seconds is 0,
    checks exactly once and returns immediately."""
    deadline = time.time() + max_wait_seconds
    while True:
        try:
            with urllib.request.urlopen(f"{COMFY_BASE}/system_stats", timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        if time.time() >= deadline:
            return False
        time.sleep(poll_interval)


def post_prompt(prompt: dict):
    client_id = str(uuid.uuid4())
    payload = json.dumps({"prompt": prompt, "client_id": client_id}).encode("utf-8")
    req = urllib.request.Request(
        f"{COMFY_BASE}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, body
    except urllib.error.HTTPError as e:
        body_raw = e.read().decode("utf-8")
        try:
            body = json.loads(body_raw)
        except json.JSONDecodeError:
            body = {"raw_error": body_raw}
        return e.code, body


def cancel_queued_job(prompt_id: str):
    """Best-effort: interrupt current execution and delete the queued
    prompt so verification doesn't burn GPU time rendering a full song."""
    try:
        req = urllib.request.Request(f"{COMFY_BASE}/interrupt", data=b"", method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass
    try:
        payload = json.dumps({"delete": [prompt_id]}).encode("utf-8")
        req = urllib.request.Request(
            f"{COMFY_BASE}/queue",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def main():
    print(f"Loading source workflow: {SOURCE_WORKFLOW}")
    workflow = load_workflow(SOURCE_WORKFLOW)

    print("Converting frontend graph -> API prompt dict...")
    prompt = convert(workflow)
    print(f"Converted {len(prompt)} nodes.")

    print("Checking ComfyUI reachability at", COMFY_BASE)
    if not wait_for_comfyui(max_wait_seconds=0):
        print(
            "ComfyUI is not reachable right now (connection refused). "
            "Waiting up to ~10 minutes, polling every 15s "
            "(model loading can take several minutes on first load)..."
        )
        if not wait_for_comfyui(max_wait_seconds=600, poll_interval=15):
            print("BLOCKED: ComfyUI never became reachable at", COMFY_BASE)
            print("Not writing an unverified output file. See NOTES.md guidance.")
            sys.exit(1)

    print("ComfyUI is reachable. Starting verification loop.")

    attempt = 0
    last_error = None
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        print(f"\n--- Attempt {attempt}/{MAX_ATTEMPTS} ---")
        status, body = post_prompt(prompt)
        print(f"HTTP {status}")

        node_errors = body.get("node_errors") if isinstance(body, dict) else None

        if status == 200 and not node_errors:
            prompt_id = body.get("prompt_id")
            print(f"SUCCESS: prompt accepted and queued (prompt_id={prompt_id}).")
            if prompt_id:
                print("Cancelling/interrupting the queued job (verification only, not a full render)...")
                cancel_queued_job(prompt_id)

            OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
                json.dump(prompt, f, indent=2)
            print(f"Wrote validated output: {OUTPUT_PATH}")
            print(f"\nVerification succeeded on attempt {attempt}/{MAX_ATTEMPTS}.")
            sys.exit(0)

        print("FAILED. Response body:")
        print(json.dumps(body, indent=2)[:4000])
        last_error = body
        # ComfyUI can still enqueue a prompt even when node_errors is
        # non-empty (it will simply error at execution time when it's
        # dequeued) -- clean that up immediately so it doesn't sit in
        # (and error out in) a shared ComfyUI instance's queue.
        failed_prompt_id = body.get("prompt_id") if isinstance(body, dict) else None
        if failed_prompt_id:
            cancel_queued_job(failed_prompt_id)
        # Do not auto-patch blindly -- report and stop so a human/agent can
        # fix the specific mapping named in node_errors. (This script is
        # meant to be re-run after a source edit, not to guess-and-loop.)
        break

    print("\n=== Verification did not succeed. ===")
    print(f"Attempts made: {attempt}/{MAX_ATTEMPTS}")
    print("Last error response:")
    print(json.dumps(last_error, indent=2) if last_error is not None else "(none)")
    print("\nNo output file written -- refusing to ship an unverified conversion.")
    sys.exit(1)


if __name__ == "__main__":
    main()
