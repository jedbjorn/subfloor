---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
title: Sprints v2 FnB board
tags: [sprints, ui, observability, fnb]
date: 2026-08-01
project: super-coder
purpose: Live FnB Sprint visibility
---
# Sprints v2 — FnB Board

## Overview

> [!class1]
> Follow-up to spec #46 after U12 lands. Deliver one reviewable PR that gives the FnB a dedicated, live Sprint board and changes the global first-three header tabs to **Chats, Sprints, Shells**.

The reference is the FnB-annotated `shared/SprintBoard.png`. It supplies the interaction intent: one board, lifecycle and feature context, pause/abort controls, five work-state columns with dependency wiring, work-unit drill-down, and collapsible event and summary feeds. This spec translates that intent into the Sprints v2 domain. It does not restore Sprint v1 state, names, tables, or orchestration.

The observable done-condition is:

- `#sprints` opens a live, deep-linkable FnB board for prepared, armed, paused, completed, and aborted Sprints.
- The board truthfully projects the existing v2 lifecycle, participants, units, dependencies, PR observations, events, judgments, and reports without copied UI state.
- FnB Pause, Resume, and Abort use the existing lifecycle authorities.
- Every application view carries the same first-three header order: **Chats → Sprints → Shells**.
- Focused API, DOM, routing, lifecycle, accessibility, and visual tests pass.

Spec #46 deliberately named the board web GUI as future scope so foundations landed first. Those foundations now exist. This sibling spec activates that deferred scope without changing spec #46's collaboration contracts.

## Prior Decisions

- Spec #46 remains authoritative for Sprint lifecycle, roles, work-unit dispositions, participant conversations, messaging, recovery, and advisory close-out.
- Decision #39 remains authoritative: Sprint chats are generic browser conversations with Sprint links and one current-conversation pointer per participant.
- Decision #45 remains authoritative: close-out completeness is advisory, merged observation completes only `merge_ready` work, and lifecycle authority is not moved into the UI.
- The FnB has fixed the global leading tab order as **Chats, Sprints, Shells**.
- The board is a projection. No new board-state, rollup, summary, or copied-status table is permitted.

## Navigation Contract

The one global header in `.super-coder/ui/index.html` is reordered to:

1. **Chats** — existing `data-tab="interface"` and existing `#interface[/shell/conversation/mode]` hashes remain stable.
2. **Sprints** — new `data-tab="sprints"`, `#sprints`, and `#sprints/<sprint_id>` routes.
3. **Shells** — existing `data-tab="shells"` and all `#shells-*` hashes remain stable.
4. Existing remaining tabs keep their current order and separators: Roadmap, Docs, Flags, Worktrees, Repo Map, Analytics, Scripts.

This is a visual-order change, not an unrequested landing-page migration. An empty hash continues to open Shells. Explicit hashes, browser Back/Forward, refresh, document titles, and active-tab styling remain correct.

`#sprints` selects the highest-priority Sprint in this order: armed, most recently paused, most recently prepared, then newest terminal Sprint. `#sprints/<id>` selects that exact Sprint. A compact selector lists Sprints newest-first and shows lifecycle; it preserves the route on refresh.

The Sprints view is full-width. When there are no Sprints, it shows a calm empty state explaining that prepared Sprints appear after declaration. Missing or unauthorized IDs show an explicit not-found/forbidden state and do not fall back to a different Sprint.

## Board Header

The board heading contains:

- `Sprint <id>` and the linked feature title.
- A link to the governing Roadmap feature. The stable route is `#roadmap-feature-<feature_id>`; opening it displays that feature's existing Roadmap modal without changing the feature.
- Bound spec links using the existing document open route.
- Originating Planner shortname.
- Lifecycle badge using the exact v2 value: prepared, armed, paused, completed, or aborted.
- Created, armed, paused, and terminal times when present, plus a derived elapsed duration. The UI derives labels and duration; it never stores them.
- Terminal outcome when present.

Lifecycle tone is semantic: armed reads active, paused reads cautionary, terminal states read settled, and prepared reads staged. A badge is never colored from a work-unit or provider vocabulary.

