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

### B4 — OpenCode adapter · owner: `cc`
Emit opencode.json + verify the research-flagged items; boot already dual-writes AGENTS.md + SKILL.md.

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

### Token & session analytics · owner: `cc`
Self-tracked token spend + session history across all harnesses (claude/opencode/codex/vibe/kimi). Sweep-parse each harness's on-disk usage data into session_token_usage; session lifecycle columns on archives; /api/analytics/* + GUI Analytics tab (7-day paged history with session titles, harness/provider/model filters, sprint clusters, usage analytics). Tokens only, no pricing, v1.

_No open flags._

### Boot spinner — launch feedback after harness pick · owner: `PLN1`
Interactive ./sc enter|boot goes silent 7-10s between the harness pick and the boot summary (git fetch + gh pr list dominate). Add a TTY-only ASCII spinner with phase labels in style.py, wrapped around the silent region of run.py main(). No headless/CI output change. Spec: specs_sc/boot-spinner.md.

**Blockers:**
- `SC-004` [Docs] Boot spinner PR #437 pending merge; after landing, freeze correction spec 15 and update the feature doc from shipped code | Blocker for: Boot spinner correction record

### Visual QA CI — Playwright viewport screenshots · owner: `PLN1`

**Blockers:**
- Visual QA CI spec (#13) live in DB; git render/snapshot pending FnB GUI Snapshot — sc mem doc pipeline defect filed as subfloor#434 | Blocker for: spec render in specs_sc/
- [Docs] Visual QA CI shipped (PRs #438/#442/#443), feature doc pending (conformance F6) | Blocker for: f19 feature doc

### Headless model routing catalogue · owner: `DEV3`
Self-healing locally authoritative model routes for interactive and generic headless launches, with exact selector and effort resolution across supported harnesses.

_No open flags._

### Local-only generated artifacts · owner: `cc`
Remove tracked artifact mode and Publish commits; keep snapshots and renders under the gitignored local artifact root so updates and shell worktrees never dirty main.

_No open flags._

### Shell-facing API identity wording · owner: `PLN1`
Hide bearer-token plumbing from ordinary shell-facing docs while keeping a minimal 'sc mem is already wired' explanation.

_No open flags._

### Harness refresh on restart · owner: `cc`
Keep harness executables image-owned, relocate Codex outside the mounted state tree, and make a normal restart refresh harness installer layers; --no-build remains the explicit pinned-image path.

_No open flags._

### Browser chat diff review · owner: `PLN1`
Read-only browser Diff for the selected shell's current worktree against fetched origin/main: ignored paths excluded, Dirty/Branch/Commits views, explicit refresh with stable scroll, and exact on-disk CLAUDE.md, AGENTS.md, and granted skills.

_No open flags._

### Sprint v1 architecture removal · owner: `PLN1`
Delete the broken Sprint v1 subsystem completely before designing its replacement: no Sprint data migration, compatibility contract, legacy runtime, or preservation requirement; retain only independently useful generic infrastructure after proving its non-Sprint ownership.

_No open flags._

### Safe repository teardown (dos-remove) · owner: `PLN2`
Add make dos-remove / ./sc remove: preflight and quiesce the repo-scoped runtime, create and verify a WAL-safe DB backup under the gitignored .sc-state/db_backups/removal/ subtree, then remove subfloor-owned files, worktrees, hooks, remote, and Makefile integration without deleting host-owned repository content.

_No open flags._

### Skill catalogue convergence and governance · owner: `PLN1`
Make upstream skill removal durable across downstream DB rebuilds and every engine-managed flat projection; retire obsolete Sprint v1, engine_surgery, and database-specific test skills; remove shell-authored skill promotion and route curation recommendations upstream.

_No open flags._

### Re-enterable closed chats — resume from the chats list · owner: `cc`
Closed browser chats are currently terminal: state machine (conversation_state.py + trg_conversations_state in migration 0132) has no edge out of 'closed'. Make a closed chat re-enterable from the chats list: user selects it, types a message, chat reopens and resumes. Feasible cleanly — harness_session_ref survives close and every turn already resumes via it, so this is a policy change, not a plumbing change. Scope: (1) migration replacing trg_conversations_state with a closed->idle reopen edge, clearing closed_at; (2) conversation_state.py map in lockstep; (3) API reopen path (send-to-closed reopens + queues, conversation.reopened event), gated to conversation_scope='normal' (Sprint chats close only via Sprint lifecycle) and honoring the one-open-normal-chat index by auto-closing the currently open chat (same UX as opening a new chat); (4) UI: chats list selection of a closed chat shows composer; (5) schema-walk + API + UI tests. Edge case: harness may have pruned the old session — failed resume surfaces as a failed run/error state. Requested by FnB 2026-08-02.

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

### Browser-native headless conversations · owner: `cc`
Durable normal browser conversations backed by exact harness session identifiers and an event-driven broker, with queued turns, Stop/Close recovery, history, stars, bounded transcripts, and read-only Diff review.

**Blockers:**
- `SC-021` [Sprints] Engine restart turns an in-flight one-shot into unrequested cancelled, leaves unit working, and emits no worker-failed directive; subfloor#820 | Blocker for: feature #24 restart recovery release assertion
- `SC-022` [Sprints] Planner conformance-handoff binding keeps triggering unit_id, but required kickoff directive is unitless; migration 0138 result trigger rejects correlation and assignment fails; subfloor#821 | Blocker for: feature #24 ordinary close-loop release assertion
- `SC-023` [Engine DB] SQLite lock contention terminalized real Sprint conformance run unknown (BROKER_RUN_ERROR) under ordinary multi-role load; reopened subfloor#331 | Blocker for: feature #24 reliable end-to-end Sprint close

### Sprints v2.0 — collaborative orchestration · owner: `cc`
Build an observable, long-running multi-shell collaboration loop over durable browser conversations: FnB-enterable Sprint chats and amber shell pills, reliable inbox wakes, armed-only GitHub observation and liveness monitoring, dependency-aware parallel work, Dev/Review judgment with recorded rationale, deterministic evidence capture, conformance, and Planner synthesis.

**Blockers:**
- `FU-QAQC-resolution-gate` [Sprint 51 follow-up, A-Md2, doc #57] Every-Medium+-QAQC-finding-resolved-before-approval is unchecked: findings_document_id stored on approval rows but read by nothing; the resolution gate exists only in sprint_prep skill text.
- `FU-preparation-checks` [Sprint 51 follow-up, A-Md3, doc #57] Preparation checks absent: no GitHub access/worktree probe at declare or arm (worktree resolution strict=False); Planner fallback capacity is reactive at runtime, never a preparation check.
- `FU-fallback-packet-unread` [Sprint 51 follow-up, A-Md6, doc #57] Planner fallback context packet is write-only: built and stored on the link row, never supplied to the replacement conversation (generic wake prompt only); fallback also fires only from liveness escalation, not primary exhaustion.
- `FU-fnb-messages-domain` [Sprint 51 follow-up, A-Md7, doc #57] FnB messages never become sprint_messages rows: FnB POSTs land as generic conversation_messages, invisible to sprint-inbox tooling and message-domain evidence; spec Terms say they should enter the domain.
- `FU-liveness-suppression-ceiling` [Sprint 51 follow-up, B-C-m3, doc #58] Wedged-but-alive worker never escalates: process.present restamps every evaluation, deferring post-nudge escalation forever. Broader than decision #42 ratified — needs a code ceiling or a spec carve-out defining positive evidence of healthy work.
- `FU-git-commit-evidence` [Sprint 51 follow-up, B-C-m4, doc #58] Git commits are in the spec strong-evidence list but not consumed by the liveness monitor — only a non-commit-specific worktree.observed supporting signal.
- `FU-spec-edit-guard` [Sprint 51 follow-up, B-C-m6, doc #58] Bound-spec edit guard is resume-granular only: any shell may edit a bound spec mid-Sprint with no write-time detection/actor/notification; drift computed only at resume by sha compare, blind to edit-then-revert.
- `FU-packet-recovery-section` [Sprint 51 follow-up, B-C-m7, doc #58] Evidence packet pause_and_recovery section misses resume evidence: lifecycle.reconciled/armed events match no marker; spec_revisions.mid_sprint_edits reads events nothing emits.
- `FU-fnb-close-untested` [Sprint 51 follow-up, B-C-m8, doc #58] FnB-drives-close positive path untested: admin branch of _require_close_authority never positively exercised; acceptance manifest overclaims coverage.
- `FU-unapproved-head-merge-audit` [Sprint 51 follow-up, REV1 obs #644] Out-of-band merge at an UNAPPROVED head while merge_ready completes silently with no grant-bypass notice — audit-trail gap adjacent to R7; bypass evidence should cover approved-state-wrong-head.
- `DOC-spec68-handoff` [Docs] Sprint handoff hardening Spec #68 is implemented in subfloor PR #904; after merge, freeze spec #68 and update the Sprints v2 feature documentation with the callable floor, participant relay, general wake template, delivered-unread recovery, and downstream proof. | Blocker for: final Spec #68 documentation closeout
- `SC-049` [Review] U5/PR#948: reconcile_linked_dispatchers overwrites worktree sc with target bytes BEFORE the pin publishes; a mid-update crash (the very #936 failure class) leaves sc@failed-target in the worktree, and the next update at a different ref can never recognize it (managed_versions = {HEAD:sc, sc@old-pin} only) — permanent 'dispatcher locally edited, left stale' misdiagnosis, self-heal dead for that worktree. Fix direction: recognize via engine.ref.prev / record pending target / or reconcile after migrate+publish. | Blocker for: Sprint maintenance round 2
- `SC-050` [Review] U5/PR#948 closes #936 on the spec's at-minimum clause (engine.ref publishes only after every step succeeds) but never identifies or reproduces the first-attempt failure mechanism: rst-c/ami failed migrate with FK at 0 migrations applied and the rerun applied all 17 cleanly at the SAME advanced ref — ref value alone cannot explain that, so first-attempt success is asserted, not demonstrated (md-converter's snapshot.py failure likewise un-reproduced). Needs explicit planner ratification of the at-minimum reading or a documented causal analysis; regression tests cover ref-publication ordering only. | Blocker for: Sprint maintenance round 2
- [Sprints] dos-arch Sprint 3 paused on two engine defects: premature-green rollup (spec #84) + SHELL_BUSY wake strand (spec #85) — fixes must ship + roll out before resume | Blocker for: dos-arch Sprint 3 resume
- [Sprints] PR watcher poll-failure backoff is in-memory only (sprint_pr_watcher.py:236) — engine restart resets it; rate detection is a stderr substring heuristic. Deliberately out of spec #84 scope | Blocker for: watcher transport hardening

### Modal and flag action normalization · owner: `cc`
Normalize shared modal footer semantics, then add a shared create/edit flag workflow with an eight-line description editor and card-level Edit action anchored bottom-right.

_No open flags._

## Next

### B1 — First-launch installer · owner: `cc`
Full installer on top of init_fork: requirements check, harness auto-detect, slot-filled shell_system_prompt template.

_No open flags._

### B6 — Commit→PR automation · owner: `cc`
edit→snapshot→render→commit→PR; per-shell-branch concurrency. The snapshot button is the manual precursor.

_No open flags._

## Near Term

### B5 — Onboarding & mapping · owner: `cc`
Base dr_* code map shipped (files/deps/env, ./sc map, surface_catalogue). NEXT — navigation layer (spec authored): dr_section + per-file desc (cartographer-authored, preserved across remap) + a CONNECTIONS block that replaces WORKSPACE. Supersedes the typed-semantic-tables plan. See specs_sc/b5-repo-navigation.md.

_No open flags._

## Brainstorm

### Fork to sibling repos · owner: `cc`
Fork super-coder into dos-arch / rst-c / emergence / md-converter; reseed pattern.

_No open flags._

### Native package distribution — Arch, Ubuntu, Fedora & Homebrew · owner: `PLN1`
Make subfloor installable through pacman/AUR, apt/PPA, dnf/COPR, and Homebrew using one machine-wide subfloor bootstrapper and a versioned, immutable engine payload. Preserve project-local ./sc, exact engine.ref pinning, update/rollback/eject semantics, and package-manager ownership boundaries; add tagged releases, canonical make install layout, explicit harness-install consent, per-ecosystem recipes, smoke-test CI, and release publishing automation.

_No open flags._

## Retired

### Sprint eventing — GitHub→inbox daemon + headless worker boot · owner: `cc`

**Blockers:**
- `SC-002` [Docs] sprint eventing shipped (PR #338), feature doc pending — and the loop is unproven until a real sprint runs on it | Blocker for: eventing feature doc + first eventing sprint
- `SC-007` [Docs] Sprint planner session-control spec #20 lives ONLY in the live engine DB — sc mem doc add/edit materializes neither specs_sc/sprint-planner-session-control.md nor a .sc-state/content.sql snapshot (render+snapshot pipeline defect, upstream subfloor#434). 2026-07-22: body materially revised with 6 spec-debt write-backs (retry semantics J2, arming posture validation J5, effort=config-effective-at-launch J4, transition-edge table J1, error-state sc enter retry-first recovery SC-466, F3 watcher re-arm softened). The pending FnB GUI Snapshot must capture CURRENT live-DB state so these write-backs reach git; until then a DB rebuild would lose spec #20 entirely. | Blocker for: reviewable flat render + durable snapshot of feature 14 spec seq 2

### Sprint reporting — unit reports, conformance pass, planner synthesis · owner: `cc`
Dev unit-report result rows at merge; pre-freeze conformance pass (review shells judge spec vs main, four-way verdicts); sprint report becomes a fixed skeleton the planner synthesizes from unit reports + conformance doc. Skill-text only — no schema, no CLI. See specs_sc/sprint-reporting.md.

_No open flags._

### Conductor — CLI sprint orchestration v1 · owner: `cc`
Rebuild sprint orchestration around a Conductor: weak-model OpenCode relay shell that never decides; ephemeral planner/dev/reviewer slot boots; engine-service sentinel as sole poller (liveness + dwell); directive/event contract schema replaces the interface wake machine. Plan by superCC (parent); he executes Step 1 (interface surface strip) on his conductor branch, then handoff: we import, QAQC his Step 1 against the spec, own Steps 2-12 to completion + maintain.

_No open flags._

### Task 137 release candidate · owner: `cc`
Report-only release-gate Sprint: verify README R1 against integrated main; no implementation or PR when already true.

_No open flags._
