---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: B7 — Engine/Fork Separation & Update Lifecycle
roadmap_status: shipped
frozen: false
title: B7 — Engine / Fork Separation & Update Lifecycle
tags: [super-coder, spec, B7, fork, engine, update, rollback, migrations, lifecycle]
date: 2026-06-07
project: super-coder
purpose: Make a fork treat the engine as a gitignored downstream dependency and its DB as the one preserved artifact — so shells stop confusing the substrate for the project, and updates become snapshot→migrate with a sound rollback.
---

# B7 — Engine / Fork Separation & Update Lifecycle

> Stage spec for a new pillar. Forks are **downstream variants**: all change
> flows *down* from the super-coder source repo, nothing flows back up. The only
> per-fork artifact that must survive is the fork's **DB (its memory)**. This
> spec stops a fork's shells from mistaking the engine for the project, and
> defines a snapshot→migrate→rollback update lifecycle around the DB.

## Overview

Shells in a fork keep referencing the super-coder engine's DB and `schema.sql`
instead of the host repo they are meant to build — they confuse the **substrate
they run on** with the **project they work on**. A QAQC of the boot + git
surfaces found the cause is *not* where the boot artifact renders (it already
renders at the host root and boots with `cwd` = host root). It is two things:

1. **The engine is committed into the host repo.** A fork vendors ~60+ engine
   files under `.super-coder/` into its own git history. `git status` / `git log`
   / `git diff` — a shell's primary "what am I working on" signal — surface
   engine churn mixed with project work, and the engine tree (`schema.sql`,
   `run.py`, `api/`, `ui/`) reads as a codebase to edit.
2. **Nothing draws the project/engine line.** The boot doc leads with
   engine-memory pointers (`.super-coder/shell_db.db`, `schema.sql`) and never
   says, on the always-loaded path, "this repo is your project; `.super-coder/`
   is the engine you run on, not your code."

The fix follows from the downstream-variant model. If updates only ever flow
down and nothing flows up, the engine is a **dependency**, not fork-owned
source — so it should be **gitignored and materialized from upstream**, exactly
like `node_modules/` or `.venv/`. That single move also *is* the conceptual fix:
a shell treats a gitignored `.super-coder/` as not-its-project automatically,
the same way it already ignores `node_modules/`. The one thing a fork must
preserve — its DB — is then handled by an explicit `make update` (snapshot →
migrate) and `make rollback` (restore) lifecycle.

## Goals

- A fork's git surfaces show **only the project**, never engine churn.
- The engine is a **materialized dependency** pinned to an upstream ref, not
  vendored source in the fork's history.
- The fork's **DB/memory is the one preserved artifact**, with a durable
  git-tracked serialization and an ephemeral pre-update restore point.
- `make update` = snapshot → migrate, automatically and safely (it largely
  exists already in `./sc update`).
- `make rollback` = a **sound** undo of a bad update — restoring the DB *and*
  the engine version together, because engine code is read live.
- One always-loaded line in the boot doc that names the project/engine split.

## Non-goals

- Per-step down-migrations (reversible `up()`/`down()` SQL). Rollback is a
  whole-restore, not a schema-reversal — see *Decisions settled*.
- Any upstream flow from forks (PRs, contributions back to super-coder). Forks
  are variants; engine changes are authored in the source repo only.
- A submodule for the engine (evaluated and dropped — see *Decisions settled*).
- Changing where the boot artifact renders or what `cwd` a shell boots in —
  both are already correct.

## Background — the current chain

What already works and must be preserved:

- **Boot artifact location.** `CLAUDE.md` / `AGENTS.md` / `opencode.json` render
  at the **host repo root** and are already gitignored. `./sc boot` execs the
  harness with `cwd` = repo root (`-w "$here"`). The engine lives in
  `.super-coder/`.
- **DB is derived, text is canonical.** `.super-coder/shell_db.db` is gitignored
  and rebuilt from `schema.sql` + `migrations/` + `snapshot/content.sql`.
  `snapshot/content.sql` (per-instance tables dumped by `./sc snapshot`) is the
  git-tracked source of truth for a fork's memory.
