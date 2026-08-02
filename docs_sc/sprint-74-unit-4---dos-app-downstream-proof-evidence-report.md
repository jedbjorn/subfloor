---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
---

# Sprint 74 Unit 4 — downstream-style proof on dos-app: evidence report

Author: PLN2 (home planner seat, unit 4 owner). Report-only unit per spec #73
Ratified decision 3 — no code PR. Proof surface: `~/Repos/dos-app` fork,
native `sc sprint` end-to-end (dos-app sprint 4, feature 22). All timestamps
UTC (dos-app engine DB rows).

## 1. Surface and floor

- Engine repin 9dea86b -> f442bb2 (subfloor main @ sprint-74 units 1–3:
  #931 CLI hygiene, #933 terminal/liveness, #934 role skills + flag
  evidence). `.sc-state/engine.ref` = `f442bb23668ee0c942a32697fd7e55a4ae2a55c3`.
- Migrations 0157–0159 each applied **exactly once**, 2026-08-02 07:06:31
  (`schema_migrations`, count=1 per file). DB backed up pre-update
  (`shell_db.preupdate.20260802_090631.db`). Repin tracked as dos-app PR #59.
- Engine service (container `sc-dos-app`) restarted onto the new floor at
  07:07:27Z and stayed up through close-out.

## 2. Environment event — double engine lay (not a defect)

At ~07:13Z PLN1 independently ran `./sc update` in dos-app during an
FnB-directed fleet sweep, unaware it was this unit's proof surface — same
target ref f442bb2 (disclosed in task #1081; dos-app then excluded from the
sweep). Bounded state check performed:

- `engine.ref` == f442bb2; `engine.ref.prev` **also** f442bb2 — the second
  lay's fingerprint (prev overwritten with the identical ref).
- Second lay applied **zero** migrations (0157–0159 timestamps unchanged at
  07:06:31, one row each) — idempotent floor lay confirmed.
- Pre-arm rows intact: sprint 4 declared 07:22:14 (QAQC approval id 7, pass,
  07:21:42 — pre-declaration), armed 07:22:28, participants 10/11/12 with
  persistent conversations, all consistent post-re-lay.
- Running service started 07:07:27Z — before the ~07:13Z re-lay — but the
  re-lay was content-identical (same ref, no migrations), and the whole
  sprint (07:22 -> 07:46) ran on this floor with zero anomalies. Logged as an
  environment event per PLN1's ruling; no defect, no restart warranted
  post-terminal.

## 3. Native sprint run — event trail (dos-app sprint 4)

sprint.declared 07:22:14 (FnB) → work_unit.created/ready + lifecycle.armed
07:22:28 (planner shell 3) → work_unit.accepted 07:22:58 (DEV1 shell 4) →
pr.registered #60 07:28:28 → checks green → review.requested 07:30:56 →
**dev->review liveness handoff**: wake to REV1 queued 07:30:56, delivered
07:30:58, review.approved 07:36:07 (judgment 8, APPROVED @ b21def6c) →
merge.authorized 07:38:06 → PR #60 squash-merged 07:38:23Z →
work_unit.completed via pr.merge_observed 07:38:24 → conformance.recorded
07:44:14 (REV1, report 7, PASS, zero findings) → final_report.recorded +
lifecycle.completed 07:46:02 (planner shell 3, report 8). Delivered: one
test-only PR (+14/-0, `test_health_ignores_invalid_api_key`), integrated
main `eae22084`.

Wake health: 7/7 outbox rows delivered, every one on attempt 1, all under
idempotency keys; no pending or failed rows.

## 4. #923 contract verification (terminal liveness, unit-1 fix)

Subfloor #923: sprint completion used to cancel the planner turn that
performed close-out. Verified fixed on this run, from durable rows:

