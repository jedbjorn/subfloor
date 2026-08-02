---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Browser-native headless conversations
roadmap_status: in_progress
frozen: true
title: Browser chat performance
tags:
  - browser
  - conversations
  - performance
  - pagination
  - streaming
date: 2026-07-30
project: super-coder
purpose: Bounded browser chat loading
---

# Browser chat performance

## Objective

Keep Interface, Chat, and Diff responsive as durable conversation history
grows. The feature is complete when arrival no longer waits for configuration
catalogues, history loading is paged, a large closed transcript opens from one
compact snapshot, and live deltas update keyed blocks at most once per animation
frame without changing conversation or Diff semantics.

This is spec sequence 7 under feature #24. It begins after feature #26's mode
shell lands, or as an explicitly coordinated stack based on that work. It does
not amend feature #26's Git, GitHub, target, patch, or mode-ownership contracts.

> [!class1]
> Optimize read projections and browser rendering, not authority. Messages,
> normalized events, state, delivery evidence, and harness transcripts keep
> their existing ownership and retention rules.

## Evidence

Current Interface arrival waits for shells, flavor defaults, the live model
catalogue, and up to 100 conversation summaries before it paints. The catalogue
is needed only by Configure; measured requests took about 3.5 seconds cold and
0.57 seconds warm on a connected instance.

One recent chat replayed 3,135 SSE events, including 2,785 `assistant.delta`
frames and about 642 KB of event envelopes. The client reparsed and replaced the
complete Markdown transcript after every frame. Conversation summary, detail,
and message requests completed in roughly 5–14 ms.

Diff is not the loading cause:

- selecting Chat does not call a review endpoint;
- review routes are isolated from ordinary conversation routes; and
- the Diff-adjacent local Git observation measured about 5 ms and runs at
  lifecycle boundaries, not transcript selection.

The dominant costs are eager configuration discovery, broad history reads,
historical delta transport, and repeated full-tree rendering.

## Design decisions

### Authority and projection

The transcript endpoint is a versioned, read-only projection over existing
messages, runs, and normalized events. V1 computes it on demand in a fixed
number of bounded reads. It adds no second transcript table and performs no
write from a GET request.

The projection may omit old display content when an explicit source or response
cap is reached. Omission is a view limit, not deletion: durable messages and
events remain unchanged. V1 does not page older transcript display content.
Adding transcript paging or a materialized read cache requires a follow-up spec
and measurements showing the on-read projection is insufficient.

### Starred exception

The non-starred history window is bounded to 20. Starred chats remain an
operator-curated exception and all are eventually loaded for the selected
shell. They do not block the first useful paint: the recent page renders first,
then paged starred results pin into the rail. A loading marker remains until the
starred pass settles or fails.

This preserves the existing “all starred” product promise without making an
unbounded number of starred pages part of the arrival gate. A future need to
cap or virtualize pathological star counts is outside this spec.

### Structural budgets

Correctness gates use request, source-row, response-byte, Markdown-parse, DOM
identity, and animation-frame counts. Wall-clock browser timing is advisory
because shared hosts and harness catalogue latency are not deterministic.

## Product contract

### R1 — Phased arrival

Opening Interface or an existing Chat or Diff route must not request
`/api/models` or `/api/flavor-defaults`.

The initial data flow is:

1. load the shell roster and lightweight open-conversation summaries;
2. resolve the selected shell or deep-linked conversation;
3. render the shell rail and the selected shell's first 20 non-starred
   summaries;
4. load and pin all starred summaries in background pages; and
5. for a selected conversation, install one transcript snapshot and then open
   its SSE stream.

Independent requests run in parallel. A starred-history failure cannot withhold
the recent page, and a transcript failure cannot withhold the shell or history
rails.

Configure loads flavor defaults and the model catalogue on demand. Concurrent
Configure opens share one in-flight promise. Successful results are reused for
the page lifetime. A rejected promise is cleared: Configure shows a bounded
error with Retry, and Retry creates exactly one new request pair. Catalogue or
defaults failure affects only configuration; existing conversations remain
usable.

### R2 — Bounded history

For the selected shell, the settled history rail contains:

- every starred normal conversation for that shell; and
- the 20 most recently active non-starred normal conversations for that shell.

Starred summaries are pinned ahead of non-starred summaries and do not consume
the 20 places. Duplicate `conversation_id` values are reduced to one keyed card.
Within each group, order is `last_activity_at DESC, conversation_id DESC`.

