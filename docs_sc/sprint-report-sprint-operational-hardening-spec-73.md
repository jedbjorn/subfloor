---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: false
---

# SPRINT REPORT: Sprint operational hardening (spec #73)

sprint doc: #74 (frozen) · spec: #73 (feature #31) · planner: PLN1 · closed: 2026-08-02
models: devs=codex/gpt-5.6-sol · reviewers=kimi/kimi-code/k3 · U4 planner seat=claude/fable
mode: organic (FnB-directed) — PLN1 booted all workers and ran its own inbox watcher; the native sprint machinery was the subject under repair, then the subject under proof.

## Verdict

**Spec #73 shipped.** 4/4 units done, 3 code PRs merged (#933, #934, #931) plus one adopted non-spec PR (#932, FnB-directed), main green at f442bb2. Conformance (doc #76): 25 requirements as-specced, 2 deviated-intentionally under ratified calls, 0 unimplemented, **0 Major / 0 Medium / 7 Low**. The report-only downstream proof (unit 4, dos-app sprint 4) ran the entire corrected chain — QAQC-before-declaration, deferred Planner terminal close, in_review liveness handoff, resolved-flag evidence reads, CLI hygiene — on native wakes with 7/7 first-attempt deliveries, exactly-once reports, idempotent replay, and zero human intervention in the dev/review lane. All six tracked issues (#923 #929 #925 #926 #922 #774) have closure evidence. Deferred with eyes open: seven conformance Lows (dispositions below), none blocking.

## Units Shipped

| seq | unit | shell | reviewer | pr | reds | outcome |
|---|---|---|---|---|---|---|
| 1 | Terminal completion deferral + in_review liveness (mig 0158) | DEV3 | REV1 | #933 | 1 | merged, review-clean 0M/0M |
| 3 | CLI hygiene: deps --help + nested test discovery | DEV5 | REV1 | #931 | 0 | merged, review-clean 0M/0M (first to land) |
| 2 | Flag evidence + five role skills + audit (mig 0159) | DEV4 | REV2 | #934 | 1 | merged, review-clean 0M/0M, audit independently verified |
| A | ADOPTED: admin shells CLI-only in browser (outside spec) | CC | FnB-certified | #932 | 0 | merged under PLN1 oversight, zero spec collision |
| 4 | Downstream proof (report-only) | PLN2 + dos-app PLN2/DEV1/REV1, home REV3 gate | REV3 | dos-app #59, #60 | 0 | PASS — evidence doc #75 + supplement; REV3 conditional pass cured |

Planned order held: 1‖3 parallel, 2 after 1, 4 after 2+3. Actual merge order: 3, 1, 2, then proof.

## Judgements Made

