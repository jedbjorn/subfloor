---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprint eventing — GitHub→inbox daemon + headless worker boot
roadmap_status: shipped
frozen: true
title: Sprint eventing — daemon + headless boot
tags: [sprints, messaging, daemon, orchestration, cross-provider]
date: 2026-07-12
project: super-coder
purpose: Event-driven sprint coordination
---

# Sprint eventing — GitHub→inbox daemon + headless worker boot

## Overview

Sprints currently coordinate by polling: every participant runs its own PR
tracker against GitHub, and a test that goes green sits unseen until the next
poll fires — in practice a ~10-minute delay per transition, multiplied across
every unit in the chain. This spec replaces that with an event-driven loop
built on the surface we already trust: the `shell_messages` bus.

Four pieces, one direction of flow:

1. **Message kinds** — a `kind` column on `shell_messages` so the trail is
   filterable: `shell` (default), `task`, `result`, `pr_event`.
2. **GitHub watcher daemon** — ONE poller for the whole fork. Watches
   registered PRs, translates transitions (checks concluded, review submitted,
   merged/closed) into `pr_event` rows addressed to the owning shell.
3. **Headless boot** — `./sc run <shortname> [-p "<prompt>"] [--harness <h>]
   [-m <model>]`: boot a shell non-interactively to drain its inbox and act,
   on any matrix harness (claude · codex · opencode). Workers become
   ephemeral, per-task sessions.
4. **Inbox watcher** — a zero-token blocking watcher armed in the planner's
   live session; wakes it the moment any message row lands.

Plus one governance seam the declaration gains: a two-question **model &
provider interview** (devs, reviewers) whose answers ride the sprint doc and
parameterize every `sc run` of the sprint.

> [!class1]
> Design constraint (FnB, 2026-07-12): every instruction and result flows
> through `shell_messages` rows — the durable, queryable data trail is a
> first-class requirement and the stated reason this is built on the message
> bus rather than harness-internal agent orchestration.

## Problem

Three costs in the current sprint shape — the first is the one driving this
spec:

- **Context burn.** A tracker poll is not free: every scheduled wake is a
  full harness turn, and a turn re-submits the shell's entire accumulated
  context — boot doc, code reads, the whole session so far — to the provider
  to learn, usually, that nothing changed. A sprint parks dev and reviewer
  shells on watch all night: N shells × a wake every ~10 minutes × a full
  context window each is a token bill that scales with idle hours, not with
  work. Long-lived dev sessions compound it — context grows at code density
  across the sprint, and every poll re-pays the ever-larger window.
- **Latency.** Each shell's tracker polls on its own schedule. A green CI run
  waits for the next poll; a chain of N units pays that delay N times.
- **Redundancy.** Every participant polls GitHub separately — N shells, N
  pollers, N credentials exercised, for the same event stream.

The planner is the exception, deliberately: it manages — reads messages,
writes messages, boots workers — and never loads code. Its context grows at
coordination density. It stays long-lived; workers don't.

## Message kinds

Additive migration on `shell_messages`:

```sql
ALTER TABLE shell_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'shell'
  CHECK (kind IN ('shell','task','result','pr_event'));
```

- `shell` — ordinary shell-to-shell mail (today's traffic; the default keeps
  every existing row and writer valid).
- `task` — planner → worker instruction. The kickoff messages the sprint
  skills already send become `task` rows.
- `result` — worker → planner completion report.
- `pr_event` — daemon → shell GitHub transition.

`sc mem message send` grows `--kind` (default `shell`); `check` output shows
the kind. The sprint trail becomes one query:
`SELECT * FROM shell_messages WHERE kind != 'shell' ORDER BY created_at`.

## Watched PRs + daemon

New table — the subscription registry and the daemon's diff state in one:

```sql
CREATE TABLE watched_prs (
    watch_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    repo           TEXT    NOT NULL,          -- owner/name
    pr_number      INTEGER NOT NULL,
    shell_id       INTEGER NOT NULL REFERENCES shells(shell_id),
    last_seen      TEXT,                      -- JSON: checks/review/state fingerprint
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at      TEXT,                      -- set on merge/close; NULL = live
    UNIQUE (repo, pr_number, shell_id)
);
```

**Registration** is explicit, never inferred from branch names:

- `./sc watch pr <owner/repo> <n> [--shell <shortname>]` — manual; defaults
  to the calling shell.
- The `sprint` skill registers the PR in the same step that opens it; the
  planner may register any PR for itself.

**The daemon** (`sc`-managed process, same supervision model as the existing
brokers: `./sc launch` brings it up, `./sc down` stops it):