Only summaries load for history entries. Messages, transcript items, events,
review targets, and patches load only for the selected conversation. Closed
history for other shells is never prefetched.

### R3 — More and deep links

When more non-starred history exists, the rail ends with **More**. Activating it
loads the server cursor's next 20 summaries, appends new ids, and retains all
starred cards at the top.

Only one More request may be in flight. Repeated activation while it is pending
is a no-op. Failure leaves existing cards and the cursor unchanged and turns the
control into Retry. The control disappears only when `next_cursor` is null.

List cursors are not a cross-request snapshot. If star metadata changes during
paging, the client deduplicates by id and applies the newest summary it has.
The next explicit history reload re-establishes the starred and recent windows;
polling never advances either cursor.

A deep link first fetches the conversation detail directly. The authoritative
conversation shell canonicalizes a mismatched shell segment in the route. The
existing same-shell switch contract still applies: an idle, waiting, or error
open chat is closed before the older target is shown; queued or running work
refuses the switch until it finishes, is Stopped, or is explicitly Closed.

The selected deep-linked summary is inserted as a keyed context card even when
it is outside loaded pages. If a later page contains the same id, the card is
updated and repositioned rather than duplicated.

Starring a loaded card pins it immediately. Unstarring an older selected card
does not eject the open pane; it remains as selected context until the next
explicit history reload.

### R4 — Transcript snapshot

Opening a conversation must not replay historical `assistant.delta` events
through the live stream. The browser first requests:

```text
GET /api/conversations/{conversation_id}/transcript
```

The endpoint folds durable prompts, runs, and normalized events into ordered
display items:

- one user item per non-control prompt;
- one materialized assistant item per run that emitted assistant text; and
- activity items only for `permission.requested`, `input.requested`,
  `run.failed`, `run.interrupted`, and `run.unknown`.

Tool chatter, usage frames, raw reasoning, session identifiers, credentials,
and other secrets are not display items. Event detail passes through the
existing event-redaction policy before projection.

The response shape is:

```json
{
  "conversation_id": "cv_opaque",
  "projection_version": 1,
  "through_sequence": 5000,
  "controls": {
    "conversation_version": 9,
    "conversation_state": "idle",
    "queued_count": 0,
    "active_run_id": null,
    "close_requested_at": null
  },
  "items": [],
  "truncation": null
}
```

Every item contains `item_id`, `kind`, `order_sequence`, `message_id`,
`run_id`, and `created_at`.

| Kind | Stable id | Required fields |
|---|---|---|
| `user` | `message:{message_id}` | `text`, message `state`, `completed_at`, `text_truncated` |
| `assistant` | `run:{run_id}:assistant` | `text`, run `outcome`, `first_sequence`, `last_sequence`, `text_truncated` |
| `activity` | `event:{sequence}` | `activity_type`, bounded redacted `label`, `sequence` |

`order_sequence` is the prompt's `message.accepted` sequence, the assistant's
first delta sequence, or the activity event sequence. The server uses stable id
as the deterministic tie-breaker. Retry uses the failed user item's
`message_id`, exact text, and state; projected activity never invents a retry.

Unknown event types are ignored as display chatter. An unsupported payload
version or malformed required event payload adds a bounded projection warning;
it never exposes raw payload or silently fabricates text.

### R5 — Snapshot consistency

The server authorizes the conversation, opens one read transaction, captures
`through_sequence = COALESCE(MAX(sequence), 0)`, and reads all selected
messages, runs, and events at or below that watermark from the same SQLite
snapshot. No external or blocking work occurs inside that transaction.

`through_sequence` is the high-water mark for the whole conversation, not the
last retained display item. This remains true when old items are truncated, so
omitted history is never replayed as live output.

After installing the snapshot once, the browser opens:

```text
GET /api/conversations/{conversation_id}/events?after={through_sequence}
```

The SSE server changes bootstrap cursor handling so native EventSource recovery
works:

- on the first connection, `after` supplies the snapshot watermark;
- on reconnect, `Last-Event-ID` takes precedence over the bootstrap `after`;
- when both exist, the effective cursor is
  `max(after, Last-Event-ID)`, never a `422`;
- an opaque `cursor` remains mutually exclusive with `after`; and
- invalid or negative values still return `422 CURSOR_INVALID`.

Events committed after the snapshot watermark and before stream connection are
therefore replayed once. Reconnect continues after the latest delivered event
without reopening the snapshot.

### R6 — Explicit truncation

V1 constants define these default hard caps:

