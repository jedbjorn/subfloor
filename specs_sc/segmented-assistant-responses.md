---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Browser-native headless conversations
roadmap_status: in_progress
frozen: true
title: Segmented assistant responses
tags:
  - browser
  - conversations
  - transcript
  - streaming
  - usability
date: 2026-07-31
project: super-coder
purpose: Split tool-separated replies
---

# Segmented assistant responses

## Objective

Render one model turn as multiple ordered assistant bubbles when assistant text
is separated by tool or actionable waiting activity. The feature is complete
when historical projection and live SSE reduction produce the same stable
segments across every harness, refresh, reconnect, truncation, and Chat/Diff
switch without exposing routine tool chatter or weakening the bounded transcript
performance contract.

This is spec sequence 8 under feature #24. It is a focused follow-up to the
shipped Browser chat performance spec and preserves Decision #32: transcript
history remains a bounded on-demand read projection, live DOM work remains keyed
and animation-frame-coalesced, and no projection cache or per-delta database
write is added.

> [!class1]
> A run remains one native turn and one durable outcome. Segmentation is only a
> display projection over its normalized event order; it never creates messages,
> runs, prompts, or harness sessions.

## Current behavior

Projection version 1 emits at most one assistant item per run with stable id
`run:{run_id}:assistant`. Every `assistant.delta` for that run is concatenated,
including text emitted before and after tool calls. The live reducer uses the
same run-wide identity.

This keeps storage and rendering simple, but it collapses conversational phases:

```text
assistant explanation -> tool work -> assistant result
```

becomes one large bubble. Routine tools are intentionally hidden, so the merged
bubble also removes the visual pause that tells the operator where execution
happened.

## Product contract

### R1 — Segment boundary

Within one run, an assistant segment is a maximal sequence of assistant deltas
that shares the same latest interstitial boundary anchor.

Boundary event types are:

- `tool.started`;
- `tool.completed`;
- `permission.requested`; and
- `input.requested`.

Every boundary updates the run's pending anchor to that event's durable
conversation sequence. The next `assistant.delta` belongs to the segment for
that anchor. Further deltas append to the same segment until another boundary
arrives.

Multiple boundary events without assistant text create no empty bubbles. They
only replace the pending anchor, so the next visible segment uses the latest
boundary sequence. A run with no boundary retains one assistant bubble and is
visually unchanged.

Terminal, usage, session, conversation, and message-state events do not create
assistant boundaries. A stacked user prompt remains a separate later run under
the existing queue contract.

### R2 — Tool visibility

Routine `tool.started` and `tool.completed` events remain hidden from the
transcript. They influence segmentation only.

`permission.requested` and `input.requested` remain visible activity items in
their durable sequence positions. Splitting the assistant text around them must
produce the actual order:

```text
assistant bubble -> activity bubble -> assistant bubble
```

Failure, interruption, and unknown-outcome activity behavior is unchanged.

### R3 — Projection version 2

The transcript endpoint advances to `projection_version = 2`. Every assistant
item retains `kind = assistant` and carries:

- `segment_anchor_sequence` (new): the latest boundary sequence for the
  segment, or `0` when no boundary precedes it in the run;
- `first_sequence` (existing field, now segment-scoped): the first retained
  delta sequence in the segment; and
- `last_sequence` (existing field, now segment-scoped): the final retained
  delta sequence in the segment.

Fixtures assert `first_sequence`/`last_sequence` as preserved fields with
narrowed scope, not as introductions.

The stable id is:

```text
run:{run_id}:assistant:{segment_anchor_sequence}
```

The anchor comes from durable event identity, not a segment counter. Loading a
larger source window, reconnecting, or replaying cannot renumber later segments.
`order_sequence` remains the first delta sequence, with stable id as the
deterministic tie-breaker.

The endpoint still returns one bounded text field per retained assistant item.
All run segments carry the same durable run outcome. No database schema or
stored event shape changes.

### R4 — Consistent historical fold

The transcript projection computes each assistant delta's latest preceding
boundary sequence within its own run before source-cap filtering. A SQL window
inside the existing event read (the projection's CTE — no schema view exists or
is added) is preferred so an omitted boundary cannot silently merge two
retained suffix segments. The active cursor is derived from that
full event prefix too, not only from retained source rows; a source-capped
boundary still anchors the next live delta.

Completeness checks count both assistant deltas and boundary events for retained
terminal runs. A terminal run missing required segmentation evidence is omitted
under the existing explicit truncation contract rather than projected with a
fabricated merge.

For an active non-terminal run, the bounded suffix may still be shown when the
existing projection rules allow it. The response's normal truncation metadata
continues to disclose omitted history.

Snapshot reads remain one SQLite snapshot with the fixed five-query read
count and existing source budgets, no external work, and no write transaction.

### R5 — Active segment cursor

A refresh may occur after a boundary but before the next assistant delta. The
snapshot therefore includes this optional control field:

```json
{
  "assistant_cursor": {
    "run_id": 42,
    "segment_anchor_sequence": 917
  }
}
```

It is present only for the active run. Its anchor is the latest boundary **of
that run** at or below `through_sequence`, or `0` when that run has none — a
boundary that closed an earlier run never anchors a fresh run's first segment.
In the example above, sequence 917 is a boundary belonging to run 42. This is
reducer state, not a display item.

Installing the snapshot hydrates the cursor before SSE opens. A subsequent
delta after the watermark therefore joins or creates the same segment it would
have used without refresh. Terminalization clears the active cursor.

