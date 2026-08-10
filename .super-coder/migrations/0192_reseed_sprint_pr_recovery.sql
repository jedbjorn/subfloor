-- 0192 — reseed FnB Sprint PR ownership recovery guidance.
-- Converge existing installs after adding the authenticated reconcile-pr surface.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_pln',
  'Run an armed Sprints v2 collaboration loop as Planner — dispatch ready lanes and execute Reviewer decisions through durable re-plan, pause, cancel, resume, and close protocols.',
  'workflow',
  NULL,
  0,
  '# sprint_pln — govern the armed Sprint

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

The armed runtime owns scheduled dispatch, unread wake recovery, liveness
evaluation, and registered-PR observation. React to its durable inbox and wake
facts; use the Planner turn for dispatch and exact execution of durable Reviewer
decisions. The Reviewer owns pause, cancel, and conclude decisions plus the
conformance and final Sprint reports. The Planner owns control transitions;
clean conformance approval performs its own atomic terminal transition.

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

`monitor` carries no evidence about the PR watcher. When a
watcher-dependent gate has stalled, use the separate bounded read once:

```text
sc sprint watcher-state --sprint <id>
```

It distinguishes a stale or never-started watcher from red, pending, or absent
PR observation and includes the newest bounded poll failures. Do not repeat it
as a polling loop; act on the evidence, then return control to native delivery.

## Questions, answers, blockers, and failures

Put one concrete question, answer, decision, blocker, or useful context item in
a short body file and address the participant who owns the next fact or action:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --key <stable-key>
```

Answer an incoming question through `send`, confirm the write, then mark the
handled message read with `accept`. For a blocker, include evidence, impact,
and the exact action needed. Continue safe independent governance, but stop at
a decision boundary when an answer is required. Unread recovery owns re-waking;
do not send duplicate reminders.

Choose one stable key for the intended recipient and exact body. Reuse it only
when retrying that same write; use a new key when the recipient or body changes.
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
PR''d in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only.

## Reviewer decision actions

Pause, cancel, re-enter, and abort are Reviewer decisions and Planner actions.
A valid control decision arrives as a durable Reviewer → Planner Re-enter
message and names the decision, evidence, target ids, reason, and exact
requested transition. Clean conformance instead closes atomically and sends an
informational engine-wide completion receipt. Mark each handled Sprint message
through `accept`, verify it came from the assigned
Reviewer, and execute that transition without re-adjudicating the decision.
Record the decision message id and action receipt together.
Re-run `sc sprint inbox --sprint <id>` before acting on any Sprint-scoped
control decision; the engine-wide completion receipt requires no Sprint inbox
acceptance.

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

PR ownership inherited from an aborted Sprint is an FnB repair boundary, not a
Planner action. Keep the replacement Sprint paused and surface the exact old
and new ownership. An authenticated FnB/admin shell may reconcile that identity:

```text
sc sprint reconcile-pr --sprint <replacement-id> --repository <owner/repo> \
  --pr <number> --work-unit <replacement-unit-id> --reason <override-reason>
```

The command refuses a live source Sprint or target Sprint, non-code or already
owned target unit, and a closed unmerged PR. It records the old and new owners
plus the live GitHub head. If the PR is already merged, it also records the
merge commit and completes the replacement unit as an explicit FnB override.
Treat the receipt as recovery evidence; wait for a separate Reviewer decision
before resuming. A Planner must never run this command under Planner authority.

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

### Re-enter after conformance

A Reviewer `re-enter` decision names the in-Sprint findings, the tasks to add
to the governing spec document with title and description, and the suggested
unit grouping, waves, dependencies, routing, and capacity rationale. The
Reviewer should identify independent lanes, expected review overlap, and useful
reserve. Preserve that projection; do not silently absorb extra scope, maximize
shell occupancy, or turn post-Sprint findings into delivery work.

Cut every named task against the governing spec document:

```text
sc mem task add "<task-title>" --feature <feature-id> \
  --doc <governing-spec-document-id> --seq <next-seq> \
  --desc "<task-description>"
```

Create the requested bound work units from those task ids, wiring the Reviewer''s
waves and dependencies directly:

```text
sc sprint plan-unit --sprint <id> \
  --developer-shell <id> --reviewer-shell <id> --title <title> \
  --expected-output-file <path> --task <task-id> \
  [--task <task-id>] [--wave <n>] [--depends-on <work-unit-id>] \
  [--output-kind code|report-only|no-code]
```

After every named task is bound, confirm the requested routes are available and
the dependency graph and capacity plan match the decision. If they do not, send
the concrete conflict back to the Reviewer rather than silently re-routing.
Then release the new ready lanes with `sc sprint dispatch --sprint <id>`. When
the added work reaches terminal disposition, the engine sends the Reviewer the
next delivery-terminal wake; the Planner does not initiate the next conformance
pass.

### Conclude or abort

The Reviewer decides when the Sprint is done. A clean `record-conformance`
command atomically stores conformance, follow-ups, the Reviewer-authored final
report, completed lifecycle, and an informational engine-wide Planner receipt.
When that Re-enter arrives, confirm the receipt names the expected Sprint,
reports, outcome, and completed state. Do not run `complete`; closure is already
durable and the notification has no actionable liveness expectation.
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
delivery waits for the prior live turn''s natural boundary; runtime delivery
owns the rest. These are delivery guarantees, not a parent/child chat topology.
The Planner receives no PR-event wakes; Developer-owned subscriptions carry
red, green, and externally closed facts directly to the owning Developer.

Never dispatch the next wave from a merge-observation turn. The Developer''s
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
Sprint.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
