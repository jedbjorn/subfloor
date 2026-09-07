-- 0262 — reseed the sprint_pln and sprint_protocol guidance for resume
-- re-entry. Pause interrupts every live participant turn and resume re-enters
-- each interrupted participant with one re-enter wake; health stays
-- reporting-only and nobody is pinged for silence. No schema change: a
-- full-body UPSERT converges upgraded installations on the same text a fresh
-- seed produces. Idempotent.

BEGIN;


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
| Bounded history packet: judgments, pauses, anomalies, follow-ups | `sc sprint compile-report --sprint <id> --limit 50` |
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
follow-ups, the Reviewer-authored final report, completion, and your
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
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_protocol',
  'The shared Sprints v2 protocol every participant follows — lifecycle, wake types, inbox/accept/decline, the typed relay with stable keys, body limits, artifact paths, receipt recovery, and the authority boundary. Load first in every Sprint turn, then your role skill.',
  'workflow',
  NULL,
  0,
  '# sprint_protocol — what every Sprint participant does the same way

Load this first in any Sprint turn, then your role skill (`sprint_prep`,
`sprint_pln`, `sprint_dev`, `sprint_rev`). Use the simplest path the current
durable state supports; treat authority, lifecycle preconditions, durable
writes, and typed handoffs as hard boundaries and use judgment inside them.
Repeat a read only when later activity could have changed it or the next
command requires live revalidation.

## Lifecycle

One Sprint binds one roadmap feature, exact governing spec revisions, a
participant set (one Planner, Developers, Reviewers) each on one
harness/model/effort route, and work units: editing lanes of spec tasks, each
with one Developer and one Reviewer, ordered by dependencies and waves.
`prepared` (editable) -> `armed` (ready lanes dispatch to Developers) <->
`paused` (relay off; restructure and reroute here) -> `completed` or `aborted`
(terminal; nothing deleted). One Sprint is armed at a time. A lane: dispatched
-> Developer builds, registers the PR, requests review -> Reviewer records a
verdict -> Developer merges under the Sprint grant once `authorize-merge`
returns live green + approved -> the merged handoff wakes the Planner, who
dispatches what became ready. After the last lane the conformance Reviewer
records the whole-Sprint report; the engine closes the Sprint, closes the
Developer chats, and deletes the Sprint artifact directory. Planner and
Reviewer chats persist; no worktree is reset.

## Wake types

Three literals name how a Sprint message reaches you:

| Type | Delivery |
|---|---|
| `new` | a fresh chat when the shell has none or its chat is idle; absorbed at the boundary of a live turn |
| `force-new` | never absorbed; waits for the live turn to end and a quiet gate, then closes the old chat and opens a fresh one |
| `re-enter` | resumes the existing chat at its next boundary |

Assignments, review requests, and verdicts arrive `force-new`; Planner-bound
results, decisions, and PR facts arrive `re-enter`. One engine wake names its
moment rather than a sender:

| Wake | Meaning |
|---|---|
| resume re-enter | a pause interrupted your live turn; on resume the engine queues this `re-enter` notification into your chat — continue that lane from its current state, do not re-accept the assignment |

You never choose a type, poll, boot a participant, or schedule a watcher; the
engine delivers. Health is reporting-only: nobody — engine or Planner — pings
a participant for silence; the board carries that signal. Stop after a
successful typed handoff and wait for the next wake.

## Inbox, accept, decline

```text
sc sprint show --sprint <id>
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Inspect the inbox once per trigger. Accepting an assignment starts ownership;
decline only with a concrete reason. After an informational message, `accept`
marks it read and changes no Sprint or work-unit state. Re-run the inbox once
immediately before each typed handoff and act on new items; after the handoff
confirms its durable write, stop without another pass.

## Relay

Every message is one short body file. Unit question or blocker (requires a
reply):

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent question|blocker --requires-reply --work-unit <work-unit-id> \
  --key <stable-key>
```

Cross-unit, closeout, or external-authority rulings are Sprint-level:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level --key <stable-key>
```

Reply through the original message; the reply inherits its scope, so never add
`--work-unit` or `--sprint-level` to a reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent information --reply-to <message-id> --key <stable-key>
```

Confirm the durable reply, then `accept` the incoming message. At a decision
boundary, stop until the required answer arrives; unread recovery re-wakes the
recipient, so send no duplicate reminder.

**Stable key** = recipient + exact body + intent + reply target + scope. Reuse
it only to retry the same failed or ambiguous write; when any field changes,
use a new key. **Body size**: near 6,000 characters and below 8,000 — run
`wc -m < <path>` before sending. A handoff is complete only when the command
exits successfully and confirms the durable write and wake. Rejected or
transport-failed -> correct and retry. Relay itself unavailable -> give the FnB
the attempted command, evidence, impact, and recommendation; invent no
alternate protocol.

## Receipt recovery

An unusable success receipt from idempotent bookkeeping does not stall the
Sprint: retry the exact command once, then use its normal read surface once to
prove the postcondition (for an informational `accept`: the message was in the
inbox and is now absent). Continue under that proof and name the receipt
defect in your next handoff. Never use this to infer assignment ownership,
review outcome, merge authorization, a lifecycle or work-unit transition, the
governing revision, PR head or green state, or cleanup authority; an unproved
postcondition stops.

## Artifacts

Working material — review notes, raw diffs, evidence packets, report drafts,
scratch proof — goes under the gitignored `shared/sprints/sprint-<n>/`; never
commit, branch, or PR it (a review-notes commit is a finding). Durable records
are DB rows: judgments via `record-review`, reports via `record-conformance`,
decisions in the relay.

## Authority

Reviewers own judgments: verdicts, conformance, re-enter and abort decisions.
The Planner owns plan structure — lanes, dependencies, waves, assignment,
routes, pause, resume, dispatch — and executes Reviewer decisions without
re-adjudicating them. A Developer owns one lane and its PR. The FnB may
override any of it from the GUI Sprints tab; name that authority in the
durable evidence when acting under it. A command that rejects a transition
returns the durable state to the deciding role; substitute nothing.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
