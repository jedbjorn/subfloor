-- 0195 — reseed Sprint role guidance for progress-carrier coordination.
-- Converge typed scoped relays, exact review identity, and delivery-terminal closeout.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_dev',
  'Execute a Sprints v2 Developer lane — accept one assignment, implement and verify it, own the PR through green and review, merge only after live authorization, and record judgment without overlapping edits.',
  'workflow',
  NULL,
  0,
  '# sprint_dev — own one editing lane

Use for an actionable work-unit assignment in an armed Sprint. Marking that
Sprint message read is acceptance and starts work immediately. If you cannot
accept, decline with a concrete reason; never leave it unread and waking.

Use the simplest path supported by current durable state. Treat ownership,
lifecycle preconditions, durable writes, and typed handoffs as hard boundaries;
use judgment for implementation and verification within them. Repeat a read
only when later activity could have changed it or the next command requires
live revalidation.

## Route the entry

Load `sprint_dev` on every entry, then classify the trigger:

- For a Sprint assignment, verdict, question, blocker, or other relay message,
  inspect the Sprint inbox once and handle the relevant message.
- For a self-describing engine-wide PR fact, inspect that fact and the
  registered PR directly. Do not manufacture a Sprint inbox item; perform the
  once-only inbox check immediately before the next typed handoff.
- For a live FnB instruction, preserve its authority distinctly and inspect
  only the durable state needed to act safely.

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Use `accept` or `decline` for actionable work. After acting on an informational
question, answer, blocker, or context message, run `accept` for that message.
For informational messages it only marks the message read; it does not change
Sprint or work-unit state.

## Orient and bound the lane

Read the assignment, expected output, bound spec revision, dependencies,
assigned Reviewer, repository/worktree, merge grant, and prior judgments. One
Developer shell may own one active work unit in the Sprint. Do not start a
second editing lane or edit another shell''s worktree.

If the requirement is ambiguous, choose the shippable reading within your
unit''s scope, record the choice and rationale, and continue. Escalate changes to
the unit boundary, interfaces another unit consumes, deliverable cuts, or scope
growth to the Planner.

Assignments use Force-new delivery; Reviewer verdicts and engine-wide PR facts
use Re-enter. Neither displaces a live turn; delivery waits for its natural
boundary, and the runtime owns bundling, rotation, and recovery. Stop after a
successful typed handoff so the next delivery can proceed cleanly.

## Questions, answers, blockers, and failures

Put one concrete question, answer, blocker, or useful context item in a short
body file. Declare the message intent and whether the required reply belongs to
this work unit or the whole Sprint.

A question or blocker about this lane requires a reply and names the work unit:

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

Ask the Planner about scope, priority, or cross-unit decisions and the Reviewer
about review evidence. Confirm the reply write, then mark the handled incoming
message read with `accept`. For a blocker or integrity concern, send the Planner
evidence, impact, the exact action needed, and your recommendation. Continue
safe independent work, but stop at a decision boundary when an answer is
required. Unread recovery owns re-waking; do not send duplicate reminders.

Choose one stable key for the intended recipient, exact body, intent, reply
linkage, and scope. Reuse it only when retrying that same write; use a new key
when any of those fields changes.

Keep the body near 6,000 characters and below the 8,000 hard maximum; run
`wc -m < <path>`. A handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake.

If a command is rejected or transport fails, the handoff is incomplete. Correct
and retry when safe. If the relay itself fails, surface the attempted command,
evidence, impact, and recommendation to FnB; do not invent an alternate
protocol. A Developer does not pause the Sprint. The Reviewer decides whether
the evidence warrants continuing, re-planning, or pausing; the Planner executes
that decision.

## Sprint artifact paths

Sprint working artifacts (per-unit review notes, raw diffs, evidence packets,
report drafts, and Dev scratch proof) go to the gitignored
`shared/sprints/sprint-<n>/` directory. They are never committed, branched, or
PR''d in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only.

## Build and verify

Sync the assigned repository, work on a feature branch, match the surrounding
code, and implement the smallest complete change. Keep external calls outside
database transactions. Preserve durable identities and append-only evidence.

Verification must exercise the unit''s independent stage gate and realistic
failure paths. A local exploratory number is not merge evidence. Record real CI
failures, anomalous infrastructure failures, retries, review friction, and
known departures for the final report.

