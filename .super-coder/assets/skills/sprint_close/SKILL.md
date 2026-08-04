---
name: sprint_close
description: Close or abort a Sprints v2 run — boot whole-Sprint conformance, compile the bounded evidence packet, synthesize the final report, preserve follow-ups, and transition terminally without deleting history.
category: workflow
common: false
---

# sprint_close — synthesize and finish

Use as the owning Planner when a Reviewer control decision or completed-Sprint
receipt arrives, or when abort has been chosen. The delivery-terminal wake
starts closeout with the Reviewer. A clean conformance approval closes the
Sprint atomically; the Planner is informed after closure and takes no second
close action.
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

The clean-close receipt is an engine-wide Re-enter wake because Sprint-scoped
delivery stops at terminal state. A verified live turn is never displaced; delivery
waits for its natural boundary and drains every undelivered message. An idle
Re-enter resumes the registry chat, while coordinate mode makes it an idle New
ticket chat after FnB closes the Planner chat. Automatic pause preserves that
mode; FnB pause/resume returns to supervise. The inactivity ceiling and reaper
own silent or unlinked processes, not this skill.

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
delivery protocol. The Reviewer decides whether evidence warrants pause,
re-plan, cancellation, or conclusion; the Planner executes control decisions,
while clean conformance approval executes its own close. FnB
retains the board-level override from decision #46. Send any needed participant
context before the Planner acts; an active relay is not available after the
lifecycle becomes paused.

Treat an exhausted recovery wake as bounded manual-recovery evidence for FnB;
preserve the unread message and failed wake, and do not create recursive
fallbacks.

On a durable Reviewer pause decision (or live FnB override), the Planner runs
`sc sprint pause --sprint <id> --reason <decision-reason>`.
For any Sprint-scoped control wake, re-run `sc sprint inbox --sprint <id>`
before acting; the engine-wide clean-close receipt is already self-contained.

## Sprint artifact paths

Sprint working artifacts (per-unit review notes, raw diffs, evidence packets,
report drafts, and Dev scratch proof) go to the gitignored
`shared/sprints/sprint-<n>/` directory. They are never committed, branched, or
PR'd in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only.

## Delivery-terminal entry

When every planned work unit becomes terminal, the engine sends the
delivery-terminal wake directly to the participating Reviewer. That wake is the
close protocol's entry signal; the Planner does not need to notice terminal
state or request conformance.

On entry, the Reviewer re-reads work units, dependencies, registered PRs,
checks, merge observations, task membership, pending actionable messages, and
active native runs. If any unit is non-terminal, the wake is stale and the
Reviewer exits until the next delivery-terminal episode. Close-out is advisory:
the packet surfaces gaps but never replaces Reviewer judgment or the FnB
board-level override. Do not infer code completion from PR state alone.

## Conformance boundary

The Reviewer first decides whether any finding requires in-Sprint patching. If
so, it defers `record-conformance` and sends the Planner a durable re-enter
decision naming the new spec tasks and suggested unit projection. The added
units run through delivery and produce a fresh delivery-terminal wake.

For a clean pass or post-Sprint-only findings, the Reviewer prepares its report,
findings, final Sprint report, reason, and outcome before calling
`sc sprint record-conformance`. That one transaction records both reports and
follow-ups, completes the Sprint, resolves terminal liveness, and publishes an
informational engine-wide Re-enter receipt plus wake to the originating Planner.
Every recorded finding becomes a pending follow-up for FnB review; it is not
also reopened as an editing lane. A safety finding may still demand immediate
operator action.

Verify conformance report id, final report id, follow-up ids, completed state,
Planner message id, Planner wake id, author identity, and idempotent replay. The
engine generates the completion receipt from those committed facts; no Planner
close command or second conclude message follows.

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

The participating Reviewer generates the packet through the authenticated
production surface by default. Planner and FnB compilation remain valid when
FnB explicitly directs the fallback:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
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
acceptable. That judgment remains the Reviewer's.

## Final report

The Reviewer writes a concise report that answers:

1. What scope and exact revisions governed the Sprint?
2. What was planned, what actually shipped, and which PRs produced it?
3. Which judgments and intentional deviations shaped the result?
4. What failed, retried, paused, recovered, or remained anomalous?
5. What did conformance conclude?
6. Which unresolved items and follow-ups now require FnB disposition?
7. Where can the complete evidence be inspected?

Name discrepancies; do not smooth them into a success narrative. A recovered
stall can be a successful Sprint when the failure stayed durable, visible, and
contained. Evidence packets, conformance drafts, and final report drafts belong
under `shared/sprints/sprint-<n>/`. The Reviewer finishes this report before
the atomic conformance write; that write stores it unchanged as the final report.

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

The Reviewer's successful clean `record-conformance` command is the terminal
handoff. It stores the Reviewer-authored final synthesis, transitions the Sprint
to `completed`, resolves terminal liveness, and queues an informational Planner
receipt in one transaction. The Planner does not accept a close decision or run
`complete`; the receipt confirms the Sprint is already terminal.

Immediately before `record-conformance`, the Reviewer drains the Sprint inbox,
confirms the final report, reason, outcome, and stable key, then performs the
atomic command as the literal last action. Terminal state stops Sprint services
and removes live pills while retaining conversations, messages, events, PR
evidence, reports, and follow-ups.

The standalone `complete` surface remains only for an FnB-directed fallback.
Abort remains a Planner action on a Reviewer decision or FnB override:

```text
sc sprint abort --sprint <id> --reason <reason> [--outcome <outcome>]
```

After `record-conformance` succeeds, emit one bounded final response from its
receipt and run no further Sprint command. Terminal lifecycle removes Sprint
authority and live pills but does not close the shell's registry chat; FnB close
remains the one unconditional chat-displacement path.
