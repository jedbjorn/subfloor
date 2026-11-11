-- 0284 — Sprint consistency reseed. Reseeds the four Sprint skills this pass
-- rewrote: sprint_dev, sprint_pln, sprint_protocol, sprint_rev. The engine
-- releases ready lanes at merge (the merged handoff is a result record), any
-- participant may pause for integrity, the Sprint inbox is read once per wake,
-- the delivery-terminal notice reaches only the conformance owner, and
-- `sc sprint complete` is named as the Planner's recovery close. No schema
-- change; full-body UPSERTs by name keep skill_id and grants. Idempotent.
BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'sprint_dev',
  'Execute a Sprints v2 Developer lane — accept one assignment, implement and verify it, own the PR through green and review, merge under the Sprint grant once live authorization returns, and record judgment without overlapping edits.',
  'workflow',
  NULL,
  0,
  '# sprint_dev — own one editing lane

Load `sprint_protocol` first; it holds the lifecycle, wake types, inbox
commands, relay contract, the typed-handoff order and body limit, artifact
paths, receipt recovery, and authority boundary. This skill holds only the
Developer''s steps. Your typed handoffs are `complete-unit`, `register-pr`,
`request-review`, and the merged handoff `send`; each follows the protocol''s
order.

## Route the entry

| Trigger | First read / action |
|---|---|
| Assignment, verdict, question, blocker, relay | Inspect `sc sprint inbox --sprint <id>` once; accept or handle the relevant message. Accepted assignment -> `sc context --work-unit <id>` next. |
| Self-describing engine-wide PR fact | Inspect the fact + registered PR directly. It is not a Sprint inbox item; do not run the inbox for it. |
| Live FnB instruction | Preserve its authority; read only durable state needed for safe action. |

## Bound the lane

`sc context --work-unit <id>` is the default planning context: assignment,
expected output, linked tasks, bound revision id, active decisions,
dependencies, unit blockers, roles, worktree, lifecycle walls, resources. Read
the full bound revision or broader indexes only for an unresolved need. Own
one active unit; never start another lane or edit another shell''s worktree.
Resolve ambiguity to shippable in-scope work + rationale. Ask the Planner
before changing boundary, interface, deliverable, priority, or scope; ask the
Reviewer about review evidence. Use the relay''s unit question/blocker form and
stop at a decision boundary until the answer arrives.

Report blocker evidence to the Planner, continue safe independent work, and
stop at the unsafe boundary; the Reviewer decides continue/replan and the
Planner executes the decision.

Pause the Sprint yourself only for an integrity problem that makes continuing
unsafe, in the protocol''s integrity-pause order: one `--intent information`
message to the Planner with the evidence, impact, and recommendation, then
`sc sprint pause --sprint <id> --reason <what is unsafe>` as the turn''s last
action.

## Build and verify

Sync + branch; implement the smallest complete change. Verification follows
TESTING POSTURE (`spec_execution`): what to run, the CI fallback when a local
gate cannot execute, and what counts as failure. In a Sprint the registered PR
supplies only the proof the seat could not run. Keep external calls outside DB
transactions; preserve durable identities and append-only evidence. Record
failures, anomalies, retries, review friction, and departures for closeout.

## Report-only or no-code completion

Only an explicitly planned report/no-code lane may finish without a PR. Its
final handoff requires a durable completion receipt:

```text
sc sprint complete-unit --sprint <id> --work-unit <id> \
  --result-file <path>
```

Stop after success. A code lane continues through merge observation.

## Register and observe the PR

```text
sc sprint register-pr --sprint <id> --repository <owner/name> \
  --pr <number> --work-unit <id>
```

Register complete code even when a local gate is unavailable; registration
obtains evidence, not review. After `register-pr` succeeds, retain ownership;
red/green/closed/merged wakes continue (the engine may already have discovered
the PR from your worktree branch; `register-pr` attaches it). Required-check
states are TESTING POSTURE''s (`spec_execution`); in a Sprint, pending -> stop
for the native wake, green -> judge, then request review, and a blocked gate
-> report it to the Planner as a blocker. Follow context: armed -> fix red +
judge/pass green + merged -> post-merge handoff;
paused -> fix red now + judge green, review after resume; no active Sprint ->
fix red if needed, green -> merge only on a standing FnB directive naming the
PR, merged -> git skill after-merge cleanup. Planner/Reviewer get none.

