---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Safe repository teardown (dos-remove)
roadmap_status: shipped
frozen: true
title: Safe Repo Teardown — dos-remove
tags: [super-coder, spec, teardown, remove, backup, lifecycle]
date: 2026-07-31
project: super-coder
purpose: Remove subfloor, preserve its DB
---

# Safe Repo Teardown — dos-remove

## Objective

Add `make dos-remove` as the safe, one-way uninstall for a fork: it stops every
repo-scoped subfloor runtime, writes and verifies a WAL-safe copy of the engine
database inside the repo's gitignored backup subtree, removes subfloor-owned
installation surfaces, and leaves the host repository and its content intact.

Done means a successful run leaves no runnable subfloor installation in the
repo. The only intentional subfloor residue is the verified removal backup
under `.sc-state/db_backups/` and the one `.gitignore` rule that keeps that
backup out of Git.

> [!class4]
> `remove` is not `eject`. `./sc eject` keeps subfloor running and converts the
> engine into fork-owned source. `./sc remove` stops subfloor and removes it.

## Command Contract

Public surfaces:

```text
make dos-remove
make dos-remove ARGS="--dry-run"
make dos-remove ARGS="--yes"

./sc remove
./sc remove --dry-run
./sc remove --yes
```

- `make dos-remove` is long-form only and delegates to `./sc remove`.
- Default mode prints the canonical repo root, backup destination, running
  services, registered shell worktrees, engine drift, and removal categories,
  then requires the exact phrase `REMOVE <repo-basename>`.
- `--yes` skips only the prompt. It never bypasses path, dirty-worktree,
  shutdown, backup, integrity, or ownership checks.
- `--dry-run` is read-only and prints the same plan and preserved paths without
  stopping or deleting anything.
- Unknown arguments exit `2`. Aborted or failed safety gates exit nonzero.
- The source repo is never a valid target. Reuse the canonical
  `install.is_source_repo()` guard and refuse before mutation.
- Resolve the target from the installed engine and Git top-level. They must
  agree. Refuse `/`, a home directory, a linked shell worktree, or any target
  outside the canonical host repo.
- The command does not commit, stage, reset, or push. It ends with the exact
  `git status` changes the operator should review and commit.

## Safety Model

The removal boundary is the installation in this repo, not the whole machine.
Remove repo-scoped containers, sidecars, brokers, worktrees, generated files,
Git integration, and upstream wiring. Preserve machine-shared harness binaries,
credentials, Docker images, unrelated system services, Git branches, project
source, and external product databases.

Before confirmation, perform only read-only discovery:

- identify the canonical host repo and reject the source repo;
- inventory repo-scoped runtime resources using the same identifiers as
  `./sc down`;
- enumerate registered worktrees rooted under `.sc-worktrees/`;
- refuse if any removable worktree has uncommitted or untracked content;
- report engine-manifest drift and locally added engine files as data that will
  be removed;
- classify every host integration as owned, surgical, conditional, or
  preserved.

There is no v1 force flag. Dirty worktree content and an unprovable target are
operator work to resolve, never data for the remover to discard.

After confirmation, create and write-probe the fixed repo-local backup parent
before stopping anything:

```text
.sc-state/db_backups/removal/<UTC-compact-timestamp>/
```

Ensure `/.sc-state/db_backups/` is present in `.gitignore` before creating the
backup. Do not use the normal override/home/fallback destination selection:
removal backups must stay visibly attached to the repo being dismantled.

## Teardown Flow

```linear
Discover and gate :::class1 -> Confirm exact repo :::class2 -> Probe backup path :::class2 -> Quiesce runtime :::class4 -> Back up and verify DB :::class3 -> Remove owned surfaces :::class4 -> Prove teardown :::class3
```

