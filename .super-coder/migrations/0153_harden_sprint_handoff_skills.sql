-- 0153 — publish hardened Sprint handoff role guidance.
--
-- Full-body UPSERTs deliberately converge drifted existing rows while the
-- generated 0001 seed remains the fresh-install catalogue.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'sprint_close',
  'Close or abort a Sprints v2 run — boot whole-Sprint conformance, compile the bounded evidence packet, synthesize the final report, preserve follow-ups, and transition terminally without deleting history.',
  'workflow',
  NULL,
  0,
  '# sprint_close — synthesize and finish

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

Boot an independent Reviewer into `sprint_rev` conformance mode with the bound
spec revision hashes, integrated main SHA, and ratified judgment list. Do not
feed it unit authors'' narrative beyond recorded judgments; conformance judges
artifacts.

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

Keep the final report at about 6,000 characters or fewer; 8,000 is the hard
maximum. Run `wc -m < <path>` before the typed terminal handoff, then require
the successful report receipt and lifecycle transition.

```text
sc sprint complete --sprint <id> --reason <summary> --outcome <outcome> \
  --report-file <path> --key <stable-key>
sc sprint abort --sprint <id> --reason <reason> [--outcome <outcome>]
```

Hand the FnB the final report id, follow-up list, integrated SHA, and evidence
links. Re-run `sc sprint inbox --sprint <id>`, act on any newly arrived message,
mark every handled informational message read with `accept`, and stop after the
terminal transition; Sprint-scoped authority is over.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
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
alternate delivery protocol. A Developer does not pause the Sprint. The Planner
decides whether the reported condition warrants continuing, re-planning, or
pausing.

## Build and verify

Sync the assigned repository, work on a feature branch, match the surrounding
code, and implement the smallest complete change. Keep external calls outside
database transactions. Preserve durable identities and append-only evidence.

Verification must exercise the unit''s independent stage gate and realistic
failure paths. A local exploratory number is not merge evidence. Record real CI
failures, anomalous infrastructure failures, retries, review friction, and
known departures for the final report.

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
until it is green. The watcher supplies red/green facts; do not write PR state
yourself. On red, diagnose and fix the PR. On green, judge readiness rather
than forwarding mechanically.

```text
sc sprint register-pr --sprint <id> --repository <owner/name> \
  --pr <number> --work-unit <id>
```

## Review handoff

Put the readiness claim in a file, then use one stable retry key:

Keep the readiness claim at about 6,000 characters or fewer; 8,000 is the hard
maximum. Run `wc -m < <path>` and condense before the typed handoff. The handoff
exists only after the command succeeds and confirms its durable write and wake.

```text
sc sprint request-review \
  --sprint <id> --registered-pr <registered-id> \
  --readiness-file <path> --key <stable-key>
```

The assigned Reviewer receives an actionable request. Stop until its durable
outcome arrives. A changes-requested verdict opens a fresh linked fix
conversation and makes it current. Apply every blocking finding, re-establish
green, and hand back with a new stable review key. Record disagreements as
judgment; the Planner resolves scope/severity disputes.

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
appropriate loop. After merge, clean the worktree, submit the unit result and
judgments, and let automatic merge observation advance dependencies.

## Report and stop

Report broken bases, destructive ambiguity, unavailable GitHub, untrustworthy
runners, provider exhaustion, or an unrecoverable environment to the Planner
with evidence, impact, and a recommendation. Stop at the unsafe boundary while
the Planner decides whether to continue, re-plan, or pause.

Stop when the unit is merged and reported, declined, awaiting Planner/FnB
recovery, or returned to review. Before stopping, re-run `sc sprint inbox
--sprint <id>`, act on newly arrived messages, mark every handled informational
message read with `accept`, and confirm the final typed handoff succeeded. Ask
the Planner for later work only after the current editing lane is terminal.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'sprint_pln',
  'Run an armed Sprints v2 collaboration loop as Planner — dispatch ready lanes, respond to durable evidence and escalations, re-plan honestly, and coordinate pause/resume without becoming a transition bottleneck.',
  'workflow',
  NULL,
  0,
  '# sprint_pln — govern the armed Sprint

