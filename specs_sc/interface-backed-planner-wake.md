---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Interface chats and interactive planner wake
roadmap_status: in_progress
frozen: false
title: Interface-backed planner wake
tags: [sprints, interface, tmux, polling, claude, codex, kimi]
date: 2026-07-22
project: super-coder
purpose: Safe interactive shell wake
---

# Interface-backed planner wake

## Overview

Sprint events are durable in `shell_messages`, but a live planner still needs a
portable notification path. The first replacement design tried to infer an
empty composer by interposing on tmux key tables. Its mandatory feasibility
spike proved that stock tmux 3.5a delivers bracketed paste to the pane before
`Any` or `PasteStart` bindings can mark the pane dirty. A pane can therefore
contain an undetected FnB draft. Focused tmux injection is not safe on that
boundary and does not ship.

This design moves the writable boundary above tmux. Subfloor remains initiated
and supervised through the CLI, and every interactive shell operation is also
available through the localhost API. The existing web application gains an
`Interface` tab: shells appear in a vertical rail, an available shell offers
`New chat`, and an occupied shell opens its one live harness TUI. tmux remains
the durable process host, while an API-owned input broker serializes browser,
CLI, and automatic wake input before any byte reaches the pane.

The broker can reliably mark human input dirty before forwarding paste or any
other byte. A three-second quiet interval is a debounce, not proof of an empty
draft. Automatic wake requires the harness to be idle, the composer to be
clean, the writer stream to have been quiet for three seconds, and the broker
to own the input queue. It submits only this fixed prompt:

`Check your inbox and act on unread sprint events.`

Message bodies never enter the terminal. `task`, `result`, and `pr_event` rows
remain the durable work; terminal submission is only a notification.

> [!class4]
> Any writable path that bypasses the API broker makes input state unknown and
> disables automatic delivery. Quiet time alone never turns dirty into clean.

The lossless guarantee applies to durable sprint events, not to an impossible
exactly-once terminal write across a broker process crash. A human frame that
was reserved but not acknowledged when the broker fails becomes
`delivery_unknown`; its bytes are never replayed automatically. The session is
disarmed until the operator inspects the live TUI and reconciles it.

Provider conversation history is not the continuity boundary. The sprint
document, inbox, flags, shell state, repository, receipts, and tmux-hosted live
process are. A confirmed-dead process is replaced explicitly; no provider
resume, second harness, or headless planner is created.

## Requirements

1. Every nondeleted shell is represented in Interface with exact interactive
   chat availability and lifecycle state.
2. One shell has at most one live interactive chat generation across CLI and
   browser clients. Browser close, terminal loss, or network loss does not end
   the tmux-hosted chat. A legacy or unmanaged harness process blocks New chat
   as `unreconciled`; absence of a managed row is not proof of availability.
3. `New chat` is offered only when no live or unreconciled generation owns the
   shell. Starting a chat uses the normal harness, model, effort, permission,
   worktree, render, boot, and archive paths.
4. CLI and web controls call the same API and state machine. Neither mutates
   tmux or the engine DB through a private side path.
5. All writable human and automated terminal input is serialized by one
   per-generation API broker. `sc enter` becomes a broker client, and the raw
   harness launch primitive requires a generation capability. No input bytes,
   drafts, or terminal output are persisted in engine tables or logs.
6. Automatic sprint wake requires a supported planner harness, lifecycle
   `idle`, composer `clean`, at least three seconds since accepted human input,
   and no conflicting writer or unmanaged client.
7. Busy turns, approvals, structured user input, dirty drafts, input races,
   uncertain ownership, and broker failure queue safely. An unacknowledged
   human frame at failure parks as `delivery_unknown` and is never blind-retried.
8. Only sprint-scoped `task`, `result`, and `pr_event` messages wake a planner,
   and only while their sprint document is unfrozen and `status: ACTIVE`.
9. Every event is durable before notification. Failed, stale, or ambiguous
   submission never deletes, reads, or blindly replays it.
10. Claude Code, Codex, and Kimi use the same session and input protocol. Their
    adapters supply lifecycle hooks only; they do not control provider-native
    conversations.
11. No scheduled model poll, provider resume, app-server client, second
    harness, public webhook, terminal screen scrape, or raw event content in
    the wake prompt exists. GitHub polling is limited to active sprint watches.
12. Browser, Bash, fish, zsh, terminal emulator, OS focus, Wayland, X11, and
    SSH state do not affect correctness. Optional desktop alerts are UX only.
13. Interface execution requires the Linux sandbox, a declared supported tmux
    version, and pinned maintained terminal/stream dependencies. A non-Linux
    no-sandbox server keeps the review UI but reports Interface unavailable.
14. Ordinary service restart reconciles the live DB and private tmux server.
    Snapshot excludes volatile credentials and transport state; rebuild/update
    refuses while sessions, sprint bindings, wake batches, or input ambiguity
    are live.