- **Update is already snapshot-then-migrate.** `./sc update` (update.py):
  (1) fetch + `git checkout super-coder/<branch> -- <ENGINE_PATHS>`,
  (2) `rebuild.backup_existing()` → timestamped binary copy
  `shell_db.prerebuild.<ts>.db` (restore point),
  (3) migrate **in place**, ledger-tracked (only un-applied migrations; all rows
  preserved), (4) sync skills catalogue (id-stable UPSERT), (5) re-grant common
  skills, (6) wire hooks + map + snapshot.
- **Migrations are the propagation channel.** New skills, boot-render changes,
  schema changes all arrive as migrations + a skills-seed sync.

What is wrong:

- The engine is **git-tracked in the fork** (the update channel is implemented as
  a `git checkout` into the same tree, which assumes tracked paths). This is the
  root of the confusion and the history pollution.
- **Per-fork state lives *inside* the engine dir** — `snapshot/content.sql` (the
  DB source of truth) and `map.config.json` (per-fork cartographer tuning) sit
  under `.super-coder/`. They only "need the engine tracked" because of where
  they happen to live.

## Design

### 1. Mental model: the engine is a dependency

`.super-coder/` is a **materialized runtime dependency**, not fork source. It is
fetched from the super-coder upstream at install, refreshed on update, and never
committed to the fork. The fork commits *what it owns* — its project, plus its
own memory serialization and a pin recording which engine version it runs.

### 2. Relocate per-fork state out of the engine dir

Move fork-owned, must-persist state out of `.super-coder/` into a host-tracked
state dir (proposed `.sc-state/`):

| Moves to `.sc-state/` (git-tracked) | Stays in `.super-coder/` (gitignored) |
|---|---|
| `content.sql` — the DB serialization (memory) | `shell_db.db*` — live DB (derived) |
| `map.config.json` — per-fork map tuning | `schema.sql`, `migrations/`, `scripts/`, `api/`, `ui/`, `templates/`, `adapters/`, `render/`, `hooks/`, `assets/skills/` — engine |
| `engine.ref` — the engine version pin (new) | `instance.json` — derived ports (already gitignored) |

This relocation is the one real cost, and it is **required by any** engine/fork
separation (submodule or gitignore alike): a fork's memory cannot live inside a
dir that is replaced wholesale from upstream. Path references update in
`run.py`, `rebuild.py`, `snapshot.py`, `update.py` (and anywhere else
`snapshot/content.sql` / `map.config.json` are named).

### 3. The engine pin — `.sc-state/engine.ref`

A tiny tracked file recording the upstream commit SHA the fork is materialized
at. Written by `make update` after a successful fetch. It is (a) the fork's
version record and (b) the engine half of a sound rollback (§6). One SHA, not a
vendored tree.

### 4. Gitignore the engine

Add `/.super-coder/` to the fork's `.gitignore` (keeping the existing explicit
ignores for the live DB redundant-but-harmless). The tracked bootstrap surface
the fork keeps is only: `sc` (the dispatcher), the `Makefile`, `.gitignore`, and
`.sc-state/` (memory + pin). `./sc install` materializes `.super-coder/` from
upstream on a fresh clone; `make update` keeps it current.

### 5. `make update` — snapshot → migrate (mostly exists)

`make update` → `./sc update`, rewritten so the engine arrives by **fetch +
materialize into the gitignored dir** (copy from a fetched ref) instead of
`git checkout -- <paths>` + commit. Flow:

1. Capture a **restore point** (§6): `backup_existing()` DB copy **+** record the
   *current* `engine.ref` as `engine.ref.prev`.
2. Fetch upstream; materialize `.super-coder/` at the new ref; write the new
   `engine.ref`.
3. Migrate in place (ledger-tracked) — unchanged.
4. Sync skills, re-grant, map, snapshot — unchanged.

No commit step for the engine (it is ignored). The fork commits only `.sc-state/`
changes (refreshed `content.sql` + bumped `engine.ref`).

### 6. `make rollback` — a *sound* pair-restore (new)

The subtlety: **engine code is read live every session**, and a migration exists
*because new code expects the new schema*. Restoring only the DB would leave new
engine code running against the old schema. So a restore point is a **pair** and
rollback restores both:

`make rollback` → `./sc rollback`:

1. Back up the *current* (post-bad-update) DB first, so rollback is itself
   reversible — you can never lose state by rolling back.
