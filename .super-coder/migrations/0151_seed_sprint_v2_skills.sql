-- 0151 — seed the five Sprints v2 role skills and owning flavor grants.
-- Generated from assets/skills/*/SKILL.md; full-body UPSERTs make existing
-- installations converge while 0001 remains the fresh-build catalogue.

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

## Delivery-complete gate

Before conformance, re-read work units, dependencies, registered PRs, checks,
merge observations, task membership, pending actionable messages, and active
native runs. Every planned unit must be completed/cancelled with an explicit
disposition, and code units must have their real PR outcome. Do not infer task
completion from PR state alone.

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

Commit the final or abort report before the lifecycle transition. Complete only
after conformance evidence and synthesis are durable; abort only under Planner
or FnB authority. Terminal state stops Sprint services and removes live pills
while retaining conversations, messages, events, PR evidence, reports, and
follow-ups.

```text
sc sprint complete --sprint <id> --reason <summary> --outcome <outcome>
sc sprint abort --sprint <id> --reason <reason> [--outcome <outcome>]
```

Hand the FnB the final report id, follow-up list, integrated SHA, and evidence
links. Stop after the terminal transition; Sprint-scoped authority is over.',
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

## Orient and bound the lane

Read the assignment, expected output, bound spec revision, dependencies,
assigned Reviewer, repository/worktree, merge grant, and prior judgments. One
Developer shell may own one active work unit in the Sprint. Do not start a
second editing lane or edit another shell''s worktree.

If the requirement is ambiguous, choose the shippable reading within your
unit''s scope, record the choice and rationale, and continue. Escalate changes to
the unit boundary, interfaces another unit consumes, deliverable cuts, or scope
growth to the Planner.

## Build and verify

Sync the assigned repository, work on a feature branch, match the surrounding
code, and implement the smallest complete change. Keep external calls outside
database transactions. Preserve durable identities and append-only evidence.

Verification must exercise the unit''s independent stage gate and realistic
failure paths. A local exploratory number is not merge evidence. Record real CI
failures, anomalous infrastructure failures, retries, review friction, and
known departures for the final report.

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

## Pause and stop

Pause immediately when integrity is threatened: broken base, destructive
ambiguity, unavailable GitHub, untrustworthy runners, provider exhaustion, or
an unrecoverable environment. State the short reason first; detailed judgment
can follow after pause is durable.

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

Stop when the unit is merged and reported, declined, paused awaiting recovery,
or returned to review. Ask the Planner for later work only after the current
editing lane is terminal.',
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

## Start from durable state

Read the Sprint inbox, lifecycle, bound spec revisions, work-unit graph,
participant routes, active conversations, registered PRs, unresolved
expectations, and recent anomalies. Viewing a participant conversation is
observation, not activity; never manufacture progress from browser presence.

Release every dependency-ready lane through the production surface:

```text
sc sprint dispatch --sprint <id>
```

The returned ids are wake identities. Work-unit disposition and messages are
the authoritative release facts. Dispatch is safe to repeat: occupied Developer
lanes and stable assignment generations prevent double booking.

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

## Escalation judgment

Read the evidence packet before acting: last strong evidence, supporting
signals, unreadable signals, failure identity, nudge history, route/quota state,
and current work facts. A proven failure may justify re-plan, route recovery, or
pause. Ambiguous silence does not justify corrupting a work-unit disposition.

If a Developer or Reviewer declines, preserve the reason, return the lane or
review request to the eligible pool, and issue a fresh assignment identity.
Never edit the declined message into a different instruction.

## Pause and resume

Any participant may pause immediately for an integrity threat. Effective pause
comes first: transition durably, stop external Sprint services, persist
interrupt intent, preserve every partial artifact, and notify Planner/FnB. Add
judgment to the generated pause report after the boundary is safe.

Only Planner or FnB resumes. Review reconciliation for native runs, unread
messages, pending wakes, work units, registered PRs, capacity, and spec drift.
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
stop dispatching and invoke `sprint_close`. Do not fix close-out conformance
findings inside this Sprint.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'sprint_prep',
  'Prepare and arm a Sprints v2 run — bind reviewed spec revisions, shape work units and dependencies, assign routes and capacity, and refuse arming until the durable plan is eligible.',
  'workflow',
  NULL,
  0,
  '# sprint_prep — declare the riverbed

Use as the owning Planner while a Sprint is `prepared`. Preparation ends at one
atomic arming decision; it does not launch participants piecemeal.

## Outcome

Produce one editable prepared Sprint with:

- one roadmap feature;
- exact governing spec revision hashes and their qualifying QAQC approvals;
- work units made from existing spec tasks, each with one Developer and one
  assigned Reviewer;
- dependency edges and planned waves;
- one primary harness/model/effective effort per participant plus eligible
  Planner fallback capacity;
- a committed Sprint merge grant; and
- enough local/GitHub capacity to execute the plan.

The arming transaction creates every participant conversation, the initial
assignment messages and wake intents, and the armed transition together.

## Eligibility pass

Read the feature, selected spec bodies, task ledgers, QAQC records, shell roster,
model routes, quota state, repository access, and worktree availability. Record
the exact revision hash you inspected; a title or document id is not a revision.

Refuse arming when any of these is true:

- a selected current revision lacks Review-shell QAQC approval;
- any Medium-or-higher QAQC finding is unresolved;
- a selected task belongs to no work unit or more than one work unit;
- a dependency cycle exists;
- a work unit lacks an assigned Developer or Reviewer;
- participant routes or required capacity are unavailable;
- another Sprint is armed, or a selected shell already participates in an armed
  Sprint; or
- the merge grant was not committed as part of the final plan.

Deficiencies remain editable in `prepared`. Do not weaken an invariant merely
to get to `armed`; surface the missing fact or capacity to the FnB.

## Shape work, do not script behavior

A work unit is one coherent editing lane and may group related spec tasks. Use
dependencies only for hard prerequisites. Waves express intent and later report
comparison; they do not forbid safe out-of-order completion. Reviews are not
editing lanes.

Prefer the smallest dependency graph that preserves correctness. Record the
expected output in outcome language. Do not encode a shell''s implementation
steps into the durable plan when its role skill and judgment can decide them.

For every participant, record role, route, model, effective effort, persistent
conversation ownership, and fallback facts the plan actually depends on. Never
pretend a native session can resume across harnesses.

Declare the prepared envelope from a JSON array of participant objects, then
add each editing lane from existing spec tasks:

```text
sc sprint declare --feature <feature-id> \
  --spec-approval <approval-id> --participants-file <path> --merge-grant
sc sprint plan-unit --sprint <id> \
  --developer-shell <id> --reviewer-shell <id> --title <title> \
  --expected-output-file <path> --task <task-id> \
  [--task <task-id>] [--wave <n>] [--depends-on <work-unit-id>]
```

The participant file contains `shell_id`, `role`, and `harness`, with optional
`model`, `effort`, and `route`. FnB may add `--planner-shell <id>` when declaring
for the originating Planner. Keep the Sprint prepared while shaping the plan.

## Final arming check

Immediately before arming, re-read the spec revision hashes, QAQC records,
participant capacity, single-armed invariant, repository access, and merge
grant. The final read and durable plan commit belong to the authoritative
arming transaction; external harness and GitHub work occurs after it commits.

Arming succeeds only when the first assignments and wake intents are durable.
A process crash after commit is outbox recovery; a crash before commit exposes
no partial Sprint.

```text
sc sprint arm --sprint <id>
```

## Handoff

Once armed, hand control to `sprint_pln`. Give the FnB a compact declaration:
Sprint id, feature, exact spec revisions, participants/routes, work-unit graph,
planned waves, merge-grant state, and known accepted risks.

Stop when the Sprint is armed or when one concrete eligibility blocker has been
surfaced. Do not dispatch from a partially prepared plan.',
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

For unit review, stop after the durable verdict is recorded. For conformance,
stop after the report and all findings replay idempotently and give the Planner
their report/follow-up ids.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

WITH sprint_skill_grants(flavor, skill_name) AS (
  VALUES
    ('planner','sprint_prep'),
    ('planner','sprint_pln'),
    ('planner','sprint_close'),
    ('dev','sprint_dev'),
    ('reviewer','sprint_rev')
)
INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT grants.flavor, skills.skill_id
FROM sprint_skill_grants grants
JOIN skills ON skills.name=grants.skill_name
WHERE skills.is_deleted=0;

COMMIT;