15. Interface reads and mutations have explicit operator authority. Browser
    mutation requires same-origin session plus anti-forgery proof; CLI uses the
    instance operator capability; hooks use only generation-scoped capability.

## Product Boundary

Subfloor's existing localhost service owns the Interface API. The service is
started and supervised through the existing CLI and host runtime; opening the
web application never starts an unsupervised engine process. The CLI is an API
client for session creation, attachment, status, release, and recovery.

The Interface process host is the Linux sandbox. tmux and the selected stream
server become declared image/install dependencies rather than assumptions about
the host. Task 1 chooses and pins the maintained stream stack, its server
topology, and its vendored browser terminal assets. It may replace the current
stdlib request loop, but API, static UI, coordinator, and stream ownership remain
one supervised service on one loopback port.

Interface is a first-class interactive surface for every shell flavor. Sprint
wake arming remains planner-only. Existing ephemeral `sc run` worker launches
are unchanged and do not become Interface chats. Interactive occupancy means a
live API-managed tmux chat, not that a model turn is currently running.

The normal provider TUI remains visible and usable. Interface is a terminal
frontend, not a replacement chat protocol and not a direct provider API client.
Harness authentication, permission prompts, tools, slash commands, keyboard
behavior, and model routing continue through the installed CLI.

## Interface Workflow

1. The FnB starts or opens Subfloor through the existing CLI-managed runtime
   and selects the `Interface` tab.
2. A vertical left rail lists active shells with availability, harness, and
   alert indicators. Selection is URL-stable and survives refresh.
3. Selecting an `available` shell shows `New chat`. The action opens the normal
   harness/model/effort choices sourced from the current model catalogue.
4. One SQLite transaction reserves the shell and generation, commits the
   reservation, and only then performs the fenced archive/tmux process side
   effects. Successful identity and `session_start` confirmation promote the
   reservation to occupied. Definite pre-spawn failure closes it; ambiguous
   spawn leaves it unreconciled. A concurrent start returns the existing owner
   rather than creating a second process.
5. Selecting an `occupied` shell attaches a terminal stream to the live pane.
   Refresh or reconnect reattaches the same generation and receives a full tmux
   redraw; it never invokes provider resume.
6. One connected client holds the writer lease. Other browser tabs or CLI
   viewers are read-only and may request an explicit takeover. Takeover
   atomically revokes the prior lease and makes the old client read-only.
7. Human input, paste, resize, and control events flow through the broker. The
   UI shows whether the composer is clean, dirty, or unknown and whether a wake
   is queued, submitting, or running.
8. Closing a browser or CLI viewer releases only its client lease. tmux and the
   harness continue. Dirty state survives disconnect and reconnect.
9. `End chat` is explicit and confirmed. The supervisor signals the exact
   verified process and waits for exit. A separate force action is available
   only after graceful termination fails and shows the PID/generation it will
   end. The shell becomes available only after absence is proved and durable
   closure is recorded.

## Interface Layout

The Interface tab uses the application's full usable width rather than the
review tabs' narrow document column.

- The left rail is a stable vertical shell list. Each row shows display name,
  shortname, `available`, `starting`, `occupied`, `lost`, or `error`, plus a
  compact harness and unread-alert indicator when applicable.
- `occupied` means one live interactive generation owns the shell. `idle`,
  `busy`, `approval`, and `user_input` are lifecycle details shown in the main
  session header, not replacements for occupancy.
- The main pane shows the live terminal without a decorative card. Its header
  contains harness/model, archive/session age, writer or read-only state, draft
  state, sprint wake state, and exact recovery actions.
- An available pane contains one primary `New chat` command. There is no second
  New-chat control for an occupied or unreconciled shell.
- A lost or error pane preserves diagnostics and queued-work counts. It offers
  explicit reconcile, close, or fresh-generation actions only when their
  preconditions are satisfied.
- Mobile view collapses the shell rail into a shell picker above the terminal.
  The terminal retains a stable minimum height and explicit resize reporting;
  labels and controls never overlap its viewport.

The browser terminal uses a proven terminal-emulation library vendored with
the static application. It supports UTF-8, ANSI modes, bracketed paste, mouse,
copy, resize, alternate screen, and accessibility. Subfloor does not implement
terminal emulation or WebSocket framing by hand.

## API Resources

All mutating requests accept an idempotency key. A repeated request returns the
original resource or result.

