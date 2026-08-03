---
name: sprint_pln
description: Run an armed Sprints v2 collaboration loop as Planner — dispatch ready lanes and execute Reviewer decisions through durable re-plan, pause, cancel, resume, and close protocols.
category: workflow
common: false
---

# sprint_pln — govern the armed Sprint

Use as the originating Planner after `sprint_prep` arms the Sprint. The system
captures deterministic facts; the Reviewer decides and documents, and the
Planner acts. Execute Reviewer decisions without taking over their judgment or
report authorship. The FnB retains the board-level override established by
decision #46.
On every wake or re-entry, load `sprint_pln`, run the exact inbox command below,
and inspect the durable message before deciding what to do.

## Start from durable state

The armed runtime owns scheduled dispatch, unread wake recovery, liveness
evaluation, and registered-PR observation. React to its durable inbox and wake
facts; use the Planner turn for dispatch and exact execution of durable Reviewer
decisions. The Reviewer owns pause, cancel, and conclude decisions plus the
conformance and final Sprint reports. The Planner owns the corresponding state
transitions.

The active-chat registry is the only current-chat authority; zero or one chat
per shell is legal. Every wake message creates delivery intent and a wake turn
drains every undelivered message for the receiver. Planner-bound messages are
declared Re-enter. If a verified turn is live, every declared type enters that
chat at the natural boundary. When idle, Re-enter resumes the registry chat and
New rotates it; no registry row behaves as New.

FnB controls the Planner mode with the close button. Keeping the Planner chat
open supervises one continuous thread. Closing it during an armed Sprint sets
coordinate mode, so later idle Planner-bound Re-enters open fresh ticket chats;
FnB pause/resume returns to supervise. Automatic pauses preserve that choice.
Neither mode displaces a live turn. The inactivity ceiling closes a silent hung
chat, and the registry reaper terminates its now-unlinked process.

Read the Sprint inbox, lifecycle, bound spec revisions, work-unit graph,
participant routes, active conversations, registered PRs, unresolved
expectations, and recent anomalies. Viewing a participant conversation is
observation, not activity; never manufacture progress from browser presence.

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Release every dependency-ready lane through the production surface:

```text
sc sprint dispatch --sprint <id>
```

The returned ids are wake identities. Work-unit disposition and messages are
the authoritative release facts. Dispatch is safe to repeat: occupied Developer
lanes and stable assignment generations prevent double booking.

Accept or decline only when the inbox item is actionable. After acting on an
informational question, answer, blocker, or evidence message, run `accept` for
that message. For informational messages it only marks the message read; it does
not change Sprint or work-unit state.

## Running loop

- Keep dependencies as the only hard sequence. When reality requires a re-plan,
  give the Reviewer the current projection and evidence, then execute the exact
  decision it returns; never rewrite completed history.
- Let Developers own their PRs through green, review, correction, and merge.
  Let Reviewers own verdicts and Sprint decisions. Do not proxy routine
  handoffs or substitute Planner judgment for a Reviewer decision.
- Consume passive system facts without waking yourself into every transition.
  Route a decision boundary to the Reviewer; act when its Re-enter decision
  message arrives.
- Record the Reviewer decision identity, the exact action taken, and the action
  receipt. Do not rewrite its rationale as Planner-authored judgment evidence.
- A mid-Sprint spec edit is allowed only by the owning Planner or FnB. Record
  the prior and new exact revision hashes. When acting as Planner, require a
  durable Reviewer decision before the edit. The running Sprint remains bound
  to its approved revision unless that decision explicitly says otherwise.

The armed runtime evaluates liveness on its five-second pulse. A one-shot
diagnostic/evaluation is available when evidence requires it:

```text
sc sprint monitor --sprint <id>
```

Run `monitor` once for concrete evidence, then return control to native
delivery. It evaluates only due accepted expectations and its
nudge/escalation identities are durable. Use Sprint-native wakes for
coordination. Do not start a recurring shell loop, scheduled job, manual
participant boot, or external PR watcher to track Sprint state.

## Questions, answers, blockers, and failures

