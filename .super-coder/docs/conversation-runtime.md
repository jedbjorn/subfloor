# Browser-conversation runtime

**Posture: current operator runbook.** Applies to every install that serves the
review layer. The FnB at the browser holds every operator verb here — there is
no CLI for Stop or Close. Example pids are illustrative. The user-facing
workflow lives in the [user guide](../../docs/README.md#browser-conversations);
authority and session minting live in
[the interface trust boundary](interface-trust-boundary.md).


A browser chat is a real native harness process, launched by the API server in
the shell's worktree. Four in-process services keep those processes honest: they
dispatch turns, watch for output, terminate what outlives its turn, and displace
a chat that has gone silent. None of it is a separate daemon — all of it lives
inside the review server and dies with it. The governing invariants: **nothing
alive may be invisible, and nothing visible may be nameless.**

## The four services

All are started from `start_runtime_services()` in `api/server.py` after the TCP
bind succeeds, so a losing duplicate process never dispatches a queued prompt.

| Service | Thread name | Cadence | Owns |
|---|---|---|---|
| Conversation broker (`scripts/conversation_broker.py`) | `conversation-broker` | commit-woken; 10 s heartbeat, 5 s lease recovery | claiming queued turns, launching the harness, streaming normalized events, finishing runs |
| Conversation reaper (`scripts/conversation_reaper.py`) | `conversation-reaper` | 60 s sweep | signalling process groups no chat links any more |
| Activity monitor (`scripts/activity_monitor.py`) | inside `sprint-runtime` | 5 s pulse | the chat inactivity ceiling and failed engine-wake recovery |
| Active-chat registry (`scripts/active_chat_registry.py`) | — | called by the others | the one-active-chat-per-shell link and its process identity |

The broker is **commit-woken, not interval-polled**: routine dispatch follows
startup, a post-commit `notify_commit()`, or a worker's terminal notify, and its
timers exist only for bounded crash reconciliation.

## The state machines

`scripts/conversation_state.py` holds the vocabulary, mirrored by migration 0132
whose DB triggers are the final backstop. Code checks the edge first so a caller
gets a typed error instead of an `IntegrityError`.

| Machine | States | Notes |
|---|---|---|
| conversation | `idle` `queued` `running` `waiting` `error` `closed` | `closed` returns only to `idle` (a send reopens the chat) |
| message | `accepted` `queued` `running` `completed` `failed` `cancelled` | the three terminals are absorbing — a message never leaves one |
| run | `leased` `starting` `running` `succeeded` `failed` `cancelled` `unknown` | `unknown` is a terminal, not an error |
| outbox | `pending` `claimed` `dispatched` `cancelled` | |

## Stop and Close are different verbs

Both are FnB actions in the Chats tab. They are not degrees of the same thing.

**Stop** — `POST /api/conversations/<cv_…>/interruptions`, optional `run_id`.
It asks the current turn to stop and never kills anything outright:

- an active `leased`/`starting`/`running` run gets a durable interrupt intent
  plus, when the broker holds the worker, immediate delivery through the
  adapter — for Claude, `SIGINT` to the turn's own process, or to its recorded
  group when the turn was attached rather than piped;
- if the newest active run has **no process of its own** — a run deferred
  `SHELL_LINGERING` behind an earlier turn's surviving process — Stop resolves
  the chat's *lingering* process instead and `SIGINT`s that group. Before
  PR #1603 Stop picked the deferred run and the process holding the chat never
  heard it;
- with no live process at all, Stop answers `409 RUN_ALREADY_TERMINAL`.

**Close** — `PATCH /api/conversations/<cv_…>` with `{"state":"closed","version":N}`.
Since PR #1603 close **is a kill**. In one committed transaction it cancels
queued turns, records `conversation.close.requested` and `conversation.closed`,
sets `closed`, and deletes the `active_shell_chats` link. Immediately after the
commit it terminates every process identity the chat still linked — the registry
process and any active run's — through `shell_liveness.terminate()`: pidfd-bound
`SIGTERM`, then `SIGKILL` after a 3 s grace, including the group each process
leads. A failure to signal never fails the committed close; it is printed and
left to the reaper. Closing is unconditional: a live turn does not block it, and
`state` may only ever be changed to `closed`.

## A lingering process is background work, not a bug

A harness can emit its `result` and keep working — background tasks, a long
tool call. The broker consumes the whole stream rather than breaking on the
first terminal. When a pending terminal has been quiet for
`linger_grace_seconds` (3.0) while the process is still alive, the run finishes
early so the reply is visible, but **keeps its registry process link** and the
same worker watches on. Meaningful output then opens a *continuation* run — same
trigger message, attempt+1, same session and process identity, with a
`run.resumed` event. A verified exit with no continuation releases the link.
Nothing on this path signals a process.

While that link is held, the chat projects
`process: {pid, start_ticks, alive, lingering, since}`, the GUI pill reads
"process still running · pid N", and Stop stays enabled. A follow-up send is
refused `SHELL_LINGERING` naming the pid, rather than starting a second process
on one native session file. A
Sprint wake for that shell is **deferred**, not failed: lease re-armed, message
still queued, one `run.deferred` event, bounded at 120 deferrals.

## The reaper ladder

The reaper acts only on runs **no active chat protects** — the registry link is
the protection, so a process becomes reapable the moment the FnB closes the
chat. Candidates are live runs (`starting`/`running`) with a pid, plus terminal
runs that still carry one (the lingering case), excluding any run whose ladder
already ended with its own `run.interrupted`/`run.reaped` event.

Each sweep advances one rung per candidate:

1. younger than `young_grace` → skipped;
2. process gone or pid recycled → finish, record the outcome, clear the identity;
3. no signal yet → native interrupt through the broker, recorded as `interrupt`;
4. `interrupt` older than `term_grace` → `SIGTERM` to the process group;
5. `SIGTERM` older than `kill_grace` → `SIGKILL`, then finish.

On an **already-terminal** run the reaper writes a `run.reaped` event and
nothing else: state, `ended_at`, message and conversation belong to whoever
finished it. An `unknown` run keeps `run.interrupted`. A run it proves exited
also loses its process identity, so it leaves every later scan — that plus
migration 0265's partial index on `conversation_events(run_id, event_type)` is
why the sweep is cheap on a long-lived engine (PR #1604).

## Config — environment only

There is no `instance.json` block. The reaper and the activity ceiling read the
environment of the server process; every broker constant (8 workers, 30 s lease,
10 s heartbeat, 5 s recovery, 3.0 s linger grace, 120 deferrals) is a
constructor default with no env override.

| Variable | Default | Effect |
|---|---|---|
| `SC_REAPER_HEARTBEAT_SECONDS` | `60` | sweep interval; must be positive |
| `SC_REAPER_TERM_GRACE_SECONDS` | `15` | interrupt → `SIGTERM` wait; `0` allowed |
| `SC_REAPER_KILL_GRACE_SECONDS` | `15` | `SIGTERM` → `SIGKILL` wait; `0` allowed |
| `SC_REAPER_YOUNG_GRACE_SECONDS` | `30` | minimum run age before the ladder starts |
| `SC_CHAT_INACTIVITY_CEILING` | `3600` | seconds of chat silence before the active chat is closed for inactivity |

A non-numeric or non-positive reaper value raises at startup; a bad
`SC_CHAT_INACTIVITY_CEILING` logs a warning and falls back to the default.

The ceiling exists specifically to displace a live but silent turn: it closes
and unlinks the chat, appends `conversation.closed` with
`reason: "chat inactivity ceiling exceeded"`, and leaves the process identity on
the run row so the reaper terminates the newly unprotected group. The same pulse
re-homes failed non-Sprint wakes (up to 24 h old) onto a pending wake or a fresh
one 180 s out.

## Refusals an operator meets

| Code | Meaning and way out |
|---|---|
| `SHELL_LINGERING` | this chat's own previous turn is still running; wait, or press Stop, then send again |
| `SHELL_BUSY` (browser) | another browser chat holds the shell; the message names that chat and pid — interrupt or close it |
| `SHELL_BUSY` (CLI) | a live or orphaned `./sc enter` session holds the worktree; the payload carries each holder's pid, start ticks and orphan state |
| `SHELL_HOLDERS_CHANGED` | `POST /api/conversations/shell-release` refused because a holder you were never shown appeared; review the new list before killing anything |
| `SHELL_RELEASE_FAILED` | no pidfd support, a foreign user, or a holder that survived `SIGKILL` |
| `RUN_ALREADY_TERMINAL` | Stop found no live process — the turn is genuinely over |
| `CONVERSATION_VERSION_CONFLICT` | the chat changed under you; re-read it and retry with the new `version` |
| `CONVERSATION_CLOSED` | a title or state change was sent to an already-closed chat |
| `CONVERSATION_BROKER_UNAVAILABLE` | the broker thread is not live; the interrupt intent is still persisted and startup reconciliation delivers it |

A CLI holder whose `docker exec` client died is detected through the enter-lease
flock (`SC_ENTER_LEASE`) and reported as `client-gone` rather than a permanent
`busy` (PR #1599/#1600): the containerd shim keeps the pty open, so nothing else
could tell.

## Running it — and telling whether it is

Nothing to start. The services come up with the review server and stop with it;
the reaper is joined in the server's `finally` before its DB can disappear.

```bash
./sc health                       # the review layer answers
./sc logs                         # server.log — each line is UTC-stamped
```

`server.log` is the operational record: `conversation-broker: startup
requeued=N recovered=M`, `conversation-broker: cycle error (…)`,
`conversation-reaper: run <id> sweep failed (…)`, and the close-path notes
`conversation close: <cv_…> pid N survived SIGKILL; left to the reaper`.
Durable liveness lives in `daemon_heartbeats`: the broker writes
`conversation-broker` every 10 s and the reaper `conversation-reaper` every
60 s (Admin-only DB read).

**A chat that looks stuck**, in order:

1. read the chat's `process` projection — `alive: false` means nothing runs and
   the state is the whole story;
2. `lingering: true` → the turn finished and its child works on: Stop `SIGINT`s
   that group, or leave it if the work is wanted;
3. still holding after Stop → Close, which kills the linked processes itself and
   is the strong verb, not a gentler one;
4. a named CLI holder → confirm no running work, then release those exact
   identities from the GUI;
5. unlinked and still alive → the reaper's ladder owns it, one rung per sweep.

## Limits

- **No CLI surface.** Stop, Close and release are browser actions against the
  loopback API; there is no `sc chat` verb.
- **Linux only.** Process identity is `/proc/<pid>/stat` pid + start ticks and
  the kills are pidfd-bound; a host without them fails closed rather than
  signalling by pid number alone.
- **One active chat per shell**, and the reaper never re-finishes a run it did
  not own.
