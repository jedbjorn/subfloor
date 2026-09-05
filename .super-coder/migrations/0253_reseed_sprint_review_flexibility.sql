-- 0253 — reseed the Sprint role skills for review flexibility.
-- Verdicts now deliver Force-new (a fresh Developer chat per review round);
-- approval binds to the PR, not to a head or base SHA, so rebases no longer
-- force phantom review rounds; the PR body carries implementation rationale.
-- No schema change: a full-body UPSERT converges upgraded installations on the
-- same text a fresh seed produces.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_close',
  'Route Sprints v2 closeout to the owning role — Reviewer conformance, Planner control actions or completion receipt, and explicit FnB fallbacks — without creating a second close workflow.',
  'workflow',
  NULL,
  0,
  '# sprint_close — route the terminal boundary

Use as a closeout router, not as a second operating workflow. The Reviewer owns
conformance and final-report judgment; the Planner executes Reviewer control
decisions; clean conformance closes atomically; the FnB owns explicit fallback
and follow-up disposition.

Use the simplest path supported by current durable state. Treat authority,
lifecycle preconditions, durable writes, and typed handoffs as hard boundaries;
use judgment within them. Repeat a read only when later activity could have
changed it or the next command requires live revalidation.

## Route the entry

- **Selected conformance Reviewer receives `sprint.delivery_terminal`.** Confirm
  the notification''s owner shell and generation match the current Sprint.
  Load `sprint_rev` and follow **Delivery-terminal closeout**. Inspect the Sprint
  inbox once, compile bounded evidence, and choose between in-Sprint re-entry,
  abort, or the atomic clean `record-conformance` path. Any other Reviewer
  records no conformance. The Planner does not initiate this pass.
- **Planner receives a Sprint-scoped Reviewer decision.** Load `sprint_pln` and
  follow **Reviewer decision actions**. Inspect and handle the durable inbox
  message once, then execute the exact requested transition without
  re-adjudicating it.
- **Planner receives an engine-wide completion or cleanup receipt.** Load
  `sprint_pln` and follow **Conclude or abort**. Inspect the self-contained
  receipt and terminal Sprint state directly. The completion receipt leaves
  cleanup pending; the later cleanup receipt makes reuse or recovery explicit.
  Do not run the Sprint inbox, accept the receipt, compile another report, or
  run `complete`.
- **FnB directs a fallback or follow-up disposition.** Use the bounded surfaces
  below and name FnB authority in the evidence.

Assignments, review requests, and verdicts use Force-new delivery; Planner-bound
results and PR events use Re-enter. Neither displaces a live turn; the runtime owns delivery, rotation,
and recovery. A successful typed handoff is the last action of that role''s
turn.

## Authority boundary

The Reviewer decides whether evidence warrants re-entry, pause, re-plan,
cancellation, abort, or clean completion. The Planner executes control
decisions. The Reviewer''s clean `record-conformance` command is the one narrow
exception: it stores conformance, findings, and the Reviewer-authored final
report, completes the Sprint, and publishes the informational Planner receipt
atomically. FnB retains the board-level override from decision #46.

Any successful completion automatically closes other active participant chats
immutably linked to that Sprint while retaining the originating Planner and the
report-authoring Reviewer. Do not manually close peer chats as an extra
closeout step. Pause, abort, re-entry, failed conformance, and rejected fallback
completion never invoke this cleanup.

Successful close schedules exact participant-worktree and Sprint-artifact
cleanup. No role manually resets those targets after completion. The initial
Planner receipt reports pending cleanup; the System later sends succeeded or
failed cleanup evidence. A failed receipt names the bounded status and retry
commands.

If a command rejects a decision, preserve the returned durable state. Do not
substitute another transition or invent an alternate handoff. Return the
conflict to the deciding role, or surface it to FnB when the relay itself is
unavailable.

## FnB-directed fallbacks

