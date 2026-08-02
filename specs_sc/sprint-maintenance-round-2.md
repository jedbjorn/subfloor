---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: true
title: Sprint maintenance round 2
tags: [sprint, maintenance, liveness, migrations, skills, update]
date: 2026-08-02
project: super-coder
purpose: Post-Sprint-74 follow-up round
---

# Sprint maintenance round 2 — atomicity, migration guardrails, update hygiene

## Overview

Follow-up round seeded by Sprint 74's conformance pass (doc #76) and sprint report (doc #77), expanded by FnB direction (2026-08-02): the four fleet-update by-catch issues come in scope, the skills-updater cleanup gets a dedicated unit with a live repro, and the migration scaffold gains the pre-migration backup policy. **Run mode: full sprint machinery, same shape as Sprint 74** (declaration, per-unit dev/review lanes, conformance pass, freeze, report). Presumptive model routing per the Sprint 74 interview — devs codex/gpt-5.6-sol, reviewers kimi family (heterogeneous pairing) — confirmed at declaration.

```stats
:::class1
value: 6
label: Units
description: 1 behavior fix, 2 update-hygiene, 1 tooling, 2 test/doc
:::class2
value: 4
label: Upstream issues closed
description: subfloor #935 #936 #937 #938
```

Source anchors verified against `~/Repos/subfloor` main @ `f442bb2`:

- `sprint_review_loop.py:219-222` — post-transaction liveness resolve (flag #123 site).
- `sprint_liveness.py:590` (`resolve`, opens its own write transaction) vs `:602` (`resolve_review_requests_for_work_unit_in_transaction`, the in-transaction pattern the #929 fix established).
- `migrations/0155_reseed_catalogue_cleanup.sql` + `0155_sprint_conversation_generations.sql` — duplicate number (conformance F6).
- `tests/test_sprint_removal_manifest.py` + `tests/fixtures/sprint_removal/manifest.json` — the allowlist both migration-bearing Sprint 74 units missed (both CI reds).
- `db_backup.py:24` `KEEP_BACKUPS = 5` with per-prefix pruning; `update.py:852` `backup_existing(prefix="preupdate")`; `migrate.py` — **no backup import or call** (the gap Unit 2 closes).
- `migrations/0154_remove_tombstoned_skills.sql` — DB-side tombstone removal (13 names), applied and verified clean on dos-arch's live DB.
- Live repro for Unit 6: dos-arch @ f442bb2, DB clean, **10 shell worktrees still carrying tombstoned flat skills** (`sprint`, `sprint_orchestration*`, `test_authoring_pg`) — dormant worktrees are unswept; dos-app is clean only because every shell booted during the Unit 4 proof.

## Unit 1 — verdict atomicity

**Fixes flag #123 (conformance F3; same defect shape issue #929 fixed for Developers).**

`record_review` commits the verdict — judgment row, `sprint_work_units.disposition`, `review.<verdict>` event, outcome receipt — inside its write transaction, then resolves the Reviewer's liveness expectation *after* the transaction closes. A crash in the gap leaves the verdict durable but the expectation open: spurious Reviewer nudges for a review already submitted.

Requirements:

1. The reviewer expectation resolve moves **inside** the verdict write transaction in `record_review`.
2. Use the in-transaction resolve pattern (`sprint_liveness.py:602` family) — `resolve()` at `:590` opens its own `write_transaction` and must not be nested. Add a narrow message-id-scoped in-transaction variant if the work-unit-scoped helper does not fit; do not widen the existing helper's contract.
3. Idempotency holds: replaying `record_review` after a committed verdict remains exactly-once.
4. Tests: (a) gap test failing on the pre-fix shape — abort between transaction commit and the old post-transaction resolve point, assert the expectation is nonetheless resolved; (b) replay/idempotency test; (c) existing review-outcome tests stay green.

Gate: a post-verdict Reviewer nudge is structurally impossible — the construction argument conformance row 23 accepted for the Developer side.

## Unit 2 — migration guardrails

**Fixes the process follow-up (both Sprint 74 CI reds) + conformance F6 + the FnB backup policy (2026-08-02).**

Authoring is fully manual (`ls | sort | tail` per the `migration_management` skill); nothing allocates numbers, nothing maintains the removal-manifest allowlist, nothing prevented the `0155` collision. Backup state today: `./sc update` backs up before applying migrations (`preupdate` class) and every backup class prunes to `KEEP_BACKUPS = 5` — but the bare `./sc migrate` path applies migrations to the live DB with **no backup at all**.

Requirements:

1. Scaffold entrypoint `./sc migration new <slug>` (subfloor source repo): allocates the next free number (collision-checked), writes the standard skeleton (`BEGIN;`/`COMMIT;`, idempotence idioms, intent-comment header), and appends the filename to the sprint-removal manifest allowlist in the same act.
2. Duplicate-number guard in the test suite: red on two migrations sharing a number prefix, with the existing `0155` pair explicitly allowlisted as frozen history. Applied migrations are never renumbered.
3. **Pre-migration backup (FnB policy):** `./sc migrate` takes a WAL-safe backup through the shared `db_backup.py` path before applying pending migrations, under its own prefix class (e.g. `premigrate`), pruned to the standard 5. The update path keeps its existing `preupdate` backup — no double-backup within one update run.
4. `migration_management` skill text routes authors through the scaffold and documents the backup behavior. Skill edit ships as the three-artifact commit (source asset + trailing reseed migration + hermetically re-rendered `skills_sc/` mirror); this unit's own migration must be created by the new scaffold, proving it end to end.

Gate: scaffold-created migration passes `test_sprint_removal_manifest` with zero manual manifest edits; duplicate guard red on a synthetic duplicate, green on the current tree; a bare `./sc migrate` run demonstrably lands a pruned `premigrate` backup.

## Unit 3 — deferral doctrine

**Records conformance F1 as intended behavior with a test.**

FnB-initiated `complete` also defers the owning Planner's live run — ruled intended (spec #73 decision 1 keys deferral on the **owning Planner**, regardless of caller); currently enforced by code nobody asserts and documented nowhere.

Requirements:

1. Test asserting an FnB-caller completion defers the owning Planner's live run exactly as a Planner-caller completion does.
2. Doctrine note in the Sprints v2 feature documentation plus a one-line comment at the deferral keying site.

Gate: the test fails if deferral is ever re-keyed to the caller.

## Unit 4 — test-depth sweep

**Clears conformance F4/F5/F7 and the Sprint 74 unit-report Lows.** No behavior change.

1. Assert the `work_unit.cancelled` event string (F4).
2. Cover the reassignment edge in the notification branch (F5).
3. Extend the CLI help-probe to the direct `pip`/`npm` shims (F7).
4. Presence-loop: short-circuit instead of full-walk, plus the string-occurrence guard test.
5. Deduplicate the `ACTIONABLE_KINDS` invariant string to a single source.
6. Flags-skill: fix the one-line output doc drift; add tests for the two untested human-output paths (three-artifact commit rule if skill text changes).

Gate: each item lands with a test that failed (or a hole that existed) before the change; suite green.

## Unit 5 — update by-catch

**Closes subfloor #935, #936, #937, #938 — all found during the Sprint 74 fleet update, all live.**

1. **#935 remote matcher:** `super_coder_remote()` (`update.py:292`) substring-matches remote URLs against `SOURCE_REPO_NAMES`, so `subfloor-marketing`'s own origin matches "subfloor" and outranks the correctly configured upstream remote. Fix: exact repo-name match (path base-name equality, not substring). Test with a remote set reproducing the subfloor-marketing shape.
2. **#936 transient first-attempt failures:** 3 of 6 installs failed their first `./sc update` (snapshot.py failure; `FOREIGN KEY constraint failed` after engine.ref had already advanced) and succeeded on identical rerun. Root-cause and fix so a first attempt succeeds where the rerun would; at minimum the failure must not leave `engine.ref` advanced past the failed step (ordering/atomicity of ref-write vs migrate+snapshot). Regression test for the identified cause.
3. **#937 stale worktree dispatcher:** after an in-place update, linked shell worktrees keep their branch's old tracked `sc`, so already-fixed dispatcher behavior (the #926 mutating `deps --help`) persists there. Fix per the issue's analysis — the update reconciles or the dispatcher self-detects staleness in linked worktrees; the mechanism choice is the implementer's with rationale recorded, but the observable requirement is fixed: post-update, a linked worktree's `./sc` must not execute retired mutating behavior.
4. **#938 provisioning self-heal:** `./sc test` gates on pytest *existence*, not runnability — a `.venv` whose python symlink dangles (host python upgraded) strands nested-only forks on stdlib tests. Fix: probe runnability; rebuild the venv when broken. Test with a synthetic dangling-symlink venv.

Gate: all four issues closed with linked commits + tests; fleet-shape regression cases in the suite.

## Unit 6 — skills sweep on forks

**Makes upstream skill removal reach every engine-managed flat projection — the feature #33 contract — with a live repro.**

Live state (verified 2026-08-02): dos-arch @ f442bb2, migration 0154 applied, DB tombstone-clean — yet 10 of its shell worktrees still hold tombstoned flat skills (`sprint`, `sprint_orchestration*`, `test_authoring_pg` under `.claude/skills/`). Boot re-renders a shell's projection, so only booted shells converge (all of dos-app's did during the Unit 4 proof); **dormant worktrees are never swept by `./sc update`**.

Requirements:

1. `./sc update` sweeps managed skill projections in the main checkout **and every linked shell worktree, dormant ones included**: tombstoned/retired skill dirs removed from each worktree's managed projection paths, current skills laid down or left for next boot per the projection's ownership rules. Only engine-managed paths are touched — never shell-authored content.
2. The sweep is idempotent and safe on dirty worktrees (removal of a managed projection dir is not "dirt" it must preserve).
3. Tests: a fixture install with a dormant worktree carrying a tombstoned projection converges on update; a booted-shell projection stays correct; shell-authored files under adjacent paths survive.
4. **Fleet remediation:** after merge, run the fixed update across the installed forks; acceptance includes dos-arch's worktrees probing clean for all 13 tombstoned names.

Gate: the dos-arch repro probe (13 tombstoned names across all worktrees) returns zero hits post-remediation; regression tests green.

## Sequencing

Dependency-light: Units 1 and 3 share the liveness/completion surface; Units 5 and 6 share `update.py`. Everything else is independent.

```linear
Declare + arm :::class1 -> U1 ‖ U2 ‖ U4 first wave :::class2 -> U3 after U1 · U5 after U2 · U6 after U5 :::class2 -> Conformance :::class3 -> Freeze + report :::class3
```

- **Wave 1 (parallel):** U1 (atomicity), U2 (migration guardrails), U4 (test sweep).
- **Wave 2:** U3 after U1 (same liveness surface, avoids merge friction); U5 after U2 (U5's fixes ride the scaffold for any migration they need); U6 after U5 (both edit `update.py`; serialize to avoid conflicts).
- Every unit is single-dev, single-PR. Suggested lanes: 3 devs (A: U1→U3, B: U2→U5, C: U4→U6-tests with U6's update.py change landing after B's U5 merge — or fold U6 into lane B if 2 devs preferred), heterogeneous cross-model reviewers per L&S.
- Skill-asset edits (U2, possibly U4 item 6): land their reseed migrations in separate ordered commits, both allocated by the new scaffold.
- Unit 6's fleet remediation is a post-merge Planner/maintainer step inside the sprint window, evidence captured durably (command output to a durable row/file — the doc #77 U4-step-2 lesson).

## Decisions taken

FnB-directed, 2026-08-02 (supersede nothing; spec #78's original open questions are resolved by these):

1. **By-catch in scope** — #935–#938 are Unit 5; all four closed this round.
2. **Scaffold vehicle** — `./sc migration new <slug>`.
3. **Backup policy** — a DB backup precedes *every* migration application path (update already compliant; bare `./sc migrate` gains it), retention bounded at 5 per lifecycle class (existing `KEEP_BACKUPS`), within the FnB's 5–10 cap.
4. **Run mode** — full sprint machinery, Sprint 74 shape; presumptive Sprint 74 model routing, confirmed at declaration.
5. **Skills sweep** — dormant-worktree convergence is an update-time responsibility, not a boot-time eventuality (Unit 6).

## Non-goals

- No Sprints v2 behavior redesign beyond Unit 1's atomicity fix.
- No renumbering or rewriting of applied migrations (the `0155` pair stays; the guard prevents the next collision).
- Frozen spec #73 untouched — its text corrections remain recorded in report doc #77.
- No boot-time skill-projection redesign — Unit 6 fixes update-time convergence only.
- No new backup subsystem — Unit 2 wires the existing `db_backup.py` path into `migrate.py`.
