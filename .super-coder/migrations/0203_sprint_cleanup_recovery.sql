-- 0203 — idempotent successful-Sprint cleanup recovery and role guidance.
-- 0202 is reserved by the concurrent context-efficient skill reseed lane.

BEGIN;

CREATE TABLE IF NOT EXISTS sprint_cleanup_requests (
    cleanup_request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id          INTEGER NOT NULL REFERENCES sprints(sprint_id),
    caller_shell_id    INTEGER NOT NULL REFERENCES shells(shell_id),
    request_kind       TEXT NOT NULL
                       CHECK (request_kind IN ('requeued','adopted_legacy')),
    idempotency_key    TEXT NOT NULL UNIQUE
                       CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    request_hash       TEXT NOT NULL
                       CHECK (length(request_hash)=64),
    response_json      TEXT NOT NULL
                       CHECK (json_valid(response_json)
                              AND json_type(response_json)='object'),
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sprint_cleanup_requests_sprint
    ON sprint_cleanup_requests(sprint_id,cleanup_request_id);

CREATE TRIGGER IF NOT EXISTS trg_sprint_cleanup_requests_append_only_update
BEFORE UPDATE ON sprint_cleanup_requests BEGIN
  SELECT RAISE(ABORT, 'Sprint cleanup requests are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_sprint_cleanup_requests_append_only_delete
BEFORE DELETE ON sprint_cleanup_requests BEGIN
  SELECT RAISE(ABORT, 'Sprint cleanup requests are append-only');
END;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'git',
  'Git conventions for a super-coder shell — one repo, one cwd. Sync the base before work, branch before committing, open PRs (never merge without the FnB''s OK), attribute commits per-shell. Use before any git work.',
  'substrate',
  NULL,
  '0',
  '# git — version control, the super-coder way

One repo at its root -> plain `git` (cwd = repo root) is safe.

Project = this repo minus `.super-coder/`. Engine = `.super-coder/` — gitignored, materialized by `sc update`, authored upstream in super-coder. NEVER commit or edit anything under `.super-coder/`.

## Sync before you start — hard pre-code gate

Run the gate every session + before each new unit of work. `shell/<shortname>` = a moving base pinned to `origin/main`, not a content branch — cut feature branches from it. A stale base -> you read code that no longer exists + your PRs conflict on arrival.

The launcher auto-syncs at boot when provably nothing can be lost (on base branch + clean tree + no local-only commits). Read the `sync:` line in ACTIVE SESSION: auto-synced + nothing done since -> current, carry on. Says **NOT auto-synced** / you''re mid-session about to start new work -> run:

1. `git fetch origin main && git rev-list --count HEAD..origin/main` -> 0 = carry on.
2. Behind -> take stock BEFORE touching anything: `git status` (uncommitted) + `git rev-list origin/main..HEAD` (unmerged commits) + `git branch --no-merged origin/main` (unlanded branches).
3. Anything local -> surface to the FnB first: list the commits/files, ask land / stash / discard. No sync without their call (soft gate).
4. Clean (or FnB said go) -> `git checkout shell/<shortname> && git reset --hard origin/main`. NEVER `git pull`/merge on the base — merge bubbles accumulate + your squash-merged work replays as conflicts.
5. Reset only the base, never a feature branch. Stale feature branch -> `git rebase origin/main`.

## Branch -> commit -> push -> PR -> stop

1. NEVER commit to the default branch. Branch first: `git checkout -b <type>/<short-desc>` (feat/fix/chore/docs). *Admin-shell exception:* it boots at the repo root on `main`, exempt from the branch-guard; committing to main is its mandate (engine updates, migrations, approved patches) and it starts each session with `git pull --ff-only`. Every other shell branches, always.
2. Commit in logical units. End every message with your shell''s trailer:
   ```
   Co-Authored-By: <shell display_name> (super-coder) <noreply@…>
   ```
3. Push -> open a PR -> stop. Do NOT merge without an explicit FnB directive — opening is the default, merging is a separate gate.

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

## Finish before you stop

Bookend to the sync gate. At end of session: `git status` (uncommitted) + `git rev-list origin/<base>..HEAD` (unpushed) -> resolve every hit:

1. Real work -> commit (attributed, trailer above) + push + open the PR. Don''t skip because the session is ending.
2. Throwaway / experiment -> discard deliberately: `git restore` / `git stash`.
3. Genuinely unsure -> surface to the FnB + leave it committed-and-pushed on a branch — never sitting uncommitted.

Pass = tree clean, or on a pushed branch with a PR. A dirty/unpushed tree forces the admin''s `git_cleanup` to map attribution, check liveness, and commit on your behalf.

## After a merge — clean up local

Only after the PR is merged:

A managed worktree whose Sprint is already `completed` is the exception: the
Sprint cleanup service owns its reset after live turns exit. Do not race that
service with manual Git cleanup. A pending or failed cleanup makes the slot
unavailable until `sc sprint cleanup-status --sprint <id>` reports succeeded;
the originating Planner or FnB uses the Sprint retry surface.

1. Re-pin the base. In a worktree `git checkout main` fails (main is checked out at the repo root; git refuses a branch checked out elsewhere) -> `git checkout shell/<shortname> && git fetch origin && git reset --hard origin/main`. Admin at repo root: `git pull --ff-only` on main.
2. `git branch -d <branch>`. Squash-merged -> `-d` refuses (commits aren''t ancestors of main); confirm the PR shows *merged* on the remote -> `git branch -D <branch>`.
3. `git fetch --prune`.

NEVER delete a branch carrying unmerged, un-PR''d work — no PR = lost work.

## Never commit the engine or derived files

- `/.super-coder/` is gitignored — never force-add anything under it.
- Gitignored + regenerated, never commit: `CLAUDE.md`, `AGENTS.md`, `opencode.json`, `.claude/skills/`, `.sc-state/engine.ref.prev` (ephemeral rollback pointer).
- From a worktree, commit only your project''s authored files. Generated
  snapshots and `_sc` renders live under ignored `.sc-state/local/` and never
  enter Git. `.sc-state/engine.ref` is the deliberate tracked exception: it is
  the dependency pin and is updated by `sc update`.
- Exception: in the super-coder SOURCE repo, `schema.sql` + `migrations/` are tracked — there the engine *is* the project.

## After DB work

An `sc mem` write lands in the shared engine DB immediately. The admin/API
save-local path refreshes the ignored snapshot and renders used by rebuild and
review. There is no generated-content commit or Publish PR. See `snapshot`.

## Notes

- Before destructive ops, confirm the repo — `git -C <abs-path>` if ever in doubt.
- Multi-shell: each shell boots into its own worktree at `.sc-worktrees/<shortname>/` on branch `shell/<shortname>`; the launcher keeps the base pinned to `origin/main` (see the sync gate). Worktree isolation is automatic — no shared cwd. Admin shell = the one exception: repo root on `main`.
- UI preview: worktree edits do NOT show on the fork''s main dev server. `sc preview` (start once from the main checkout if not running) serves every shell''s worktree UI live (HMR) on the fork''s `dev_port`, one subdomain each: `http://<shortname>.localhost:<dev_port>/`. The `post-commit` hook prints your URL after each commit — surface that line to the FnB.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_close',
  'Route Sprints v2 closeout to the owning role — Reviewer conformance, Planner control actions or completion receipt, and explicit FnB fallbacks — without creating a second close workflow.',
  'workflow',
  NULL,
  '0',
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

- **Reviewer receives `sprint.delivery_terminal`.** Load `sprint_rev` and follow
  **Delivery-terminal closeout**. Inspect the Sprint inbox once, compile bounded
  evidence, and choose between in-Sprint re-entry, abort, or the atomic clean
  `record-conformance` path. The Planner does not initiate this pass.
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

Assignments and review requests use Force-new delivery; role results use
Re-enter. Neither displaces a live turn; the runtime owns delivery, rotation,
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
  'sprint_pln',
  'Run an armed Sprints v2 collaboration loop as Planner — dispatch and restructure lanes, change participant routes, and execute Reviewer decisions through durable pause, resume, and close protocols.',
  'workflow',
  NULL,
  '0',
  '# sprint_pln — govern the armed Sprint

Use as the originating Planner after `sprint_prep` arms the Sprint. The system
captures deterministic facts; the Reviewer decides and documents, and the
Planner acts. Execute Reviewer decisions without taking over their judgment or
report authorship. The FnB retains the board-level override established by
decision #46.

Use the simplest path supported by current durable state. Treat authority,
lifecycle preconditions, durable writes, and typed handoffs as hard boundaries;
use judgment for investigation and execution within them. Repeat a read only
when later activity could have changed it or the next command requires live
revalidation.

## Route the entry

Classify the entry before reading an inbox:

- For a Sprint-scoped control decision, merged-work handoff, question, blocker,
  or other relay message, inspect the Sprint inbox once and handle that message.
- For an engine-wide completion or cleanup receipt, inspect the receipt and
  terminal Sprint state directly. It is informational: do not run the Sprint
  inbox, accept it, or issue a close command.
- For a live FnB instruction, act under the board-level override and name that
  authority in the durable evidence.

Load `sprint_pln` on every entry. Do not turn entry routing into a polling loop.

## Start from durable state

The armed runtime owns scheduled dispatch and unread wake recovery. The
registered-PR watcher owns subscription observation. React to their durable
facts; use the Planner turn for dispatch, plan structure, and exact execution of
durable Reviewer decisions. The Reviewer owns review and conformance judgment
plus the conformance and final Sprint reports. The Planner owns the plan and its
control transitions: it may modify, repeat, recall, reassign, or reroute work on
its own operational judgment, and it executes Reviewer decisions that cross
those same boundaries. Clean conformance approval performs its own atomic
terminal transition.

Assignments and review requests use Force-new delivery. Planner-bound results
use Re-enter. Neither displaces a live turn; delivery waits for its natural
boundary, and the runtime owns bundling, rotation, recovery, and coordinate
mode. Stop after a successful typed handoff so delivery can proceed cleanly.

Start with the durable trigger, then read only the lifecycle, work-unit,
dependency, route, PR, expectation, or anomaly facts needed for the current
decision. Viewing a participant conversation is observation, not activity;
never manufacture progress from browser presence.

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Release every dependency-ready lane through the production surface:

```text
sc sprint dispatch --sprint <id>
```

The returned ids are wake identities. Work-unit disposition and messages are
the authoritative release facts. Dispatch is safe to repeat: occupied Developer
lanes and stable assignment generations prevent double booking.

Accept or decline only when the inbox item is actionable. After acting on an
informational question, answer, blocker, or evidence message, run `accept` for
that message. For informational messages it only marks the message read; it does
not change Sprint or work-unit state.

## Running loop

- Keep dependencies as the only hard sequence. When reality requires a re-plan,
  restructure the current projection under Planner authority and record why.
  Ask the Reviewer when the change depends on review or conformance judgment;
  do not outsource ordinary assignment, capacity, or route judgment. Never
  rewrite completed history.
- Let Developers own their PRs through green, review, correction, and merge.
  Let Reviewers own verdicts and Sprint decisions. Do not proxy routine
  handoffs or substitute Planner judgment for a Reviewer decision.
- Consume passive system facts without waking yourself into every transition.
  Route a decision boundary to the Reviewer; act when its Re-enter decision
  message arrives.
- Record the Reviewer decision identity, the exact action taken, and the action
  receipt. Do not rewrite its rationale as Planner-authored judgment evidence.
- A mid-Sprint spec edit is allowed only by the owning Planner or FnB. Record
  the prior and new exact revision hashes. When acting as Planner, require a
  durable Reviewer decision before the edit. The running Sprint remains bound
  to its approved revision unless that decision explicitly says otherwise.

Use Sprint-native wakes for coordination. Do not start a recurring shell loop,
scheduled job, manual participant boot, or external PR watcher to track Sprint
state. When a watcher-dependent gate has stalled, use the bounded read once:

```text
sc sprint watcher-state --sprint <id>
```

It distinguishes a stale or never-started watcher from red, pending, or absent
PR observation and includes the newest bounded poll failures. Do not repeat it
as a polling loop; act on the evidence, then return control to native delivery.

## Questions, answers, blockers, and failures

Put one concrete question, answer, decision, blocker, or useful context item in
a short body file. Declare the message intent and whether the required reply
belongs to one work unit or the whole Sprint.

A question or blocker about one lane requires a reply and names that unit:

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

Confirm the reply write, then mark the handled incoming message read with
`accept`. For a blocker, include evidence, impact, and the exact action needed.
Continue safe independent governance, but stop at a decision boundary when an
answer is required. Unread recovery owns re-waking; do not send duplicate
reminders.

Choose one stable key for the intended recipient, exact body, intent, reply
linkage, and scope. Reuse it only when retrying that same write; use a new key
when any of those fields changes.
Keep the body near 6,000 characters and below the 8,000 hard maximum; run
`wc -m < <path>`. A handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake.

If a command is rejected or transport fails, the handoff is incomplete. Correct
and retry when safe. If the relay itself fails, surface the attempted command
and durable evidence to FnB; do not invent an alternate protocol. Relay a
Developer integrity concern to the Reviewer with its evidence, impact, and
recommendation. Send needed context before pausing because the Sprint relay is
unavailable while paused.

## Sprint artifact paths

Sprint working artifacts (per-unit review notes, raw diffs, evidence packets,
report drafts, and Dev scratch proof) go to the gitignored
`shared/sprints/sprint-<n>/` directory. They are never committed, branched, or
PR''d in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only.

## Reviewer decisions and Planner actions

Review, conformance, re-enter, and abort judgments remain Reviewer decisions.
Planner independently owns operational plan structure, including pause-safe
recall, cancellation of unreleased scope, reassignment, repeated task lanes,
and participant route changes. When an action does arrive as a durable Reviewer
→ Planner Re-enter decision, resolve every required-reply decision through its
original message before acting. Complete this order without reordering:

1. Re-run `sc sprint inbox --sprint <id>`, verify the decision came from the
   assigned Reviewer, and retain its message id.
2. Put a short acknowledgement of the exact requested transition in a body
   file, then send the linked reply:

```text
sc sprint send --sprint <id> --to <reviewer-shortname> --body-file <path> \
  --intent information --reply-to <decision-message-id> \
  --key <stable-control-reply-key>
```

3. Require the reply command to confirm its durable message and wake. Retry the
   same command and key if it fails or does not confirm both.
4. Mark the original decision accepted and require a successful receipt:

```text
sc sprint accept --sprint <id> --message <decision-message-id>
```

5. Only after acceptance confirms, execute the requested transition without
   re-adjudicating the decision. The linked reply must precede any pause or
   abort that makes the Sprint relay unavailable.

Record the decision message id, reply receipt, acceptance receipt, and action
receipt together. Clean conformance instead closes atomically and sends an
informational engine-wide completion receipt; it requires no linked reply or
Sprint inbox acceptance.

The FnB board-level override from decision #46 is unaffected: a live FnB
instruction may direct or supersede any action. Name that override in the
evidence instead of attributing it to the Reviewer.

If a requested action fails a lifecycle, authority, or disposition precondition,
do not substitute a different action. Send the refusal and current durable state
back to the Reviewer (or surface it directly to FnB for an FnB override), then
stop at that decision boundary.

### Pause or resume

Pause on a Reviewer decision or when Planner needs a safe restructuring window.
Transition durably, stop external Sprint services, persist interrupt intent,
preserve every partial artifact, and retain the governing judgment and evidence
for recovery:

```text
sc sprint pause --sprint <id> --reason <decision-or-restructure-reason>
```

Resume after the requested recovery/restructure is fully recorded, or on a
later Reviewer decision or FnB override. Reconcile native runs, unread messages,
pending wakes, work units, registered PRs, capacity, and spec drift, then act
with the supplied reason:

```text
sc sprint resume --sprint <id> [--reason <validated-reconciliation-reason>]
```

An exhausted recovery wake is bounded manual-recovery evidence, not a retry
loop. Preserve the unread message and failed wake, involve FnB, and do not create
recursive fallbacks. Drift informs; it never silently blocks resume.

PR ownership inherited from an aborted Sprint is an originating-Planner repair
boundary. Keep the replacement Sprint paused and establish the exact old and
new ownership. The originating Planner may reconcile that identity; the FnB
retains the same operation as a board-level override:

```text
sc sprint reconcile-pr --sprint <replacement-id> --repository <owner/repo> \
  --pr <number> --work-unit <replacement-unit-id> --reason <recovery-reason>
```

The command refuses a live source Sprint or target Sprint, a non-originating
Planner, a non-code or already owned target unit, and a closed unmerged PR. It
records the old and new owners plus the live GitHub head and acting authority.
If the PR is already merged, it also records the merge commit and completes the
replacement unit as explicit recovery evidence. Treat the receipt as recovery
evidence; wait for a separate Reviewer decision before resuming.

### Modify, recall, repeat, reassign, or reroute

Planner may cancel one unreleased work unit with its retained terminal reason.
When cancellation executes a Reviewer decision, preserve that decision id in
the reason and evidence:

```text
sc sprint cancel-unit --sprint <id> --work-unit <id> --reason <cancellation-reason>
```

Edit any subset of an unreleased lane directly. Omitted fields retain their
current values; `--clear-dependencies` is the explicit empty dependency set:

```text
sc sprint replan-unit --sprint <id> --work-unit <id> \
  [--developer-shell <id>] [--reviewer-shell <id>] [--title <title>] \
  [--expected-output-file <path>] [--task <task-id>] [--wave <n>] \
  [--depends-on <work-unit-id> | --clear-dependencies] \
  [--output-kind code|report-only|no-code]
```

Do not edit a released lane in place. To change an accepted or pending
assignment, first pause so dispatch cannot race the edit, recall the unmerged
lane, replan it, then resume:

```text
sc sprint pause --sprint <id> --reason <restructure-reason>
sc sprint recall-unit --sprint <id> --work-unit <id> \
  --reason <why-the-old-assignment-is-obsolete>
sc sprint replan-unit --sprint <id> --work-unit <id> <changed-fields>
sc sprint resume --sprint <id> --reason <validated-replan-reason>
```

Recall preserves the old accepted/declined message and event history, returns
only an unmerged lane to `planned`, and refuses completed, cancelled, or
PR-bound work. For a PR-bound lane, leave it intact and plan a replacement or
use the supported PR-ownership recovery path; never force the projection back.
Resume dispatches a fresh assignment generation.

The same governing spec task may deliberately appear in more than one work
unit. Use this for conformance reruns, repeat verification, or replacement work
whose scope is still governed by the original task. Do not create a duplicate
spec task merely to satisfy lane membership. Each work unit still lists a task
at most once.

To change which model will receive future assignments or reviews, pause first
when the Sprint is armed, clear the participant''s released expectation through
recall or completion, then replace its exact route:

```text
sc sprint reroute-participant --sprint <id> --participant-shell <id> \
  --harness <harness> [--model <model>] [--effort <effort>] \
  [--route <display-route>]
```

Prepared Sprints may reroute before arm. Armed Sprints must pause; Developer
routes reject any released lane and Reviewer routes reject an in-review lane.
The engine validates the new route before writing it. Existing chats and runs
stay immutable history; the next Force-new assignment or review request rotates
onto the replacement route. Only already-declared participants can be selected
or rerouted.

If a Developer or Reviewer declines, preserve the reason and choose the
replacement assignment or route from current capacity before issuing a fresh
assignment. Ask the Reviewer only when that choice changes review or
conformance judgment.

### Re-enter after conformance

A Reviewer `re-enter` decision names the in-Sprint findings, the governing
tasks (existing ids when scope is unchanged; new title and description when
scope is new), and the suggested
unit grouping, waves, dependencies, routing, and capacity rationale. The
Reviewer should identify independent lanes, expected review overlap, and useful
reserve. Preserve that projection; do not silently absorb extra scope, maximize
shell occupancy, or turn post-Sprint findings into delivery work.

Reuse an existing task id when the decision repeats or repairs that exact
governing scope. Cut a new task only for genuinely new scope:

```text
sc mem task add "<task-title>" --feature <feature-id> \
  --doc <governing-spec-document-id> --seq <next-seq> \
  --desc "<task-description>"
```

Create the requested bound work units from those task ids, wiring the Reviewer''s
waves and dependencies directly:

```text
sc sprint plan-unit --sprint <id> \
  --developer-shell <id> --reviewer-shell <id> --title <title> \
  --expected-output-file <path> --task <task-id> \
  [--task <task-id>] [--wave <n>] [--depends-on <work-unit-id>] \
  [--output-kind code|report-only|no-code]
```

After every named task is bound, confirm the routes are available and the
dependency graph and capacity plan match the decision. Planner may reassign or
reroute for operational capacity; send the concrete conflict back to the
Reviewer when that adaptation changes the Reviewer''s scope or conformance
judgment. Then release the new ready lanes with `sc sprint dispatch --sprint <id>`. When
the added work reaches terminal disposition, the engine sends the Reviewer the
next delivery-terminal wake; the Planner does not initiate the next conformance
pass.

### Conclude or abort

The Reviewer decides when the Sprint is done. A clean `record-conformance`
command atomically stores conformance, follow-ups, the Reviewer-authored final
report, completed lifecycle, and an informational engine-wide Planner receipt.
When that Re-enter arrives, confirm the receipt names the expected Sprint,
reports, outcome, and completed state. Do not run `complete`; closure is already
durable and the notification is informational because closure is already
terminal.
Successful completion also closes every other active participant chat
immutably linked to that Sprint. The originating Planner and report-authoring
Reviewer remain open. Do not manually close peer chats as a second closeout
action. Pause, abort, re-entry, failed conformance, and rejected fallback
completion retain their existing no-cleanup behavior.

The initial completion receipt reports `cleanup_state=pending`. Delivery is
finished, but its managed worktrees are not reusable yet. Stop and wait for the
engine-wide cleanup receipt; do not poll or manually reset participant trees.
On `cleanup_state=succeeded`, record the bounded receipt and treat the slots as
reusable. On a cleanup failure, inspect once and retry only after correcting the
named condition:

```text
sc sprint cleanup-status --sprint <id>
sc sprint cleanup --sprint <id> --key <stable-retry-key>
```

Require `created`, the cleanup request id, action, exact target ids, and the
aggregate projection in the retry response. Reuse the same key only for that
same request. FnB alone may add `--adopt-legacy` for one completed Sprint with
no scheduled targets; neither caller supplies a path.

Do not run `compile-report` by default, synthesize the final report, or
editorialize the Reviewer body. The Reviewer compiles its own evidence. A
Planner compile remains a valid FnB-directed fallback:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
```

Do not wait for or request a conclude action message after the completion receipt.
Abort is likewise an action taken only on a Reviewer decision or FnB override;
it is terminal and deletes nothing.

## Handoffs and stop

Planner → Developer assignments and Developer → Reviewer review requests use
Force-new delivery; Developer/Reviewer → Planner results are Re-enter. Forced
delivery waits for the prior live turn''s natural boundary; runtime delivery
owns the rest. These are delivery guarantees, not a parent/child chat topology.
The Planner receives no PR-event wakes; Developer-owned subscriptions carry
red, green, and externally closed facts directly to the owning Developer.

Never dispatch the next wave from a merge-observation turn. The Developer''s
merged-work handoff wake is the only normal next-wave dispatch trigger. On that
wake, complete the turn in this exact order:

1. Run `sc sprint inbox --sprint <id>` and inspect the durable merged-work
   handoff plus current work-unit and dependency state.
2. Act on every earlier informational item and mark each handled item read with
   `accept`, including the Developer handoff.
3. Finish all reconciliation, judgment recording, and other Planner
   bookkeeping. No work remains after this step.
4. As the literal final action of the turn, release dependency-ready lanes:

```text
sc sprint dispatch --sprint <id>
```

5. When the command confirms the durable assignment writes and New wakes, stop
   immediately. Run no trailing command. Empty dispatch is still the final
   action for that handoff turn; investigate only on a later durable wake.

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
  'sprint_prep',
  'Prepare and arm a Sprints v2 run — bind exact current specs, optionally gather QA/QC evidence, shape work units and dependencies, and enforce every launch invariant.',
  'workflow',
  NULL,
  '0',
  '# sprint_prep — declare the riverbed

Use as the owning Planner while a Sprint is `prepared`. Preparation ends at one
atomic arming decision; it does not launch participants piecemeal.

Use the simplest path supported by current durable state. Treat authority,
lifecycle preconditions, durable writes, and typed handoffs as hard boundaries;
use judgment for planning and evidence gathering within them. Repeat a read only
when later activity could have changed it or the final command requires live
revalidation.

## Outcome

Produce one editable prepared Sprint with:

- one roadmap feature;
- exact governing spec revision hashes and any optional QA/QC evidence;
- work units made from existing spec tasks, each with one Developer and one
  assigned Reviewer;
- dependency edges and planned waves;
- one validated harness/model/effective effort selection per participant;
- a committed Sprint merge grant; and
- a capacity plan sized to justified parallel work and review demand, with the
  local/GitHub capacity to execute it.

The arming transaction validates every recorded selection (explicit null
model/effort means the route default), records the armed transition, publishes
the initial assignment messages, and declares a New wake to the overseeing
Planner. Defaults satisfy the gate, but dispatch never precedes that validation.
Participant chats are created or re-entered later by wake delivery.

## Eligibility pass

Read the feature, selected spec bodies, task ledgers, available QA/QC records,
shell roster, model routes, quota state, repository access, and worktree
availability. Record the exact revision hash you inspected; a title or document
id is not a revision.

The FnB decides whether pre-Sprint QA/QC is useful. If requested, the Review
shell records its verdict against the current exact body through the
authenticated Sprint surface:

```text
sc sprint record-qaqc --document <spec-document-id> --verdict pass \
  [--findings-document <document-id>]
```

The record is inspectable evidence, not launch authorization. Its absence,
verdict, findings, revision age, or signer state never blocks declaration or
arming. A body edit makes the prior record historical evidence; it does not
change the exact revision the Planner later binds.

When the FnB requests pre-Sprint QA/QC, contact the Review shell through the
ordinary shell-to-shell channel because no Sprint relay or inbox exists yet.
Proceed with preparation regardless of whether review was performed or what it
found; after arming, switch to `sprint_pln`.

Refuse arming when any of these is true:

- no current non-empty `spec` document belonging to the feature is bound;
- a bound spec body changed after declaration, so its current hash no longer
  matches the exact declared revision;
- a selected task belongs to no work unit or more than one work unit;
- a dependency cycle exists;
- a work unit lacks an assigned Developer or Reviewer;
- participant routes or required capacity are unavailable;
- a selected shell has an unresolved cleanup target from an earlier Sprint;
- another Sprint is armed, or a selected shell already participates in an armed
  Sprint; or
- the merge grant was not committed as part of the final plan.

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

For every participant, record role, route, model, and effective effort. Never
pretend a native session can resume across harnesses.

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
`model`, `effort`, and `route`. FnB may add `--planner-shell <id>` when declaring
for the originating Planner. Keep the Sprint prepared while shaping the plan.

## Final arming check

Immediately before arming, re-read the exact spec revision hashes, available
QA/QC evidence, task coverage, participant routes and capacity, single-armed
invariant, repository access, prior-Sprint cleanup state, and merge grant.
Review evidence is summarized, never interpreted as authorization. The final
read and durable plan commit belong to the authoritative arming transaction;
external harness and GitHub work occurs after it commits.

If arming reports an unresolved cleanup target, inspect it once and act on its
named recovery instead of manually changing that worktree:

```text
sc sprint cleanup-status --sprint <prior-sprint-id>
sc sprint cleanup --sprint <prior-sprint-id> --key <stable-retry-key>
```

Only the originating Planner or FnB retries a failed scheduled cleanup. Only
FnB may add `--adopt-legacy` for one completed Sprint that predates scheduling.
Successful retry writes return cleanup to `pending`; native runtime and launch
preflight own execution.

Arming succeeds only when the first assignments and wake intents are durable.
A process crash after commit is outbox recovery; a crash before commit exposes
no partial Sprint.

```text
sc sprint arm --sprint <id>
```

After `arm` succeeds, participant pickup belongs to native delivery. The armed
runtime dispatches ready work and wake recovery reconciles unread pickup; the
preparing Planner does not manually boot participants or create a second wake
path. Initial assignments use Force-new delivery; a live turn reaches its
natural boundary before delivery and the runtime owns rotation and recovery.

## Handoff

Once armed, hand control to `sprint_pln` and stop preparation work. Give the FnB
a compact declaration:
Sprint id, feature, exact spec revisions, participants/routes, work-unit graph,
planned waves, capacity rationale and reserve, merge-grant state, and known
accepted risks. State whether pre-Sprint QA/QC was performed and summarize any
available evidence without treating it as an eligibility result.

Stop when the Sprint is armed or when one concrete eligibility blocker has been
surfaced. Do not dispatch from a partially prepared plan.',
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
  '0',
  '# sprint_rev — independent review and conformance

Use in one of two modes: a work-unit PR review during the loop, or the final
whole-Sprint conformance pass. Pre-declaration QAQC is a third entry condition,
before a Sprint exists. The evidence differs; independence does not. The
Reviewer decides and documents review and conformance judgments; the Planner
owns operational plan structure and acts on control transitions. The FnB
retains the board-level override established by decision #46.

Use the simplest path supported by current durable state. Treat independence,
authority, lifecycle preconditions, durable writes, and typed handoffs as hard
boundaries; use judgment for investigation and review within them. Repeat a
read only when later activity could have changed it or the next command
requires live revalidation.

## Route the entry

Classify the entry before reading an inbox:

- Pre-declaration QAQC begins from an explicit Planner or FnB request through
  the ordinary shell-to-shell channel. Read and sign the exact current spec
  body directly; there is no Sprint id or Sprint inbox to inspect yet.
- A work-unit review request or delivery-terminal notification is Sprint-scoped.
  Inspect the Sprint inbox once and accept the actionable request before work.
- For a live FnB instruction, preserve its board-level authority distinctly and
  inspect only the durable state needed for an independent decision.

Record pre-declaration QAQC through the authenticated surface:

```text
sc sprint record-qaqc --document <spec-document-id> \
  --verdict pass [--findings-document <document-id>]
```

Once a Sprint is armed, review and conformance entries arrive through durable
wakes. Load `sprint_rev` on every entry, then use the Sprint inbox for the
Sprint-scoped cases above:

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
```

Decline an actionable request you cannot take, with a concrete reason:

```text
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Use `accept` or `decline` for actionable work. After acting on an informational
question, answer, blocker, or context message, run `accept` for that message.
For informational messages it only marks the message read; it does not change
Sprint or work-unit state.

Review requests use Force-new delivery. Reviewer verdicts to Developers and
decisions to Planners use Re-enter. Neither displaces a live turn; delivery
waits for its natural boundary, and the runtime owns bundling, rotation, and
recovery. Stop after a successful typed handoff. Reviewers never receive
PR-event subscription wakes.

## Questions, answers, blockers, and failures

Put one concrete question, answer, blocker, or useful context item in a short
body file. Declare the message intent and whether the required reply belongs to
one work unit or the whole Sprint. Ask the Developer for missing PR evidence
with a unit-scoped question:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent question --requires-reply --work-unit <work-unit-id> \
  --key <stable-key>
```

Use `--intent blocker` instead when the unit cannot advance. Ask the Planner for
durable state or action-feasibility facts without delegating Reviewer judgment.
A cross-unit review, closeout, re-enter, abort, or safety ruling is a
Sprint-level decision:

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

Confirm the reply write, then mark the handled incoming message read with
`accept`. A blocker or integrity concern is evidence for your decision. If
action is needed, send the Planner the decision, impact, exact action, and
recommendation. Continue safe independent review, but stop at the decision
boundary when missing facts prevent an honest decision. Unread recovery owns
re-waking; do not send duplicate reminders.

Choose one stable key for the intended recipient, exact body, intent, reply
linkage, and scope. Reuse it only when retrying that same write; use a new key
when any of those fields changes.

Keep the body near 6,000 characters and below the 8,000 hard maximum; run
`wc -m < <path>`. A handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake.

If a command is rejected or transport fails, the verdict or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command, evidence, impact, and recommendation to FnB; do not invent an
alternate protocol. A Reviewer decides whether review or conformance evidence
warrants changes requested, re-entry, abort, or conclusion; the Planner
independently decides ordinary operational replanning and executes control
transitions. Record a live FnB override as FnB authority, not Reviewer judgment.

## Sprint artifact paths

Sprint working artifacts (per-unit review notes, raw diffs, evidence packets,
report drafts, and Dev scratch proof) go to the gitignored
`shared/sprints/sprint-<n>/` directory. They are never committed, branched, or
PR''d in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only.

## Conformance decisions and Planner controls

The Reviewer owns review, re-enter, abort, and conclude judgments. The Planner
independently owns operational plan structure: pausing for safe edits, recalling
unreleased work, modifying or repeating task lanes, reassigning work, changing
participant routes, cancelling unreleased scope, and resuming after validation.
A clean conformance approval atomically performs its own close. Base a Reviewer
judgment on durable Sprint state,
the exact bound revisions, current work/PR facts, progress-carrier evidence, and any
ratified judgment; ambiguous silence is not enough to corrupt a disposition.

Send re-enter, abort, or safety-critical control recommendations through the
durable `send` surface above. A clean conclude instead runs the atomic
`record-conformance` close below. Every Reviewer → Planner route is Re-enter.
The Reviewer-authored body must name:

- `decision`: `re-enter`, `abort`, or the exact safety-critical recommendation;
- the evidence and rationale owned by the Reviewer;
- exact Sprint/work-unit ids, reason, outcome, and complete action arguments;
- any immediate safety impact that the FnB must see.

The Planner marks the message handled, verifies the assigned Reviewer, and acts
on the judgment without surrendering its separate authority over the concrete
plan. The clean completion receipt is informational because the Sprint is
already terminal. The Reviewer never runs the standalone pause, replan, recall,
reroute, cancel, resume, complete, or abort action; its clean
`record-conformance` command owns the narrow automatic close. If a requested
action is rejected, inspect the returned durable state and issue a revised
judgment only when the evidence supports one; never ask the Planner to improvise
around a precondition.

The FnB board-level override from decision #46 is unaffected. A live FnB
instruction can direct or supersede any decision; preserve it as a distinct
authority record.

## Severity rubric

This skill owns severity. The governing spec intentionally does not.

- **Critical** — active security/authority violation, destructive corruption,
  or a condition that makes continued operation unsafe.
- **Major** — wrong behavior, data loss, broken invariant, material spec
  violation, or a loop/recovery path that can silently wedge delivery.
- **Medium** — a concrete correctness or recovery gap likely to bite normal
  use soon, including missing negative enforcement or an unreliable handoff.
- **Low** — bounded cleanup, clarity, test depth, or resilience improvement that
  does not make the delivered behavior wrong now.

During a work-unit review, Critical/Major/Medium block approval; Low is a
report note. During close-out conformance, severity does not decide timing: the
Reviewer judges whether each finding requires in-Sprint patching or is an
acceptable post-Sprint follow-up.

## Work-unit review

Accept the actionable review request and retain that exact message id as the
identity of this review round. Its readiness body must be only the bare
locator: submitting or resubmitting intent, PR URL, registered Sprint PR id,
exact head SHA, and work-unit id. Treat scope narrative, verification evidence,
judgment rationale, or review-focus steering in that body as a protocol defect;
do not use it to frame the review. Neither party writes PR comments or
annotations, and the PR body contains only the work-unit id and spec reference.

Bind every inspection and the eventual verdict to the accepted request''s
message id, registered PR, work unit, and exact head. Another request in the
same delivery, another unit assigned to this Reviewer, or role activity in a
different conversation does not belong to this round. Accept and review each
request explicitly; never infer a review lane from Reviewer identity alone.

Review the exact bound spec revision and the full diff at the request''s exact
head, then inspect checks, tests, relevant runtime evidence, and ratified
judgments. Each round is clean: no prior Developer evidence or prose is input,
and prior findings are cleared only when the code at the new head proves they
are cleared. Review code quality, edge cases/failure paths, and spec
conformance. Trace the real path; do not trust names or PR prose.

### Red-check doctrine

Accepted-red is not a legal review outcome. A departure that leaves checks
failing is never acceptable: do not note the failure and approve anyway. The
review handoff remains green-only, without exception or waiver.

`Note it and pass anyway` is the acceptance-shaped anti-pattern. In the
dos-arch incident, a Reviewer accepted known-failing tests as a scoped
departure and created a deadlock: the green-only handoff gate could never pass.
Decision #93 records why this no-waiver rule exists.

When failing checks are within the lane''s ratified scope, record
`changes_requested` so the Developer fixes them and re-establishes green. When
the failures are outside that scope, name the blocking failures in the finding
and send the Planner a `replan` decision through the control protocol. The
Planner must either widen the lane explicitly or cut a follow-up work unit; the
current lane remains unapproved until the resulting work is green.

Read resolved closure evidence through the authenticated memory surface; no SQL
or mutation is needed. Use the exact form for a cited flag and the scoped form
to audit every resolved flag attached to the feature:

```text
sc mem get flags <flag-id>
sc mem get flags --feature <feature-id> --resolved
```

Findings must state:

- severity and concise title;
- violated behavior or invariant;
- exact code/evidence location;
- a reproducible consequence; and
- the fix boundary, without prescribing unnecessary architecture.

Complete a unit verdict in this exact order:

1. Finish the review, findings, and verdict body; no inspection remains after
   this step.
2. Re-run `sc sprint inbox --sprint <id>`, act on newly arrived messages, and
   mark every handled informational message read with `accept`.
3. Run `wc -m < <path>`; keep the verdict near 6,000 characters and below the
   8,000-character hard maximum.
4. As the literal final action of the turn, record the typed verdict through
   the authenticated surface:

```text
sc sprint record-review \
  --sprint <id> --registered-pr <registered-id> \
  --verdict changes_requested --body-file <path> --key <stable-key>
```

5. When the command confirms the durable write and Developer wake, stop
   immediately. Run no trailing command.

Use `approved` only when no Critical/Major/Medium finding remains. The engine
checks that the request was accepted and still binds to the reviewed head,
records judgment evidence and sends a Re-enter wake to the Developer. Do not
message around this surface;
an unrecorded verdict cannot unlock merge.

## Delivery-terminal closeout

The `sprint.delivery_terminal` notification is the entry signal for
whole-Sprint conformance. Retain that exact notification message id and its
delivered wake as the closeout entry identity; another Reviewer turn or an old
terminal notification does not carry this episode. On that wake, inspect the
inbox, lifecycle, and current work-unit state first. If the lifecycle is already
`completed` or `aborted`, mark the informational notification handled with
`accept` and stop. If any non-terminal unit is visible, the wake is stale: mark
it handled with `accept`, exit, and await the next episode''s delivery-terminal
wake. Only an armed Sprint whose units are all terminal enters conformance.

Compile the bounded evidence packet first and do so yourself, then judge
integrated `main` against every governing bound revision, exact recorded
mid-Sprint revision fact, and ratified judgment:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
```

Increase the bound only when truncation counters show the default omitted
needed evidence; 200 is the maximum. The packet supplies facts, not judgment.
If every work unit was cancelled and nothing shipped, the honest decision is
`abort`, not `conclude`.

After classifying the requirements, choose exactly one branch:

- **In-Sprint patching required.** Do not run `record-conformance`. Send the
  Planner a durable `re-enter` decision naming every blocking finding; each
  spec task to cut against the governing spec document, with title and
  description; and the suggested unit grouping, waves, dependencies,
  Developer/Reviewer routing, and capacity rationale. Identify independent
  lanes, expected review overlap, useful reserve, and why additional capacity
  would or would not shorten the critical path. The durable decision is the
  failed-pass record.
  After three re-entry episodes in one Sprint, escalate the non-convergence to
  FnB instead of starting another patch round.
- **Clean or post-Sprint-only findings.** Prepare the conformance report,
  findings, final Sprint report, reason, and outcome. Submit them through the
  atomic `record-conformance` protocol below: the engine commits the evidence,
  completed lifecycle, informational Planner receipt, and wake together. Do not
  send a separate conclude message.

## Whole-Sprint conformance

Review the integrated system, not unit diffs. Classify each requirement as:

- `as-specced`;
- `deviated-intentionally` with its ratified judgment;
- `deviated-silently`; or
- `unimplemented`.

The last two are findings. Include spec document id and work-unit id when known.
For the clean or post-Sprint-only branch, write the conformance report and a
JSON findings array:

```json
[
  {
    "severity": "Major",
    "title": "Integrated seam diverges",
    "body": "Evidence and consequence.",
    "spec_document_id": 46,
    "work_unit_id": 9
  }
]
```

Keep the conformance report and each finding body at about 6,000 characters or
fewer; 8,000 is the hard maximum for each. Run `wc -m < <report>` and length-check
each finding body before submission.

Before recording conformance, author the final Sprint report. Name the Reviewer
as its author and answer:

1. Which exact scope and revisions governed?
2. What shipped, through which work units and PRs?
3. Which Reviewer judgments and ratified deviations shaped the result?
4. What failed, retried, paused, recovered, or remained anomalous?
5. What did conformance conclude?
6. Which follow-ups or unresolved items require FnB disposition?
7. Where is the complete evidence?

Keep the final report at about 6,000 characters or fewer and below the 8,000
hard maximum; run `wc -m < <report>`. Do not smooth discrepancies into a
success narrative. Keep the final report below 8,000 characters, then choose
the exact completion reason, terminal outcome, and stable completion key. The
engine stores the final report unchanged and generates the Planner receipt from
the committed report and follow-up identities.

Record the clean branch as one atomic final write:

```text
sc sprint record-conformance \
  --sprint <id> --body-file <report> --findings-file <json> \
  --final-report-file <final-report> --reason <reason> --outcome <outcome> \
  --key <stable-pass-key>
```

The receipt must name the conformance report id, final report id, follow-up ids,
completed state, Planner message id, and Planner wake id. This creates
append-only evidence, pending follow-ups, terminal lifecycle, and one
informational engine-wide Planner Re-enter in the same transaction. Never
record conformance first and then close around it; send no conclude message.
Require the receipt''s cleanup projection to report `pending`; cleanup executes
after participant turns exit. Do not reset a participant worktree, poll cleanup,
or wait for cleanup before stopping. The originating Planner receives the later
engine-authored success or failure receipt.
On that successful commit, the engine also closes every other active chat
immutably linked to the Sprint. The originating Planner and this
report-authoring Reviewer remain open. Do not manually close peer chats as an
extra closeout step. Pause, abort, re-entry, failed conformance, and rejected
fallback completion keep their existing no-cleanup behavior.
Never reopen an editing
lane after recording; the re-enter branch defers the report until added scope
reaches terminal disposition and a fresh delivery-terminal wake starts the next
episode. Surface any immediate safety risk to FnB.

## Stop

For unit review, follow the ordered verdict procedure above: inbox handling and
all evidence work precede `record-review`; the durable verdict is the literal
last action, then the Reviewer stops.

For the clean or post-Sprint-only branch, require both reports, findings,
reason, and outcome to replay idempotently. For the re-enter or abort branch,
confirm that the decision body carries the complete evidence and exact
requested action. Then complete this final handoff order:

1. Re-run `sc sprint inbox --sprint <id>`, act on newly arrived messages, and
   mark every handled informational message read with `accept`.
2. Confirm every Reviewer-authored artifact and decision body is final and
   below its 8,000-character hard maximum.
3. For a clean conclude, run the atomic `record-conformance` command above as
   the literal final action. When it confirms completed state, pending cleanup,
   and all receipt identities, stop immediately; the Planner is already
   notified.
4. For re-enter or abort, deliver the decision to the Planner as the literal
   final action:

```text
sc sprint send --sprint <id> --to <planner-shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level \
  --key <stable-decision-handoff-key>
```

5. When the command confirms the durable write and Planner wake, stop
   immediately. Run no trailing command until another native wake arrives.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