The participating Reviewer normally compiles the bounded packet. Planner or
FnB compilation is valid only when FnB explicitly directs it:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
```

Raise the limit only when truncation counters omit needed evidence; 200 is the
maximum. The packet supplies facts, not judgment. The standalone `complete`
surface is likewise an FnB-directed recovery fallback, never the normal clean
close path.

FnB may inspect any completed Sprint''s bounded cleanup state, retry failed
targets, or explicitly adopt exactly one historical completed Sprint that has
no targets. Target identities are System-derived; never supply a path or add a
confirmation handshake:

```text
sc sprint cleanup-status --sprint <id>
sc sprint cleanup --sprint <id> --key <stable-retry-key>
sc sprint cleanup --sprint <legacy-id> --adopt-legacy \
  --key <stable-adoption-key>
```

Require the cleanup request id, `created`, action, exact target ids, and
aggregate projection. Reuse a key only for the identical request.

Abort remains a Planner action on a Reviewer decision or FnB override and
deletes nothing:

```text
sc sprint abort --sprint <id> --reason <reason> [--outcome <outcome>]
```

After closure, FnB records one disposition per pending follow-up. `accepted`
acknowledges ship-as-is; `resolved` and `dismissed` require a bounded resolution
file.

```text
sc sprint disposition-followup --sprint <id> --followup <id> \
  --disposition accepted
sc sprint disposition-followup --sprint <id> --followup <id> \
  --disposition resolved --resolution-file <path>
```

## Sprint artifact paths

Sprint working artifacts (per-unit review notes, raw diffs, evidence packets,
report drafts, and Dev scratch proof) go to the gitignored
`shared/sprints/sprint-<n>/` directory. They are never committed, branched, or
PR''d in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only.

## Stop

After routing, continue in the owning role skill. Stop when the typed handoff or
terminal receipt has been handled. Do not perform a second close action or
duplicate another role''s report.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_dev',
  'Execute a Sprints v2 Developer lane — accept one assignment, implement and verify it, own the PR through green and review, merge only after live authorization, and record judgment without overlapping edits.',
  'workflow',
  NULL,
  0,
  '# sprint_dev — own one editing lane

Use for an actionable armed-Sprint assignment. Use the simplest path supported
by current durable state. Treat ownership, lifecycle, writes, and handoffs as
hard boundaries. Repeat a read only when later activity could have changed it
or a command requires live revalidation.

## Route the entry

Load `sprint_dev` on every entry, then classify it:

| Trigger | First read / action |
|---|---|
| Assignment, verdict, question, blocker, relay | Inspect `sc sprint inbox --sprint <id>` once; accept or handle the relevant message. |
| Self-describing engine-wide PR fact | Inspect the fact + registered PR directly. Do not manufacture a Sprint inbox item; check the inbox once immediately before the next typed handoff. |
| Live FnB instruction | Preserve its authority; read only durable state needed for safe action. |

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Accepting starts ownership; decline concretely. After an informational message,
`accept` marks the message read and does not change Sprint or work-unit state.

For an unusable bookkeeping receipt, retry the exact command once, then use its
normal read surface once to prove the postcondition. For informational `accept`,
prior inbox presence + absence of that exact message id proves the read landed;
name the defect next handoff. NEVER infer assignment ownership, review outcome,
merge authorization, lifecycle/work-unit transition, governing revision, PR
head/green state, or cleanup authority. An unproved postcondition stops.

Assignments, review requests, and verdicts use Force-new delivery; PR-event
wakes use Re-enter. Delivery waits for a natural boundary; the runtime owns
bundling, rotation, and recovery. Stop after a successful typed handoff.

## Bound the lane

Read assignment, output, bound revision, dependencies, roles, worktree, grant,
and judgments. Own one active unit; never start another lane or edit another
shell''s worktree. Resolve ambiguity to shippable in-scope work + rationale. Ask
Planner before changing boundary, interface, deliverable, priority, or scope.

Put one question, blocker, decision, answer, or useful context item in a short
body file. Unit questions/blockers require a reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent question --requires-reply --work-unit <work-unit-id> \
  --key <stable-key>
```