Immediately before a typed Developer handoff (`complete-unit`, `register-pr`,
or `request-review`), re-run `sc sprint inbox --sprint <id>` once and act on
anything new; a ruling may have arrived during the build. This is a once-only
pre-handoff check. After the handoff confirms its durable write, stop without a
further inbox pass.

## Report-only or no-code completion

An explicitly planned report-only or no-code lane completes with its durable
result instead of a PR. Code lanes cannot use this path; they complete only
after merge authorization and observation.

Keep the result at about 6,000 characters or fewer; 8,000 is the hard maximum.
Run `wc -m < <path>` before submitting, then require a successful command and
durable completion receipt.

```text
sc sprint complete-unit --sprint <id> --work-unit <id> \
  --result-file <path>
```

Register the PR through the authoritative Sprint surface and retain ownership
until it is green. After `register-pr` succeeds, the native registered-PR
watcher creates an engine-wide subscription owned by your shell. It supplies
self-describing red, green, and externally closed facts as Re-enter wakes even
after chat rotation or outside an armed Sprint. On red, diagnose and fix the PR.
On green, judge readiness rather than forwarding mechanically. Planner and
Reviewer receive no PR-event wakes.

```text
sc sprint register-pr --sprint <id> --repository <owner/name> \
  --pr <number> --work-unit <id>
```

When no local implementation action remains, stop and await the native PR-fact
wake. Use Sprint-native wakes for coordination. Do not start a recurring shell
loop, scheduled job, manual watcher daemon, or external PR watcher to track the
registered PR.

If a watcher-dependent gate has stalled, one bounded inspection is sanctioned:

```text
sc sprint watcher-state --sprint <id>
```

Run it once to distinguish a stale or never-started watcher from red, pending,
or absent PR observation, then stop or return the evidence to the Planner. Do
not repeat the read as a polling loop.

## Review handoff

Complete a review handoff in this exact order. Every review round uses
Force-new delivery, so stop cleanly after the request confirms and let the
Reviewer begin in a fresh chat with the full bundled request:

1. Finish the readiness claim and every local verification step.
2. Perform the once-only typed-handoff inbox check above, act on newly arrived
   messages, and mark every handled informational message read with `accept`.
3. Make the readiness body a bare one-line locator containing only the
   submitting or resubmitting intent, PR URL, registered Sprint PR id, exact
   head SHA, and work-unit id. Include no scope narrative, verification
   evidence, judgment rationale, or review-focus steering. Put only the
   work-unit id and spec reference in the PR body, and write no PR comments or
   annotations.
4. Run `wc -m < <path>` and confirm the locator is below the 8,000-character
   hard maximum.
5. As the literal final action of the turn, send the typed handoff with one
   stable retry key:

```text
sc sprint request-review \
  --sprint <id> --registered-pr <registered-id> \
  --readiness-file <path> --key <stable-key>
```

6. When the command confirms the durable write and Reviewer wake, immediately
   stop and await the native verdict wake. Run no trailing command.

A changes-requested verdict returns as Re-enter to your registry chat. Apply
every blocking finding, re-establish green, and hand back with a new stable
review key and a new bare locator. Do not narrate how prior findings were
cleared; the Reviewer verifies that from the full diff at the new head. Record
disagreements as judgment; the Reviewer owns scope/severity decisions and the
Planner executes any resulting action.

## Merge boundary

Approval alone is stale evidence. Immediately before merge, ask the engine to
re-read live GitHub state and revalidate the armed grant, ownership, work-unit
state, approved head, and checks:

```text
sc sprint authorize-merge \
  --sprint <id> --registered-pr <registered-id>
```

Merge only the exact repository, PR number, and head SHA returned. If the
command refuses, do not work around it; wait for the watcher or return to the
appropriate loop.

## Post-merge handoff

After the exact authorized merge succeeds, complete close-out in this order:

1. Clean the worktree and collect the merged PR identity, merge SHA, unit
   result, verification evidence, and judgments in the handoff body file.
2. Re-run `sc sprint inbox --sprint <id>`, act on newly arrived messages, and
   mark every handled informational message read with `accept`.
