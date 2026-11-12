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
| `SC_CHAT_COMPACT_WINDOW_CLAUDE` | `120000` | auto-compact window (tokens) passed to Claude browser chats; `off` restores the harness default |
| `SC_CHAT_COMPACT_WINDOW_CODEX` | `120000` | the same for Codex browser chats; `off` restores the harness default |
| `SC_CHAT_COLD_RETURN_TOKENS` | `60000` | context size (tokens) at or above which an idle chat gets the cold-return notice |
| `SC_CHAT_COLD_RETURN_MINUTES` | `60` | minutes since the chat's last activity after which that notice is due |

The two compact-window settings are read at each dispatch, so a restart of the
server process is what picks up a change; an open chat then takes it on its
next turn. A value that is neither a positive whole number nor `off` logs a
warning and uses the default. Kimi and OpenCode have no window setting. The
two cold-return settings are read whenever a chat is projected; a value that
is not a positive whole number logs a warning and uses the default.

## Compaction, the context meter and the re-ground line

Feature #95, spec #272. The engine summarises nothing; it makes each
harness's own compaction earlier and visible.

- **`context.compacted`** is a normalized event: `trigger` (`auto`, `manual`,
  `unknown`), `tokens_before` and `tokens_after` when the harness states them
  or the adapter can read them, and never any summary text. One event per
  compaction. The chat shows it as a marker, for example
  `Context compacted · 123k → 28k · automatic`; a figure that was not reported
  is left out. Claude, Codex and OpenCode are mapped; Kimi is not (see each
  adapter's README).
- **`usage`** now says what it measures. `scope: request` is one model request
  of the chat and carries `context_tokens` (uncached input, cache read and
  cache write together) and, where the harness states it, `context_window`.
  `scope: turn` is a whole-turn total and `scope: subagent` is a delegated
  agent's request; neither is the size of the chat's context. An event with no
  scope (recorded earlier, or from Kimi) still labels its reply as before but
  never drives the meter.
- **The meter** is the latest `request` reading, or the size a newer
  compaction left behind, with a percentage only when a window is known. The
  transcript projection returns it as `context`
  (`context_tokens`, `context_window`, `measured_at`, `sequence`), each `null`
  when unknown. Nothing is estimated. `context.compact_window` is the
  auto-compact window the engine applies to that chat (`null` when the
  harness has none, the setting is `off`, or the model's window is smaller);
  the meter's tooltip states it. Each assistant item carries
  `context_measured`: true only when its figure is a `request` reading, so a
  figure of another meaning gets no percentage and a tooltip that says so.
- **`message.reground`** records that the engine appended the re-ground line
  to the text it dispatched for that message. The broker adds the line to the
  first message sent to a resumed session after a compaction (operator
  messages and engine wakes alike), once per compaction. The stored message,
  the boot file and the system prompt are never changed.
- **Claude's marker says "summary".** Claude's figure after is the summary
  alone; the next request also carries the system prompt and tools and is
  several times larger. Its marker reads
  `Context compacted · 172k → 13k summary · automatic`. Only the wording
  differs: the stored event is the same for every harness.

## The manual compact button

`POST /api/conversations/<cv_…>/compactions` (empty body, `Idempotency-Key`
required) asks the chat's harness to compact its stored session now. The
button sits at the bottom left of the composer, beside the meter.

- **How it runs.** The request joins the chat's own queue as an engine-sent
  control message (`sender_kind: engine`, `sender_ref: compact-button`,
  `message_kind: control`). The broker runs it like a turn: one at a time, in
  order, interruptible with Stop, and a message sent meanwhile waits behind
  it. Instead of sending text it calls the adapter's `compact()`: Claude, a
  resumed headless run whose prompt is `/compact`; Codex,
  `thread/compact/start` on the resumed thread; OpenCode,
  `POST /session/{id}/summarize`. Kimi has no confirmed trigger and no button.
- **What is recorded.** `compaction.requested` (engine-initiated, with the
  requesting user) when the button is pressed. No operator message is
  created, and the transcript, which lists prompts only, never shows the
  request. From the request's own run only `session.started`, `run.started`
  and the compaction are kept: the summary a harness streams, and the usage of
  the request that wrote it, are dropped.
- **Exactly one result per press**, each written on the chat rather than on
  the request's run so it shows without a turn:
  `context.compacted` with `trigger: manual` (the marker, and the meter
  drops); `compaction.failed` (one line: the harness refused, the request
  broke, or it was stopped); or `compaction.not_reported` (the harness
  accepted the request and finished without reporting a compaction, within a
  bounded wait). A failed request does not put the chat in `error`: the
  session is as it was.
- **The re-ground line** is not added to the request and is not spent by it.
  The first real message after a manual compaction carries it, exactly as
  after an automatic one.
- **What the page is told.** The single-chat read and the transcript's
  `controls` carry `compaction`: `supported` (the harness has a trigger),
  `available` (and the chat has a stored session and is open; this shows the
  button), `enabled` (and nothing is in flight; otherwise it is greyed out),
  `pending` and the pending request's `message_id`. The server refuses a
  press it would have greyed out.

| Code | Meaning |
|---|---|
| `COMPACTION_UNSUPPORTED` | the chat's harness has no manual trigger |
| `COMPACTION_NO_SESSION` | the chat has not had a turn yet, so there is nothing to compact |
| `COMPACTION_PENDING` | an earlier press is still in flight; one compaction runs |
| `CONVERSATION_BUSY` | a turn is queued, running or waiting; press again when it has finished |

## The cold-return notice

A chat whose latest `context_tokens` is at or above
`SC_CHAT_COLD_RETURN_TOKENS` and whose last activity is more than
`SC_CHAT_COLD_RETURN_MINUTES` ago has lost its provider cache: the next
message re-reads the whole context uncached. When the operator opens such a
chat, comes back to a tab left open on it, or puts the cursor in its
composer, one line above the composer says so, with the size.

- **Actions.** *Compact now* (the button's own request, where the button is
  available), *Continue here* (dismiss), and *New chat* (the history pane's
  close-and-start path, after a reminder that only control-plane state
  carries over).
- **It is a suggestion.** It never disables the composer or Send. Sending,
  compacting or *Continue here* dismisses it. It is not shown during a turn,
  for a closed or closing chat, or for a chat the engine still manages for a
  participant.
- **Data.** The transcript projection returns `cold_return`
  (`context_tokens`, `idle_minutes`, `last_activity_at`, and `due` as of that
  read); the single-chat read returns the two settings. The page re-checks
  the same rule against its own clock, so a tab left open is covered without
  a reload.

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
