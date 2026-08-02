---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Browser-native headless conversations
roadmap_status: in_progress
frozen: false
---

# SPRINT REPORT: Segmented assistant responses (F24 spec 56)

sprint: doc #63 (frozen) · spec: doc #56 (frozen, spec seq 8, feature #24) · planner: PLN2
declared 2026-08-01 · closed 2026-08-01 · final main: d56fdff (== PR #902 tree 97eb911)

## Verdict

**5 units / 5 PRs merged, conforms, main green.** Spec 56 shipped as specced at
main d56fdff: conformance judged 13/14 rows as-specced with 1 intentional
deviation under ratified rulings, and the single silent deviation (F1 Medium,
per-message completeness gate) was fixed as unit 5 under still-active sprint
authority and verified CLOSED in a scoped re-run. Zero deviations declared in
any unit report and none found beyond F1. One CI red all sprint (self-fixed in
one push). Nothing deferred that blocks release; 11 Lows + 1 backlog
observation ride the follow-up ledger below, eyes-open.

The transcript now renders one model turn as ordered assistant bubbles split at
tool/permission/input boundaries, identically in historical projection and live
SSE reduction, across all four harnesses, with the bounded-projection
performance contract (five reads, one snapshot, keyed frame-coalesced DOM)
intact — Decision #32 preserved.

## Units Shipped

| seq | unit | shell | reviewer | pr | outcome |
|---|---|---|---|---|---|
| 0 | Phase-0 QAQC on spec 56 | REV3 | — | — | round 1 FAIL (1 Medium M1: R5 cursor not run-scoped; 3 Lows) → spec amended → scoped re-round PASS (docs 64) |
| 1 | Executable segmented-response traces | DEV5 | REV3 | #885 | merged @ 1623516; 1 ruled Medium (marker ownership), 3 Lows; 1 self-fixed CI red |
| 2 | Transcript projection v2 | DEV6 | REV3 | #891 | merged @ 190978d; first-round clean; 3 Lows |
| 3 | Keyed live assistant segments | DEV5 | REV3 | #893 | merged @ 38c0360; first-round clean; 2 Lows; closed the R3 window; both u1 carry-ins landed |
| 4 | Cross-harness release gate | DEV6 | REV3 | #896 | merged @ 652e56b; first-round clean; 2 Lows; flag #100 fixed; zero-marker invariant repo-wide |
| C | Conformance: spec 56 vs main @ 652e56b | REV3 | — | — | doc 66: 0 Major / 1 Medium (F1) / 4 Low; 13/14 as-specced |
| 5 | F1 fix — per-run completeness gate (unplanned) | DEV6 | REV3 | #902 | merged @ 97eb911; 1 review Medium (coverage) fixed with mutation proof; 1 Low |
| C2 | Scoped F1 re-run vs main @ 97eb911 | REV3 | — | — | F1 CLOSED, as-specced; addendum on doc 66 |

Planned order held exactly (serial chain u1→u2→u3→u4); unit 5 + C2 were the
conformance remediation, inserted before the freeze as designed.

## Judgements Made

- **R1** (ratified, then amended): the "v1 must fail the fixtures" gate vs.
  green-merge tension resolved with strict staged probes
  (`pytest.xfail(strict=True)`/`expectedFailure`), ownership partitioned
  per unit in reason strings; XPASS-is-red makes removal self-enforcing.
  Amended on REV3's unit-1 Medium: the same-adapter concurrency probe re-owned
  U4→U2 (its only red assertion was projection behavior). Final state: zero
  markers repo-wide, verified independently.
- **R2**: unit-1 fixture defect (anchor expected tool.started:6; spec's
  latest-boundary rule requires tool.completed:7) corrected inside unit 2 as a
  named, reviewer-visible change to a merged unit's artifact.
- **R3**: the u2→u3 window where main served projection v2 against the v1 UI
  was accepted deliberately — R7's safe-failure is the specced mismatch
  behavior, CI stayed green via staged probes, and nothing deploys subfloor
  main mid-sprint. Closed by unit 3's merge.
- **R4**: U4's marker set was legitimately empty post-R1-amendment; cross-
  harness assertions shipped as direct passing gates, no synthetic marker churn.
- **R5**: F1's regression fixture must be multi-attempt with a retained later
  acceptance marker — a naive suffix cap removes message.accepted and
  short-circuits projection before the code path under test.
- No severity disputes; every ambiguity was reported before merge and ruled
  while the unit was open.

## Spec Accuracy

Conformance doc 66 (+ F1 addendum), judged against code at pinned SHAs only:
13/14 rows as-specced with load-bearing citations; 1 deviated-intentionally
(the construction-1 gate's evolution under R1/R4 — ratified); 1
deviated-silently: **F1** — completeness gate implemented per-message where
the spec is per-run, letting an active sibling mask a source-capped terminal
run (transient fabricated merge). Cross-check: all five unit reports declared
`deviations: none`; F1 was real but arose from an implementation reading, not
an undeclared choice — the unit-2 report's claims were accurate about
everything it asserted, and the gap was exactly the kind conformance exists to
catch. Fixed (unit 5), re-verified CLOSED.

## Issues Encountered

- 1 CI red all sprint: unit 1's xfail reason string tripped the F31
  v1-removal-manifest guard; label-only fix, one push (matches F31's known
  guard pattern).
- Stale `pr_event`: unit 1's red event arrived after the dev had already pushed
  the fix head — ground-truth `gh` read prevented a wasted re-boot.
- Repeating pre-assert terminalization timing race (u2, u3): escalated from
  anomalous to flag #100; fixed in unit 4 (`wait_for_run_count` now waits for a
  fully terminal run set). Closed.
- Review-coverage Medium on unit 5 (SC-037/flag #101): activity-omission half
  of the F1 fix untested; closed with a red-capable mutation proof.
- Tooling trap: work-repo `./sc` lacks `watch` — PR watches must register via
  the home-engine sc. Briefly misreported by DEV5, corrected; tracked (SC-035,
  regression comment on subfloor#368).
- Environment side-flag: local baked Chromium mismatch (SC-034).
- Engine gaps found by adjacent traffic: spec_tasks has no reopen transition
  and cancelled rows burn their (doc, seq) slot — filed as subfloor#882 after a
  stray `task start` from another shell hit spec 62's task table.
- Flags opened and closed in-sprint: #84 (phase-0 M1), #97 (marker ownership),
  #100 (timing race), #101 (F1 coverage). None left open.

## Deferred & Follow-ups

Test/coverage:
1. Reshape the boundary-completeness test window so it cannot pass verbatim
   under v1 (u2 Low; conformance F2-adjacent).
2. Assert mixed-terminal composition (complete + incomplete terminal runs, no
   active sibling) — spec-conformant under the per-run gate but unasserted
   (u5 Low).
3. Restore boundary-free single-bubble coverage in the release gate or delete
   the dead fake-adapter branch (u4 Low / conformance F4).
4. Re-prove preserved scroll inside the u4 live smoke rather than older tests
   (u4 Low / F4).
5. Assert (or stop emitting) the pre-reload boundary emissions in the live
   test's setup (u3 Low).

Code hygiene:
6. Remove the unreachable backward-anchor reconcile branch app.js:4145 (u3
   Low / conformance F3).

Product (v1-inherited, pre-existing):
7. Malformed-payload whole-turn omission discloses only via `warnings` —
   consider truncation-metadata parity (u2 Low / F2).
8. Boundary-evidence COUNT subquery full-scans conversation_events; v2's new
   subquery is indexable, the inherited one isn't — index or restructure
   (u2 Low).
9. `message.accepted` re-sorts state order but never repositions the optimistic
   user bubble's DOM node until the next full build (conformance F5, v1-era).

## Spec Debt

- Phase-0 M1 (R5 run-scoping) and L1–L3 were written back into spec 56 before
  build — no residue.
- The staged-probe pattern (R1) proved out and belongs in the next spec that
  ships a failing-gate-first construction plan as an explicit convention,
  including per-unit marker ownership in reason strings and the
  empty-set/direct-gates outcome (R4).
- R4's completeness clause was correct but its granularity ("per run") was
  implemented per-message once — future specs with per-X gates should state the
  aggregation boundary in the gate's own sentence, as spec 56's R5 amendment
  did for the cursor.
- R5-fixture subtlety (R5 ruling): specs that gate on source-cap behavior
  should note that caps can remove the acceptance marker itself.

## Metrics

- 5 build units, 5 PRs (#885, #891, #893, #896, #902), 2 conformance passes,
  1 phase-0 QAQC round + 1 scoped re-round.
- Review cycles: u1 2, u2 1, u3 1, u4 1, u5 2. CI reds: 1. Anomalous reruns: 2
  (same root cause → flag #100 → fixed).
- Rulings: 5 (+1 amendment). Flags: 4 opened, 4 closed in-sprint. Lows banked:
  11 + 1 backlog observation.
- Shell boots: DEV5 ×5, DEV6 ×5, REV3 ×7. Zero scheduled polls; every wake was
  a message row or pr_event.
- Wall clock: ~3.5 h declaration → freeze, including phase-0 spec repair and
  conformance remediation.
