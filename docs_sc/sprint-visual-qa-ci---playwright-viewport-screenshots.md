---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: true
---

# SPRINT: Visual QA CI — Playwright viewport screenshots (feature #19)
status: CLOSED
declared: 2026-07-20 · planner: PLN1
models: devs=codex/gpt-5.6-sol · reviewers=claude/fable

Spec: doc #13 (feature #19). Decision #3 fixes the v1 shape: capture-only
galleries, advisory check, sticky PR comment + artifact only.

## WE ARE UPSTREAM — read before touching anything

This repo is the **canonical subfloor/super-coder source repo**, the root all
forks materialize from. Here `.super-coder/` is **git-tracked engine source**
(212 tracked files) — **the engine IS the project**. The boot doc's
"never edit `.super-coder/`" rule is fork-generic text and does not apply to
tracked engine source in this repo. Engine changes are authored **here,
directly** — never "filed upstream" (there is no further upstream).

Path mapping — the spec's Change Surface names engine-relative paths; in this
repo they live at:

| Spec path | Actual path here |
|---|---|
| `scripts/visual_qa.py` | `.super-coder/scripts/visual_qa.py` |
| `templates/fork/subfloor-visual-qa.yml` | `.super-coder/templates/fork/subfloor-visual-qa.yml` (create `fork/`) |
| `templates/fork/visual-qa.example.json` | `.super-coder/templates/fork/visual-qa.example.json` |
| `sc` dispatch | `sc` (repo root) |
| `install.py` / `init_fork.py` / `update.py` / `engine_manifest.py` | `.super-coder/scripts/` |
| tests | `tests/` (repo root — hermetic, stdlib+pytest, no network/browser) |

Still off-limits: the gitignored runtime artifacts — `shell_db.db*`,
`instance.json`, `run/`, `logs/`.

## Board

| seq | unit | shell | reviewer | depends on | branch | pr | status |
|---|---|---|---|---|---|---|---|
| 1 | Runner: `visual_qa.py` (ci/run/init) + `sc` dispatch + runner unit tests | DEV3 | REV1 | — | feat/visual-qa-runner | #442 | merged |
| 2 | Distribution: shim + example-config templates, install/init_fork seeding, `update.py ensure_workflows()` reconcile, `engine_manifest.py`, reconcile tests | DEV4 | REV2 | — | feat/visual-qa-distribution | #438 | merged |
| 3 | Conformance fix unit — F1 gallery/ collision: prepare_gallery into publish path, `output` config key, ci error wording | DEV3 | REV1 | 1, 2 | fix/visual-qa-gallery-output | #443 | merged |

No dependency edge — the units meet only at the spec-defined contracts (the
shim invokes `./sc visual-qa ci`; the config schema is fixed by the spec).
They run fully parallel.

## Unit contracts

**Unit 1 (DEV3):** everything under spec §Runner — config load/validate,
skip rules, neutral-pass paths, services/setup/serve/ready loop, capture loop
with per-route failure rules, gallery + `summary.json` + `index.html`, sticky
comment build + graceful comment-post failure, `run` and `init` modes,
`sc visual-qa` dispatch + help line. Tests: config validation, skip logic,
comment-body build, gallery/summary assembly with a mocked capture layer.
The Playwright version pin lives in the runner.

**Unit 2 (DEV4):** everything under spec §Workflow shim and §Distribution —
the ~30-line shim yml with managed marker, triggers, least-privilege
permissions, concurrency group, engine-materialize-at-`engine.ref` step,
artifact upload (always); the commented example config; seeding in
`install.py`/`init_fork.py` (with `is_source_repo()` guard); update-time
`ensure_workflows()` with the four reconcile rules; `engine_manifest.py`
template paths. Tests: marker/version reconcile rules, source-repo guard,
seeding. Planner ruling: the shim's Playwright browser-cache key must be
derived from the materialized engine at CI time (e.g. `hashFiles` of the
runner) — never a version hardcoded in the shim; the shim stays boring.

## Log