Use `--intent blocker` for a blocked lane. Cross-unit, closeout, or external
authority rulings are Sprint-level decisions:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level --key <stable-key>
```

Reply through the original message; the server inherits its scope, so never add
`--work-unit` or `--sprint-level` to a reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent information --reply-to <message-id> --key <stable-key>
```

Ask the Reviewer about review evidence and the Planner about scope or
cross-unit authority. Confirm the durable reply, then `accept` the incoming
message. At a decision boundary, stop until the required answer arrives;
unread recovery re-wakes, so send no duplicate reminder.

Stable key = recipient + exact body + intent + reply + scope. Reuse it only for
the same failed/ambiguous write; when any of those fields changes, use a new
key. Keep bodies near 6,000 characters and below
8,000; run `wc -m < <path>`. Handoff completes only when the command exits
successfully and confirms durable state + wake. If a command is rejected or
transport fails, correct/retry. If relay itself fails, give FnB command +
evidence + impact + recommendation; invent no alternate protocol.

A Developer does not pause the Sprint. Report blocker or integrity evidence to
the Planner, continue safe independent work, and stop at the unsafe boundary.
The Reviewer decides continue/replan/pause; the Planner executes the decision.

Scratch proof/diffs/reports -> gitignored `shared/sprints/sprint-<n>/`; never
commit/PR them. Durable judgment -> `record-review`; reports -> `sprint_reports`;
decisions -> relay.

## Build and verify

Sync + branch; implement the smallest complete change. Per boot `TESTING
POSTURE`, finish code + run every available smallest affected gate. If the
selected interpreter, runner, or declared dependency cannot execute one,
record exact seat evidence; the registered PR supplies only that proof. Test
assertion/source collection red or incomplete code = failure. Optional browser
skip = non-failing. Keep external calls outside DB transactions; preserve
durable identities and append-only evidence. Record failures, anomalies,
retries, review friction, and departures for closeout.

Immediately before `complete-unit`, `register-pr`, or `request-review`, re-run
`sc sprint inbox --sprint <id>` once and act on new messages. After the typed
handoff confirms its durable write, stop without another inbox pass. The
reopened-PR route below is the sole exception.

## Report-only or no-code completion

Only an explicitly planned report/no-code lane may finish without a PR. Keep
the result near 6,000 characters and below 8,000; run `wc -m < <path>`, perform
the pre-handoff inbox check, then require a durable completion receipt:

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
Red/green/closed/merged Re-enter wakes continue (the engine may already have
discovered the PR from your worktree branch; `register-pr` attaches it). Required checks: pending -> native
wake; red -> fix/push; green -> judge/request review; none or untrustworthy
watcher after one bounded read -> report + block. Follow context: armed -> fix
red + judge/pass green + merged -> post-merge handoff; paused -> fix red now +
judge green, review after resume; no active Sprint -> fix red if needed, green
arrives only as red recovery, merged -> git skill after-merge cleanup.
Planner/Reviewer get none.

If the same registered PR was externally closed, then reopened, rebased, and
pushed, replay the exact `register-pr` command. Require `created: false`, which
keeps identity/ownership and takes a fresh snapshot. Its one pre-handoff inbox
check covers registration replay + the immediately following review request.
Do not wait for a second PR-fact wake: immediately request review. Green
proceeds; any other snapshot returns the watcher diagnostic without partial
handoff. Never register a replacement PR or ask the Planner to bypass observed
green.

Otherwise, when no local action remains, stop for the native PR fact. Start no
recurring loop, scheduled job, daemon, or external watcher. A stalled gate
permits one bounded read, then stop or report its evidence:

```text
sc sprint watcher-state --sprint <id>
```

Do not repeat this read as a polling loop.

## Review handoff and correction

Complete each round in order:

1. Finish readiness judgment + available local proof; require observed green.
2. Perform the once-only inbox check; handle and `accept` new messages.
3. Use `submit` first or `resubmit` after changes requested. The engine injects
   the PR URL, registered id, exact green head, and work-unit id into the
   Reviewer''s canonical bare one-line locator. Create no readiness file. Send
   no scope narrative, verification evidence, rationale, or review-focus
   steering in the request. The PR body carries the work-unit id and spec
   reference plus your rationale (decisions, rejected trade-offs): each verdict
   opens a fresh chat with only GitHub to read. Write no PR comments or
   annotations.
