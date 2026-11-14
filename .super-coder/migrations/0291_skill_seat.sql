-- 0291 — skill seat tag (Feature #97, spec #276).
--
-- skills.seat names the one launch seat a skill is usable in: 'gui' (browser
-- chat or headless `./sc run`) or 'tui' (terminal `./sc enter`). NULL = both
-- seats. The boot SKILLS list and the managed harness skill roots omit a
-- skill whose seat does not match; grants never change.
--
-- schema.sql is the partial core baseline and every rebuild applies it and
-- THEN every migration, so the column lives here only (same convention as
-- 0268 on documents). For the same reason the generated 0001 seed never names
-- `seat`: this file tags the engine skills, and seed_skills owns the column on
-- every later heal.
--
-- Reseeds cli_orchestration (now `seat: tui`, its Seat gate removed),
-- delegation and spec_execution (seat-split text replaced by a pointer to the
-- boot SEAT section). Full-body UPSERT by name keeps skill_id and grants.
-- Additive; idempotent apart from the ADD COLUMN, which the ledger runs once.
BEGIN;

ALTER TABLE skills ADD COLUMN seat TEXT CHECK (seat IN ('gui','tui'));

INSERT INTO skills (name, description, category, command, common, content, seat, is_deleted)
VALUES (
  'cli_orchestration',
  'Planner-run delivery from a terminal seat; load when the FnB assigns work in this chat for you to orchestrate with CLI agents. You stay the oversight layer while native or cross-harness agents verify the spec, build, review, deploy to Dev, and verify. Covers the kickoff contract, agent launch per harness (Claude, Codex, Kimi), terminal-seat PR/CI monitors, the FnB''s merge and deploy directives, acceptance review, and cleanup.',
  'workflow',
  NULL,
  0,
  '# cli_orchestration — run a delivery from the terminal

Use when the FnB assigns a spec, task set, or work unit "in this chat" and
wants you to orchestrate it with your harness''s own agents. You are the
oversight layer: you brief, launch, monitor, verify handoffs, and surface
questions. Agents do the work. Keep your context window for the FnB and you.

## Your lane

- Do not write code or propose fixes. Route findings to the agent that owns
  the change. Offer an opinion only when the FnB asks for one.
- Delegate reading as well as writing. Have agents read the large sources
  and report only conclusions, identifiers, and evidence paths.
- Surface every question that could change scope, acceptance, or authority
  to the FnB, verbatim and with the agent''s evidence. Do not answer it
  yourself, and do not let agents choose for the FnB.
- You own the durable writes: `sc mem state`, narrative, flags, decisions,
  and task status. Agents report back to you; they do not write `sc mem`.
- Never accept an agent''s report as proof. Verify each claim against the
  durable artifact: the branch on the remote, the PR, check results, and
  receipts or evidence files.

## Kickoff contract

1. Load the assignment with `sc context --task <id>` or
   `--work-unit <id>`. Read the spec''s tasks and run the version-control sync
   from your boot.
2. Restate the assignment in one block: the work, the lanes, the model and
   effort per lane, the monitors, the verification seat, and what stays with
   the FnB.
3. Ask the open questions before launching anything, and ask them in one
   batch:
   - Spec freshness. The spec may name sites that have moved on main. Offer
     a spec-verification agent before dev starts.
   - Merge authority. This loop runs outside an armed Sprint, so your boot''s
     merge gate applies in its first form: merge only on an explicit FnB
     directive that identifies the PR. The directive may name the PR by
     number, or name an assignment and the condition its PRs must meet (for
     example "merge this assignment''s PRs once green and review clean"); that
     kind covers only that assignment''s PRs, and only once the stated
     condition is met. Ask for that directive here. It is not a standing
     grant or a third form: no answer, a PR outside the assignment, or an
     unmet condition -> report the PR as ready and wait.
   - Deploy authority. Ask for the target (Dev only, unless the FnB says
     otherwise), when to deploy, and whether a final deploy at the end
     replaces the intermediate ones.
   - Prerequisites and gates. List the PRs, seats, or flags the work waits
     on, and decide with the FnB whether to wait or to proceed.
   - Lanes the FnB has not assigned, such as doc or skill edits, and whether
     an agent takes each one.
   - What stays with the FnB, such as release, follow-up disposition, and
     production.
4. When the FnB answers, record the directives (merge, deploy, models, gates)
   in one `sc mem narrative` line and point `sc mem state` at it. They last
   for this assignment only; cite the merge directive when you merge.

## Lanes and order

Run the lanes in this order. Independent lanes run in parallel.

1. Spec verification. One agent checks every site, contract, and budget the
   spec names against current `origin/main`. It reports a drift table. You
   take any drift to the FnB, who may revise the spec (`sc mem doc edit`)
   before the build starts.
2. Baseline evidence, when acceptance compares before with after. Capture it
   on the verification seat before any deploy changes that seat.
3. Development. The agent builds one branch per unit in its own worktree. It
   pushes the branch and opens a PR. It does not merge.
4. Review. Use a reviewer from a different model family than the developer
   wherever one is available. Give the reviewer the spec and the diff, not
   your view of them. Route findings back to the developer, and repeat until
   the review is clean or the FnB rules on a disagreement.
5. Merge and deploy, only under the FnB''s recorded directive. Run
   `gh pr merge <n> --squash` (or the repo''s convention) on a PR the directive
   covers, once its condition holds: checks green and review clean unless the
   FnB said otherwise. Deploy only to the directed target, using the fork''s
   dev_kit or remote-seat procedure.
6. Acceptance review. Before any verification run, show the FnB the method
   and pass condition for each criterion. Change them only with the FnB''s
   agreement.
7. Verification. An agent runs the agreed criteria on the named seat and
   keeps transcripts, receipts, and readback. It reports each criterion as
   pass, fail, or pending, with the evidence path. It never presents
   inference as proof.
8. Evaluation. Report the results to the FnB. The FnB decides the follow-ups
   and you record them as flags or tasks.

## Launching agents

Name the model and effort the FnB asked for, exactly. If a harness cannot set
one of them, say so and do not substitute silently.

**Native subagents** keep their output out of your context:

| harness | how |
|---|---|
| Claude Code | Agent tool. Set `model` (alias such as `opus`) and `effort` only when the FnB names them. Agents run in the background and notify you on completion. Use `isolation: "worktree"` for a builder. Continue an agent with SendMessage instead of starting a new one. |
| Codex | Native multi-agent tools, when `codex features list` shows `multi_agent` enabled. If you need a model or effort the native tool cannot pin, use a CLI child instead. |
| Kimi, others | Use CLI children. |

**CLI children** let any harness run any other harness, which gives you
cross-family review. Write the brief to a file and run each child as one
background command with its own log:

```bash
# Claude child (from Codex/Kimi or for a pinned model)
claude -p --model <model> --effort <level> --dangerously-skip-permissions \
  --output-format json < brief.md > lane.json 2> lane.err
# Codex child
codex exec -m <model> -c model_reasoning_effort=<level> -s danger-full-access \
  -C <worktree> -o lane.final.md - < brief.md > lane.log 2>&1
# Kimi child (prompt mode auto-approves; --yolo/--auto are refused with -p)
KIMI_MODEL_THINKING_EFFORT=<level> kimi -m <alias> -p "$(cat brief.md)" > lane.log 2>&1
```

To continue a child, resume its session: `claude -p --resume <session_id>`
(the id is in the JSON result), `codex exec resume <thread_id> "<follow-up>"`,
or `kimi -m <alias> -S <session_id> -p "<follow-up>"`. Kimi''s session id has
the form `session_<uuid>`; `kimi session list --cwd <worktree> --json` lists it.

Rules for CLI children:

- Run a child from its own worktree, never from yours. Your worktree holds
  your rendered boot file, so a child started there boots as you.
- A child inherits your `SC_*` environment, so the engine sees it as you.
  The brief forbids `sc mem` writes. Read-only `sc` verbs and the project
  verbs you name (`sc test`, `sc context`) are allowed.
- Start one background child, or one monitor, per tool call. A process
  started inside a compound command can lose its completion signal.
- Keep the run directory (briefs, logs, final messages) outside git, for
  example `${TMPDIR:-/tmp}/sc-orch-<shortname>/`.

## The brief

Each brief is self-contained. It contains:

- the goal, and the spec and task ids with the command that reads them;
- the worktree path, branch name, and base (`origin/main`);
- the scope boundary and what is out of scope;
- the authority: push the branch and open a PR, yes; merge, deploy, and
  `sc mem` writes, no;
- the acceptance that applies to this lane and the tests to run;
- a stop rule: on a question that changes scope or acceptance, stop and
  report it instead of choosing; and
- the report shape: PR number and URL, commit SHAs, each test run with its
  result, evidence paths, open questions, and nothing else.

## Monitors

Set up your own monitors: no engine service watches a PR for you in this
session. Inside a Sprint the engine delivers PR facts as wakes instead.

- **PR checks:** `gh pr checks <n> --watch --interval 60` as a background
  command; it exits when the checks settle. Read the result with
  `gh pr view <n> --json state,mergeable,reviewDecision,statusCheckRollup`.
- **Merge conflicts:** an open PR whose `mergeable` is `CONFLICTING` belongs
  to its developer. Route it back.
- **Stalls:** if a child''s log has stopped growing past the expected window,
  read its tail before you decide. Kill and relaunch only with a fresh brief.
- **No auto-wake:** if your harness does not re-invoke you when a background
  command exits, read every log and exit status at each turn and before any
  status report. Report only what you have read.

## Ledger

Keep a short ledger in the run directory. List each lane with its agent
or session id, model and effort, worktree, branch, PR, monitor, and status.
Update it at every event. After a context compaction, the ledger and
`sc mem state` are how you recover the run.

## Close

Before you report done:

- Confirm every PR is merged, closed, or handed to the FnB with its state.
- Remove every worktree you created with `git worktree remove <path>`, then
  run `git worktree prune`. Delete local branches whose PRs merged.
- Stop every background child and monitor you started. Leave no orphans.
- Delete the run directory, or name it to the FnB as evidence.
- Mark tasks done, open flags for the follow-ups the FnB decided, and close
  the flags your work cleared.
- Update `sc mem state` and add a closing narrative line.
- Give the FnB a final report: what shipped (PRs), the verification result
  for each criterion, what remains pending, and the decisions that are theirs.',
  'tui',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, seat=excluded.seat, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, seat, is_deleted)
VALUES (
  'delegation',
  'Delegate small work to an `sc job` subagent or set a watcher; load before you launch, continue, stop or clean one. Covers who may launch, the rule that a subagent acts as you and you answer for it, harness, model and effort, the brief, reading a result, the limits and their refusals, and cleanup.',
  'workflow',
  NULL,
  0,
  '# delegation — subagents and watchers through `sc job`

A subagent is one headless harness run that the engine starts for you, on any
installed harness, with the model you name. It works in its own worktree,
outlives your turn, and wakes you once when it ends. A watcher is a job that
ends when a condition holds and wakes you once. Both go through `sc job` and
both work on either seat.

## When to use it, and when not

- Use a subagent for small work with a clear boundary and a clear report: a
  check of a spec against `origin/main`, a review of a diff by another model
  family, a research question, one contained change on its own branch.
- Big work that needs several shells, review and a merge is a Sprint, not a
  subagent.
- A question for the FnB is not delegated. Ask the FnB.
- Whether your harness''s native agents are an alternative depends on your
  launch seat: see your boot SEAT section.

## You are accountable

- A subagent holds exactly your identity and your authority, no more and no
  less. It runs with your environment: your API token, your shell identity,
  your git identity. The engine sees its `sc` calls as yours, and its commits
  carry your name.
- The engine adds no refusal for what a subagent writes to `sc mem`, to git or
  to GitHub. Your brief is the only thing that limits it.
- You answer for everything it does. Your mandate, your boot rules and your
  merge gate bind the work you delegate. A merge by your subagent is a merge by
  you: outside an armed Sprint it needs the FnB directive that identifies the
  PR; inside one it needs the grant `sc sprint authorize-merge` returns.
- Its report is input to you, never proof. Verify before you rely on it.

## Who may launch

- Every non-Admin flavor may launch a subagent, on either seat. Admin is
  refused.
- Inside an armed or paused Sprint, Developers and the Planner may launch. A
  Reviewer of that Sprint may not: a review verdict must not rest on a reader
  nobody assigned. A Reviewer outside a Sprint may launch.
- A subagent cannot launch or continue a subagent. It may start command jobs
  and watchers; those belong to you and wake you.

## Choose the harness, model and effort

- `--harness` and `--model` are required. `--effort` is optional: when you
  omit it the engine sends no effort setting and the harness''s own default for
  that model applies.
- Name the model and effort the FnB asked for, exactly. If a harness cannot
  carry what was asked, the launch is refused; say so and never substitute
  another model silently.
- For review, choose a model family different from the one that built the
  change.
- Check a selector before launch: `sc models resolve <harness> <selector>`.
  A model id the provider rejects is not caught at launch: the run starts,
  ends `failed`, and its log carries the provider''s message.

## Write the brief

The subagent starts as a plain harness in a fresh worktree. It has no boot
file, no skills and no memory of your conversation. The brief is all it knows.
A brief is at most 64 KiB and states:

- the goal, with the spec and task ids and the commands that read them;
- the base and the branch it works on, and what is out of scope;
- what it may and may not do, each stated: push, open a PR, merge, deploy,
  `sc mem` writes;
- the acceptance that applies and the tests to run;
- a stop rule: on a question that would change scope or acceptance, stop and
  report it instead of choosing;
- the shape of the report: PR number and URL, commit SHAs, each test run with
  its result, evidence paths, open questions.

## Launch, then end your turn

```bash
sc job agent --harness <name> --model <selector> [--effort <level>] \
  (--brief-file <path> | --brief "<text>") \
  [--base <ref>] [--branch <name>] [--label <text>] [--timeout <seconds>]
```

- The engine creates the worktree `.sc-worktrees/agent-<run_id>` at `--base`
  (default: your current commit). Without `--branch` the worktree is detached,
  which suits reading, review and research. With `--branch` the engine creates
  that new branch at the base; use it when the subagent is to commit.
- A refusal registers nothing, creates no worktree and starts no process.
- The verb prints the run id, the resolved harness, model and effort, the time
  limit and the worktree. One launch per tool call. The run''s end wakes you
  once; how that wake reaches you, and whether to end your turn now, is in
  your boot SEAT section. `sc job wait <id>` is a bounded foreground wait.
- `sc job list --kind agent` shows your live subagents with the time each has
  left, and the input + output tokens of each once a usage sweep has read them
  (cache reads and writes are not counted there; the Analytics tab counts
  them). `sc job status <id>` shows one run. `sc job tail <id>` shows its log.

## Read the result

The wake states the facts: state and exit, harness, model and effort, time run
against the limit, the worktree or that it was removed, branch, HEAD, whether
uncommitted changes exist, and the size of the result. It carries no part of
the subagent''s final message.

```bash
sc job result <id>          # the final message and the recorded facts
sc job result <id> --json   # the structured form
```

- The result shows at most 256 KiB and says when it was cut; the full text is
  `result.md` beside the run''s log.
- States: `done`, `failed`, `timeout` (the time limit stopped it), `killed`
  (you or the operator stopped it), `lost` (its supervisor died; the outcome is
  unknown, never success, and nothing is rerun). For a lost run the wake has
  a line `When the run was marked lost:` that says what the engine found of
  the harness process then: that it stopped it; that it was left running,
  because it could not be tied to the run or could not be signalled; or that
  it could not be checked, so it may still be running. No such line means the
  harness was found gone.
- Check every claim against the branch, the PR and its checks before you act
  on it or repeat it to the FnB.

## Continue, or launch again

```bash
sc job continue <id> (--brief-file <path> | --brief "<text>") [--label <text>] [--timeout <seconds>]
```

- A continue is a new run in the same worktree and the same harness session,
  on the same harness and model. It takes a slot and has its own time limit.
  Use it for a follow-up in the same thread of work, such as "re-check after
  commit abc".
- It is refused while the earlier run is live, when no session reference was
  captured, and after the worktree was removed with `--discard`.
- When the first run went wrong, do not continue it. Launch a fresh subagent
  with a fresh brief.

## What happens to the worktree

- When a run ends and its worktree is unchanged (no uncommitted change, and
  HEAD still at the base commit), the engine removes the worktree. A later
  continue recreates it at the same path.
- Untracked files count as a change and keep the worktree. Files the
  repository ignores (`.gitignore`) and files excluded locally
  (`.git/info/exclude`) do not count as work and are not kept: they are removed
  with the worktree, and the wake says so. Tell the subagent to commit
  anything you need to keep, or to write it where git does not ignore it.
- A changed worktree is kept until you remove it:

```bash
sc job clean <id>             # remove a kept worktree
sc job clean <id> --discard   # remove it with uncommitted or unpushed work
```

- `clean` is refused while a run is live in the worktree. Without `--discard`
  it is refused when the worktree holds uncommitted changes or commits that
  are on no remote branch. A branch the run created is kept either way. After
  a discard the subagent cannot be continued.

## Stop

`sc job kill <id>` stops a running subagent or watcher. The run ends `killed`
and you are woken with that state. The worktree is kept or removed by the same
rule as for any ended run.

## Watchers

```bash
sc job start --until "<shell command>" [--every <seconds>] [--timeout <seconds>]
sc job watch pr --pr <n> [--repository owner/name] [--every <seconds>] [--timeout <seconds>]
sc job watch timer --after <seconds>
```

- `--until` repeats a `/bin/sh` command until it exits 0. It tries every 30
  seconds for at most 1 hour unless you say otherwise.
- `watch pr` wakes you when that PR''s checks settle (green or red) or it is
  merged or closed. It reads every 60 seconds for at most 1 hour unless you
  say otherwise. Green means proven green at the head commit. Any shell may
  watch any PR it can read. The wake grants no merge authority: green is not a
  directive.
- `watch timer` wakes you once after the delay, which is at most 24 hours.
- A Developer''s own PR still gets its facts from the PR watcher; do not
  duplicate those wakes. Never loop on `gh` in your own turn.

## Limits, and what a refusal means

| Limit | Value |
|---|---|
| Live subagents for one shell | 3 |
| Live subagents on the install | 6 |
| Run time of one subagent run | 2 hours by default; `--timeout` up to 8 hours |
| Nesting | none: a subagent cannot launch or continue a subagent |
| Live watchers for one shell | 10 |
| Brief size | 64 KiB |

- A refusal at a concurrency limit names the count and the runs holding the
  slots. Wait for one to end or stop one with `sc job kill <id>`. Do not retry
  in a loop.
- A run that reaches its time limit is stopped and ends `timeout`.
- A refusal for nesting, for a Reviewer in a Sprint, or for Admin is a rule,
  not an error to work around. Report it; do not look for another route.

## Close

Before you report the work done:

- Stop every subagent and watcher of yours that is still running:
  `sc job list` shows them.
- Remove every kept worktree with `sc job clean <id>`, after you have pushed
  or copied what you need from it.
- Record the outcome in `sc mem`: state, a narrative line, flags and task
  status. Those writes are yours.

## Seat

Whether `sc job agent` is your way to delegate or an option beside your
harness''s native agents is set by your boot SEAT section. On any seat it adds
a launch on another harness built by the engine, a ledger row, and a run the
operator can see and stop.',
  NULL,
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, seat=excluded.seat, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, seat, is_deleted)
VALUES (
  'spec_execution',
  'Developer spec loop and testing posture; load before you build against a spec or work unit, or pick the tests that prove a change. Holds spec selection, the pre-plan analysis, the task ledger, one-task-at-a-time execution, verification, the ship and docs handoff, and the testing posture (smallest affected gate, long-running checks by seat, CI fallback, when the full suite runs).',
  'workflow',
  NULL,
  0,
  '# spec_execution — build what the spec promises, prove what you built

A feature spec governs the work whenever one exists. Unspec''d quick fixes (a
small UI tweak, a minor migration) skip the task plan; the testing posture
below applies to every change.

## Before you touch code

1. **Select the spec.** An assignment that names a task or work unit ->
   `sc context --task <id>` / `sc context --work-unit <id>` is your planning
   context. Otherwise `sc mem get documents --feature <id>`, then the whole
   body with `sc mem get documents --doc <doc_id>` and its ledger with
   `sc mem get tasks --doc <doc_id>`. Never auto-pick the latest document;
   several plausible specs -> ask the FnB. Existing tasks -> resume the first
   unfinished one. Read broader indexes only for an unresolved need.
2. **Analyze before planning.** Viability: bounded, clear entry points,
   session-sized verification; a missing done-condition is unclear. Current
   Posture matches the documented baseline; a mismatch stops for the
   Planner/FnB — never redefine it silently. Every In Scope promise is
   planned and every Out of Scope item stays out. Anticipated User Activity
   roles, reach, authority, and tenancy are planned. Two plausible readings or
   unstated required knowledge -> ask the FnB. A missing prerequisite,
   environment, or external dependency -> open one High flag on the feature
   and stop at its boundary.
3. **Plan.** Building now moves `brainstorm|long_term|near_term` to
   `in_progress`: `sc mem roadmap status <feature_id> in_progress`. Confirm
   the work-stream; assign the obvious one with
   `sc mem roadmap project <feature_id> <shortname>`, ask when ambiguous. Lay
   the ledger: `Preparation` at seq 0, one row per independently verifiable
   step, `Verification` last, each
   `sc mem task add "<step>" --feature <id> --doc <doc_id> --seq <n> --desc "<outcome>"`,
   then `sc mem state "[F<id>] last: —. next: Preparation."`. No task plan, no
   build.

## Build

4. **Execute one task at a time.** `sc mem task start <id>`; work and verify
   only that task; `sc mem task done <id>`; re-read the ledger; set
   `current_state` to the highest done + lowest pending task. Work moved
   elsewhere -> `sc mem task cancel <id> --notes "moved to F<id> task #<n>"`;
   a whole unfrozen spec moves intact with
   `sc mem doc move <document_id> --feature <target>`. Small growth within the
   same intent -> revise the unfrozen spec and add tasks; a separate mental
   model -> stop and recommend a new feature + spec, never absorb it.
5. **Verification** follows TESTING POSTURE below and requires every In Scope
   done-condition and Anticipated User Activity contract; unexpected reach,
   weakened hardening, or crossed tenancy fails. A large spec may stop after a
   verified task slice with the next task named in `current_state`.
6. **Ship and hand docs to the Planner.** All tasks done + Verification green
   -> `sc mem roadmap status <feature_id> shipped`; open one Medium flag
   `"[Docs] <feature> shipped, doc pending | Blocker for: <feature> doc"
   --feature <id>`; message the planner to freeze the spec, write the
   `kind=doc` document, and close the flag; tell the FnB. No planner shell ->
   tell the FnB and leave the flag open. Never freeze or author the shipped
   doc yourself.

## TESTING POSTURE

- **What to run.** Run every available smallest affected test target that
  proves the changed behavior and realistic failure paths.
- **Long checks.** Start any check that may outlive the current tool call or
  session as a registered job, then wait on it the way your boot SEAT
  section says:
<!-- sc-root-only: commands carried verbatim from the Developer template; bare `sc` works the same from a worktree -->
  Use `./sc job start --label gate -- <command and args>` for checks that may outlive the current tool call or session. Inspect a run with `./sc job status <id>` and `./sc job tail <id>`.
  A lost outcome is unknown, never a pass.
- **CI fallback.** Complete the implementation before using CI fallback. If a
  focused local gate cannot execute because the selected interpreter, runner,
  or declared dependency is unavailable, record the exact evidence, run the
  remaining checks, then push/open the PR and register it when the workflow
  provides registration. Required checks pending -> wait; red -> diagnose,
  fix, and push; green -> review readiness.
- **What counts as failure.** A test assertion, source-caused collection
  error, red CI result, or incomplete code is a failure, never unavailable
  infrastructure. No configured checks or an untrustworthy watcher after one
  bounded read -> block because no trustworthy seat remains. An optional
  browser-capability skip is informational and non-failing.
- **The full suite.** When the repository declares an authoritative full-suite
  CI gate, do not run the repository-wide suite locally merely to duplicate
  CI. Run the full suite locally only when no authoritative CI gate exists,
  the change crosses test/CI/harness infrastructure, the FnB explicitly
  requests it, or bounded diagnosis requires it. Never start a competing
  repository-wide suite on a shared host.',
  NULL,
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, seat=excluded.seat, is_deleted=0;

COMMIT;