Actions are state-dependent:

| Current state | Actions |
|---|---|
| prepared | Abort Sprint |
| armed | Pause Sprint, Abort Sprint |
| paused | Resume Sprint, Abort Sprint |
| completed or aborted | none |

Pause, Resume, and Abort open a small confirmation modal with a required reason. Abort explains that it stops work but retains the full history. The button says **Abort Sprint**, not Cancel, because `aborted` is the v2 terminal state. The UI does not expose Arm, Complete, merge, review, re-plan, assignment, or follow-up-disposition controls in this PR.

The browser handler delegates to the same pause/resume/abort coordinators used by authenticated shell surfaces. It must not recreate transition logic or manufacture a UI-only Planner message. In particular, Abort retains the coordinator's durable passive request for the Planner's abort report.

## Work Unit Board

The board uses the five reference columns in this exact order:

| Column | v2 dispositions |
|---|---|
| Done | completed, cancelled |
| Review | in_review, fixing, merge_ready |
| Dev | active |
| Waiting | planned, ready |
| Blocked | blocked |

Every work-unit card shows:

- `U<work_unit_id>`, full title on hover, and exact disposition.
- Developer and Reviewer shortnames.
- Planned wave.
- Dependency IDs in text.
- Output kind.
- Linked PR number and latest normalized PR state when present.
- A completion/cancellation distinction inside Done.

Within each column, order by planned wave and then work-unit ID. Empty columns remain visible so the whole process is legible. At narrow widths the board scrolls horizontally rather than collapsing the five meanings into one ambiguous list.

Dependency wires reuse the Roadmap Flow view's established measured-SVG approach. Wires connect prerequisite to dependent. They are subdued until a card receives hover or keyboard focus; then that card's direct prerequisites and dependents are emphasized. Dependencies remain written on the card and in the modal, so the wires are never the only accessible representation.

Clicking a card opens a read-only modal containing:

- title, ID, disposition, wave, output kind, expected output, and completion result;
- Developer and Reviewer, each linking to the participant's current Sprint conversation when one exists;
- included spec-task IDs and task titles/statuses;
- prerequisites and dependents;
- registered PR links with latest observed state, head, and observation time;
- work-unit-scoped events, messages, judgments, and timestamps needed to audit shell involvement.

Opening a card, participant chat, PR, spec, or Roadmap feature performs no Sprint transition and does not count as participant activity.

## Events and Summaries

Two collapsed sections sit below the board:

1. **Sprint events** — append-only `sprint_events`, newest first, displaying actor shell/system, readable action, and time. Expanding a row shows a sanitized, event-specific detail projection.
2. **Sprint summaries** — a read projection over `sprint_judgments` and `sprint_reports`, newest first, displaying author shell, kind, bounded summary, and time. Expanding opens the full body and related work-unit/report metadata.

Both sections load lazily, use cursor pagination, provide **Load more**, and preserve their open state across board refreshes. A work-unit modal may request the same feeds with `work_unit_id` filtering.

Raw event payload JSON is never sent wholesale to the browser. The projection allowlists known display fields per event type. Unknown event types remain visible with type, actor, and time but no unreviewed payload. Bodies already intended as Sprint judgments/reports may be returned in full within their existing storage bounds.

## API Contract

The browser UI uses FnB session authentication under `/api`; shell bearer-token routes under `/_sc` remain unchanged.

### List Sprints

`GET /api/sprints?lifecycle=<value>&limit=<1..100>&cursor=<opaque>`

- Returns `{items, next_cursor}` newest-first.
- `lifecycle` is optional and allowlisted to exact v2 lifecycle values.
- Each item contains Sprint/feature/Planner identity, lifecycle timestamps, terminal outcome, and per-column counts.
- Cursor order is stable by `(created_at, sprint_id)` and the cursor remains opaque to clients.

### Board Snapshot

`GET /api/sprints/<sprint_id>`