4. As the literal final action, run:

```text
sc sprint request-review \
  --sprint <id> --registered-pr <registered-id> \
  --intent <submit|resubmit> --key <stable-key>
```

5. Require confirmation of the durable write + Reviewer wake; run no trailing
   command and stop and await the native verdict wake.

Changes requested arrives as a fresh chat: orient from the PR body, diff, and
verdict; do not re-litigate choices the rationale explains. Apply every
blocking finding, re-establish green, and resubmit with a new review-round key.
Do not narrate cleared findings; the Reviewer verifies the full diff. Record disagreements as judgment. Reviewer owns scope/severity; Planner
executes resulting action.

## Merge boundary

Immediately before merge, re-read live GitHub, grant, ownership, unit state,
and checks through:

```text
sc sprint authorize-merge \
  --sprint <id> --registered-pr <registered-id>
```

Merge only the returned repository, PR, and head SHA. A refusal means not
green, not approved, or not yours; fix that, never bypass it. A rebase does not
undo approval.

## Post-merge handoff

After the authorized merge:

1. Clean the worktree; put merged PR + SHA, unit result, verification,
   judgments, and departures in the handoff file.
2. Re-run `sc sprint inbox --sprint <id>` once; handle and `accept` new items.
3. Run `wc -m < <path>`; keep the body near 6,000 characters and below 8,000.
4. As the literal final action, send:

```text
sc sprint send --sprint <id> --to <planner-shortname> --body-file <path> \
  --intent handoff --key <stable-merged-handoff-key>
```

5. Require the durable message + Planner wake, then stop immediately. Run no trailing Git,
   Sprint, inbox, cleanup, or status command. Automatic merge
   observation records the PR transition; this handoff releases the next wave.

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
red/green/closed/merged facts to Developers, never Planner.

Assignments, review requests, and verdicts use Force-new delivery;
Planner-bound results use Re-enter. Delivery waits for a natural boundary; runtime owns bundling,
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
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_rev',
  'Review Sprints v2 work and whole-Sprint conformance — own review, re-enter, abort, and conclude judgments, author the conformance and Sprint reports, and direct safety actions through durable messages.',
  'workflow',
  NULL,
  0,
  '# sprint_rev — independent review and conformance

Use for pre-declaration QAQC, one work-unit review, or whole-Sprint
conformance. The Reviewer decides review/conformance; the Planner owns
operational plan structure + control execution. FnB retains the board-level
override from decision #46.

Use the simplest path supported by current durable state. Treat independence,
authority, lifecycle preconditions, durable writes, and typed handoffs as hard
boundaries. Repeat a read only when later activity could have changed it or the
next command requires live revalidation.

## Route the entry

Classify the entry before reading an inbox:

| Entry | Route |
|---|---|
| Explicit pre-declaration request | Read/sign the exact current spec directly; there is no Sprint id or Sprint inbox to inspect yet. |
| Work-unit review / `sprint.delivery_terminal` | Inspect the Sprint inbox once; accept the actionable request. |
| Live FnB instruction | Preserve board-level authority; read only durable state needed for independent judgment. |

QAQC precedes all Sprint inbox commands:

```text
sc sprint record-qaqc --document <spec-document-id> \
  --verdict pass [--findings-document <document-id>]
```

For an armed Sprint, load `sprint_rev` on every entry:

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Decline actionable work only with a concrete reason. After handling an
informational message, run `accept`; it marks the message read and does not
change Sprint or work-unit state.

An unusable success receipt from idempotent bookkeeping does not stall the
Sprint. Retry the exact command once, then use its normal read surface once to
prove the exact postcondition. For informational `accept`, prior inbox presence
+ absence of that exact message id proves the read landed. Continue under that
proof + name the receipt defect in the next normal handoff. NEVER use this
recovery to infer assignment ownership, review outcome, merge authorization,
lifecycle/work-unit transition, governing revision, PR head/green state, or
cleanup authority. An unproved postcondition stops.

