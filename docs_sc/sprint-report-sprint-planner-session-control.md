---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: false
---

# SPRINT REPORT: Sprint planner session control

sprint: doc #21 (frozen) · spec: doc #20 (feature 14) · planner: PLN1
conformance: docs #22 + #23 (scoped re-run) · closed: 2026-07-22
models: devs=codex/gpt-5.6-sol · reviewers=claude/fable

## Verdict

**10 units, 10 PRs, 9 merged — conforms-with-deviations, all deviations
intentional and ruled; main green at 90866a6.** The spec's code surface is
fully implemented and conformance-verified, including the two motivating
scenarios: issue #454 (provider-neutral planner wake) and issue #439
(liveness/no parent-only authority) are both proven closed hermetically.
Zero Major findings across nine reviews and two conformance passes; every
Medium (8 total) was fixed in-sprint and re-review-verified; flags #15–#23
all closed.

Deferred with eyes open — the FnB holds all four keys:
1. **Live release gates (J7):** per-provider disposable-session smokes + one
   real sprint each on Claude/Codex/Kimi K3. Blocked on `./sc update` (stale
   materialized engine lacks `./sc session`) and provider spend
   authorization. **Spec #20 stays UNFROZEN until these pass** — #454/#439
   closure is hermetically proven now, operationally proven then.