1. Run discovery and all fail-closed gates. `--dry-run` exits here.
2. Confirm the exact repo. Ensure and probe the removal backup directory.
3. Quiesce through a shared runtime-down implementation, not duplicated shell
   commands. Stop/remove the repo container, configured PostgreSQL sidecar,
   repo-scoped VM/Tailscale/PM2/DB brokers, legacy watcher, durable local jobs,
   and broker-owned conversation children. Assert each known resource is gone.
   If shutdown cannot be proved, abort without deleting installation files.
4. Remove clean registered worktrees beneath `.sc-worktrees/` with
   `git worktree remove`; preserve their branch refs, then prune stale worktree
   metadata. A worktree that becomes dirty after discovery aborts this phase.
5. With writers stopped, back up `.super-coder/shell_db.db` using SQLite's
   online-backup API so committed WAL pages are included. Never use a plain
   file copy.
6. Verify the backup before deleting the live DB: open it read-only, require
   `PRAGMA integrity_check` to return `ok`, record its byte size and SHA-256,
   and confirm the source and backup agree on `PRAGMA user_version`.
7. Write `manifest.json` beside `shell_db.db` with format version, UTC time,
   canonical repo path, engine ref, source DB path, size, SHA-256, SQLite user
   version, integrity result, and removal status. Directory mode is `0700`;
   backup files are `0600`.
8. If no live DB exists, allow teardown of an incomplete installation only
   after the confirmation has named that fact. Write a manifest with
   `database: null`; never claim that a DB was backed up.
9. Remove the explicit owned surfaces and surgically unwind shared host files.
   Delete `.super-coder/` and `sc` last so the loaded remover can finish and
   every fallible engine-backed step has already passed.
10. Re-scan the repo and write the final preserved/removed/error inventory into
    the backup manifest. Print the backup path and recovery note even when file
    cleanup is partial.

## Ownership Matrix

| Class | Surface | Teardown behavior |
|---|---|---|
| Owned | `.super-coder/`, `.sc-worktrees/`, `.sc-state/*` except `db_backups/` | Remove after backup and clean-worktree gate |
| Owned | `sc`, generated boot/config/render artifacts | Remove explicit known paths; never glob outside the repo |
| Surgical | host `Makefile` | Remove only the alias include and installer comment block; delete the file only when it exactly matches the installer-created template |
| Surgical | host `.gitignore` | Remove known subfloor rules/comments, but retain `/.sc-state/db_backups/` under a removal-backup comment |
| Surgical | Git `core.hooksPath` | Unset only when its resolved value equals this install's `.super-coder/hooks`; preserve any other hook owner |
| Surgical | engine upstream remote | Remove only the recognized non-`origin` subfloor/super-coder remote; preserve every unrelated remote |
| Conditional | `.github/workflows/subfloor-visual-qa.yml` | Remove only with the managed-by header; otherwise preserve and report it as host-owned |
| Conditional | `.claude/skills/`, `shared/`, `shared/redlines/` | Remove engine-rendered files and exact empty scaffolds; preserve unknown or user content |
| Preserved | `.git/`, project files, branches, host Make targets/rules | Never delete or rewrite beyond the surgical edits above |
| Preserved | `.sc-state/db_backups/**` | Keep all prior backups plus the new removal backup; removal never prunes |
| Preserved | host harness installs, credentials, shared Docker image/cache | Machine-scoped and outside this repo teardown |

Known generated paths include `CLAUDE.md`, `AGENTS.md`, `opencode.json`,
`.claude/settings.local.json`, `.codex/hooks.json`, `roadmap_sc.md`, `docs_sc/`,
`specs_sc/`, and `skills_sc/`. The implementation must keep this inventory in
one ownership definition used by install/update/remove tests. Unknown files
inside mixed host directories are preserved and named in the final report.

## Failure Behavior

- Backup destination unavailable: abort before runtime shutdown.
- Runtime shutdown incomplete: abort before worktree or file removal.
- Dirty worktree: abort before confirmation and mutation.
- Database backup or integrity verification fails: keep the stopped but intact
  installation, print `./sc launch` as the recovery action, and delete no live
  DB or engine files.