- `TRANSCRIPT_MAX_TURNS = 200`;
- `TRANSCRIPT_MAX_SOURCE_EVENTS = 20000`;
- `TRANSCRIPT_MAX_SOURCE_BYTES = 8 MiB`; and
- `TRANSCRIPT_MAX_RESPONSE_BYTES = 4 MiB`.

Tests may lower caps through dependency injection, not environment variables.
Production clients cannot choose larger limits through query parameters.

The projection retains the newest complete turn groups that fit. Non-terminal
turns and their control state are always represented. If one retained text
item alone exceeds the response budget, it is byte-bounded on a UTF-8 boundary
and sets `text_truncated=true`.

When any cap is reached, `truncation` is:

```json
{
  "reason": "turn_limit",
  "omitted_message_count": 12,
  "omitted_source_event_count": 2400,
  "omitted_through_sequence": 3100,
  "retained_from_sequence": 3101
}
```

`reason` is one of `turn_limit`, `source_event_limit`, `source_byte_limit`, or
`response_byte_limit`. Counts are non-negative and may be zero only when a
single retained item was text-truncated. The browser shows a persistent
“Earlier transcript omitted from this view; durable history was not deleted”
banner. Omission is never silent and never changes `through_sequence`.

### R7 — Incremental rendering

The browser installs keyed state from the snapshot. Live events are reduced
immediately, while DOM work is coalesced behind one scheduled
`requestAnimationFrame`.

An `assistant.delta` mutates only the active assistant item's text state.
During a frame, only that keyed assistant block may be reparsed and replaced.
Completed user, assistant, and activity nodes retain object identity when later
events arrive. Conversation header, queue, Working, Stop, Close, and retry
controls update from the same reducer without rebuilding stable transcript
nodes.

A full transcript rebuild is allowed only when:

- a different conversation is selected;
- the initial snapshot is installed; or
- reconciliation proves a sequence gap, projection-version mismatch, or keyed
  identity conflict.

Only one reconciliation snapshot may be in flight. Repeated failure stops the
automatic loop and shows Retry while preserving safe controls.

When Diff is visible, Chat state and SSE remain live. Events continue reducing
in memory, but hidden Chat performs no Markdown parse or transcript DOM
replacement. Returning to Chat performs at most one catch-up frame and
preserves draft, scroll intent, jump-to-latest, queued count, Stop, Close,
retry identity, and conversation identity.

### R8 — Lightweight freshness

The two-second history poll requests only open normal-conversation summaries
needed for shell indicators and any already loaded open card. It follows the
open cursor if the shell roster exceeds one server page.

Closed cards are not refetched by polling. They reconcile after a local
rename/star mutation or the next explicit selected-shell history reload.

Polling never:

- advances the starred or non-starred history cursor;
- loads another closed-history page;
- requests models or flavor defaults;
- opens a transcript or review resource; or
- replaces the Interface root.

## History API

Extend `GET /api/conversations` with:

- `starred=true|false`; and
- `open=true|false`.

The only accepted boolean spellings are lowercase `true` and `false`.
Malformed or repeated values return `422 VALIDATION_ERROR`. Existing unknown
query fields retain their current compatibility behavior.

`open=true` means `state != closed`; `open=false` means `state = closed`.
Combining `open` with `state` is allowed only when the predicates can both be
true. A contradictory pair returns `422 VALIDATION_ERROR`.

Existing `shell_id`, `state`, `mode`, `limit`, ordering, owner isolation, and
cursor behavior remain. The cursor payload binds the normalized filter scope,
including owner, shell, starred, open/state, and mode. Reusing a cursor under a
different scope returns `422 CURSOR_INVALID`.

The browser uses:

```text
GET /api/conversations?open=true&limit=100
GET /api/conversations?shell_id={id}&starred=false&limit=20
GET /api/conversations?shell_id={id}&starred=false&limit=20&cursor={cursor}
GET /api/conversations?shell_id={id}&starred=true&limit=100
GET /api/conversations?shell_id={id}&starred=true&limit=100&cursor={cursor}
```

Add a composite or partial index only when `EXPLAIN QUERY PLAN` on the release
fixtures shows the existing shell/activity index cannot satisfy the filtered
ordering without a material scan or temporary sort. Index decisions belong in
the migration and query-plan regression test, not in startup schema mutation.

## Transcript API

`GET /api/conversations/{conversation_id}/transcript` uses the existing
operator authentication, loopback Host check, ownership isolation, uniform
error envelope, and `Cache-Control: no-store`.

