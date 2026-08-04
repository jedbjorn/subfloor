-- 0178 — reseed Sprint watcher-state diagnosis doctrine.
-- Full-body UPSERTs converge existing Developer and Planner skill rows.

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
On every wake or re-entry, load `sprint_dev`, run the exact inbox command below,
and inspect the durable message before deciding what to do.

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

The active-chat registry, not Sprint participant pointers, owns your current
chat. Assignments use Force-new delivery. A live turn is allowed to finish;
then, after the receiver has stayed quiet for the configured grace interval,
delivery atomically closes the exact registry chat and starts a fresh chat with
the complete undelivered message bundle. Concurrent Force-new wakes coalesce
into that one rotation, and a retry resumes the chat created for its own wake
instead of rotating again. Stop cleanly after every typed handoff so the next
assignment can cross the quiet boundary. The inactivity ceiling remains the
fallback for a silent hung turn: it unlinks the chat so the reaper can terminate
its verified process identity and Force-new delivery can proceed.

Plain New remains a separate route: it is eligible immediately, enters a
verified live turn at its natural boundary, and rotates only when the registry
chat is idle. Re-enter resumes the registry chat; no registry row behaves as
New.

## Questions, answers, blockers, and failures

Write a concrete question, answer, blocker, or useful context to a short body
file, then send it durably to the participant who can act:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --key <stable-key>
```

Ask the Planner about scope, priority, or cross-unit decisions; ask the assigned
Reviewer about review evidence. Answer an incoming question through `send` so it
wakes the asker, confirm that write, then mark the handled question read with
`accept`. For a blocker or integrity concern, send the Planner concise evidence,
impact, the exact action needed, and your recommendation. Continue safe
independent work, but stop at a decision boundary when the answer is required.
No immediate response is not a reason to send duplicates: the durable message
and recovery reconciler own re-waking.

Choose one stable key for the intended recipient and exact body. Reuse it only
when retrying that same write; use a new key when the recipient or body changes.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command, evidence, impact, and recommendation to FnB; do not invent an
alternate delivery protocol. A Developer does not pause the Sprint. The
Reviewer decides whether the evidence warrants continuing, re-planning, or
pausing; the Planner executes that decision.

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
not repeat the read as a polling loop. `sc sprint monitor` evaluates accepted
liveness expectations and carries no evidence about the PR watcher.

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
  --key <stable-merged-handoff-key>
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

Pause, cancel, re-enter, and conclude are Reviewer decisions and Planner
actions. A valid decision arrives as a durable Reviewer → Planner Re-enter
message and names the decision, evidence, target ids, reason, and exact
requested transition. Accept the actionable message, verify it came from the
assigned Reviewer, and execute that transition without re-adjudicating the
decision. Record the decision message id and action receipt together.

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

Do not run `compile-report` by default, synthesize the final report, or
editorialize the Reviewer body. The Reviewer compiles its own evidence. A
Planner compile remains a valid FnB-directed fallback:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
```

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

On receipt, re-run `sc sprint inbox --sprint <id>`, accept the conclude message,
execute its exact close action through the protocol above, and confirm the typed
transition succeeded. After `complete` succeeds, emit its bounded receipt and
run no further Sprint command. The Planner does not author a second report.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
