---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# Roadmap

> Rendered from the DB. Status is a planning horizon; a feature's open flags are its blockers.

## Brainstorm

### Fork to sibling repos · owner: `cc`
Fork super-coder into dos-arch / rst-c / emergence / md-converter; reseed pattern.

_No open flags._

## In Progress

### super-coder · owner: `cc`
The substrate itself: data layer we build, harness we rent. v1 targets Claude Code + OpenCode; GUI review layer; fork + reseed.

_No open flags._

## Next

### B1 — First-launch installer · owner: `cc`
Full installer on top of init_fork: requirements check, harness auto-detect, slot-filled shell_system_prompt template.

_No open flags._

### B6 — Commit→PR automation · owner: `cc`
edit→snapshot→render→commit→PR; per-shell-branch concurrency. The snapshot button is the manual precursor.

_No open flags._

### B7 — Engine/Fork Separation & Update Lifecycle · owner: `cc`
Engine becomes a gitignored downstream dependency (materialized from upstream, pinned by engine.ref); fork's DB is the one preserved artifact; update = snapshot→migrate, rollback = sound (DB+engine) pair-restore. Stops shells confusing the substrate for the project. See specs_sc/b7-engine-fork-separation.md.

_No open flags._

## Near Term

### B4 — OpenCode adapter · owner: `cc`
Emit opencode.json + verify the research-flagged items; boot already dual-writes AGENTS.md + SKILL.md.

_No open flags._

### B5 — Onboarding & mapping · owner: `cc`
Base dr_* code map shipped (files/deps/env, ./sc map, surface_catalogue). NEXT — navigation layer (spec authored): dr_section + per-file desc (cartographer-authored, preserved across remap) + a CONNECTIONS block that replaces WORKSPACE. Supersedes the typed-semantic-tables plan. See specs_sc/b5-repo-navigation.md.

_No open flags._

## Shipped

### B0 — Core spine · owner: `cc`
Repo skeleton, schema, migrations, DB rebuild-from-text, render→boot (CLAUDE.md + AGENTS.md). PR #-/a1cc1e2.

_No open flags._

### Dev shell git worktrees · owner: `cc`
Give each dev shell its own git worktree so multiple dev shells can run in parallel without sharing a tree. Reviewer/planner stay on the main tree (read-only on git).

_No open flags._

### B2 — Content & render · owner: `cc`
Flat _sc render, per-shell SKILL.md, skill seed pipeline. PR #1.

_No open flags._

### B3 — Review layer · owner: `cc`
Dependency-free localhost GUI (shells/roadmap/flags), per-fork ports. PR #3.

_No open flags._

### Dev shell live UI preview · owner: `cc`
One router on the fork's dev_port fans out to each dev shell's worktree vite, routed by subdomain (http://<shortname>.localhost:<dev_port>/) — live HMR per worktree, no base-path config, no concurrent-edit conflict. post-commit hook prints the URL. See specs_sc/dev-preview.md.

_No open flags._
