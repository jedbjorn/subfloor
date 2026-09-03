-- 0248 — reseed sprint_pln with Planner orientation and read surfaces.
-- The Planner skill now explains how a Sprint runs and names every read a
-- shell has (including the new `sc sprint show` board read) before the
-- control procedures. No schema change: a full-body UPSERT converges upgraded
-- installations on the same text a fresh seed produces.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_pln',
  'Run an armed Sprints v2 collaboration loop as Planner — dispatch and restructure lanes, change participant routes, and execute Reviewer decisions through durable pause, resume, and close protocols.',
  'workflow',
  NULL,
  0,
  '# sprint_pln — govern the armed Sprint

Use after `sprint_prep` arms Sprint. System records; Reviewer judges/reports;
Planner structures/controls; FnB board override = decision #46.

Use the simplest path supported by current durable state. Treat authority,
lifecycle, writes, and handoffs as hard boundaries. Repeat a read only when
later activity could have changed it or a command requires revalidation.

## How a Sprint runs

One Sprint binds one roadmap feature, exact governing spec revisions, a
participant set (you, Developers, Reviewers) each on one harness/model/Thinking
level (`effort`) route, and work units: editing lanes made of spec tasks, each
with one Developer and one Reviewer, ordered by dependencies and planned waves.

Lifecycle: `prepared` (editable, `sprint_prep`) → `armed` (the runtime
dispatches every dependency-ready lane as a Force-new Developer wake) ⇄
`paused` (relay is off; restructuring and rerouting happen here) →
`completed` or `aborted` (terminal; nothing is deleted). One Sprint is armed at
a time.

A lane''s life: dispatched → Developer builds on a branch, registers the PR,
requests review → Reviewer records a verdict → Developer authorizes the merge
on live green → the merged-work handoff wakes you → you dispatch whatever
became ready. After the last lane the conformance Reviewer records the
whole-Sprint report; the engine closes the Sprint and cleans worktrees. The
engine delivers every wake and watches every registered PR; you never poll or
boot participants.

Authority: Reviewers own verdicts, conformance, and re-enter/abort judgment.
You own plan structure: lanes, dependencies, waves, assignment, routes, pause,
resume, dispatch. The FnB can override any of it from the GUI Sprints tab,
which renders the same projection `sc sprint show` returns.

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
default, and Vibe takes no effort. Nothing else is readable from a shell; the
engine database is not a surface.

## Route the entry

Load `sprint_pln` on every entry, then classify:

| Trigger | Route |
|---|---|
| Sprint decision, merged-work handoff, question, blocker, relay | Inspect `sc sprint inbox --sprint <id>` once; handle that message. |
| Engine-wide completion or cleanup receipt | Inspect receipt + terminal state directly; it is informational—do not run the Sprint inbox, accept it, or close again. |
| Live FnB instruction | Act under board override; name FnB authority in durable evidence. |

Do not poll. Armed runtime owns scheduled dispatch + unread wake recovery;
registered-PR watcher owns subscription observation. Developer-owned subscriptions send
red/green/closed facts to Developers, never Planner.

Assignments/review requests use Force-new delivery; Planner-bound results use
Re-enter. Delivery waits for a natural boundary; runtime owns bundling,
rotation, recovery, and coordinate mode. Stop after a successful typed handoff.

## Durable running loop

Read only trigger-required lifecycle, unit, dependency, route, PR, expectation,
and anomaly facts. Browser presence is not progress.

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
sc sprint dispatch --sprint <id>
```

Dispatch every dependency-ready lane; returned ids are wake identities.
Disposition + messages are release facts. Stable assignment generations and
occupied lanes make dispatch repeat-safe.

Accept/decline only actionable items. After acting on an informational message,
run `accept`; it marks the message read and does not change Sprint or work-unit
state.

An unusable success receipt from idempotent bookkeeping does not stall the
Sprint. Retry the exact command once, then use its normal read surface once to
prove the exact postcondition. For informational `accept`, prior inbox presence
+ absence of that exact message id proves the read landed. Continue under that
proof + name the receipt defect in the next normal handoff. NEVER use this
recovery to infer assignment ownership, review outcome, merge authorization,
lifecycle/work-unit transition, governing revision, PR head/green state, or
cleanup authority. An unproved postcondition stops.

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
- Use native wakes. Start no recurring loop, scheduled job, manual participant
  boot, or external watcher. Do not repeat a stalled-gate inspection:

```text
sc sprint watcher-state --sprint <id>
```

## Relay contract

Unit question/blocker requiring reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent question --requires-reply --work-unit <work-unit-id> \
  --key <stable-key>
```

