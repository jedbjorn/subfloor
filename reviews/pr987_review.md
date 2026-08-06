Review of e78ef4c against spec `sprints-v2.1-new-wake-delay` (decision #81). Adversarial pass, contract-walking; all claims traced to code at the PR head. Target tests re-run locally at the head (test_sprint_skills handoff-ordering + reseed parity, test_sprint_message_delivery, test_conversation_ui, test_api_endpoints — 117 passed). CI green.

## Verdict: clean — 0 Major, 0 Medium, 2 Low (notes only)

## Contract walk

**Engine (`sprint_message_delivery.py`)** — conforms.
- `available_at` stamp on `new`-declared enqueue: present in `_coalesce_wake`, reached by both send paths (`_send` sprint-scoped, `send_to_shell_in_transaction` shell-scoped). All `declared_type="new"` producers (dispatch assignments `sprint_domain.py:2487`, arming `:1910`, review requests `sprint_review_loop.py:96`) route through the store — no bypass. The direct outbox inserts in `activity_monitor.py` / `sprint_domain.py` are spec-#89 recovery/requeue machinery carrying no declared-new message sends; out of scope, correctly untouched.
- MAX() extension on coalesce: correct. `available_at` is `TEXT NOT NULL DEFAULT (datetime('now'))` (migration 0146/0164), so `MAX(available_at, ?)` never sees NULL; `_stamp` produces a fixed-width UTC format, so the lexicographic MAX is a true temporal max.
- `SC_WAKE_NEW_DELAY_SECONDS`: read once at store construction, default 15, `0` disables, negative rejected.
- Delivery side untouched: `claim_next` still claims `pending AND available_at<=now`; drain-all-undelivered and claim-time coordinate-mode promotion (the `CASE … THEN 'new'` in the claim query, deliberately unstamped) are unchanged. The expired-`delivering` re-claim path ignores `available_at`, which is correct — it was already due when first claimed.

**UI (server.py / app.js / style.css)** — conforms.
- Projection: one scalar subquery in `get_shells`, filtered `state='pending' AND available_at>datetime('now')`; delivered wakes drop out (test-pinned).
- Sidebar card: red clock `◷` (`#ef545f`) with tooltip `wake message pending — delivering in ~Ns`, matching the spec text; refreshes ride the existing 2 s history poll and a `run.started` SSE hook — no new `setInterval` (test-pinned count == 1).

**Skills + migration 0170** — conforms. All three role skills now carry explicitly ordered, handoff-last-then-stop procedures; planner dispatches only from the dev-handoff wake turn. Migration 0170 is a full-body UPSERT that converges dirty rows and replays idempotently, with asset parity proven by `test_terminal_handoff_reseed_converges_dirty_rows_and_replays_idempotently` (full-row tuple comparison — re-run locally, green).

**Tests** — pin the stamp (exact `12:00:15`), the coalesce extension, the 0-disable, the re-enter-untouched case, the claim boundary at the future stamp, and the shell-scoped path. Legacy suites are pinned to `SC_WAKE_NEW_DELAY_SECONDS=0` so old expectations still assert pre-delay behavior.

## Specific check: `skills_sc/` mirror

**Current main has retired the mirror — no render artifact is missing.** Commit `8a44d26` (PR #572, "local artifact persistence mode") removed `skills_sc/` and `docs_sc/` from the source index; this source repo now persists renders under the ignored `.sc-state/local/`, and `render-check.yml` was rewritten to prove the public seed/migrations reconstruct the catalogue instead of comparing a tracked mirror. The PR's two-artifact shape (asset + reseed migration) is correct for current main, and render-check is green on the head. The spec's "three-artifact commit … re-rendered `skills_sc/` mirror" language is stale doctrine, not a defect in this diff — worth a one-line correction wherever that doctrine text lives.

## Low notes (non-blocking)

1. `app.js` (`run.started` handler in `chatRenderOpen`): `onWakeDelivered` fires on *every* run start of the open conversation, not only wake deliveries, and reads `conversation.shell.shell_id` without the optional chaining used elsewhere (`chatHeaderLabel`). Harmless in practice — an extra `/shells` fetch per run start — but the guard costs nothing.
2. `tests/test_sprint_message_delivery.py`: the MAX-rule test only covers extending immediate→future. A regression that *overwrites* a later existing stamp with an earlier one is unpinned. Given the spec's empirical bar, a note only.

Merging is yours; from the review gate this is clean to merge.