If the same registered PR was externally closed, then reopened, rebased, and
pushed, replay the exact `register-pr` command. Require `created: false`, which
keeps identity/ownership and takes a fresh snapshot.
Do not wait for a second PR-fact wake: immediately request review. Green
proceeds; any other snapshot returns the watcher diagnostic without partial
handoff. Never register a replacement PR or ask the Planner to bypass observed
green.

Otherwise, when no local action remains, stop for the native PR fact. A stalled
gate permits one bounded read, then stop or report its evidence:

```text
sc sprint watcher-state --sprint <id>
```

Do not repeat this read as a polling loop.

## Review handoff and correction

Complete each round in order:

1. Finish readiness judgment + available local proof; require observed green.
2. Use `submit` first or `resubmit` after changes requested. The engine injects
   the PR URL, registered id, exact green head, and work-unit id into the
   Reviewer''s canonical bare one-line locator. Create no readiness file. Send
   no scope narrative, verification evidence, rationale, or review-focus
   steering in the request. The PR body carries the work-unit id and spec
   reference plus your rationale (decisions, rejected trade-offs): each verdict
   opens a fresh chat with only GitHub to read. Write no PR comments or
   annotations.
3. Final handoff:

```text
sc sprint request-review \
  --sprint <id> --registered-pr <registered-id> \
  --intent <submit|resubmit> --key <stable-key>
```

   Require the durable write + Reviewer wake, then await the native verdict
   wake.

Changes requested arrives as a fresh chat: orient from the PR body, diff, and
verdict; do not re-litigate choices the rationale explains. Apply every
blocking finding, re-establish green, and resubmit with a new review-round key.
The engine pauses the Sprint for the FnB when a unit''s eighth recorded verdict
is still changes requested: the lane is not converging. Request no further
review then; wait for the resume wake or the Planner''s direction.
Do not narrate cleared findings; the Reviewer verifies the full diff. Record
disagreements as judgment. Reviewer owns scope/severity; Planner executes
resulting action.

## Merge boundary

Immediately before merge, re-read live GitHub, grant, ownership, unit state,
and checks through:

```text
sc sprint authorize-merge \
  --sprint <id> --registered-pr <registered-id>
```

Merge only the returned repository, PR, and head SHA. A refusal means not
green, not approved, or not yours; fix that, never bypass it. A rebase does not
undo approval. The FnB granted this merge by arming the Sprint; this command
verifies that grant live and is the only gate — never wait for a separate FnB
directive, and never merge on approval or green alone.

## Post-merge handoff

After the authorized merge:

1. Clean the worktree; put merged PR + SHA, unit result, verification,
   judgments, and departures in the handoff file.
2. Final handoff:

```text
sc sprint send --sprint <id> --to <planner-shortname> --body-file <path> \
  --intent handoff --key <stable-merged-handoff-key>
```

   Require the durable message + Planner wake. Automatic merge observation
   completes the unit and releases the lanes that became ready; this handoff
   is the lane''s result record for the Planner.

## Report and stop

Report broken bases, destructive ambiguity, unavailable GitHub, untrustworthy
runners, provider exhaustion, or unrecoverable environment with evidence,
impact, and recommendation. Stop when merged + reported, declined, returned to
review, paused for a native wake, or awaiting Planner/FnB recovery. Ask for
later work only after this editing lane is terminal.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'sprint_pln',
  'Run an armed Sprints v2 collaboration loop as Planner — dispatch and restructure lanes, change participant routes, and execute Reviewer decisions through durable pause, resume, and close protocols.',
  'workflow',
  NULL,
  0,
  '# sprint_pln — govern the armed Sprint

Load `sprint_protocol` first; it holds the lifecycle, wake types, inbox
commands, relay contract, the typed-handoff order and body limit, artifact
paths, receipt recovery, and authority boundary. This skill holds only the Planner''s steps after
`sprint_prep` arms the Sprint.

## What you can read

| Need | Read |
|---|---|
| Whole Sprint: lifecycle, participants with current routes, units, dependencies, PRs, health | `sc sprint show --sprint <id>` |
| Full append-only event log — the authoritative lifecycle read (look for `lifecycle.completed` / `lifecycle.aborted`) | `sc sprint timeline --sprint <id> [--last <n>]` |
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