| Method and path | Contract |
|---|---|
| `GET /api/interface/shells` | Shell availability plus current session summary |
| `POST /api/interface/sessions` | Reserve an available shell and start a generation |
| `GET /api/interface/sessions/{id}` | Exact session, lifecycle, input, writer, wake, and alert state |
| `POST /api/interface/stream-tickets` | Mint one short-lived, single-use viewer or writer stream ticket |
| `GET /api/interface/session-streams/{session-id}` | WebSocket upgrade for the authenticated bidirectional terminal and control stream |
| `POST /api/interface/writer-leases` | Acquire or explicitly take over one session's writer lease |
| `DELETE /api/interface/writer-leases/{id}` | Release the caller's writer lease only |
| `POST /api/interface/clean-certifications` | Certify one session's empty composer under its writer lease |
| `POST /api/interface/termination-requests` | Request graceful end, or force a previously failed request |
| `POST /api/interface/reconciliations` | Revalidate one session's tmux, process, lease, hook, and wake state |
| `POST /api/interface/sprint-bindings` | Arm one ACTIVE sprint document to one planner generation |
| `DELETE /api/interface/sprint-bindings/{id}` | Release the binding and cancel its queued wake work |
| `POST /api/interface/pr-watches` | Baseline and register one PR under an ACTIVE binding |
| `DELETE /api/interface/pr-watches/{id}` | Stop future polls without deleting observations or messages |
| `POST /api/planner-action-receipts` | Record idempotent action intent before a planner side effect |
| `PATCH /api/planner-action-receipts/{id}` | Record observed result, ambiguity, or explicit reconciliation |

Session creation returns `201` plus `Location`; an occupied-shell race returns
`409` with the current session reference. Validation uses `422`, stale or
revoked authority uses `409`, authentication uses `401` or `403`, and all
errors use `{ "error": { "code", "message", "details" } }`. Unknown payload
fields are rejected. Timestamps are ISO-8601 UTC and resource IDs are opaque.

`./sc launch` provisions a mode-0600 instance operator capability for the
server and CLI. A same-origin UI bootstrap exchanges it for an HttpOnly,
SameSite=Strict browser session and a rotating anti-forgery token; hostile sites
cannot read that token. Every Interface mutation requires both browser session
and token, or the CLI operator bearer. Stream setup also validates the exact
Origin and consumes a single-use ticket bound to session, generation, client,
role, and expiry. Hook tokens can call only the callback route for their one
generation and cannot attach, write, take over, stop, or reconcile a session.

Every HTTP mutation requires `Idempotency-Key`. Keys are scoped by actor and
operation and stored with a canonical request hash. An exact retry returns the
original status and resource; reuse with a different body returns `409`.
Terminal input frames use their lease sequence instead of the HTTP key.

The session stream uses an explicit versioned WebSocket subprotocol and carries
typed frames for terminal output, human input,
resize, writer state, lifecycle, wake state, alerts, heartbeat, and errors.
Input frames include a client sequence number and are acknowledged only after
the API has durably changed input state and accepted the bytes into the ordered
broker. Output frames contain terminal bytes but are never stored in the DB or
application event log.

Browser reconnect presents its last acknowledged control sequence. The server
replays bounded control-state changes and asks tmux for a fresh terminal redraw;
it does not replay or persist terminal history. A stale generation, revoked
writer token, duplicate input sequence, gap, or malformed frame is rejected
without forwarding bytes.

The exact streaming implementation must use a maintained library with bounded
buffers, ping/timeout handling, frame-size limits, and clean disconnect
semantics. Adding that small runtime dependency is explicit; the terminal and
stream protocols are not reimplemented inside the current stdlib HTTP handler.

## CLI Parity

`sc enter <shell>` remains the terminal client. It resolves the Interface API,
then either offers the normal harness picker for an available shell or attaches
the occupied generation. Its stdin and terminal resize events use the same
session stream and writer lease as the browser. It never runs `tmux attach`
directly while API-managed input is enabled.

Add:

- `sc interface status [shell]` for the same availability, lifecycle, writer,
  draft, wake, and alert state returned to the tab;
- `sc interface start <shell>` for a scriptable API-backed New chat;
- `sc interface view <shell>` for a read-only attach;
- `sc interface take-control <shell>` for an explicit writer transfer;
- `sc interface stop <shell>` and `reconcile <shell>` for exact recovery.

Noninteractive CLI output is stable and machine-readable with `--json`. An API
outage prevents new Interface mutations and reports the supervised-runtime
remediation. It never falls back to direct DB or direct writable tmux access.

## Occupancy Model

Occupancy, harness lifecycle, input cleanliness, and client presence are
orthogonal.

| Dimension | States | Meaning |
|---|---|---|
| Shell occupancy | `available`, `reserved`, `occupied`, `unreconciled` | Whether a live generation owns New chat authority |
| Harness lifecycle | `starting`, `idle`, `busy`, `approval`, `user_input`, `stopping`, `lost`, `error`, `ended` | What the installed TUI is doing |
| Composer state | `clean`, `dirty`, `unknown` | Whether automatic input is allowed |
| Client state | `none`, `viewer`, `writer`, `unmanaged` | Who may send human input |
| Wake state | `disarmed`, `armed`, `queued`, `submitting`, `running`, `parked` | Sprint notification state |

`available` is derived only after no live or uncertain generation remains. A
busy model is still occupied. A disconnected browser does not make a shell
available. A lost generation remains unreconciled until exact process absence
is proved and the operator closes or replaces it.

