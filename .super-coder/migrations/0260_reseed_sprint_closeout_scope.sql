-- 0260 — reseed the guidance for the scoped Sprint close. A successful close
-- now ends only the Developer participants' chats and deletes the Sprint
-- artifact directory; the Planner and every Reviewer keep their chats, no
-- participant worktree is reset, and no prior-Sprint cleanup gates arming.
-- No schema change: a full-body UPSERT converges upgraded installations on the
-- same text a fresh seed produces. Idempotent.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'git',
  'Git events for a Subfloor shell — GitHub capability recovery, merging a stack the FnB hands you, after-merge cleanup, and what never enters Git. The every-session rules (sync, branch, PR, merge gate, finish) live in your boot.',
  'substrate',
  NULL,
  0,
  '# git — the event procedures

Your boot''s VERSION CONTROL section carries the every-session rules: sync the
base, branch before you build, commit → push → PR → stop, the merge gate''s two
forms, the disposable `shell/<shortname>` base, and the finish gate. This skill
holds what fires on an event.

One repo at its root -> plain `git` (cwd = repo root) is safe. Project = this
repo minus `.super-coder/`. In a tracking fork the engine (`.super-coder/`) is
gitignored, materialized by `sc update`, and authored upstream in Subfloor —
NEVER commit or edit anything under it. In the Subfloor source repository
(`git ls-files --error-unmatch .super-coder/schema.sql` exits 0) `.super-coder/`
is tracked project source and `.sc-state/engine.ref` is not the delivery unit;
engine changes still land by branch and PR.

## GitHub capability boundary

`sc launch` and `sc restart` re-resolve Git transport and GitHub API
capabilities from the host on every invocation, including `--no-build` forms.
`build`, `enter`, and an already running sandbox do not refresh auth. Pass = the
lifecycle summary says `ready` for the operation you need; `unavailable` and
`unverified` are NEVER readiness claims.

Preserve the configured `origin` transport. For SSH, fix the host agent and
load an authorized GitHub identity; NEVER copy or mount private keys. For
HTTPS/API, fix a scoped host `SC_GH_TOKEN` or the host `gh` OAuth login. Then
run `sc launch` or `sc restart`; the running sandbox remains unchanged
until that refresh. NEVER rewrite the remote or start an interactive login
inside the sandbox to work around a missing capability.

## Merging a stack (only when the FnB hands you one)

Merge bottom-up, retargeting before each merge — never rely on GitHub''s auto-retarget:

1. `gh pr view <n> --json mergeable,mergeStateStatus` -> clean.
2. `gh pr merge <low> --squash --delete-branch`.
3. BEFORE the next merge: `gh pr edit <next> --base main` — deleting the merged base otherwise orphans the PR above it (GitHub closes it `CONFLICTING`, base ref gone).
4. Re-check `MERGEABLE` -> merge. Repeat up the stack.

PR already orphaned (base deleted under it) -> the head branch still holds the commits; reopen the SAME PR, don''t rebuild:

1. `git push origin <merged-sha>:refs/heads/<deleted-branch>` — `<merged-sha>` = `gh pr view <merged-pr> --json headRefOid`.
2. `gh pr reopen <closed-pr>` -> `gh pr edit <closed-pr> --base main`.
3. Verify `MERGEABLE` -> delete the recreated branch again.

## After a merge — clean up local

Only after the PR is merged:

1. Re-pin the base. In a worktree `git checkout main` fails (main is checked out at the repo root; git refuses a branch checked out elsewhere) -> `git checkout shell/<shortname> && git fetch origin && git reset --hard origin/main`. Admin at repo root: `git pull --ff-only` on main.
2. `git branch -d <branch>`. Squash-merged -> `-d` refuses (commits aren''t ancestors of main); confirm the PR shows *merged* on the remote -> `git branch -D <branch>`.
3. `git fetch --prune`.

NEVER delete a branch carrying unmerged, un-PR''d work — no PR = lost work.

## Never commit the engine or derived files