1. From this wake''s inbox read, verify the assigned Reviewer + retain the id.
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
artifacts, interrupt intent, judgment, and evidence. A Developer or Reviewer
may also pause for integrity: the pause notice carries the reason and the
pauser''s evidence message waits in the Sprint inbox — read it once, reconcile,
then resume as below:

```text
sc sprint pause --sprint <id> --reason <decision-or-restructure-reason>
```

Pause interrupts every live participant turn, and resume re-enters each
interrupted participant automatically with one re-enter wake into their
existing chat. For silence, read the board (`sc sprint show --sprint <id>`);
the protocol''s health rule forbids status-check messages.

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
Do not run `complete` after it; do not author a second report; do not manually
close peer chats. `sc sprint complete --sprint <id> --reason <reason>
--outcome <outcome> [--report-file <path> --key <stable-key>]` is a recovery
close, not the normal one: use it only when every unit is terminal and the
Reviewer''s `record-conformance` cannot close the Sprint, and say why in the
reason. It refuses non-terminal work and records no conformance report.

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

The engine releases dependency-ready lanes itself when it observes a merge;
the Developer''s merged-work handoff is the lane''s result record, not a
dispatch trigger. On that handoff wake:

1. Read `sc sprint inbox --sprint <id>` once; inspect the merged handoff +
   unit/dependency state.
2. Handle informational items and `accept` each, including the handoff.
3. Finish reconciliation and Planner bookkeeping, then stop.

Run `sc sprint dispatch --sprint <id>` yourself only after a restructure, or
when `sc sprint show` has a ready lane undispatched. Then it is the literal
final action: require durable assignments + wakes and run no trailing
command. An empty dispatch remains final.

On a clean completion receipt, verify the named Sprint is terminal and record
the closed Developer chats; run no close command. Then stop — a cleanup wake
arrives only if artifact deletion failed.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'sprint_protocol',
  'The shared Sprints v2 protocol for every participant; load it first in every Sprint turn, then your role skill. Covers lifecycle, wake types, inbox/accept/decline, the typed relay with stable keys, the one handoff order and body limit, artifact paths, receipt recovery, and the authority boundary.',
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
returns live green + approved -> the engine observes the merge, completes the
unit, and releases the lanes that became ready; the Developer''s merged handoff
is the lane''s result record for the Planner. After the last lane the
conformance Reviewer records the whole-Sprint report; the engine closes the Sprint, closes the
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

Sprint communication is by wake only: read the inbox once per wake and never
re-read it within the turn; a new message arrives as its own wake. The one
exception is receipt recovery (below): a single further inbox read to prove a
postcondition after an unconfirmed write. Accepting
an assignment starts ownership;
decline only with a concrete reason. After an informational message, `accept`
marks it read and changes no Sprint or work-unit state.

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
use a new key. Rejected or transport-failed -> correct and retry. Relay itself
unavailable -> give the FnB the attempted command, evidence, impact, and
recommendation; invent no alternate protocol.

## Typed handoffs

A typed handoff is a command that passes work to another seat: a relay `send`,
`complete-unit`, `register-pr`, `request-review`, `record-review`,
`record-conformance`. Every one follows this order; role skills name their
handoffs and add only what differs.

1. Finish the work, the bookkeeping, and every body file first.
2. Keep each body, result, verdict, and report file near 6,000 characters. The
   engine refuses anything over 8,000 and writes nothing, so check with
   `wc -m < <path>`.
3. Run the command. It is complete only when it exits successfully and
   confirms the durable write and the wake.
4. Do not run the inbox before or after it. A handoff a role skill marks
   **final** ends the
   turn: it is the literal last action, with no trailing Git, Sprint, inbox,
   cleanup, or status command after it. Stop and wait for the next wake.

**Integrity pause** — the one exception to stopping after a handoff. Any
participant may pause for an integrity problem that makes continuing unsafe:

1. Send the Planner one `--intent information` message with the evidence,
   impact, and recommendation. Do not stop on it.
2. Immediately run `sc sprint pause --sprint <id> --reason <what is unsafe>`
   as the literal last action.

