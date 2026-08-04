-- 0179 — make Reviewer conformance and Sprint completion atomic.
-- Full-body UPSERTs converge the three closeout role skills.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_close',
  'Close or abort a Sprints v2 run — boot whole-Sprint conformance, compile the bounded evidence packet, synthesize the final report, preserve follow-ups, and transition terminally without deleting history.',
  'workflow',
  NULL,
  0,
  '# sprint_close — synthesize and finish

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
PR''d in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only.

## Delivery-terminal entry

When every planned work unit becomes terminal, the engine sends the
delivery-terminal wake directly to the participating Reviewer. That wake is the
close protocol''s entry signal; the Planner does not need to notice terminal
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
that synthesis needs more detail; the maximum is 200. Follow the packet''s full
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
acceptable. That judgment remains the Reviewer''s.

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

The Reviewer''s successful clean `record-conformance` command is the terminal
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
authority and live pills but does not close the shell''s registry chat; FnB close
remains the one unconditional chat-displacement path.',
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
On every wake or re-entry, load `sprint_pln`, run the exact inbox command below,
and inspect the durable message before deciding what to do.

## Start from durable state

The armed runtime owns scheduled dispatch, unread wake recovery, liveness
evaluation, and registered-PR observation. React to its durable inbox and wake
facts; use the Planner turn for dispatch and exact execution of durable Reviewer
decisions. The Reviewer owns pause, cancel, and conclude decisions plus the
conformance and final Sprint reports. The Planner owns control transitions;
clean conformance approval performs its own atomic terminal transition.

The active-chat registry is the only current-chat authority; zero or one chat
per shell is legal. Every wake message creates delivery intent and a wake turn
drains every undelivered message for the receiver. Assignments and review
requests use Force-new delivery: a live turn finishes, the receiver remains
quiet for the configured grace interval, then delivery atomically closes the
exact registry chat and starts a fresh chat with the complete undelivered
bundle. Concurrent Force-new wakes coalesce into one rotation, and retries reuse
the chat created for their own wake. Participants must stop cleanly after typed
handoffs so the next forced delivery can cross the quiet boundary. The
inactivity ceiling and registry reaper remain the fallback for a silent hung
turn.

Planner-bound messages are Re-enter. Plain New remains separate and is eligible
immediately. It enters a verified live turn at its natural boundary and rotates only when the
registry chat is idle. Re-enter resumes the registry chat; no registry row
behaves as New.

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

`monitor` carries no evidence about the PR watcher. When a
watcher-dependent gate has stalled, use the separate bounded read once:

```text
sc sprint watcher-state --sprint <id>
```

It distinguishes a stale or never-started watcher from red, pending, or absent
PR observation and includes the newest bounded poll failures. Do not repeat it
as a polling loop; act on the evidence, then return control to native delivery.

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
unit grouping, waves, dependencies, and routing. Preserve that projection; do
not silently absorb extra scope or turn post-Sprint findings into delivery work.

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

After every named task is bound and the dependency graph matches the decision,
release the new ready lanes with `sc sprint dispatch --sprint <id>`. When the
added work reaches terminal disposition, the engine sends the Reviewer the next
delivery-terminal wake; the Planner does not initiate the next conformance pass.

### Conclude or abort

The Reviewer decides when the Sprint is done. A clean `record-conformance`
command atomically stores conformance, follow-ups, the Reviewer-authored final
report, completed lifecycle, and an informational engine-wide Planner receipt.
When that Re-enter arrives, confirm the receipt names the expected Sprint,
reports, outcome, and completed state. Do not run `complete`; closure is already
durable and the notification has no actionable liveness expectation.

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
delivery starts a fresh chat only after the prior live turn ends and the quiet
grace passes, coalescing all receiver messages into the bundle. These are
delivery guarantees, not a parent/child chat topology. The Planner receives no
PR-event wakes; Developer-owned subscriptions carry red, green, and externally
closed facts directly to the owning Developer.

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

## Entry and durable state

