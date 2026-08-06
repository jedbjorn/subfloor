# SPRINT REPORT: Sprint 87 — Sprints v2.1

Final report per decision #67 (Rev documents, Planner acts). Authored by REV1 on
the conformance verdict (doc #91, amended spec #89 vs main @ `4c7d6d4`) and the
Planner's bounded evidence packet (msg #1353). Manual sprint under decisions
#75 (manual mode) and #76 (Sol×Fable routing).

## Verdict

**Sprint 87 delivered Sprints v2.1 complete and conformant. Recommendation:
conclude.** All nine work units merged green and review-clean; whole-sprint
conformance judged 11 sections: 9 as-specced, 1 deviated-intentionally
(Arming, ratified in-spec by the 2026-08-03 amendment), 1 as-specced with one
Low silent clause (Reaper terminal-status literal, flag #167). 0 Major, 0
Medium, 2 Low findings at close — both flagged as post-sprint follow-ups, none
requiring in-sprint repair. Verification basis: code trace + 392 tests / 241
subtests across the sprint footprint, all green on main.

Per the authority split (decision #67), this report is the Reviewer's
conclude-decision document; the Planner executes the transition.

## Units → PRs → merges

All squash merges, all green + review-clean at merge. Final main: `4c7d6d4`.

| Unit | Scope | PR → merge | Review history |
|---|---|---|---|
| U1 | Active Chat Registry | #961 → `89e604e` | 2 rounds: 3 Med fixed; M3 ruled deferred-with-U2 |
| U2 | Producer / delivery | #965 → `2537e49` | 3 rounds: 2 Maj + 3 Med, then 1 Maj + 3 Med — scope-split root cause; hardest unit |
| U3 | Reaper | #963 → `735eac6` | 1 CI red (shutdown lifecycle) + 1 Med (sweep-enum), both fixed |
| U4 | Monitor / ceiling | #975 → `2be3606` | 1 Med (recovery once-only) |
| U5 | Routing / coordinate | #974 → `064ac08` | 1 Med (coordinate bypass × U2-seam) |
| U6 | PR subscriptions | #976 → `8d4185f` | 1 Maj (lifecycle spec-gap → decision #80) |
| U7 | Arming flow | #979 → `3445d74` | Clean 1st pass; deviation ruled faithful, spec amended |
| U8 | Authority split | #980 → `d1b0aa7` | Clean 1st pass |
| U9 | Retirements | #981 → `4c7d6d4` | Clean 1st pass; 9 writer sites re-inventoried → 0 |

Build Order respected: retirements (U9) landed last, after every replacement
was integrated.

## Rulings recorded during the sprint

- **Decision #75** — manual sprint mode for Sprint 87.
- **Decision #76** — Sol×Fable heterogeneous dev/review routing.
- **Decision #77** — PLN1 merge grant; **expires at sprint close**.
- **Decision #79** — drain ride-along + no resume re-delivery (paused-sprint
  bodies drained under another wake skip resume re-delivery only when the
  drain run succeeded; failed drains requeue).
- **Decision #80** — derived subscription quiescence (terminal-latest skips
  polling; no unsubscribe verb). FnB may still override.
- **Spec #89 amendment** — arming at base reality: no modal/pickers; gate
  validates stored per-participant harness/model/effort; adjust-in-chat
  deferred to a future UI unit.
- **Open Item 1** — ruled mechanical timer, 3600s ceiling (REV2-verified safe).
- **Open Item 2** — tunable defaults land as env config (SC_REAPER_*,
  SC_CHAT_INACTIVITY_CEILING) at spec defaults.

## Conformance summary (doc #91)

Judged seams-first against the ratified rulings. Per-section: Literals &
binding rules, Active Chat Registry, Wake Types & Routing, Mode dial /
coordinate, PR Subscriptions, Activity Monitor, Authority Split, Retired
Machinery, and cross-unit seams — **as-specced**. Arming Flow —
**deviated-intentionally, ratified in-spec**. Reaper — as-specced except one
Low clause:

- **F1 · Low · flag #167** — reaper terminal status is `cancelled` + a
  `run.interrupted` event + error_code CONVERSATION_RUN_REAPED, not the spec's
  literal `interrupted` state (no such state exists in RUN_TRANSITIONS). The
  invariant — reaper-owned terminal write, never `unknown` — holds; the
  literal name needs ratification or a spec erratum.
- **F2 · Low · flag #168** — `_enable_planner_coordinate_mode` picks an
  arbitrary armed sprint (fetchone, no ORDER BY) if one planner shell
  originates two armed sprints simultaneously. Rare; deterministic ordering or
  an all-sprints flip would close it.

All retired machinery (9 items) verified gone with writers removed; kept list
verified present.

## Follow-up inventory (post-sprint, FnB disposition)

None are in-sprint fixes; all are preserved for post-sprint triage:

- Flags #167 / #168 — the two conformance Lows above.
- U9-L1 — `participant_capacity.available` conflates legal idle (cheap rename).
- U6-L2 — subscribe-endpoint 401/non-dev surface tests.
- U6 re-review Low — silent unretried reactivation failure.
- U7-L1 — future arming-UI unit (dispatch gates behind chat adjust/go).
- U7-L2 / L3 — gate error naming; arming-specific chat tests.
- U2 F6 / F7 / F8 + R5 / R6-residuals (R6 closed by U4).
- U4-L1 — >1h silent tool call vs ceiling.
- U3 Lows L1–L7 — incl. `conversation_events.run_id` index, TOCTOU/pidfd
  hardening.
- U5-L1 / L2 — dead close-interrupt path; coordinate-enable keying.
- Filed issues #962 #966 #967 #972 #973 #977 #978 + flag #160.
- PLN2 dos-arch pin advisory (msg #1274) — liftable at close; noted here so
  the lift is not forgotten.

## Sprint-method observations

Reviewer's judgement on the Sol×Fable heterogeneous pairing (decision #76),
from the round data: every gating finding across the sprint (1+1 Major, 8
Medium across all rounds) was cross-unit-seam or spec-gap class — zero core
contract breaks inside a unit. The pairing caught what it was routed to catch.
Waves 4–5 (U7/U8/U9) were clean first-pass after earlier-wave lessons were
encoded into handoffs — the manifest warning, the three-artifact callout, and
declared deviations. The scope-split root cause behind U2's three rounds is
the sprint's main process lesson: the hardest unit was hard because its
boundary was drawn late, not because the work was defective. Declared-deviation
discipline (U7) worked as designed — a faithful deviation was ruled in, and the
spec amended rather than the code contorted.

## Close-out notes

- Decision #77 (PLN1 merge grant) expires with this close.
- PLN2 dos-arch pin advisory (msg #1274) is liftable at close.
- Conformance created no fix lane; every finding above is a follow-up for FnB
  disposition, per the Medium-and-above gate — nothing at Medium or above
  remained open at close.