The API returns these dimensions separately. The shell rail may project them
for compact display only: `reserved + starting` displays `starting`,
`occupied + idle|busy|approval|user_input` displays `occupied`, and
`unreconciled + lost|error` displays `lost` or `error`. Projection never changes
New chat authority.

Legal occupancy edges are `available -> reserved -> occupied`,
`reserved -> unreconciled|ended`, `occupied -> unreconciled|ended`,
`unreconciled -> occupied|ended`. `ended` removes ownership and derives
`available`; no edge skips proof of exact process identity or absence. Legal
lifecycle edges are `starting -> idle`, `idle -> busy`,
`busy -> idle|approval|user_input|error`, `approval|user_input -> busy|error`,
any live state to `stopping`, and `stopping -> ended|lost|error`. Unexpected
verified process exit moves to `lost`; all other edges are rejected and audited.

A fresh generation starts `unknown` and becomes `clean` only when its mandatory
ready callback proves the normal empty prompt and no human sequence has been
accepted. `clean -> dirty` occurs before accepted human bytes are forwarded.
`dirty -> clean` requires a fenced submit callback or writer certification.
Any ownership, broker, client, or input-delivery ambiguity moves to `unknown`;
only exact recovery plus certification can clear it.

One partial uniqueness constraint permits only one non-ended Interface session
per shell. A reservation lease plus exact PID start ticks fences concurrent
browser, CLI, restart, and double-click starts. Startup reconciliation repairs
expired reservations and validates every occupied generation before accepting
input. The existing worktree/process liveness scan also runs before reservation;
any legacy or directly launched harness process makes the shell unreconciled and
blocks New chat until the operator proves absence or adopts a supported managed
generation.

## Tmux Runtime

The engine starts a private tmux server on an instance-scoped mode-0700 Unix
socket. It uses engine configuration, a distinct prefix, and one named session
per interactive chat. It does not depend on `~/.tmux.conf` or another server.
The Docker image and installer declare and version-gate tmux; ambient host
availability is not a dependency contract.

The pane directly execs the selected harness through the existing render and
launch path. The binding records tmux socket, session, window, immutable pane
ID, generation, pane and harness PID/start ticks, shell, archive, worktree,
harness, model route, permissions, CLI version, and callback identity.

The current render/archive/launch logic is factored behind an internal
generation-capability entrypoint. Public `sc enter` always calls the Interface
API; it cannot invoke the raw entrypoint. An attempted direct interactive launch
without the reservation capability refuses before creating an archive. Process
scanning remains the backstop for binaries launched outside `sc`.

The API broker owns the writable tmux client or PTY. Browser and CLI clients
attach to the broker, not to tmux. A separately attached tmux client may be
read-only for diagnostics. Detection of any unmanaged writable client changes
composer state to `unknown`, disables wake delivery, raises an alert, and
requires its removal plus explicit clean certification before rearming.

PID presence alone is never authority. Every start, input, stop, and wake
operation validates Linux start ticks, command identity, worktree, pane
ancestry, shell, archive, and generation. Any unreadable or mismatched field
fails closed. No process is killed automatically when identity is uncertain.

## Input Broker

Every accepted human input frame is processed under the generation's ordered
input queue and lock. A writer may have only one unacknowledged input frame;
the browser or CLI buffers later keystrokes locally:

1. Validate session generation and current writer lease.
2. Validate monotonic client input sequence and bounded payload.
3. Commit a metadata-only `pending` reservation for the sequence, set composer
   state to `dirty`, and update the last-human-input timestamp.
4. Forward the exact bytes once to the broker-owned tmux PTY.
5. Commit the sequence as `forwarded`, then acknowledge it to the client.

The transactions store state and sequence metadata, never input bytes. An exact
duplicate of a known `forwarded` sequence returns its prior acknowledgement and
does not forward again. A broker failure after `pending` but before the client
receives acknowledgement cannot distinguish pre-write from post-write without
storing the bytes or inspecting the TUI. Startup therefore changes composer and
input delivery to `unknown`, revokes the writer, disarms wake, and alerts. The
pending frame is never replayed automatically; operator inspection and explicit
reconciliation are required.

UTF-8,
paste, control and Alt chords, function keys, mouse, and escape sequences use
the same path. Resize is ordered but does not dirty the composer. Purely local
browser copy never reaches the broker and therefore does not dirty it.

`prompt_submit` from the authenticated harness hook clears dirty and changes
the lifecycle to `busy`. `turn_stop` changes it to `idle`; it is clean only if
no later human input sequence was accepted. Editing or erasing a draft remains
dirty because the engine does not inspect the pane. The writer may use
`certify-clean` only after clearing the visible composer; the API records the
certifying writer and input sequence.

Three seconds without input is only a quiet debounce. It does not clear dirty,
repair unknown, or override lifecycle. The interval is configurable within a
small bounded range for accessibility, but zero is forbidden. Live timing uses
a monotonic clock; after service restart every otherwise-clean generation waits
a fresh full debounce before wake can submit.

