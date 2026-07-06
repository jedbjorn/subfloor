---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Agents skill — delegated waves
roadmap_status: shipped
frozen: false
title: agents — delegated waves
tags: [skill, orchestration, dev, reviewer]
date: 2026-07-06
project: super-coder
purpose: Agent-delegation skill for dev + reviewer
---

# `agents` — delegated implementation & review

## Overview

One new engine skill, `agents`, granted to the **dev** and **reviewer**
flavors. Invoked by the FnB as `--agents [model]`, it lets the parent shell
delegate work to spawned subagents: implementer waves when executing a spec,
adversarial finding-panels when reviewing a diff.

It is written as an **overlay** on the existing `spec` and `review` skills —
it states only what changes when agents are in play, and never restates their
procedure. Everything upstream and downstream (loading the spec, task
tracking, flags, the FnB handoff gate) is byte-for-byte the base skill.

> [!class1]
> Design stance: the skill constrains **structure** (contract, checkpoints,
> ledger, timeout floor); the parent model decides **scale and content**
> (agent count, batching, prompts, worker tier) per the session's demands.
> Session demands vary; the parent is the judge.

Explicitly **not** a workflow-script system: no deterministic orchestration
scripts. The parent spawns agents directly and stays in the loop between
every wave. A shell must not "upgrade" this to scripted workflows.

## Problem

- A dev shell executes spec tasks strictly one at a time; independent tasks
  serialize even when they touch disjoint surfaces.
- A reviewer reads a large diff alone; breadth costs depth on the three axes.
- Generic multi-agent workflows exist in the claude harness, but they are
  uncontrolled for our system: agents could write memory, open flags, push
  git — bypassing the parent-adjudication and FnB gates that keep the shared
  DB coherent.