- 2026-07-20 declared; both units boot immediately (parallel).
- 2026-07-20 kickoff: task rows #11–14 sent. DEV4 booted headless
  (codex/gpt-5.6-sol) — unit 2 building. DEV3 boot refused by liveness guard
  (live session, wrapping feature #18) — task row #11 waits in its inbox;
  unit 1 stays `waiting` until DEV3 picks it up or its session ends (then
  re-boot). Watcher armed.
- 2026-07-20 15:27 DEV4 result #16: unit 2 building, no blockers. Ambiguity
  call: spec has no task ledger → DEV4 creates a unit-2-scoped
  prep→implement→verify plan, leaving unit 1 independently owned. Planner
  ruling: stands (silent affirm).
- 2026-07-20 15:40 DEV4 result #20: unit 2 pr-open — PR #438
  (feat/visual-qa-distribution), checks running. Ambiguity call: example
  seeded inactive at .sc-state/visual-qa.example.json so absent live config
  stays neutral-pass. Planner ruling: stands — matches spec (live config is
  fork-authored / init-scaffolded; absent config = neutral). Suite 455 pass
  host-mode; 3 vm-bake sandbox fails are the known unrelated #435.
- 2026-07-20 15:42 unit 2 in-review — checks green @026749b (pr_event #23,
  result #22). REV2 booted (claude/fable); review request in its inbox.
  DEV4 session closed clean (168k tokens, 11 regression tests added,
  455/455 suite).
- 2026-07-20 15:48 REV2 result #25: unit 2 review-clean — 0 major, 0 medium,
  4 Lows for the report: (1) example config is strict JSON vs spec's
  "commented example" (undeclared, defensible); (2) ensure_workflows lacks a
  missing-template guard (unreachable today); (3) shim 62 lines vs spec ~30 —
  inline retention-resolution duplicates config parsing shim-side, retention 0
  coerces to 14; (4) update-time example seeding beyond the four rules is
  spec-silent scope, preserves existing files. Task #26 → DEV4: merge #438 +
  unit report; DEV4 re-booted.
- 2026-07-20 15:51 unit 2 MERGED @026749b (pr_event #28, watch retired).
  Unit report filed whole as result #27 — deviations honest, match REV2's
  Lows; no CI reds, no fix loop. Unit 2 complete.
- 2026-07-20 15:53 DEV3 boot retried, liveness guard refused again — session
  live and demonstrably active on f18 (msgs 15:35/15:38). Task row #11 waits;
  not a stall yet. Next: re-boot when session ends; escalate only if it goes
  silent while refusing boots.
- 2026-07-20 ~16:5x DEV3's pre-sprint codex session (f18) stayed live-but-idle
  post-#437-merge with task #11 unread ~45min; FnB directed reboot. Killed
  pid 4969 (worktree clean, #437 merged — nothing at risk), booted fresh:
  session 0004, codex/gpt-5.6-sol. Unit 1 → building.
- 2026-07-20 18:22 DEV3 result #30: unit 1 building, no blockers; unit-scoped
  tasks #6–#11 added to spec 13 (same ledger pattern as unit 2 — stands).
- 2026-07-20 18:24 DEV3 result #31: unit 1 prep done. Ambiguity calls, both
  stand: (1) route success = any viewport yields 200 + screenshot — so
  "all routes failed → fail check" keeps meaning "app not serving";
  (2) unresolvable PR base diff → run capture, never a false neutral skip
  (fail-open toward QA on an advisory check).
- 2026-07-20 18:34 DEV3 result #32: unit 1 verification — 29 focused
  visual-QA tests + compile + shell syntax + Ruff green; full suite riding
  job 3-visual-qa-unit1-tests (timeout 900s).
- 2026-07-20 18:36 DEV3 result #34: local suite 489/3 — the 3 are the known
  sandbox-only vm-bake #435; host-mode rerun in progress (matches unit 2).
- 2026-07-20 18:37 CROSS-UNIT DEFECT (DEV3 result #35): merged unit-2 shim
  doesn't export github.token → runner has no GITHUB_TOKEN, sticky comments
  always skip. Planner ruling (task #38): unit 1 authorized to carry the
  two-line seam fix in the shim template + managed marker bump v1→v2 (so
  reconcile rule 2 refreshes already-seeded shims) + explicit callout in the
  PR for REV1. Goes to Spec Accuracy / Issues in the report.
- 2026-07-20 18:38 DEV3 result #37: unit 1 host-mode full suite green
  (job 4, exit 0).
- 2026-07-20 18:43 unit 1 pr-open — PR #442 (feat/visual-qa-runner), watch
  registered. Includes authorized github.token seam fix + shim marker v2
  bump (result #40).
- 2026-07-20 18:44 unit 1 in-review — PR #442 all checks green (result #42);
  REV1 booted (claude/fable), review request + seam-fix callout in its inbox.
- 2026-07-20 18:51 REV1 result #45: unit 1 → fixing. 0 Major, 1 Medium
  (SC-006: unguarded rmtree of gallery output dir; sentinel-guard fix
  proposed, handed direct to DEV3). 5 Lows for the report, incl. two spec
  follow-ups: (a) networkidle all-routes-fail can red a serving app;
  (b) "local mode picks a free port" contradicts run-as-capture-only. All
  three ambiguity calls verified as declared; seam fix verified against
  merged reconcile logic. DEV3 re-booted for the fix.
- 2026-07-20 19:01 SC-006 fixed @6bbd512 (results #51 + pr_event #52):
  sentinel approach — preserve non-runner gallery/ contents, validate
  runner-owned output before cleanup; regression coverage added; 501/501
  host suite, all 6 checks green. Unit 1 back in-review; REV1 re-booted for
  delta re-review. DEV3 also flagged stale map entries to CART1 (map heal —
  outside sprint scope, CART1 session live, will pick up on next inbox check).
- 2026-07-20 19:05 REV1 result #54: delta re-review → review-clean. SC-006
  verified (sentinel-guarded rmtree + regression tests) and closed. 3 new
  Lows for the report: interrupted-run gallery needs manual clear
  (fail-closed); --output wording in ci error message; pre-existing symlink
  rmtree OSError. Task #55 → DEV3: merge #442 + unit report; DEV3 booted.
- 2026-07-20 19:08 unit 1 MERGED @6bbd512 (pr_event #56, watch retired).
  Unit report filed whole as result #57 — deviations honest (single
  intentional deviation = the authorized seam fix), 8 report-only Lows in
  follow-ups. ALL UNITS MERGED.
- 2026-07-20 19:10 conformance pass kicked off (task #58 → REV2, booted
  claude/fable): spec doc 13 vs main @6bbd512, five ratified judgement
  calls listed. Freeze held until verdicts ruled.
- 2026-07-20 19:15 CONFORMANCE done (doc #16, result #59): 46 requirements —
  36 as-specced, 6 deviated-intentionally, 3 deviated-silently,
  2 unimplemented. 0 Major, 1 Medium, 5 Low. All 5 ratified calls verified
  implemented-as-ratified; all 13 spec edge cases verified.
- 2026-07-20 19:18 planner rulings on findings: F1 (Medium, gallery/
  collision hard-reds every PR outside graceful path) → FIX UNIT 3 inserted
  pre-freeze (task #60 → DEV3, REV1 gates, task #61); scope = prepare_gallery
  into publish path + optional 'output' config key + ci error wording.
  F3 + F5 ratified as intentional (retention step shim-side; strict-JSON
  example) — spec wording debt. F2 + F4 → Spec Debt. F6 = ship-gated feature
  doc, due at close-out. Freeze still held; authority still ACTIVE for
  unit 3.
- 2026-07-20 19:19 DEV3 result #64: unit 3 prep done. Ambiguity call
  (stands): 'output' key must also drive the shim's artifact upload path —
  resolved in the shim's existing config step (F3-ratified pattern), marker
  v2→v3 so the escape reaches already-seeded forks.
- 2026-07-20 19:29 unit 3 pr-open — PR #443 (fix/visual-qa-gallery-output),
  watch registered; verification green pre-PR (503 host-mode, 40 focused +
  10 subtests; sandbox = known #435 trio).
- 2026-07-20 19:35 REV1 result #74: unit 3 → fixing. 0 Major, 1 Medium
  SC-007 (flag #11): prepare_gallery still precedes the config-is-None
  neutral branch — an UNCONFIGURED fork tracking gallery/ still reds every
  PR; F1 only half-fixed (req 24). 4 Lows to report. DEV3 re-booted.
- 2026-07-20 19:42–19:46 SC-007 fixed @6ced66e (neutral branches publish
  write=False before prepare_gallery + no-config/tracked-gallery regression);
  REV1 delta re-review → review-clean, flag #11 closed, no new Lows.
- 2026-07-20 19:48 unit 3 MERGED @6ced66e (pr_event #86, watch retired);
  unit report filed whole (result #87) — deviations: none (shim artifact-path
  update = the ratified escape completion). 4 report-only Lows in follow-ups.
- 2026-07-20 19:50 scoped conformance re-check kicked off (task #88 → REV2,
  booted): F1 surface only — req 24 neutral-path, req 35 degradation,
  output-escape end-to-end incl. shim v3. Verdict delta appends to doc 16.
  Freeze still held.
- 2026-07-20 19:56 REV2 result #89: scoped re-check CLEAN — 0 Major,
  0 Medium. F1/row 35 resolved deviated-intentionally (RC6), verified on
  main @6e3615e (squash of 6ced66e, trees identical); req 24 + path-skip
  neutrals non-destructive; output escape verified end-to-end incl. shim v3.
  Conformance flags #10 + #11 closed. New Low F7 (collision/neutral artifact
  ships fork-tracked gallery content) — planner ruling: DEFERRED, accepted
  v1 boundary, cheap-fix backlog candidate.
- 2026-07-20 19:58 SPRINT CLOSED — status set CLOSED, doc frozen (authority
  revoked), participants notified, watches verified retired. Sprint report
  follows as its own doc.
