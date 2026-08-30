# super-coder — Universal Boot Pointer

This file is loaded by the harness for every invocation. It deliberately
carries no operating content.

Shells boot via `make dos-e` (`make enter`) from a substrate clone. The
launcher reads live engine state and renders a per-shell boot document in
`<substrate>/.sc-worktrees/<shortname>/`.

If no per-shell boot document was loaded alongside this file, the session was
started without the launcher. Stop and run `make dos-e` from the substrate
clone. There is no fallback path, with one exception:

**Admin-only repair mode.** Proceed without a boot document only when the
operator explicitly assigns an engine-down repair to the Admin maintenance
seat. Load the Admin maintenance skills, change only the named fault, and hand
back to the normal boot path. Every non-Admin invocation stops here; missing
API service never grants direct control-plane access.
