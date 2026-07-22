# Sprint 21 · Unit 8 — PR #465 review (REV2)

- **PR:** #465 `docs: make sprint wake provider-neutral` @ f9d3680, branch `chore/sprint-provider-conformance`, base `main`
- **Spec:** doc #20 (feature 14), task #57 — sprint workflow + provider conformance
- **Scope reviewed:** sprint + sprint_orchestration skill sources, reseed migration 0079, rendered `skills_sc/` mirrors + README
- **CI:** all green (tests, verify, render-check, CodeQL ×3)
- **Verdict:** **review-clean** — 0 Major, 0 Medium, 3 Low

## What was verified (not trusted)

1. **Migration 0079 exactness (the core risk — `replace()` reseeds no-op silently on mismatch).**
   Programmatically parsed all 16 UPDATE statements (14 content replaces + 2
   description sets, SQL-literal-aware, `''`-unescaped) and checked against
   `origin/main` (pre) and PR head (post) assets:
   - every old-string appears **exactly once** in the pre-asset, zero times post;
   - every new-string appears exactly once in the post-asset **and** in the
     rendered `skills_sc/` mirror;
   - applying all replaces + description swap to the pre-assets reconstructs
     both post-assets **byte-identically** — full hunk coverage, nothing edited
     in the asset that the migration misses.
2. **Fresh-rebuild convergence.** `tests/test_skills_freshness.py` builds
   schema + every migration and asserts zero stale engine skills vs assets;
   green on this branch proves the 0001→0079 chain lands identical to the new
   assets. Existing-fork drift additionally self-heals via
   `sync_engine_skills` (same mechanism 0076 relies on).
3. **Migration hygiene.** 0079 is the next free number on main (main tops at
   0078); BEGIN/COMMIT + comment style matches the house reseed pattern
   (0072/0074/0076); WHERE name= scoping correct; no ambiguous old-strings.
4. **Doc claims vs executable reality** (`origin/main` code, not the PR text):
   - `sc session status [shortname]` / `manage <shortname> --sprint <ref>`
     (required) / `release <shortname> [--after-turn]` / `retry <shortname>`
     all match `session_cli.py` argparse exactly.
   - The close-out instruction "do not wait on the currently executing turn;
     have the operator run `release --after-turn` after this turn returns" is
     **correct and load-bearing**: `--after-turn` is a blocking wait-loop in
     the CLI, so a planner releasing its own binding mid-dispatched-turn would
     self-deadlock. The text routes around a real hazard.
   - Binding lifecycle bullets match the schema's seven states and dispatcher
     semantics verified in units 2–4/6; watcher-as-active-channel framing
     matches the unit 4 adapter (README accurate).
5. **False-claim removal within skills.** Repo-wide sweep: no remaining
   "watcher wakes you"/fire-and-wake claims in any `assets/skills/` file;
   Claude/Fable planner recommendation removed from sprint_orchestration.
   README_sc mirror description lines byte-equal the new frontmatter.
6. **Release constraints honestly framed.** Live gates (per-provider smokes +
   one real sprint each) deferred per PLN1 ruling msg #261; PR body states
   them as constraints, not passes; spec #20 stays unfrozen. Conforms to the
   ratified ruling — not treated as passes here either.

## Findings

**Low L1 — `docs_sc/job-runner.md` still teaches the false wake model.**
"**Fire-and-wake (default).** … The completion `result` row wakes you through
the normal inbox path — nothing polls, nothing is parked on the session."
For an ordinary (unmanaged) worker nothing delivers that row — the exact claim
this PR removes from the sprint skill, and the two surfaces now contradict
each other. Outside unit 8's spec scope (spec #20 surfaces row says *Skills*;
this is a `documents` row), so a follow-up, not a gate — but it should be
corrected before the conformance pass reads "remove false wake claims"
broadly. Same fix shape as the sprint skill's "Detached completion" rewrite.

**Low L2 — `docs/README.md` carries the superseded planner-wake model.**
~Lines 428–432 ("run the planner on Claude … the only role the inbox watcher
fully serves"), 634–638 ("its exit wakes the live planner session"), and 791
("the planner's zero-token wake"). All superseded by spec #20's managed
binding plane. Repo doc, outside unit scope — follow-up cleanup.

**Low L3 — wrap-width nit.** Sprint skill step 3 rewrap leaves "Upstream
visibly stalls from where you sit -> `result` row to the planner; don't sit"
over the file's ~74-col wrap. Cosmetic.

(Note, not a finding: "the session dispatcher delivers those unread events
only to the planner's managed binding" is precise-in-context — delivery goes
to any *managed* binding, and only planners are managed in a sprint.)

## Recommendation

Unit 8 is review-clean on its hermetic scope. DEV4 holds scoped merge
authority (green + clean + ACTIVE doc). L1/L2 to the planner as sprint-report
follow-ups — suggest a doc-cleanup item (job-runner + README) before the
freeze, since the conformance pass will otherwise surface both as
`deviated-silently` under a broad reading of "remove false wake claims".
