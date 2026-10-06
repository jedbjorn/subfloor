-- 0280 — remove the Sprint conformance follow-up queue and its data.
-- Findings belong in report prose; no archive or conversion is retained.

BEGIN;

DROP TABLE IF EXISTS sprint_followups;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_rev',
  'Review Sprints v2 work and whole-Sprint conformance — own review, re-enter, abort, and conclude judgments, author the conformance and Sprint reports, and direct safety actions through durable messages.',
  'workflow',
  NULL,
  0,
  '# sprint_rev — independent review and conformance

Load `sprint_protocol` first; it holds the lifecycle, wake types, inbox
commands, relay contract, body limits, artifact paths, receipt recovery, and
authority boundary. This skill holds only the Reviewer''s steps: pre-declaration
QA/QC, one work-unit review, or whole-Sprint conformance.

## Route the entry

| Entry | Route |
|---|---|
| Explicit pre-declaration QA/QC request | Read and sign the exact current spec directly; there is no Sprint id or Sprint inbox yet. |
| Work-unit review / `sprint.delivery_terminal` | Inspect the Sprint inbox once; accept the actionable request. |
| Live FnB instruction | Preserve board-level authority; read only durable state needed for independent judgment. |

QA/QC precedes all Sprint inbox commands and is one write:

```text
sc mem doc qaqc <spec-document-id> --verdict pass|fail [--findings-doc <document-id>]
```

Reviewers never receive PR-event wakes.

## Conformance decisions and Planner controls

You own review, re-enter, abort, and conclude judgments. The Planner
independently owns operational plan structure: safe-edit pauses, recalling
unreleased work, lane changes/repeats, assignment/routing, unreleased-scope
cancellation, and validated resume. Never run standalone pause, replan,
recall, reroute, cancel, resume, complete, or abort actions; clean
`record-conformance` alone performs its narrow atomic close.

Base judgment on durable Sprint state, bound revisions, current work/PR facts,
progress-carrier evidence, and ratified judgments. A decision body names:

- `decision`: `re-enter`, `abort`, or the exact safety-critical recommendation;
- Reviewer-owned evidence + rationale;
- exact Sprint/unit ids, reason, outcome, and complete action arguments;
- immediate safety impact for FnB.

The Planner verifies your identity and executes the transition without
surrendering plan authority. A rejected action requires a revised judgment
supported by returned durable state, never an improvised bypass. A live FnB
instruction is the FnB''s board-level override.

## Severity rubric

- **Critical** — active security/authority violation, destructive corruption,
  or unsafe continued operation.
- **Major** — wrong behavior, data loss, broken invariant, material spec
  violation, or silently wedged delivery/recovery.
- **Medium** — concrete normal-use correctness/recovery gap, missing negative
  enforcement, or unreliable handoff.
- **Low** — bounded cleanup, clarity, test-depth, or resilience improvement;
  delivered behavior remains correct.

Critical/Major/Medium block unit approval; Low is a report note. At closeout,
severity does not decide timing: you judge whether each finding requires
in-Sprint patching or acceptable post-Sprint follow-up.

## Work-unit review

Accept the request and retain that exact message id. Its body is a bare
locator: intent, PR URL, registered PR id, exact head, work-unit id. Scope
narrative, verification, rationale, or focus steering is a protocol defect. PR
comments and annotations are forbidden; the PR body contains only unit id +
spec reference plus the Developer''s rationale.

Bind inspection/verdict to the accepted request''s message id, registered PR,
and work unit. Review the live PR head; a rebase since the locator''s head is
not a defect. Read the exact spec revision + full diff, then checks, tests,
relevant runtime facts, and ratified judgments. Each round is clean: no prior
Developer evidence or prose; prior findings clear only when the new head
proves it. Trace code paths, failure cases, and spec behavior rather than
names or PR prose.

### Red-check doctrine

Accepted-red is not a legal review outcome. A departure that leaves checks
failing is never acceptable; the handoff remains green-only, without exception
or waiver: do not note the failure and approve anyway.

