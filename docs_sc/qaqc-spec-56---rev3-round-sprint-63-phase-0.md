---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Browser-native headless conversations
roadmap_status: in_progress
frozen: false
title: "QAQC: spec #56 — REV3 round (SPRINT 63 phase 0)"
tags: [sprints, qaqc, review, browser, conversations]
date: 2026-08-01
project: super-coder
purpose: Phase-0 QAQC verdict on Segmented assistant responses spec (F24 spec seq 8)
---

# QAQC: spec #56 "Segmented assistant responses" — REV3 round

- **Reviewed artifact:** spec doc #56 (feature #24, spec seq 8), judged against `origin/main` of ~/Repos/subfloor @ `869219e` (fetched 2026-08-01). Local checkout sits on `feat/s2-u13-fnb-board-ui` (F31 U13, PR #881 in flight); all verification read `origin/main` content explicitly — `app.js` differs materially between the two.
- **Reviewer:** REV3 (shell #13), SPRINT 63 phase 0 (task #672). Findings only; no spec edits, no code, no git mutations.
- **Method:** adversarial claim-by-claim verification of the spec's "current behavior", event model, and R1–R7 feasibility against the code on main; then a gap sweep of the spec text itself.

## Verdict: FAIL — one Medium (flag #84), blocking unit activation pending a one-clause R5 amendment

The spec is exceptionally well grounded: every verifiable claim about existing behavior checked out against main (see claim verification below), the construction plan's seams are real, and the four units map cleanly onto tasks #194–#197. One Medium wording defect at the spec's own self-declared highest-risk seam blocks activation; three Lows go to the record.

## Medium finding (blocks activation)

### M1 — R5 cursor anchor is not scoped to the active run (flag #84)

R5 defines `assistant_cursor.segment_anchor_sequence` as "the latest boundary at or below `through_sequence`, or `0` when none exists." R1 and R4 both scope anchoring "within its own run"; R5 drops the run qualifier. Read literally — and unit 2's dev implements exactly this sentence — the cursor query ranges over the whole conversation prefix, so after any earlier run ended with a boundary (e.g. `tool.completed` at seq 917), a fresh active run that has streamed text but hit no boundary yet hydrates `{run_id: N, segment_anchor_sequence: 917}` instead of `0`. The next live delta then forms `run:N:assistant:917` while the historical projection computes `run:N:assistant:0` — snapshot/SSE parity breaks on exactly the seam the spec names its highest risk, surfacing as duplicate-id/anchor-moved reconciliation or silently divergent segment ids.

The scenario is common in real use (multi-turn conversation, earlier turn used tools, current turn is prose so far, operator refreshes) and is not obviously covered by the unit-1 fixture list ("refresh between boundary and delta" presumes a boundary *in the active run*). One-clause fix: "Its anchor is the latest boundary **of that run** at or below `through_sequence`, or `0` when none exists." R5's worked example (`run_id: 42`, anchor 917) should likewise state that 917 belongs to run 42.

## Low findings (recorded, non-blocking)

- **L1 — R3 "adds" fields that already exist.** V1 assistant items already carry `first_sequence` and `last_sequence` (`conversation_routes.py:1708-1709`); only `segment_anchor_sequence` is new. Editorial, but unit 1's fixture author should assert the two existing fields as *preserved*, not introduced.
- **L2 — R4's "existing event view" is a table.** The projection reads `conversation_events` through a CTE (`_transcript_projection`, `conversation_routes.py:1547-1573`); no database view exists. Intent is clear (a SQL window inside the existing event read); the word "view" invites a hunt for a schema object that isn't there.
- **L3 — Permission/input boundaries are unreachable on Claude and Kimi.** `claude.py` and `kimi.py` refuse interactive permission bridging and never emit `permission.requested`/`input.requested`; only `codex.py` and `opencode.py` do. The R1 boundary set is event-type-based so the spec is not wrong, but unit 4's "same multi-phase response journey" across all four harnesses can only exercise tool boundaries on Claude/Kimi. The gate should state that permission/input phases are Codex/OpenCode-only, or name the per-harness journey variants.

## Claim verification (all passed)

Verified against `origin/main` @ `869219e`; line numbers from that revision.

1. **Current behavior (v1):** one assistant item per run, id `run:{run_id}:assistant`, all `assistant.delta` text concatenated across tool activity — `conversation_routes.py:1699-1711`; live reducer uses the same run-wide identity and append rule — `app.js:4055-4076`. ✓
2. **Boundary event types exist on every harness:** `tool.started`/`tool.completed` emitted by claude/codex/kimi/opencode adapters; `permission.requested`/`input.requested` by codex/opencode; all six in `NORMALIZED_EVENTS` (`conversation_adapters/base.py:30-44`). ✓
3. **R2 tool visibility:** routine tool events are excluded from the projection's `activity_types` (`conversation_routes.py:1644-1650`) and from the reducer's activity branch (`app.js:4084-4103`); permission/input render as activity items in durable sequence order. ✓
4. **R3 id/sort contract:** items sort on `(order_sequence, item_id)` already (`conversation_routes.py:1739`), matching the spec's deterministic tie-breaker; `through_sequence` exists and gates SSE replay (`_after_sequence`). ✓
5. **R4 five-query / one-snapshot contract is real and test-enforced:** `test_snapshot_projection_uses_one_fixed_five_read_view` (`tests/test_conversation_api.py:1693`) asserts exactly 5 reads + 1 BEGIN + 1 ROLLBACK. Anchor windows are computable inside the existing event CTE (windows already run over the full pre-cap prefix), and the full-prefix completeness pattern already exists (`assistant_delta_count` correlated subquery, `conversation_routes.py:1532-1535`) — boundary-evidence counting extends it without a new query class. Non-terminal runs are retained under incomplete load (`:1679`), matching R4's bounded-suffix rule; the explicit truncation contract exists (`:1351-1367`). ✓
6. **R5 hydration seam exists:** snapshot install precedes SSE open (`installSnapshot`, `app.js:3750-3769`; stream opens at `:4161`), and SSE delivers boundary events unfiltered (`_event_batch` selects all event types, `conversation_routes.py:1997-2026`; browser subscribes to tool/permission/input types, `app.js:2686-2698`). ✓
7. **R6 performance rules all present on main:** single outstanding animation frame (`app.js:3739-3748`), hidden-Chat suppression via `hiddenDirty` with exactly one catch-up frame on return (`:3739-3741, 3809-3810`), dirty-set per-item flush with keyed node identity, paused-scroll preservation (`followTranscriptTail ? scrollHeight : previousTop`), sequence dedupe with gap-triggered single-flight reconciliation that stops after repeated failure (`:4020-4027, 3771-3795`). ✓
8. **R7 safe failure exists:** `projection_version !== 1` throws before any SSE open (`app.js:3447-3448`); the caller shows a retryable "Transcript unavailable" error with working Close control and preserves app rails (`:4640-4686`). Old conversations need no backfill — tool events are already durable in `conversation_events`, so v2 projects them on read. ✓
9. **Plan mapping:** tasks #194–#197 exist and match units 1–4; all construction-plan files exist (`tests/test_conversation_api.py`, `test_conversation_ui.py`, `test_conversation_diff_browser.py`, `test_conversation_release_gate.py`, `.super-coder/api/conversation_routes.py`, `.super-coder/ui/app.js`). ✓

## Disposition

- Verdict: **FAIL** — one Medium (flag #84) blocks activation of units 1–4 per the phase-0 bar; the amendment is one clause in R5 and I expect a scoped re-round to pass on re-read.
- Lows L1–L3 recorded above for the sprint report; no flags opened for them.
- Spec #56 otherwise reads as implementable within its own estimate (one focused PR-equivalent per unit, no migration, no adapter protocol change).
- Awareness note for unit 3 (not a finding): F31 U13 (PR #881) rewrites ~640 lines of `app.js` around the Chat view; if it merges first, unit 3's base must be re-synced — the sprint doc already carries this caution.