Automated wake uses the same lock and queue. It revalidates exact ownership,
`idle`, `clean`, quiet interval, empty pending-human-input queue, supported
hooks, and armed sprint; changes the batch to `submitting`; then writes the
fixed prompt plus Enter. A human frame ordered first dirties the composer and
cancels that attempt. A frame ordered after submission is causally later and
is processed normally; no frame can interleave inside the fixed submission.

## Harness Hooks

Each adapter maps native events into one authenticated contract:

| Event | Required behavior |
|---|---|
| `session_start` | Confirm harness PID, version, pane, shell, archive, generation, ready prompt, and zero accepted human sequence; move `starting` to `idle` and `unknown` to `clean` |
| `prompt_submit` | Record accepted input sequence; clear dirty; move to `busy` |
| `turn_stop` | Move `busy` to `idle`; signal queued work |
| `session_end` | Verify process exit and move to `ended` or `lost` |
| `approval_wait` | Move `busy` to `approval`; queue and alert |
| `approval_result` | Move `approval` to `busy`; clear its alert |
| `user_input_wait` | Move `busy` to `user_input`; queue and alert |
| `interrupt/failure` | Preserve queues and record the explicit terminal state |

Start-ready, submit, stop, and end are mandatory. A provider event that fires
before its interactive prompt is ready does not satisfy start-ready; without a
later native readiness signal that harness cannot arm sprint wake. A harness lacking distinct approval
or user-input hooks stays `busy` during that wait, which is safe; Interface
reports degraded alerts. Unsupported mandatory hooks prevent sprint wake
arming but do not prevent an ordinary interactive chat.

Hook configuration is merged without replacing fork or user hooks. Callbacks
discard prompt, tool, transcript, and terminal content and send only event,
session, generation, sequence, PID identity, and token. Wrong tokens, stale
generations, replayed sequences, illegal transitions, and PID mismatch are
rejected and audited.

## Sprint Scope

A wake is eligible only when all of these are true:

- The sprint document exists, is unfrozen, and declares `status: ACTIVE`.
- Its binding names the selected planner shell and occupied Interface session.
- The message is addressed to that planner, has kind `task`, `result`, or
  `pr_event`, and carries the same `sprint_doc_id`.
- The binding and Interface generation have not been released or replaced.
- The harness supports mandatory lifecycle hooks and the input broker is
  healthy.

`shell` messages and legacy unscoped messages remain visible but never wake a
planner. Message bodies are not parsed for sprint identity. A planner may own
only one ACTIVE binding, and a sprint names only one planner. Separate planners
may own separate active sprints.

Sprint close atomically releases its wake binding and cancels queued wake items
with an audit reason while leaving messages unread. It does not end the
underlying Interface chat. Chat lifecycle and sprint lifecycle are separate.

## Event Ingress

Eligible message producers use the engine API. The message and wake item are
inserted in one transaction under unique `(binding_id, message_id)`. After
commit, the API signals the supervised coordinator. No SQLite trigger launches
a process and no second writer edits the DB.

The coordinator blocks on committed wake work, harness callbacks, Interface
input and lifecycle events, and explicit operator commands. It performs no
interval model scan. Startup performs one reconciliation of active sprint
bindings, Interface sessions, tmux/process identity, unread scoped messages,
and unfinished batches.

An API outage cannot accept `task` or `result` sends or persist poll-derived
events. Callers receive a hard failure and use the existing deduplicated retry
workflow. No terminal input is accepted or injected while the broker API needed
to order and acknowledge it is unavailable.

Before a planner performs an engine-owned or external side effect for a message,
it records action intent through `sc sprint action begin` using a key derived
from message, operation, and target. A completed existing receipt suppresses the
duplicate; an intent without observed result requires inspection. The planner
records `complete` or `unknown`, reconciles `unknown` explicitly, and only then
marks the message read. Informational messages need no action receipt. The CLI
is an API client for the receipt resources; skill text lands only after those
commands work.

## GitHub Polling

GitHub state enters through local watched-PR polling. A watch requires sprint,
repository, PR number, and current head SHA. Registration performs an immediate
GitHub read and stores the normalized baseline before arming. A failed baseline
creates no armed watch and returns a retryable sanitized error.

This coordinator replaces the current host `sc watch daemon`, which polls every
live `watched_prs` row and writes the DB directly. Cutover stops and disables
that daemon before the new scheduler is enabled; compatibility CLI commands call
the API and never retain a second DB writer. Existing unscoped watches remain
readable but dormant until explicitly rebound to an ACTIVE sprint. The migration
rebuilds the old `(repo, pr, shell)` uniqueness into one active watch per
binding/repository/PR while retaining closed historical rows.

The poller runs a bounded interval only while ACTIVE sprint watches exist, plus
startup and explicit reconciliation. The default interval is 30 seconds with
jitter. Failures use capped repository backoff without blocking local task or
result events.

It stores normalized PR state, head SHA, review decision, check rollup, and
mergeability only. It stores no PR prose, logs, commit messages, raw payloads,
or tokens. Semantic transitions create idempotent `pr_event` messages and wake
items. Dedupe is keyed by watch, transition, head SHA, and state.

