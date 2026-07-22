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

**Blockers:**
- Engine-tooling hygiene (surfaced in Sprint 25 seq 2): (1) './sc render-check' run from a shell worktree redirects to the MAIN checkout and reports live-system drift instead of the worktree's state — misleading; shells must run the worktree's script directly. Same root cause as './sc' resolving engine code from the main checkout not the caller's worktree (also blocks freshly-merged CLI verbs like 'sc mem task edit' from being callable until reconcile). (2) migrations/0001_seed_skills.sql lags assets for issue_reporting + sprint_orchestration (shipped as deltas 0074/0076 without a 0001 regen) — harmless (freshness guard builds from all migrations), but a './sc seed-skills' regen on main folds them in. Both are engine fixes to author here (we are upstream).
- Stale shell liveness blocks re-boot: a headless './sc run' session can end WITHOUT closing its shell_memory_archives row (ended_at stays NULL); the shell's active_archive_id keeps pointing at the unclosed archive, so the next './sc run' fails 'already has a live session'. Hit on DEV3 (archive 74, the #496-merge session ended ~20:25 but never closed) blocking the seq-5 boot; archive 68 (17:15) also unclosed; DEV4 archive 69 unclosed too. Read-only 'sc sql' + 'sc mem' expose NO session/liveness-clear verb — currently requires an FnB session-kill. Engine fix: (1) reliably close the archive (set ended_at) on headless session exit, and (2) add a self-serve liveness reap/clear command with a staleness timeout. Matches the known live-session-lock stall pattern (L&S).
- DATA LOSS in memory DB — roadmap writes reverted (roadmap #21 render-pipeline edits + the JSON-integrity item lost; a later 'roadmap add' silently reused the freed id — clobber). Decisions #21-23 survived (table-selective). CORRECTION: my first hypothesis (seq-2 snapshot-on-'sc mem doc edit' reverts roadmap) is DISPROVEN by a controlled test — roadmap #21-23 survived a board doc-edit unchanged; that snapshot is PROTECTIVE (dumps live->content.sql). Actual mechanism: a rebuild-from-content.sql (the './sc update' reconcile / any rebuild) reverts any sc mem write not yet snapshotted to content.sql — roadmap writes made between snapshots are lost on rebuild. Two real engine defects: (1) rebuild silently drops un-snapshotted live writes instead of snapshot-first-then-rebuild or failing loud; (2) 'sc mem roadmap add' reuses an existing/freed id (silent clobber) instead of a fresh id or a loud conflict. Both violate the validate-at-write / fail-loud stance (L&S #15). Recovery: #21 re-edited, JSON-integrity re-added at #23; a board doc-edit since has snapshotted them to content.sql. Sprint CODE unaffected (git/PRs).

### Interface chats and interactive planner wake · owner: `cc`
First-class Interface tab and API-owned input broker for one durable tmux-hosted chat per shell, with CLI/API parity, safe clean+idle+3s planner wake, durable sprint events, and local watched-PR polling. Spec #20; brokered PTY streaming and concurrency fidelity are the first ship gate.

**Blockers:**
- `SC-002` [Release] Interface-backed planner wake spec #20 is active; Interface stream/broker feasibility, implementation, feature documentation, and real Claude/Codex/Kimi sprint gates remain pending | Blocker for: freeze/ship feature 14
- `SC-008` [Docs] Interface-backed planner wake spec #20 is canonical in the live DB; flat render specs_sc/interface-backed-planner-wake.md and content.sql snapshot require FnB GUI Publish | Blocker for: reviewable and rebuild-durable spec artifact
- `SC-010` [Engine] Pending spec-task titles/descriptions are writable by the memory API but sc mem task exposes no edit verb; tasks #79-87 cannot be aligned to Spec #20's QA contracts for crash ambiguity, idempotency/auth, watcher-daemon cutover, action receipts, and rebuild refusal through the documented CLI | Blocker for: unambiguous Spec #20 kickoff handoff
- Pre-existing render drift: live-DB doc #21 (SPRINT: Sprint planner session control) has render_path=NULL while content.sql carries orphan committed render specs_sc/sprint-planner-session-control.md (stale from reverted sprint 21). Once seq 2's render pipeline lands, the next Publish will surface this as render-check drift. Reconcile (drop orphan or restore render_path) as part of the spec #20 Publish flow — NOT absorbed by seq 2.

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

### Content-write cross-process safety · owner: `PLN1`
Render / snapshot-publish pipeline redesign (FnB-decided 2026-07-22; supersedes the flock-everywhere approach, closes flags #27 + #32). ARCHITECTURE: a DEDICATED render worktree whose sole purpose is snap-publish, driven by the web GUI (CLI ./sc publish uses the SAME atomic path — parity). Snap-publish is ONE atomic action: render all flat _sc files + snapshot content.sql are NEVER decoupled — that decoupling is the entire drift bug class (flags #27/#32). FLOW: FF the render tree to main -> render + snapshot -> commit -> push branch -> open PR on remote -> merge on remote -> local main pulls the merged render (never a direct push to protected main). FAIL-CLOSED on mid-publish failure via the existing error message + reset the tree to clean main before the next publish. Main moving mid-publish is deliberately NOT handled — periodic, not real-time; no SHA-capture/rebase machinery. Companion robustness item: DB JSON-integrity guards (roadmap #23). Spec AFTER Sprint 25 (don't churn spec #20).

_No open flags._

### Zombie-session kill tooling (admin/planner) · owner: `PLN1`
From Sprint 25 W1 stall (flag #38): an orphaned harness process (detached, ppid=0, cwd in a shell's worktree) holds that shell's one-session liveness slot and blocks every headless './sc run' of it, with no self-serve reap path — required manual /proc diagnosis + kill by the planner. Build: (1) an API/CLI verb usable by admin AND planner shells to list + kill zombie/orphan sessions safely (idle-verify — no working children, sleeping state — before SIGTERM->SIGKILL, scoped to harness procs whose cwd is under a worktree; never kill mid-work); (2) a 'make dos-kill' host command for the same. Pairs with the existing shell_liveness.py detector (reporting-only today). FnB-requested 2026-07-22.

_No open flags._

### DB JSON-integrity guards (render/boot robustness) · owner: `PLN1`
SANITIZE/VALIDATE AT THE WRITE BOUNDARY so malformed JSON can never enter the DB (FnB 2026-07-22). Every sc mem / API write that stores a JSON field validates + REJECTS malformed input with a clear error BEFORE it lands. Invariant: every JSON field in the DB is valid JSON. One-time cleanup migration for pre-existing bad rows. Render + boot then TRUST the invariant; if ever violated they FAIL LOUD identifying the exact offending row, NEVER silent skip-and-continue. Explicitly NOT graceful degradation (that hides problems). Prevent at input, don't mask downstream (L&S #15). Companion to roadmap #21.

_No open flags._

## Brainstorm

### Fork to sibling repos · owner: `cc`
Fork super-coder into dos-arch / rst-c / emergence / md-converter; reseed pattern.

_No open flags._
