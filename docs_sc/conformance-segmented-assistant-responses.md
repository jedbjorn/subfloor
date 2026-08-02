---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Browser-native headless conversations
roadmap_status: in_progress
frozen: false
---

# CONFORMANCE: Segmented assistant responses

sprint: doc #63 (feature #24) · spec: doc #56 (spec seq 8)
judged: main @ 652e56b — tree identical to origin/main 077370c (squash of PR #896); verified `git rev-parse 652e56b^{tree} == 077370c^{tree}`
method: spec requirements read against code at the pinned SHA only — projection (`.super-coder/api/conversation_routes.py`), live reducer (`.super-coder/ui/app.js`), gates (`tests/test_conversation_{api,ui,diff_browser,release_gate}.py`, `tests/segmented_response_traces.py`). Reviewer spot-verified all load-bearing citations. Execution evidence: CI 6/6 green at the judged SHA (verified in U4 review, result #777); zero-marker sweep re-run at the SHA (zero `xfail`/`expectedFailure` matches repo-wide).
narrative input: ratified rulings R1 (strict staged xfail probes, per-unit marker ownership; amended: same-adapter concurrency probe re-owned U4→U2), R2 (unit-1 fixture anchor corrected tool.started:6→tool.completed:7 per latest-boundary semantics), R3 (temporary v2-API/v1-UI safe-failure window on main, closed by unit 3), R4 (U4 marker set empty post-amendment → cross-harness assertions as direct passing gates).

## Verdicts

| Spec requirement | Verdict | Notes |
|---|---|---|
| R1 — Segment boundary | as-specced | Exact 4-type boundary set in SQL window (`conversation_routes.py:1571-1576`) and reducer (`app.js:4135-4150`); latest-replaces-anchor, no empty bubbles (items only from delta groups, `:1739-1754`), no-boundary run = one anchor-0 item; terminal/usage/session events never anchor. |
| R2 — Tool visibility | as-specced | `tool.*` hidden both sides; `permission.requested`/`input.requested` visible activity items keyed `event:{sequence}` in durable positions (`app.js:4206-4226`); bubble→activity→bubble order asserted exactly in release gate (`test_conversation_release_gate.py:546-607`). |
| R3 — Projection version 2 | as-specced | `TRANSCRIPT_PROJECTION_VERSION = 2` (`:80`); item shape exact — id `run:{run_id}:assistant:{anchor}`, segment-scoped `first/last_sequence`, `order_sequence` = first delta seq, one bounded `text`, shared run `outcome` (`:1739-1754`). No schema/migration change in the feature range (git log over migrations/schema empty). |
| R4 — Consistent historical fold | **deviated-silently** — 1 Medium (F1) | Core fold as-specced: anchor window computed pre-cap inside the CTE (`:1570-1582`), cursor from full pre-cap prefix (`:1538-1544`), evidence counted deltas+boundaries (`:1532-1537` vs `:1677-1683`), incomplete terminal runs omitted (`:1717-1718`), five-read single snapshot (`:1476-1557`, test `:1993-2030`). Deviation: completeness gate is per-MESSAGE, not per-run — see F1. |
| R5 — Active segment cursor | as-specced | `assistant_cursor` only for the active run, run-scoped anchor via `boundary.run_id=r.run_id` subquery with COALESCE 0 (`:1538-1544`, `:1845-1851`); prior-run leak test passes (`test_conversation_api.py:1707-1739`); hydration validated and installed before SSE opens (`app.js:3535-3554`, stream at `:4280`); terminalization clears (`app.js:4260-4266`). |
| R6 — Live reducer | as-specced | All clauses verified in code: immediate in-memory reduce (`app.js:4203`), ≤1 frame outstanding (`:3844-3849`), dirty-segment-only reparse (`:3690-3691`, `:3598-3603`), keyed DOM identity (`:3694-3706`), hidden-Chat suppression (`:3839-3843`), single Diff-return catch-up (`:3909-3910`), append-in-order without rebuild or forced scroll when follow paused (`:3694-3703`, `:3715`). |
| R7 — Compatibility & recovery | as-specced | Version-mismatch safe failure before any DOM/SSE (`app.js:3520-3521`, catch `:4795-4834` — retryable error, rails/controls intact); reconcile triggers: gap (`:4125-4128`), duplicate ids (`:3524-3525`), runless/foreign-run delta (`:4171-4175`); single-flight (`:3874-3879`) with automatic stop after 2 failures (`:3873-3875`); reconcile is read-only GET + installSnapshot, no message-lifecycle touch. R3's ratified v2/v1 window is closed at this SHA — UI requires v2, API serves v2. |
| Failure-behavior table (9 rows + lifecycle no-touch) | as-specced | Each row traced in code (runless tool event ignored for segmentation — window CASE requires `run_id IS NOT NULL`; runless delta → reconcile; boundary-before-text → no empty bubble; consecutive tools → latest anchor; reconnect dedupe by sequence `app.js:4122-4124`; truncation without silent merge; hidden-Chat in-memory only; failed run preserves segments + activity item; no failure path submits/retries/interrupts/closes/reorders/duplicates). |
| Construction 1 — executable traces (10 scenarios) | as-specced | All 10 present: `tests/segmented_response_traces.py` + named tests (plain prose, prose-tool-prose, multiple tools, tool-before-prose, permission/input pauses, pending-boundary refresh, fresh-run cursor-0 leak guard, source truncation ×2, reconnect replay, same-adapter concurrency `test_conversation_release_gate.py:627-674`). |
| Construction 1 gate — v1 fails fixtures | deviated-intentionally (R1, R4) | Staged strict-xfail probes per R1; concurrency probe re-owned U4→U2 per R1 amendment; final state zero markers repo-wide with all assertions as direct passing gates per R4. Sweep re-verified at the SHA. |
| Construction 2 — projection v2 gate | as-specced | Exact ordered ids from one snapshot (`test_conversation_api.py:373-427`); fixed five-read view asserted (`:1993-2030`); no source mutation (`:1820`, `:1822-1911`). R2's fixture anchor correction (tool.completed:7) matches the spec's latest-boundary rule — as-specced content, ratified process. |
| Construction 3 — keyed live segments gate | as-specced | Live/historical equivalence proven by reload-interleaved trace with exact unified assertion (`test_conversation_diff_browser.py:1207-1429`); REAL DOM node identity compared by element reference (`:1413-1420`), plus replacement counters (`:1390-1392`, `:1424`). |
| Construction 4 — cross-harness release gate | as-specced | Four harnesses (`REQUIRED_HARNESSES`, `:32`); actionable boundaries Codex/OpenCode-only (`ACTIONABLE_BOUNDARY_HARNESSES`, `:33`); exact ordered v2 id+text equality per harness (`:511-613`); live smoke with pending-boundary refresh + Chat/Diff switch, draft/controls/node-identity/no-chatter/no-lost-text asserted (`test_conversation_diff_browser.py:1207-1429`); delivery/terminal semantics unchanged (`:345-509`). Scroll not re-proved in the u4 smoke — covered by `test_diff_workspace_preserves_live_chat_and_uses_get_only` (`:850`) and the follow-pause UI test (`test_conversation_ui.py:204-221`). Carried Low F4. |

## Findings

### F1 — Medium · deviated-silently · R4
**Completeness gate is per-message; a non-terminal sibling run masks an incomplete terminal run.**
`conversation_routes.py:1708-1718`: `loaded_complete = all(evidence counts match for run in message_runs)` but the omission gate is `if ... (not loaded_complete and not non_terminal): continue` with `non_terminal = any(run active for run in message_runs)`. When one run of a message is active, a terminal sibling whose segmentation evidence was source-capped (or dropped malformed) is NOT omitted — its retained deltas project with whatever anchors survive, which is exactly the "fabricated merge" R4's completeness check exists to prevent. Spec granularity is per-run: "A terminal run missing required segmentation evidence is omitted…" and the non-terminal allowance is "For an active non-terminal run" — not for its terminal siblings.
Reachability: multi-run message (retry/stacked), one run active, source caps cutting the older terminal run's boundary events. Effect: transient cosmetic mis-segmentation (merged bubble) while the sibling is active; self-corrects when the run terminalizes and the gate applies cleanly. No data loss, no durable effect. Severity reasoning: a real spec-violating path, but narrow, transient, and display-only — Medium; planner rules if disputed.
Suggested direction (planner's call): gate per run — omit the incomplete terminal run's segments while still showing the active run's bounded suffix.

### F2 — Low · carried from U2 review (disclosed there; restated for the record) · R4
Malformed/unsupported-payload events are dropped with warnings-only (`conversation_routes.py:1613-1642`); the resulting whole-turn omission leaves `truncation: null` when no cap also fired (`:1784-1843`) — disclosure is only the per-event `warnings` entries. Banked as a U2 Low ("warning-only omission on malformed boundary payload", v1-inherited).

### F3 — Low · carried from U3 review · R7
Dead backward-anchor reconcile branch, `app.js:4145-4148` — unreachable given the sequence-contiguity guard at `:4125`. R7's backward-anchor protection is in practice enforced by the snapshot-install validator (`:3543-3547`), which is adequate; the dead branch is clutter. Banked as a U3 Low; presence re-verified at this SHA.

### F4 — Low · carried from U4 review · Construction 4
Dead non-segmented fake-adapter branch (`test_conversation_release_gate.py:157-161`, unreachable — every instantiation sets `segmented_trace=True`); scroll preservation not re-proved in the u4 smoke (covered by older tests, see Construction 4 row). Banked as U4 Lows; presence re-verified.

### F5 — Low · new observation · adjacent to R6 (outside strict spec scope)
`message.accepted` corrects the optimistic user bubble's `order_sequence` and re-sorts `state.order` (`app.js:4157-4164`), but the incremental flush never repositions existing DOM nodes to match — only new-node insertion consults order (`:3690-3706`). If the accepted sequence lands after intervening items, the user bubble's DOM position stays optimistic until the next full build. Narrow timing window, no corruption; pre-existing v1-era behavior, not a segmentation deviation. Noted for the backlog.

## Summary

Spec #56 shipped as specced on main @ 652e56b: 13 of 14 verdict rows as-specced, 1 deviated-intentionally under ratified rulings (the evolved construction-1 gate), 1 deviated-silently (F1, Medium). Findings: 1 Medium (F1), 4 Low (F2–F5, of which F2–F4 were already banked in unit reviews). Nothing found that contradicts the unit reports' declared judgements; the R3 compatibility window is closed and main serves matched v2 end to end.


## Addendum — F1 re-run (2026-08-01, scoped task #808)

Scope: F1 ONLY, re-judged after unit 5 (PR #902) merged. Judged main @ d56fdff (squash of #902) — tree identical to the task-pinned 97eb911 (verified `git rev-parse 97eb911^{tree} == d56fdff^{tree}`). F2–F5 stand as recorded above; nothing else re-opened.

**F1 verdict: CLOSED — now as-specced.** `conversation_routes.py:1704-1721` (at d56fdff): the completeness gate is per run — `projected_runs` keeps a run iff its retained evidence count matches `segmentation_evidence_count` OR the run is active (`leased`/`starting`/`running`); segment projection and the new `retained_runs` activity filter iterate `projected_runs` only. The F1 path is dead: an active sibling no longer masks an incomplete terminal run — the terminal run's segments AND its activity items (e.g. `run.failed`) are omitted while the active run's bounded suffix projects, exactly the suggested direction. Message-level omission still occurs when every run of a message is unprojectable (`message_runs and not projected_runs`) — the whole-turn omission case, unchanged in intent. No new query class: the diff is pure Python regrouping over the existing loaded rows; the five-read single-snapshot contract and source budgets are untouched (no SQL in the diff).

Regression coverage: `test_conversation_api.py::test_segmented_terminal_suffix_is_omitted_with_active_sibling` seeds the exact F1 shape (multi-run message, source-capped terminal run + active sibling, `max_source_events=5`), asserts exact item list (user message + active suffix only), asserts no item carries the terminal run's id (pins activity omission), and asserts `assistant_cursor` still tracks the active run. Red-capable against the pre-fix code (old gate retained the message and projected all runs whenever any run was active). CI 6/6 green at the judged tree (verified in the unit-5 review thread).
