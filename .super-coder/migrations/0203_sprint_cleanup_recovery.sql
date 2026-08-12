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

Use as originating Planner after `sprint_prep` arms a Sprint. System records
facts; Reviewer owns review/conformance judgment + reports; Planner owns plan
structure and executes control transitions. FnB retains board-level override
under decision #46.

Use the simplest path supported by current durable state. Treat authority,
lifecycle preconditions, durable writes, and typed handoffs as hard boundaries.
Repeat a read only when later activity could have changed it or the next command
requires live revalidation.

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

Read only lifecycle, unit, dependency, route, PR, expectation, and anomaly
facts required by the trigger. Browser presence is not progress.

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

- Keep dependencies as hard sequence; restructure current projection under
  Planner authority, record why, and never rewrite completed history.
- Developers own PR green/review/correction/merge. Reviewers own verdicts and
  conformance. Do not proxy routine handoffs or judgments.
- Record Reviewer decision id + exact action + receipt; never rewrite rationale
  as Planner judgment.
- Mid-Sprint spec edits require owning Planner/FnB + durable Reviewer decision.
  Record old/new revision hashes; binding changes only when the decision says so.
- Use native wakes. Start no recurring loop, scheduled job, manual participant
  boot, or external watcher. One stalled-gate inspection is allowed:

```text
sc sprint watcher-state --sprint <id>
```

Do not repeat this read as a polling loop. Act on its bounded watcher evidence,
then return to native delivery.

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

To change future assignment/review model, pause armed Sprint, clear released
expectation, then validate + replace route:

```text
sc sprint reroute-participant --sprint <id> --participant-shell <id> \
  --harness <harness> [--model <model>] [--effort <effort>] \
  [--route <display-route>]
```

Prepared Sprints may reroute directly. Armed Developer route rejects released
lane; Reviewer route rejects in-review lane. Existing chats/runs remain history;
next Force-new delivery uses replacement. Reroute declared participants only.
On decline, preserve reason and choose replacement from current capacity; ask
Reviewer only if review/conformance judgment changes.

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

Review requests use Force-new delivery. Verdicts and Planner decisions use
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
work unit, and exact head. Review each request explicitly. Read the exact spec
revision + full diff at that head, then checks, tests, relevant runtime facts,
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

5. Require durable judgment evidence + Developer Re-enter wake. Run no trailing command;
   stop.

Use `approved` only with no Critical/Major/Medium finding. Engine validation
requires the accepted request and reviewed head. Do not message around the
surface; an unrecorded verdict cannot unlock merge.

## Delivery-terminal closeout

Retain the exact notification message id + delivered wake as this closeout
episode''s identity. Inspect inbox, lifecycle, and units first:

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
