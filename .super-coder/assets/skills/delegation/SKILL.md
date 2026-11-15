---
name: delegation
description: Delegate small work to an `sc job` subagent or set a watcher; load before you launch, continue, stop or clean one. Covers who may launch, the rule that a subagent acts as you and you answer for it, harness, model and effort, the brief, reading a result, the limits and their refusals, and cleanup.
category: workflow
common: false
---

# delegation — subagents and watchers through `sc job`

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
- Whether your harness's native agents are an alternative depends on your
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
  omit it the engine sends no effort setting and the harness's own default for
  that model applies.
- Name the model and effort the FnB asked for, exactly. If a harness cannot
  carry what was asked, the launch is refused; say so and never substitute
  another model silently.
- For review, choose a model family different from the one that built the
  change.
- Check a selector before launch: `sc models resolve <harness> <selector>`.
  A model id the provider rejects is not caught at launch: the run starts,
  ends `failed`, and its log carries the provider's message.

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
  limit and the worktree. One launch per tool call. The run's end wakes you
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
the subagent's final message.

```bash
sc job result <id>          # the final message and the recorded facts
sc job result <id> --json   # the structured form
```

- The result shows at most 256 KiB and says when it was cut; the full text is
  `result.md` beside the run's log.
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
- `watch pr` wakes you when that PR's checks settle (green or red) or it is
  merged or closed. It reads every 60 seconds for at most 1 hour unless you
  say otherwise. Green means proven green at the head commit. Any shell may
  watch any PR it can read. The wake grants no merge authority: green is not a
  directive.
- `watch timer` wakes you once after the delay, which is at most 24 hours.
- A Developer's own PR still gets its facts from the PR watcher; do not
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
harness's native agents is set by your boot SEAT section. On any seat it adds
a launch on another harness built by the engine, a ledger row, and a run the
operator can see and stop.
