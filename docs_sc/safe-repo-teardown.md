---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Safe repository teardown (dos-remove)
roadmap_status: shipped
frozen: false
title: Safe Repo Teardown
tags: [subfloor, teardown, remove, backup, lifecycle]
date: 2026-07-31
project: super-coder
purpose: Remove an install without losing its engine database
---

# Safe Repo Teardown

## What It Does

`make dos-remove` delegates to `./sc remove` and removes a repository-local
subfloor installation while preserving the host repository and a verified
engine-database backup.

This is different from eject: eject removes engine coupling while preserving
more generated state for continued use. Remove is the terminal uninstall path.

## Operator Flow

Preview the operation:

```bash
make dos-remove ARGS=--dry-run
```

Run interactively:

```bash
make dos-remove
```

The confirmation prompt requires `REMOVE <repo-basename>`. Automation may use:

```bash
make dos-remove ARGS=--yes
```

The command resolves and validates the repository, stops managed jobs and
services, proves the repo-scoped runtime is quiet, creates and verifies the
backup, removes owned integration surfaces, and deletes the engine directory
last.

## Safety Guarantees

- Canonical-path checks refuse unsafe roots, linked worktrees, and dirty
  worktrees.
- Runtime checks stop managed jobs and `./sc down`, then reject remaining
  repo-scoped containers or an active engine API listener.
- A host without Docker is accepted only when there is no configured Postgres
  dependency and the API listener is absent.
- SQLite backup uses the online backup API, verifies integrity, records the
  schema version, computes a SHA-256 digest, and preserves file modes.
- A manifest records the source repository, engine reference, backup result,
  removed surfaces, and final completion or partial-failure status.
- Failures before backup verification do not begin destructive cleanup.

## Removed and Preserved

The remover deletes subfloor-owned surfaces: `.super-coder`, the `sc`
entrypoint, managed worktrees and local state outside the backup subtree,
generated engine documents and configuration, managed hooks and remotes,
Makefile integration, and managed ignore rules.

It preserves project files, branches, unrelated hooks/remotes/configuration,
user-owned shared files, harness credentials and images, external application
databases, and:

```text
.sc-state/db_backups/removal/<timestamp>/
```

The backup subtree remains gitignored after removal. Its manifest is the
authoritative record of what happened.

## Failure and Recovery

If cleanup fails after a verified backup, the command stops, retains the
backup, and marks the manifest as partial with the completed and pending
operations. Re-running is safe after resolving the reported condition.

Recovery is manual: reinstall the engine version named by the manifest, keep
the runtime stopped, then restore the recorded SQLite backup before launch.
The remove command does not automatically restore data or delete external
application databases.