2. **Unit 9 (docs cleanup, draft PR #466):** blocked on the admin Publish
   gate — landing DB-rendered docs requires committing the
   `.sc-state/content.sql` snapshot, which is FnB/admin-only. DEV4 correctly
   refused to hand-commit renders. Render-check red on #466 is the gate
   itself, not a defect. Watch kept so the merge surfaces later.
3. **U7 Low 4 escalation:** unauthenticated local `/api/session-control`
   POST routes let any local process release another shell's managed binding
   (cancelling queued wakes, deleting the kimi token). Recommendation:
   token-scope them. Awaiting posture ruling; recorded here as
   deferred-with-eyes-open.
4. **Roadmap:** feature 14 stays `in_progress` until the live gates pass and
   #466 lands; then ship + freeze spec #20.

## Units Shipped

| seq | unit | dev | reviewer | pr | status | planned → actual |
|---|---|---|---|---|---|---|
| 1 | Schema + state machine (#50) | DEV3 | REV1 | #455 | merged | as planned |
| 2 | Supervisor + leases incl. #439 (#51) | DEV4 | REV2 | #456 | merged | as planned |
| 3 | Wake dispatcher + control API (#52) | DEV3 | REV1 | #460 | merged | as planned |
| 4 | Claude adapter (#53) | DEV4 | REV2 | #463 | merged | delayed by FnB borrow of DEV4; merged after 5/6 |
| 5 | Codex app-server adapter (#54) | DEV3 | REV1 | #461 | merged | as planned |
| 6 | Kimi K3 adapter (#55) | DEV3 | REV2 | #462 | merged | resequenced DEV4→DEV3 (DEV4 on FnB hold) |
| 7 | Status + analytics (#56) | DEV3 | REV1 | #464 | merged | as planned |
| 8 | Sprint workflow + provider conformance (#57) | DEV4 | REV2 | #465 | merged | as planned |
| 9 | Docs cleanup: stale wake claims (scope-add, U8 L1/L2) | DEV4 | REV2 | #466 (draft) | publish-gated → FnB | scope-add |
| 10 | Conformance F1 fix: managed `sc enter` (scope-add) | DEV4 | REV2 | #469 | merged | scope-add from conformance |

Spec tasks #50–57 all done. Task #57's live-gate clause is the one
honestly-deferred deviation (see Verdict item 1), documented in unit 8's
report and PR body at the time of `done`.

## Judgements Made

All ruled at receipt, ratified into the conformance kickoff (J1–J8), and
confirmed by the pass:

- **J1 (U1/DEV3):** spec named binding states but not edges → conservative
  transition set; foreground→dispatching included (required for Claude
  active-watcher delivery — my initial wording omitted it; REV1's L1
  corrected the record); error/released recover only via starting. Stands.
- **J2 (U3/DEV3):** contradictory retry wording → initial attempt + three
  delayed retries (15s/60s/5m), terminal on fourth failure. Stands; spec debt.
- **J3 (U3/DEV3):** internal endpoints in unit 3, public `sc session` CLI in
  unit 7. Stands; moot at final SHA (both shipped).
- **J4 (U5/REV1+PLN1):** effort pinning "from the original archive" =
  config-effective at launch. Stands; spec debt.
- **J5 (U5/PLN1):** arming-time approval-posture validation is in-spec
  intent; requirement propagated to Claude and Kimi adapters via their
  kickoffs. All three adapters conform.
- **J6 (U6/DEV3):** kimi token cleanup split — server-exit path unit 6,
  release-while-server-lives via unit 7's generic release. Verified
  fail-closed by REV1 in U7.
- **J7 (PLN1, msg #261):** live provider gates deferred to close-out;
  unit 8 merged on hermetic scope. Deviated-intentionally per conformance.
- **J8 (PLN1):** unit 9's stale-doc surfaces pre-declared
  deviated-intentionally pending the publish gate.
- **Publish-gate ruling (unit 9):** DEV4's refusal to hand-commit DB renders
  upheld; branch parked as draft; Publish handoff to FnB.
- **SC-466 routing (unit 10):** REV2's Medium (error-state `sc enter`
  funneled operators into queue-cancelling release) fixed in-loop and
  re-verified — recovery is now retry-first, failing pre-archive/pre-spawn.
- No severity disputes arose all sprint.

## Spec Accuracy

Conformance doc #22 (main @ 2cc320e) + scoped re-run doc #23 (@ 90866a6):
**0 Major / 1 Medium / 2 Low across ~45 spec requirements; everything else
as-specced**, including exact schemas, the full state model, one-owner
PID+start-ticks leases, all three provider contracts, dispatch loop
semantics, failure handling, operator surfaces, and analytics attribution.

- **F1 Medium (unimplemented → fixed):** managed-binding `sc enter`
  attach-by-default + `--new-session` refusal was absent — the exact
  two-conversations-for-one-shell split the spec exists to prevent. Fixed as
  unit 10; re-run judged it as-specced.
- **F2 Low (unimplemented, deferred):** provider-contract `interrupt`
  operation absent everywhere. Operator ergonomics only; backlog.
- **F3 Low (deviated-silently, deferred):** spec claims the sprint skill
  re-arms the watcher after every handled batch; skills don't say it.
  Runtime degrades safely (queue → dormant resume). Skill-text backlog.
- Cross-check vs unit reports: no contradiction — every dev-reported
  `deviations:` line matches a ratified J-call; no silent deviations found
  beyond F3 (a skill-text gap, not a unit's).
- #454 and #439 each have a dedicated proof section in doc #22, mapped to
  named tests and mechanisms at the SHA.

## Issues Encountered

- **Engine staleness (open, FnB):** materialized engine predates the
  sprint's own features — `./sc models` (PR #453) and `./sc session` both
  missing. Worked around via `sc sql` reads of `flavor_defaults`; blocked
  the live gates; conformance had to rely on green CI instead of a local
  suite run. `./sc update` resolves all of it.
- **Real CI red (1):** CodeQL on dynamic-field SQL in unit 3 — fixed at
  3987995.
- **Phantom reds (not counted, per doctrine):** #435 vm-bake trio
  (sandbox-only) twice; #459 ambient `SC_SANDBOX` in unit 10's first run
  (env-clean rerun green); one pytest invocation miss (job 12). Plus the
  *expected* render-check red on #466 — the publish gate itself, logged as
  such so it never reads as anomalous.
- **Review-caught Mediums (8, all fixed + re-verified in-sprint):** U2 two
  lease-lifecycle races; U3 SC-460 (retry stranded `starting`) + SC-461
  (PATCH unfencing `dispatching`); U5 SC-462 (no arming posture validation);
  U6 SC-463 (kimi token not deleted on server exit); U4 SC-464 (ack-wait
  liveness re-check); U7 SC-465 (release AttributeError); U10 SC-466
  (error-state enter recovery). Flags #15–#23 all closed.
- **Resequencing (1):** FnB borrowed DEV4 mid-sprint; unit 6 moved
  DEV4→DEV3 at near-zero hold cost; unit 4 resumed on release.
- **Zero stalls:** no dead links, no liveness-lock repeats of the sprint 14
  read-status failure (kickoff READ status verified early per L&S).

## Deferred & Follow-ups

The next sprint's seed list, by weight:

1. Live release gates + spec #20 freeze (J7) — after `./sc update` + spend
   auth. The one item holding the spec open.
2. PR #466 merge after FnB Publish (unit 9; watch active).
3. U7 Low 4: token-scope the local session-control POST routes (or ruled
   otherwise by FnB).
4. F2: implement provider `interrupt` (operator-only).
5. F3: add re-arm-after-batch instruction to sprint skills (three-artifact
   engine-skill commit).
6. U4 L7: ack-wait residual under whole-group `kill -9`.
7. U3 L4: dispatcher restart lacks backoff.
8. U10 Lows: harness-mismatch message/precedence path untested; bare
   headless run of errored-managed shell now refused (fail-closed —
   confirm no automated caller ever needs it).
9. U1 L2: reconstruction test for one-shell-two-bindings case.
10. Remaining per-unit Lows: reviews/sprint21-unit{1..10}-pr*.md on
    shell/rev1 + shell/rev2 branches.

## Spec Debt

Write-backs owed to spec #20 (input to the spec-update pass, alongside the
freeze):

- Retry semantics: replace contradictory wording with the ratified reading
  (initial + 3 retries at 15s/60s/5m, terminal on 4th failure).
- Arming-time approval-posture validation: spec is silent; make J5 explicit
  (provider-generic vocabulary, all adapters).
- Effort pinning: state J4's config-effective-at-launch reading.
- Transition-edge table: enumerate the ratified edge set (J1) instead of
  leaving edges implicit.
- Error-state `sc enter` recovery: encode SC-466's retry-first,
  fail-before-archive behavior as the specified contract.
- Watcher re-arm claim (F3): either require it of the skills explicitly or
  soften the spec's claim to match the safe-degradation reality.

## Metrics

- 10 units, 10 PRs, 9 merged; 2 scope-adds (1 from review Lows, 1 from
  conformance).
- Review cycles: 7 units needed exactly one fix loop; units 1 and 8 were
  clean on first review. 0 Majors from unit reviews; 1 Major (SC-460)
  surfaced by REV1 in U3's re-review cycle, fixed same loop.
- CI: 1 real red, 4 phantom/expected reds (all rerun-and-reported, none
  counted, none merged over).
- Conformance: 2 passes (full + scoped), 3 findings, 1 fixed in-sprint.
- Worker boots ~19 across 4 shells; planner stayed a single long-lived
  session; zero scheduled polls — every transition event-driven.
- Wall clock: declared 2026-07-21, closed 2026-07-22.