- Polls on a 60–90s loop. One batched GraphQL query covers every live watch —
  at authenticated rate limits (5,000 req/hr) this is ~1% of budget.
- Diffs each PR against `last_seen`; on transition, INSERTs a `pr_event` row
  to the watch's `shell_id` and updates `last_seen`. Events: check suite
  concluded (green or red), review submitted, merged, closed.
- On merge/close: emit the final event, set `closed_at` — the watch retires
  itself; no leaked watchers to tear down at sprint close.
- The daemon only ever writes message rows. It never boots shells, never
  marks anything read, never touches git.

> [!class4]
> Body caps stay honest: a `pr_event` body is one line — repo, PR, what
> changed, and the head SHA. Detail lives in `gh`; the message is the wake-up,
> not the payload.

## Headless boot

`./sc run <shortname> [-p "<prompt>"] [--harness <h>] [-m <model>]` — the
same render-then-exec path as the interactive launcher, minus the picker and
the TTY:

- Renders the shell's boot CLAUDE.md exactly as `./sc enter` would, then
  execs the harness non-interactively via a per-harness adapter:

| Harness | Headless exec | Model flag |
|---|---|---|
| **claude** | `claude -p "<prompt>"` | `--model` |
| **codex** | `codex exec "<prompt>"` | `-m` |
| **opencode** | `opencode run "<prompt>"` | `-m <provider>/<model>` |

- All three stream a final message and exit — scriptable by construction.
  Approval prompts are disabled in headless runs (codex: approvals off;
  claude/opencode: the permission posture the sandbox already grants); the
  docker sandbox stays the safety boundary, exactly as in interactive runs.
- Harness + model resolve in order: explicit flags → the sprint doc's
  `models:` line (interview, below) → the shell's `flavor_defaults`. Vibe is
  out of scope (see Non-goals).
- Default prompt when `-p` is omitted: *"Check your inbox and act on your
  unread messages."* The messaging skill's boot stance already makes a
  non-zero inbox the first thing a shell surfaces — the prompt just starts
  the turn.
- Liveness guard: refuse to boot a shell that already has a live session
  (`shell_liveness` is the existing line to build on). One shell, one
  session.
- The session writes memory, archives, and messages exactly as an
  interactive one — an ephemeral worker is still the same shell; its rows
  accrete across boots.

This is what makes workers resettable for free: the planner doesn't reset
anything — it boots a fresh session per task, the worker replies with a
`result` row and exits, and no context is ever carried between tasks.

## Models & providers — the declaration interview

Model doctrine today lives at the operator's launch seam (`flavor_defaults`
+ the picker). Sprints boot workers headlessly — no picker — so the seam
moves to the declaration: when the planner declares a sprint, it asks the
FnB exactly two questions:

1. **Devs** — which harness and model? One answer; every dev in the sprint
   runs it.
2. **Reviewers** — which harness and model? One answer; every reviewer runs
   it.

Flavor-uniform by design: shells of a flavor are interchangeable workers,
and one answer per flavor keeps the board readable and the review lineage
coherent — reviewers stay a *different lineage* from the code they gate
(the existing doctrine, now chosen per sprint instead of per boot).

The answers land in the sprint doc header —

```
models: devs=<harness>/<model> · reviewers=<harness>/<model>
```

— and every `sc run` the planner issues for the sprint carries them. No
answer → `flavor_defaults`, unchanged.

The planner itself is not interviewed — it is already booted. **Strong
recommendation, not a gate: run the planner on Claude (Fable).** The
planner is the low-volume, high-leverage reasoning seat, the one long-lived
context in the loop, and the only role the inbox watcher (claude-only)
fully serves; the research below also favors Anthropic models in the
orchestrator seat over mixed worker fleets. Any harness *works* — wake
latency and ergonomics degrade, correctness doesn't. On ship, the README's
*Sprints* and *Harnesses & models* sections state the recommendation.

## Cross-provider coordination — research inputs

