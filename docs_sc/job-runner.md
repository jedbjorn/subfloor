---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Session-surviving job runner (sc job)
roadmap_status: shipped
frozen: false
title: sc job — local jobs that survive your session
tags: [jobs, sprints, headless, eventing]
date: 2026-07-14
project: super-coder
purpose: How to run and wait on long local work with sc job
---

# sc job — local jobs that survive your session

## What it is

`./sc job` runs a long local command — a test suite, a bench, a build —
as a detached, supervised one-shot that outlives the session that
started it. When the job exits, its completion arrives as a `result`
row in YOUR inbox: the same wake-up path PR events use, so the sprint
loop needs nothing new to act on it.

Reach for it whenever work must outlive the turn. A harness background
task is session-scoped — in a headless (`-p`) boot it dies with the
session, silently. That failure killed benches and wedged a sprint for
four hours before this existed; the sprint skills now hard-ban parking
long work on one.

## The verbs

```
./sc job start [--label <slug>] [--timeout <sec>] -- <cmd ...>
./sc job list [--all]            # live jobs; --all includes finished
./sc job status <id>             # state · pid · exit · timestamps · log path
./sc job tail <id> [-n N]        # last N log lines (default 50)
./sc job wait <id> [--for <sec>] # bounded foreground wait (≤550s slice)
./sc job kill <id>               # SIGTERM→SIGKILL the whole process group
```

Job states: `running` · `done` · `failed` · `timeout` · `killed` ·
`lost` (supervisor died without recording an exit — reboot/SIGKILL;
check the log before trusting anything).

## The two ways to wait

**Fire-and-wake (default).** Start the job, report its id if someone
is waiting on it, end the turn. The completion `result` row wakes you
through the normal inbox path — nothing polls, nothing is parked on
the session.

**Wait-slice (the result decides this turn's next step).**
`./sc job wait <id>` blocks up to 550 seconds in the foreground and
exits `0` = finished (status line printed) or `2` = still running.
Between slices, drain your inbox (`sc mem message check`) and act on
what landed, then slice again. Exit `1` = no such job / lost.

## Timeouts and stuck jobs

Always pass `--timeout` for anything that can wedge: the supervisor
SIGTERMs the job's whole process group at the deadline (SIGKILL after
a grace period), records `timeout`, and still sends the completion
row. A deadlocked suite becomes a bounded failure with a wake-up, not
a silent hole. `kill` is the manual version, same group-kill, same
completion row.

## How it works

`start` writes `<engine>/run/jobs/<id>/` (`meta.json` + `log`) and
spawns a small supervisor in its own session. The supervisor spawns
the command as its own process group, streams combined stdout+stderr
to the log, waits, records the exit in `meta.json`, and posts one
completion message to the starting shell's own inbox through the
engine API — kind `result`, stamped with a `dedupe_key` so a retry
never double-sends. If the API is down it retries briefly and gives
up: `meta.json` is the durable record; `status`/`wait` read it without
the API.

No DB surface beyond that one message. Jobs are per-shell and
engine-local; the message bus is the only fleet-visible part. Spec:
`specs_sc/job-runner.md` (frozen).
