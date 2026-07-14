---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Session-surviving job runner (sc job)
roadmap_status: shipped
frozen: true
title: sc job — session-surviving job runner
tags: [jobs, sprints, headless, eventing, supervision]
date: 2026-07-14
project: super-coder
purpose: Local long-running work that survives the session that started it
---

# sc job — session-surviving local job runner

## Overview

`./sc job` runs a long local command — a test suite, a bench, a build —
as a **detached, supervised process** that survives the harness session
that started it, and reports its completion as a `result` row in the
starting shell's own inbox. The existing eventing loop (inbox watcher,
headless boots on message rows) then covers local long jobs exactly the
way it already covers PR transitions: the process outlives the session;
the completion row wakes the shell.

```
./sc job start [--label <slug>] [--timeout <sec>] -- <cmd ...>
./sc job list [--all]
./sc job status <id>
./sc job tail <id> [-n N]
./sc job wait <id> [--for <sec>]
./sc job kill <id>
```

## Problem

The dos-arch sprint-221 report's recurring failure (~6 occurrences):
sessions ended turns with suite runs, benches, and watches parked on
harness background tasks. In a headless (`-p`) session those tasks die
with the session — "the harness will wake me" is false. Consequences
observed in one sprint: a bench that died silently and contaminated a
measurement decision; a 4-hour wedge on a deadlocked local suite; an
evolving stack of hand-rolled mitigations (detached runs with manual
kill-0 poll slices, drain-inbox-between-slices discipline), each shell
reinventing its own waiter — one with a self-match bug that masked the
dead bench.

> [!class1]
> A harness background task is session-scoped. Anything that must
> outlive the turn — a suite, a bench, a build — must not be parked on
> one. `sc job` is the engine-owned answer; hand-rolled `nohup` + poll
> loops are the thing it replaces.

## Job lifecycle

`start` spawns a small **supervisor** (`job.py _supervise <dir>`) in a
new session (`setsid`), which spawns the command as its own process
group, then:

1. records the child pid in `meta.json`,
2. streams combined stdout+stderr to `log`,
3. waits for exit (or kills the group at `--timeout` seconds),
4. writes `exit_code` + `finished_at` to `meta.json`,
5. posts ONE completion message to the starting shell's own inbox via
   the engine API — kind `result`, body one line:
   `job <id> (<label>) exited <code> after <duration> — log: <path>`,
   stamped with a `dedupe_key` so an API retry never double-sends.

The message is the wake-up, not the payload — detail lives in
`sc job status` / `tail`. If the API is unreachable at completion the
supervisor retries briefly, then gives up: `meta.json` still holds the
result, `status`/`wait` still see it. The completion row is the fast
path, never the only path.

State lives under `<engine>/run/jobs/<id>/` — `meta.json` (cmd, label,
cwd, owner shortname, timestamps, pid, exit) + `log`. No DB schema
changes: the only DB surface is the existing `shell_messages` bus, and
the supervisor is the one writer, through the API like any shell-side
actor (token from the environment it inherited at `start`).

## The two wait patterns

- **Event-driven (preferred, planner-visible):** `job start`, report
  the job id in your `result`/board row, end the turn. The completion
  row wakes the shell through the normal inbox path. Nothing polls.
- **Wait-slice (when the result decides THIS turn's next step):**
  `job wait <id> --for <sec>` blocks in the foreground up to `--for`
  seconds (default 300, cap 550 — under harness foreground-timeout
  limits). Exit 0 = finished (status line printed); exit 2 = still
  running. Between slices: drain your inbox, then slice again. The
  canonical waiter every shell was hand-rolling, once, correctly.

> [!class4]
> `job wait` exit codes: 0 done · 2 still running · 1 no such job.
> A wait-slice loop that never drains the inbox between slices
> reproduces the stale-state failure the sprint hit — the slice is
> bounded exactly so the inbox gets read.

## Timeouts and the wedge case

`--timeout <sec>` arms the supervisor to SIGTERM (then SIGKILL after a
grace period) the whole process group and report
`exited timeout(-15) …`. The sprint's 4-hour deadlocked pytest wedge is
the motivating case: a wedged suite becomes a bounded failure with a
completion row, not a silent hole in the sprint.

`job kill <id>` is the manual form: SIGTERM to the group, recorded as
killed, completion row still posted (by the supervisor, which survives).

## Surfaces to change

| Surface | Change |
|---|---|
| `scripts/job.py` | new — verbs + the supervisor |
| `sc` dispatcher | `job)` arm + help stanza |
| `run/jobs/` | new state dir (gitignored with the rest of `run/`) |
| `shell_messages` | none — existing kind `result` + `dedupe_key` |
| skills (`sprint`, `sprint_orchestration`) | teach: long local work goes through `sc job`; wait-slice + drain-inbox discipline (separate reseed) |
| `tests/test_job.py` | new — lifecycle, wait codes, timeout, completion row |

## Non-goals

- **Fleet-wide job registry.** Jobs are per-shell, state is
  engine-local to the worktree that started them; the completion row on
  the message bus is the fleet-visible surface. A cross-shell `job list`
  is a later feature if wanted.
- **Supervision beyond one shot.** No restarts, no cron, no queues —
  a job runs once and reports once. The brokers own long-lived services.
- **Replacing CI.** CI-vs-CI on the same runner stays the decision
  number for measurements (sprint-221 ruling); `sc job` exists so local
  runs that do happen can't die silently.

## Done condition

A headless sprint worker starts a 30-minute suite with
`./sc job start --timeout 3600 -- …`, ends its turn immediately, and
the suite keeps running. On exit the worker's inbox gets one `result`
row; the planner's next boot of that shell acts on the outcome.
`job wait` covers the in-turn case in ≤550s slices with inbox drains
between. A wedged run dies at its timeout with a completion row instead
of wedging the sprint. Zero scheduled polling anywhere.
