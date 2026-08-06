# REV1 decision — Sprint 2: CONCLUDE

`decision: conclude`. Sprint 2, feature #38, spec #108 @ bound revision
`4b1b2cf7…a626e4` (bound == current; zero mid-Sprint edits).

## Rationale (Reviewer-owned)

All 11 planned units terminal and completed; 8 PRs merged. I compiled the
evidence packet myself, ran the full suite on the clean main checkout at
`3780151` (**1942 passed, 947 subtests**, no red), and read unit 16's diff and
unit 17's failure directly rather than from report prose.

Episode-2 conformance recorded: **report #4, pass with follow-ups**, adding
**#10 (Major), #11 (Major), #12 (Low)**; #6–#9 carry over from report #3. Spec
#108's downstream acceptance gate is **unproven** after three attempts; unit 17
self-declared its report INVALID and made no VM contact. Full reasoning for
concluding rather than re-entering is in §5 of the report body below.

## Immediate safety impact for FnB

#10 affects **every fork and is self-perpetuating**: once a worktree dispatcher
falls more than one engine generation behind, no number of `./sc update` runs
recovers it. Shells booted there silently run a retired floor and lose every
command added since — the whole `vm` surface and `sc sprint` — while the
operator sees a correct root and a correct `engine.ref`. Four of rst-c's five
worktrees are in this state now.

## Action to execute — exact arguments

Write the report body below to a file **verbatim, unchanged**, then run:

```text
sc sprint complete --sprint 2 \
  --reason "11 units terminal and completed; 8 PRs merged; conformance report #4 pass with follow-ups; downstream-acceptance blocker outside spec #108 scope, escalated to FnB as #10 and #11." \
  --outcome completed_with_followups \
  --report-file <path> \
  --key s2-final-report-rev1-v1
```

Conformance reports: #3 (episode 1), #4 (episode 2, governing).
Follow-ups pending FnB disposition: #6–#12.

If `complete` is rejected, return the durable state to me; do not improvise
around a precondition.

---

## Final Sprint report body (submit unchanged)

# Sprint 2 final report — Windows VM Driving Hardening (feature #38)

Author: REV1 (Reviewer, shell 7). Outcome: **completed with unproven downstream
acceptance and one Major engine follow-up.**

## 1. Scope and revisions

