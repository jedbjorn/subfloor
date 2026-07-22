---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprint eventing — GitHub→inbox daemon + headless worker boot
roadmap_status: in_progress
frozen: false
title: Sprint planner session control
tags: [sprints, sessions, daemon, claude, codex, kimi]
date: 2026-07-21
project: super-coder
purpose: Provider-neutral autonomous wake
---

# Sprint planner session control

## Overview

Sprint eventing v1 writes durable `result` and `pr_event` messages, but only a
Claude planner is reliably awakened: `./sc watch inbox` completes as a Claude
background task and the harness turns that completion into another turn. A
Codex or Kimi planner can have unread work indefinitely because the daemon does
not address its live conversation or resume it after exit. This is issue
[#454](https://github.com/jedbjorn/subfloor/issues/454).

This correction gives the engine a provider-neutral session-control plane:

1. Bind each sprint planner archive to the harness's native conversation ID.
2. Turn each unread planner message into a durable, deduplicated wake job.
3. Deliver through the active harness control surface when one is available.
4. Otherwise queue until the conversation has no owner, then resume that same
   conversation headlessly.
5. Treat `shell_messages.read_at` as delivery acknowledgement; never infer
   success merely from a child process exiting zero.

The planner keeps one conversation across the sprint. Its DB state remains the
source of truth, but it does not repay discovery and declaration context on a
fresh harness session for every event.

> [!class1]
> The consistency boundary is behavior and state, not identical provider
> commands. Every adapter binds, queues, delivers, acknowledges, and recovers
> the same way; the final transport is harness-specific.

This spec supersedes the frozen eventing spec's claims that the GitHub daemon
never leads to planner execution, that the Claude-only inbox watcher merely
degrades on other harnesses, and that a later event already boots a headless
planner. Worker `sc run` behavior is unchanged.

## Decisions

### Include Claude

Claude uses the same session binding, wake-job ledger, one-owner lease,
acknowledgement, retry, and dormant-resume path as Codex and Kimi. We do not
maintain two correctness models.

Claude's active delivery may retain the proven background-task notification in
v1. Claude Remote Control is documented as a user-facing claude.ai/mobile
surface that uses OAuth and an Anthropic relay, not as a stable local daemon
API. When the Claude process is absent, the dispatcher uses
`claude --resume <id> -p <prompt>`. The launcher supplies the UUID up front with
`--session-id`, so no transcript heuristic is required. Anthropic documents
both resume-by-ID and the danger of resuming one session in two terminals:
[sessions](https://code.claude.com/docs/en/sessions) and
[Remote Control](https://code.claude.com/docs/en/remote-control).

`./sc watch inbox` registers and heartbeats its background PID against the
binding before it blocks, then clears that registration when it fires. A live
Claude process without an armed watcher is not reported as deliverable, so a
missing watcher is never mistaken for a live delivery channel: the binding
treats that process as `foreground` with no active-control transport, wake work
queues, and once the process exits the dispatcher resumes the same conversation
dormant by native ID. That queue-then-dormant-resume floor is the correctness
guarantee. Re-arming the watcher after every handled batch keeps live delivery
warm and is the recommended sprint-skill behavior, but it is an optimization
over that floor, not a requirement the engine depends on — delivery degrades
safely whether or not the skill re-arms.

### Codex control

Interactive Codex planners run through a super-coder-owned `codex app-server`
on a per-binding Unix socket; the normal TUI attaches with `codex --remote`.
The `thread/start` response supplies the native thread ID. The dispatcher uses
`turn/start` only when the thread is idle. If a turn is active, it leaves the
wake job queued; it does not use `turn/steer` for routine sprint events because
steering changes an in-flight plan. If the server is gone and no owner lease is
live, fallback is `codex exec resume <id> <prompt>`.

The protocol and remote TUI are documented by
[Codex app-server](https://developers.openai.com/codex/app-server/). The Unix
socket transport is preferred; no unauthenticated TCP listener is opened.

### Kimi control

Kimi planners use a locally authenticated Kimi session server. Session creation
returns the native ID; the dispatcher submits a prompt to that session only
after status reports idle. Although Kimi exposes native queue and steer
operations, normal sprint delivery queues in the engine so one policy governs
every harness; steering remains reserved for an explicit urgent operator
action. If the server is gone and no owner lease is live, fallback is
`kimi --session <id> --prompt <prompt>`. The bound model route remains
`kimi-code/k3` with the sprint's recorded effort.

For a managed planner, Kimi's server-backed web client is the interactive
surface; `./sc enter` prints and opens its loopback URL instead of starting a
second standalone TUI. Non-planner Kimi shells keep the existing terminal TUI.
This provider-specific UX is the cost of supported live session injection; the
conversation, worktree, permissions, and K3 route remain the same.

The adapter probes the installed CLI and its OpenAPI/ACP capabilities at boot;
it does not assume that deprecated `kimi server` and current `kimi web` command
trees are identical. Server authentication stays enabled and loopback-only.
See the [Kimi command reference](https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html)
and [session guide](https://moonshotai.github.io/kimi-code/en/guides/sessions.html).

### Other harnesses

The contract is capability-based. OpenCode and Vibe can add a binding adapter
after their installed versions pass the same capture, resume, concurrency, and
acknowledgement tests. They are not silently treated as supported. Selecting a
planner harness without `session_control` capability fails at sprint declaration
with an actionable message; worker launches remain unaffected.

## User workflow

1. The FnB boots a planner normally and may choose Claude, Codex, or Kimi.
2. The launcher opens the engine archive, establishes a native session binding,
   and shows both IDs in the boot summary: `session=0006 · codex=<uuid>`.
3. The planner declares the sprint and arms eventing. Arming sets
   `managed=1` on its binding and, before any worker is kicked off, validates
   in provider-generic terms that (a) the binding's approval/permission posture
   is one the adapter can honor for unattended autonomous turns — an
   interactive posture that would block a headless turn on a confirmation
   prompt fails here rather than arming into silent non-delivery — (b) the
   native session ID is captured, and (c) active delivery or dormant resume is
   available. Arming that cannot satisfy all three fails with an actionable
   message. The posture check is stated in neutral vocabulary and is enforced
   identically across the Claude, Codex, and Kimi adapters.
4. A `result`, `pr_event`, task, or ordinary shell message addressed to that
   planner becomes a wake job. The event body stays in `shell_messages`; the
   injected prompt is fixed: `Check your unread sprint inbox, act on every
   message, and mark each handled message read.`
5. A live controlled session receives the turn when idle. A busy session keeps
   the job queued. A dormant session is resumed headlessly in the same native
   conversation.
6. The planner acts, marks handled rows read, and re-arms any provider-local
   notification it needs. New messages arriving during a turn remain queued.
7. Sprint close releases the binding from managed wake. The conversation stays
   resumable manually and its archive remains intact.

`./sc enter <planner>` against a managed binding resumes or attaches to that
binding by default instead of opening another engine archive. An explicit
`--new-session` is refused until the managed binding is released. This keeps
the one-shell, one-conversation rule visible rather than relying on convention.

When the targeted managed binding is in `error`, `./sc enter` recovers it
retry-first: it re-attempts capture/resume of the existing native conversation,
and if that cannot proceed it fails before opening a new archive or spawning a
harness process. It never funnels the operator into releasing the binding as
the path out of the error — release cancels queued wake jobs, so making it the
implicit escape would silently discard pending work. Releasing an errored
binding stays a deliberate, separate operator action (`./sc session release`),
never a side effect of trying to re-enter.

## State model

Each binding has one of these states:

| State | Meaning | Dispatcher action |
|---|---|---|
| `starting` | Harness launched; native ID/control endpoint not confirmed | Queue only |
| `foreground` | Interactive client owns the conversation | Use active transport if supported; otherwise queue |
| `idle` | Controlled server is live with no active turn | Submit one queued wake batch |
| `dispatching` | A live turn or headless resume owns the conversation | Queue new work |
| `dormant` | No process owns a resumable conversation | Acquire lease and resume headlessly |
| `released` | Autonomous wake disabled intentionally | Do nothing |
| `error` | Capture, transport, or retry budget failed | Do nothing; surface remediation |

### Allowed transitions

The lifecycle is a closed, conservative edge set — any edge not listed is
rejected by `validate_transition`. A binding may always re-enter its own state
(an idempotent status refresh); the table below lists the edges to a *different*
state:

| From | Allowed next states |
|---|---|
| `starting` | `foreground`, `idle`, `dormant`, `released`, `error` |
| `foreground` | `idle`, `dispatching`, `dormant`, `released`, `error` |
| `idle` | `foreground`, `dispatching`, `dormant`, `released`, `error` |
| `dispatching` | `foreground`, `idle`, `dormant`, `released`, `error` |
| `dormant` | `starting`, `foreground`, `idle`, `dispatching`, `released`, `error` |
| `released` | `starting` |
| `error` | `starting`, `released` |

Three properties of this set are load-bearing:

- **Only `foreground`, `idle`, and `dormant` reach `dispatching`.** A wake batch
  is claimed only from those three; `starting` must first confirm a native ID
  and control endpoint (advancing to `foreground`/`idle`/`dormant`) before any
  turn can be dispatched.
- **`foreground → dispatching` is deliberately allowed** so Claude's active
  inbox watcher can deliver from a live interactive foreground session — without
  this edge the proven Claude active-delivery path could not fire.
- **`released` and `error` are recover-only via `starting`.** Neither resumes
  work in place: a released binding re-arms only through a fresh managed launch
  (`released → starting`), and an errored binding either restarts the same way
  (`error → starting`) or is retired outright (`error → released`). This makes
  disabling autonomous wake and hitting a terminal failure both require an
  explicit, visible re-launch rather than silently drifting back into dispatch.

State is not trusted merely because it is in SQLite. Every ownership decision
validates the recorded PID plus Linux process start ticks, command identity,
and worktree. PID reuse or a stale row cannot authorize a second writer.
Reconciliation runs when the dispatcher starts and before every lease claim.

> [!class4]
> Provider session IDs are addresses, not locks. Claude explicitly permits two
> terminals to resume the same session and interleaves their messages. Codex and
> Kimi also persist mutable conversation histories. No adapter may resume while
> another validated owner or active provider turn exists.

## Provider contract

Adapters gain a `session_control` block whose implementation must provide:

| Operation | Contract |
|---|---|
| `create` | Start the interactive/controlled session and return its native ID |
| `status` | Return `starting`, `idle`, `active`, `dormant`, or an explicit error |
| `deliver` | Start a new idle turn; never silently steer an active turn |
| `resume` | Run one non-interactive turn against the same native ID |
| `interrupt` | Operator-only; never used by normal event delivery |
| `release` | Disable automation without deleting provider history |

The capability probe records CLI version and supported operations on the
binding. Unknown versions fail closed for active delivery but may use a
smoke-tested resume command. No adapter scrapes terminal presentation text.

Model, provider, effort, worktree, permissions, and sandbox posture are pinned
from the original archive — specifically the configuration-effective values as
they resolved when that archive's session launched, recorded at launch and
replayed verbatim. Resume reads these launch-recorded settings; it does not
re-resolve them against live configuration, so a later edit to a default (model
route or effort tier) does not retroactively change an in-flight sprint's turns.
Resume does not silently fall back to a different model or a changed default
effort. Kimi K3 therefore remains K3 at the sprint's launch-recorded effort for
every planner turn unless the FnB explicitly changes the route.

## Data model

Add `shell_session_bindings`:

```sql
CREATE TABLE shell_session_bindings (
  binding_id          INTEGER PRIMARY KEY,
  archive_id          INTEGER NOT NULL UNIQUE
                      REFERENCES shell_memory_archives(archive_id),
  shell_id            INTEGER NOT NULL REFERENCES shells(shell_id),
  harness             TEXT NOT NULL,
  native_session_id   TEXT,
  control_endpoint    TEXT,
  control_capabilities TEXT NOT NULL DEFAULT '{}',
  cli_version         TEXT,
  state               TEXT NOT NULL CHECK (state IN
                      ('starting','foreground','idle','dispatching',
                       'dormant','released','error')),
  managed             INTEGER NOT NULL DEFAULT 0 CHECK (managed IN (0,1)),
  lease_pid           INTEGER,
  lease_start_ticks   INTEGER,
  active_channel_pid  INTEGER,
  active_channel_start_ticks INTEGER,
  active_channel_heartbeat_at TEXT,
  lease_generation    INTEGER NOT NULL DEFAULT 0,
  last_error          TEXT,
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (harness, native_session_id)
);
```

`control_endpoint` stores only a local Unix-socket path or loopback endpoint,
never bearer credentials. Tokens live in a mode-0600 runtime file or inherited
environment and are deleted on release.

Add `session_wake_jobs`:

```sql
CREATE TABLE session_wake_jobs (
  wake_id             INTEGER PRIMARY KEY,
  binding_id          INTEGER NOT NULL REFERENCES shell_session_bindings(binding_id),
  trigger_message_id  INTEGER NOT NULL REFERENCES shell_messages(message_id),
  state               TEXT NOT NULL DEFAULT 'queued'
                      CHECK (state IN ('queued','running','done','failed','cancelled')),
  attempt_count       INTEGER NOT NULL DEFAULT 0,
  available_at        TEXT NOT NULL DEFAULT (datetime('now')),
  started_at          TEXT,
  finished_at         TEXT,
  last_error          TEXT,
  UNIQUE (binding_id, trigger_message_id)
);
```

Wake jobs are reconstructible from unread messages plus managed bindings. A
startup reconciliation inserts missing jobs with `INSERT OR IGNORE`; the table
is an audit/claim ledger, not a second source of message truth.

The existing `session_token_usage.harness_session_ref` remains analytics data.
It is not reused as the control binding because it can arrive only after a
sweep, can contain a transcript path rather than an ID, and can have multiple
model rows for one conversation. Analytics attribution learns the binding's
native ID/ref as an exact match before falling back to time-window inference.

## Dispatch loop

The GitHub watcher remains a message producer. A separate supervised session
dispatcher owns execution:

```linear
Message row arrives :::class1 -> Reconcile wake job :::class2 -> Lock managed binding :::class2 -> Check provider + process state :::class2 -> Deliver live or resume dormant :::class3 -> Planner marks message read :::class3 -> Finish job + release lease :::class3
```

The dispatcher:

- Scans managed planner bindings and unread messages at a one-second local
  interval; this costs no model tokens and recovers rows written by any path.
- Claims one binding transactionally. Only one process may move it to
  `dispatching` for a given lease generation.
- Coalesces every currently queued message into one turn without embedding
  their bodies in the prompt.
- After the turn, marks jobs `done` only for messages whose `read_at` is set.
  A message arriving during the turn is also completed if the planner handled
  and acknowledged it; otherwise it remains queued for the next turn.
- Never marks a message read on the planner's behalf.
- Writes bounded attempt logs under the engine runtime directory and stores a
  short sanitized error on the binding/job for the CLI and GUI.

`./sc launch` starts the dispatcher; `./sc down` stops it. It has its own
heartbeat and status surface rather than borrowing the GitHub watcher's
heartbeat.

## Failure handling

| Failure | Required behavior |
|---|---|
| Native ID unavailable or ambiguous | Binding enters `error`; no fresh session is substituted |
| Active turn when event arrives | Leave queued; deliver after the provider reports idle |
| Foreground process without active-control support | Leave queued until it exits; never concurrent-resume |
| Provider server/socket disappears | Reconcile owner; resume only if the lease is truly vacant |
| Resume exits before acknowledgement | One initial attempt, then three delayed retries at 15s, 60s, and 5m; the fourth consecutive failure enters `error` |
| API unavailable | Do not start a model turn that cannot read/ack inbox; retry locally |
| Usage/rate limit | Preserve queue and surface the provider error; bounded retry only |
| Dispatcher crash mid-turn | Reconcile exact PID/start ticks; adopt a live child or retry only after it exits |
| FnB decision required | Planner opens/updates a linked flag, marks the triggering message read, and parks; no invented answer or retry loop |
| Sprint released with queued events | Leave messages unread and cancel queued wake jobs with an audit reason |

The dispatcher never kills an unverified process. This work must incorporate
the liveness correction tracked in
[#439](https://github.com/jedbjorn/subfloor/issues/439); a parent-only process
scan is not sufficient authority for session ownership.

## Operator surfaces

Add:

- `./sc session status [shortname]` — binding ID, engine/native session IDs,
  harness/model, state, owner, queued count, last delivery, and last error.
- `./sc session manage <shortname> --sprint <ref>` — enable autonomous wake
  after capability validation; idempotent.
- `./sc session release <shortname>` — stop autonomous wake without deleting
  history; refuses while a dispatch is active unless `--after-turn` is used.
- `./sc session retry <shortname>` — requeue failed unread work after the
  operator fixes auth, limits, or provider state.

The Shells/Analytics GUI shows a compact session-control status and queued/error
count. It does not expose control tokens or raw transcript paths.

## Surfaces

| Area | Change |
|---|---|
| Schema + migration | Session bindings, wake jobs, indexes, constraints |
| `run.py` | Session supervisor, binding creation, exact archive reuse, attach/resume behavior |
| Harness adapters | Declarative control capabilities and provider commands |
| Codex adapter | App-server lifecycle, Unix socket, remote TUI, thread status/turn delivery |
| Claude adapter | Supplied UUID, live watcher acknowledgement, `--resume -p` fallback |
| Kimi adapter | Authenticated local session server, K3 route, prompt queue, resume fallback |
| Dispatcher | Reconciliation, leases, coalescing, bounded retries, heartbeat |
| API/CLI | Token-scoped binding updates; `sc session` status/manage/release/retry |
| Analytics | Exact native-session attribution when a binding exists |
| GUI | Managed/idle/running/error state and queued count |
| Skills | Replace Claude-only planner recommendation and false wake claims |

## Delivery plan

1. **Schema and pure state machine.** Land bindings, wake jobs, transition
   validation, and reconstruction tests without launching providers.
2. **Supervisor and leases.** Replace exec-only planner launch with a
   signal-forwarding supervisor; capture native IDs and reconcile exact process
   ownership. This closes the concurrency prerequisite before any dispatcher
   can resume sessions.
3. **Claude vertical slice.** Bind a supplied UUID, feed wake jobs through the
   existing live watcher, and prove dormant `--resume -p` fallback. This proves
   the queue and acknowledgement model with the least new transport.
4. **Codex vertical slice.** Add app-server/remote TUI launch, idle turn
   delivery, busy queueing, server-loss fallback, and version capability tests.
5. **Kimi K3 vertical slice.** Add authenticated session-server lifecycle,
   prompt queueing, K3/effort preservation, and CLI resume fallback.
6. **Operator surfaces and skills.** Add `sc session`, GUI status, update
   sprint orchestration, and remove the frozen spec's Claude-only claims only
   after all three provider gates pass.
7. **Conformance sprint.** Run the same two-event synthetic sprint once per
   harness, then one real mixed-provider sprint before freezing this spec.

Steps 4 and 5 are parallelizable after steps 1-3. Skill rewrites are last so
the documented workflow never leads the executable one.

## Verification

Hermetic tests cover:

- Migration constraints, uniqueness, foreign keys, and rebuild-from-text.
- Every allowed/forbidden state transition and lease generation race.
- PID reuse, orphaned supervisor, live child, and stale endpoint reconciliation.
- Wake-job reconstruction, duplicate scans, batch coalescing, messages arriving
  during a turn, release with queued work, and acknowledgement by `read_at`.
- Adapter argv/API payloads, model/effort preservation, auth redaction, version
  gates, and no `turn/steer`/Kimi steer in the normal path.
- Crash before spawn, after spawn, after model completion, and before job
  completion; each yields at-least-once wake with at-most-one conversation
  writer.
- API outage, provider failure, rate limit, unknown session, and retry
  exhaustion.
- Analytics exact attribution from the binding without double counting.

Provider smoke tests run behind opt-in environment gates and use disposable
sessions:

1. Start an interactive controlled planner and capture its displayed native ID.
2. Deliver event A while idle and prove the same conversation acknowledges it.
3. Deliver event B during an active turn and prove it queues rather than steers.
4. Stop the control server/process, deliver event C, and prove the same native
   conversation resumes headlessly.
5. Confirm no second rollout/session/transcript was created and no message was
   marked read by infrastructure.

The release gate is a real sprint on each of Claude, Codex, and Kimi K3. Each
must progress through task result, CI event, reviewer result, and merge without
an operator prompt or scheduled model poll. Review also checks that #439's
orphan/liveness scenario cannot create concurrent writers.

## Non-goals

- Using Claude Remote Control's private relay as an automation API.
- Steering an in-progress model turn for routine sprint messages.
- Starting a fresh planner conversation when capture or resume fails.
- Embedding inbox bodies in wake prompts; messages remain the durable trail.
- Changing ephemeral worker sessions or their `sc run` task lifecycle.
- General scheduled-agent infrastructure outside managed sprint planners.
- Enabling unsupported harnesses by optimistic command construction.

## Done condition

A planner selected on Claude, Codex, or Kimi K3 holds one native conversation
for the sprint and autonomously handles every durable inbox event. Idle events
start a turn immediately, busy events queue without steering, dormant sessions
resume by native ID, every handled event is acknowledged only through
`read_at`, crashes recover without duplicate conversation writers, and the
entire delivery history is auditable from bindings, wake jobs, and
`shell_messages`.

