---
name: sprint_pln
description: Run an armed Sprints v2 collaboration loop as Planner — dispatch and restructure lanes, change participant routes, and execute Reviewer decisions through durable pause, resume, and close protocols.
category: workflow
common: false
---

# sprint_pln — govern the armed Sprint

Use as the originating Planner after `sprint_prep` arms the Sprint. The system
captures deterministic facts; the Reviewer decides and documents, and the
Planner acts. Execute Reviewer decisions without taking over their judgment or
report authorship. The FnB retains the board-level override established by
decision #46.

Use the simplest path supported by current durable state. Treat authority,
lifecycle preconditions, durable writes, and typed handoffs as hard boundaries;
use judgment for investigation and execution within them. Repeat a read only
when later activity could have changed it or the next command requires live
revalidation.

## Route the entry

Classify the entry before reading an inbox:

- For a Sprint-scoped control decision, merged-work handoff, question, blocker,
  or other relay message, inspect the Sprint inbox once and handle that message.
- For the self-contained engine-wide clean-completion receipt, inspect the
  receipt and terminal Sprint state directly. It is informational: do not run
  the Sprint inbox, accept it, or issue a close command.
- For a live FnB instruction, act under the board-level override and name that
  authority in the durable evidence.

Load `sprint_pln` on every entry. Do not turn entry routing into a polling loop.

## Start from durable state

The armed runtime owns scheduled dispatch and unread wake recovery. The
registered-PR watcher owns subscription observation. React to their durable
facts; use the Planner turn for dispatch, plan structure, and exact execution of
durable Reviewer decisions. The Reviewer owns review and conformance judgment
plus the conformance and final Sprint reports. The Planner owns the plan and its
control transitions: it may modify, repeat, recall, reassign, or reroute work on
its own operational judgment, and it executes Reviewer decisions that cross
those same boundaries. Clean conformance approval performs its own atomic
terminal transition.

Assignments and review requests use Force-new delivery. Planner-bound results
use Re-enter. Neither displaces a live turn; delivery waits for its natural
boundary, and the runtime owns bundling, rotation, recovery, and coordinate
mode. Stop after a successful typed handoff so delivery can proceed cleanly.

Start with the durable trigger, then read only the lifecycle, work-unit,
dependency, route, PR, expectation, or anomaly facts needed for the current
decision. Viewing a participant conversation is observation, not activity;
never manufacture progress from browser presence.

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
  restructure the current projection under Planner authority and record why.
  Ask the Reviewer when the change depends on review or conformance judgment;
  do not outsource ordinary assignment, capacity, or route judgment. Never
  rewrite completed history.
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

Use Sprint-native wakes for coordination. Do not start a recurring shell loop,
scheduled job, manual participant boot, or external PR watcher to track Sprint
state. When a watcher-dependent gate has stalled, use the bounded read once:

```text
sc sprint watcher-state --sprint <id>
```

It distinguishes a stale or never-started watcher from red, pending, or absent
PR observation and includes the newest bounded poll failures. Do not repeat it
as a polling loop; act on the evidence, then return control to native delivery.

## Questions, answers, blockers, and failures

Put one concrete question, answer, decision, blocker, or useful context item in
a short body file. Declare the message intent and whether the required reply
belongs to one work unit or the whole Sprint.

A question or blocker about one lane requires a reply and names that unit:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent question --requires-reply --work-unit <work-unit-id> \
  --key <stable-key>
```

Use `--intent blocker` instead for a blocked lane. A cross-unit, closeout, or
external-authority ruling is a Sprint-level decision:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level --key <stable-key>
```

Answer the stored sender through the original message. The server inherits its
unit or Sprint scope; never add `--work-unit` or `--sprint-level` to a reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent information --reply-to <message-id> --key <stable-key>
```

Confirm the reply write, then mark the handled incoming message read with
`accept`. For a blocker, include evidence, impact, and the exact action needed.
Continue safe independent governance, but stop at a decision boundary when an
answer is required. Unread recovery owns re-waking; do not send duplicate
reminders.

Choose one stable key for the intended recipient, exact body, intent, reply
linkage, and scope. Reuse it only when retrying that same write; use a new key
when any of those fields changes.
Keep the body near 6,000 characters and below the 8,000 hard maximum; run
`wc -m < <path>`. A handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake.

If a command is rejected or transport fails, the handoff is incomplete. Correct
and retry when safe. If the relay itself fails, surface the attempted command
and durable evidence to FnB; do not invent an alternate protocol. Relay a
Developer integrity concern to the Reviewer with its evidence, impact, and
recommendation. Send needed context before pausing because the Sprint relay is
unavailable while paused.

## Sprint artifact paths

Sprint working artifacts (per-unit review notes, raw diffs, evidence packets,
report drafts, and Dev scratch proof) go to the gitignored
`shared/sprints/sprint-<n>/` directory. They are never committed, branched, or
PR'd in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only.

## Reviewer decisions and Planner actions

Review, conformance, re-enter, and abort judgments remain Reviewer decisions.
Planner independently owns operational plan structure, including pause-safe
recall, cancellation of unreleased scope, reassignment, repeated task lanes,
and participant route changes. When an action does arrive as a durable Reviewer
→ Planner Re-enter decision, resolve every required-reply decision through its
original message before acting. Complete this order without reordering:

1. Re-run `sc sprint inbox --sprint <id>`, verify the decision came from the
   assigned Reviewer, and retain its message id.
2. Put a short acknowledgement of the exact requested transition in a body
   file, then send the linked reply:

```text
sc sprint send --sprint <id> --to <reviewer-shortname> --body-file <path> \
  --intent information --reply-to <decision-message-id> \
  --key <stable-control-reply-key>
