---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Harness refresh on restart
roadmap_status: shipped
frozen: false
title: Harness refresh on restart
tags: [harnesses, docker, codex, lifecycle]
date: 2026-07-30
project: super-coder
purpose: Current image-owned harnesses
---

# Harness refresh on restart

## Objective

A normal `sc restart` installs current harness releases into the sandbox image,
and a running sandbox always executes image-owned harness binaries even while
host credentials and durable harness state are mounted. The build is complete
when an older host Codex package cannot mask the baked Codex release and the
restart tests prove a fresh harness-layer cache key is used before Docker build.

## Prior decisions

No active decision constrains harness refresh behavior. Preserve the established
boundary that harness executables belong to the Linux image while host mounts
carry authentication and durable harness state. Preserve `restart --no-build`
as the explicit path that reuses the current image without network refresh.

## Construction

1. Relocate the Codex executable resolved by the official installer into an
   image-owned path outside `~/.codex`, then repoint the image launcher to it.
2. Change normal restart preflight to roll a unique harness epoch before
   building. Do not roll under `--no-build`; failed builds must still occur
   before teardown.
3. Update harness freshness documentation and command help to state that normal
   restart refreshes harnesses and `--no-build` pins the existing image.
4. Add regression coverage for Codex target isolation, restart epoch ordering,
   unique refresh tokens, and the `--no-build` escape hatch.
5. Build the real image and compare Codex resolution with and without an older
   host `~/.codex` mount.

## Order

Codex relocation and restart epoch mechanics are parallelizable after the
current installer/mount contract is pinned. Documentation follows the final
behavior. Focused tests precede the real Docker build; the Docker proof precedes
the repository-wide finish gate.

## Risks

- A copied Codex executable could depend on sibling release files. Prove the
  relocated binary directly with `codex --version` before accepting it.
- Always-refresh restart adds network/build latency and can fail before a
  restart. Preserve the existing preflight order so a failed build never tears
  down the healthy container, and retain `--no-build`.
- A date-only epoch does not guarantee refresh on repeated same-day restarts.
  Use a unique UTC token for explicit refreshes while keeping plain launch/build
  cache-warm.
- Selectively mounting Codex state would omit evolving runtime files. Keep the
  full `~/.codex` mount and isolate only the executable.

## Verification gate

- Focused lifecycle and harness tests pass.
- A real rebuilt image reports the refreshed Codex version.
- With an older host `~/.codex` mounted, the container resolves the same baked
  executable and version.
- `restart --no-build` neither rolls the epoch nor builds.
- `sc render-check`, `sc verify`, and `git diff --check` pass before PR.