Spec #108 at bound revision `4b1b2cf7…a626e4`, approved pre-declaration
(approval #7, pass, 06:25:37). Bound == current at close; **zero mid-Sprint spec
edits**. Armed 06:31:39. Integrated target `jedbjorn/subfloor` `origin/main` @
`3780151`; pre-Sprint base `07f1c0b`.

Governing control decisions: #178's 11-unit projection, ratified by #188, which
superseded conclude decision #183 and voided #179. A later FnB board-level
override (decision #46 authority) reassigned unit 17's reviewer from REV3 to
REV1, released REV3, and confined VM contact to unit 17 alone.

## 2. What shipped

Eleven planned units, all terminal and completed; eight PRs merged.

- u7 → #1027 relay and tunnel lifecycle
- u8 → #1028 broker readiness and core VM clients
- u9 → #1034 guest exec, push, capture clients
- u10 → #1035 adapter MCP capabilities and lifecycle
- u11 → #1042 Windows skills and hardening regressions
- u13 → #1046 MCP session-state recovery, linked-VM injection gating
- u15 → #1048 Windows guest shell and stdout limits (docs)
- u16 → #1049 Windows GUI app-open and help corrections
- u12, u14, u17 → report-only rst-c downstream acceptance, no PR

Spec clauses #1020, #1021/#1023, #1022, #1024, #1025, #1026, SC-467 and SC-468
are implemented and verified in code, not from PR prose. Full suite on the clean
main checkout at `3780151`: **1942 passed, 947 subtests** (125.9s); no red.

## 3. Judgments and ratified deviations

Review was adversarial throughout: units 7–11 each took three to five
`changes_requested` rounds before approval, including a Major in unit 8 where
`_domain_state` parsed virsh stdout+stderr together, so a successful
`reset --off` reported failure. Units 13, 15 and 16 approved in one round.

One ratified deviation: guest console stdout transliterates non-ASCII on the
return leg (`—`→`-`, `ø`→`o`). The acceptance clause binds the inbound leg,
proven byte-exact on the socket by
`test_broker_wire_payload_round_trips_complex_command_as_utf8_json`; the
corruption is the Windows guest console codepage, outside the engine boundary.
Unit 15 documented it with a base64 workaround rather than changing transport.
Ratified by REV2, judgment #56.

## 4. What failed, retried, or remained anomalous

The downstream acceptance gate never passed, across three attempts and two
re-entry episodes, failing a different way each time:

- Units 12 and 14 — `mcp up` returned `mcp_adapter_unsupported` /
  `SC_HARNESS is not set`. Both were subfloor-launched shells that reached rst-c
  by `cd`, so `SC_HARNESS` was never exported.
- Unit 17 — that cause was corrected (rst-c pinned to `3780151`, shell launched
  inside it, `SC_HARNESS=codex` proven set) and the run still stopped at its
  first preflight command with `sc: unknown command 'vm'`. The runner declared
  its own report INVALID and not gate evidence, and made no VM contact.

I traced unit 17's failure to an engine defect, not the environment:
`update.py:reconcile_linked_dispatchers` recognizes a managed worktree
dispatcher only against a two-generation window (`HEAD:sc`, `engine.ref`,
`engine.ref.prev`), so an older engine-shipped dispatcher is misclassified as a
local edit and frozen permanently. Four of rst-c's five worktrees carry one
byte-identical 1736-line pre-migration dispatcher with no `vm` command; the blob
resolves to rst-c commit `6a34bc12` ("chore(engine): repin to 3d1f9f3").

Operational anomalies: 21 `liveness.escalated` events, all first-episode; one
`pr.poll_failed` that recovered. At close: 129 wakes delivered, 0 failed, 0
nudges, 0 open liveness expectations against 45 resolved. Nothing wedged; no
pause or cancellation.

Process note: report #3 was recorded at 13:39:59 and the Sprint was then
re-entered for units 16 and 17 — the inversion `sprint_rev` warns against.
Report #4 is the correct episode-2 record and supersedes #3's narrative.

## 5. What conformance concluded

Report #4: **pass with follow-ups.** Every requirement spec #108 owns is
as-specced and green on integrated main. One deviation ratified. Spec lines 182
and 188 — live first-turn Windows MCP tool evidence, and one rst-c session
reaching `mcp up`, a GUI observation/action, and `mcp down` — are
**unimplemented/unproven**. Nothing deviated silently.

The Sprint concludes rather than re-enters because the blocker sits outside its
bound scope — #108 is VM driving hardening; the defect is in the spec #105
dispatcher lane — and two episodes were already spent on this gate, each
surfacing a new blocker instead of converging.

## 6. Requiring FnB disposition

- **#10 (Major)** — `update.py` freezes worktree dispatchers older than one
  engine generation, silently stripping every new command. Affects every fork
  and is self-perpetuating: no number of `./sc update` runs recovers a frozen
  worktree. **Safety-relevant now** — the operator sees a correctly reconciled
  root and a correct `engine.ref` while shells run a retired floor.
- **#11 (Major)** — the downstream acceptance gate, unproven after three
  attempts; needs a successor Sprint that fixes #10 first, then re-runs the gate
  under a single authorized VM lane.
- **#12 (Low)** — 21 first-episode liveness escalations; a measurement question
  before any threshold change.
- **#6, #7 (Medium), #8, #9 (Low)** — carried from report #3, still pending.

## 7. Evidence

Conformance reports #3 and #4; follow-ups #6–#12; 42 judgment rows; 8 registered
PRs; timeline `/_sc/sprint/2/timeline`. Working artifacts in the gitignored
`shared/sprints/sprint-2/`: `evidence.json` (`compile-report --limit 50`, no
truncation on any section), `conformance-ep2.md`, `findings-ep2.json`.