- In a fork `/.super-coder/` is gitignored — never force-add anything under it.
- Gitignored + regenerated, never commit: `CLAUDE.md`, `AGENTS.md`, `opencode.json`, `.claude/skills/`, `.sc-state/engine.ref.prev` (ephemeral rollback pointer).
- From a worktree, commit only your project''s authored files. Generated
  snapshots and `_sc` renders live under ignored `.sc-state/local/` and never
  enter Git. `.sc-state/engine.ref` is the deliberate tracked exception: it is
  the dependency pin and is updated by `sc update`.

## Notes

- Before destructive ops, confirm the repo — `git -C <abs-path>` if ever in doubt.
- Multi-shell: each shell boots into its own worktree at `.sc-worktrees/<shortname>/` on branch `shell/<shortname>`; the launcher keeps the base pinned to `origin/main`. Admin shell = the one exception: repo root on `main`, committing there by mandate.
- UI preview: worktree edits do NOT show on the fork''s main dev server. `sc preview` (start once from the main checkout if not running) serves every shell''s worktree UI live (HMR) on the fork''s `dev_port`, one subdomain each: `http://<shortname>.localhost:<dev_port>/`. The `post-commit` hook prints your URL after each commit — surface that line to the FnB.',
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
  'sprint_prep',
  'Prepare and arm a Sprints v2 run — bind exact current specs, optionally gather QA/QC evidence, shape work units and dependencies, and enforce every launch invariant.',
  'workflow',
  NULL,
  0,
  '# sprint_prep — declare the riverbed

Load `sprint_protocol` first. Use this as the owning Planner while a Sprint is
`prepared`. Preparation ends at one atomic arming decision; it does not launch
participants piecemeal.

## Outcome

Produce one editable prepared Sprint with:

- one roadmap feature;
- exact governing spec revision hashes and any optional QA/QC evidence;
- work units made from existing spec tasks, each with one Developer and one
  assigned Reviewer;
- dependency edges and planned waves;
- one harness/model/Thinking level (`effort`) intent per participant;
- the Sprint merge grant — the FnB''s merge authorization for every registered
  Sprint PR, given by deciding to run the Sprint and recorded with
  `--merge-grant`; the engine refuses to declare or arm without it; and
- a capacity plan sized to justified parallel work and review demand, with the
  local/GitHub capacity to execute it.

Preview every participant with
`sc models resolve <harness> [<model>] [--effort <level>]`; omit `--effort` for
Vibe and Harness default. Pass = each controlled preview names the requested
Thinking level and each uncontrolled preview returns explicit `effort: null`.
Arm binds every route, records the armed transition, and publishes the first
assignments in one transaction; any mismatch rolls the whole arm back.

## Eligibility pass

Read the feature, selected spec bodies, task ledgers, available QA/QC records,
shell roster, model routes, quota state, repository access, and worktree
availability. Record the exact revision hash you inspected; a title or document
id is not a revision.

The FnB decides whether pre-Sprint QA/QC is useful. If requested, ask the
Review shell through ordinary inbox mail (no Sprint relay exists yet); it
signs the current exact body with:

```text
sc mem doc qaqc <spec-document-id> --verdict pass|fail [--findings-doc <document-id>]
```

The record is inspectable evidence, not launch authorization. Its absence,
verdict, findings, revision age, or signer state never blocks declaration or
arming. A body edit makes the prior record historical evidence. Proceed with
preparation regardless of whether review was performed or what it found.

Refuse arming when any of these is true:

- no current non-empty `spec` document belonging to the feature is bound;
- a bound spec body changed after declaration, so its current hash no longer
  matches the exact declared revision;
- a selected task belongs to no work unit or more than one work unit;
- a dependency cycle exists;
- a work unit lacks an assigned Developer or Reviewer;
- participant routes or required capacity are unavailable;
- another Sprint is armed, or a selected shell already participates in an armed
  Sprint.

Deficiencies remain editable in `prepared`. Do not weaken an invariant merely
to get to `armed`; surface the missing fact or capacity to the FnB.

## Shape work, do not script behavior

A work unit is one coherent editing lane and may group related spec tasks. Use
dependencies only for hard prerequisites. Waves express intent and later report
comparison; they do not forbid safe out-of-order completion. Reviews are not
editing lanes.

Prefer the smallest dependency graph that preserves correctness. Record the
expected output in outcome language. Do not encode a shell''s implementation
steps into the durable plan when its role skill and judgment can decide them.

### Balance capacity and parallelism