- In-scope failure -> record `changes_requested` so the Developer fixes them and restores green.
- Out-of-scope failure -> keep the lane unapproved and send the Planner a `replan`
  decision naming the failures; Planner widens the lane or cuts follow-up work.

Read cited and feature-scoped resolved flag evidence through memory
(`sc mem get flags <flag-id>`, `sc mem get flags --feature <id> --resolved`).

Each finding pins severity/title, violated invariant, exact location/evidence,
reproducible consequence, and fix boundary without unnecessary architecture.

Complete a unit verdict in this exact order:

1. Finish every inspection, finding, and verdict body.
2. Re-run `sc sprint inbox --sprint <id>` once; handle + `accept` new items.
3. Run `wc -m < <path>`; require near 6,000 and below 8,000 characters.
4. As the literal final action, run:

```text
sc sprint record-review \
  --sprint <id> --registered-pr <registered-id> \
  --verdict changes_requested|approved --body-file <path> --key <stable-key>
```

5. Require durable judgment evidence + the Developer wake. Run no trailing
   command; stop.

Use `approved` only with no Critical/Major/Medium finding. Engine validation
requires the accepted request. Do not message around the surface; an
unrecorded verdict cannot unlock merge.

## Delivery-terminal closeout

Retain the exact notification message id + delivered wake as this closeout
episode''s identity. Proceed only when the notification names this shell as the
selected conformance owner for its current ownership generation. A different
Reviewer accepts the informational notification if received and records no
conformance. Inspect inbox, lifecycle, and units first:

- Already completed/aborted -> `accept` notification and stop.
- Any non-terminal unit visible -> the wake is stale: `accept`, stop, and
  await a fresh delivery-terminal episode.
- Only an armed Sprint whose units are all terminal enters conformance.

Compile the bounded evidence packet first, yourself:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
```

Increase only when truncation omitted needed evidence; maximum 200. Judge
integrated `main` against every bound/current revision + ratified judgment. All
units cancelled and nothing shipped -> `abort`, not `conclude`.

Choose one branch:

- **In-Sprint patching required.** Do not run `record-conformance`. Send the
  Planner a durable `re-enter` decision with every blocking finding; each spec
  task''s title and description; grouping, waves, dependencies, routing, and
  capacity rationale. State independent lanes, expected review overlap, useful
  reserve, and critical-path effect. After three re-entry episodes, escalate
  non-convergence to FnB.
- **Clean or post-Sprint-only findings.** Prepare conformance report,
  final report, reason, and outcome; submit the atomic close below. Send no
  conclude message.

## Whole-Sprint conformance

Review the integrated system, not unit diffs. Classify every requirement
`as-specced`, `deviated-intentionally` with ratified judgment,
`deviated-silently`, or `unimplemented`; the last two are findings. Include
spec document + work-unit ids when known.

For the clean branch, write findings directly in the conformance report.
Keep the report near 6,000 and below 8,000 characters; run `wc -m < <report>`.

Before recording conformance, author the final Sprint report. Name yourself as
author and cover governing scope/revisions, shipped units/PRs, judgments +
ratified deviations, failures/retries/recovery/anomalies, conclusion,
follow-ups, and evidence location. Keep it near 6,000 and below 8,000; preserve
discrepancies.

Record one atomic final write:

```text
sc sprint record-conformance \
  --sprint <id> --body-file <report> \
  --final-report-file <final-report> --reason <reason> --outcome <outcome> \
  --key <stable-pass-key>
