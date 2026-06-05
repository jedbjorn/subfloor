---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: B5 — Onboarding & mapping
roadmap_status: near_term
frozen: false
title: B5 — Repo Navigation — Sections, Descriptions & the CONNECTIONS Block
tags: [super-coder, spec, B5, mapping, navigation, cartographer, render]
date: 2026-06-06
project: super-coder
purpose: Give every shell a cheap, accurate answer to "where does this live?" — a sectioned, described repo map surfaced through a single CONNECTIONS block.
---

# B5 — Repo Navigation: Sections, Descriptions & the CONNECTIONS Block

> Stage spec for **B5 — Onboarding & mapping**. The base `dr_*` map (files /
> deps / env, `./sc map`, the `surface_catalogue` skill) is shipped. This stage
> builds the *navigation layer* on top of it and fixes how the boot doc orients a
> shell about where things live. Supersedes the old "semantic tables
> (api/db/page)" line in the B5 summary.

## Overview

A QAQC of the boot render (render → compose → flat → prompt) surfaced one real
failure mode behind "shells get confused about where things are," plus three
contributing gaps:

1. **`connections` is a dead column.** `schema.sql` defines `shells.connections`;
   `shell_factory` collects it; `compose.py` **never renders it** (it renders
   `workspace` only). Verified empty on every live shell. A column literally
   named "where things live" is stored and shown to no one.
2. **`workspace` and `connections` are the same concept** — "where things live" —
   but only `workspace` is rendered, and it is thin free-text.
3. **The repo map is surfaced as a number, not a pointer.** `compose.py` reads
   `dr_filepath`/`dr_repo` and renders only counts into `## STATUS`. The actual
   "where things live" knowledge sits in `dr_filepath` and is reachable only if
   the shell elects to run `surface_catalogue`. The FIRST-RUN block tells a fresh
   shell *how* to read the map; that nudge vanishes once `bootstrapped=1`, leaving
   a returning shell with a count and no standing instruction.
4. **The docs count is misleading.** `## STATUS` shows `N ingested / M in repo`,
   where `M` counts *all* host-repo markdown (READMEs, PR/issue templates,
   CHANGELOG, and — in substrate-containing forks — embedded substrate assets).
   It can never reconcile with `documents` (only things ingested via `onboard`),
   so it always reads as a false "un-ingested" backlog.

The fix is a single **`## CONNECTIONS`** block that replaces `## WORKSPACE`,
backed by a **sectioned, described** view of the repo map. The shell sees *where
to start* at boot (UI here, API here, docs here) and queries *the leaves* (file
names + descriptions) only inside the one section it picked — one cheap query
deep, never a full preload.

## Goals

- A returning (already-bootstrapped) shell can answer "where does X live?" from
  the boot doc + one scoped query — without grepping blind or reading wrong files.
- One surface for "where things live." No dead column, no redundant pair.
- Per-file descriptions exist and are *curated* (single owner: the cartographer),
  never invented at render time, and survive the auto-remap that working shells
  trigger.
- Boot cost stays bounded and cache-stable; description detail is paid only on
  demand, inside a chosen section.

## Non-goals

- Typed semantic tables (`dr_api` / `dr_db` / `dr_page`). `dr_section` is the
  general navigational layer and replaces that deferred pass. Revisit only if a
  concrete need appears.
- Changing the base mapper's file/dep/env extraction. This stage layers on top.
- Auto-generating descriptions with an ad-hoc LLM call. Descriptions are
  cartographer-authored content, not a render-time inference.

## Background — the current chain

```
DB (shell_db.db, gitignored cache; rebuilt from schema.sql + migrations/ + snapshot/content.sql)
  → compose.py :: compose_boot()   assembles the boot markdown
  → run.py                         dual-writes CLAUDE.md + AGENTS.md, exec's the harness
dr_* map (separate): map_repo.py walks the host repo → dr_filepath / dr_dependency / dr_env / dr_repo
```

Key current facts this stage changes:
- `compose.py` renders `## WORKSPACE` from `shells.workspace`; `connections` is
  never read.
- `map_repo.py` does `DELETE FROM dr_filepath` then re-INSERTs on **every** run,
  and the git `post-checkout` hook runs it automatically (working shells, not the
  cartographer). Any authored column on `dr_filepath` is therefore destroyed on
  the next checkout unless explicitly preserved.