Field reports on one model orchestrating workers from other providers
converge on a short list of failure modes ([86-session orchestrator
lessons](https://dev.to/ji_ai/building-a-multi-agent-llm-orchestrator-with-claude-code-86-sessions-of-hard-won-lessons-13n6),
[Microsoft Conductor](https://opensource.microsoft.com/blog/2026/05/14/conductor-deterministic-orchestration-for-multi-agent-ai-workflows/),
[bag-of-agents error analysis](https://towardsdatascience.com/why-your-multi-agent-system-is-failing-escaping-the-17x-error-trap-of-the-bag-of-agents/)).
Each is either already answered by this design's architecture or becomes a
rule here:

| Observed failure | This design |
|---|---|
| Model-driven routing is fragile — sequencing left to LLM inference is non-repeatable | Sequencing is data: the sprint doc's dependency column + `task` rows. The planner fills a deterministic structure; it doesn't improvise one. |
| No implicit context sharing — workers rediscover the same facts and re-find the same bugs | Explicit injection is the only channel: a `task` row carries the pointer set (sprint doc id, unit seq, spec doc, branch); the DB render is the shared memory every boot reads. |
| Prompt dialect drift — instructions tuned for one model silently underperform on another | `task` bodies stay model-neutral and constraint-explicit: point at the spec and the skill, don't restate them in planner phrasing. Constraints live in specs, which every lineage reads the same way. |
| Worker self-reports get trusted — "done" accepted as done | Already sprint posture: a `result` row is a wake-up, never the verdict — green checks + the reviewer gate are the only ground truth. |
| Overnight parallel workers exhaust metered credits | The README's subscription-plan posture, plus this spec's whole point: ephemeral workers + zero scheduled polling make idle hours cost ~nothing. |
| The coordinator's own context becomes the (drifting) source of truth | The board + message table are the state; a rebooted planner replays rows and loses nothing. |

## Inbox watcher

The planner-side replacement for per-shell PR trackers: one background
process, armed once per session, that blocks until the shell has unread
messages and exits — the harness wakes the shell on exit. A 30s internal
poll against SQLite costs zero tokens; the shell is only ever woken by an
actual message. Re-arm after draining the inbox.

Claude-harness only, inert elsewhere (precedent: the `agents` skill). Other
harnesses keep the task-boundary inbox check the sprint skills already
mandate — correctness is identical, latency degrades gracefully.

## The loop, end to end

```linear
Planner sends task row :::class1 -> sc run dev (headless) :::class2 -> Dev reads inbox, builds, opens PR + watch :::class2 -> CI concludes :::class3 -> Daemon writes pr_event to planner :::class3 -> Watcher wakes planner :::class1 -> Planner books reviewer / next unit :::class1
```

Every arrow that matters is a row in `shell_messages`. The board stays the
planner's summary; the message table is the ground truth a rebooted shell —
or the FnB — can replay.

## Surfaces to change

| Surface | Change |
|---|---|
| `schema.sql` + migration | `shell_messages.kind`; `watched_prs` |
| `sc mem message` | `--kind` on send; kind in `check` output |
| `sc watch` (new) | `pr` verb — register/list watches |
| daemon (new script) | poll loop, GraphQL batch, event emit; wired into `./sc launch`/`down` |
| `sc run` (new verb) | headless render+exec; liveness guard; `--harness`/`-m` flags; claude/codex/opencode adapters |
| `sprint` skill | register watch on PR open; drop per-dev tracker |
| `sprint_orchestration` skill | arm inbox watcher; drop planner tracker; kickoff/report messages become `task`/`result`; declaration gains the two-question model interview + the `models:` header line |
| README (at ship) | *Sprints* + *Harnesses & models* gain the sprint interview and the Fable-planner recommendation |
| tests | daemon diff/emit logic; watch registration; `kind` constraint; `sc run` harness/model resolution order |

Skill rewires land last, in the same feature — the daemon must be proven
before the skills stop telling shells to poll.

## Non-goals

- **Webhooks.** A localhost fork has no exposed endpoint; the 60–90s poll is
  within the latency budget. Revisit only if polling ever isn't.
- **Daemon-initiated boots.** The daemon writes rows; only the planner (or
  the FnB) boots shells. Autonomous wake is a separate, deliberate decision.
- **Vibe headless workers.** Vibe takes no model from the launch seam — it
  self-selects via `active_model` in its own config — so the interview
  cannot parameterize it. It stays an interactive option; revisit if it
  grows a model flag.
- **Per-unit model mixing.** One answer per flavor, for the whole sprint.
  Mixing models within a flavor is complexity with no driver — the
  interview covers the real need, provider choice per role.
- **Hard planner-model enforcement.** Fable-as-planner is a strong
  recommendation in the README and skill text, never a gate — any harness
  can hold the planner seat, at degraded wake latency.
- **Threading / read-receipts beyond `read_at`.** The bus stays flat.
- **Multi-operator auth.** Single-operator fork assumptions hold, as in
  messaging v1.

## Done condition

A sprint unit can go task → build → PR → green → review → merge with **zero
scheduled polling by any shell**: the planner is woken by rows, workers are
booted per task and exit on `result`, and the full coordination history is
reconstructable from `shell_messages` alone. Cross-provider is first-class:
the same sprint runs with devs on one harness and reviewers on another,
exactly as the declaration interview chose, every `sc run` honoring it.