3. Run `wc -m < <path>`; keep the report near 6,000 characters and below the
   8,000-character hard maximum.
4. As the literal final action of the turn, send the merged-work handoff to the
   Planner:

```text
sc sprint send --sprint <id> --to <planner-shortname> --body-file <path> \
  --intent handoff --key <stable-merged-handoff-key>
```

5. When the command confirms the durable write and Planner wake, stop
   immediately. Run no trailing Git, Sprint, inbox, cleanup, or status command.
   Automatic merge observation records the durable PR transition; the Planner
   uses this handoff wake to release the next wave.

## Report and stop

Report broken bases, destructive ambiguity, unavailable GitHub, untrustworthy
runners, provider exhaustion, or an unrecoverable environment to the Planner
with evidence, impact, and a recommendation. Stop at the unsafe boundary while
the Reviewer decides whether to continue, re-plan, or pause and the Planner
executes that decision.

Stop when the unit is merged and reported, declined, awaiting Planner/FnB
recovery, returned to review, or paused awaiting a native PR-fact or verdict
wake. For normal review and merge handoffs, the
ordered procedures above place inbox handling before the typed handoff and make
that handoff the turn''s last action. Ask the Planner for later work only after
the current editing lane is terminal.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

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

The armed runtime owns scheduled dispatch and unread wake recovery. The
registered-PR watcher owns subscription observation. React to their durable
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
PR''d in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only.

## Reviewer decision actions

Pause, cancel, re-enter, and abort are Reviewer decisions and Planner actions.
A valid control decision arrives as a durable Reviewer → Planner Re-enter
message and names the decision, evidence, target ids, reason, and exact
requested transition. Resolve every required-reply control decision through its
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

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_rev',
  'Review Sprints v2 work and whole-Sprint conformance — own pause, cancel, and conclude decisions, author the conformance and Sprint reports, and direct Planner actions through durable messages.',
  'workflow',
  NULL,
  0,
  '# sprint_rev — independent review and conformance

Use in one of two modes: a work-unit PR review during the loop, or the final
whole-Sprint conformance pass. Pre-declaration QAQC is a third entry condition,
before a Sprint exists. The evidence differs; independence does not. The
Reviewer decides and documents; the Planner acts. The FnB retains the
board-level override established by decision #46.

Use the simplest path supported by current durable state. Treat independence,
authority, lifecycle preconditions, durable writes, and typed handoffs as hard
boundaries; use judgment for investigation and review within them. Repeat a
read only when later activity could have changed it or the next command
requires live revalidation.

## Route the entry

Classify the entry before reading an inbox:

- Pre-declaration QAQC begins from an explicit Planner or FnB request through
  the ordinary shell-to-shell channel. Read and sign the exact current spec
  body directly; there is no Sprint id or Sprint inbox to inspect yet.
- A work-unit review request or delivery-terminal notification is Sprint-scoped.
  Inspect the Sprint inbox once and accept the actionable request before work.
- For a live FnB instruction, preserve its board-level authority distinctly and
  inspect only the durable state needed for an independent decision.

Record pre-declaration QAQC through the authenticated surface:

```text
sc sprint record-qaqc --document <spec-document-id> \
  --verdict pass [--findings-document <document-id>]
```

Once a Sprint is armed, review and conformance entries arrive through durable
wakes. Load `sprint_rev` on every entry, then use the Sprint inbox for the
Sprint-scoped cases above:

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
```

Decline an actionable request you cannot take, with a concrete reason:

```text
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Use `accept` or `decline` for actionable work. After acting on an informational
question, answer, blocker, or context message, run `accept` for that message.
For informational messages it only marks the message read; it does not change
Sprint or work-unit state.

Review requests use Force-new delivery. Reviewer verdicts to Developers and
decisions to Planners use Re-enter. Neither displaces a live turn; delivery
waits for its natural boundary, and the runtime owns bundling, rotation, and
recovery. Stop after a successful typed handoff. Reviewers never receive
PR-event subscription wakes.

## Questions, answers, blockers, and failures

Put one concrete question, answer, blocker, or useful context item in a short
body file. Declare the message intent and whether the required reply belongs to
one work unit or the whole Sprint. Ask the Developer for missing PR evidence
with a unit-scoped question:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent question --requires-reply --work-unit <work-unit-id> \
  --key <stable-key>