- Returns one consistent SQLite read snapshot containing scope, bound specs, participants/current-conversation links, work units, tasks, dependency edges, registered PRs with latest normalized transition, and feed counts.
- Does not include arbitrary event payloads, whole conversations, transcripts, or unbounded history.
- Uses one read transaction so header, counts, units, and latest PR states cannot describe different moments.

### Lazy Feeds

- `GET /api/sprints/<sprint_id>/events?limit=<1..100>&cursor=<opaque>&work_unit_id=<id>`
- `GET /api/sprints/<sprint_id>/summaries?limit=<1..100>&cursor=<opaque>&work_unit_id=<id>`

`work_unit_id` is optional, integer-only, and must belong to the Sprint. Both endpoints return `{items, next_cursor}`.

### Lifecycle Mutation

`PATCH /api/sprints/<sprint_id>` with `{lifecycle, reason}`

- `lifecycle` accepts only `paused`, `armed`, or `aborted`.
- `armed` is accepted only as paused-to-armed Resume; this route never arms prepared work.
- `reason` is required, nonblank, and bounded to 2,000 characters.
- Unknown fields are rejected.
- A legal transition returns `200` with `{changed, sprint}`. A retry already at the requested state is safe and returns `changed: false`.
- Invalid lifecycle/state combinations return `409`; bad input `422`; unauthenticated `401`; unauthorized `403`; unknown Sprint `404`.
- New endpoints use `{error: {code, message, details}}` and never expose SQL, paths, tokens, or stack traces.

GET routes perform SQLite reads only. They never poll GitHub, touch harnesses, deliver messages, update liveness, or write observation rows.

## Refresh and State

While the Sprints tab is visible, the selected board snapshot refreshes every five seconds. Polling stops immediately when another tab is selected or the document becomes hidden, and resumes with an immediate read when visible again. Events and summaries poll only while their section is open.

Refresh preserves:

- selected Sprint and URL;
- open event/summary accordions and pagination position;
- the selected work-unit modal;
- board horizontal scroll;
- focused card where possible.

Stale responses from an earlier selection or tab generation are discarded. A lifecycle action triggers an immediate refresh after success. Read failures leave the last good board visible with a clear stale/error notice and a manual retry; they do not blank trustworthy prior state.

## Construction Plan

> [!class2]
> Prerequisite: spec #46 U12 must land first. This is one follow-up PR and does not join or modify the active remediation branch.

```linear
U13 Board read API :::class1 -> U14 Header and route :::class2 -> U15 Board and drill-down :::class2 -> U16 Actions and feeds :::class3 -> U17 Adversarial proof :::class3
```

| Unit | Change | Primary areas | Verification |
|---|---|---|---|
| U13 | Add a dedicated Sprint board projection service plus list/detail/feed browser routes | `.super-coder/scripts/`, `.super-coder/api/server.py` | temporary-DB API contracts, auth, cursor, consistent snapshot, no external calls |
| U14 | Reorder global tabs; add `#sprints`, exact-ID routing, selector, Roadmap feature deep link, and polling lifecycle | `.super-coder/ui/index.html`, `app.js` | DOM order, hashes, Back/Forward, empty-hash compatibility, title and tab state |
| U15 | Render five columns, cards, dependency wires, participant/PR/spec links, and read-only audit modal | `app.js`, `style.css` | disposition mapping, keyboard focus, dependency text/wires, narrow viewport |
| U16 | Add lazy event/summary accordions and FnB Pause/Resume/Abort confirmation flows | API/UI projection paths | pagination, sanitized payloads, reason validation, coordinator delegation, state-dependent controls |
| U17 | Add browser, restart, failure, and visual proof; update map-owned descriptions only through normal cartographer flow | `tests/`, visual QA fixtures | full acceptance matrix below |

U13 and the header-only part of U14 are parallelizable because the global nav contract does not depend on payload shape. U15 depends on U13. U16 depends on both U13 and the route/page shell. U17 lands last but its fixtures and test harness can begin alongside U13.

The highest-risk spike is the single-read board projection plus sanitized event mapping. Prove that contract before styling the whole board.