The guarantee is current-state convergence, not historical capture between
successful polls. Blind windows are durable and visible. Polling may create an
event but never injects terminal input, reads a message, or acts on a PR.

## Wake Delivery

Each unread eligible message owns one wake item. The coordinator coalesces
currently queued items for a binding into one fixed-prompt batch. Only one
batch may be submitting or running for a generation.

Before submission it acquires the input lock and proves lifecycle `idle`,
composer `clean`, quiet time at least three seconds, no pending human frame,
exact Interface and tmux identity, mandatory hook health, API health, and an
active sprint. Any uncertainty queues without sending a byte.

The submit callback acknowledges terminal delivery and changes the batch to
`running`. The following stop callback reconciles every item:

- message read: item `done`;
- unread with durable ambiguous action: item `reconcile`;
- unread without ambiguity: return to `queued` and increment wake count;
- new message handled in the turn: complete it; otherwise leave it queued.

Infrastructure never marks messages read. Planner actions use message-derived
idempotency keys. External side effects record intent and observed result. An
uncertain result parks for reconciliation before any retry. An unread item that
survives three completed wake turns becomes quarantined and alerts without
blocking newer work.

Wake item states are `queued`, `batched`, `submitting`, `running`, `done`,
`reconcile`, `quarantined`, and `cancelled`. Batch states are `queued`,
`submitting`, `running`, `complete`, and `delivery_unknown`. On service restart,
a `submitting` or `running` batch becomes `delivery_unknown` unless durable hook
sequence evidence proves the transition; it is never submitted again blindly.

## Data Model

| Surface | Key fields and invariants |
|---|---|
| `interface_sessions` | shell, archive, harness route, tmux and PID identity, generation, occupancy/lifecycle, timestamps, end/error; one non-ended row per shell |
| `interface_writer_leases` | session/generation, client ID, token hash, monotonic input sequence, heartbeat, acquired/revoked times; one current writer |
| `interface_input_state` | session/generation, clean/dirty/unknown, pending/forwarded input sequence metadata, last submitted sequence, last human input time, delivery ambiguity, certification audit; no bytes |
| `interface_idempotency_keys` | actor scope, operation, key, canonical request hash, response status/resource, created/expiry times; unique actor/operation/key |
| `sprint_planner_bindings` | sprint, planner, Interface session/generation, lifecycle hook token hash and sequence; one unreleased row per planner and sprint |
| `planner_wake_items` | binding, message, batch, state, completed wakes, ambiguity, error, times; unique binding/message |
| `planner_wake_batches` | binding/generation, queued/submitting/running/complete/delivery_unknown, input sequence fence, hook acknowledgements; one live batch |
| `planner_action_receipts` | message, operation, target, idempotency key, intent/result/reconcile state; unique operation key |
| `pr_poll_runs` | repository, source, watch count, start/finish, status, rate-limit and sanitized error |
| `pr_poll_observations` | watch, run, head SHA, normalized fingerprint, transition, blind-window marker, times |
| `planner_alerts` | session/binding/message/watch reference, severity, reason, opened/resolved times; deduplicated while open |

Add nullable `sprint_doc_id` to `shell_messages` and `watched_prs`. Existing
rows remain valid and unwoken.

Ordinary service restart preserves and reconciles all live rows in
`shell_db.db`. Git-tracked snapshot preserves durable messages, closed session
and binding audit, terminal/quarantined wake audit, receipts, watch definitions,
semantic transition/blind-window observations, and alerts. It excludes
operator/hook/stream tokens and hashes,
live leases, pending input metadata, PIDs/start ticks, tmux sockets, heartbeats,
terminal/control buffers, and successful no-transition poll runs. Snapshot may
run while a chat is live because it omits that volatile state; `sc rebuild`,
`sc update`, and engine materialization refuse while any non-ended session,
unreleased binding, nonterminal wake batch, or input ambiguity exists. Clean
operator release is the required drain path before rebuild.

## Retry Policy

| Condition | Required behavior |
|---|---|
| Busy, approval, user input, dirty, unknown, or quiet debounce | No attempt; await an event |
| Human input wins the broker order | Cancel wake attempt and preserve queue |
| Wake holds the lock first | Submit the indivisible fixed prompt; later input is ordered after it |
| Broker fails with a pending human sequence | Mark input delivery and composer unknown; revoke writer; never replay bytes |
| Definite failure before bytes are sent | Keep queued; bounded retries at 1s, 5s, and 30s |
| Prompt may be sent but submit hook is missing | `delivery_unknown`; never auto-retry |
| Browser or CLI disconnect | Release client lease; preserve chat, draft, and queue |
| Writer heartbeat expires | Revoke writer; preserve dirty state; allow explicit takeover |
| Unmanaged writable tmux client | Set unknown, disarm, alert, require removal and certification |
| Broker or API unavailable | Accept no input or wake submission; durable work stays queued |
| PID, pane, worktree, shell, archive, or generation mismatch | Refuse operation; never kill uncertain process |
| Harness, pane, tmux, container, or supervisor lost | Mark lost/unreconciled; queue and alert |
| Sprint closes | Cancel wake items; keep messages unread; leave chat running |
| Snapshot while chat is live | Exclude volatile live transport state; continue without disrupting chat |
| Rebuild/update with live session, binding, batch, or ambiguity | Refuse and report exact drain/reconcile commands |
| External action uncertain | Park and inspect before retry |
| Three completed wakes leave an item unread | Quarantine and alert; newer work continues |
| PR poll failure or blind window | Preserve prior state, back off, converge to current state |

