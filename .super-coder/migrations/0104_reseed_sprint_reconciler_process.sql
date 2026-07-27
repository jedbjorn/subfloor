-- 0104 — reseed sprint skills with the shipped reconciler and process findings.
-- Generated from assets/skills/*/SKILL.md; full-body UPSERTs keep existing
-- installations aligned with the source assets.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_dev',
  'Execute a developer unit in an ACTIVE structured sprint. Read the assigned unit, build within its scope, persist long work through sc job, open and register the PR, drive CI and review, merge the exact approved head under scoped authority, and send a structured unit report. Load when the boot sprint directive or a scoped task assigns a developer unit.',
  'craft',
  NULL,
  0,
  '# sprint_dev

Own the assigned unit from accepted task through merge.

## Activate from the record

The boot sprint directive names every active developer role. Read the scoped task
and board:

```sh
./sc mem message check
./sc sprint board --sprint <doc-id>
./sc mem get doc --doc <doc-id>
```

Confirm the unit, scope, dependency, reviewer, branch, and governing spec. The
board assigns ownership; the latest scoped task supplies current instructions.

Keep one current-state line while active:

```text
SPRINT doc=<id> unit=<seq> status=<pending|working|in_review|blocked>
```

Every transition and ruling request goes to the planner as a durable scoped
result:

```sh
./sc mem message send <planner> "$(<./sprint-result.md)" \
  --kind result --sprint <doc-id>
./sc mem message sent
```

Compose message and task bodies via a file, never inline in a quoted shell
string: backticks and `$()` execute before `sc` receives the body. Write the
body to `./sprint-result.md`, send its content as one argument, then use
`message sent` to read the stored row back and confirm its body.

File findings, flags, PRs, and reports within your assigned authority. A question
printed only in final output reaches no sprint actor.

Message IDs are scoped to their recipient. Treat an ID from another actor''s
inbox as provenance, never as a fetch instruction; the task row must quote the
substance you need. Ask the planner for that substance when it is absent.

For DB-assigned document, task, flag, and message IDs, use the ID returned by the
creating write. Confirm the target before an irreversible mutation. Refer to a
flag by `flag_id` plus its sprint-scoped label, such as
`#247 SC-S59-U8-ID-SPACES`. Establish absence through a complete direct read,
count, or exact-ID query. Before closing a flag by number, resolve and read back
its exact `flag_id`; display names and flag IDs share an integer range.

Validate every absence instrument against a known-positive target before
reporting an empty result as absence. Inspect its exit status and stderr; a
probe that cannot see the positive control leaves the claim unmeasured.

**Activation pass condition:** your reading of the task, board, and spec names
one executable unit and one observable completion condition.

## Make progress observable

The sprint reconciler compares the board''s live expectations with positive work
and result evidence. It reports confirmed divergences to the planner; it never
changes the board or supervises the worker.

Send a scoped partial before any turn ends with work unfinished. Name completed
work, evidence, and the next untouched action. Send a scoped result when the
work finds nothing; "nothing found" is evidence the worker ran. A qualifying
result closes the reconciler''s window for the current unit state.

Treat `read_at` in one direction only: READ proves something marked the row
read; UNREAD proves nothing about delivery, liveness, or work. Never infer a
fault or safe action from an unread marker.

Drain scoped messages immediately before every durable action you own, including
a push or merge. An earlier inbox check does not satisfy this gate.

## Resolve ambiguity before building

Test assumptions that can invalidate or dramatically simplify the unit. Send
the planner a ruling request when:

- the requested behavior has two materially different readings;
- the premise is false;
- a human-held credential or external action is required;
- the unit changes product meaning or another unit''s interface;
- the work cannot stay inside its recorded resource boundary.

Include alternatives, evidence, and downstream effect. Mark the unit `blocked`
through the planner until the ruling arrives.

## Prepare and build

Load `spec` when a governing feature spec requires tracked tasks. Load `git`
before branch work. Sync your base and create one feature branch for the unit.

Branch from current `main` when the unit is independently buildable. Stack on an
upstream branch only for a real code dependency and accept the later retarget
and rebase duty.

Implement the smallest complete change. Verify in proportion to risk. Keep scope
changes as planner rulings or follow-up flags.

Give each high-value test method one property. When one setup must exercise
several properties, use `subTest` so an early assertion cannot mask the later
detectors.

Run session-surviving local suites, builds, and benches through:

```sh
./sc job start --label <slug> --timeout <seconds> -- <command> <args>
./sc job status <job-id>
./sc job tail <job-id>
./sc job wait --for <seconds> <job-id>
```

The job completion row is the durable transition. Use CI-to-CI measurements for
merge-gating performance claims.

## Open and register the PR

When dependencies are on `main` and the planner has released the unit:

1. Fetch and rebase onto `origin/main`.
2. Run the unit''s verification gate.
3. Push and open the PR.
4. Register the planner''s sprint watch.
5. Report the exact PR and head.

```sh
./sc watch pr <owner/repo> <pr-number> \
  --shell <planner> --sprint <doc-id>
./sc mem message send <planner> "$(<./sprint-result.md)" \
  --kind result --sprint <doc-id>
./sc mem message sent
```

Update the board''s branch and PR through the planner. An unregistered PR has no
event path back to orchestration.

If a PR is unexpectedly in draft, ask the planner before marking it ready. This
is a caution check, not a hold mechanism.

## Drive CI

While live, use `gh pr checks <pr> --watch`. If the session ends, the registered
watch delivers later state to the planner.

Classify red checks:

- diff-caused: fix, verify, push, and report;
- runner, network, or known flake: rerun the failed job;
- failing independently on `main`: report evidence and block;
- uncertain after three honest fixes: report attempts and block.

One isolated rerun may distinguish a race from a diff-caused failure. Keep every
known flake or environmental exclusion enumerable as a skip or quarantine; a
known-flake list with no count is not a gate. Two repeated anomalous failures
escalate to the planner. Green means required checks completed successfully on
the merge head.

## Pass sprint review

After CI is green, send the planner an `in-review` result naming the PR and exact
head. The planner sends and boots the reviewer.

The planner returns Major and Medium findings as a scoped fix task. Fix them,
keep CI green, and report the new exact head for another review route. Low
findings enter follow-ups.

When a review ruling changes the file surface, report the new surface with the
ruling before continuing. The planner must re-send the overlap notice to every
affected concurrent sprint.

An explicit `review-clean` result at a known head is required for merge.

If `main` moves after review:

- compare intervening changes with this unit''s files;
- preserve the verdict when surfaces are disjoint;
- rebase when surfaces overlap;
- compare the unit''s contribution before and after rebase;
- return to the reviewer when the contribution changes or a hunk was
  hand-resolved.

Report the evidence and final head to the planner.

## Merge and report

Merge only when:

- the sprint document is ACTIVE and unfrozen;
- the board still assigns this unit to you;
- required checks are green on the merge head;
- the planner relayed a reviewer-clean verdict for that head or an explicit
  carry-through across a disjoint rebase;
- the planner''s merge order releases the unit.

Merge with the repository''s `git` procedure, then send one structured result:

```text
unit-report <doc-id> unit=<seq> pr=#<n>
shipped: <observable behavior>
judgements: <calls and final rulings, or none>
issues: <CI, stalls, review friction, or none>
deviations: <known departures and disposition, or none>
follow-ups: <Low findings and deferred work, or none>
```

Send it:

```sh
./sc mem message send <planner> "$(<./sprint-result.md)" \
  --kind result --sprint <doc-id>
./sc mem message sent
```

Clean the local branch according to `git`. Remove the sprint current-state line
when the unit reaches a terminal board state.

**Developer completion:** the recorded PR is merged at an approved green head,
the planner has the complete unit report, and the worktree is clean.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_orchestration',
  'Run the steady-state planner loop for a multi-shell sprint. Declare the sprint and structured unit board, assign developers and reviewers, arm event-driven wake, dispatch scoped tasks, advance units from durable events, and route merge order. Load sprint_orchestration_recover only when an expected transition stalls or an alert opens. Load sprint_orchestration_close only when every unit is terminal and main is green.',
  'craft',
  NULL,
  0,
  '# sprint_orchestration

Coordinate the sprint from durable records. Keep code context in worker shells
and coordination context here.

Use three procedures:

- This skill: declare, dispatch, monitor, and advance the normal path.
- `sprint_orchestration_recover`: diagnose and repair a stalled transition.
- `sprint_orchestration_close`: run conformance, revoke authority, and report.

Load a chain skill only when its trigger is true.

## 1. Establish the boundary

Confirm the objective, governing spec or decisions, and success condition with
the FnB. Ask two routing questions:

1. Which harness and model should every developer use?
2. Which harness and model should every reviewer use?

Treat account configuration and billing limits as operator-managed inputs. Use
the selected routes.

Read active decisions before choosing architecture or sequencing:

```sh
./sc mem get decisions
./sc mem get decisions <id>
```

For concurrent sprints, partition hard resources before dispatch:

- shells;
- branches and worktrees;
- migrations and other globally ordered identifiers;
- files with substantial overlap;
- external environments or exclusive services.

Give each sprint a distinct label and record every reservation. Read back
globally allocated IDs after creation; a planned number is provisional until
the authoritative store confirms it.

Use `S<doc-id>-U<seq>` in messages, flags, and reports. Unit labels are unique
only inside one sprint.

Keep the declared shell sets exclusive. Transfer a shell through an explicit
planner-to-planner handoff recorded on both sprint documents before booting it.

**Pass condition:** the sprint has one objective, one authority source, resolved
routes, and no silently shared hard resource.

## 2. Cut units and merge surfaces

Define units that one developer can own through merge. Add dependency edges
only for real code dependencies.

Predict each unit''s file surface and compare intersections:

- empty intersection: parallel;
- shared files: sequence the units or record an explicit overlap protocol;
- one file shared by three or more units: reconsider the cut.

Assign one developer and one reviewer to every unit. Balance reviewer load
against the dependency graph.

Reserve the close-time conformance reviewer now, while reviewer independence is
still available. Record that shell''s conflict set: units authored, unit reviews
performed, rulings supplied, and any other reason it would grade its own work.
Pass only with a named reviewer whose recorded set is disjoint from conformance
scope, or an explicit FnB disposition for every unavoidable conflict.

Define the merge rule at kickoff:

- A verdict is bound to the reviewed head SHA.
- Unrelated movement on `main` preserves the verdict.
- Overlapping movement requires a rebase and an interference check.
- A changed contribution or hand-resolved hunk returns to the reviewer.

**Pass condition:** every unit has an owner, reviewer, dependency list, predicted
surface, and deterministic route to merge.

## 3. Declare the sprint

Create one unfrozen `documents` row titled `SPRINT: <title>`, kind `doc`. Keep
the body concise:

```markdown
# SPRINT: <title>
status: ACTIVE
declared: <date> · planner: <shortname>
models: devs=<harness>/<model> · reviewers=<harness>/<model>
spec: <doc id or decision ids>
success: <observable outcome>
resource reservations: <migration ranges, shells, environments>
```

The structured board lives in `sprint_units`; render it with:

```sh
./sc sprint board --sprint <doc-id>
```

Create its rows:

```sh
./sc sprint unit add --sprint <doc-id> --seq U1 --title "<unit>" \
  --dev <shortname> --reviewer <shortname> \
  --depends-on <U0|none> --overlap "<protocol|none>"
```

Use `unit set` for assignments, branch, PR, dependencies, and overlap. Use
`unit state` as the sole state writer:

```sh
./sc sprint unit set --sprint <doc-id> --seq U1 --branch feat/example --pr 123
./sc sprint unit state --sprint <doc-id> --seq U1 working
```

States are `pending`, `working`, `in_review`, `blocked`, `merged`, and
`cancelled`.

**Pass condition:** `./sc sprint board --sprint <doc-id>` reproduces the complete
assignment and sequencing plan without reading prose.

## 4. Arm event-driven wake

A confirmation that is not about the thing it appears to confirm is not a
confirmation. Verify the durable artifact that governs the next action.

Arm the planner binding from the Interface Sprint wake panel or
`POST /api/interface/sprint-bindings` with:

```http
Idempotency-Key: sprint-bind-<doc>-<planner>-<attempt>

{"sprint_doc_id": <doc-id>, "planner_shell_id": <planner-shell-id>}
```

Generate one caller-stable attempt value and reuse it for transport retries of
that attempt. Generate a fresh value for a later re-arm.

Verify:

```sh
./sc sprint status
./sc sprint alerts
```

The binding must report armed with no critical alert before the first worker
boot.

PR events and scoped task/result rows drive the loop. Avoid scheduled trackers
and session-bound inbox waiters.

When the sprint pauses, release its binding from the Interface Sprint wake panel
or `DELETE /api/interface/sprint-bindings/<id>` with a caller-stable
`Idempotency-Key` and `reason: pause`. Read `./sc sprint status --all` back and
stop only when the binding reports released or disarmed. Plain status hides
released bindings and cannot prove release. A paused sprint may remain ACTIVE
and unfrozen; it must not remain armed.

## 5. Dispatch ready work

Every sprint `task` and `result` row carries `--sprint <doc-id>`. This makes the
event wake-eligible and keeps parallel sprints separable.

Compose message and task bodies via a file, never inline in a quoted shell
string: backticks and `$()` execute before `sc` receives the body. Write the
body to `./sprint-result.md`, send its content as one argument, then use
`message sent` to read the stored row back and confirm its body.

Send exact assignments:

```sh
./sc mem message send <dev> "$(<./sprint-result.md)" \
  --kind task --sprint <doc-id>
./sc mem message sent
```

Quote every fact the worker needs in its own task row. A message ID addressed to
another shell is provenance, never a fetch instruction; inbox IDs are
recipient-scoped.

A message you sent is not an instruction the recipient has received. Treat
delivery and receipt as separate facts; do not infer receipt from the send
result or from an unread row.

The board reserves each reviewer. Dispatch the reviewer when a developer reports
a green exact head:

```sh
./sc mem message send <reviewer> "$(<./sprint-result.md)" \
  --kind task --sprint <doc-id>
./sc mem message sent
```

Boot only the actor that owns the next transition:

```sh
./sc run <dev> --harness <harness> -m <model> --effort high
```

Run `./sc run` from a durable interactive planner process. It executes the
harness over its own process, so a timeout, bounded background task, or exiting
wrapper becomes the worker''s lifetime. If a wrapper already owns a live harness,
load `sprint_orchestration_recover` and unwrap it there.

Dispatch passes when the harness PID is non-zombie at the assigned worktree and
no temporary launcher bounds its lifetime.

Treat the board as assignment truth and the task row as the current instruction.
Update both before a reassignment or scope change.

**Pass condition:** every ready unit is `working`, every waiting unit is
`pending`, and the producer of each expected transition has one scoped task
naming the same unit record.

## 6. Advance from events

On each wake:

1. Drain inbox rows.
2. Read `./sc sprint board --sprint <doc-id>`.
3. Read `./sc sprint status` and `./sc sprint alerts`.
4. Apply the smallest state transition supported by the event.
5. Dispatch newly ready work.

The reconciler runs independently of PR watches and returns a report-only
reading for every live sprint expectation. Each reading carries `expectation`,
`signal`, `confirmed`, `evidence`, `measurement`, `observed_at`, and
`explanation`; classification never writes. After confirmation, every
actionable signal produces one `planner_alerts` row keyed
`(sprint_doc_id, unit_id, role, signal)`; `shell_id` stays off-key. A planner
message is only the push layer and is emitted only when `board_writer` resolves
a recipient. Open alerts dedupe, resolve when evidence returns, and re-arm after
resolution.

Read `sc watch list` for the reconciler''s own heartbeat. A stale `reconcile`
line means the watchdog may be wedged; no alerts from a stale watchdog do not
prove a quiet sprint.

Treat the explanation tier as WHY, never WHETHER: transcript mtime, the raw
argv-derived `launch_shape` plus `cpu_delta`, persisted quota exhaustion with
`resets_at`, and process-local provider fault with its probe timestamp may
explain a reading but never change its classification.

Use this routing table:

| Event | Planner action |
|---|---|
| Developer starts | Confirm `working`. |
| PR opens | Record branch and PR; confirm its sprint watch exists. |
| Developer reports green review-ready head | Move unit to `in_review`; send the assigned reviewer an exact-head task and boot it. |
| Reviewer reports Major/Medium | Send a scoped fix task to the developer; keep `in_review`. |
| Reviewer reports clean + checks green | Send the developer a scoped merge instruction for the reviewed head. |
| PR merged + unit report | Move unit to `merged`; dispatch newly unblocked units. |
| Declared blocker | Move unit to `blocked`; rule on mechanics or escalate judgment. |
| Reconciler checkup or critical wake alert | Treat it as a request to verify, then load `sprint_orchestration_recover`. |
| All units terminal and `main` green | Load `sprint_orchestration_close`. |

Register each PR with the planner at PR open:

```sh
./sc watch pr <owner/repo> <number> --shell <planner> --sprint <doc-id>
```

Monitor before interrupting a worker. Send a new task when behavior must change,
not to request generic progress.

Never use a draft PR as a planner hold. A state the worker''s own procedure
teaches it to clear cannot carry a stop; record the hold in board state plus a
scoped task. No reliable interrupt exists today, and flag #321 owns that gap.
A stop signal indistinguishable from an obstacle is an instruction to proceed;
this rule does not close flag #321.

When a review ruling changes a unit''s file surface, update the overlap record
and re-send the surface notice to every affected concurrent planner before the
next durable action. The ruling that widens the surface triggers the notice.

At each merge, resolve the merge base and compare the executed file surface with
every concurrent sprint. Send surface deviations to the affected planner.
Declarations record intent; merged files are the overlap evidence.

## 7. Preserve the authority boundary

An ACTIVE, unfrozen sprint grants:

- assigned developers: merge their unit after green checks and explicit
  review-clean;
- assigned reviewers: file findings and return a recommendation to the planner.

That authority is scoped to the recorded sprint and assigned unit. The close
procedure freezes the document and ends it.

Escalate changes to product meaning, scope, interface contracts, or human-only
inputs to the FnB. Resolve sequencing, reassignment, retries, CI triage, and
worker boots within the declared boundary.

## Steady-state completion

This procedure is complete when either:

- the next expected unit transition is dispatched and the binding is healthy;
- a diagnosed stall triggers `sprint_orchestration_recover`; or
- every unit is terminal with green `main`, triggering
  `sprint_orchestration_close`.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_orchestration_close',
  'Close a sprint after every unit is terminal and main is green. Run an independent spec-to-main conformance pass, route findings while authority remains active, close and freeze the sprint document, verify wake and PR-watch cleanup, synthesize the report, and settle flags and roadmap state. Load only at the close trigger.',
  'craft',
  NULL,
  0,
  '# sprint_orchestration_close

Close from integrated evidence. Keep the sprint ACTIVE until conformance
findings are ruled.

## Confirm the close trigger

Read:

```sh
./sc sprint board --sprint <doc-id>
./sc sprint status
./sc watch list --all
```

Confirm:

- every unit is `merged` or `cancelled`;
- every merged unit has a structured unit report;
- every assigned review ended `review-clean` at a known head;
- all required checks on `main` are green;
- the sprint document remains unfrozen.

Confirm the conformance reviewer and its conflict set were declared at sprint
setup. The set names units authored, unit reviews performed, rulings supplied,
and any other overlap with conformance scope. Do not choose a reviewer at the
freeze gate after every available shell has reviewed its own evidence; return to
orchestration until an independent reviewer is reserved or the FnB explicitly
disposes each conflict.

If any condition fails, return to `sprint_orchestration` or
`sprint_orchestration_recover`.

## Run conformance

Assign an independent reviewer to compare the governing spec with integrated
`main` at one recorded SHA.

Compose message and task bodies via a file, never inline in a quoted shell
string: backticks and `$()` execute before `sc` receives the body. Write the
body to `./sprint-result.md`, send its content as one argument, then use
`message sent` to read the stored row back and confirm its body.

Send:

```sh
./sc mem message send <reviewer> "$(<./sprint-result.md)" \
  --kind task --sprint <doc-id>
./sc mem message sent
./sc run <reviewer> --harness <review-harness> -m <review-model> --effort high
```

State the sections and units included. For decision-driven units without a spec,
name the alternate evidence: unit report, exact-head review, and mutation check.

Ask the tense question across the whole spec at the freeze SHA. Re-verify every
present-tense code claim, beginning with the problem statement and corrections.
Rewrite historical claims in past tense with their ref, and qualify current
provenance with the exact SHA it verifies.

The reviewer writes `CONFORMANCE: <sprint title>` with one verdict per
requirement:

- `as-specced`;
- `deviated-intentionally`;
- `deviated-silently`;
- `unimplemented`.

Route findings while the sprint is ACTIVE:

- Major: add a fix unit and rerun affected conformance.
- Medium: add a fix unit or record an explicit FnB-approved deferral.
- Low: add an actionable follow-up.

**Conformance pass condition:** every scoped requirement has a verdict and every
finding has a disposition.

## Revoke sprint authority

Drain the scoped inbox immediately before editing or freezing the sprint
document. An earlier inbox check does not satisfy this gate.

Edit the sprint document body to `status: CLOSED`, preserving its declaration
fields, then freeze:

```sh
./sc mem doc edit <doc-id> --body-file <closed-sprint-body>
./sc mem doc freeze <doc-id>
```

Freezing ends sprint merge and direct-handoff authority. Verify:

```sh
./sc sprint status --all
./sc watch list --all
```

Every binding for the sprint must be released and every sprint PR watch retired.
Resolve any surviving watch by identifying its open or mis-scoped PR.

Send participants an ordinary `shell` message announcing closure and restoration
of default gates. Scoped task/result messages require an ACTIVE sprint, so closure
notices are intentionally unscoped.

## Write the sprint report

Create one `SPRINT REPORT: <title>` document. Synthesize the structured board,
unit reports, event trail, review verdicts, and conformance document into:

```markdown
## Verdict
## Units Shipped
## Judgements Made
## Spec Accuracy
## Issues Encountered
## Deferred & Follow-ups
## Spec Debt
## Metrics
```

Use `Verdict` for the five-second answer: unit and PR count, conformance state,
green-main state, cancellations, and explicit deferrals. Cross-check unit
`deviations` against conformance verdicts and state the resulting synthesis.

Persist the report:

```sh
./sc mem doc add "SPRINT REPORT: <title>" --kind doc --body-file <report.md>
```

Add a shared copy only when the fork''s artifact policy or FnB requests one.

## Settle bookkeeping

- Before closing a flag by number, resolve and read back its exact `flag_id`.
  Display names and flag IDs share an integer range; never infer one from the
  other.
- Close flags resolved by the sprint with how-they-were-resolved notes.
- Advance the linked roadmap feature to its earned state.
- Open flags for actionable deferred work.
- Record spec debt where the implementation exposed a missing or wrong rule.
- Give each deferral an owner and an observation that will surface it again:
  query, metric, marker, failing test, or alert. Record an unmeasurable
  acceptance as a decision with rationale.
- Tell the FnB the sprint doc id, report doc id, conformance verdict, and
  remaining follow-ups.

**Close completion:** the sprint doc is frozen, bindings are released, watches
are retired, the report exists, and every finding or follow-up has an owner or
explicit disposition.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_orchestration_recover',
  'Recover a declared sprint after a critical wake alert or a missing expected transition. Diagnose from the board, binding, messages, jobs, PR state, and worker process evidence; preserve completed work; apply one bounded repair; return to sprint_orchestration. Load only after the steady-state loop identifies a stall.',
  'craft',
  NULL,
  0,
  '# sprint_orchestration_recover

Recover one stalled transition without replaying completed work.

## Establish the last durable truth

Read:

```sh
./sc sprint board --sprint <doc-id>
./sc sprint status
./sc sprint alerts
./sc mem message check
./sc job list --all
./sc watch list --all
```

For the affected unit, confirm:

- last scoped task and result;
- board state, branch, PR, developer, and reviewer;
- live process or job evidence;
- PR head, checks, review state, and merge state;
- binding batch, park, quarantine, and alert state.

Quiet is a symptom. Declare a stall only when an expected transition is absent
and positive evidence shows no active process, job, review, CI run, or queued
wake that can produce it.

Validate every absence instrument against a known-positive target before using
its empty output. A missing command, rejected predicate, non-zero probe, or
positive control it cannot see makes the claim unmeasured, not absent. Inspect
the probe''s exit status and stderr; never translate empty stdout alone into
"nothing is running."

A reconciler finding requests a checkup. For a headless shell:

1. Resolve the assigned subject from the sprint board, scoped task, launching
   planner, and worktree.
2. Find the harness process whose `/proc/<pid>/cwd` is that worktree.
3. Read `/proc/<pid>/stat` and reject state `Z`; a zombie directory and stat
   file are not liveness.
4. Sample `/proc/<pid>/stat` twice over a bounded interval and compare
   `utime + stime`.
5. Treat a positive CPU delta as active work.
6. Treat process presence with no delta as indeterminate until the task,
   artifacts, and another bounded sample establish progress or fault.
7. Treat no matching non-zombie process, combined with no live job or producing
   external operation, as positive stop evidence.

Interface availability, a missing Interface session row, and an open archive
describe session bookkeeping. Use them as context. `/proc` proves process
activity; the board and scoped task prove which sprint and unit that activity
belongs to.

Use reconciler explanations only to explain the reading. Transcript mtime, raw
`launch_shape` plus `cpu_delta`, persisted quota exhaustion with `resets_at`,
and process-local provider fault with its probe timestamp never decide whether
the worker is active.

**Diagnosis pass condition:** name the missing transition and the evidence that
its producer is inactive or unable to deliver it.

## Apply one recovery

| Diagnosis | Recovery |
|---|---|
| Worker stopped with a clean worktree and no artifact | Re-send the exact scoped task and boot the assigned shell. |
| Worker stopped with a branch, commit, PR, report, or dirty worktree | Preserve the owner and artifact. Send an idempotent continuation task naming the durable state, then boot that shell. |
| Worker is active | Leave it running. Record the observed process and wait for its bounded completion event. |
| Session input parked with delivery unknown | Decide whether input landed, then run `./sc sprint retry --binding <id> --outcome delivered\|not_delivered`. |
| Wake batch parked before delivery | Run `./sc sprint retry --binding <id>`; the coordinator creates a new gated batch. |
| Item quarantined | Read and act on that message manually, then send a fresh scoped task for any remaining action. |
| Reviewer unavailable | Reassign the board to an idle reviewer, send the exact-head request, and boot the new reviewer. |
| Developer unavailable before work starts | Reassign the board and task the replacement. |
| Developer unavailable after durable work exists | Hand the branch and exact head to a replacement; preserve authorship and report the handoff. |
| Real CI failure | Return a scoped fix task to the developer. |
| Runner, network, or known-flake failure | Rerun the failed job; escalate after two anomalous failures. |
| `main` is red independently | Hold dependent merges and add or assign a repair unit. |
| Provider auth, quota, or service failure | Surface the external condition to the FnB. Resume or reroute only after the operator changes the route. |
| Assignment or scope conflicts across parallel sprints | Stop the affected unit, resolve the hard-resource owner, update both boards, then resume one owner. |
| Product meaning or scope is ambiguous | Ask the FnB with the alternatives and downstream effect. |

If a timeout, bounded background task, or exiting shell wrapped `./sc run`, the
wrapper owns the worker''s lifetime because `./sc run` executes the harness over
its own process. When the harness is still healthy, send SIGKILL to the wrapper
PID alone, never its process group. Recovery passes only when the harness PID
remains non-zombie and its parent becomes PID 1; otherwise preserve artifacts
and use the applicable stopped-worker row above.

Every continuation, reassignment, fix request, and recovery result uses
`--sprint <doc-id>`.

Compose message and task bodies via a file, never inline in a quoted shell
string: backticks and `$()` execute before `sc` receives the body. Write the
body to `./sprint-result.md`, send its content as one argument, then use
`message sent` to read the stored row back and confirm its body.

Record assignment changes before boot:

```sh
./sc sprint unit set --sprint <doc-id> --seq <unit> --dev <dev> --reviewer <rev>
./sc mem message send <worker> "$(<./sprint-result.md)" \
  --kind task --sprint <doc-id>
./sc mem message sent
```

Use `blocked` while the unit has no active path:

```sh
./sc sprint unit state --sprint <doc-id> --seq <unit> blocked
```

Move it back to `working` or `in_review` only after the replacement action is
durable.

## Engine-source recovery

When the sprint changes the engine that is running it, load `engine_surgery`.
Distinguish:

- feature worktree source;
- `origin/main`, which proves merged engine code;
- the main checkout and live DB, which define the running floor.

Defer pull, reconcile, migration of the live DB, and restart to a declared sprint
boundary. A feature worktree being current does not update the running floor.

## Return to orchestration

Read the board and wake status again. Recovery succeeds when:

- the board names one active owner;
- the next transition has one producer;
- that producer has a durable scoped task or live job;
- the binding has no unresolved critical alert that blocks delivery; and
- completed artifacts were not duplicated or discarded.

Send a concise recovery result, then return to `sprint_orchestration`.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_review',
  'Execute an assigned sprint unit review or the close-time conformance pass. Review the requested exact head, test high-value properties adversarially, return Major/Medium/Low findings and a recommendation to the planner, declare review-clean explicitly, or write requirement-level conformance verdicts against integrated main. Load when the boot sprint directive or a scoped task assigns review work.',
  'craft',
  NULL,
  0,
  '# sprint_review

Use the base `review` discipline with the sprint''s exact-head and
planner-routed handoff contract.

## Activate from the record

Read the scoped task and board:

```sh
./sc mem message check
./sc sprint board --sprint <doc-id>
./sc mem get doc --doc <doc-id>
```

Confirm the assigned unit or conformance scope. Keep:

```text
SPRINT doc=<id> reviewing=<unit or conformance>
```

Every review transition uses a durable scoped result:

```sh
./sc mem message send <planner> "$(<./sprint-result.md)" \
  --kind result --sprint <doc-id>
./sc mem message sent
```

Compose message and task bodies via a file, never inline in a quoted shell
string: backticks and `$()` execute before `sc` receives the body. Write the
body to `./sprint-result.md`, send its content as one argument, then use
`message sent` to read the stored row back and confirm its body.

File findings and verdicts within the assignment. Send ruling requests to the
planner with alternatives and evidence.

Message IDs are scoped to their recipient. Treat an ID from another actor''s
inbox as provenance, never as a fetch instruction; the task row must quote the
substance you need. Ask the planner for that substance when it is absent.

Use IDs returned by creating writes for documents, tasks, flags, and messages.
Confirm the target before an irreversible mutation. Refer to flags by `flag_id`
plus a sprint-scoped label. Establish absence through a complete direct read,
count, or exact-ID query. Before closing a flag by number, resolve and read back
its exact `flag_id`; display names and flag IDs share an integer range.

Validate every absence instrument against a known-positive target before
reporting an empty result as absence. Inspect its exit status and stderr; a
probe that cannot see the positive control leaves the claim unmeasured.

The sprint reconciler compares the board''s live expectations with positive work
and result evidence. It reports confirmed divergences to the planner; it never
changes the board or supervises the reviewer. Send a scoped partial before a
turn ends with review unfinished, naming completed checks, evidence, and the
next untouched action. "Nothing found" becomes the explicit clean verdict.

Treat `read_at` in one direction only: READ proves something marked the row
read; UNREAD proves nothing about delivery, liveness, or work. Never infer a
fault or safe action from an unread marker.

## Review a unit

### Pin the review target

Confirm the requested PR and exact head SHA before reading the diff. Confirm CI
belongs to that head.

If `main` advanced, compare intervening changes with the PR''s files:

- disjoint surface: review the requested head;
- overlapping surface: request a rebase before review.

For a superseded, force-pushed, or unverified head, send a scoped correction
request and wait for a pinned target.

Drain scoped messages immediately before every durable action you own, including
a verdict, pushed review artifact, or conformance document write. An earlier
inbox check does not satisfy this gate.

### Review adversarially

Trace behavior against:

- unit scope and governing spec;
- correctness and error handling;
- empty, boundary, concurrent, partial-failure, and permission states;
- repository conventions and avoidable complexity;
- tests that constrain the new behavior.

For a high-value property, mutate the implementation or condition, prove the
relevant test fails, restore the source, and prove it passes. Record the
property tested and result. Use a narrowed interference review after a rebase
or hand-resolved hunk.

Before reporting a mutation red set or empty set, validate the test-selection
and result-counting instrument against a known-positive mutation. A selector
that cannot detect its control leaves the set unmeasured.

Require one property per test method, or `subTest` boundaries when setup must be
shared. Sequential assertions for independent properties are not independent
detectors: an early failure masks every later one.

### Classify and hand off

- Major: wrong behavior, security/data risk, or material spec violation.
- Medium: likely production defect or incomplete required path.
- Low: non-blocking clarity, cleanup, or improvement.

Under an ACTIVE sprint document the planner holds the FnB''s delegated approval
for your sprint-scoped sends to the planner. Sending them SATISFIES the
outbound-handoff approval gate — whether that gate reaches you from the base
`review` skill or from your own system prompt — rather than bypassing it. The
gate reverts to the FnB when the sprint document freezes. Each verdict includes
location, consequence, required behavior, and severity. The planner routes fix
work or merge authority to the developer.

On a clean pass, explicitly send the planner:

```text
U1 review-clean head=<sha>; mutation=<property/result>; findings=0
```

Use `--kind result --sprint <doc-id>`. A clean verdict applies only to the named
head and any later head whose contribution is proven unchanged across disjoint
interference.

**Unit-review completion:** the planner holds an explicit recommendation for the
review head, with every blocking finding named.

## Run close-time conformance

Use this procedure when the task says `Conformance`.

The sprint-scoped send approval rule in **Classify and hand off** applies here.

Read the governing spec and integrated code on `main` at the supplied SHA.
Treat ratified deviations from the task as the complete intentional-deviation
list. Judge the integrated result, not PR diffs or developer narratives.

Give every requirement in scope exactly one verdict:

- `as-specced`: implementation matches the spec;
- `deviated-intentionally`: implementation matches a ratified deviation;
- `deviated-silently`: implementation departs without a ratified decision;
- `unimplemented`: no integrated implementation satisfies it.

For `deviated-silently` and `unimplemented`, attach:

- spec section;
- code location or confirmed absence;
- Major, Medium, or Low severity;
- observable consequence.

At the supplied main SHA, re-check every present-tense code claim in the whole
spec, beginning with the problem statement and correction sections. A
provenance line carries the ref and SHA it actually verified. Mark historical
claims as historical.

Create a document titled `CONFORMANCE: <sprint title>`, kind `doc`, containing:

```markdown
## Scope
## Main SHA
## Requirement Verdicts
## Findings
## Evidence
```

Persist it with `./sc mem doc add`, then send one pointer:

```sh
./sc mem message send <planner> "$(<./sprint-result.md)" \
  --kind result --sprint <sprint-doc-id>
./sc mem message sent
```

The planner owns finding disposition and close authority.

**Conformance completion:** every scoped requirement has one verdict, every gap
has evidence and severity, and the planner has the document pointer.

## Stand down

Remove the sprint current-state line when the unit becomes terminal or the
planner confirms receipt of conformance. A frozen sprint document ends
sprint-scoped review authority and restores the base review gate.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