Review requests and verdicts use Force-new delivery. Planner decisions use
Re-enter. Delivery waits for a natural boundary; the runtime owns bundling,
rotation, and recovery. Stop after a successful typed handoff. Reviewers never
receive PR-event wakes.

## Relay contract and authority

Ask the Developer for unit evidence with a unit-scoped question/blocker:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent question --requires-reply --work-unit <work-unit-id> \
  --key <stable-key>
```

Use `--intent blocker` when the unit cannot advance. Cross-unit, closeout,
re-enter, abort, and safety rulings are Sprint-level decisions:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level --key <stable-key>
```

Reply through the original message; the server inherits its scope, so never
add `--work-unit` or `--sprint-level` to a reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent information --reply-to <message-id> --key <stable-key>
```

Confirm a reply, then `accept` its incoming message. Missing facts stop review
at the decision boundary; unread recovery re-wakes, so send no duplicate.
A stable key identifies recipient + exact body + intent + reply + scope. Reuse
it only for the same failed/ambiguous write; when any of those fields changes,
use a new key.

Keep bodies near 6,000 characters and below the 8,000 hard maximum; run
`wc -m < <path>`. A handoff completes only when the command exits successfully
and confirms the durable write + wake. If a command is rejected or transport fails,
the verdict/handoff is incomplete. Correct and retry safely. If the relay itself fails,
give FnB the attempted command, evidence, impact, and recommendation; invent no
alternate protocol.

## Sprint artifact paths

Keep review notes, diffs, evidence, reports, and scratch proof in gitignored
`shared/sprints/sprint-<n>/`; never commit, branch, or PR them. Durable records
belong in `record-review`, `sprint_reports`, and the relay.

## Conformance decisions and Planner controls

Reviewer owns review, re-enter, abort, and conclude judgments. Planner
independently owns operational plan structure: safe-edit pauses, recalling
unreleased work, lane changes/repeats, assignment/routing, unreleased-scope
cancellation, and validated resume. Reviewer never runs standalone pause,
replan, recall, reroute, cancel, resume, complete, or abort actions; clean
`record-conformance` alone performs its narrow atomic close.

Base judgment on durable Sprint state, bound revisions, current work/PR facts,
progress-carrier evidence, and ratified judgments. Every Reviewer→Planner route
is Re-enter. A decision body names:

- `decision`: `re-enter`, `abort`, or the exact safety-critical recommendation;
- Reviewer-owned evidence + rationale;
- exact Sprint/unit ids, reason, outcome, and complete action arguments;
- immediate safety impact for FnB.

Planner verifies Reviewer identity and executes the transition without
surrendering plan authority. A rejected action requires a revised judgment
supported by returned durable state, never an improvised bypass. A live FnB
instruction supersedes as distinct FnB board-level override authority under
decision #46.

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
severity does not decide timing: Reviewer judges whether each finding requires in-Sprint patching
or acceptable post-Sprint follow-up.

## Work-unit review

Accept the request and retain that exact message id. Its body is a bare locator:
intent, PR URL, registered PR id, exact head, work-unit id. Scope narrative,
verification, rationale, or focus steering is a protocol defect. PR comments
and annotations are forbidden; PR body contains only unit id + spec reference.

Bind inspection/verdict to the accepted request''s message id, registered PR,
and work unit. Review the live PR head; a rebase since the locator''s head is
not a defect. Read the exact spec revision + full diff, then checks, tests,
relevant runtime facts,
and ratified judgments. Each round is clean: no prior Developer evidence or
prose; prior findings clear only when the new head proves it. Trace code paths,
failure cases, and spec behavior rather than names or PR prose.

### Red-check doctrine

Accepted-red is not a legal review outcome. A departure that leaves checks
failing is never acceptable; the handoff remains green-only, without exception
or waiver: do not note the failure and approve anyway.

- In-scope failure -> record `changes_requested` so the Developer fixes them and restores green.
- Out-of-scope failure -> keep the lane unapproved and send the Planner a `replan`
  decision naming the failures; Planner widens the lane or cuts follow-up work.

Read cited and feature-scoped resolved flag evidence through memory, never SQL:

```text
sc mem get flags <flag-id>
sc mem get flags --feature <feature-id> --resolved
```

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
  --verdict changes_requested --body-file <path> --key <stable-key>
```

