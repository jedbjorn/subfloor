---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
title: "CONFORMANCE: Maintenance round 2 — atomicity, migration guardrails, update hygiene"
tags: [sprint, conformance, maintenance]
date: 2026-08-02
project: super-coder
purpose: Sprint 79 close-out spec-vs-main judgement
---

# CONFORMANCE: Sprint maintenance round 2 (doc #79)

- **Spec:** doc #78 (feature 31) · **Sprint:** doc #79 · **Judged against:** `~/Repos/subfloor` main @ `3d1f9f3` (HEAD verified; tests + render-check + CodeQL green on that SHA).
- **Method:** spec requirements judged against the code on main, never the diffs. Ratified judgement calls (PLN1 msgs #1110, #1129, #1136, #1143, #1181, #1192) are the only narrative input. U6 req 4 judged from PLN1's evidence file `shared/SPRINT79_U6_fleet_remediation.md`.
- **Result: PASS — 0 Major, 0 Medium findings.** Every requirement is as-specced or matches a ratified judgement call. No `deviated-silently`, no `unimplemented`. Low observations listed at the end (none blocking).

## Unit 1 — verdict atomicity

| Req | Verdict | Evidence |
|---|---|---|
| 1. Resolve inside verdict write transaction | as-specced | `sprint_review_loop.py:218` — `resolve_in_transaction(...)` is the last statement inside `write_transaction("sprint.review.outcome")` (:142); verdict facts (judgment :192-197, disposition :198-202, event :203-214, receipt :155-165) all in the same block. |
| 2. In-transaction pattern; no nesting; no widening | as-specced | New narrow message-id variant `resolve_in_transaction` (`sprint_liveness.py:594-606`, raises without an active transaction). `resolve()` (:590-592) is now a thin self-transacting wrapper, never called from the verdict path; `resolve_review_requests_for_work_unit_in_transaction` (:608-636) contract unwidened. |
| 3. Replay exactly-once | as-specced | Dedupe on `sprint_messages.idempotency_key` (replay returns existing conversation, no re-write) + resolve UPDATE guarded by `AND resolved_at IS NULL`. |
| 4. Tests (a) gap (b) replay (c) existing green | as-specced | (a) `test_verdict_and_liveness_resolution_survive_post_commit_abort` (test_sprint_review_loop.py:330) — abort after commit, expectation nonetheless resolved; (b) `test_handoff_and_outcome_replay_without_duplicate_durable_facts` (:260); (c) ReviewOutcomeTest suite green (18 passed on main). |
| Gate: post-verdict Reviewer nudge structurally impossible | as-specced | Single production writer (`/_sc/sprint/review-record` → `record_review`); any raise inside the block rolls verdict + resolve back together; no path commits a verdict with the expectation open. |

## Unit 2 — migration guardrails

| Req | Verdict | Evidence |
|---|---|---|
| 1. `./sc migration new <slug>` scaffold | as-specced | `sc:1167` → `migration.py`: collision-checked allocation (:64-70, exclusive create :138, validate before/after :125/:143), skeleton with `BEGIN;`/`COMMIT;` + idempotence idioms + intent header (:73-83), same-act manifest allowlist append (:129-133, atomic write, rollback on failure). |
| 2. Duplicate-number guard + frozen 0155 pair | as-specced | `validate_unique_numbers` (`migration.py:51-61`) with `FROZEN_NUMBER_COLLISIONS` (:25-34) allowlisting exactly the existing 0155 pair (both on disk). Tests: real tree green, synthetic duplicate red, third-0155 red. |
| 3. Premigrate backup, pruned, no double-backup | as-specced | `migrate.py:101-110` via shared `db_backup.py` (`KEEP_BACKUPS = 5`, per-prefix prune), runs before connect/pending (:123-124), failure aborts. CLI-only opt-in (`backup=True` at :175); update path keeps `preupdate` and calls `migrate(..., backup=False)` (`update.py:971-975`, pinned by test). Backup tests hermetic via `SC_DB_BACKUP_DIR`. |
| 4. Skill text routes via scaffold + documents backup; three-artifact | **deviated-intentionally** (ratified call #2, msg #1129) | Skill asset routes authors through the scaffold and documents premigrate/preupdate policy (SKILL.md:21-30, 46-49); reseed 0160 byte-matches the asset (pinned by test). The tracked `skills_sc/` mirror sentence is stale since PR #726 — ratified reading is local-only render + hermetic render-check, which exists (`render_check.py`, CI workflow render-check green on main). Scaffold provenance of 0160 itself: consistent but inferential — see Low 1. |
| Gate | as-specced | Scaffold-created migration passes `test_sprint_removal_manifest` with a pure append (git-verified); duplicate guard red on synthetic duplicate (test-proven); bare `./sc migrate` lands a pruned premigrate backup (test-proven). |

## Unit 3 — deferral doctrine

| Req | Verdict | Evidence |
|---|---|---|
| 1. FnB-caller completion defers owning Planner — test | as-specced | `test_fnb_completion_still_defers_owning_planner_run` (test_sprint_recovery.py:1209): FnB actor (shell 5, no live run of its own) completes; only the developer run is interrupted, planner run deferred with close intent. Discriminating by construction — under caller-keying the planner run would be interrupted and the test fails. |
| 2. Doctrine note + comment at keying site | **deviated-intentionally** (ratified call #3, msg #1136) | Vehicle = new doctrine doc #81 under feature 31 (exists; records decision #59 + spec #78 U3, names the regression test) instead of editing a Sprints v2 feature doc. One-line comment present verbatim at the keying site (`sprint_participant_chats.py:303-308`: "Completion defers the owning Planner even when FnB called complete."). Deferral keys on `originating_planner_shell_id`; signature takes no caller param — caller-keying is structurally impossible. |
| Gate: test fails if re-keyed to caller | as-specced | Construction argument verified against the test code (see req 1). |

## Unit 4 — test-depth sweep

| Req | Verdict | Evidence |
|---|---|---|
| 1. `work_unit.cancelled` event string asserted | as-specced | `test_cancelled_work_unit_resolves_assignment_expectation` (test_sprint_liveness.py:666) pins `resolution == "work_unit.cancelled"` end-to-end through the trigger. |
| 2. Reassignment edge in notification branch | **deviated-intentionally** (ratified call #1, msg #1110) | Boundary test only, zero behavior change: `test_reassignment_does_not_resolve_former_developer_notification` (:679) asserts the stale old-assignee notification stays unresolved; trigger code (migration 0158:28-35) untouched by the unit (git-verified). |
| 3. Help-probe extended to direct pip/npm shims | as-specced | `test_deps_help_is_read_only_and_byte_stable_from_both_checkouts` (test_worktree_targets.py:322, :341) probes bare `pip` + `npm` shims. Coverage notes: Low 4. |
| 4. Presence-loop short-circuit + string-occurrence guard | as-specced (both, per ratified call #4, msg #1143) | Short-circuit in `_sc_has_python_tests` (`sc:275-283`) + behavioral guard `test_presence_stops_after_first_matching_test`; string-occurrence guard `test_sc_test_caches_one_presence_result_for_both_gates` (test_devkit_sc.py:101) present. Note: the guard test pre-existed the unit's fix commit — REV2's "replaced, not maintained" finding was cured by restoring it; main carries both. |
| 5. `ACTIONABLE_KINDS` string deduplicated | as-specced | Single definition `ACTIONABLE_KIND_ERROR` (`sprint_message_delivery.py:24-26`), both raise sites consume it; repo-wide search shows exactly one occurrence of the string. Test-strength caveat: Low 5. |
| 6. Flags-skill doc drift + two human-output paths tested | as-specced | One-line drift fixed (`flags/SKILL.md:18` + reseed 0161, idempotency-pinned). Both previously-untested human paths now asserted (test_mem.py:288 new; :377 extended with exact 5-line stdout) — substance holds; see Low 6. |
| Gate: test per item; suite green | as-specced | Main @ 3d1f9f3 CI: tests + render-check + CodeQL all success. |

## Unit 5 — update by-catch (#935–#938)

| Req | Verdict | Evidence |
|---|---|---|
| 1. #935 exact repo-name remote match | as-specced | `super_coder_remote()` (`update.py:295-316`) — path basename equality, no substring. Test `test_update_remote_matcher_rejects_fork_name_containing_source_name` reproduces the subfloor-marketing shape; live-validated during fleet remediation (subfloor-marketing clean update). |
| 2. #936 first-attempt failure root-cause/fix | **deviated-intentionally** (ratified call #6, msg #1192) | The ratified at-minimum ordering floor shipped: `publish_engine_ref` runs only after successful migrate/map/snapshot (`update.py:1279-1281`), atomically (pending + `os.replace`); ordering regression tests pin `["migration","map_setup.py","snapshot.py","publish","reconcile"]` and old-ref retention on failure. Root-cause of the transient first-attempt failures remains — subfloor #936 open in reduced scope (annotated shipped-vs-remaining). Fleet evidence: 4/6 genuine first-attempt successes (2 refusals were a separate defect, #953). |
| 3. #937 stale worktree dispatcher | as-specced | Mechanism choice was the implementer's per the spec; the ratified mechanism (msg #1181) is what shipped: update-time reconcile of clean tracked dispatchers (`reconcile_linked_dispatchers`, update.py:784-853) + `update_compat` seam + prior engine.ref/engine.ref.prev recognition (:813-828) + dirty worktrees named by path in a WARNING, never silently skipped. Observable requirement test-proven: a stale worktree dispatcher never executes retired mutating behavior. |
| 4. #938 venv runnability probe + rebuild | as-specced | `_sc_venv_runnable()` (`sc:299-303`) probes execution, not existence; `sc_test` rebuilds a broken venv before runner selection (`sc:469-504`). Synthetic dangling-symlink test `test_sc_test_rebuilds_dangling_venv_before_running_pytest` + probe unit tests. |
| Gate: all four issues closed + tests | **deviated-intentionally** (ratified call #6) | #935, #937, #938 closed with linked commits + fleet-shape regression tests; #936 stays open in reduced scope by PLN1 ratification. |

## Unit 6 — skills sweep on forks

| Req | Verdict | Evidence |
|---|---|---|
| 1. Sweep main checkout + every linked worktree, dormant included; managed paths only | as-specced | `reconcile_skill_projections` (update.py:1111-1126, called :1256) → `reconcile_existing_checkouts` (skill_projection.py:342-374) scans `.sc-worktrees/` — filesystem enumeration is exactly what reaches dormant worktrees. Ownership: managed roots from adapter manifests; removal gated on managed-name (seed + tombstone + DB skills) or `rendered_by: super-coder` banner (:104-123). Edge observations: Lows 9-10. |
| 2. Idempotent + dirty-worktree safe | as-specced | Content-compare skip (second run a proven no-op); ref-keyed marker `update-compat-skill-sweep.ref` written only after a successful sweep (failure exits before the write, retry re-runs); managed roots are engine-gitignored (install.py:548-557, topped up by update before the sweep); name/banner gate protects shell-authored content; symlink-safe loud failure. |
| 3. Tests (dormant converges / booted correct / authored survives) | as-specced | `test_first_adoption_sweeps_main_and_dormant_skill_projections` (real git worktree fixture pinned to pre-feature-33 baseline; fails deterministically on the pre-fix shape); `test_sweep_cleans_existing_worktree_without_creating_dormant_one`; revocation/render tests; operator-owned controls survive in every fixture. |
| 4. Fleet remediation — dos-arch acceptance | as-specced | PLN1's evidence (`shared/SPRINT79_U6_fleet_remediation.md`): dos-arch post-update probe = 0 tombstoned across all 13 worktrees + main (was 9); all six installs on 3d1f9f3. By-catch (2 refusals exiting 0, not clean aborts) filed as subfloor #953 — a new issue found by remediation, not a deviation in this unit's code. |
| Gate: dos-arch probe zero + regression tests green | as-specced | See req 4; tests green on main @ 3d1f9f3. |

## Low observations (report input — none blocking)

1. **U2:** scaffold provenance of migration 0160 is inferential (generated header rewritten, no provenance marker) — filename format, append-position manifest entry, and the scaffold's own test fixture agree, but nothing asserts it directly.
2. **U2:** `migrate()` defaults `backup=False` — backup safety lives entirely at the CLI seam; any future programmatic caller that omits the keyword gets no backup. (Only in-tree callers are deliberate and tested.)
3. **U2:** `migration` dispatch in `sc` lacks the `CALLER_ENGINE` existence guard that render-check/seed-skills carry (declared in the U2 unit report as a follow-up Low).
4. **U4 item 3:** probe covers bare `pip`/`npm` only — not `pip3` / `python -m pip` (declared Low), and the bare-`pip` shim guards a regression shape: current code only ever invokes `"$venv/bin/pip"`. The `npm` shim guards a live call shape.
5. **U4 item 5:** the dedup test asserts both raise sites equal the constant — it guards divergence between paths, not the literal wording or re-duplication with identical text.
6. **U4 item 6:** "tests added for two paths" landed as one new test plus assertions appended to an existing test. Coverage is real either way.
7. **U5 #937:** dirty-dispatcher skips are WARNING-named on stderr only, not repeated in the closing stdout summary — visible, but easy to miss in captured output.
8. **U5 #938:** the unittest-discover fallback and pytest-required hard-fail branches have no behavioral tests (declared in the U5 unit report).
9. **U6:** ownership-by-name: a banner-less directory whose slug collides with *any* DB skill row (fork-local included) is `rmtree`'d without the banner check. Consistent with the engine's name-based ownership model, but wider than the documented banner mark — and engine-written projections don't carry the banner, so re-deletion relies on the name set alone.
10. **U6:** worktree enumeration is a `.sc-worktrees/` directory scan, not git-verified: a stray directory there gets swept (with `shell_id=None` semantics); a registered worktree outside `.sc-worktrees/` would be missed (engine worktrees are convention-scoped, so this is consistent today; the U5 report's `.sc-worktrees` vs `git worktree list` Low is the same shape).
11. **U1:** `resolve()` is now dead in production (only a test calls it) — kept as public API; not a defect.

## Verdict summary

- as-specced: 18 · deviated-intentionally (ratified): 5 · deviated-silently: 0 · unimplemented: 0
- **The spec shipped.** Recommend freeze; Lows above to the sprint report.
