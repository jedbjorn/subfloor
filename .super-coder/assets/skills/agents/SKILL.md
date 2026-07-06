---
name: agents
description: --agents [model] — delegate work to spawned subagents under the system's discipline. Dev — execute a spec's task plan as implementer waves; reviewer — fan the three review axes out to an adversarial finding-panel. Overlay on spec/review; parent-only memory writes; AGENTS spawn ledger with a hard 6h validity window; parent-set timeouts. Load ONLY when the FnB invokes --agents.
category: craft
common: false
---

# agents — delegated waves under your discipline

The FnB invokes this as `--agents [model]`. It is an **overlay** on `spec`
(dev mode) and `review` (review mode): it changes only what is written here.
Everything upstream and downstream of the named steps — loading the spec,
task tracking, flags, the FnB handoff gate — is the base skill, unchanged.
Load the base skill first; apply this on top of it.

`[model]` sets the **worker tier**, passed through verbatim to the harness's
agent tool. No arg → agents inherit your model. Guidance is one line: heavier
judgment work warrants a heavier worker, and you may bump a single agent's
tier when a task is judged hard. You — the parent — never change tier; you
stay the judge.

**The core loop is `implement → you verify → adversarially refute → you
fix` — the refute step is where the quality comes from.** Parallel
implementers are an optional scale-up for genuinely large, file-disjoint
work, not the headline. The spend buys verification depth and an audit
trail, not wall-clock: your loop (compose → wait → adjudicate → re-verify)
is serial, and field runs measured hundreds of k of subagent tokens even
on small waves. Fit test before spawning: multi-surface, file-disjoint,
spec'd work with high correctness stakes → waves. A single-file or small
fix → run the base procedure solo; at most, spawn one adversarial skeptic
against your own diff — that is the cheap, high-ROI slice of this skill.

- **Harness:** subagent tooling exists in the claude harness only. No
  subagent tooling in your harness → this skill is inert; run the base
  procedure.
- **Not a workflow-script system.** No deterministic orchestration scripts —
  you spawn agents directly and stay in the loop between waves. Do not
  "upgrade" this to scripted workflows; the point is that you decide scale,
  batching, and prompts live, per this session's demands.

---

## The contract — four rules, non-negotiable

1. **You are the only memory writer.** Agents never run `sc mem` — no task
   status, no flags, no messages, no current_state, no narrative — and never
   `git push`, open PRs, or message shells. They return diffs and findings;
   you adjudicate and record. This keeps the shared DB coherent and leaves
   the reviewer's FnB handoff gate untouched.
2. **Prompt ingredients, not canned prompts.** You compose every agent
   prompt fresh, and it must carry: the spec excerpt / done-condition it
   serves, the exact file paths in play, the fork conventions that apply,
   the expected base commit (the agent verifies it via `git log -1` before
   editing and REPORTS a mismatch instead of silently proceeding), the
   deadline block (see the ledger check), and a required return shape.