```

Require the receipt: conformance report id, final report id,
completed state, Planner message id, and Planner wake id. Closing ends the
Developer chats and schedules deletion of the Sprint artifact directory; your
chat persists and no worktree is reset. Do not poll cleanup, wait before
stopping, or manually close peer chats. Never reopen editing after recording;
a re-enter defers reports until new scope is terminal and a fresh
delivery-terminal wake arrives.

## Stop

Unit review ends with the ordered `record-review` write as the literal final
action.

For closeout, first re-run `sc sprint inbox --sprint <id>`, handle + `accept`
new messages, then confirm every artifact/body is final and below 8,000.

- Clean conclude -> run the atomic `record-conformance` command above as the
  literal final action. When it confirms completed state and all receipt
  identities, stop immediately; the Planner is notified.
- Re-enter/abort -> as literal final action send the Sprint-level `decision`
  to the Planner (relay form in `sprint_protocol`), require durable write +
  Planner wake, then stop immediately. Run no trailing command until another
  native wake.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description,
  category=excluded.category,
  command=excluded.command,
  common=excluded.common,
  content=excluded.content,
  is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_pln',
  'Run an armed Sprints v2 collaboration loop as Planner — dispatch and restructure lanes, change participant routes, and execute Reviewer decisions through durable pause, resume, and close protocols.',
  'workflow',
  NULL,
  0,
  '# sprint_pln — govern the armed Sprint

Load `sprint_protocol` first; it holds the lifecycle, wake types, inbox
commands, relay contract, body limits, artifact paths, receipt recovery, and
authority boundary. This skill holds only the Planner''s steps after
`sprint_prep` arms the Sprint.

## What you can read

| Need | Read |
|---|---|
| Whole Sprint: lifecycle, participants with current routes, units, dependencies, PRs, health | `sc sprint show --sprint <id>` |
| Messages addressed to you | `sc sprint inbox --sprint <id>` |
| Exact bound spec body | `sc sprint spec-revision --sprint <id> --document <id> [--body-only]` |
| PR-watcher evidence behind a stalled gate | `sc sprint watcher-state --sprint <id>` |
| Post-Sprint cleanup evidence | `sc sprint cleanup-status --sprint <id>` |
| Bounded history packet: judgments, pauses, anomalies, reports | `sc sprint compile-report --sprint <id> --limit 50` |
| Candidate routes and what each supports | `sc models list [<harness>]`; preview one with `sc models resolve <harness> [<model>] [--effort <level>]` |
| Shell roster, feature, spec tasks, settled decisions | `sc mem get shells`, `sc mem get roadmap`, `sc mem get tasks --feature <id>`, `sc mem get decisions` |

`show` participants carry `shell_id`, `role`, `harness`, `model`, `effort`,
`binding_status`, and `route_revision`; units carry `developer`, `reviewer`,
`disposition`, `prerequisite_ids`, and `pull_requests`. A Thinking level
applies only to controlled routes: `null` model + `null` effort is Harness
default, and Vibe takes no effort.

## Route the entry

| Trigger | Route |
|---|---|
| Sprint decision, merged-work handoff, question, blocker, relay | Inspect `sc sprint inbox --sprint <id>` once; handle that message. |
| Engine-wide completion or cleanup receipt | Inspect receipt + terminal state directly; it is informational — do not run the Sprint inbox, accept it, or close again. |
| Live FnB instruction | Act under board override; name FnB authority in durable evidence. |

You receive no PR-event wakes; red/green/closed/merged facts go to Developers.

## Durable running loop

Read only trigger-required lifecycle, unit, dependency, route, PR, expectation,
and anomaly facts. Browser presence is not progress.

```text
sc sprint dispatch --sprint <id>
```

Dispatch every dependency-ready lane; returned ids are wake identities.
Disposition + messages are release facts. Stable assignment generations and
occupied lanes make dispatch repeat-safe.

- Keep dependencies as hard sequence; restructure current projection under
  Planner authority, record why, and never rewrite completed history.
- Developers own local/PR proof, review/fix/merge. Complete code + unavailable
  local gate -> registered CI: pending wait, red fix, green review; browser skip
  is non-failing. With fallback, Planner NEVER mutates packages/toolchains or
  runs repair. No checks/untrustworthy watcher after one read -> blocker.
  Reviewers own verdicts/conformance; do not proxy handoffs/judgments.
- Record Reviewer decision id + exact action + receipt; never rewrite rationale
  as Planner judgment.
- Reviewer-approved Planner/FnB spec rebind:
  pause -> `sc mem doc edit` -> `sc sprint rebind-spec --sprint <id>
  --document <id> --expected-revision <old-sha256> --reason <decision>` ->
  replan -> resume. Pass = old/new hashes + changed boolean; conflict -> reread.
- Relay Developer integrity evidence, impact, and recommendation to the
  Reviewer. Send required context before pausing: paused Sprint relay is
  unavailable.

## Reviewer decisions and Planner actions

For a required-reply Reviewer decision, keep this order:

1. Re-run `sc sprint inbox --sprint <id>`; verify the assigned Reviewer + retain the id.
2. Send a linked acknowledgement (`--intent information --reply-to <decision-message-id>`).
3. Require the reply command to confirm its durable message and wake; retry the
   same command/key if ambiguous.
4. `sc sprint accept --sprint <id> --message <decision-message-id>`.
5. Only after acceptance, execute the requested transition without
   re-adjudicating it. The linked reply must precede any pause or abort that
   makes the relay unavailable.

Record decision id + reply, acceptance, and action receipts. Clean conformance
closes atomically and sends an informational receipt; no reply/accept is
needed. If an action fails a lifecycle/authority/disposition precondition,
send the refusal + durable state to the Reviewer (or FnB for an override),
substitute nothing, and stop.

### Pause or resume

Pause for a Reviewer decision or safe Planner restructuring; preserve partial
artifacts, interrupt intent, judgment, and evidence:

```text
sc sprint pause --sprint <id> --reason <decision-or-restructure-reason>
```

Pause interrupts every live participant turn, and resume re-enters each
interrupted participant automatically with one re-enter wake into their
existing chat. Health is reporting-only: never send status-check messages on
silence — read the board (`sc sprint show --sprint <id>`) instead.

Resume only after recording recovery/restructure and reconciling native runs,
unread messages, wakes, units, PRs, capacity, and spec drift:

```text
sc sprint resume --sprint <id> [--reason <validated-reconciliation-reason>]
```

Preserve the current conformance owner on ordinary resume. Replace that owner
only while paused, only with an eligible participating Reviewer, and always
record a reason:

```text
sc sprint resume --sprint <id> \
  --conformance-reviewer-shell <replacement-shell-id> \
  --reason <ownership-replacement-reason>
