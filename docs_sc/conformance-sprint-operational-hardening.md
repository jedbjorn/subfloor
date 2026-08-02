---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
title: 'CONFORMANCE: Sprint operational hardening'
tags: [sprints, conformance]
date: 2026-08-02
project: super-coder
purpose: Sprint 74 close-out — spec #73 judged against main @ f442bb2
---

# CONFORMANCE: Sprint operational hardening

Conformance pass for sprint doc #74. Spec: #73 (feature #31). Judged against
the integrated code on `main @ f442bb2` — never the diffs, never the trail.
Units: #933 (terminal/liveness), #934 (role skills + flag evidence), #931
(CLI hygiene); unit 4 = report-only proof (dos-app, evidence doc #75).
Adopted non-spec PR #932 (admin shells CLI-only) was excluded from spec
judgment; zero collision with any spec claim (confirmed zero file overlap
with #933 per sprint log, and its surface — browser chat admin — is outside
every spec #73 requirement).

Narrative inputs honored (PLN1 ratifications): (1) unit 1 reused the existing
actionable changes-requested notification for rework re-observation and
extended the existing disposition trigger for `in_review`; (2) unit 4's
liveness-threshold step accepted on structural proof; (3) unit 4 step-2
evidence supplemented post-gate (doc #75 supplement, flag #122 closed).

Focused suites re-run green on `main @ f442bb2` during this pass:
`test_sprint_recovery.py` + `test_sprint_liveness.py` +
`test_sprint_review_loop.py` + `test_sprint_message_delivery.py` (71 passed);
`test_devkit_sc.py` + `test_worktree_targets.py` (49 passed);
`test_sprint_skills.py` + `test_mem.py` (60 passed, 27 subtests).

## Verdict table

| # | Spec requirement (doc #73 section) | Verdict | Evidence |
|---|---|---|---|
| 1 | Terminal completion: defer owning Planner run on `completed` — identify active run, cancel queued turns, append close-request, omit interrupt intent/signaling | as-specced | `.super-coder/scripts/sprint_participant_chats.py:243-341` (deferral predicate :303-307, close-requested append :291-302, queued-turn cancel :277); authorization binds planner caller to `originating_planner_shell_id` (`sprint_domain.py:1486-1497`). Test: `test_completed_defers_only_owning_planner_run_until_broker_finish` (`tests/test_sprint_recovery.py:1045`) |
| 2 | Immediate close for idle conversations; interruption preserved for other participants | as-specced | `sprint_participant_chats.py:315-340` (idle path unchanged), :308-313 (non-owner runs interrupted). Tests: `test_terminal_lifecycle_closes_every_sprint_conversation` (:1241), Developer-only interrupt asserted :1086-1111 |
| 3 | Existing broker finalization records Planner run result and closes its conversation exactly once | as-specced | Pre-existing close-aware `finish_run` path (`conversation_broker.py:647-663, 680-779`), sequence-guarded, terminal-run replay returns False. Test: `tests/test_sprint_recovery.py:1146-1236` (real BrokerStore, all conversations closed exactly once, Planner `run.completed` with final_output) |
| 4 | `aborted` preserves interrupt-all | as-specced | Deferral gated on `lifecycle == "completed"`; abort persists interrupt intents for all active runs and hard-fails on set mismatch (`sprint_domain.py:460-546`, :529-532). Test: `test_abort_interrupts_owning_planner_and_other_active_participants` (:1188) |
| 5 | Completion retry idempotent — no second report, lifecycle event, close event, or synthesized response | as-specced | `transition` short-circuits same-state before any side effect (`sprint_domain.py:289-290`); final-report replay keyed separately (`sprint_close.py:130-143`); close-requested append self-deduping. Test: `tests/test_sprint_recovery.py:1113-1144` |
| 6 | No new caller-run header, deferred-close state, delayed job, or post-completion message | as-specced | Migration 0158 = trigger replacement + backfill only; `.super-coder/api/` untouched by #933; deferral reuses existing `conversation.close.requested` + broker finish path |
| 7 | Liveness: `in_review` resolves unresolved `work_assignment` expectations as `work_unit.in_review` | as-specced | Migration `0158_sprint_terminal_liveness_hardening.sql:12-37` — trigger `trg_sprint_liveness_work_terminal` on `disposition IN ('in_review','completed','cancelled')`; backfill :40-53. Tests: `test_sprint_review_loop.py:135` (resolution triple asserted), `test_sprint_liveness.py:666` (no Developer nudge 20 min past handoff) |
| 8 | Terminal `completed`/`cancelled` resolutions preserved | as-specced | Same trigger `WHEN` clause covers all three dispositions; resolution derives from `NEW.disposition`. Test: `test_sprint_liveness.py:654` (`work_unit.completed`). (No `cancelled`-string test — finding F4) |
| 9 | Review-request expectation stays open until `record-review`; Reviewer liveness not resolved at handoff | as-specced | Trigger subquery matches only `work_assignment` / developer `notification` kinds; `record_review` resolves reviewer expectation explicitly (`sprint_review_loop.py:219-222`). Handoff test asserts review expectation unresolved with `next_evaluation_at` armed post-handoff |
| 10 | Transition, assignment resolution, review message, Reviewer wake, work-unit event, judgment in the existing request-review transaction; retry idempotent; no reopen/duplicate | as-specced | `sprint_review_loop.py:82-125` single `write_transaction`; trigger fires on the same UPDATE; replay short-circuits at :102-103; `OLD <> NEW` + `resolved_at IS NULL` guards; zero reopen paths in scripts. Tests: `test_handoff_and_outcome_replay_without_duplicate_durable_facts`, `test_in_review_upgrade_backfills_dirty_assignment_once` (migration run twice, byte-identical state) |
| 11 | Changes-requested verdict re-observes Developer via accepted verdict wake; resolved assignment never reopened | as-specced | Ratified call (1) is the spec's own reading ("extend the existing mechanism"): `sprint_review_loop.py:162` (`actionable = verdict == "changes_requested"`), `ACTIONABLE_KINDS` widened (`sprint_message_delivery.py:23`); acceptance trigger inserts a NEW expectation keyed by the new message. Test: `test_changes_and_approval_select_fresh_linked_conversations` asserts original stays `work_unit.in_review`, new wake resolves on second handoff |
| 12 | Unit 1 gate: "ship the ordered migration and schema source together" | deviated-intentionally | Spec premise contradicts the engine migration contract (`migrate.py:9-12`: migrations are NEVER folded back into `schema.sql` — that double-applies on fresh builds). Code correctly ships migration-only; fresh-build composition proven by the upgrade test. Spec text error, not a code defect — finding F2 |
| 13 | Pre-declaration QAQC split from post-declaration inbox entry (#925) | as-specced | `sprint_rev/SKILL.md:16-23` (explicit Planner/FnB request → `record-qaqc --document`, no inbox step, no Sprint row), :25-32 (armed entry via `sc sprint inbox`). Test: `test_reviewer_entry_separates_predeclaration_qaqc_from_armed_inbox`. Live-proven in unit 4 (QAQC approval id 7 at 07:21:42 precedes sprint.declared 07:22:14, doc #75 §6) |
| 14 | Resolved flag reads: exact-ID form, feature-scoped `--resolved`, unscoped refused, full field set human+JSON, reuse authenticated single-row endpoint, no new command family (#922) | as-specced | CLI `mem.py:524-539` (refusal matrix) + server `server.py:2779-2826` (defense in depth, 400s); both queries select all nine required fields; exact-ID hits the pre-existing `GET /_sc/mem/flags/{id}` used by `flag close`. Tests: `test_mem.py` — field completeness, scope exclusion (open/deleted/other-feature), refusal matrix at CLI and raw API. Live-proven in unit 4 (flag 15 closure notes read verbatim via both forms, doc #75 §5) |
| 15 | `flags`, `db_map`, `sprint_rev` skills updated to the supported read | as-specced | `flags/SKILL.md:20-37`, `db_map/SKILL.md:18-20,110-111`, `sprint_rev/SKILL.md:102-109` |
| 16 | Native wake ownership stated per-role in all five skills; no skill directs shell-owned scheduling/polling/watchers/boots | as-specced | `sprint_prep:27-28,101-112,116` (atomic arm, hand to `sprint_pln`, native pickup); `sprint_pln:17-19,61-72` (native dispatch/liveness/PR observation; monitor bounded one-shot); `sprint_dev:97-100,107-110,126-127` (await native red/green; stop after request-review); `sprint_rev:16-32,188` (durable wake entry, stop after typed receipt); `sprint_close:76-79,173-176,188-191` (durable conformance relay, pre-complete inbox drain, no post-complete command). Negative grep across all five bodies: no shell-owned loop/poll/watch directives remain. Test: `test_role_contracts_assign_scheduled_coordination_to_native_wakes` |
| 17 | One final skill seed + ordered reseed migration; seed and migration consistent | as-specced | `0001_seed_skills.sql` + `0159_reseed_sprint_native_wake_skills.sql` (7 UPSERTs) byte-identical to authoritative assets (verified programmatically). Test: `test_native_wake_reseed_converges_dirty_rows_and_replays_idempotently` |
| 18 | Unit 2 six-dimension audit as final gate | as-specced | Audit matrix supplied by DEV4, independently verified by REV2 per sprint doc #74 notes; current skill bodies show role-lifecycle organization, one home per repeated rule, directives over prohibitions. No contradiction found in this pass |
| 19 | `sc deps` help returns zero before discovery, `.venv`, probes, installs; `-h`/`--help` forms; worktree byte-stable (#926) | as-specced | `sc:344-348` help gate is the first statement of `sc_deps`; first `.venv` access :357, first discovery :363, first pip :423. `sc_help_form` matches only `-h\|--help` (`sc:60-65`). Test: `test_deps_help_is_read_only_and_byte_stable_from_both_checkouts` (rc=0 ×4, byte-identical output, no `.venv`, empty find/venv shim log, DB digest unchanged). Live-proven in unit 4 supplement (sha256-identical across container runs and host; pip/npm/pnpm/yarn/uv shims zero-hit) |
| 20 | Nested Python discovery: reuse pruned manifest walker, exclusion list, one presence result at both gates, one root pytest invocation (#774) | as-specced | `_sc_has_python_tests` (`sc:275-284`) pipes the existing `_sc_find_manifests` walker; prune list covers engine/state/worktrees/venv/VCS/cache/build/deps/vendor (`sc:266-268`); literal `tests/` path component required; result computed once (`sc:472-473`), consumed by both gates (:481, :515); single root invocation `( cd "$here" && "$pytest_bin" "$@" )` (:503). Tests: `test_pruned_and_non_test_paths_do_not_count`, `test_nested_suite_provisions_then_runs_one_root_pytest`, `test_sc_test_caches_one_presence_result_for_both_gates`. Live-proven in unit 4 supplement (nested `designs_os/api/tests` detected, pytest selected, one root invocation) |
| 21 | Explicit `sc test <args>` and frontend execution unchanged | as-specced | `"$@"` forwarded (:503); exit-5 leniency still gated on `$# -eq 0` (:505); npm/vitest leg untouched (:530-539) |
| 22 | Unit 4 downstream proof: full chain (QAQC → deps/test → assign → review → liveness → flag read → verdict → complete → terminal evidence) with native wakes only | as-specced | Doc #75: real dos-app sprint 4 end-to-end, 7/7 wakes delivered attempt-1, exactly-once reports, deferred Planner close observed live (caller conversation closed at 07:46:15 after run completion, `{"recovered": true}`), idempotent replay exercised, zero post-terminal commands, zero human intervention |
| 23 | Unit 4 step 4: advance beyond the liveness threshold with Reviewer active; observe no Developer nudge | deviated-intentionally | Live window handoff→verdict was ~5 min; threshold not crossed in real time. Accepted on structural proof per PLN1 ratification (2): the expectation is resolved at `in_review`, making post-handoff nudges impossible; mechanism additionally test-proven (`test_sprint_liveness.py:666` — 20-min advance, zero nudges) |
| 24 | Unit 4 step 2: `sc deps --help` + bare `sc test` durable command-output evidence | as-specced | Supplemented post-gate per PLN1 ratification (3): doc #75 supplement holds byte-hashes, exit codes, fs sweeps, shim logs; raw captures in dos-app `shared/sprint74-u4-supplement/`; flag #122 closed against it |
| 25 | Adversarial gate — none of the ten failure modes remain possible | as-specced | Each checked in this pass: skills clean of coordination loops (#16); no command/authority lost in audit (#16-18); completion no longer interrupts its caller, other participants still interrupted, abort unchanged (#1-4); assignment liveness resolves at `in_review`, Reviewer liveness untouched (#7-9); QAQC needs no Sprint, declared review uses inbox (#13); reviewer needs no SQL/credentials/mutation/fleet history for closure notes (#14); `deps --help` touches nothing (#19); nested suite detected, ignored trees not counted (#20); no new lifecycle state, liveness table, QAQC inbox, flag-history family, package manager, or test runner (#6, #14, #20) |
| 26 | Ratified decisions 1-4 (completion deferral scope; resolved-flags shape; four-unit packaging; wake ownership) | as-specced | 1: verified #1-4 (edge noted in F1). 2: verified #14. 3: sprint doc #74 shows three implementation units + report-only unit 4, audit inside unit 2's gate. 4: verified #16 and unit-4 live run |
| 27 | Non-goals held (no shell-owned machinery, no fleet-wide history, no import-root policy, no per-project pytest loops, no renamed commands) | as-specced | Confirmed across #6, #14, #16, #20; unit-4 integrated diff was test-only (+14/-0) |

## Findings (0 Major, 0 Medium, 7 Low)

- **F1 (Low) — FnB-initiated `complete` also defers the owning Planner's live run.** Deferral keys on `originating_planner_shell_id`, not caller identity (`sprint_participant_chats.py:303-307`; FnB completion allowed by `_sprint_actor`, `api/server.py:2043-2044`). The spec's trigger describes the completing-owning-Planner case; an FnB complete with a live Planner run now defers that run too — benign in effect (run finishes, then closes), untested, and a behavior change beyond the stated trigger. Recommend: planner decision — document as intended or narrow the predicate.
- **F2 (Low) — Spec text error, Unit 1 gate.** "Ship the ordered migration and schema source together" contradicts the engine contract (`migrate.py:9-12`: schema.sql is the frozen baseline; migrations are never folded back — doing so double-applies on fresh builds). The code is correct; the spec sentence should read "ordered migration plus updated removal-manifest/fixture references." Recommend: planner corrects the spec record; no code action.
- **F3 (Low) — Reviewer-liveness resolve runs outside the verdict transaction.** `record_review` commits the verdict in `sprint.review.outcome`, then resolves the reviewer expectation afterwards (`sprint_review_loop.py:219-222`). A crash in the gap leaves Reviewer liveness open → spurious Reviewer nudges — the same defect shape #929 fixed for Developers. Pre-existing, narrow window, self-limiting impact. Flag opened for follow-up (feature #31).
- **F4 (Low) — No test asserts the `work_unit.cancelled` resolution string.** The trigger covers it textually (`WHEN` clause includes `cancelled`); only `completed` and `in_review` are asserted. Pre-existing hole from the 0149 era.
- **F5 (Low) — Reassignment edge in the trigger's notification branch.** Resolution requires `participant.shell_id = NEW.assigned_shell_id`; a Developer reassigned after accepting a changes-requested wake keeps that expectation open and nudging the old shell. Marginal, unhandled.
- **F6 (Low) — Pre-existing duplicate migration number 0155** (`0155_reseed_catalogue_cleanup.sql` + `0155_sprint_conversation_generations.sql`). Ordering resolves lexically; benign. Hygiene note for the planner.
- **F7 (Low) — Help-probe test does not shim pip/npm directly** (`tests/test_worktree_targets.py:314-344`). The realistic regression (discovery before help) is caught via the `find` shim; an exotic reorder that skips discovery but runs installs would slip the probe. Unreachable in the current code order.

## Overall

Spec #73 is **shipped**: every behavioral requirement is on `main @ f442bb2`
as specced or under a ratified intentional deviation (rows 12, 23). Zero
Major, zero Medium findings. The seven Lows are planner dispositions — none
blocks freeze. Adopted PR #932 does not collide with any spec claim.