- `dr_filepath` columns today: `path, ext, lang, role, bytes, lines`. No
  description, no section.

## Design

### 1. `## CONNECTIONS` replaces `## WORKSPACE`

One block, three layers, top to bottom:

1. **Derived header** (rendered from facts, never authored):
   - "Need to find something? Look here first." → `surface_catalogue` /
     `dr_filepath`.
   - Repo root + branch + `mapped_at` (from `dr_repo`).
   - Shared-folder path: `<repo_root>/shared` (see §6).
2. **Section index** — the navigational core (from `dr_section`): one line per
   section — `name · location · file-count · one-line description`. This is the
   "UI here / API here / docs here" list.
3. **Authored notes** — the free-text `connections` column: wiring, services,
   deploy facts the map cannot derive ("prod deploys from `main` via CF Pages";
   "email OAuth spans these three files").

`shells.workspace` is dropped. Any existing workspace text migrates into the
authored `connections` notes layer.

**Division of responsibility (single source of truth):** the map owns raw paths;
`connections` notes own *meaning the map cannot derive*. Do not hand-author file
paths into the notes — that re-creates drift the section index already prevents.

### 2. `dr_section` — sectioned navigation (new table)

Authored, stable, small (~10–20 rows). **Not wiped** by the remap.

```sql
CREATE TABLE dr_section (
    section_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,          -- "API", "UI", "Docs", "Schema", …
    path_prefix  TEXT NOT NULL,          -- repo-relative prefix or glob the section covers
    description  TEXT,                    -- one line, what this area is
    sort_order   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name)
);
```

- Files join to a section **by path-prefix at query/render time** — `dr_section`
  stores no file IDs, so it never needs re-stitching when `dr_filepath` is wiped
  and repopulated. A new file the hook adds **auto-falls into its section** by
  path.
- **Seeded** from top-level directories on first map (non-empty index on day
  one); the cartographer renames / merges / re-describes (e.g. seed gives
  `shell_core` → cartographer splits into `API: shell_core/api`, `UI:
  shell_core/ui`).
- **Catch-all:** files matching no section render under an **"other /
  unsectioned"** bucket — never hidden. That bucket is the cartographer's
  worklist ("a new area appeared; section it").

### 3. `dr_filepath.desc` — per-file descriptions

```sql
ALTER TABLE dr_filepath ADD COLUMN desc TEXT;   -- ≤100 chars, cartographer-authored
```

- Authored by the cartographer; `NULL` until described.
- **Never bulk-loaded at boot.** Queried only *within a chosen section*, via
  `surface_catalogue`. The boot doc carries the section index; the descriptions
  are the leaves the shell reaches for after it has narrowed to one section.

### 4. Surviving the auto-remap wipe (the core mechanic)

This is the make-or-break. `map_repo.py` must stop destroying authored content:

- Replace `DELETE FROM dr_filepath` + blind re-INSERT with an **UPSERT keyed by
  `path`** that **preserves `desc`** for paths whose content is unchanged.
- New or changed paths land with `desc = NULL`.
- Paths that disappeared from the repo are deleted (their `desc` goes with them —
  correct).
- `dr_section` is untouched by the mapper (authored content; the cartographer
  owns it). Section membership is recomputed for free by the prefix join.

Result: the automatic hook (working shells) keeps the file list fresh; authored
descriptions and sections persist; the cartographer fills gaps on its own cadence
and **never blocks the hook**.

**Worklists (queryable):**
- Undescribed files: `SELECT path FROM dr_filepath WHERE desc IS NULL ORDER BY role, path;`
- Unsectioned files: files whose path matches no `dr_section.path_prefix`.

### 5. Render: the section index, not the leaves

`compose.py` renders the section index (layer 2 above) — bounded by construction
(~10–20 sections), so boot cost is small and **cache-stable** (changes only when
sections change, not when files churn). Per-file descriptions are deliberately
*not* rendered at boot. The shell flow:

> need a doc → CONNECTIONS shows `Docs → docs_sc/, *.md` → query that section's
> leaves → read one or two descriptions → open the right file.

Knows where to start and what to look for, without loading it all at the top.

### 6. Shared folder

`install` creates `<repo_root>/shared/` (a host-repo scratch/handoff dir). The
CONNECTIONS derived header states the path. Derived by convention (relative to
repo root) — not stored in a column.