```

Use `--intent blocker` instead when the unit cannot advance. Ask the Planner for
durable state or action-feasibility facts without delegating Reviewer judgment.
A cross-unit, closeout, pause, replan, re-enter, cancel, or abort ruling is a
Sprint-level decision:

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
`accept`. A blocker or integrity concern is evidence for your decision. If
action is needed, send the Planner the decision, impact, exact action, and
recommendation. Continue safe independent review, but stop at the decision
boundary when missing facts prevent an honest decision. Unread recovery owns
re-waking; do not send duplicate reminders.

Choose one stable key for the intended recipient, exact body, intent, reply
linkage, and scope. Reuse it only when retrying that same write; use a new key
when any of those fields changes.

Keep the body near 6,000 characters and below the 8,000 hard maximum; run
`wc -m < <path>`. A handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake.

If a command is rejected or transport fails, the verdict or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command, evidence, impact, and recommendation to FnB; do not invent an
alternate protocol. A Reviewer decides whether the condition warrants
continuing, re-planning, pausing, cancellation, or conclusion; the Planner
executes the durable decision. Record a live FnB override as FnB authority, not
Reviewer judgment.

## Sprint artifact paths

Sprint working artifacts (per-unit review notes, raw diffs, evidence packets,
report drafts, and Dev scratch proof) go to the gitignored
`shared/sprints/sprint-<n>/` directory. They are never committed, branched, or
PR''d in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only.

## Control and conclude decisions

The Reviewer owns all pause, cancel, and conclude decisions and recommendations.
The Planner owns control actions; clean conformance approval atomically performs
its own close. Base a decision on durable Sprint state,
the exact bound revisions, current work/PR facts, progress-carrier evidence, and any
ratified judgment; ambiguous silence is not enough to corrupt a disposition.

Send pause, resume, replan, re-enter, cancel, and abort decisions through the
durable `send` surface above. A clean conclude instead runs the atomic
`record-conformance` close below. Every Reviewer → Planner route is Re-enter.
The Reviewer-authored body must name:

- `decision`: `pause`, `resume`, `replan`, `re-enter`, `cancel`, or `abort`;
- the evidence and rationale owned by the Reviewer;
- exact Sprint/work-unit ids, reason, outcome, and complete action arguments;
- any immediate safety impact that the FnB must see.

The Planner marks the message handled, verifies the assigned Reviewer, and
executes exactly that control transition. The clean completion receipt is
informational because the Sprint is already terminal. The Reviewer never runs
the standalone pause, replan, cancel, resume, complete, or abort action; its
clean `record-conformance` command owns the narrow automatic close. If a control
action is rejected, inspect the returned durable state and issue
a new decision only when the evidence supports one; never ask the Planner to
improvise around a precondition.

The FnB board-level override from decision #46 is unaffected. A live FnB
instruction can direct or supersede any decision; preserve it as a distinct
authority record.

## Severity rubric

This skill owns severity. The governing spec intentionally does not.

- **Critical** — active security/authority violation, destructive corruption,
  or a condition that makes continued operation unsafe.
- **Major** — wrong behavior, data loss, broken invariant, material spec
  violation, or a loop/recovery path that can silently wedge delivery.
- **Medium** — a concrete correctness or recovery gap likely to bite normal
  use soon, including missing negative enforcement or an unreliable handoff.
- **Low** — bounded cleanup, clarity, test depth, or resilience improvement that
  does not make the delivered behavior wrong now.

During a work-unit review, Critical/Major/Medium block approval; Low is a
report note. During close-out conformance, severity does not decide timing: the
Reviewer judges whether each finding requires in-Sprint patching or is an
acceptable post-Sprint follow-up.

## Work-unit review

Accept the actionable review request and retain that exact message id as the
identity of this review round. Its readiness body must be only the bare
locator: submitting or resubmitting intent, PR URL, registered Sprint PR id,
exact head SHA, and work-unit id. Treat scope narrative, verification evidence,
judgment rationale, or review-focus steering in that body as a protocol defect;
do not use it to frame the review. Neither party writes PR comments or
annotations, and the PR body contains only the work-unit id and spec reference.

Bind every inspection and the eventual verdict to the accepted request''s
message id, registered PR, work unit, and exact head. Another request in the
same delivery, another unit assigned to this Reviewer, or role activity in a
different conversation does not belong to this round. Accept and review each
request explicitly; never infer a review lane from Reviewer identity alone.

Review the exact bound spec revision and the full diff at the request''s exact
head, then inspect checks, tests, relevant runtime evidence, and ratified
judgments. Each round is clean: no prior Developer evidence or prose is input,
and prior findings are cleared only when the code at the new head proves they
are cleared. Review code quality, edge cases/failure paths, and spec
conformance. Trace the real path; do not trust names or PR prose.

### Red-check doctrine

Accepted-red is not a legal review outcome. A departure that leaves checks
failing is never acceptable: do not note the failure and approve anyway. The
review handoff remains green-only, without exception or waiver.

`Note it and pass anyway` is the acceptance-shaped anti-pattern. In the
dos-arch incident, a Reviewer accepted known-failing tests as a scoped
departure and created a deadlock: the green-only handoff gate could never pass.
Decision #93 records why this no-waiver rule exists.

When failing checks are within the lane''s ratified scope, record
`changes_requested` so the Developer fixes them and re-establishes green. When
the failures are outside that scope, name the blocking failures in the finding
and send the Planner a `replan` decision through the control protocol. The
Planner must either widen the lane explicitly or cut a follow-up work unit; the
current lane remains unapproved until the resulting work is green.

Read resolved closure evidence through the authenticated memory surface; no SQL
or mutation is needed. Use the exact form for a cited flag and the scoped form
to audit every resolved flag attached to the feature:

```text
sc mem get flags <flag-id>
sc mem get flags --feature <feature-id> --resolved
```

Findings must state:

- severity and concise title;
- violated behavior or invariant;
- exact code/evidence location;
- a reproducible consequence; and
- the fix boundary, without prescribing unnecessary architecture.

Complete a unit verdict in this exact order:

1. Finish the review, findings, and verdict body; no inspection remains after
   this step.
2. Re-run `sc sprint inbox --sprint <id>`, act on newly arrived messages, and
   mark every handled informational message read with `accept`.
3. Run `wc -m < <path>`; keep the verdict near 6,000 characters and below the
   8,000-character hard maximum.
4. As the literal final action of the turn, record the typed verdict through
   the authenticated surface:

```text
sc sprint record-review \
  --sprint <id> --registered-pr <registered-id> \
  --verdict changes_requested --body-file <path> --key <stable-key>