Use `--intent blocker` for a blocked lane. Cross-unit, closeout, or external
authority rulings are Sprint-level:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level --key <stable-key>
```

Answer through the original message; the server inherits its scope, so never
add `--work-unit` or `--sprint-level` to a reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent information --reply-to <message-id> --key <stable-key>
```

Confirm reply, then `accept` incoming. A missing answer stops at the decision
boundary; unread recovery re-wakes, so send no duplicate. Choose a stable key
for recipient + body + intent + reply + scope; reuse it only for that identical
write. When any of those fields changes, use a new key.

Keep bodies near 6,000 characters and below the 8,000 hard maximum; run
`wc -m < <path>`. Handoff completes only when the command exits successfully
and confirms durable write + wake. If a command is rejected or transport fails,
correct/retry safely. If relay itself fails, give FnB attempted command +
durable evidence; invent no alternate protocol. Relay Developer integrity
evidence, impact, and recommendation to Reviewer. Send required context before
pausing because paused Sprint relay is unavailable.

Keep scratch review notes, diffs, evidence, reports, and proof in gitignored
`shared/sprints/sprint-<n>/`; never commit/branch/PR them. Durable judgment,
reports, and decisions belong in `record-review`, `sprint_reports`, and relay.

## Reviewer decisions and Planner actions

Review/conformance/re-enter/abort judgment remains Reviewer-owned. Planner
independently owns operational plan structure: pause-safe recall, cancellation
of unreleased scope, reassignment, repeated task lanes, and route changes.

For a required-reply Reviewer→Planner Re-enter decision, keep this order:

1. Re-run `sc sprint inbox --sprint <id>`; verify assigned Reviewer + retain id.
2. Send linked acknowledgement:

```text
sc sprint send --sprint <id> --to <reviewer-shortname> --body-file <path> \
  --intent information --reply-to <decision-message-id> \
  --key <stable-control-reply-key>
```

3. Require the reply command to confirm its durable message and wake; retry the
   same command/key if ambiguous.
4. Accept the decision:

```text
sc sprint accept --sprint <id> --message <decision-message-id>
```

5. Only after acceptance, execute the requested transition without
   re-adjudicating it. The linked reply must precede any pause or
   abort that makes the Sprint relay unavailable.

Record decision id + reply, acceptance, and action receipts. Clean conformance
closes atomically and sends informational receipt; no reply/accept is needed.

The FnB board-level override from decision #46 remains distinct. If action
fails lifecycle/authority/disposition precondition, send refusal + durable state
to Reviewer (or FnB for override), substitute nothing, and stop.

### Pause or resume

Pause for Reviewer decision or safe Planner restructuring; preserve partial
artifacts, interrupt intent, judgment, and evidence:

```text
sc sprint pause --sprint <id> --reason <decision-or-restructure-reason>
```

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
new ownership generation before treating the Sprint as resumed.

Exhausted recovery wake = bounded manual evidence: preserve unread message +
failed wake, involve FnB, create no recursive fallback. Drift informs but never
silently blocks resume.

Aborted-Sprint PR ownership repair belongs to the originating Planner. Keep
replacement Sprint paused; establish old/new identity. The originating Planner
may reconcile that identity (FnB may override):

```text
sc sprint reconcile-pr --sprint <replacement-id> --repository <owner/repo> \
  --pr <number> --work-unit <replacement-unit-id> --reason <recovery-reason>
```

It refuses a live source Sprint or target Sprint, a non-originating Planner,
invalid/owned target, and closed-unmerged PR. Receipt records owners, live head,
authority, and merged completion when applicable. Require a separate Reviewer
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

Never edit a released lane in place. Pause → recall unmerged lane → replan →
resume:

```text
sc sprint pause --sprint <id> --reason <restructure-reason>
sc sprint recall-unit --sprint <id> --work-unit <id> --reason <obsolete-reason>
sc sprint replan-unit --sprint <id> --work-unit <id> <changed-fields>
sc sprint resume --sprint <id> --reason <validated-replan-reason>
```