### 7. Docs-count fix

Narrow the `## STATUS` docs denominator to *ingestable host-repo docs*:
- Exclude engine paths (already `.super-coder/%`) **and** embedded substrate
  assets in substrate-containing forks.
- Exclude non-ingestable markdown (PR/issue templates, CHANGELOG, license-style
  files) — or reframe the line so it does not imply "ingest all of these."

Goal: the ratio stops reading as a false "un-ingested" backlog.

### 8. Cartographer — two standing jobs

The cartographer flavor + skill gain explicit ownership of:
1. `dr_section` — author/curate sections (seed-refine), keep the catch-all empty.
2. `dr_filepath.desc` — fill descriptions, worklist-driven (`desc IS NULL`).

Both are best-effort and NULL-until-curated; neither blocks the auto-remap hook
that working shells trigger.

## Change surface (all engine-side, in the super-coder source repo)

| File | Change |
|---|---|
| `schema.sql` | add `dr_section`; add `dr_filepath.desc`; drop `shells.workspace` |
| `migrations/NNNN_*.sql` | additive migration for installed forks (new table + column; workspace handled per "Migration" below) |
| `scripts/map_repo.py` | DELETE+INSERT → UPSERT preserving `desc`; seed `dr_section` from top-level dirs |
| `render/compose.py` | `## CONNECTIONS` (header + section index + authored notes); drop `## WORKSPACE`; fix docs-count denominator |
| `scripts/install.py` | create `<repo_root>/shared/` |
| `assets/shells/cartographer.*` / cartographer SKILL | add the two jobs + worklist queries |
| `snapshot/content.sql` | regenerated via `./sc snapshot` (shell content: connections notes, sections) |
| reseed migration | so already-installed forks pick up the schema + cartographer-asset changes |

## Migration & fork reseed

- New table + new column are additive — straightforward forward migration.
- Dropping `shells.workspace`: SQLite requires table-rebuild for a true `DROP
  COLUMN` (or leave the column unused and stop rendering it). Decide at
  implementation time; leaving it unrendered is the lower-risk path and can be
  cleaned up later.
- The cartographer asset/skill edits need a **reseed migration** (the
  asset-content sync path) so existing forks pick up the new behavior on
  `./sc update`, not just fresh installs.
- This is engine work, done in the super-coder source repo — never patched into a
  fork's `.super-coder/` directly (a fork's engine is checkout-scoped on update;
  in-place edits are clobbered or silently diverge).

## Token economics (why this shape)

- The boot doc is the system prompt — **prompt-cached** and stable between map
  changes. A bounded section index is one cheap cache-stable cost.
- Per-file descriptions, if preloaded, would be a large recurring cost (hundreds
  of files × ~25 tokens) and would churn the cache on every auto-remap. Querying
  them inside a chosen section pays detail cost only when needed.
- Wrong-file reads are *uncacheable churn* (fresh tokens every time + polluted
  context). The section index + on-demand descriptions trade a small stable boot
  cost for fewer wrong reads — net win over a session.

## Decisions settled

- **CONNECTIONS replaces WORKSPACE** — one "where things live" surface; the dead
  `connections` column gets a render; `workspace` retired.
- **`dr_section` (general, authored) over typed semantic tables** — defer
  `dr_api`/`dr_db`/`dr_page`.
- **Descriptions are cartographer-authored and preserved across remap** — not
  render-time inference, not wiped by the hook.
- **Render the section index, not the leaves** — boot loads where-to-start;
  descriptions are queried on demand inside a section.
- **Sections are seeded from top-level dirs, then curated** — non-empty on day
  one; catch-all bucket keeps the index complete.

## Open questions

- Exact `dr_section` seeding heuristic (top-level dirs only, or first two levels
  for nested layouts like `shell_core/{api,ui}`?).
- Whether to surface an undescribed/unsectioned count in `## STATUS` to ping the
  cartographer sooner (vs. leaving it as a quiet worklist).
- Whether to true-`DROP` `workspace` (table rebuild) now or leave it unrendered
  and clean up in a later structural pass.

## Out of scope (later)

- Typed semantic tables (`dr_api`/`dr_db`/`dr_page`).
- Semantic descriptions of *symbols* (functions/classes) — this stage is
  file-level.
- Cross-repo sections (multi-repo shells) — current model is one shell, one repo.