### R6 — Live reducer

The browser tracks the latest boundary anchor per active run. Boundary events
update that cursor even though routine tool nodes are not rendered. On an
assistant delta, the reducer derives the version 2 item id from run id and the
current anchor, creates the keyed bubble when absent, or appends only to that
bubble when present.

The existing performance rules remain:

- deltas reduce immediately in memory;
- no more than one animation frame is outstanding;
- only the dirty assistant segment is reparsed;
- completed bubbles retain DOM identity;
- hidden Chat performs no Markdown parse or transcript replacement; and
- returning from Diff performs at most one catch-up frame.

Creating a new segment appends one keyed node in sequence order. It does not
rebuild earlier transcript nodes or force scroll-to-bottom when follow mode is
paused.

### R7 — Compatibility and recovery

API and UI ship atomically with projection version 2. A projection-version
mismatch retains the existing safe failure: preserve rails and controls, show a
retryable transcript error, and do not open SSE from an uninstalled snapshot.

Sequence gaps, duplicate item ids, an assistant delta without a run id, or an
anchor that moves backward trigger one authoritative snapshot reconciliation.
Repeated reconciliation failure stops automatically and preserves safe controls.

Old durable conversations require no backfill. Their normalized tool and
assistant events project into version 2 on read. Conversations without tool
events remain one bubble per run.

## Failure behavior

| Failure | Required behavior |
|---|---|
| Tool event lacks a run id | Ignore it for segmentation; retain existing event handling |
| Delta lacks a run id | Reconcile; never create a cross-run bubble |
| Boundary occurs before any text | No empty bubble; next text uses latest anchor |
| Several tools run consecutively | One later bubble anchored to the latest boundary |
| Refresh between tool and follow-up | Snapshot cursor opens the correct later bubble |
| SSE reconnect repeats a boundary | Sequence dedupe keeps one cursor transition |
| Historical source is truncated | Stable anchors and explicit truncation; no silent merge |
| Chat is hidden during tools | Cursor reduces in memory; no hidden DOM work |
| Run fails after an early segment | Preserve segments, then show the existing activity item |

No segmentation failure may submit, retry, interrupt, close, reorder, or
duplicate a message or alter the durable run outcome.

## Construction plan

### 1. Executable event traces

Add historical and live fixtures for plain prose, prose-tool-prose, multiple
tools, tool-before-prose, permission/input pauses, refresh between boundary and
delta, refresh during a fresh boundary-free run whose conversation's earlier
run ended with a tool boundary (cursor must be `0`, not the prior run's
boundary), source truncation, reconnect replay, and two simultaneous runs on
the same adapter in separate conversations.

Areas: `tests/test_conversation_api.py`, `tests/test_conversation_ui.py`,
`tests/test_conversation_diff_browser.py`, and
`tests/test_conversation_release_gate.py`.

**Gate:** current version 1 code fails on item count, ids, ordering, active
cursor, and stable-node assertions.

### 2. Projection version 2

Fold normalized event order into anchored assistant segments, extend terminal
run completeness checks to boundary evidence, return the active segment cursor,
and preserve all existing caps, redaction, warnings, and fixed-query behavior.

Areas: `.super-coder/api/conversation_routes.py` and API projection tests.

**Gate:** historical fixtures return exact stable segment ids and ordering from
one read snapshot without source mutation or an increased query class.

### 3. Keyed live segments

Teach snapshot hydration and the SSE reducer the same anchor rule. Keep tool
events hidden, visible waiting activity ordered, segment-local Markdown parses,
stable completed nodes, paused-scroll behavior, hidden-Chat suppression, and
single-flight reconciliation.

Areas: `.super-coder/ui/app.js`, optional focused style adjustments,
`tests/test_conversation_ui.py`, and the instrumented Playwright Chat harness in
`tests/test_conversation_diff_browser.py`.

**Gate:** live and historical reductions of the same event trace are identical,
and only the active segment node changes during a delta burst. The browser test
must compare actual node identity; source-string assertions alone do not prove
this contract.

### 4. Cross-harness release gate

Run the same multi-phase response journey through Claude, Codex, Kimi, and
OpenCode normalized traces. Permission/input boundary phases are
Codex/OpenCode-only (the Claude and Kimi adapters never emit
`permission.requested`/`input.requested`); the Claude and Kimi journey variants
exercise tool boundaries only. Smoke one live browser turn that explains,
writes through a tool, and reports the result; refresh once while the tool
boundary is pending and switch Chat to Diff during the follow-up stream.

**Gate:** every harness shows ordered separate bubbles, no tool chatter, no
lost text, no duplicate segment, preserved scroll/draft/controls, bounded
projection, and unchanged delivery and terminal semantics.

## Scope and estimate

This is a low-to-moderate implementation: one focused PR, no migration, no
adapter protocol change, and no new endpoint. The work is concentrated in the
transcript projection, live reducer, and adversarial tests. The highest-risk
part is snapshot/SSE parity around a pending boundary, which construction step
1 proves before production code changes.

## Non-goals

- showing routine tool cards, commands, arguments, or output;
- splitting on time gaps, paragraph breaks, token batches, or arbitrary text
  size;
- changing native run, message, queue, Stop, Close, or retry semantics;
- storing assistant bubbles as new database rows;
- adapter-specific segmentation rules; or
- transcript paging, caching, or revised source budgets.