```

Require the receipt and board projection to show the replacement owner and a
new ownership generation before treating the Sprint as resumed. An exhausted
recovery wake = bounded manual evidence: preserve the unread message + failed
wake, involve FnB, create no recursive fallback. Drift informs but never
silently blocks resume.

Aborted-Sprint PR ownership repair belongs to the originating Planner. Keep
the replacement Sprint paused; establish old/new identity, then:

```text
sc sprint reconcile-pr --sprint <replacement-id> --repository <owner/repo> \
  --pr <number> --work-unit <replacement-unit-id> --reason <recovery-reason>
```

It refuses a live source or target Sprint, a non-originating Planner, an
invalid/owned target, and a closed-unmerged PR. Require a separate Reviewer
decision before resuming.

### Modify, recall, repeat, reassign, or reroute

Cancel unreleased scope with retained terminal reason/Reviewer id:

```text
sc sprint cancel-unit --sprint <id> --work-unit <id> --reason <reason>
```

Edit an unreleased lane; omitted fields stay, `--clear-dependencies` means none:

```text
sc sprint replan-unit --sprint <id> --work-unit <id> \
  [--developer-shell <id>] [--reviewer-shell <id>] [--title <title>] \
  [--expected-output-file <path>] [--task <task-id>] [--wave <n>] \
  [--depends-on <work-unit-id> | --clear-dependencies] \
  [--output-kind code|report-only|no-code]
