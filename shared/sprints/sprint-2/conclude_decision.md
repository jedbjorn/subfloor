# REVIEWER DECISION — Sprint 2 — conclude

decision: **conclude** · sprint 2 · feature #38
spec #108 @ `4b1b2cf7…a626e4` (no mid-Sprint edits)
authority: REV1, assigned Sprint 2 Reviewer. Reviewer judgment.
immediate safety impact: **none**.

## Rationale (Reviewer-owned)

All nine planned units are terminal and completed, none cancelled; seven PRs
merged, tip `f9dbc9c`. I compiled the evidence packet myself (no truncation),
read spec #108 in full, diffed `07f1c0b..origin/main`, traced each acceptance
clause in code, and ran the full suite on a clean checkout at `f9dbc9c`:
**1939 passed, no red**. Every automated clause is met; nothing deviated
silently.

Two live-session gates are unproven, and **no code change closes them** — they
need a shell launched inside rst-c on an engine pinned to `f9dbc9c`, an
operator act rather than a delivery lane. One re-entry episode already ran and
did not close them for exactly this reason; a second would repeat the pattern.
They are post-Sprint follow-ups. Cause analysis in section 5 below.

Conformance recorded: report **#3**, follow-ups **#6–#9** (idempotent on
replay). My flags #202/#203 are closed with notes.

## Exact action for the Planner

Write everything after this section to a file and submit it **unchanged**:

```
sc sprint complete --sprint 2 --outcome "delivered" \
  --reason "9/9 units completed; 7 PRs merged to f9dbc9c; suite green (1939 passed). Conformance report #3: pass with follow-ups #6-#9 for FnB disposition." \
  --report-file <that-file> \
  --key sprint:2:complete:rev1:conclude:1
```

conformance report id: 3 · follow-up ids: 6, 7, 8, 9

## Final report body (submit verbatim)

# SPRINT REPORT: Sprint 2 — Windows VM Driving Hardening

Author: REV1 (Review-01), Sprint 2 Reviewer. Feature #38.

## 1. Governing scope and revisions