Optimize for the smallest participant set that keeps justified critical-path
development and review moving without avoidable queues. Neither minimum
headcount nor maximum shell occupancy is a goal.

Before choosing participants, analyze the task ledger and dependency graph for
coherent non-overlapping editing lanes, expected readiness, critical-path work,
and likely review demand. Put dependency-free Developer lanes in the same wave
and plan Reviewer capacity so ready reviews can run alongside ongoing
independent development. Do not serialize work merely because it appears in
task order, split coherent work, or start a review before its unit is ready just
to create concurrency.

- For one coherent small lane, normally use one Developer and one Reviewer.
- Add a Developer only when another independent lane can start or make useful
  progress without conflicting ownership and has enough review capacity.
- Add Reviewer capacity when expected concurrent review demand would otherwise
  queue critical-path work. Reuse a Reviewer across units when their review
  readiness is unlikely to overlap.
- Leave eligible capacity unassigned when the roster allows, preserving room
  for correction, re-plan, or urgent work. Use every eligible shell only when
  the work graph and review demand justify simultaneous work and coordination
  cost does not erase the expected time-to-completion gain.

Record the capacity rationale: chosen participants, parallel lanes, expected
review overlap, retained reserve, and why another shell would or would not
shorten the critical path.

For every participant, record role, route, nullable model, and Thinking level
(`effort`). Controlled exact routes default omitted effort to `high` only when
the preview proves support. Set both model and effort to JSON null for Harness
default. Vibe requires effort null and reports **Thinking control unavailable**.
Never pretend a native session can resume across harnesses.

Declare the prepared envelope from a JSON array of participant objects, binding
each current governing document directly. The server reads and hashes the body
inside the declaration transaction; the client never supplies a revision hash.
Then add each editing lane from existing spec tasks:

```text
sc sprint declare --feature <feature-id> \
  --spec <spec-document-id> --participants-file <path> --merge-grant
sc sprint plan-unit --sprint <id> \
  --developer-shell <id> --reviewer-shell <id> --title <title> \
  --expected-output-file <path> --task <task-id> \
  [--task <task-id>] [--wave <n>] [--depends-on <work-unit-id>] \
  [--output-kind code|report-only|no-code]
```

Repeat `--spec` for multiple governing documents. The deprecated
`--spec-approval <approval-id>` selector remains compatible when an old caller
must also retain a specific review row as evidence, but its verdict and reviewed
revision do not affect eligibility and direct `--spec` is canonical.

The participant file contains `shell_id`, `role`, and `harness`, with optional
nullable `model`, Thinking level (`effort`), and `route`. FnB may add
`--planner-shell <id>` when declaring for the originating Planner. Keep the
Sprint prepared while shaping the plan.

## Final arming check

Immediately before arming, select exactly one participating Reviewer as the
whole-Sprint conformance owner. Then re-read the exact spec revision hashes,
available QA/QC evidence, task coverage, participant routes and capacity,
single-armed invariant, repository access, and merge grant. Review evidence is
summarized, never interpreted as authorization. A prior Sprint''s cleanup never
gates arming.

```text
sc sprint arm --sprint <id> --conformance-reviewer-shell <shell-id>
```

Require the receipt to identify the selected owner and one revision-1 binding
for every participant. After `arm` succeeds, participant pickup belongs to
native delivery: the armed runtime dispatches ready work and wake recovery
reconciles unread pickup. Do not boot participants or create a second wake
path.

## Handoff

Once armed, hand control to `sprint_pln` and stop preparation work. Give the FnB
a compact declaration: Sprint id, feature, exact spec revisions,
participants/routes, work-unit graph, planned waves, capacity rationale and
reserve, merge-grant state, and known accepted risks. State whether pre-Sprint
QA/QC was performed and summarize any available evidence without treating it
as an eligibility result.

Stop when the Sprint is armed or when one concrete eligibility blocker has been
surfaced. Do not dispatch from a partially prepared plan.',
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
results, decisions, and PR facts arrive `re-enter`. You never choose a type,
poll, boot a participant, or schedule a watcher; the engine delivers. Stop
after a successful typed handoff and wait for the next wake.

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

Before recording conformance, author the final Sprint report. Name yourself as
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

Require the receipt: conformance report id, final report id, follow-up ids,
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
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