```

5. When the command confirms the durable write and Developer wake, stop
   immediately. Run no trailing command.

Use `approved` only when no Critical/Major/Medium finding remains. The engine
checks that the request was accepted and still binds to the reviewed head,
records judgment evidence and sends a Re-enter wake to the Developer. Do not
message around this surface;
an unrecorded verdict cannot unlock merge.

## Delivery-terminal closeout

The `sprint.delivery_terminal` notification is the entry signal for
whole-Sprint conformance. Retain that exact notification message id and its
delivered wake as the closeout entry identity; another Reviewer turn or an old
terminal notification does not carry this episode. On that wake, inspect the
inbox, lifecycle, and current work-unit state first. If the lifecycle is already
`completed` or `aborted`, mark the informational notification handled with
`accept` and stop. If any non-terminal unit is visible, the wake is stale: mark
it handled with `accept`, exit, and await the next episode''s delivery-terminal
wake. Only an armed Sprint whose units are all terminal enters conformance.

Compile the bounded evidence packet first and do so yourself, then judge
integrated `main` against every governing bound revision, exact recorded
mid-Sprint revision fact, and ratified judgment:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
```

Increase the bound only when truncation counters show the default omitted
needed evidence; 200 is the maximum. The packet supplies facts, not judgment.
If every work unit was cancelled and nothing shipped, the honest decision is
`abort`, not `conclude`.

After classifying the requirements, choose exactly one branch:

- **In-Sprint patching required.** Do not run `record-conformance`. Send the
  Planner a durable `re-enter` decision naming every blocking finding; each
  spec task to cut against the governing spec document, with title and
  description; and the suggested unit grouping, waves, dependencies,
  Developer/Reviewer routing, and capacity rationale. Identify independent
  lanes, expected review overlap, useful reserve, and why additional capacity
  would or would not shorten the critical path. The durable decision is the
  failed-pass record.
  After three re-entry episodes in one Sprint, escalate the non-convergence to
  FnB instead of starting another patch round.