1. **Terminal live response** — planner close-out run 78
   (cv_c22896c88a404d99a376c76b8425227b, 07:44:47 -> 07:46:15) state
   `succeeded`, no error. It performed the complete call at 07:46:00–07:46:02
   and then **streamed its full close-out summary at 07:46:15 — 13s after
   `lifecycle.completed`** — ending with `run.completed: succeeded`. The
   pre-fix failure mode (caller marked `cancelled`) did not occur.
2. **Exactly-once report** — exactly one `final` row (report 8, key
   `pln2-s4-complete-v1`) and one `conformance` row (report 7, key
   `rev1-s4-conformance-pass1-eae22084`); a single `final_report.recorded`
   event (id 71).
3. **Closed conversations** — all four sprint-4 conversations `closed`:
   three at 07:46:02 (terminal cleanup), the **caller's deferred to
   07:46:15** and closed with `{"recovered": true}` only after its run
   completed — the deferral shipped in #933, observed live.
4. **Idempotent retry** — exercised natively by REV1: report 7 recorded
   07:44:14, then deliberately replayed ("Report recorded (id 7)…
   Replaying to confirm idempotency" 07:44:21 → "Idempotent replay
   confirmed" 07:44:33); table still holds one row. Completion itself
   carried idempotency key `e05a7a31996d4d2445634705fad69551` on the
   sprints row.
5. **No post-terminal command** — last wake queued 07:44:38 / delivered
   07:44:43 (pre-terminal); the final sprint event is `lifecycle.completed`
   (id 72) — nothing after; wake outbox empty of pending work; run 78 closed
   with "no further Sprint commands will run — Sprint-scoped authority is
   over."

## 5. Resolved-flag reads (unit-2 fix, exercised natively)

Judgment 8 (REV1 review verdict) quotes dos-app flag #15's closure notes
**verbatim** via both required views (`sc mem get flags 15` and
`sc mem get flags --feature 22 --resolved`). Independently re-read
`flags.resolution_notes` for flag 15 and diffed against the quote:
word-for-word identical. Conformance report 7 re-confirms (requirement 7,
AS-SPECCED).

## 6. Remaining unit checks

- **QAQC pre-decl** — QA/QC approval id 7 (pass) at 07:21:42, before
  sprint.declared 07:22:14; spec doc #16 frozen, revision sha256
  `0a868266…` bound at declaration and byte-identical at close-out
  (recomputed by REV1).
- **Deps/test hygiene** — integrated diff is test-only (+14/-0, one file);
  zero dependency/config/schema changes (conformance reqs 2, 5); suite
  conventions matched (req 3); hosted checks green at reviewed head.
- **Complete-from-conversation** — `sc sprint complete` issued from the
  linked persistent planner conversation (participant 10's
  persistent_conversation_id == the conversation that ran close-out).
- **Conformance + final** — report 7 (PASS, zero findings, zero follow-ups),
  report 8 (success — shipped as specced). No pauses, recoveries, failed
  wakes, nudges, or escalations.

One environment fact carried, not smoothed: 14 pre-existing permission-suite
setup errors from a fixture hardcoding `/workspace/designs_os/designs_os.db`
(absent in worktree sandboxes) — recorded independently by DEV1, REV1, and
the conformance report as out-of-scope; fixture-path cleanup recommended
outside the sprint.

## Verdict

The native sprint machinery repaired in units 1–3 ran a real downstream
sprint end-to-end with **zero human intervention, zero scheduled polling,
zero anomalies**, and the #923 contract held on every item. Unit 4 proof:
**PASS**.

Evidence sources: dos-app engine DB (`sprint_events`, `sprint_reports`,
`sprint_judgments`, `sprint_participants`, `sprint_wake_outbox`,
`conversations`, `conversation_runs`, `conversation_events`,
`schema_migrations`, `flags`), `.sc-state/engine.ref{,.prev}`, container
state, dos-app PRs #59/#60.


## Supplement — step 2 proof

REV3 conditional-pass remedy (finding 1, flag #122 / SC-74-U4; PLN1 ruling,
task #1086): spec #73 step 2 lacked durable command-output evidence. Both
commands re-run 2026-08-02 ~10:09–10:20Z by PLN2 on the dos-app surface. Raw
captures (outputs, exit codes, sha256, filesystem sweeps, shim log):
`dos-app shared/sprint74-u4-supplement/` (gitignored drop zone). This section
is the primary durable copy.

### sc deps --help (#926) — PASS

Authoritative surface = container `sc-dos-app` (engine f442bb2), repo root:

- Runs 1+2: **exit 0**, output exactly `Usage: ./sc deps [-h|--help]`,
  **byte-identical** — sha256 `9b692d731b1ca65613955f4deffda2048818701a
  fbabc29544200ed0859d5497` on container run 1, container run 2, AND the
  host-root run (byte-stable across repeats and across host/container).
- **No filesystem side effects**: full pre/post mtime+size sweep of the repo
  (capture dir excluded) — empty diff. No `.venv` touch.
- **No pip/npm processes**: PATH fronted with logging shims for
  pip/pip3/npm/pnpm/yarn/uv — zero hits across all runs.

Control run, pre-fix engine: `.sc-worktrees/dev1` still pins old engine
`9dea86b` (repin PR #59 lives on `chore/repin-engine-f442bb2`, not on the
worktree's branch), and its `./sc deps --help` reproduced defect #926
verbatim — printed `→ deps: creating …/dev1/.venv`, attempted venv creation,
exit 1. Environment fact carried, not smoothed: dev1's `.venv/bin/python3`
was **already dangling before the run** (symlink → `/usr/local/bin/python3`,
a container-only path — the venv was provisioned in-container and is
unusable from the host regardless), and the old-engine attempt additionally
rewrote `pyvenv.cfg` (now 3.14.6) and added `python3.14`/`𝜋thon` symlinks
plus an empty 3.14 `site-packages`. dev1's venv needs reprovisioning on the
new floor — recommended outside this unit.

### bare sc test (#774) — PASS (gate); exit 2 carried as-is

Container `sc-dos-app`, repo root, **exit 2**:

- `→ test: /home/j3d1/Repos/dos-app/.venv/bin/pytest` — the pruned presence
  gate **detected the nested Python suite** below `designs_os/api/tests`
  and **selected the existing pytest** (pytest-9.1.1). The pre-fix failure
  mode — reporting no tests — did not occur.
- One repository-root pytest invocation (`rootdir: /home/j3d1/Repos/dos-app`):
  `collected 14 items / 3 errors`. All four nested modules were reached:
  `test_permissions.py` collected (14 items);
  `test_crypto.py` / `test_health.py` / `test_strict_query_params.py` failed
  import with `ModuleNotFoundError: No module named 'api'` (repo-root
  `sys.path` vs the fork's `api.*`-relative imports); pytest interrupted on
  collection errors → exit 2.
- Gate reading: detection, pytest selection, and the single root invocation
  are exactly the specced behavior; execution-time collection divergence is
  expressly "accepted behavior, not a gate failure" (spec #73, nested
  discovery — import-root policy is a stated non-goal). Exit 2 is pytest's
  collection-error code, recorded faithfully.
- Host-root control run: exit 1 — the host sees the container-provisioned
  venvs as broken (dangling python3), so `sc test` fell back to stdlib
  `unittest discover`, which errored (`Start directory is not importable:
  'tests'`); `aimail/service` `npm run test` = `Error: no test specified`.
  The host is not the sprint's proof surface; environment fact only.

### Durable stores

- Raw command outputs, exit files, hashes, fs sweeps, shim log:
  `shared/sprint74-u4-supplement/` on the dos-app surface.
- dos-app engine DB: narrative line appended to the active ADM1 archive
  pointing at this capture.
- This doc (#75) supplement — primary durable evidence; flag #122 closed
  against it.