Bounded send retries belong to one definite pre-send failure and stop after the
third delay. The three-second quiet debounce is event-reset, not polling. GitHub
watch intervals are the only steady timer and never invoke a model directly.

## Security And Privacy

- The service remains localhost-only under the existing host/container network
  boundary. Remote access uses the owner's established secure transport.
- Session streams and writer leases use random generation-scoped tokens. Token
  hashes, not plaintext, are durable in the live DB and excluded from snapshot.
- tmux sockets and runtime token files are mode 0700/0600 and owner-only.
- Input bytes, drafts, terminal output, hook prompt/tool/transcript content, raw
  GitHub payloads, and provider credentials are never persisted or logged.
- Input and output buffers are bounded. Oversized frames, slow clients, replay,
  sequence gaps, and malformed terminal data fail the client connection without
  affecting the harness process.
- Origin checks and per-request anti-forgery tokens protect browser mutation
  and stream setup even on localhost.
- The stream ticket is single-use, exact-Origin validation is mandatory,
  per-message compression is disabled for terminal secrets, and a restrictive
  CSP limits the UI to its vendored scripts, styles, and same-origin connection.
- API mutations remain shell- or operator-scoped. A viewer cannot become writer
  or stop a session without an explicit authorized action.
- Capability probes fail closed without weakening branch, permission, hook, or
  provider authentication policy.

## Delivery Plan

1. **Streaming and broker spike.** Prove a maintained terminal/stream stack can
   relay real Claude, Codex, and Kimi TUIs with exact byte fidelity, redraw,
   resize, reconnect, writer transfer, and atomic human-versus-wake ordering
   while the broker is live. Prove that every crash window parks unknown without
   replay. Record the chosen server topology, pinned dependencies/licenses,
   WebSocket subprotocol, auth/ticket flow, launch capability, buffer limits,
   and fault model. Any silent loss, duplicate, bypass, or interleaving stops
   the build for rescope.
2. **Session schema and state machines.** Add Interface session, reservation,
   writer, pending-input, idempotency, generation, binding, wake, receipt,
   polling, and alert state with exact transition constraints, reconstruction,
   snapshot projection, rebuild refusal, and migration coverage.
3. **One-shell vertical slice.** Start one shell through the API, open it in the
   Interface tab, exchange input/output, refresh and reconnect to the same tmux
   process, end it, and make New chat available again. Include operator/stream
   authorization and refusal of a legacy unmanaged harness before broad UI work.
4. **CLI and full Interface workflow.** Route `sc enter` through the broker;
   add shell rail, lifecycle UI, model picker, read-only viewers, takeover,
   stop/recovery, responsive layout, and API/CLI parity.
5. **Harness lifecycle adapters.** Merge and authenticate required hooks for
   Claude, Codex, and Kimi; gate sprint arming by capability without blocking
   ordinary chats. Adapters are parallelizable after the session contract.
6. **Transactional sprint wake.** Add scoped wake creation, coalescing, clean +
   idle + quiet gate, input locking, hook acknowledgement, receipts,
   ambiguity, quarantine, alerts, and crash recovery.
7. **Watched-PR polling.** Add normalized baselines, active scheduling,
   transition dedupe, poll audit, blind windows, backoff, and reconcile; migrate
   existing watches and retire the direct-DB host daemon before enablement. It
   may land dark after schema and run in parallel with Interface UI work.
8. **Operator workflow.** Add sprint arm/release/retry/resolve, status surfaces,
   action-receipt begin/complete/unknown/reconcile, sprint-close integration,
   structured messaging/watch registration, and provider-neutral skill guidance
   only after executable support is green.
9. **Conformance and real sprints.** Run adversarial input/session/provider
   matrices, then one real task/CI/review/merge sprint on each supported harness
   before freezing.

The vertical slice precedes broad UI and wake construction. No schema-only
stack is allowed to grow without a proven live input/output path.

## Verification

Hermetic and integration tests cover:

- Competing New chat requests, expired reservations, one-live-session
  uniqueness, PID reuse, wrong worktree/archive/generation, graceful and forced
  stop, server restart, legacy/unmanaged harness detection, raw-launch refusal,
  and shell availability reconstruction.
