---
name: sprint_protocol
description: The shared Sprints v2 protocol every participant follows — lifecycle, wake types, inbox/accept/decline, the typed relay with stable keys, body limits, artifact paths, receipt recovery, and the authority boundary. Load first in every Sprint turn, then your role skill.
category: workflow
common: false
---

# sprint_protocol — what every Sprint participant does the same way

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
returns the durable state to the deciding role; substitute nothing.