Spec document #108 at bound revision `4b1b2cf7…a626e4`, signed pass by REV1 at
06:25 (approval #7) before declaration, and still the document's current
revision: **zero mid-Sprint spec edits**, so every unit was reviewed against
the text it was planned from. Scope covered subfloor issues #1020–#1026 plus
flags SC-467 and SC-468. Armed 06:31, delivery terminal 13:29.

## 2. What shipped

Nine planned units, all completed, none cancelled. Seven implementation PRs,
all merged to `jedbjorn/subfloor` `main`, final tip `f9dbc9c`:

- **u7** relay/tunnel lifecycle — DEV3/REV1 — PR #1027 → `4c34fff`
- **u8** broker readiness + core VM clients — DEV4/REV2 — #1028 → `387a8c0`
- **u13** MCP session-state recovery + linked-VM gating — DEV6/REV1 — #1046 → `5c75284`
- **u9** guest exec/push/capture clients — DEV5/REV3 — #1034 → `d65fb4a`
- **u10** adapter MCP capabilities + lifecycle — DEV6/REV1 — #1035 → `89e9258`
- **u15** Windows guest shell + stdout limits — DEV3/REV2 — #1048 → `f9dbc9c`
- **u11** Windows skills + hardening regressions — DEV3/REV2 — #1042 → `003f6ae`
- **u14 / u12** rst-c downstream acceptance (rerun / original) — DEV4/REV3 — report-only

Net: 27 files, +6,579/−395. Three reseed migrations (0180–0182) distributed the
revised skills. Full suite on integrated main: **1939 passed, 945 subtests**,
no skips, no red.

## 3. Reviewer judgments and ratified deviations

Units 7–11 each took two to five rounds; every round recorded
`changes_requested` with findings, and every approval was proved against the
new head, not the Developer's account of it. Unit 8 ran five rounds, opening on
a Major — `_domain_state` parsing virsh stdout+stderr together, which made a
successful `reset --off` report failure. Units 13 and 15 approved in one round.
No review was accepted red; the green-only handoff held throughout.

One ratified deviation: guest console stdout transliterates non-ASCII on the
return leg (`—`→`-`, `ø`→`o`), seen in both acceptance runs. Unit 15 documented
the limit in `windows_devkit` with a base64 workaround rather than changing
transport. I hold that correct — clause #1024 binds the inbound leg, proven
byte-exact on the socket, and the corruption is the Windows guest codepage,
outside the engine boundary. Ratified by REV2, judgment #56.

## 4. What failed, retried, or stayed anomalous

- **One re-entry episode.** After unit 12's acceptance surfaced gaps, I sent
  PLN1 a reconciliation decision superseding my #152; units 13, 15, and the
  unit 14 rerun were cut. The episode delivered its code fixes but did not
  close the acceptance gate — see below.
- **Both acceptance runs are incomplete, honestly.** Unit 12 (12:24) and unit
  14 (13:29, after the re-entry fixes merged) both confirmed status, start,
  push, complex exec, capture, `mcp down`, and the sole final `reset --off` to
  snapshot `clean` powered-off. Both failed `mcp up` with
  `mcp_adapter_unsupported` / `SC_HARNESS is not set` and performed no GUI
  action. Neither Developer claimed an unconfirmed operation succeeded, as
  spec line 192 requires.
- **20 liveness escalations and 1 PR poll failure**, all single-episode and
  self-recovered; 102 wakes delivered, 42 expectations resolved, 0 open, 0
  failed, 0 pauses. Process noise, not delivery damage.
- **Unit 13's durable `expected_output` holds 23.5KB of pre-Sprint `vm.py`**
  instead of a deliverable description (my flag #203). Resolved as a Planner
  slip in a path argument, not an engine defect: `sprint_cli.py:_text` reads
  the named file verbatim. DEV6 was corrected by durable message and shipped
  correctly, but the permanent record misstates that unit's intent.

## 5. What conformance concluded

**Pass with follow-ups** (report #3). Every automated acceptance clause —
#1020, #1021/#1023, #1022, #1024, #1025, #1026, SC-467, SC-468 — is met and
green on integrated main, each verified in code at `f9dbc9c` rather than from
PR prose. Nothing deviated silently.

Two acceptance gates remain unproven: the live-Codex first-turn MCP tool
minimum (line 182) and downstream gate step 3's `mcp up` + one GUI action
(line 188). I traced the cause rather than accept the symptom. `SC_HARNESS` is
set only by `run.py` at launch and is new this Sprint; MCP injection is further
gated on `linked_vm_configured()`. **subfloor has no `vm` block; rst-c does.**
Both acceptance runs were performed by subfloor-launched shells reaching rst-c
by `cd`, so neither could carry `SC_HARNESS` nor receive an injected MCP
definition — and that gating is itself correct and tested. The engine behaved
exactly as specced, including refusing `up` before any transport mutation.

No code change closes these gates. They need a shell **launched inside rst-c**
on an engine pinned to `f9dbc9c` (rst-c sits at `e4778ec`, not an ancestor of
main). That is an operator act, not a delivery lane — which is why I route it
as a follow-up rather than spend a second re-entry episode repeating the
pattern that already failed to close it once.

## 6. Follow-ups requiring FnB disposition

Report #3, follow-ups #6–#9:

- **#6 (Medium)** — live first-turn Windows MCP tool evidence never obtained.
- **#7 (Medium)** — downstream gate step 3 incomplete; tunnel→relay→endpoint has
  unit coverage but no live end-to-end proof.
- **#8 (Low)** — Revit did not remain open after open-if-absent; likely guest
  behavior, and application-specific automation is out of scope.
- **#9 (Low)** — `plan-unit` accepts an unbounded `expected_output`; a bounded
  length check would have caught the 23.5KB body at submission.

#6 and #7 are one operator action: `./sc update` in rst-c, launch a Codex shell
there, record the first-turn inventory, then `mcp up` → one GUI action →
`mcp down` → sole final `reset --off`. My flags #202 and #203 are both closed
with notes; #202's proposed remedy shipped in unit 13.

## 7. Where the evidence is

Conformance report #3 and follow-ups #6–#9 in the engine DB; judgments #17–#56;
timeline at `/_sc/sprint/2/timeline`. Working artifacts (evidence packet, spec
body, conformance report, findings) in gitignored `shared/sprints/sprint-2/`.
Integrated target `f9dbc9c`; pre-Sprint base `07f1c0b`.