- Git config, remote, Makefile, or `.gitignore` surgery cannot be completed:
  stop before deleting `.super-coder/`; keep the verified backup and print the
  exact unresolved surface.
- File removal fails after the verified backup: mark the manifest `partial`,
  list every remaining path, exit nonzero, and never delete the backup.
- A symlinked removal target is unlinked as a symlink only. The remover never
  follows it recursively and never operates on a resolved path outside the
  canonical repo.
- Signals during the destructive phase leave the backup intact. Signal handlers
  update the manifest to `partial` when possible and print its location.

## Implementation Surface

- New `.super-coder/scripts/remove.py`: discovery, confirmation, backup
  manifest, ownership plan, teardown orchestration, and final proof.
- `.super-coder/scripts/db_backup.py`: expose verified backup metadata without
  changing existing update/restart destination selection or five-backup
  retention. Removal uses an explicit destination and no pruning.
- `sc`: dispatch `remove`; refactor the existing `down` sequence into a reusable
  command/helper with a machine-checkable completion result.
- `.super-coder/aliases.mk`: add `dos-remove`, help text, and `.PHONY` coverage.
- `.super-coder/scripts/install.py`: centralize the install-owned path and
  marker inventory so install/update/remove cannot drift.
- Tests: add focused unit tests plus one temporary-Git-repo end-to-end teardown
  fixture.
- User docs: distinguish `remove` from `eject`, state the repo-only boundary,
  and document the preserved backup and manual reinstall/restore sequence.

## Construction Plan

1. Centralize the owned/surgical/conditional inventory and implement read-only
   discovery plus `--dry-run`.
2. Extract a reusable, verifiable repo-runtime shutdown path and add the clean
   worktree gate.
3. Implement the fixed local removal backup, integrity verification, manifest,
   permissions, no-DB case, and no-pruning rule.
4. Implement host-integration surgery and explicit-path deletion, with engine
   and dispatcher deleted last.
5. Wire `./sc remove`, `make dos-remove`, and command help.
6. Add regression and end-to-end tests. Documentation may proceed in parallel
   once the command contract and output are stable.

Dependencies are `1 -> 2 -> 3 -> 4 -> 5 -> 6`; help/docs can run in parallel
with test completion after step 5.

## Verification Gate

The feature fails its gate unless all of these pass:

- A live WAL-mode fixture with an uncheckpointed committed row survives in the
  removal backup, passes integrity check, and matches the manifest hash.
- Backup-path permission failure leaves the runtime and installation untouched.
- Runtime-down failure leaves app files and the live DB untouched.
- Dirty and newly-dirtied worktrees refuse without losing their files; clean
  worktrees are unregistered while their Git branches remain.
- An existing host Makefile, unrelated `.gitignore` lines, custom hook config,
  unrelated remotes, shared files, credentials, and project source survive
  byte-for-byte.
- Installer-created Makefile, managed Visual QA shim, generated artifacts,
  repo-scoped services, worktrees, engine state, `sc`, upstream wiring, and
  alias include are gone.
- Source-repo, linked-worktree, symlink-escape, noninteractive-without-`--yes`,
  missing-DB, repeated timestamp, signal, and partial-cleanup cases have
  deterministic outcomes.
- `git status` shows only expected teardown deletions/surgical edits and nothing
  is staged or committed.
- After success, `make dos-remove` and `./sc remove` no longer exist, while the
  backup remains ignored, readable, integrity-clean, and sufficient to identify
  the matching engine ref for recovery.

## Deliberate Limits

- No uninstall of machine-shared harness CLIs, credentials, Docker images, or
  package-manager payloads.
- No backup or deletion of a target project's external application database.
  This command preserves subfloor's own engine database only.
- No automatic commit, branch deletion, push, remote repository deletion, or
  database restore.
- No force mode that can discard dirty worktree content or bypass backup
  verification.