Put a concrete question, answer, decision, blocker, or useful context in a short
body file and address the participant who owns the needed fact or action:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --key <stable-key>
```

Answer incoming questions through `send` so the answer is durable and wakes the
asker, confirm that write, then mark the handled question read with `accept`.
For a cross-unit blocker, send evidence, impact, and the exact action needed to
every directly affected participant. Continue safe independent governance, but
stop at a decision boundary when an answer is required. Do not spam duplicates
when no response is immediate; unread recovery owns re-waking.

Choose one stable key for the intended recipient and exact body. Reuse it only
when retrying that same write; use a new key when the recipient or body changes.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command and durable evidence to FnB; do not invent an alternate
delivery protocol. When a Developer reports an integrity concern, relay its
evidence, impact, and recommendation to the Reviewer. When the Reviewer returns
a decision, execute it through the protocol below. Send any needed participant
context before pausing; an active relay is not available after the lifecycle
becomes paused.

## Reviewer decision actions

Pause, cancel, and conclude are Reviewer decisions and Planner actions. A valid
decision arrives as a durable Reviewer → Planner Re-enter message and names the
decision, evidence, target ids, reason, and exact requested transition. Accept
the actionable message, verify it came from the assigned Reviewer, and execute
that transition without re-adjudicating the decision. Record the decision
message id and action receipt together.

The FnB board-level override from decision #46 is unaffected: a live FnB
instruction may direct or supersede any action. Name that override in the
evidence instead of attributing it to the Reviewer.

If a requested action fails a lifecycle, authority, or disposition precondition,
do not substitute a different action. Send the refusal and current durable state
back to the Reviewer (or surface it directly to FnB for an FnB override), then
stop at that decision boundary.

### Pause or resume

On a Reviewer pause decision, transition durably, stop external Sprint services,
persist interrupt intent, preserve every partial artifact, and retain the
Reviewer judgment and evidence for recovery:

```text
sc sprint pause --sprint <id> --reason <reviewer-decision-reason>
```

Resume only on a later Reviewer decision or an FnB override. Reconcile native
runs, unread messages, pending wakes, work units, registered PRs, capacity, and
spec drift, then act with the supplied reason:

```text
sc sprint resume --sprint <id> [--reason <reviewer-reconciliation-decision>]
```

An exhausted recovery wake is bounded manual-recovery evidence, not a retry
loop. Preserve the unread message and failed wake, involve FnB, and do not create
recursive fallbacks. Drift informs; it never silently blocks resume.

### Cancel or re-plan

A Reviewer cancel decision must name one unreleased work unit and its retained
terminal reason. Execute exactly that cancellation:

```text
sc sprint cancel-unit --sprint <id> --work-unit <id> --reason <reviewer-decision-reason>
```

For a Reviewer re-plan decision, apply its complete projection; never infer
omitted fields or alter a released or completed lane:

```text
sc sprint replan-unit --sprint <id> --work-unit <id> \
  --developer-shell <id> --reviewer-shell <id> --wave <n> \
  [--depends-on <work-unit-id>] [--output-kind code|report-only|no-code]
```

If a Developer or Reviewer declines, preserve the reason and ask the Reviewer
for the replacement routing decision before issuing a fresh assignment.

### Conclude or abort

The Reviewer decides when the Sprint is done, records conformance, authors the
final Sprint report, and sends a conclude decision containing the conformance
receipt, follow-up ids, exact reason/outcome, stable key, and full report body.
Write that Reviewer-authored body to `<reviewer-report>` unchanged, then perform
the close action:

```text
sc sprint complete --sprint <id> --reason <reviewer-decision-reason> \
  --outcome <reviewer-decision-outcome> --report-file <reviewer-report> \
  --key <reviewer-decision-key>
```

Do not run `compile-report`, synthesize the final report, or editorialize the
Reviewer body. Abort is likewise an action taken only on a Reviewer decision or
FnB override; it is terminal and deletes nothing.

## Handoffs and stop

Planner → Developer assignments are declared New; Developer → Reviewer review
requests are declared New; Developer/Reviewer → Planner results are Re-enter.
These are idle-time guarantees under the live-turn boundary rule, not a
parent/child chat topology. The Planner receives no PR-event wakes;
Developer-owned subscriptions carry red, green, and externally closed facts
directly to the owning Developer.

Never dispatch the next wave from a merge-observation turn. The Developer's
merged-work handoff wake is the only normal next-wave dispatch trigger. On that
wake, complete the turn in this exact order:

1. Run `sc sprint inbox --sprint <id>` and inspect the durable merged-work
   handoff plus current work-unit and dependency state.
2. Act on every earlier informational item and mark each handled item read with
   `accept`, including the Developer handoff.
3. Finish all reconciliation, judgment recording, and other Planner
   bookkeeping. No work remains after this step.
4. As the literal final action of the turn, release dependency-ready lanes:

```text
sc sprint dispatch --sprint <id>
```

5. When the command confirms the durable assignment writes and New wakes, stop
   immediately. Run no trailing command. Empty dispatch is still the final
   action for that handoff turn; investigate only on a later durable wake.

When all planned delivery work is terminal and merged or explicitly no-code,
send the Reviewer the bound revisions, integrated main SHA, ratified judgments,
and current close evidence, then stop and await its Re-enter conclude decision.
The Reviewer writes the conformance and final Sprint reports; conformance
findings become follow-ups rather than new editing lanes in this Sprint.

On receipt, re-run `sc sprint inbox --sprint <id>`, accept the conclude message,
execute its exact close action through the protocol above, and confirm the typed
transition succeeded. After `complete` succeeds, emit its bounded receipt and
run no further Sprint command. The Planner does not author a second report.