- **Clean or post-Sprint-only findings.** Prepare the conformance report,
  findings, final Sprint report, reason, and outcome. Submit them through the
  atomic `record-conformance` protocol below: the engine commits the evidence,
  completed lifecycle, informational Planner receipt, and wake together. Do not
  send a separate conclude message.

## Whole-Sprint conformance

Review the integrated system, not unit diffs. Classify each requirement as:

- `as-specced`;
- `deviated-intentionally` with its ratified judgment;
- `deviated-silently`; or
- `unimplemented`.

The last two are findings. Include spec document id and work-unit id when known.
For the clean or post-Sprint-only branch, write the conformance report and a
JSON findings array:

```json
[
  {
    "severity": "Major",
    "title": "Integrated seam diverges",
    "body": "Evidence and consequence.",
    "spec_document_id": 46,
    "work_unit_id": 9
  }
]
```

Keep the conformance report and each finding body at about 6,000 characters or
fewer; 8,000 is the hard maximum for each. Run `wc -m < <report>` and length-check
each finding body before submission.

Before recording conformance, author the final Sprint report. Name the Reviewer
as its author and answer:

1. Which exact scope and revisions governed?
2. What shipped, through which work units and PRs?
3. Which Reviewer judgments and ratified deviations shaped the result?
4. What failed, retried, paused, recovered, or remained anomalous?
5. What did conformance conclude?
6. Which follow-ups or unresolved items require FnB disposition?
7. Where is the complete evidence?

Keep the final report at about 6,000 characters or fewer and below the 8,000
hard maximum; run `wc -m < <report>`. Do not smooth discrepancies into a
success narrative. Keep the final report below 8,000 characters, then choose
the exact completion reason, terminal outcome, and stable completion key. The
engine stores the final report unchanged and generates the Planner receipt from
the committed report and follow-up identities.

Record the clean branch as one atomic final write:

```text
sc sprint record-conformance \
  --sprint <id> --body-file <report> --findings-file <json> \
  --final-report-file <final-report> --reason <reason> --outcome <outcome> \
  --key <stable-pass-key>
```

The receipt must name the conformance report id, final report id, follow-up ids,
completed state, Planner message id, and Planner wake id. This creates
append-only evidence, pending follow-ups, terminal lifecycle, and one
informational engine-wide Planner Re-enter in the same transaction. Never
record conformance first and then close around it; send no conclude message.
On that successful commit, the engine also closes every other active chat
immutably linked to the Sprint. The originating Planner and this
report-authoring Reviewer remain open. Do not manually close peer chats as an
extra closeout step. Pause, abort, re-entry, failed conformance, and rejected
fallback completion keep their existing no-cleanup behavior.
Never reopen an editing
lane after recording; the re-enter branch defers the report until added scope
reaches terminal disposition and a fresh delivery-terminal wake starts the next
episode. Surface any immediate safety risk to FnB.

## Stop

For unit review, follow the ordered verdict procedure above: inbox handling and
all evidence work precede `record-review`; the durable verdict is the literal
last action, then the Reviewer stops.

For the clean or post-Sprint-only branch, require both reports, findings,
reason, and outcome to replay idempotently. For the re-enter or abort branch,
confirm that the decision body carries the complete evidence and exact
requested action. Then complete this final handoff order:

1. Re-run `sc sprint inbox --sprint <id>`, act on newly arrived messages, and
   mark every handled informational message read with `accept`.
2. Confirm every Reviewer-authored artifact and decision body is final and
   below its 8,000-character hard maximum.
3. For a clean conclude, run the atomic `record-conformance` command above as
   the literal final action. When it confirms completed state and all receipt
   identities, stop immediately; the Planner is already notified.
4. For re-enter or abort, deliver the decision to the Planner as the literal
   final action:

```text
sc sprint send --sprint <id> --to <planner-shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level \
  --key <stable-decision-handoff-key>
```

5. When the command confirms the durable write and Planner wake, stop
   immediately. Run no trailing command until another native wake arrives.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