## Risks and Bounds

- **Authorization drift:** Browser FnB auth and shell-authenticated Sprint routes must call the same domain services without sharing credentials or weakening either boundary.
- **Projection tearing:** A board assembled from unrelated reads can show impossible combinations; one read snapshot is required.
- **Payload leakage:** Event payloads may contain implementation details; display projections are allowlisted.
- **Refresh disruption:** Naive `replaceChildren` polling can close modals, lose scroll, and steal focus; UI state is explicit and restored.
- **Graph ambiguity:** Wires are an enhancement, never the only dependency signal.
- **History growth:** Events and summaries are cursor-paged and lazy; the board detail is bounded.
- **Lifecycle duplication:** UI code may request transitions but never reproduce coordinator rules or direct-write Sprint tables.
- **Reference drift:** `SprintBoard.png` is design input, not a runtime or test dependency. This written contract is authoritative.

## Verification Gate

The PR fails its gate unless all of the following are demonstrated:

- Header DOM order is Chats, Sprints, Shells on every route; all existing hashes still resolve; empty hash still opens Shells.
- Prepared, armed, paused, completed, and aborted fixtures each render the correct header, actions, times, and terminal information.
- Every work-unit disposition maps to exactly one column; cancelled never appears completed and `fixing` remains Review work.
- Dependency text is correct without SVG; hover and keyboard focus highlight the same direct edges when SVG is available.
- Unit modal links the correct participant conversations, tasks, dependencies, PR state, and scoped audit entries without causing activity or writes.
- Events and summaries paginate without duplicates or omissions across equal timestamps and reject cross-Sprint work-unit filters.
- Unknown event payloads cannot leak unallowlisted fields.
- Repeated Pause, Resume, or Abort requests are safe; illegal transitions and missing reasons fail with the documented status and error shape.
- GET board polling performs no GitHub, harness, message, liveness, or database write operation.
- Polling stops off-tab/hidden, ignores stale responses, and preserves modal, accordion, focus, and scroll state.
- A read failure leaves the last good snapshot visible and clearly marked stale.
- Visual QA covers a wide desktop board, a populated dependency graph, a terminal Sprint, an empty installation, modal/accordion states, and a narrow horizontal-scroll viewport.
- Restart proof shows the same selected Sprint route and authoritative board after the API process restarts.
- `./sc test`, focused mutation checks, `./sc render-check`, syntax/lint checks, and `git diff --check` pass; `./sc verify` runs only from an owning live checkout per its current worktree restriction.

## Non-Goals

- Editing plans, work units, dependencies, participants, routes, specs, or task membership in the board
- Arming a prepared Sprint from the browser
- Completing a Sprint, authorizing/performing merges, recording reviews, or dispositioning conformance follow-ups
- Sending participant messages from the board; existing Sprint conversations remain the collaboration surface
- Replacing `sc sprint` CLI or shell-authenticated `/_sc/sprint/*` routes
- New Sprint tables, copied board status, stored counts, or migrated Sprint v1 data
- Multiple concurrently armed Sprints
- Streaming/SSE for the first board release
- A general theme or header redesign beyond the requested tab insertion and order

## QAQC Checklist

- Does every displayed field have one authoritative v2 source?
- Can an FnB distinguish lifecycle from work progress and participant disposition?
- Does the board expose advisory close-out gaps without turning them into a hard gate?
- Do Pause, Resume, and Abort preserve existing authority, reports, messages, interrupts, and recovery behavior?
- Can any GET route cause external polling or mutate liveness?
- Can event payloads, report bodies, PR evidence, or conversation identifiers leak secrets?
- Are pagination cursors stable under concurrent appends?
- Can polling overwrite a newer selection or destroy an open audit context?
- Are dependencies understandable without color, hover, or SVG?
- Do terminal Sprints remain inspectable even though live participant pills disappear?
- Does the header change preserve every old deep link and correctly introduce `#sprints`?
- Is the result one coherent follow-up PR after U12 rather than a parallel rewrite of active Sprint remediation?
