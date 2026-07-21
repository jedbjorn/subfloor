# REVIEW: Sprint 21 unit 2 — session supervisor + ownership leases

**Sprint:** doc #21 · **Spec:** doc #20 (feature 14) · **Task:** #51 · **PR:** #456
(`feat/session-supervisor-leases` @ `3d110dd`, base `main` @ `a15579b`)
**Reviewer:** REV2 · 2026-07-21 · CI: all checks green at review time.

## Scope reviewed

`session_supervisor.py` (new, 538 lines), `run.py` integration (supervise
replaces `execvpe`, `--session-binding` resume, lease hooks, binding creation),
`tests/test_session_supervisor.py` (new, 486 lines), `test_style_spinner.py`
(mock swap). Reviewed against spec doc #20 (state model, class4 one-writer
rule, failure table, delivery-plan step 2) and issue #439.

## What holds up (verified, not trusted)

- **#439 correction is real.** Signal forwarding targets the child's process
  group; `terminate_group` reaps daemonized descendants after leader exit; a
  dead leader with surviving group members is fenced (`orphan-group` →
  `LeaseConflict`, binding → `error`), never silently adopted. The
  descendant-group cancellation test uses real processes and a real SIGTERM —
  it would catch a regression to leader-only signalling.
- **Lease discipline.** `/proc` stat parsing is correct (last-`)` seam; pgrp
  field 5 → index 2, starttime field 22 → index 19). Claims are
  `BEGIN IMMEDIATE` + CAS on both state (unit 1's `transition_binding`) and
  `lease_generation`, with a live two-thread contention test on a file DB.
  PID reuse is invalidated by start ticks; an old generation cannot release a
  new owner (tested).
- **Interaction checks I ran myself:**
  - `sc job` supervisors spawn with `start_new_session=True` (job.py:207,289)
    — their own session/group, so the new `terminate_group` does **not** kill
    session-surviving jobs.
  - `shell_liveness` identifies sessions by harness comm + worktree cwd, not
    wrapper PID — the guard still sees a surviving harness child if the
    supervisor is SIGKILLed.
  - **No-controlling-terminal probe:** all four installed harness binaries
    reference `/dev/tty`, so I spawned each TUI with `start_new_session=True`
    on a pty (exactly what `supervise()` produces). claude, codex, and kimi
    all boot and render normally. The catastrophic interactive regression does
    not exist (residual deltas → L6).
- **Seams match the delivery plan.** Native-ID capture is contract-only
  (`SC_NATIVE_SESSION_ID` env, `register_native_session`, no TTY scraping);
  no adapter declares `session_control` yet, so bindings/leases are dormant in
  production until units 3-6 — consistent with step 2's "close the concurrency
  prerequisite first".
- Archive reuse on resume is exact (no new row, lifecycle not rewritten,
  cross-shell archive refused — tested); model pin is enforced against the
  archive; released bindings refuse resume.

## Findings

### F1 · Medium · graceful shutdown races the reconciler — healthy exits can strand the binding in `error` and crash the exit path

`supervise()` reaps the leader (`child.wait()`), then runs `terminate_group`
with up to `group_grace=2.0`s of SIGTERM grace before `on_exited` releases the
lease. During that window the recorded owner is dead while group members
(harness children — MCP servers, LSPs — are in the group) are still dying.
`reconcile_binding` classifies exactly that shape as `orphan-group`: it fences
the binding to `error` with the lease left in place. Unit 3's dispatcher
"scans … at a one-second local interval" (spec) and "reconciliation runs …
before every lease claim" — so once unit 3 lands, a routine clean session end
with any live child process can be fenced mid-shutdown. Then the supervisor's
`release_lease` finds `state='error'`, computes target `dormant` (native ID
set, rc 0), and `transition_binding` raises `InvalidStateTransition`
(`error → dormant` is forbidden) — the exception propagates out of
`supervise()`'s `finally` and run.py exits
"session launch refused: invalid session binding transition" *after* a healthy
session, leaving the binding in `error` (recoverable only through
`starting`/operator retry) with a misleading orphan message.

Two defects in one shape: (a) the reconciler cannot distinguish
"supervisor mid-cleanup" from "#439 orphan" — the supervisor's own live
cleanup should count as authority (e.g. record the supervisor PID on the
binding at claim, or only fence an orphan group that persists beyond a grace
across two scans); (b) `release_lease` must degrade, not raise, when the state
moved underneath it (treat `error`/`released` as terminal: clear or keep the
lease per policy, same-state refresh, never compute an illegal edge).
Fix (b) regardless of how (a) is resolved.

### F2 · Medium · resume/attach spawns the harness before ownership is validated

`claim_lease` runs in `on_started` — *after* `Popen`. On the
`--session-binding` path, a boot against a binding with a live validated owner
launches a full harness process, renders the boot, and only then hits
`LeaseConflict` → SIGTERM. Today the cost is a spawned-then-killed TUI and a
late refusal (nothing resumes the native conversation yet). But this seam is
what units 4-6 will put `--resume <native-id> -p` into: at that point the
second process begins operating on the provider conversation in the window
between spawn and claim — precisely what spec class4 forbids ("No adapter may
resume while another validated owner or active provider turn exists") and what
the state table's `foreground` row ("never concurrent-resume") excludes.
Cheap fix in this unit: run `reconcile_binding` + an owner-vacancy check
before spawning (refuse early), keeping the post-spawn claim as the atomic
gate. Alternatively rule it explicitly as a unit-3/4 obligation — but the
ordering lives in unit 2's `run.py`, so silence here will be inherited.

### Lows (report notes, non-blocking)

- **L1 — benign endings mapped to `error`.** `lease_exited` treats any rc
  outside `(0, -SIGINT, -SIGTERM)` as an error: a SIGHUP death (terminal
  window closed — the canonical way a resumable session goes dormant) or any
  nonzero harness exit fences the binding to `error`, blocking autonomous
  dormant resume until operator retry. At minimum add `-SIGHUP`; consider
  whether a nonzero exit of a *resumable* conversation should be `dormant`
  with `last_error` set instead.
- **L2 — `ValueError` escapes the launch guard.** run.py catches
  `(OSError, RuntimeError, LeaseConflict)` around `supervise()`, but
  `claim_lease` raises plain `ValueError` for harness/worktree identity
  mismatches (e.g. a boot that fell back to repo root while the binding
  expects the worktree) → raw traceback instead of "session launch refused".
- **L3 — stale prose now lies.** `shell_liveness.py`'s docstring rationale
  ("run.py ends in `os.execvpe` … leaving no exit hook") and `open_session`'s
  comment ("run.py execs the harness, so no code runs at exit") are both false
  after this PR. The liveness *mechanism* survives (verified above) but its
  written justification doesn't; next reader inherits a wrong model.
- **L4 — effort is not pinned on resume.** Spec pins "model, provider,
  effort, worktree, permissions" from the original archive; the archive
  lifecycle (migration 0071) and binding rows store no effort, and
  `binding_for_resume` neither returns nor checks one. Units 4-6 cannot pin
  what unit 2's seam doesn't carry — needs a ruling (store it, or name the
  route table as the source) before the adapters land.
- **L5 — concurrent `ensure_binding` race.** Two simultaneous boots of the
  same archive both pass the SELECT and race the INSERT; the loser gets an
  uncaught `sqlite3.IntegrityError` traceback rather than adopting the
  winner's row. Unlikely (liveness guard usually refuses the second boot) but
  unguarded.
- **L6 — interactive boots now run without a controlling terminal.**
  Verified non-fatal (probe above), but job-control semantics change: SIGTSTP
  is not in `FORWARDED_SIGNALS`, so Ctrl-Z suspends the supervisor while the
  TUI keeps writing to the pty (desync); `/dev/tty`-dependent features fail
  closed to fd 0. Worth one real interactive boot per harness as post-merge
  sanity, and a decision on forwarding/ignoring SIGTSTP.

## Test coverage notes (Low, folded into report)

Suite is strong where it counts (real-process #439 test, contention test,
migrated-schema integration). Missing: the F1 race (release after a
reconciler fence), the F2 pre-spawn refusal once added, L2's escape path, and
any assertion that `supervise()` restores prior signal handlers.

## Verdict

**2 Medium (F1, F2) block merge; 6 Lows to the sprint report.** No Major: the
#439 scenario itself is correctly closed and the lease model is sound. Fix
F1/F2, push, re-request — re-review will be scoped to those seams.