1. **Unit 1 mechanism reuse** (DEV3, REV1-ratified): rework re-observation rides the existing actionable changes-requested notification; in_review resolution extends the existing disposition trigger. Conformance row 11 confirms this as the spec's own reading. Final.
2. **DEV3 worktree isolation** (call stood): shared checkout found on CC's feat/admin-cli-only with dirty work; DEV3 isolated into a dedicated worktree. Led to adopting #932 (below).
3. **U4 seat mapping** (PLN1-ratified): dos-app has no DEV6/REV3, so native seats went to dos-app PLN2/DEV1/REV1; home REV3 kept the evidence gate. Routes inherited the interview.
4. **REV3 finding 1 → supplement ordered** (Medium, flag #122): spec step 2 lacked durable command-output evidence; PLN1 ruled the gate's letter binding — PLN2 re-ran both commands with durable capture, supplement appended to doc #75, flag closed.
5. **REV3 finding 2 → structural proof accepted** (Low): the liveness threshold was never crossed in real time (~5-min review window); accepted because the resolved-at-in_review expectation makes a post-handoff nudge impossible by construction, plus the 20-min mechanism test. Conformance row 23 ratifies.
6. **Anomalous-environment calls stood** (DEV3, DEV4): linked-worktree ./sc verify refusals (SC-019/#769) and the repo lint baseline (#878) treated as environment gates, PR CI canonical. No phantom red counted against fix attempts.
No severity disputes arose.

## Spec Accuracy

Conformance doc #76 against unit reports:
- 25/27 rows as-specced with code+test+live evidence; both deviations were PLN1-ratified before the pass.
- **F2 confirmed a spec text error** (unit 1 gate: "ship the ordered migration and schema source together" contradicts the engine's frozen-baseline migration contract). Code is right; spec sentence should read "ordered migration plus updated removal-manifest/fixture references." Recorded here as the spec correction of record (spec #73 left unedited).
- **Unit report discrepancy**: DEV4's report claimed "No CI reds" while the trail shows one (#934 red @ f4b77c6, removal-manifest miss for 0159, fixed in one loop). DEV5's and DEV3's reports matched the trail exactly; deviations lines were "none" across all units and conformance found no silent deviations — the discrepancy is report hygiene, not code.

## Issues Encountered

- **Two CI reds, same class**: both migration-bearing units missed the tracked sprint-removal manifest (0158, then 0159). Deterministic one-line fixes; see follow-ups for the scaffold gap.
- **Shared-checkout collision**: CC's #932 work sat on the shared subfloor checkout DEV3 was using; isolated without loss, PR adopted and merged at FnB direction.
- **Environment gates, pre-existing**: linked-worktree ./sc verify refusal (SC-019/#769) hit DEV3 and DEV4; lint baseline noise (#878) hit DEV4. Hosted CI stayed canonical.
- **dos-app double engine lay**: PLN1's FnB-directed fleet update raced PLN2's deliberate repin of the same ref (f442bb2). Content-identical, zero re-applied migrations; disclosed pre-arm, verified harmless by REV3 from engine.ref/engine.ref.prev + migration rows.
- **Fleet update collateral finds** (adjacent to sprint, PLN1 maintainer lane): subfloor #935 (update remote-matcher substring bug — subfloor-marketing fetched engine refs from its own origin), #936 (transient first-attempt update failures on 3 of 6 forks, retry-clean), #937 (stale linked worktree still runs the pre-fix mutating deps dispatcher), #938 (broken-venv self-heal gap on host-seat sc test).

## Deferred & Follow-ups

Conformance Lows (dispositions):
- **F1**: FnB-initiated complete also defers the owning Planner's live run — ruled *intended* (decision 1's letter keys on the owner, not the caller); needs a doc note + test. Follow-up.
- **F3** (= unit 1 reviewer Low, now **flag #123**): record_review resolves the reviewer expectation outside the verdict transaction — same defect shape #929 fixed for devs. Highest-value follow-up of the set.
- **F4/F5/F7**: test-depth holes (work_unit.cancelled string unasserted; reassignment edge in the notification branch; help-probe lacks direct pip/npm shims). Backlog.
- **F6**: duplicate migration number 0155 (pre-existing, second sighting after sprint 70). Hygiene backlog.
Unit-report Lows: presence-loop full-walk instead of short-circuit + string-occurrence guard test (U3); duplicated ACTIONABLE_KINDS invariant string (U1); flags-skill one-line output doc drift + two untested human-output paths (U2); dos-app final-report "5/5 wakes" vs actual 7/7 (U4 nit).
Process follow-up: **migration scaffold should append the removal-manifest entry** — the same allowlist trap cost both migration units a CI round.
Operational (FnB queue): ami/dos-arch engine restarts pending (live-session kill prompt); 5 repin PRs undecided (dos-app's #59 done by PLN2).

## Spec Debt

- Unit 1 gate sentence (F2): replace "ship the ordered migration and schema source together" with "ship the ordered migration plus updated removal-manifest/fixture references" — the engine never folds migrations into schema.sql.
- U4 step 4 should permit the structural proof it actually admits ("or prove by durable expectation state that a post-handoff nudge is impossible").
- U4 step 2 should name the durable-evidence form it expects (command output captured to a durable row/file), which would have prevented the conditional pass.
- Completion-deferral trigger (F1): spec describes the completing-owning-Planner case; state explicitly that the deferral keys on the owning Planner regardless of caller.

## Metrics

- 4 units + 1 adopted PR; 5 PRs merged total (3 sprint + #932 + dos-app #60 inside the proof).
- Review cycles: 1 per unit (zero fixing loops post-review). CI reds: 2, both cured in one loop.
- Worker boots: DEV3×2, DEV4×2, DEV5×2, REV1×3 (incl. conformance), REV2×1, REV3×1, PLN2×3. Zero scheduled polling; wall-clock ~3h05m from declaration (05:23) to close (08:30).
- Defects filed upstream during the sprint: subfloor #935–#938; flags: #122 opened+closed, #123 opened (follow-up), SC-012 (#38) closed by U3.