Recall preserves message/event history, returns only unmerged work to planned,
and refuses terminal/PR-bound work. PR-bound work stays; plan replacement or use
reconciliation. Resume creates fresh assignment generation. One spec task may
govern repeated verification/replacement lanes; each lane lists it once. Do not
duplicate the spec task.

Close a released lane terminal when its work finished out-of-band (a PR that
merged while paused) or its lane is abandoned — the PR-bound case recall
refuses:

```text
sc sprint resolve-unit --sprint <id> --work-unit <id> \
  --to completed|cancelled --reason <reason>
```

Paused-only; retires the lane''s open expectations, supersedes its PR links
(registration kept for reconcile-pr), and wakes both seats. Recall+replan
ships revised work; resolve only closes the lane.

To change future assignment/review model, pause armed Sprint, take each
participant''s `shell_id` and current route from `sc sprint show`, preview the
replacement with `sc models resolve`, then replace the route and resume:

```text
sc sprint reroute-participant --sprint <id> --participant-shell <id> \
  --harness <harness> [--model <model>] [--effort <effort>] \
  [--route <display-route>]
```

Prepared Sprints may reroute directly. Paused reroute retires the
participant''s own released expectations and queues fresh ones on the
replacement route at resume; another seat''s open turn still blocks. Existing
chats/runs remain history; next Force-new delivery uses replacement. Reroute
declared participants only. On decline, preserve reason and choose
replacement from current capacity; ask Reviewer only if review/conformance
judgment changes.

### Re-enter after conformance

Reviewer decision names findings; governing tasks (existing for same scope,
new title/description for new scope); and grouping, waves, dependencies,
routing, capacity. Preserve it; do not absorb extra/post-Sprint scope or
maximize occupancy.

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

Reuse task for exact repair/repeat; add only genuinely new scope. Bind every
task, then confirm routes + dependency graph and capacity plan match the
decision. Planner may reassign or reroute for operational capacity; tell
Reviewer if conflict changes scope/judgment. Release ready lanes with
`sc sprint dispatch --sprint <id>`. Engine sends next delivery-terminal wake;
Planner does not initiate conformance.

### Conclude or abort

The clean `record-conformance` command atomically stores conformance, follow-ups,
Reviewer-authored final report, completion, and informational engine-wide
Planner receipt. On Re-enter, verify Sprint/reports/outcome/completed state.
Do not run `complete`; notification is informational because closure is already
terminal. Planner does not author a second report. The originating Planner and
report-authoring Reviewer remain open. Do not manually close peer chats. Pause,
abort, re-entry, failed conformance, and rejected fallback retain no-cleanup
behavior.

The initial completion receipt reports `cleanup_state=pending`. Delivery is
finished, but managed worktrees are not reusable. Stop for the engine-wide
cleanup receipt; do not poll or manually reset participant trees. On
`cleanup_state=succeeded`, treat the slots as reusable. On failure, inspect once
and retry only after correcting the named condition:

```text
sc sprint cleanup-status --sprint <id>
sc sprint cleanup --sprint <id> --key <stable-retry-key>
```

Require `created`, cleanup request id, action, exact target ids, and aggregate
projection. Reuse the key only for the same request. FnB alone may add
`--adopt-legacy` for a completed Sprint with no scheduled targets; neither caller
supplies a path.

Do not compile or editorialize by default; Reviewer owns evidence/report. Only
FnB-directed fallback may run:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
```

Abort only on Reviewer decision/FnB override; it is terminal and deletes
nothing.

## Handoffs and stop

Planner receives no PR-event wakes. Never dispatch the next wave from merge
observation. The merged-work handoff wake is the only normal next-wave dispatch trigger.
On it:

1. Run `sc sprint inbox --sprint <id>`; inspect merged handoff + unit/dependency state.
2. Handle earlier informational items and `accept` each, including handoff.
3. Finish reconciliation and Planner bookkeeping; no work remains.
4. As literal final action run `sc sprint dispatch --sprint <id>`.
5. Require durable assignments + New wakes, then stop. Run no trailing command.
   Empty dispatch remains final; investigate only on a later durable wake.

On an initial clean completion receipt, verify the named Sprint is terminal and
record `cleanup_state=pending`; run no close command. Stop until the
engine-authored cleanup success or failure receipt arrives. The Planner does
not author a second report, accept an actionable handoff, poll cleanup, or ask
another role to reset a worktree.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