- Stale spawn state (a resumed or compacted session finding an old "agents
  out" note) can accidentally re-trigger agents against a moved tree.

The skill exists to make delegation available **inside** the system's
discipline, with retrigger protection spelled out mechanically.

## The contract

Four rules, shared by both modes. These are the skill's core and are
non-negotiable.

1. **The parent is the only memory writer.** Agents never run `sc mem` — no
   task status, no flags, no messages, no current_state, no narrative — and
   never `git push`, open PRs, or message shells. They return diffs and
   findings; the parent adjudicates and records. This preserves the
   reviewer's FnB handoff gate untouched.
2. **Prompt ingredients, not canned prompts.** The skill lists what every
   agent prompt must carry — the spec excerpt / done-condition, exact file
   paths, the fork's conventions, the deadline line (see Spawn ledger), and
   a required return shape. The parent composes each prompt fresh.
3. **Isolation by role.** Parallel implementers each get an isolated
   worktree (writers never share a tree — same principle the dev-worktrees
   feature shipped for shells). Reviewer/checker agents are read-only and
   need none.
4. **Agent claims are inputs, not results.** The parent re-runs the real
   check itself (`./sc test`, lint, the done-condition) before marking
   anything done. "Agent says tests pass" is not verification.

## Dev mode

Overlay on `spec` **Step 4** (Track). After the task plan exists:

1. Classify pending tasks into **dependency waves** — independent tasks may
   run in parallel; dependent tasks sequence. (Use `blueprint` for the
   dependency read; don't reimplement it.)
2. Per wave: run the ledger check (see Spawn ledger & validity) → mark each
   wave task `in_progress` → spawn one implementer per task (worktrees if
   more than one) → on each returned diff, spawn checker agent(s) prompted
   to **refute** it → adjudicate, apply, run the real tests → `task done` →
   update current_state → next wave.
3. One wave live at a time.

```linear
Ledger check :::class1 -> Spawn wave :::class2 -> Adjudicate + verify :::class2 -> Tasks done :::class3 -> Next wave :::class1
```

Stance amendment, stated in the skill: `spec`'s "one task at a time" becomes
"one **wave** at a time" under `--agents`. Each task is still independently
verified before it is marked done — the spirit holds.

## Review mode

Overlay on `review` **Step 2** (the three axes). Steps 1, 3, 4 — diff+spec
load, flags, the FnB gate — are unchanged, and agents never open flags.

1. Ledger check, then fan out **one agent per axis** (code quality / edge
   cases & gaps / spec conformance) **plus one per applicable lens** from
   the base skill's lens table, each read-only, each returning candidate
   findings in a fixed shape:
   `file:line · claim · severity · how to reproduce`.
2. The parent dedupes, optionally spawns a skeptic per uncertain finding
   (prompted to refute it), and adjudicates every survivor itself.
3. Proceed to base Step 3 with the adjudicated findings. The agents widen
   the search; the Reviewer remains the gate.

## Monitoring

Agents cannot self-report (rule 1) — monitoring is the parent's checkpoint
discipline, written to surfaces the FnB already watches. No new surface, no
GUI change.

| Surface | What it shows |
|---|---|
| `spec_tasks` | live board: wave tasks flip `in_progress` at spawn, `done` at adjudication — GUI Tasks tab renders it |
| `current_state` | the in-flight `AGENTS` ledger line (see below), rewritten at every wave boundary |
| narrative | one line per inflection: wave landed, timeout, checker refuted an implementation |
| on demand | "status?" from the FnB → parent inspects running agents' output, answers in two lines |

Honest limitation, stated in the skill: mid-task granularity inside a single
agent is only visible via the parent inspecting its output on demand. There
is no per-agent progress bar — giving agents a write surface would break
rule 1.

## Timeouts

The parent sets a timeout per agent at spawn, sized to the task, and records
it in the ledger line — the budget is visible, not private.

At expiry: inspect the agent's partial output → stop it → either respawn
with a **narrower** prompt (timeout usually means the prompt was too broad)
or take the task inline.

> [!class4]
> **Two-strike rule:** a task whose agent times out twice is done inline by
> the parent, full stop. No respawn loops. Every timeout gets a narrative
> line — timeouts are signal about the plan's granularity.

## Spawn ledger & validity

The retrigger guard. This section lands in the skill as a **verbatim
mechanical block** — the spawning model executes it, it does not reason
about it.

**The ledger** is a single line embedded in current_state (one wave live at
a time → one line is the complete record):

```
AGENTS wave=2/3 spawned=2026-07-06T14:32Z timeout=30m out=task4,task5
```

Review mode uses the same format with axis/lens names in `out=`. The parent
stamps it at spawn (UTC, from the clock — never recalled from context) and
removes it at wave close.

**The check** — runs before ANY spawn and before acting on ANY agent
result. No exceptions, no interpretation:

```
1. Read current_state.
   No AGENTS line → you may spawn. Write the AGENTS line, spawned=<now UTC>,
   in the same act.
2. AGENTS line present → age = now(UTC) − spawned.
3. age > 6h → the wave is DEAD. Unconditionally:
   a. Stop any agent still running.
   b. Discard their output UNREAD — do not apply, adjudicate, or "just
      check" it, even if it looks correct.
   c. Reconcile spec_tasks against reality: a task is done only if its diff
      is on the branch and verification passes NOW.
   d. Remove the AGENTS line; narrative: "wave expired (spawned <ts>);
      reconciled <n> tasks".
   e. Only now may current-session judgment start a NEW wave — fresh spawn,
      fresh timestamp.
4. age ≤ 6h → the wave is LIVE:
   - agents running → monitor; never spawn a duplicate for anything in out=
   - agents not running (prior session died) → their tasks revert to
     pending; respawning is a NEW wave: rewrite the AGENTS line with a
     fresh timestamp first, after checking no orphan diff already landed.
```

**In-agent backstop** — every agent prompt carries its deadline verbatim:

```
Your deadline is <spawned + timeout>. Past it, stop and return partial
results. If the current time is after <spawned + 6h>, do no work — return
immediately.
```

> [!class4]
> Step 3b is deliberate: expired output is discarded even when it looks
> correct — "looks correct" six hours later against a moved tree is exactly
> the trap. Step 3c recovers anything real: a diff that genuinely landed and
> verifies passes reconciliation as done. Stale ledger text is never
> evidence.

The 6-hour window is a hard constant. The parent chooses timeouts freely
under it; nothing extends it.

## Harness & models

- **Harness:** subagent tooling exists in the claude harness only. The skill
  states: no subagent tooling in your harness → this skill is inert; run the
  base `spec` / `review` procedure. No grant gating needed.
- **Models:** the skill text names **zero** models and no tier tables.
  `--agents [model]` passes the worker tier through verbatim to the harness;
  no arg → agents inherit the parent's model. Guidance is one line: heavier
  judgment work warrants a heavier worker; the parent may bump a single
  agent's tier when a task is judged hard. (Operationally we run Anthropic
  models only for now — an informal fact, kept out of skill text so it can't
  go stale.)

## Surfaces to change

1. **Migration `00XX_agents_skill.sql`** — INSERT the `agents` row into
   `skills` (category `craft`, full skill body in `content`); INSERT
   `shell_skills` grants for existing shells with `flavor IN
   ('dev','reviewer')`.
2. **Flavor templates** — add `"agents"` to the `skills` arrays of
   `templates/shells/dev.json` and `templates/shells/reviewer.json` (new
   shells).
3. **Reseed pointers** — same migration reseeds `spec` (one line at Step 4)
   and `review` (one line at Step 2): "if granted `agents` and the FnB
   invokes `--agents`, its overlay applies here."
4. **Render** — `sc render` emits `skills_sc/agents.md`; snapshot per the
   normal pipeline.

No schema change, no new tables, no API change, no GUI change.

## Done condition

- `agents` skill row seeded; body carries: the four contract rules, both
  mode overlays, the monitoring table, the timeout rules, and the verbatim
  ledger check + in-agent backstop.
- Existing dev/reviewer shells hold the grant; new ones get it from the
  templates.
- `spec` and `review` carry their one-line overlay pointers.
- `skills_sc/agents.md` renders; migration applies cleanly on a fork via the
  standard update path.
