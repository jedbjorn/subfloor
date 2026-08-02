---
name: sprint_close
description: Close or abort a Sprints v2 run — boot whole-Sprint conformance, compile the bounded evidence packet, synthesize the final report, preserve follow-ups, and transition terminally without deleting history.
category: workflow
common: false
---

# sprint_close — synthesize and finish

Use as the owning Planner when delivery work is terminal, or when abort has
been chosen. Close-out supplies meaning; the compiler supplies facts.
On entry or any wake, load `sprint_close`, run `sc sprint inbox --sprint <id>`,
inspect the durable message, and accept or decline it only when actionable.

```text
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Use `accept` or `decline` for actionable work. After acting on an informational
question, answer, blocker, or context message, run `accept` for that message.
For informational messages it only marks the message read; it does not change
Sprint or work-unit state.

## Questions, answers, blockers, and failures

Put a concrete question, answer, blocker, or context request in a short body
file, then send it to the conformance Reviewer or participant who owns the fact:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --key <stable-key>
```

Answer incoming questions through `send`, confirm that write, then mark the
handled question read with `accept`. For a blocker, relay the evidence, impact,
and exact action needed to every directly affected Sprint participant, and
surface the exceptional recovery need separately to FnB. Continue safe
synthesis, but stop at a decision boundary when the answer is required. Do not
send duplicate reminders; unread recovery owns re-waking.

Choose one stable key for the intended recipient and exact body. Reuse it only
when retrying that same write; use a new key when the recipient or body changes.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command and durable evidence to FnB; do not invent an alternate
delivery protocol. This skill is Planner-owned: the Planner or FnB decides
whether an integrity threat warrants pause. Send any needed participant context
before pausing; an active relay is not available after the lifecycle becomes
paused.

Treat an exhausted recovery wake as bounded manual-recovery evidence for FnB;
preserve the unread message and failed wake, and do not create recursive
fallbacks.

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

## Delivery-complete gate

Before conformance, re-read work units, dependencies, registered PRs, checks,
merge observations, task membership, pending actionable messages, and active
native runs. Normally every planned unit is completed/cancelled with an
explicit disposition, and code units have their real PR outcome. Close-out is
advisory: the packet surfaces gaps but never prevents the owning Planner or FnB
from making and reporting a completion judgment. Do not infer code completion
from PR state alone.

Request an independent Reviewer through the durable Sprint relay, using the
`sc sprint send` command above with the bound spec revision hashes, integrated
main SHA, ratified judgment list, and `sprint_rev` conformance mode. Confirm the
write and wake receipt, then stop and await the native conformance-result wake.
Give the Reviewer recorded judgments, not unit authors' narrative; conformance
judges artifacts.

## Conformance boundary

The Reviewer records its report and findings with `sc sprint
record-conformance`. Every finding becomes a pending follow-up for FnB review.
No conformance finding is fixed inside this Sprint at any severity. A safety
finding may demand immediate operator action, but it still remains follow-up
evidence rather than a silently reopened editing lane.

Verify report id, follow-up ids, author identity, and idempotent replay before
synthesis.

FnB records one terminal disposition per follow-up. `accepted` acknowledges
ship-as-is; `resolved` and `dismissed` require a resolution file.

Keep a resolution at about 6,000 characters or fewer; 8,000 is the hard
maximum. Run `wc -m < <path>` and require a successful durable disposition.

```text
sc sprint disposition-followup --sprint <id> --followup <id> \
  --disposition accepted
sc sprint disposition-followup --sprint <id> --followup <id> \
  --disposition resolved --resolution-file <path>
```

## Compile bounded evidence

Generate the packet through the authenticated production surface:

```text
sc sprint compile-report --sprint <id> --limit 50 > evidence.json
```

Increase the per-section bound only when the default truncation counters show
that synthesis needs more detail; the maximum is 200. Follow the packet's full
timeline and participant-conversation links for raw history. Never paste the
entire event stream into the final report.

The packet supplies:

- scope and lifecycle times;
- exact bound spec revisions and recorded mid-Sprint edits;
- planned versus actual work;
- PR outcomes and links;
- judgments and deviations;
- pause, resume, recovery, interrupt, and drift evidence;
- wake states, attempts, liveness aggregates, nudges, and escalations;
- anomalies with bounded detail;
- unresolved units, actionable messages, and follow-ups; and
- links to the complete timeline and every participant conversation.

The compiler does not decide whether a deviation was wise or an anomaly was
acceptable. That is your synthesis.

## Final report

Write a concise report that answers:

1. What scope and exact revisions governed the Sprint?
2. What was planned, what actually shipped, and which PRs produced it?
3. Which judgments and intentional deviations shaped the result?
4. What failed, retried, paused, recovered, or remained anomalous?
5. What did conformance conclude?
6. Which unresolved items and follow-ups now require FnB disposition?
7. Where can the complete evidence be inspected?

Name discrepancies; do not smooth them into a success narrative. A recovered
stall can be a successful Sprint when the failure stayed durable, visible, and
contained.

## Pause and abort reports

When closing after a pause, include the integrity threat, deterministic state at
pause, interrupt delivery, reconciliation, spec drift, judgment, and resume
outcome. Keep this section behind the pause/recovery evidence seam; missing
optional pause facts must not prevent compiling an otherwise valid packet.

Abort is terminal and history-preserving. Its report names reason, completed
work, partial artifacts, outstanding work, active interruption outcome, and
recovery disposition. A prepared Sprint may abort with a stub report; delete
nothing.

## Terminal handoff

Pass the final synthesis to `complete`; the surface commits the append-only
`final` report before attempting the lifecycle transition. Omitting the report
is permitted under advisory close-out, but the evidence packet records the gap.
Abort only under Planner or FnB authority. Terminal state stops Sprint services
and removes live pills while retaining conversations, messages, events, PR
evidence, reports, and follow-ups.

Immediately before `complete`, re-run `sc sprint inbox --sprint <id>` to drain
newly arrived messages, mark every handled informational message read with
`accept`, and confirm the final report file is the intended synthesis. This is
the last pre-terminal evidence read.

Keep the final report at about 6,000 characters or fewer; 8,000 is the hard
maximum. Run `wc -m < <path>` before the typed terminal handoff, then require
the successful report receipt and lifecycle transition.

```text
sc sprint complete --sprint <id> --reason <summary> --outcome <outcome> \
  --report-file <path> --key <stable-key>
sc sprint abort --sprint <id> --reason <reason> [--outcome <outcome>]
```

After `complete` succeeds, emit one bounded final response from its receipt:
final report id, follow-up list, integrated SHA, and evidence links. Run no
further Sprint command; close intent terminalizes the owning conversation and
Sprint-scoped authority is over.
