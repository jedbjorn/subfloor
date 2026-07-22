---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# Roadmap

> Rendered from the DB. Status is a planning horizon; a feature's open flags are its blockers.

## Shipped

### B0 — Core spine · owner: `cc`
Repo skeleton, schema, migrations, DB rebuild-from-text, render→boot (CLAUDE.md + AGENTS.md). PR #-/a1cc1e2.

_No open flags._

### Dev shell git worktrees · owner: `cc`
Give each dev shell its own git worktree so multiple dev shells can run in parallel without sharing a tree. Reviewer/planner stay on the main tree (read-only on git).

_No open flags._

### Agents skill — delegated waves · owner: `cc`
New engine skill 'agents' (--agents [model]) for dev + reviewer flavors: delegate spec execution to implementer waves and reviews to adversarial finding-panels. Overlay on spec/review; parent-only memory writes; wave checkpoints as monitoring; parent-set timeouts (two-strike floor); AGENTS spawn ledger with hard 6h validity window as a verbatim guard. See specs_sc/agents-skill.md.

**Blockers:**
- `SC-001` [Docs] sprint eventing shipped (PR #338), feature doc pending — eventing loop PROVEN by sprint 14 (f19 Visual QA CI, 2026-07-20): full event-driven cycle ran end-to-end, zero scheduled polls | Blocker for: eventing feature doc

### Session-surviving job runner (sc job) · owner: `cc`

_No open flags._

### Boot spinner — launch feedback after harness pick · owner: `PLN1`
Interactive ./sc enter|boot goes silent 7-10s between the harness pick and the boot summary (git fetch + gh pr list dominate). Add a TTY-only ASCII spinner with phase labels in style.py, wrapped around the silent region of run.py main(). No headless/CI output change. Spec: specs_sc/boot-spinner.md.

**Blockers:**
- `SC-004` [Docs] Boot spinner PR #437 pending merge; after landing, freeze correction spec 15 and update the feature doc from shipped code | Blocker for: Boot spinner correction record

### Visual QA CI — Playwright viewport screenshots · owner: `PLN1`

**Blockers:**
- Visual QA CI spec (#13) live in DB; git render/snapshot pending FnB GUI Snapshot — sc mem doc pipeline defect filed as subfloor#434 | Blocker for: spec render in specs_sc/
- [Docs] Visual QA CI shipped (PRs #438/#442/#443), feature doc pending (conformance F6) | Blocker for: f19 feature doc

### Sprint model routing catalogue · owner: `DEV3`
Self-healing locally authoritative model routes populated by Refresh models; exact sprint resolver, Kimi model routing, and high-effort launches across supported harnesses.

_No open flags._

### B2 — Content & render · owner: `cc`
Flat _sc render, per-shell SKILL.md, skill seed pipeline. PR #1.

_No open flags._

### B3 — Review layer · owner: `cc`
Dependency-free localhost GUI (shells/roadmap/flags), per-fork ports. PR #3.

_No open flags._

### B7 — Engine/Fork Separation & Update Lifecycle · owner: `cc`
Engine becomes a gitignored downstream dependency (materialized from upstream, pinned by engine.ref); fork's DB is the one preserved artifact; update = snapshot→migrate, rollback = sound (DB+engine) pair-restore. Stops shells confusing the substrate for the project. See specs_sc/b7-engine-fork-separation.md.

_No open flags._

### Dev shell live UI preview · owner: `cc`
One router on the fork's dev_port fans out to each dev shell's worktree vite, routed by subdomain (http://<shortname>.localhost:<dev_port>/) — live HMR per worktree, no base-path config, no concurrent-edit conflict. post-commit hook prints the URL. See specs_sc/dev-preview.md.

_No open flags._

## In Progress

### super-coder · owner: `cc`
The substrate itself: data layer we build, harness we rent. v1 targets Claude Code + OpenCode; GUI review layer; fork + reseed.

_No open flags._

### Sprint eventing — GitHub→inbox daemon + headless worker boot · owner: `cc`

**Blockers:**
- `SC-002` [Docs] sprint eventing shipped (PR #338), feature doc pending — and the loop is unproven until a real sprint runs on it | Blocker for: eventing feature doc + first eventing sprint
- `SC-007` [Docs] Sprint planner session-control spec #20 lives ONLY in the live engine DB — sc mem doc add/edit materializes neither specs_sc/sprint-planner-session-control.md nor a .sc-state/content.sql snapshot (render+snapshot pipeline defect, upstream subfloor#434). 2026-07-22: body materially revised with 6 spec-debt write-backs (retry semantics J2, arming posture validation J5, effort=config-effective-at-launch J4, transition-edge table J1, error-state sc enter retry-first recovery SC-466, F3 watcher re-arm softened). The pending FnB GUI Snapshot must capture CURRENT live-DB state so these write-backs reach git; until then a DB rebuild would lose spec #20 entirely. | Blocker for: reviewable flat render + durable snapshot of feature 14 spec seq 2

### Token & session analytics · owner: `cc`
Self-tracked token spend + session history across all harnesses (claude/opencode/codex/vibe/kimi). Sweep-parse each harness's on-disk usage data into session_token_usage; session lifecycle columns on archives; /api/analytics/* + GUI Analytics tab (7-day paged history with session titles, harness/provider/model filters, sprint clusters, usage analytics). Tokens only, no pricing, v1.

_No open flags._

## Next

### B1 — First-launch installer · owner: `cc`
Full installer on top of init_fork: requirements check, harness auto-detect, slot-filled shell_system_prompt template.

_No open flags._

### Sprint reporting — unit reports, conformance pass, planner synthesis · owner: `cc`
Dev unit-report result rows at merge; pre-freeze conformance pass (review shells judge spec vs main, four-way verdicts); sprint report becomes a fixed skeleton the planner synthesizes from unit reports + conformance doc. Skill-text only — no schema, no CLI. See specs_sc/sprint-reporting.md.

_No open flags._

### B6 — Commit→PR automation · owner: `cc`
edit→snapshot→render→commit→PR; per-shell-branch concurrency. The snapshot button is the manual precursor.

_No open flags._

## Near Term

### B4 — OpenCode adapter · owner: `cc`
Emit opencode.json + verify the research-flagged items; boot already dual-writes AGENTS.md + SKILL.md.

_No open flags._

### B5 — Onboarding & mapping · owner: `cc`
Base dr_* code map shipped (files/deps/env, ./sc map, surface_catalogue). NEXT — navigation layer (spec authored): dr_section + per-file desc (cartographer-authored, preserved across remap) + a CONNECTIONS block that replaces WORKSPACE. Supersedes the typed-semantic-tables plan. See specs_sc/b5-repo-navigation.md.

_No open flags._

## Brainstorm

### Fork to sibling repos · owner: `cc`
Fork super-coder into dos-arch / rst-c / emergence / md-converter; reseed pattern.

_No open flags._
