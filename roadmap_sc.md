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
- DATA LOSS in memory DB — roadmap writes reverted (roadmap #21 render-pipeline edits + the JSON-integrity item lost; a later 'roadmap add' silently reused the freed id — clobber). Decisions #21-23 survived (table-selective). CORRECTION: my first hypothesis (seq-2 snapshot-on-'sc mem doc edit' reverts roadmap) is DISPROVEN by a controlled test — roadmap #21-23 survived a board doc-edit unchanged; that snapshot is PROTECTIVE (dumps live->content.sql). Actual mechanism: a rebuild-from-content.sql (the './sc update' reconcile / any rebuild) reverts any sc mem write not yet snapshotted to content.sql — roadmap writes made between snapshots are lost on rebuild. Two real engine defects: (1) rebuild silently drops un-snapshotted live writes instead of snapshot-first-then-rebuild or failing loud; (2) 'sc mem roadmap add' reuses an existing/freed id (silent clobber) instead of a fresh id or a loud conflict. Both violate the validate-at-write / fail-loud stance (L&S #15). Recovery: #21 re-edited, JSON-integrity re-added at #23; a board doc-edit since has snapshotted them to content.sql. Sprint CODE unaffected (git/PRs).

### Interface chats and interactive planner wake · owner: `cc`
First-class Interface tab and API-owned input broker for one durable tmux-hosted chat per shell, with CLI/API parity, safe clean+idle+3s planner wake, durable sprint events, and local watched-PR polling. Spec #20; brokered PTY streaming and concurrency fidelity are the first ship gate.

**Blockers:**
- `SC-002` [Release] Interface-backed planner wake remains unfrozen. Corrective spec #30 covers all 16 AMI findings plus FnB operator polish in tasks #88-#93 and #95-#97: lifecycle/update/snapshot correctness, restricted Admin operability, 1300x850 terminal, generation-scoped alerts, validated simplified model picker, unified stranded-shell recovery, restored Rich CLI chooser, documented Make surface, make dos-token, full-service dos-r, and exact AMI plus Claude/Codex/Kimi acceptance. | Blocker for: freeze/ship feature #14
- `SC-008` [Docs] Interface-backed planner wake spec #20 is canonical in the live DB; flat render specs_sc/interface-backed-planner-wake.md and content.sql snapshot require FnB GUI Publish | Blocker for: reviewable and rebuild-durable spec artifact
- [Recovery] Stale shell liveness and unclosed archives can block re-entry after a headless or tmux process exits. Corrective spec #30 task #95 now owns one browser/CLI API recovery: preview exact Interface/archive/tmux/process/worktree evidence, close absence-proved state atomically, preserve worktrees by default, and require scoped confirmation for an exact force or separate discard. Roadmap #22 is absorbed; no direct DB edit or broad process kill is an accepted workaround. | Blocker for: task #95 and feature #14 freeze
- real-tmux tier env gap: the image bake (flag #52) installed @xterm/headless + @xterm/xterm at the npm global root /usr/lib/node_modules, but interface_runtime.SHADOW_NODE_PATH expects /opt/sc-shadow/node_modules (or .super-coder/shadow/node_modules). Without a workaround the shadow sidecar cannot resolve @xterm/headless and HAS_SHADOW_STACK-gated tests skip. Workaround in dev3 worktree: symlink .super-coder/shadow/node_modules -> /usr/lib/node_modules. Fix: Dockerfile installs to /opt/sc-shadow/node_modules, or the engine adds the npm global root to its NODE_PATH candidates + _shadow_module_present check.
- `Host process/tmux accumulation audit` During Sprint 31 kickoff a /proc snapshot showed high live process counts: 773 git, 194 tmux:server, 179 sh, 161 cat, 24 node. The '24 unreadable harness processes' liveness warning = the node runtimes classified as indeterminate = the #517 fail-open behavior U6 is fixing, NOT orphans. All 1496 pid dirs were readable; most processes belong to the 4 active Sprint-31 workers + git/tmux churn. OPEN QUESTION for later: is the tmux-server/git count genuine accumulation across old sessions, or just active-sprint footprint? DO NOT resolve by broad pkill — that would kill live workers and violates spec 30's no-broad-match recovery contract. Correct path: orphan audit at sprint idle (clean gate) via the U8 stranded-shell recovery tool (API-owned, absence-proven, exact-PID); release only what is provably dead. Owner: FnB/host-supervisor call; planner assists at close-out.
- `Flaky bounded-buffer spike test (2x in sprint 31)` Recurring flaky test in the local full suite: the bounded-buffer spike (spikes/interface-stream test_bounded_buffers) failed twice in Sprint 31 across two independent units, both around the ~5,065,000-byte slow-consumer scenario. Unit 1 (DEV5, kimi): stalled at 5,007,670 / 5,065,000 bytes. Unit 3 (DEV4, sol): timed out AFTER the broker pumped all 5,065,000 bytes (906 passed, 8 skipped otherwise). KEY DIAGNOSTIC: unit 3's isolated rerun of the same test passed GREEN IN 1 SECOND — it fails only under full-suite load, so this is resource/scheduling contention, not a defect in the test's own logic. Two distinct failure shapes (incomplete pump vs. post-pump timeout) plus the 1s isolated pass point at a timing/backpressure assumption that does not hold when the suite competes for CPU/IO. Both devs correctly judged it anomalous, reran in isolation, and did NOT patch healthy code. NOT in CI's gate set, so it blocks no merge — impact is wasted dev rerun cycles and noise that can mask a real red. Follow-up: reproduce under deliberate load, then either make the test's timeout/backpressure load-independent (preferred), isolate it from parallel execution, or quarantine it. Do NOT simply raise the timeout — that hides the contention sensitivity rather than fixing it.

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
Render / snapshot-publish pipeline redesign (FnB-decided 2026-07-22; supersedes flock-everywhere; closes flags #27, #32). MOTIVATION (sharpened 2026-07-22, ties to flag #39 data-loss): the snapshot is currently AVOIDED because it breaks whenever main is dirty at all — so sc mem writes go un-snapshotted and are silently lost on the next rebuild (that IS the flag-#39 root cause). A DEDICATED always-clean render tree, synced to main, removes the dirty-main failure mode → snapshotting becomes safe + routine → avoidance disappears → writes persist. ARCHITECTURE: dedicated render worktree, sole purpose snap-publish, GUI-driven (CLI ./sc publish = same atomic path). Snap-publish is ONE atomic action: render all flat _sc files + snapshot content.sql NEVER decoupled (the drift bug class, flags #27/#32). FLOW: FF tree to main -> render + snapshot -> commit -> push branch -> PR on remote -> merge on remote -> local main pulls (never direct push to protected main). FAIL-CLOSED on mid-publish failure + reset tree to clean main. Main moving mid-publish deliberately NOT handled (periodic, not real-time). MUST PAIR with the flag-#39 engine fix (rebuild snapshots-first or fails LOUD on un-snapshotted writes) so durability is not purely human discipline (L&S #15). Companion: DB JSON-integrity guards (roadmap #23). Spec AFTER Sprint 25.

_No open flags._

### DB JSON-integrity guards (render/boot robustness) · owner: `PLN1`
SANITIZE/VALIDATE AT THE WRITE BOUNDARY so malformed JSON can never enter the DB (FnB 2026-07-22). Every sc mem / API write that stores a JSON field validates + REJECTS malformed input with a clear error BEFORE it lands. Invariant: every JSON field in the DB is valid JSON. One-time cleanup migration for pre-existing bad rows. Render + boot then TRUST the invariant; if ever violated they FAIL LOUD identifying the exact offending row, NEVER silent skip-and-continue. Explicitly NOT graceful degradation (that hides problems). Prevent at input, don't mask downstream (L&S #15). Companion to roadmap #21.

_No open flags._

## Brainstorm

### Fork to sibling repos · owner: `cc`
Fork super-coder into dos-arch / rst-c / emergence / md-converter; reseed pattern.

_No open flags._

## Retired

### Zombie-session kill tooling (admin/planner) · owner: `PLN1`
Absorbed into feature #14 corrective spec #30, task #95: one API-owned browser/CLI recovery preview and execution flow for stale durable locks and exact orphan processes, preserving worktrees by default and requiring fresh evidence plus scoped confirmation for force or discard. The original standalone dos-kill design is superseded by dos-recover.

_No open flags._
