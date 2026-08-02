---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: true
---

# SPRINT: F33 Skill catalogue convergence
status: CLOSED
closed: 2026-08-02 · conformance: doc #71 (0 Major / 0 Medium / 5 Low) · main @ 9668def
declared: 2026-08-01 · planner: PLN1
models: devs=codex/gpt-5.6-sol · reviewers=kimi/kimi-code/k3

Feature #33 · spec doc #69 · tasks #231–#237 · base: main @ 446dc39 (includes PR #904).

| seq | unit | shell | reviewer | depends on | branch | pr | status |
|---|---|---|---|---|---|---|---|
| 1 | Task #231 — baseline pin + dirty-fork fixture | DEV4 | REV1 | — | feat/skill-convergence-dirty-fixture | #911 | merged |
| 2 | Task #232 — tombstone registry + shared DB reconciler | DEV5 | REV2 | — | feat/f33-tombstone-reconciler | #912 | merged |
| 3 | Task #233 — migration, rebuild, update, snapshot convergence | DEV5 | REV2 | 1, 2 | feat/f33-lifecycle-convergence | #913 | merged |
| 4 | Task #234 — catalogue source removals + compatibility rewrites | DEV6 | REV1 | 3 | feat/f33-catalogue-source-removals | #915 | merged |
| 5 | Task #235 — curation governance, recommendations only | DEV6 | REV1 | 4 | feat/f33-curation-governance | #920 | merged |
| 6 | Task #236 — exact managed projection reconciliation | DEV5 | REV2 | 3 | feat/f33-exact-projection-reconciliation | #917 | merged |
| 7 | Task #237 — adversarial downstream verification + release gate | DEV4 | REV3 | 5, 6 | chore/f33-release-gate | #924 | merged (verification complete: gate re-run green @ 9668def, REV3 evidence sign-off clean, #119 closed) |
| 8 | FIX (flag #119) — bound reconcile_root root-sweep deletions to engine-managed dirs, per J2 | DEV5 | REV2 | — | fix/f33-root-sweep-ownership | #928 | merged |

Sequencing notes:
- Units 1 and 2 run in parallel from kickoff (disjoint surfaces: fixture vs registry/reconciler).
- Unit 3 needs unit 2's reconciler API and unit 1's fixture for the stale-snapshot idempotency proofs.
- Units 4→5 chain on DEV6 (both touch skill assets + trailing reseed migrations + the rendered mirror — serialized to avoid three-artifact merge conflicts). Unit 6 runs parallel to that chain on DEV5 (projection reconciler surface is disjoint from asset rewrites).
- Unit 7 is the release gate — runs only after everything else is merged; REV3 gates it and later runs the conformance pass.
- Trailing migrations are append-only: each unit that adds one numbers it against main at branch time and renumbers on rebase if a sibling landed first.

CI notes: #911 and #912 first runs both red on the same invariant — tests/test_sprint_removal_manifest.py coverage (new files referencing retired sprint/tombstone names must be inventoried in the baseline manifest). Real, not anomalous; hidden locally while files were untracked. Both devs fixed in one loop; both PRs green @ 868786b / d385eb4. Both fixes touch the same manifest — later merger rebases.

Judgement calls:
- J1 (unit 2, DEV5, msg #834, ruled #835): active/tombstone disjointness is staged — registry carries all 13 tombstones and the reconciler removes all 13 from day one, but strict disjointness validation tolerates seed-owned overlap for the 3 not-yet-deleted names (engine_surgery, test_authoring_pg, test_authoring_sqlite) until unit 4 regenerates the seed. Ruling: UPHELD with bound — tolerance scoped to generated-0001 seed membership only, never fork-local rows; unit 4 removes the tolerance and adds a negative test.

- J2 (unit 6, DEV5, msg #901, ruled #903): repo-root projection is not exact — the root has no resolved-grant owner, so it preserves live-catalogue names and removes only engine-managed non-live directories; known shell worktrees stay exact per-grant. Ruling: UPHELD with bound — root deletions limited to engine-managed (banner-owned / active-namespace) directories, never foreign files; asymmetry declared in the unit report for conformance.

- J3 (flag #119, REV3 probe on unit 7, ruled #954): merged unit-6 reconcile_root deletes foreign non-namespace dirs at repo root — violates ratified J2 and spec's never-delete-unmarked-foreign-files rule. Ruling: J2 NOT amended; code is the defect. Fix unit 8 inserted (DEV5, REV2 reviews) under still-ACTIVE authority; unit 7 adds the missing foreign-dir gate control and re-runs the full matrix against post-unit-8 main before REV3 re-review.

- J4 (unit-7 evidence audit, DEV4 msg #995, ruled #997): exactness is defined over engine-managed directories only — unmarked foreign dirs are preserved in shell worktrees and at the repo root alike (spec's never-delete-foreign rule is universal). REV3's #952 worktree-removal expectation overruled by spec text; J2 read accordingly. Gate evidence: main 9668def, jobs 50/52/53 all green.

Event plumbing: PLN1 inbox watcher (home `./sc watch inbox`, backgrounded) + PLN1-owned GitHub poller on jedbjorn/subfloor (own poller per FnB directive — supplements pr_event rows; detail reads stay `gh`).