Pre-declaration QAQC begins from an explicit Planner or FnB request. Read the
exact current spec document and sign that body directly; there is no Sprint id
or Sprint inbox to inspect yet:

```text
sc sprint record-qaqc --document <spec-document-id> \
  --verdict pass [--findings-document <document-id>]
```

Once a Sprint is armed, every review or conformance entry arrives through its
durable wake/inbox. On every wake or re-entry, load `sprint_rev`, inspect the
message, and accept the actionable request before beginning:

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

Review requests use Force-new delivery. A live turn is allowed to finish;
then, after the receiver has stayed quiet for the configured grace interval,
delivery atomically closes the exact registry chat and starts a fresh chat with
the complete undelivered message bundle. Concurrent Force-new requests coalesce
into that one rotation, and a retry resumes the chat created for its own wake
instead of rotating again. Stop cleanly after every typed verdict or decision
handoff so the next request can cross the quiet boundary. The inactivity ceiling
and registry reaper remain the fallback for a silent hung turn.

Plain New remains separate and is eligible immediately. It enters a verified
live turn at its natural boundary and rotates only when the registry chat is idle. Re-enter
resumes the registry chat; no registry row behaves as New. Reviewer verdicts to
Developers and decisions to Planners are Re-enter. Reviewers never receive
PR-event subscription wakes.

## Questions, answers, blockers, and failures

Put a concrete question, answer, blocker, or useful context in a short body file
and send it to the participant who can act. Ask the Developer for missing PR
evidence and the Planner for durable state or action-feasibility facts. Do not
delegate Reviewer judgment to the Planner:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --key <stable-key>
```

Answer incoming questions through `send` so the answer is durable and wakes the
asker, confirm that write, then mark the handled question read with `accept`. A
blocker or integrity concern is evidence for your decision. If action is needed,
send the Planner the decision, impact, exact action, and recommendation through
the protocol below. Continue independent safe review, but stop when missing
facts prevent an honest decision at the decision boundary. Do not send duplicate
reminders; unread recovery owns re-waking.

Choose one stable key for the intended recipient and exact body. Reuse it only
when retrying that same write; use a new key when the recipient or body changes.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the verdict or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command, evidence, impact, and recommendation to FnB; do not invent an
alternate delivery protocol. A Reviewer decides whether the condition warrants
continuing, re-planning, pausing, cancellation, or conclusion, but does not
invoke the lifecycle transition. Send the durable decision to the Planner, who
executes it. A live FnB board-level override may supersede that decision and
must be recorded as FnB authority, not Reviewer judgment.

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
the exact bound revisions, current work/PR facts, liveness evidence, and any
ratified judgment; ambiguous silence is not enough to corrupt a disposition.

Send pause, resume, replan, re-enter, cancel, and abort decisions through the
durable `send` surface above. A clean conclude instead runs the atomic
`record-conformance` close below. Every Reviewer → Planner route is Re-enter.
The Reviewer-authored body must name:

- `decision`: `pause`, `resume`, `replan`, `re-enter`, `cancel`, `conclude`, or
  `abort`;
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

Accept the actionable review request. Its readiness body must be only the bare
locator: submitting or resubmitting intent, PR URL, registered Sprint PR id,
exact head SHA, and work-unit id. Treat scope narrative, verification evidence,
judgment rationale, or review-focus steering in that body as a protocol defect;
do not use it to frame the review. Neither party writes PR comments or
annotations, and the PR body contains only the work-unit id and spec reference.

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
records judgment evidence, sends a Re-enter wake to the Developer, and
resolves the review liveness expectation. Do not message around this surface;
an unrecorded verdict cannot unlock merge.

## Delivery-terminal closeout

The `sprint.delivery_terminal` notification is the entry signal for
whole-Sprint conformance. On that wake, inspect the inbox and current work-unit
state first. If any non-terminal unit is visible, the wake is stale: mark the
informational notification handled with `accept`, exit, and await the next
episode''s delivery-terminal wake.

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
  description; and the suggested unit grouping, waves, dependencies, and
  Developer/Reviewer routing. The durable decision is the failed-pass record.
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