```

Never edit a released lane in place. Pause -> recall the unmerged lane
(`sc sprint recall-unit --sprint <id> --work-unit <id> --reason <reason>`) ->
replan -> resume. Recall preserves message/event history, returns only
unmerged work to planned, and refuses terminal/PR-bound work. PR-bound work
stays; plan replacement or use reconciliation. Resume creates a fresh
assignment generation. One spec task may govern repeated verification or
replacement lanes; each lane lists it once — do not duplicate the spec task.

Close a released lane terminal when its work finished out-of-band (a PR that
merged while paused) or its lane is abandoned — the PR-bound case recall
refuses:

```text
sc sprint resolve-unit --sprint <id> --work-unit <id> \
  --to completed|cancelled --reason <reason>
```

Paused-only; retires the lane''s open expectations, supersedes its PR links
(registration kept for reconcile-pr), and wakes both seats.

To change a future assignment or review route, pause the armed Sprint, take
each participant''s `shell_id` and current route from `sc sprint show`, preview
the replacement with `sc models resolve`, then replace the route and resume:

```text
sc sprint reroute-participant --sprint <id> --participant-shell <id> \
  --harness <harness> [--model <model>] [--effort <effort>] \
  [--route <display-route>]
```

Prepared Sprints may reroute directly. Reroute declared participants only. On
a decline, preserve the reason and choose a replacement from current capacity;
ask the Reviewer only if review/conformance judgment changes.

### Re-enter after conformance

The Reviewer decision names findings; governing tasks (existing for the same
scope, new title/description for new scope); and grouping, waves,
dependencies, routing, capacity. Preserve it; do not absorb extra or
post-Sprint scope or maximize occupancy.

```text
sc mem task add "<task-title>" --feature <feature-id> \
  --doc <governing-spec-document-id> --seq <next-seq> \
  --desc "<task-description>"

sc sprint plan-unit --sprint <id> \
  --developer-shell <id> --reviewer-shell <id> --title <title> \
  --expected-output-file <path> --task <task-id> \
  [--task <task-id>] [--wave <n>] [--depends-on <work-unit-id>] \
  [--output-kind code|report-only|no-code]
```

Reuse a task for exact repair/repeat; add only genuinely new scope. Bind every
task, then confirm routes, dependency graph, and capacity plan match the
decision. Release ready lanes with `sc sprint dispatch --sprint <id>`. The
engine sends the next delivery-terminal wake; you do not initiate conformance.

### Conclude or abort

Clean `record-conformance` by the Reviewer atomically stores conformance,
the Reviewer-authored final report, completion, and your
informational receipt. On it, verify Sprint/reports/outcome/completed state.
Do not run `complete`; do not author a second report; do not manually close
peer chats.

The completion receipt is the only success wake: it names the Developer chats
the engine closed and the artifact directory it deletes. Your chat and every
Reviewer''s persist; no worktree is reset. Do not poll cleanup or reset
participant trees. A second wake arrives only if artifact deletion failed —
inspect once and retry only after correcting the named condition:

```text
sc sprint cleanup-status --sprint <id>
sc sprint cleanup --sprint <id> --key <stable-retry-key>
```

Require `created`, cleanup request id, action, exact target ids, and aggregate
projection. Reuse the key only for the same request. Abort only on a Reviewer
decision or FnB override; it is terminal and deletes nothing:

```text
sc sprint abort --sprint <id> --reason <reason> [--outcome <outcome>]
```

## Handoffs and stop

Never dispatch the next wave from merge observation. The merged-work handoff
wake is the only normal next-wave dispatch trigger. On it:

1. Run `sc sprint inbox --sprint <id>`; inspect the merged handoff + unit/dependency state.
2. Handle earlier informational items and `accept` each, including the handoff.
3. Finish reconciliation and Planner bookkeeping; no work remains.
4. As the literal final action run `sc sprint dispatch --sprint <id>`.
5. Require durable assignments + wakes, then stop. Run no trailing command.
   Empty dispatch remains final; investigate only on a later durable wake.

On a clean completion receipt, verify the named Sprint is terminal and record
the closed Developer chats; run no close command. Then stop — a cleanup wake
arrives only if artifact deletion failed.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description,
  category=excluded.category,
  command=excluded.command,
  common=excluded.common,
  content=excluded.content,
  is_deleted=0;

COMMIT;
