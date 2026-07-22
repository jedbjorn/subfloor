---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Visual QA CI — Playwright viewport screenshots
roadmap_status: shipped
frozen: false
---

# SPRINT REPORT: Visual QA CI — Playwright viewport screenshots (feature #19)

sprint doc: #14 (frozen) · spec: #13 · conformance: #16 · planner: PLN1 · 2026-07-20
models: devs=codex/gpt-5.6-sol · reviewers=claude/fable

## Verdict

**Shipped, conforms-with-deviations — every deviation intentional and
ratified.** 3 units (2 planned + 1 conformance fix unit), 3 PRs (#438, #442,
#443), all merged; main green throughout — **zero CI reds across the whole
sprint**. Final conformance: 0 Major, 0 Medium, 6 Lows deferred with eyes
open. Forks get viewport-screenshot CI on every PR via a managed shim (v3) +
engine-side runner; existing forks adopt via `make update`. Nothing was
deferred that blocks use; the one contract-threatening defect found
(gallery/-collision hard-red) was fixed pre-freeze under sprint authority.

One structural note for the record: this sprint ran in the **canonical
upstream repo** — `.super-coder/` is tracked engine source here. Every task
row carried that framing; no shell mistook the engine for a foreign
dependency at any point.

## Units Shipped

| seq | unit | shell | reviewer | pr | review cycles | status |
|---|---|---|---|---|---|---|
| 2 | Distribution: shim + example templates, install/init_fork seeding, `ensure_workflows()` reconcile, manifest, tests | DEV4 | REV2 | #438 | 1 (clean first pass) | merged @026749b |
| 1 | Runner `visual_qa.py` (ci/run/init) + `sc` dispatch + hermetic tests + authorized token seam fix | DEV3 | REV1 | #442 | 2 (SC-006) | merged @6bbd512 |
| 3 | Conformance fix unit (F1): prepare_gallery into publish path, `output` config key, shim v3 | DEV3 | REV1 | #443 | 2 (SC-007) | merged @6ced66e (main @6e3615e) |

Planned order: units 1+2 parallel. Actual: unit 2 finished first (DEV3's
pre-sprint session delayed unit 1's start ~3h — see Issues); unit 3 was
inserted at close-out by the pre-freeze conformance pass, exactly as the
process intends.

## Judgements Made

All ratified; REV verification confirmed each implemented as declared.

1. **RC1** — example config seeded inactive at `.sc-state/visual-qa.example.json`;
   live `visual-qa.json` never auto-created (DEV4; silent-affirm).
2. **RC2** — route success = any viewport returning 200 + screenshot;
   "all routes failed → fail" keeps meaning "app not serving" (DEV3).
3. **RC3** — unresolvable PR base diff → run capture, never a false-neutral
   skip (DEV3).
4. **RC4** — cross-unit seam: shim exports `github.token` as `GITHUB_TOKEN`,
   managed marker v1→v2 (planner-authorized mid-sprint, REV1-verified).
5. **RC5** — unit-scoped task ledgers on spec 13 (both devs; spec shipped no
   shared ledger).
6. **RC6** — `output` config key must also drive the shim artifact path;
   resolved in the shim config step, marker v2→v3 (DEV3, REV1-ratified).
7. Planner pre-ruling — shim Playwright cache key derives from the
   materialized runner (`hashFiles`), never a shim-hardcoded version.

Severity disputes: none. Every ambiguity call was reported before merge and
ruled or affirmed on receipt.

## Spec Accuracy

Conformance (doc #16): 46 requirements — 36 as-specced, 7
deviated-intentionally (all ratified), 0 unresolved deviated-silently after
re-check, 1 unimplemented remaining = the ship-gated feature doc (F6). All
13 spec edge-case rows verified implemented.

Cross-check against unit reports: unit reports and conformance agree — the
two Mediums (SC-006 rmtree guard, SC-007 neutral-path ordering) were both
*review* catches, not silent deviations; unit 3's report declared "deviations:
none" and the re-check upheld it. The one spec-contract violation found
(F1) was caught by conformance, fixed as unit 3, and re-verified clean.

## Issues Encountered

- **DEV3 cold-start stall (~3h):** a pre-sprint codex session (feature #18)
  held the liveness lock with the sprint task unread; boots refused. Resolved
  by FnB-directed session kill + fresh boot. Lesson: a task row queued behind
  a busy session is silent — check `message sent` read-status early.
- **Cross-unit token seam (pre-PR catch):** unit 1 found the merged unit-2
  shim never exported `github.token` → sticky comments would always skip.
  Fixed under planner authorization inside unit 1 (RC4).
- **F1 gallery/ collision (conformance catch):** tracked `gallery/` dir would
  hard-red every PR outside the graceful path; fix unit 3 inserted pre-freeze;
  second loop (SC-007) needed because the first fix missed the no-config
  neutral branch.
- **Sandbox vm-bake trio (#435):** every full-suite sandbox run showed the
  same 3 known unrelated failures; host-mode reruns green every time
  (455 → 501 → 504 tests). Pre-existing, tracked, not sprint debt.
- CI reds: none. Anomalous reds: none. Re-scopes: none.

## Deferred & Follow-ups

Report-only Lows from reviews, unit reports, and conformance — the next
pass's seed list:

1. **F7 (accepted v1 boundary, cheap-fix candidate):** on output collision or
   neutral runs, the artifact can ship fork-tracked gallery content under the
   `visual-qa-gallery` name.
2. `run` mode ignores config `output` (CLI/default only) + duplicates
   `_relative_output` validation inline.
3. Interrupted-run gallery requires manual clear (fail-closed).
4. Sentinel-valid symlinked gallery can surface raw `shutil.rmtree` OSError.
5. Root-level files don't match `**/`-prefixed fnmatch patterns; networkidle
   can fail continuously-polling apps after readiness.
6. `build_comment` failed/neutral branches lack direct tests; reported image
   dimensions are DOM px, not PNG px.
7. CI collision wording: "choose another config key" → should say "point the
   config key at another directory".
8. `ensure_workflows` missing-template guard (unreachable today).
9. Spec open questions carried forward: inline thumbnails / gallery hosting;
   non-GitHub remotes; Playwright pin config override.
10. **Feature #19 doc (F6)** — ship-gated, flagged, planner queue (with the
    feature #18 doc).

## Spec Debt

Write-backs owed to spec #13 (input to the spec-update pass):

- Add RC2 (per-viewport route success), RC3 (unresolvable-diff → run), RC4
  (token export via shim env), RC6 (`output` key + shim artifact path) as
  spec text; add a gallery/-collision row to the edge-case table.
- Fix the §Fork config self-contradiction: "local mode picks a free port"
  vs `run` being capture-only (F4) — delete or re-scope to a future
  `run --serve`.
- "Commented example" → "example config" (F5; comments would break
  `json.loads`).
- Acknowledge the shim-side retention-resolution step (F3) — the spec's own
  "~30 lines" and "retention per config" requirements conflict; ~60 lines is
  the honest number.
- One sentence settling `paths` semantics: fnmatch (depth-crossing `*`) vs
  path-aware glob (F2).

## Metrics

- Units: 3 (2 planned, 1 inserted) · PRs: 3 · merges: 3 · CI reds: 0
- Review cycles: unit 2 = 1 · unit 1 = 2 · unit 3 = 2; Mediums found in
  review: 2 (SC-006, SC-007), both fixed same-session
- Conformance: full pass (46 reqs) + 1 scoped re-check; findings 0/1/5 →
  0/0/6 after fix unit
- Worker boots: DEV3 ×5 (1 killed-idle restart), DEV4 ×2, REV1 ×3, REV2 ×3;
  planner stayed single-session, event-driven throughout (zero scheduled polls)
- Wall-clock: declared 15:25 → frozen ~19:58 (~4.5h, ~3h of which was the
  unit-1 cold-start stall)