Use as the originating Planner after `sprint_prep` arms the Sprint. The system
captures deterministic facts; you decide scope, sequencing, and recovery.
On every wake or re-entry, load `sprint_pln`, run the exact inbox command below,
and inspect the durable message before deciding what to do.

## Start from durable state

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

- Keep dependencies as the only hard sequence. Re-plan assignments, waves, or
  dependencies when reality changes, but never rewrite completed history.
- Let Developers own their PRs through green, review, correction, and merge.
  Let Reviewers own verdicts. Do not proxy routine handoffs.
- Consume passive system facts without waking yourself into every transition.
  Act on decisions, escalations, re-plans, pauses, and terminal synthesis.
- Record scope calls, spec edits, ratified deviations, and their rationale as
  judgment evidence while context is live.
- A mid-Sprint spec edit is allowed only by the owning Planner or FnB. Record
  the prior and new exact revision hashes. The running Sprint remains bound to
  its approved revision unless an explicit recorded judgment says otherwise.

The armed runtime evaluates liveness on its five-second pulse. A one-shot
diagnostic/evaluation is available when evidence requires it:

```text
sc sprint monitor --sprint <id>
```

Do not poll this command on a schedule. It evaluates only due accepted
expectations and its nudge/escalation identities are durable.

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
delivery protocol. When a Developer or Reviewer reports an integrity concern,
evaluate its evidence, impact, and recommendation. Decide whether to continue,
re-plan, or pause. Send any needed participant context before pausing; an active
relay is not available after the lifecycle becomes paused.

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

Revise only a still-planned lane; name its complete new projection so the
before/after event is reviewable. Cancel only an unreleased lane, with the
reason retained as its terminal result:

```text
sc sprint replan-unit --sprint <id> --work-unit <id> \
  --developer-shell <id> --reviewer-shell <id> --wave <n> \
  [--depends-on <work-unit-id>] [--output-kind code|report-only|no-code]
sc sprint cancel-unit --sprint <id> --work-unit <id> --reason <reason>
```

## Escalation judgment

Read the evidence packet before acting: last strong evidence, supporting
signals, unreadable signals, failure identity, nudge history, route/quota state,
and current work facts. A proven failure may justify re-plan, route recovery, or
pause. Ambiguous silence does not justify corrupting a work-unit disposition.

If a Developer or Reviewer declines, preserve the reason, return the lane or
review request to the eligible pool, and issue a fresh assignment identity.
Never edit the declined message into a different instruction.

## Pause and resume

Developer and Reviewer participants report integrity concerns; the Planner or
FnB decides whether to pause. When pause is warranted, transition durably, stop
external Sprint services, persist interrupt intent, preserve every partial
artifact, and retain the judgment and evidence for FnB recovery.

Only Planner or FnB resumes. Review reconciliation for native runs, unread
messages, pending wakes, work units, registered PRs, capacity, and spec drift.
An exhausted recovery wake is one bounded fallback, not a retry loop. Preserve
the unread message and failed wake as evidence, involve FnB for manual recovery,
and do not create recursive fallbacks.
Drift informs; it never silently blocks resume. Record the exact revision facts
and choose continue, re-plan, or abort.

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
sc sprint resume --sprint <id> [--reason <reconciliation-judgment>]
```

Abort only when continuing would be dishonest or unsafe. It is terminal and
deletes nothing.

## Handoffs and stop

Assign ready work in the Developer''s persistent Sprint conversation. Review
outcomes move the Developer to fresh fix/merge conversations automatically;
the next work assignment returns it to the persistent lane.

When all planned delivery work is terminal and merged or explicitly no-code,
re-run `sc sprint inbox --sprint <id>`, act on any newly arrived message, confirm
every handled informational message is marked read with `accept`, confirm the
final typed transition succeeded, stop dispatching, and invoke `sprint_close`.
Do not fix close-out conformance findings inside this Sprint.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'sprint_rev',
  'Review Sprints v2 work and whole-Sprint conformance — apply the Medium-and-above gate, record precise verdicts through the authenticated surface, and route conformance findings only to post-Sprint follow-ups.',
  'workflow',
  NULL,
  0,
  '# sprint_rev — independent review and conformance

Use in one of two modes: a work-unit PR review during the loop, or the final
whole-Sprint conformance pass. The evidence differs; independence does not.
On every wake or re-entry, load `sprint_rev`, run the exact inbox command below,
and inspect the durable message before deciding what to do.

Read and accept the actionable request before beginning. During preparation,
sign the exact current spec revision through the same authenticated surface:

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint record-qaqc --document <spec-document-id> \
  --verdict pass [--findings-document <document-id>]
```