3. **Isolation by conflict risk.** Concurrent writers on the same files —
   or any writer that must touch git state — each work in their own
   isolated worktree (writers never share a tree's index). A file-disjoint
   wave may share your tree, edits only: agents run no `git
   add`/`stash`/`checkout`/`commit`. Read *Worktree reality* below before
   reaching for isolation — it has real costs. Reviewer and checker agents
   are read-only; no isolation needed.
4. **Agent claims are inputs, not results.** Re-run the real check yourself
   — `./sc test`, lint, the spec's done-condition — before marking anything
   done. "Agent says tests pass" is not verification. Same for diffs: pull
   them yourself (`git -C <worktree> diff`); never adjudicate pasted diffs
   or pasted test output — pastes are lossy and unverifiable.

---

## Worktree reality — what isolation actually gives an agent

Harness worktrees are fresh trees, and two properties bite (both observed
on first fork runs — super-coder #303, #304):

- **They seed from the default branch (origin/main), not your branch
  HEAD.** In a stacked feature, a later-wave implementer authors — and
  "verifies" — against a base missing the earlier waves' commits. Hence
  the base-commit ingredient in contract rule 2, and hence: writers
  return diffs, you apply each one to YOUR tree with `git apply --3way`
  (note: it STAGES — inspect via `git diff HEAD`), and every check runs
  on the merged state.
- **They lack untracked toolchains.** No `node_modules`; sandboxed
  interpreters are typically mounted only into the primary worktree. An
  isolated agent often cannot run the app's suite at all. Say so in the
  prompt so it doesn't burn a turn rediscovering it, and treat its tree
  as an authoring surface: verification is yours, in your tree.

---

## The ledger check — before EVERY spawn, before acting on ANY result

The ledger is a single line embedded in current_state (one wave live at a
time, so one line is the complete record):

```
AGENTS wave=2/3 spawned=2026-07-06T14:32Z timeout=30m out=task4,task5
```

Review mode uses axis/lens names in `out=` (e.g.
`out=quality,edges,conformance,api-design`). Stamp `spawned=` from the
clock (UTC) at the moment you spawn — never recalled or recomputed from
context. Remove the line at wave close.

Execute this check verbatim; do not interpret it:

```
1. Read current_state.
   No AGENTS line → you may spawn. Write the AGENTS line,
   spawned=<now UTC>, in the same act as spawning.
2. AGENTS line present → age = now(UTC) − spawned.
3. age > 6h → the wave is DEAD. Unconditionally:
   a. Stop any agent still running.
   b. Discard their output UNREAD — do not apply, adjudicate, or "just
      check" it, even if it looks correct.
   c. Reconcile the task plan against reality: a task is done only if its
      diff is on the branch and verification passes NOW.
   d. Remove the AGENTS line; narrative: "wave expired (spawned <ts>);
      reconciled <n> tasks".
   e. Only now may current-session judgment start a NEW wave — fresh
      spawn, fresh timestamp.
4. age ≤ 6h → the wave is LIVE:
   - agents running → monitor; never spawn a duplicate for anything
     listed in out=.
   - agents not running (a prior session died) → their tasks revert to
     pending; respawning is a NEW wave: check no orphan diff already
     landed, then rewrite the AGENTS line with a fresh timestamp.
```

Every agent prompt ends with this deadline block, filled in:

```
Your deadline is <spawned + timeout> UTC. Past it, stop and return
partial results. If the current time is after <spawned + 6h>, do no
work — return immediately. Run all verification synchronously; never
end your turn waiting on a background task — your final message is
your only channel back.
```

The 6-hour window is a hard constant. You choose timeouts freely under it;
nothing extends it. Step 3b is deliberate: expired output is discarded even
when it looks correct — "looks correct" hours later against a moved tree is
exactly the trap. Step 3c recovers anything real: a diff that genuinely
landed and verifies passes reconciliation as done. Stale ledger text is
never evidence.

---

## Dev mode — overlay on `spec` Step 4

After the task plan exists (base skill, Steps 1–3, unchanged):

1. Classify pending tasks into **dependency waves** — independent tasks may
   run in parallel; dependent tasks sequence. When the ordering is
   non-obvious, use `blueprint` for the dependency read; a task plan that
   already encodes the order stands on its own.
2. Per wave: run the ledger check → mark each wave task `in_progress`
   (`sc mem task start`) → spawn one implementer per task (isolation per
   contract rule 3) → pull each returned diff yourself and apply it to
   your tree → spawn checker agent(s) prompted to **refute** it →
   adjudicate, run the real tests on the merged state → `sc mem task
   done` → update current_state → next wave.
3. One wave live at a time.

Stance amendment: `spec`'s "one task at a time" becomes "one **wave** at a
time" under `--agents`. Each task is still independently verified before it
is marked done — the spirit holds. Step 5 of `spec` (handoff on completion)
is unchanged and is yours, never an agent's.

## Review mode — overlay on `review` Step 2

Steps 1, 3, and 4 of `review` — loading the diff and its spec, flags, the
FnB-gated handoff — are unchanged. Agents never open flags.

1. Run the ledger check, then fan out **one agent per axis** (code quality /
   edge cases & gaps / spec conformance) **plus one per applicable lens**
   from the base skill's lens table. Each agent is read-only and returns
   candidate findings in a fixed shape:
   `file:line · claim · severity · how to reproduce`.
2. Dedupe the returns. For an uncertain finding, optionally spawn a skeptic
   prompted to refute it. Adjudicate every survivor yourself — re-read the
   code path; an agent's finding is a lead, not a verdict.
3. Proceed to base Step 3 with the adjudicated findings. The agents widen
   the search; you remain the gate.

---

## Monitoring

Agents cannot self-report (contract rule 1) — monitoring is your checkpoint
discipline, written to surfaces the FnB already watches:

| Surface | What it shows |
|---|---|
| task plan (`sc mem get tasks`) | live board — wave tasks flip `in_progress` at spawn, `done` at adjudication; the GUI Tasks tab renders it |
| `current_state` | the in-flight AGENTS ledger line, rewritten at every wave boundary |
| narrative | one line per inflection: wave landed, timeout, checker refuted an implementation |
| on demand | "status?" from the FnB → inspect your running agents' output, answer in two lines |

Honest limitation: mid-task granularity inside a single agent is only
visible by inspecting its output on demand. There is no per-agent progress
bar — giving agents a write surface would break rule 1.

## Timeouts

Set a timeout per agent at spawn, sized to the task, and record it in the
ledger line — the budget is visible, not private.

At expiry: inspect the agent's partial output → stop it → either respawn
with a **narrower** prompt (a timeout usually means the prompt was too
broad) or take the task inline.

**Two-strike rule:** a task whose agent times out twice is done inline by
you, full stop. No respawn loops. Every timeout gets a narrative line —
timeouts are signal about the plan's granularity.