- Browser and CLI attach, full redraw, disconnect/reconnect, multiple viewers,
  writer takeover, stale writer, slow client, bounded buffers, frame replay,
  sequence gap, resize, and stream loss.
- Operator/browser/hook authority separation, exact Origin, CSRF rejection,
  expired/replayed stream tickets, idempotency retry, and key/body mismatch.
- Byte-for-byte ASCII, UTF-8, bracketed paste, control/meta, function keys,
  mouse, alternate screen, copy mode, nested tmux, and all three harness TUIs.
- Draft before event, erased but dirty draft, explicit clean certification,
  clean three-second debounce, input at 2.99/3.00 seconds, human-before-wake,
  wake-before-human, duplicate input frame, broker crash before/after tmux write,
  unknown-without-replay recovery, and unmanaged-client detection.
- Every legal and illegal lifecycle edge, stale hook sequence, callback auth,
  missing/degraded hooks, approval, user input, interruption, failure, and
  process loss.
- Atomic message/wake insertion, duplicate sends, coalescing, messages during a
  turn, read acknowledgement, quarantine, newer work past poison, close races,
  receipt intent/result/ambiguity/reconcile, and idempotent engine actions.
- Crash before send, during the input lock, after prompt bytes, before submit
  acknowledgement, during model work, and around external action intent/result.
- PR baseline, duplicate fingerprints, head changes, review and check
  transitions, merge/close, force-push, rate limits, normalization failures,
  blind windows, old-watch migration, legacy-daemon cutover, single-poller
  ownership, and explicit reconcile.
- API, tmux, coordinator, poller, container, and host restart matrices; startup
  reconciliation runs once and no steady model wake poll exists. Snapshot while
  live omits volatile state; rebuild/update refuse until exact drain.

Provider smoke tests use disposable shells:

1. Start each harness from New chat and prove the browser and `sc enter` attach
   the same generation.
2. Deliver event A to an idle, clean composer; prove submission occurs only
   after three quiet seconds.
3. Type and paste unsent drafts; deliver event B and prove no automatic byte
   reaches the pane until submit or explicit clean certification.
4. Race input against event C on each side of the broker order and prove bytes
   never interleave.
5. Deliver while busy, approval, and structured input states; prove it queues.
6. Disconnect all clients, deliver event D, reconnect, and prove the same TUI
   handled it.
7. Add a writable unmanaged tmux client; prove wake disarms and exact recovery
   is required.
8. Kill the harness, deliver event E, and prove it queues without a new process;
   explicit recovery starts a fresh generation from durable engine context.
9. Break submit acknowledgement and an external action response; prove both
   park without duplicate action.
10. Kill the broker before and after one human frame reaches tmux; prove neither
    case auto-replays, the writer is revoked, and recovery requires inspection.

The release gate runs one real sprint on Claude, Codex, and Kimi through task,
CI red/green, review, merge, and close. Inspection proves one planner process,
one ordered input broker, one fixed prompt per eligible batch, scoped PR
polling, and no terminal/event content persisted in logs or engine tables.

## Non-Goals

- A provider-specific chat UI or direct provider API integration.
- Provider conversation IDs, resume, continue, app-server, ACP, remote control,
  web-client, steer, or prompt-queue APIs.
- Headless or automatically respawned planner processes.
- A second planner, backup model, controller shell, or authority transfer.
- General worker conversion: ephemeral `sc run` remains unchanged.
- Inferring clean state from quiet time, focus, cursor, pane contents, or screen
  scraping.
- Supporting unmanaged writable tmux clients while automatic wake is armed.
- Persisting terminal scrollback, keystrokes, drafts, raw provider callbacks,
  or raw GitHub payloads.
- Waking on ordinary `shell` messages or unscoped legacy traffic.
- Guaranteed capture of GitHub transitions entirely between successful polls.
- Public webhook ingress or terminal-specific focus automation.
- General scheduled agents outside ACTIVE sprint planners.

## Done Condition

Every shell can start exactly one normal interactive harness chat from CLI or
the Interface tab, show occupied versus available truthfully, reconnect to the
same tmux-hosted generation, transfer one writer lease, and end or recover the
chat without provider resume or duplicate processes.

During an ACTIVE sprint, Claude, Codex, and Kimi planners receive the fixed
inbox prompt only through the API-owned input broker when lifecycle is idle,
composer state is clean, and human input has been quiet for three seconds.
Drafts, concurrent input, busy turns, approvals, structured input, unmanaged
clients, loss, and uncertainty queue or park without corrupting terminal input.

Every eligible event is auditable from scoped message or normalized PR
observation through wake batch, input-sequence fence, action receipt, and read
acknowledgement. There are no lost accepted sprint events, interleaved terminal
bytes, concurrent planner turns, duplicate engine-owned actions, blind external
or terminal-input replays, scheduled model polls, provider resumes, second
planner processes, public webhook dependencies, or event bodies injected into
the terminal. A broker-crash window for unacknowledged human input is surfaced
as `delivery_unknown`, never falsely reported as delivered or safe to retry.