```

3. Require the reply command to confirm its durable message and wake. Retry the
   same command and key if it fails or does not confirm both.
4. Mark the original decision accepted and require a successful receipt:

```text
sc sprint accept --sprint <id> --message <decision-message-id>
```

5. Only after acceptance confirms, execute the requested transition without
   re-adjudicating the decision. The linked reply must precede any pause or
   abort that makes the Sprint relay unavailable.

Record the decision message id, reply receipt, acceptance receipt, and action
receipt together. Clean conformance instead closes atomically and sends an
informational engine-wide completion receipt; it requires no linked reply or
Sprint inbox acceptance.

The FnB board-level override from decision #46 is unaffected: a live FnB
instruction may direct or supersede any action. Name that override in the
evidence instead of attributing it to the Reviewer.

If a requested action fails a lifecycle, authority, or disposition precondition,
do not substitute a different action. Send the refusal and current durable state
back to the Reviewer (or surface it directly to FnB for an FnB override), then
stop at that decision boundary.

### Pause or resume

Pause on a Reviewer decision or when Planner needs a safe restructuring window.
Transition durably, stop external Sprint services, persist interrupt intent,
preserve every partial artifact, and retain the governing judgment and evidence
for recovery:

```text
sc sprint pause --sprint <id> --reason <decision-or-restructure-reason>
```

Resume after the requested recovery/restructure is fully recorded, or on a
later Reviewer decision or FnB override. Reconcile native runs, unread messages,
pending wakes, work units, registered PRs, capacity, and spec drift, then act
with the supplied reason:

```text
sc sprint resume --sprint <id> [--reason <validated-reconciliation-reason>]
```

An exhausted recovery wake is bounded manual-recovery evidence, not a retry
loop. Preserve the unread message and failed wake, involve FnB, and do not create
recursive fallbacks. Drift informs; it never silently blocks resume.

PR ownership inherited from an aborted Sprint is an originating-Planner repair
boundary. Keep the replacement Sprint paused and establish the exact old and
new ownership. The originating Planner may reconcile that identity; the FnB
retains the same operation as a board-level override:

```text
sc sprint reconcile-pr --sprint <replacement-id> --repository <owner/repo> \
  --pr <number> --work-unit <replacement-unit-id> --reason <recovery-reason>
```

The command refuses a live source Sprint or target Sprint, a non-originating
Planner, a non-code or already owned target unit, and a closed unmerged PR. It
records the old and new owners plus the live GitHub head and acting authority.
If the PR is already merged, it also records the merge commit and completes the
replacement unit as explicit recovery evidence. Treat the receipt as recovery
evidence; wait for a separate Reviewer decision before resuming.

### Modify, recall, repeat, reassign, or reroute

Planner may cancel one unreleased work unit with its retained terminal reason.
When cancellation executes a Reviewer decision, preserve that decision id in
the reason and evidence:

```text
sc sprint cancel-unit --sprint <id> --work-unit <id> --reason <cancellation-reason>
```

Edit any subset of an unreleased lane directly. Omitted fields retain their
current values; `--clear-dependencies` is the explicit empty dependency set:

```text
sc sprint replan-unit --sprint <id> --work-unit <id> \
  [--developer-shell <id>] [--reviewer-shell <id>] [--title <title>] \
  [--expected-output-file <path>] [--task <task-id>] [--wave <n>] \
  [--depends-on <work-unit-id> | --clear-dependencies] \
  [--output-kind code|report-only|no-code]
```

Do not edit a released lane in place. To change an accepted or pending
assignment, first pause so dispatch cannot race the edit, recall the unmerged
lane, replan it, then resume:

```text
sc sprint pause --sprint <id> --reason <restructure-reason>
sc sprint recall-unit --sprint <id> --work-unit <id> \
  --reason <why-the-old-assignment-is-obsolete>