The pause notifies the Planner with the reason and interrupts every live
participant turn, yours included, so nothing can follow it. Wakes are not
delivered while paused; your message waits in the Planner''s Sprint inbox.

## Receipt recovery

An unusable success receipt from idempotent bookkeeping does not stall the
Sprint: retry the exact command once, then use its normal read surface once to
prove the postcondition (for an informational `accept`: the message was in the
inbox and is now absent — the one inbox re-read a turn allows). Continue under that proof and name the receipt
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
re-adjudicating them. A Developer owns one lane and its PR. Any participant
may pause the Sprint for integrity (Typed handoffs). The FnB may
override any of it from the GUI Sprints tab; name that authority in the
durable evidence when acting under it. A command that rejects a transition
returns the durable state to the deciding role; substitute nothing.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'sprint_rev',
  'Review Sprints v2 work units and whole-Sprint conformance as Reviewer; load after sprint_protocol in every Sprint review turn. Own review, re-enter, abort, and conclude judgments, author the conformance and Sprint reports, and direct safety actions through durable messages. Inside an armed Sprint the recorded verdict replaces the Reviewer boot''s FnB-gated handoff.',
  'workflow',
  NULL,
  0,
  '# sprint_rev — independent review and conformance

Load `sprint_protocol` first; it holds the lifecycle, wake types, inbox
commands, relay contract, the typed-handoff order and body limit, artifact
paths, receipt recovery, and authority boundary. This skill holds only the
Reviewer''s steps: pre-declaration QA/QC, one work-unit review, or whole-Sprint
conformance. Your typed handoffs are `record-review`, `record-conformance`,
and a Sprint-level `decision` to the Planner; each is final and follows the
protocol''s order.

Inside an armed Sprint this verdict path stands in place of the Reviewer
boot''s FnB-gated handoff. Your boot''s REVIEW PROCEDURE has you draft the
handoff, propose it to the FnB, and send only on approval; here the FnB
already authorized the loop by arming the Sprint, so you record the verdict
or decision directly, with its findings in the body, and wait for no
per-review approval. Outside an armed Sprint the boot procedure governs.

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
cancellation, and validated resume. Never run standalone replan, recall,
reroute, cancel, resume, complete, or abort actions; clean
`record-conformance` alone performs its narrow atomic close.

Pause the Sprint yourself only for an integrity problem that makes continuing
unsafe, in the protocol''s integrity-pause order: one `--intent information`
message to the Planner with the evidence, impact, and recommendation, then
`sc sprint pause --sprint <id> --reason <what is unsafe>` as the turn''s last
action. That message is not a `decision` and is not final; the pause
is.

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

Finish every inspection, finding, and the verdict body, then record the
verdict as the final handoff:

```text
sc sprint record-review \
  --sprint <id> --registered-pr <registered-id> \
  --verdict changes_requested|approved --body-file <path> --key <stable-key>
```

Require durable judgment evidence + the Developer wake.

Use `approved` only with no Critical/Major/Medium finding. Engine validation
requires the accepted request. Do not message around the surface; an
unrecorded verdict cannot unlock merge.

## Delivery-terminal closeout

Retain the exact notification message id + delivered wake as this closeout
episode''s identity. The engine sends this notification only to the selected
conformance owner for the current ownership generation, and its body names no
owner: receiving it normally means you are that owner. Ownership can move
across a pause and resume while an earlier notice is still delivered: if
`sc sprint show` names a different `conformance_owner`, or `record-conformance`
is refused as not the selected conformance Reviewer, `accept` the notice and
stop without sending anything. Inspect inbox, lifecycle, and units first:

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

Before recording conformance, author the final Sprint report. Name yourself as
author and cover governing scope/revisions, shipped units/PRs, judgments +
ratified deviations, failures/retries/recovery/anomalies, conclusion,
follow-ups, and evidence location. Preserve discrepancies. Both reports are
handoff bodies under the protocol''s limit.

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

Each ending is a final handoff in the protocol''s order:

- Unit review -> `record-review`.
- Clean conclude -> the atomic `record-conformance` command above, once every
  artifact and body is final. When it confirms completed state and all receipt
  identities, stop; the Planner is notified.
- Re-enter/abort -> the Sprint-level `decision` to the Planner (relay form in
  `sprint_protocol`); require durable write + Planner wake, and run nothing
  more until another native wake.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