Decline an actionable request you cannot take, with a concrete reason:

```text
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Use `accept` or `decline` for actionable work. After acting on an informational
question, answer, blocker, or context message, run `accept` for that message.
For informational messages it only marks the message read; it does not change
Sprint or work-unit state.

## Questions, answers, blockers, and failures

Put a concrete question, answer, blocker, or useful context in a short body file
and send it to the participant who can act. Ask the Developer for missing PR
evidence and the Planner for scope or severity decisions:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --key <stable-key>
```

Answer incoming questions through `send` so the answer is durable and wakes the
asker, confirm that write, then mark the handled question read with `accept`. A
blocker or integrity concern goes to the Planner with concise evidence, impact,
the exact action needed, and your recommendation. Continue independent safe
review, but stop at a decision boundary when the answer is required. Do not send
duplicate reminders; unread recovery owns re-waking.

Choose one stable key for the intended recipient and exact body. Reuse it only
when retrying that same write; use a new key when the recipient or body changes.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the verdict or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command, evidence, impact, and recommendation to FnB; do not invent an
alternate delivery protocol. A Reviewer does not pause the Sprint. The Planner
decides whether the reported condition warrants continuing, re-planning, or
pausing.

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
report note. During close-out conformance, every severity becomes a follow-up
and none is fixed inside the Sprint.

## Work-unit review

Accept the actionable review request, then inspect the exact bound spec
revision, readiness claim, PR head, diff, checks, tests, relevant runtime
evidence, and prior judgment calls. Review code quality, edge cases/failure
paths, and spec conformance. Trace the real path; do not trust names or PR prose.

Findings must state:

- severity and concise title;
- violated behavior or invariant;
- exact code/evidence location;
- a reproducible consequence; and
- the fix boundary, without prescribing unnecessary architecture.

Put the verdict body in a file and record it through the authenticated surface:

Keep the verdict at about 6,000 characters or fewer; 8,000 is the hard maximum.
Run `wc -m < <path>` before submission. The typed review handoff exists only
after the command succeeds and confirms its durable write and Developer wake.

```text
sc sprint record-review \
  --sprint <id> --registered-pr <registered-id> \
  --verdict changes_requested --body-file <path> --key <stable-key>
```

Use `approved` only when no Critical/Major/Medium finding remains. The engine
checks that the request was accepted and still binds to the reviewed head,
records judgment evidence, opens the correct fresh Developer conversation, and
resolves the review liveness expectation. Do not message around this surface;
an unrecorded verdict cannot unlock merge.

## Whole-Sprint conformance

Judge integrated `main` against every governing bound revision, plus the exact
recorded mid-Sprint revision facts and ratified judgments. Review the integrated
system, not unit diffs. Classify each requirement as:

- `as-specced`;
- `deviated-intentionally` with its ratified judgment;
- `deviated-silently`; or
- `unimplemented`.

The last two are findings. Include spec document id and work-unit id when known.
Write the narrative report and a JSON findings array:

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

Then record both atomically:

Keep the conformance report and each finding body at about 6,000 characters or
fewer; 8,000 is the hard maximum for each. Run `wc -m < <report>` and length-check
each finding body before submission. Require the successful report and
follow-up receipt before stopping.

```text
sc sprint record-conformance \
  --sprint <id> --body-file <report> --findings-file <json> \
  --key <stable-pass-key>
```

This creates append-only conformance evidence and pending follow-ups for FnB
disposition. It must not create a fix lane, reopen a completed work unit, or
send findings to a Developer for in-Sprint repair — including Critical ones.
Surface immediate safety risk to the FnB, but preserve the close-out rule.

## Stop

For either mode, re-run `sc sprint inbox --sprint <id>` and act on newly arrived
messages before stopping. For unit review, stop after the durable verdict is
recorded and every handled informational message is marked read with `accept`.
For conformance, also require the report and all findings to replay
idempotently and give the Planner their report/follow-up ids.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
