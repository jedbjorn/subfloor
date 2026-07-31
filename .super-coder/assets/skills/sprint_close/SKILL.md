---
name: sprint_close
description: Close or abort a Sprints v2 run — boot whole-Sprint conformance, compile the bounded evidence packet, synthesize the final report, preserve follow-ups, and transition terminally without deleting history.
category: workflow
common: false
---

# sprint_close — synthesize and finish

Use as the owning Planner when delivery work is terminal, or when abort has
been chosen. Close-out supplies meaning; the compiler supplies facts.

## Delivery-complete gate

Before conformance, re-read work units, dependencies, registered PRs, checks,
merge observations, task membership, pending actionable messages, and active
native runs. Every planned unit must be completed/cancelled with an explicit
disposition, and code units must have their real PR outcome. Do not infer task
completion from PR state alone.

Boot an independent Reviewer into `sprint_rev` conformance mode with the bound
spec revision hashes, integrated main SHA, and ratified judgment list. Do not
feed it unit authors' narrative beyond recorded judgments; conformance judges
artifacts.

## Conformance boundary

The Reviewer records its report and findings with `sc sprint
record-conformance`. Every finding becomes a pending follow-up for FnB review.
No conformance finding is fixed inside this Sprint at any severity. A safety
finding may demand immediate operator action, but it still remains follow-up
evidence rather than a silently reopened editing lane.

Verify report id, follow-up ids, author identity, and idempotent replay before
synthesis.

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

Commit the final or abort report before the lifecycle transition. Complete only
after conformance evidence and synthesis are durable; abort only under Planner
or FnB authority. Terminal state stops Sprint services and removes live pills
while retaining conversations, messages, events, PR evidence, reports, and
follow-ups.

Hand the FnB the final report id, follow-up list, integrated SHA, and evidence
links. Stop after the terminal transition; Sprint-scoped authority is over.