5. Require durable judgment evidence + Developer Force-new wake. Run no trailing command;
   stop.

Use `approved` only with no Critical/Major/Medium finding. Engine validation
requires the accepted request. Do not message around the
surface; an unrecorded verdict cannot unlock merge.

## Delivery-terminal closeout

Retain the exact notification message id + delivered wake as this closeout
episode''s identity. Proceed only when the notification names this shell as the
selected conformance owner for its current ownership generation. A different
Reviewer accepts the informational notification if received and records no
conformance. Inspect inbox, lifecycle, and units first:

- Already completed/aborted -> `accept` notification and stop.
- If any non-terminal unit is visible, the wake is stale -> `accept`, stop, and
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

- **In-Sprint patching required.** Do not run `record-conformance`. Send Planner
  a durable `re-enter` decision with every blocking finding; each spec task''s
  title and description; grouping, waves, dependencies, routing, and capacity
  rationale. State independent lanes, expected review overlap, useful reserve,
  and critical-path effect. After three re-entry episodes, escalate
  non-convergence to FnB.
- **Clean or post-Sprint-only findings.** Prepare conformance report, findings,
  final report, reason, and outcome; submit the atomic close below. Send no
  conclude message.

## Whole-Sprint conformance

Review the integrated system, not unit diffs. Classify every requirement
`as-specced`, `deviated-intentionally` with ratified judgment,
`deviated-silently`, or `unimplemented`; the last two are findings. Include
spec document + work-unit ids when known.

For the clean branch, write a conformance report and JSON findings array with
`severity`, `title`, `body`, `spec_document_id`, and `work_unit_id`. Keep the
report and each body near 6,000 and below 8,000 characters; run
`wc -m < <report>` and validate each body.

Before recording conformance, author the final Sprint report. Name Reviewer as
author and cover governing scope/revisions, shipped units/PRs, judgments +
ratified deviations, failures/retries/recovery/anomalies, conclusion,
follow-ups, and evidence location. Keep it near 6,000 and below 8,000; preserve
discrepancies.

Record one atomic final write:

```text
sc sprint record-conformance \
  --sprint <id> --body-file <report> --findings-file <json> \
  --final-report-file <final-report> --reason <reason> --outcome <outcome> \
  --key <stable-pass-key>
```

Require receipt: conformance report id, final report id, follow-up ids,
completed state, Planner message id, and Planner wake id. The transaction adds
append-only evidence, follow-ups, terminal state, and one informational engine-wide Planner Re-enter;
send no conclude message. Require cleanup projection `pending`; cleanup runs
after participant turns exit. Do not reset a worktree, poll cleanup, or wait
before stopping. The Planner receives the later engine-authored receipt.
Successful conformance also closes other Sprint-linked
chats while the originating Planner + report-authoring Reviewer stay open. Do
not manually close peer chats. Pause, abort, re-entry, failed conformance, and
rejected fallback retain no-cleanup behavior. Never reopen editing after recording; a re-enter defers
reports until new scope is terminal and a fresh delivery-terminal wake arrives.

## Stop

Unit review ends with the ordered `record-review` write as the literal final
action.

For closeout, first re-run `sc sprint inbox --sprint <id>`, handle + `accept`
new messages, then confirm every artifact/body is final and below 8,000.

- Clean conclude -> run the atomic `record-conformance` command above as the
  literal final action. When it confirms completed state, pending cleanup, and
  all receipt identities, stop immediately; Planner is notified.
- Re-enter/abort -> as literal final action send:

```text
sc sprint send --sprint <id> --to <planner-shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level \
  --key <stable-decision-handoff-key>
```

Require durable write + Planner wake, then stop immediately. Run no trailing
command until another native wake.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
