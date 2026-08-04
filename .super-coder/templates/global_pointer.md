# super-coder — Universal Boot Pointer

This file is loaded by the harness for every invocation. It deliberately
carries no operating content.

Shells boot via `make dos-e` (`make enter`) from a substrate clone. The
launcher reads live engine state and renders a per-shell boot document in
`<substrate>/.sc-worktrees/<shortname>/`.

If no per-shell boot document was loaded alongside this file, the session was
started without the launcher. Stop and run `make dos-e` from the substrate
clone. There is no fallback path, with one exception:

**Repair mode.** If the operator explicitly states that the engine is down for
repair (for example, a broken migration during update or a launcher failure),
proceed without a boot document as a maintenance session, not a shell. The
engine lives under `<substrate>/.super-coder/`. Its live `shell_db.db` is a
gitignored cache rebuilt from `schema.sql`, `migrations/`, and
`.sc-state/content.sql`; those tracked files are the source of truth. Do not
write memory, seed, or identity. Fix only what the operator points at, then
hand back to the normal boot path.
