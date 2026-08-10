---
name: sprint_dev
description: Execute a Sprints v2 Developer lane — accept one assignment, implement and verify it, own the PR through green and review, merge only after live authorization, and record judgment without overlapping edits.
category: workflow
common: false
---

# sprint_dev — own one editing lane

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
second editing lane or edit another shell's worktree.

If the requirement is ambiguous, choose the shippable reading within your
unit's scope, record the choice and rationale, and continue. Escalate changes to
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
PR'd in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only.

## Build and verify

Sync the assigned repository, work on a feature branch, match the surrounding
code, and implement the smallest complete change. Keep external calls outside
database transactions. Preserve durable identities and append-only evidence.

Verification must exercise the unit's independent stage gate and realistic
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
that handoff the turn's last action. Ask the Planner for later work only after
the current editing lane is terminal.