Required stable errors are:

- `CONVERSATION_NOT_FOUND` for missing or cross-owner ids;
- `TRANSCRIPT_PROJECTION_UNAVAILABLE` when a safe projection cannot be
  produced; and
- existing `ENGINE_DB_BUSY` behavior for bounded acquisition failure.

GET is safe and idempotent. It accepts no client-selected source, byte, event,
or projection limits. Unknown query parameters retain the route's existing
compatibility behavior.

## Performance budgets

Release fixtures include:

- at least 60 summaries across several shells;
- starred conversations older than the first recent page;
- at least three non-starred pages for one shell;
- a deep-linked conversation outside those pages;
- one transcript with at least 5,000 normalized events and 4,000 assistant
  deltas, below configured caps;
- one fixture exceeding each transcript cap; and
- a live burst of at least 500 deltas.

Deterministic gates are:

1. Interface and existing Chat/Diff arrival issue zero model and flavor-default
   requests.
2. The first useful rail paint waits for no starred page and contains exactly
   20 non-starred cards when at least 20 exist.
3. The settled rail contains every starred card, no duplicate id, and only the
   first 20 non-starred cards before More.
4. Each More action adds at most the next 20 non-starred ids and requests no
   transcript or review data.
5. The snapshot uses one database view, a fixed query count, and no more than
   the configured source caps.
6. Historical deltas become materialized assistant items and never arrive as
   one historical SSE frame per stored delta.
7. Initial snapshot installation performs one full transcript build.
8. A 500-delta burst schedules no more than one outstanding animation frame,
   never replaces completed nodes, and reparses only the active assistant item.
9. Hidden Chat performs zero Markdown parses and transcript replacements; return
   from Diff performs at most one catch-up frame.
10. Snapshot and SSE races lose and duplicate no sequence; reconnect with both
    bootstrap `after` and `Last-Event-ID` succeeds.
11. Every cap returns bounded bytes and explicit truncation without mutating
    source rows.

Record advisory browser timings for cold Interface, large closed-chat open, and
the live burst. Timing regressions require investigation but are not a brittle
shared-CI threshold.

## Failure behavior

| Failure | Required behavior |
|---|---|
| Model/default catalogue slow | Chat and Diff render; Configure alone shows loading |
| Model/default catalogue fails | Configure shows Retry; next attempt creates one new shared request |
| Starred page fails | Recent page stays visible with retryable starred warning |
| More fails | Existing cards and cursor remain; same control retries |
| Deep link is outside pages | Detail and transcript load directly after normal switch gate |
| Snapshot fails | Rails and safe controls remain; transcript shows Retry and SSE waits |
| Snapshot races an event | SSE after the high-water mark supplies it once |
| Snapshot is truncated | Persistent omission banner; source data remains unchanged |
| SSE reconnects | `Last-Event-ID` advances beyond bootstrap `after` without rebuild |
| Sequence gap or id conflict | One authoritative snapshot reconciles; repeated failure stops |
| Diff is visible during output | State reduces; hidden transcript performs no Markdown/DOM work |

No loading, paging, projection, rendering, polling, or mode failure may submit,
retry, interrupt, close, reorder, or duplicate a conversation message.

## Construction plan

### 1. Executable fixtures

Add API fixtures for filtered cursors, starred/recent paging, deep links,
consistent snapshot races, source/response caps, and SSE bootstrap reconnect.
Add an instrumented browser fixture that counts requests, scheduled frames,
Markdown parses, transcript replacements, and stable node identities.

Areas: `tests/test_conversation_api.py`, `tests/test_conversation_ui.py`, a
focused browser/reducer test helper if needed, and conversation fixtures.

**Gate:** current code fails structural counts rather than only elapsed time.

### 2. Filtered history API

Implement strict boolean parsing, filter-compatible cursors, owner-isolated
queries, contradiction validation, and query-plan-backed indexes if required.

Areas: `.super-coder/api/conversation_routes.py`, a trailing migration only if
an index is proven, and schema/API tests.

**Gate:** content, ordering, negative-space, invalid/repeated input, cursor
scope, pagination mutation, and cross-owner cases pass.

### 3. Snapshot and SSE seam

Implement the bounded, same-read-view transcript projection, exact response
schema, redaction, warnings, caps, truncation, and `through_sequence`. Change
SSE bootstrap parsing so `Last-Event-ID` safely supersedes `after` on reconnect.