2. Restore the DB from the most recent `shell_db.prerebuild.<ts>.db`.
3. Re-materialize the engine at `engine.ref.prev`; restore `engine.ref`.
4. Checkpoint/clear `-wal`/`-shm`.

Whole-restore, not down-migration. The only data lost is anything written
*between* the update and the rollback — a seconds-wide window in practice
("migrated, it broke, rolled back").

### 7. Boot doc — the always-loaded project/engine line

One short block near the top of `templates/boot.md` (a render-chain edit, gated
on owner OK):

> **Your project is this repo** — everything except `.super-coder/`.
> `.super-coder/` is the **engine** you run on (your memory + identity
> substrate), a gitignored dependency — do not treat it as the project or edit
> it. Engine changes are authored upstream in super-coder, never here.

Plus a one-line guardrail in the `git-workflow` skill: operate on the project,
never the engine.

## Change surface (all engine-side, in the super-coder source repo)

- `rebuild.py`, `snapshot.py`, `run.py`, `update.py` — repoint `content.sql` and
  `map.config.json` to `.sc-state/`; add `engine.ref` read/write.
- `update.py` — swap the engine `git checkout` for fetch-and-materialize; capture
  `engine.ref.prev`; write `engine.ref`.
- New `scripts/rollback.py` — pair-restore (DB backup + engine ref).
- `sc` — add `rollback` dispatch; keep `update` (now materialize-based).
- `Makefile` (fork-facing) — `update` and `rollback` targets wrapping `./sc`.
- `.gitignore` (fork template) — ignore `/.super-coder/`.
- `install.py` — materialize `.super-coder/` from upstream on fresh clone;
  initialize `.sc-state/`.
- `templates/boot.md` — project/engine block (**owner-gated render-chain edit**).
- `git-workflow` skill — one-line guardrail.

## Migration & fork reseed

- **State relocation migration**: move `snapshot/content.sql` →
  `.sc-state/content.sql` and `map.config.json` → `.sc-state/map.config.json` on
  first `make update` after this ships; leave a one-release shim that reads the
  old path if the new is absent.
- **Engine untrack**: `git rm -r --cached .super-coder` in a fork once, then the
  new `.gitignore` keeps it out. The working tree is untouched (files stay; only
  git stops tracking them).
- **Proving ground**: land it on the super-coder source repo, then exercise the
  full `make update` / `make rollback` cycle on the **dos-arch** fork before
  calling it done (it carries a legacy substrate too — the sharpest test of "is
  the project clearly the project").

## Decisions settled

- **Gitignore the engine, not submodule it.** Submodule adds ceremony (detached
  HEAD, `--recursive` clones, gitlink churn) and still requires the same state
  relocation. The dependency/gitignore model is simpler *and* self-documents the
  project/engine split.
- **Engine is tracked nowhere in the fork.** The only reasons to track it
  (update channel, memory persistence, version pin, local edits) are each
  satisfied without it: update → fetch/materialize; memory → `.sc-state/`;
  version → `engine.ref`; local edits → not allowed (forks are downstream).
- **Rollback = whole-restore, not down-migrations.** Zero reverse-SQL to author,
  never surprisingly lossy, always works. Down-migrations are a maintenance tax
  and lossy on column drops — overkill for a single-writer fork DB.
- **Rollback restores the (DB + engine) pair.** Because engine code is live,
  DB-only rollback yields new-code-on-old-schema. The `engine.ref.prev` pin is
  what makes rollback sound.
- **Two preservation artifacts, distinct jobs.** `.sc-state/content.sql` (text,
  tracked) = preserve the fork forever (portable, diffable, survives a fresh
  clone). `shell_db.prerebuild.<ts>.db` (binary, ignored) = undo this one update.

## Open questions

- State dir name/location: `.sc-state/` vs folding into an existing tracked dir.
- Backup retention: how many `prerebuild.*.db` to keep before pruning.
- Should `make update` auto-rollback on a migration *failure* (vs leaving it for
  a manual `make rollback`)? Default here is manual; auto-on-failure is a small
  add.
- Fresh-clone bootstrap UX: does `make update` imply `./sc install` when
  `.super-coder/` is absent, or stay separate verbs?

## Out of scope (later)

- Multi-engine / pinned-channel forks (a fork tracking a non-`main` engine line).
- Signed/verified engine fetches.
- A `make doctor` check that the engine ref matches what migrations expect.
