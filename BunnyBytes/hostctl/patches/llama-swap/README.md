# Rocsy Addons patch for llama-swap

`rocsy-addons.patch` adds the Rocsy Addons bootstrap script to llama-swap's
`ui/index.html`. The script, parameter editor, save API, and per-model launcher
are maintained in BunnyBytes (`gateway/static/llama-api-nav.js`,
`gateway/static/llama-params.*`, `gateway/llama_params.py`, and
`Llama/llama-launch`). The patched UI is intended to be opened through the
BunnyBytes `/Llama/ui/` gateway, which serves those assets and adds a cache
version. Direct access to port 8400 does not provide the Rocsy Addons APIs.

`hostctl/coreswap.py` checks this patch against each fetched upstream release
before offering a build. A conflict makes the release unavailable until this
patch is updated. The built binary is stored separately and becomes active
only after the user selects it with no model loaded. The built-in upstream
binary remains available for rollback.