sc sprint replan-unit --sprint <id> --work-unit <id> <changed-fields>
sc sprint resume --sprint <id> --reason <validated-replan-reason>
```

Recall preserves the old accepted/declined message and event history, returns
only an unmerged lane to `planned`, and refuses completed, cancelled, or
PR-bound work. For a PR-bound lane, leave it intact and plan a replacement or
use the supported PR-ownership recovery path; never force the projection back.
Resume dispatches a fresh assignment generation.

The same governing spec task may deliberately appear in more than one work
unit. Use this for conformance reruns, repeat verification, or replacement work
whose scope is still governed by the original task. Do not create a duplicate
spec task merely to satisfy lane membership. Each work unit still lists a task
at most once.

To change which model will receive future assignments or reviews, pause first
when the Sprint is armed, clear the participant's released expectation through
recall or completion, then replace its exact route:

```text
sc sprint reroute-participant --sprint <id> --participant-shell <id> \
  --harness <harness> [--model <model>] [--effort <effort>] \
  [--route <display-route>]
```

Prepared Sprints may reroute before arm. Armed Sprints must pause; Developer
routes reject any released lane and Reviewer routes reject an in-review lane.
The engine validates the new route before writing it. Existing chats and runs
stay immutable history; the next Force-new assignment or review request rotates
onto the replacement route. Only already-declared participants can be selected
or rerouted.

If a Developer or Reviewer declines, preserve the reason and choose the
replacement assignment or route from current capacity before issuing a fresh
assignment. Ask the Reviewer only when that choice changes review or
conformance judgment.

### Re-enter after conformance

A Reviewer `re-enter` decision names the in-Sprint findings, the governing
tasks (existing ids when scope is unchanged; new title and description when
scope is new), and the suggested
unit grouping, waves, dependencies, routing, and capacity rationale. The
Reviewer should identify independent lanes, expected review overlap, and useful
reserve. Preserve that projection; do not silently absorb extra scope, maximize
shell occupancy, or turn post-Sprint findings into delivery work.

Reuse an existing task id when the decision repeats or repairs that exact
governing scope. Cut a new task only for genuinely new scope:

```text
sc mem task add "<task-title>" --feature <feature-id> \
  --doc <governing-spec-document-id> --seq <next-seq> \
  --desc "<task-description>"
```

Create the requested bound work units from those task ids, wiring the Reviewer's
waves and dependencies directly:

```text
sc sprint plan-unit --sprint <id> \
  --developer-shell <id> --reviewer-shell <id> --title <title> \
  --expected-output-file <path> --task <task-id> \
  [--task <task-id>] [--wave <n>] [--depends-on <work-unit-id>] \
  [--output-kind code|report-only|no-code]
```

After every named task is bound, confirm the routes are available and the
dependency graph and capacity plan match the decision. Planner may reassign or
reroute for operational capacity; send the concrete conflict back to the
Reviewer when that adaptation changes the Reviewer's scope or conformance
judgment. Then release the new ready lanes with `sc sprint dispatch --sprint <id>`. When
the added work reaches terminal disposition, the engine sends the Reviewer the
next delivery-terminal wake; the Planner does not initiate the next conformance
pass.

### Conclude or abort

The Reviewer decides when the Sprint is done. A clean `record-conformance`
command atomically stores conformance, follow-ups, the Reviewer-authored final
report, completed lifecycle, and an informational engine-wide Planner receipt.
When that Re-enter arrives, confirm the receipt names the expected Sprint,
reports, outcome, and completed state. Do not run `complete`; closure is already
durable and the notification is informational because closure is already
terminal.
Successful completion also closes every other active participant chat
immutably linked to that Sprint. The originating Planner and report-authoring
Reviewer remain open. Do not manually close peer chats as a second closeout
action. Pause, abort, re-entry, failed conformance, and rejected fallback
completion retain their existing no-cleanup behavior.

Do not run `compile-report` by default, synthesize the final report, or
editorialize the Reviewer body. The Reviewer compiles its own evidence. A
Planner compile remains a valid FnB-directed fallback:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
```

Do not wait for or request a conclude action message after the completion receipt.
Abort is likewise an action taken only on a Reviewer decision or FnB override;
it is terminal and deletes nothing.

## Handoffs and stop

Planner → Developer assignments and Developer → Reviewer review requests use
Force-new delivery; Developer/Reviewer → Planner results are Re-enter. Forced
delivery waits for the prior live turn's natural boundary; runtime delivery
owns the rest. These are delivery guarantees, not a parent/child chat topology.
The Planner receives no PR-event wakes; Developer-owned subscriptions carry
red, green, and externally closed facts directly to the owning Developer.

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

On a clean completion receipt, verify the named Sprint is terminal and record
the bounded receipt; run no close command. The Planner does not author a second
report, accept an actionable handoff, or wait for another actor to finish the
Sprint.
