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
second editing lane or edit another shell's worktree.

If the requirement is ambiguous, choose the shippable reading within your
unit's scope, record the choice and rationale, and continue. Escalate changes to
the unit boundary, interfaces another unit consumes, deliverable cuts, or scope
growth to the Planner.

The active-chat registry, not Sprint participant pointers, owns your current
chat. Assignments are declared New, but a verified live turn is never displaced:
delivery enters this chat at its natural boundary and drains every undelivered
wake message. When idle, New rotates to a fresh chat; Re-enter resumes the
registry chat; no registry row behaves as New. A generous inactivity ceiling
unlinks a silent hung turn so the 60-second reaper can terminate its verified
process identity.

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

## Build and verify

Sync the assigned repository, work on a feature branch, match the surrounding
code, and implement the smallest complete change. Keep external calls outside
database transactions. Preserve durable identities and append-only evidence.

Verification must exercise the unit's independent stage gate and realistic
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

## Review handoff

Complete a review handoff in this exact order:

1. Finish the readiness claim and every local verification step.
2. Re-run `sc sprint inbox --sprint <id>`, act on newly arrived messages, and
   mark every handled informational message read with `accept`.
3. Run `wc -m < <path>`; keep the claim near 6,000 characters and below the
   8,000-character hard maximum.
4. As the literal final action of the turn, send the typed handoff with one
   stable retry key:

```text
sc sprint request-review \
  --sprint <id> --registered-pr <registered-id> \
  --readiness-file <path> --key <stable-key>
```

5. When the command confirms the durable write and Reviewer wake, immediately
   stop and await the native verdict wake. Run no trailing command.

A changes-requested verdict
returns as Re-enter to your registry chat. Apply every blocking finding,
re-establish green, and hand back with a new stable review key. Record
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
recovery, or returned to review. For normal review and merge handoffs, the
ordered procedures above place inbox handling before the typed handoff and make
that handoff the turn's last action. Ask the Planner for later work only after
the current editing lane is terminal.
