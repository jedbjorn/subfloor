-- 0252 — reseed git, sprint_dev, and sprint_pln for universal PR owner wakes.
-- Ordinary PRs now enrol in the installation watcher through `sc pr subscribe`
-- as part of the git finish gate; the watcher routes red/green/closed/merged
-- owner facts inside or outside a Sprint, with non-Sprint green limited to
-- red-to-green recovery. No schema change: full-body UPSERTs converge upgraded
-- installations on the same text a fresh seed produces.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'git',
  'Git conventions for a Subfloor shell — one repo, one cwd. Sync the base before work, branch before committing, open PRs (never merge without the FnB''s OK), attribute commits per-shell. Use before any git work.',
  'substrate',
  NULL,
  0,
  '# git — version control, the Subfloor way

One repo at its root -> plain `git` (cwd = repo root) is safe.

Project = this repo minus `.super-coder/`. Engine = `.super-coder/` — gitignored, materialized by `sc update`, authored upstream in Subfloor. NEVER commit or edit anything under `.super-coder/`.

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

## Sync before you start — hard pre-code gate

Run the gate every session + before each new unit of work. `shell/<shortname>` = a moving base pinned to `origin/main`, not a content branch — cut feature branches from it. A stale base -> you read code that no longer exists + your PRs conflict on arrival.

The launcher auto-syncs at boot when provably nothing can be lost (on base branch + clean tree + no local-only commits). Read the `sync:` line in ACTIVE SESSION: auto-synced + nothing done since -> current, carry on. Says **NOT auto-synced** / you''re mid-session about to start new work -> run:

1. `git fetch origin main && git rev-list --count HEAD..origin/main` -> record remote freshness; continue through the branch/target gate even when the count is 0.
2. Compare `git rev-parse --show-toplevel` + `git branch --show-current` with ACTIVE SESSION before any destructive command. A mismatch -> stop + surface it.
3. Exact `shell/<shortname>` base -> discard local-only commits, tracked changes, and non-ignored untracked files without asking: `git reset --hard origin/main && git clean -fd`. Durable coordination belongs in the control plane and code belongs on a pushed remote branch with a PR. Pass = `git status --short` is empty + `git rev-parse HEAD` equals `git rev-parse origin/main`.
4. NEVER reset or clean a feature branch / open PR. Clean stale feature branch -> `git rebase origin/main`. Dirty or unpushed feature work -> list it + ask the FnB to land / stash / discard.
5. NEVER `git pull`/merge on the base — merge bubbles accumulate + squash-merged work replays as conflicts.

## Branch -> commit -> push -> PR -> stop

1. NEVER commit to the default branch. Branch first: `git checkout -b <type>/<short-desc>` (feat/fix/chore/docs). *Admin-shell exception:* it boots at the repo root on `main`, exempt from the branch-guard; committing to main is its mandate (engine updates, migrations, approved patches) and it starts each session with `git pull --ff-only`. Every other shell branches, always.
2. Commit in logical units. End every message with your shell''s trailer:
   ```
   Co-Authored-By: <shell display_name> (super-coder) <noreply@…>
   ```
3. Push -> open a PR -> subscribe -> stop. Do NOT merge without an explicit FnB directive — opening is the default, merging is a separate gate.

## Subscribe the PR — the engine watches it, you don''t

Right after the PR exists, enrol it in the installation watcher:

```
sc pr subscribe --repository <owner/name> --pr <number>
```

The receipt (`created: true`, or `created: false` with the same subscription id on an exact retry) is part of the PR finish gate. From then on the engine wakes you with a self-describing Re-enter fact — inside or outside a Sprint — on red checks, merge, close-without-merge, and red-to-green recovery. Never poll GitHub, schedule a watcher, or ask another shell to relay. Subscribe fails -> keep the branch and PR, surface the exact error, and do not claim notification coverage. A Sprint lane runs `sc sprint register-pr` instead (it creates the same owner subscription); never run both on one PR.

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

Only after the PR is merged. The `event=merged` wake from your subscription is
what starts this; confirm it on the remote (`gh pr view <n> --json state,mergedAt`)
before deleting anything:

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
- Exception: in the Subfloor source repo, tracked engine database source is project source; identify exact files through the repository catalogue.

## After DB work

A confirmed `sc mem` write lands in the shared control plane immediately. The
Admin/API persistence path owns generated serialization and renders; they are
not a Developer commit or Publish PR.

## Notes

- Before destructive ops, confirm the repo — `git -C <abs-path>` if ever in doubt.
- Multi-shell: each shell boots into its own worktree at `.sc-worktrees/<shortname>/` on branch `shell/<shortname>`; the launcher keeps the base pinned to `origin/main` (see the sync gate). Worktree isolation is automatic — no shared cwd. Admin shell = the one exception: repo root on `main`.
- UI preview: worktree edits do NOT show on the fork''s main dev server. `sc preview` (start once from the main checkout if not running) serves every shell''s worktree UI live (HMR) on the fork''s `dev_port`, one subdomain each: `http://<shortname>.localhost:<dev_port>/`. The `post-commit` hook prints your URL after each commit — surface that line to the FnB.',
  0
) ON CONFLICT(name) DO UPDATE SET
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

Assignments and review requests use Force-new delivery; verdicts and PR-event
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
Red/green/closed/merged Re-enter wakes continue (never add `sc pr subscribe`
on top — it is the same owner subscription). Required checks: pending -> native
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
   steering. Put only the work-unit id and spec reference in the PR body; write
   no PR comments or annotations.
4. As the literal final action, run:

```text
sc sprint request-review \
  --sprint <id> --registered-pr <registered-id> \
  --intent <submit|resubmit> --key <stable-key>
```

5. Require confirmation of the durable write + Reviewer wake; run no trailing
   command and stop and await the native verdict wake.

Changes requested returns by Re-enter. Apply every blocking finding,
re-establish green, and resubmit with a new review-round key. Do not narrate
cleared findings; the Reviewer verifies the full diff at the engine-injected
head. Record disagreements as judgment. Reviewer owns scope/severity; Planner
executes resulting action.

## Merge boundary

Approval is stale evidence. Immediately before merge, re-read live GitHub,
grant, ownership, unit state, approved head, and checks through:

```text
sc sprint authorize-merge \
  --sprint <id> --registered-pr <registered-id>
```

Merge only the returned repository, PR, and head SHA. A refusal means wait for
the watcher or re-enter the appropriate loop; never bypass it.

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
) ON CONFLICT(name) DO UPDATE SET
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
