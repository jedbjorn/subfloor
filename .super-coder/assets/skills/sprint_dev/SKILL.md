---
name: sprint_dev
description: Execute a Sprints v2 Developer lane — accept one assignment, implement and verify it, own the PR through green and review, merge under the Sprint grant once live authorization returns, and record judgment without overlapping edits.
category: workflow
common: false
---

# sprint_dev — own one editing lane

Load `sprint_protocol` first; it holds the lifecycle, wake types, inbox
commands, relay contract, the typed-handoff order and body limit, artifact
paths, receipt recovery, and authority boundary. This skill holds only the
Developer's steps. Your typed handoffs are `complete-unit`, `register-pr`,
`request-review`, and the merged handoff `send`; each follows the protocol's
order.

## Route the entry

| Trigger | First read / action |
|---|---|
| Assignment, verdict, question, blocker, relay | Inspect `sc sprint inbox --sprint <id>` once; accept or handle the relevant message. Accepted assignment -> `sc context --work-unit <id>` next. |
| Self-describing engine-wide PR fact | Inspect the fact + registered PR directly. Do not manufacture a Sprint inbox item; the next typed handoff's inbox check covers it. |
| Live FnB instruction | Preserve its authority; read only durable state needed for safe action. |

## Bound the lane

`sc context --work-unit <id>` is the default planning context: assignment,
expected output, linked tasks, bound revision id, active decisions,
dependencies, unit blockers, roles, worktree, lifecycle walls, resources. Read
the full bound revision or broader indexes only for an unresolved need. Own
one active unit; never start another lane or edit another shell's worktree.
Resolve ambiguity to shippable in-scope work + rationale. Ask the Planner
before changing boundary, interface, deliverable, priority, or scope; ask the
Reviewer about review evidence. Use the relay's unit question/blocker form and
stop at a decision boundary until the answer arrives.

A Developer does not pause the Sprint. Report blocker or integrity evidence to
the Planner, continue safe independent work, and stop at the unsafe boundary.
The Reviewer decides continue/replan/pause; the Planner executes the decision.

## Build and verify

Sync + branch; implement the smallest complete change. Per TESTING POSTURE
(`spec_execution`), finish code + run every available smallest affected gate. If the
selected interpreter, runner, or declared dependency cannot execute one,
record exact seat evidence; the registered PR supplies only that proof. Test
assertion/source collection red or incomplete code = failure. Optional browser
skip = non-failing. Keep external calls outside DB transactions; preserve
durable identities and append-only evidence. Record failures, anomalies,
retries, review friction, and departures for closeout.

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
the PR from your worktree branch; `register-pr` attaches it). Required checks:
pending -> native wake; red -> fix/push; green -> judge/request review; none or
untrustworthy watcher after one bounded read -> report + block. Follow
context: armed -> fix red + judge/pass green + merged -> post-merge handoff;
paused -> fix red now + judge green, review after resume; no active Sprint ->
fix red if needed, green -> merge only on a standing FnB directive naming the
PR, merged -> git skill after-merge cleanup. Planner/Reviewer get none.

If the same registered PR was externally closed, then reopened, rebased, and
pushed, replay the exact `register-pr` command. Require `created: false`, which
keeps identity/ownership and takes a fresh snapshot. This is the one exception
to the protocol's inbox step: a single pre-handoff inbox check covers the
registration replay + the immediately following review request.
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
   Reviewer's canonical bare one-line locator. Create no readiness file. Send
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
   records the PR transition; this handoff releases the next wave.

## Report and stop

Report broken bases, destructive ambiguity, unavailable GitHub, untrustworthy
runners, provider exhaustion, or unrecoverable environment with evidence,
impact, and recommendation. Stop when merged + reported, declined, returned to
review, paused for a native wake, or awaiting Planner/FnB recovery. Ask for
later work only after this editing lane is terminal.
