---
name: cli_orchestration
description: Planner-run delivery from a terminal seat; load when the FnB assigns work in this chat for you to orchestrate with CLI agents. You stay the oversight layer while native or cross-harness agents verify the spec, build, review, deploy to Dev, and verify. Covers the kickoff contract, agent launch per harness (Claude, Codex, Kimi), terminal-seat PR/CI monitors, the FnB's merge and deploy directives, acceptance review, and cleanup.
category: workflow
common: false
---

# cli_orchestration — run a delivery from the terminal

Use when the FnB assigns a spec, task set, or work unit "in this chat" and
wants you to orchestrate it with your harness's own agents. You are the
oversight layer: you brief, launch, monitor, verify handoffs, and surface
questions. Agents do the work. Keep your context window for the FnB and you.

## Seat gate

The launch and monitor commands below need a terminal seat: boot `## SEAT`
says "You hold a terminal". On the GUI seat your native background agents and
commands die when your turn ends, so do not use them there. Load the
`delegation` skill instead: `sc job agent` launches each agent on any harness
and wakes you when it ends, and `sc job watch pr` replaces the PR monitors.
Your lane, the kickoff contract, the lane order, the brief and the close in
this skill still apply.

## Your lane

- Do not write code or propose fixes. Route findings to the agent that owns
  the change. Offer an opinion only when the FnB asks for one.
- Delegate reading as well as writing. Have agents read the large sources
  and report only conclusions, identifiers, and evidence paths.
- Surface every question that could change scope, acceptance, or authority
  to the FnB, verbatim and with the agent's evidence. Do not answer it
  yourself, and do not let agents choose for the FnB.
- You own the durable writes: `sc mem state`, narrative, flags, decisions,
  and task status. Agents report back to you; they do not write `sc mem`.
- Never accept an agent's report as proof. Verify each claim against the
  durable artifact: the branch on the remote, the PR, check results, and
  receipts or evidence files.

## Kickoff contract

1. Load the assignment with `sc context --task <id>` or
   `--work-unit <id>`. Read the spec's tasks and run the version-control sync
   from your boot.
2. Restate the assignment in one block: the work, the lanes, the model and
   effort per lane, the monitors, the verification seat, and what stays with
   the FnB.
3. Ask the open questions before launching anything, and ask them in one
   batch:
   - Spec freshness. The spec may name sites that have moved on main. Offer
     a spec-verification agent before dev starts.
   - Merge authority. This loop runs outside an armed Sprint, so your boot's
     merge gate applies in its first form: merge only on an explicit FnB
     directive that identifies the PR. The directive may name the PR by
     number, or name an assignment and the condition its PRs must meet (for
     example "merge this assignment's PRs once green and review clean"); that
     kind covers only that assignment's PRs, and only once the stated
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
5. Merge and deploy, only under the FnB's recorded directive. Run
   `gh pr merge <n> --squash` (or the repo's convention) on a PR the directive
   covers, once its condition holds: checks green and review clean unless the
   FnB said otherwise. Deploy only to the directed target, using the fork's
   dev_kit or remote-seat procedure.
6. Acceptance review. Before any verification run, show the FnB the method
   and pass condition for each criterion. Change them only with the FnB's
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
or `kimi -m <alias> -S <session_id> -p "<follow-up>"`. Kimi's session id has
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

On the terminal seat, set up your own monitors: no engine service watches a
PR for you here. This is specific to this loop on this seat. On the GUI seat
and inside a Sprint the engine delivers PR facts as wakes, and your boot's
"never poll GitHub" rule holds.

- **PR checks:** `gh pr checks <n> --watch --interval 60` as a background
  command; it exits when the checks settle. Read the result with
  `gh pr view <n> --json state,mergeable,reviewDecision,statusCheckRollup`.
- **Merge conflicts:** an open PR whose `mergeable` is `CONFLICTING` belongs
  to its developer. Route it back.
- **Stalls:** if a child's log has stopped growing past the expected window,
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
  for each criterion, what remains pending, and the decisions that are theirs.
