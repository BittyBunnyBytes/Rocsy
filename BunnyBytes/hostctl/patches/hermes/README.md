# Rocsy Addons patch for Hermes

`rocsy-addons.patch` is the saved diff of the 13 local edits in the Rocsy
Hermes checkout, based on `a12c3417c82d9495869962e320b4e73a907e849d`
(October 11, 2026) and verified against stable v0.22.2 (`d923a5eb`) on
October 18, 2026. The patch applies only to the separate `~/AI/hermes-rocsy`
source checkout used to build the Rocsy container.
It adds API run reasoning and terminal events, changes goal handling to allow
unlimited turns, and includes the corresponding CLI, TUI, and test changes.
It also adds page screenshots after successful browser navigate/click/back
calls (shown in the Rocsy chat), and the Dockerfile layer with the VA-API
media drivers and the Manager-managed yt-dlp link.

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