Areas: `.super-coder/api/conversation_routes.py` and API/SSE tests. Do not
change event retention, dispatch, terminalization, or harness transcript files.

**Gate:** 4,000 deltas produce exact final text below caps; every cap is
explicit; a snapshot/connect race and native reconnect lose or duplicate no
sequence.

### 4. Phased browser loading

Defer flavor defaults and models, add retryable shared Configure loading,
resolve deep links directly, render recent history before paged stars, add More,
and replace the broad poll with open-only reconciliation.

Areas: `.super-coder/ui/app.js`, `.super-coder/ui/style.css`, and UI/request
tests.

This starts after feature #26's mode shell lands or is explicitly stacked on it.

**Gate:** failure isolation, selected context, same-shell switch rules,
deduplication, cursor retention, and zero eager configuration requests pass.

### 5. Keyed live transcript

Extract keyed transcript state and event reduction, install one snapshot,
bootstrap SSE from its watermark, coalesce DOM work per frame, preserve completed
nodes, and suppress hidden-Chat Markdown/DOM work.

Areas: `.super-coder/ui/app.js`, `.super-coder/ui/style.css`, reducer/browser
tests, and feature #26 mode-preservation tests.

**Gate:** the large replay and 500-delta burst meet every structural budget
while draft, scroll, queue, Working, Retry, Stop, Close, and Diff switching stay
unchanged.

### 6. Integrated proof

Run schema, conversation API, SSE, broker, UI, Diff preservation, render, and
full verification suites. Smoke a long closed chat, running chat, old starred
chat, three More pages, deep link, Configure failure/retry, disconnect/reconnect,
and Chat/Diff switching.

**Gate:** every release journey passes without a delivery, ownership, or review
semantic change.

## Release gate

1. Open Interface with more than 60 chats and see the shell rail plus 20 recent
   non-starred chats before starred completion.
2. Let starred loading settle and see every starred chat pinned with no
   duplicate id.
3. Activate More twice and receive two non-overlapping 20-item pages.
4. Star an old loaded chat; see it pin immediately and persist after reload.
5. Deep-link beyond loaded pages without loading intervening pages and without
   bypassing the same-shell switch gate.
6. Open a 5,000-event chat and observe one compact snapshot build followed by
   SSE after its high-water mark.
7. Open each over-cap fixture and see bounded bytes plus explicit omission or
   text-truncation state.
8. Stream 500 deltas and observe one outstanding frame, active-block-only
   parsing, and stable completed nodes.
9. Disconnect after live output and reconnect with no duplicate text, no `422`,
   and no snapshot rebuild.
10. Enter Interface, Chat, and Diff with zero model/default catalogue requests.
11. Open Configure twice concurrently and observe one shared request; fail it,
    Retry, and observe exactly one new request.
12. Switch Chat to Diff during a run and back with draft, scroll, Stop, queue,
    streamed output, and identity intact, with no hidden Markdown work.
13. Fail stars, More, snapshot, catalogues, and SSE in turn; each failure stays
    scoped and retryable and produces no message lifecycle mutation.

## Out of scope

- Changing delivery, outbox leasing, harness resume, Stop, Close, ownership, or
  message ordering.
- Deleting, compacting, or rewriting durable messages and normalized events.
- Adding a durable transcript authority or mutating state from transcript GET.
- Transcript paging, full transcript virtualization, semantic Markdown diffing,
  or rich tool-log playback.
- Capping or virtualizing pathological starred-chat counts.
- Changing Diff target, patch, Git, or GitHub behavior.
- Loading transcript bodies for unselected history entries.
- Repository-global conversation search.
- Solving stale-process deployment or server/static asset version skew.

## Prior decisions

- Decision #18 remains: durable conversations survive ephemeral harness
  processes; the transcript endpoint is a read projection, not transport or a
  second transcript authority.
- Decision #20 remains: one open browser conversation owns one shell; deep links
  and paging do not bypass the same-shell switch gate.
- Decision #22 remains: no blocking or external work enters conversation write
  transactions; this feature's snapshot uses a read transaction only.
- Decision #23 remains: SQLite write contention is not traded for a projection
  cache without measurements; V1 adds no per-delta projection writes.
- Decision #25 remains: only explicit Stop interrupts a run. Rendering, paging,
  snapshot, retry UI, and mode switches have no delivery semantics.
- Decision #31 remains: Chat and Diff are coequal modes. Performance work builds
  on the landed mode shell, retains one Chat state and stream, and suppresses
  hidden rendering without suspending event reduction.
