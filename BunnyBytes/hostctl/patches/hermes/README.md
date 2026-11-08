# Rocsy Addons patch for Hermes

`rocsy-addons.patch` is the saved diff of the 14 local edits in the Rocsy
Hermes checkout, based on stable **v0.23.0** (`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`,
ported November 8, 2026). The patch applies only to the separate `~/AI/hermes-rocsy`
source checkout used to build the Rocsy container.
It adds API run reasoning and terminal events (on both v0.23 run paths: the
canonical admission turn and the direct agent), changes goal handling to allow
unlimited turns, and includes the corresponding CLI, TUI, and test changes.
It copies the page screenshots `browser_exec` takes into the Rocsy workspace
(shown in the Rocsy chat; v0.23 replaced the built-in browser_navigate/click/back
tools with `browser_exec`, so screenshots now come from the agent's own
`capture_screenshot()` calls rather than after every navigation), and the
Dockerfile layer with the VA-API media drivers and the Manager-managed yt-dlp link.

Earlier patches are kept in `previous/<base commit>.patch` (`a12c3417.patch`:
the October 11, 2026 base). A checkout still carrying one shows that in Manager;
**Update** takes it off, moves to the release and puts the current patch on, and
puts the checkout back exactly as it was if any step fails.

Model Manager checks whether the patch is already applied, applies it after a
Hermes stable release update when needed, and builds the Hermes Docker image
only after the patch is present. If a future release conflicts with the patch,
the build stops so the changes can be reviewed and the patch refreshed.

Model Manager has separate Rocsy Hermes and Personal Hermes controls. Rocsy
updates use `~/AI/hermes-rocsy`, reapply this patch, and rebuild its containers.
Personal updates use `~/.hermes/hermes-agent` through the personal launcher;
they never apply this patch or rebuild Rocsy containers. Both check the stable
release channel independently.

If Model Manager detects a Personal Hermes checkout or working terminal launcher
but no Rocsy checkout, it queues one automatic Rocsy install. It clones the patch's pinned
base commit into an independent source directory, checks and applies the patch,
then builds and starts the Rocsy containers. A failed automatic attempt is not
retried repeatedly; its log and a manual retry remain available in Manager.
If neither version exists, Manager's install action runs the upstream personal
installer first, then creates the separate Rocsy checkout. Existing personal
files are never overwritten by the Rocsy creation step. A launcher-only personal
install is detected but cannot be updated by Manager until it has a managed Git
checkout. The Rocsy patch base is newer than the current stable tag; stable
release checks remain independent after both installs are present and only move
forward when a newer compatible stable release exists.

To verify the Rocsy checkout has the patch:

```bash
git -C ~/AI/hermes-rocsy apply --reverse --check rocsy-addons.patch
```
