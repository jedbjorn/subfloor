# Sprint 25 — Unit 10 review (REV2): PR #513 `feat/interface-operator-workflow` @88f055c

Spec: doc #20 (Interface-backed planner wake) · task #86 · dev: DEV3 · CI: 6/6 green (799 passed / 4 tmux-gated) at head 88f055c.
Verdict: **1 Major, 1 Medium — fix + re-review.** 4 Lows to the report.
Ambiguity rulings (decision #33) honored: route shapes, retry-resolves-its-alerts, close-on-both-triggers all ACCEPTED — not re-flagged. The render-check worktree-ROOT note in the PR body is the known flag #32/#47 foot-gun, not a defect.

## Scrutiny results (planner's five points)

### (1) RETRY — parking invariant: FAIL (Major, SC-015)

The single-batch path is correct and well-proven: `resolve_batch` closes the
park as audit (`delivery_unknown → complete` + `completed_at`), items return to
`queued` with `batch_id=NULL`, the input park 422s without an explicit
`outcome=delivered|not_delivered` verdict, and `notify_binding` re-signals the
coordinator, whose drain forms a NEW batch through `submit_wake_batch` (full
re-gate: armed + ACTIVE + unfrozen + occupied + idle + clean + quiet + hooks).
`test_retry_parked_batch_never_resubmits_park` proves all of this — for exactly
one parked batch.

**The defect is the multi-batch case, which is the COMMON case in production:**

- A parked batch is not "live": `idx_pwb_live` covers only
  `('queued','submitting','running')` (schema.sql:765), and the coordinator's
  `_drain_sync` (interface_wake.py:213-227) forms a NEW batch whenever no live
  batch exists. So after a park (crash-window restart in
  `interface_reconcile.py:86-93`, or submit-hook-missing), the next queued
  message — and the sprint keeps producing `task`/`result`/`pr_event` rows
  while the operator hasn't intervened yet — creates batch2 (queued) alongside
  parked batch1 (delivery_unknown).
- `_retry_binding` (interface_routes.py) selects
  `state IN ('queued','delivery_unknown') ORDER BY batch_id DESC LIMIT 1` →
  picks batch2 → takes the "wake work re-signalled" branch → **batch1 is never
  resolved**. `resolve_batch` is the only code path that frees a parked
  batch's items (startup reconciliation parks the batch but leaves items
  `batched`), so batch1's items are stranded permanently — their messages
  never wake the planner again. Silent wake loss, the exact failure class the
  parking invariant exists to prevent.
- Worse, the retry then runs its blanket alert resolution
  (`resolved_at=datetime('now') WHERE reason IN RETRY_CLEARS AND
  (binding_id=? OR session_id=?)`) — resolving batch1's
  `wake_batch_delivery_unknown` / `crash_window_delivery_unknown` alerts for a
  condition it did NOT address. The status projection shares the same
  DESC-LIMIT-1 shape, so `park` disappears from `GET sprint-bindings` too.
  After one retry the park is invisible everywhere and the items are wedged.

The new tests construct only the single-batch case, so the suite is green over
the hole. Suggested direction (dev's call): resolve ALL `delivery_unknown`
batches of the binding (or order parked-first), and/or refuse the re-signal
while a parked batch exists; make the projection surface any parked batch
regardless of newer live batches; add the two-batch red test.

### (2) CLOSE INTEGRATION — edge divergence: FAIL (Medium, SC-016)

The freeze path is genuinely atomic: `UPDATE documents SET frozen=1` →
`_close_sprint_wake` → one `con.commit()` (server.py `_mem_patch`).

The `status: CLOSED` path is NOT: `patch_document` → `patch_columns` calls
`con.commit()` itself (server.py:649), so the doc edit commits in transaction 1
and `_close_sprint_wake` + its commit run in transaction 2. A crash between
leaves a CLOSED sprint with an armed binding, queued batch, and open alerts —
the "SAME transaction" claim in the docstring/PR body holds only for freeze.
Spec Sprint Scope requires close to "atomically release its wake binding and
cancel queued wake items".

Bounded impact (why Medium not Major): the submit gate re-checks
`_sprint_active`, so no wake can fire for the CLOSED doc (fail-closed), and
the stranded binding is releasable through the existing DELETE route. But the
orphan armed binding blocks the planner's next arming
(`idx_spb_live_planner ... WHERE released_at IS NULL`) until someone does, and
the two triggers the planner ruled must be edge-identical are not.

Shared-helper behavior itself verified clean: `release_bindings_for_sprint`
releases every unreleased binding, cancels queued batches/items with an audit
reason, resolves the released bindings' open alerts, leaves `submitting`/
`running` batches for hook reconciliation, and never touches
`shell_messages.read_at`. `test_status_closed_releases_binding_and_cancels_queue`
+ `test_freeze_releases_binding_and_cancels_queue` prove both paths end-to-end
through the real mem `server.Handler`.

### (3) ALERTS + retry re-arm: PASS (with the SC-015 carve-out)

`GET /api/interface/sprint-alerts`: open-by-default, `include_resolved=1` for
the audit trail, shell actors OR-scoped to their own sessions/bindings.
Dedupe-while-open is `idx_planner_alerts_open` (partial unique on dedupe_key
WHERE resolved_at IS NULL) + `_alert`'s INSERT OR IGNORE — so once retry sets
`resolved_at`, a recurrence inserts a FRESH alert. Re-arm works at the DB
level; a retry that doesn't fix root cause does not permanently silence the
alert **unless** it resolved an alert for a batch it never resolved (that's
SC-015, flagged once, there).

### (4) ACTOR SCOPING + trust boundary (decision #30): PASS

- Shell actor on `GET sprint-bindings`: `planner` is force-overwritten to
  `actor.shell_id` — even an explicit `?planner_shell_id=<other>` returns
  empty (proven by `test_status_shell_actor_sees_only_itself`).
- Shell actor on `GET sprint-alerts`: ANDed OR-scope over own
  sessions/bindings; extra filters can't widen it.
- Retry: shell may retry only its own binding (403 `not_the_planner`);
  released binding → 409 `binding_released` (proven).
- Shell-token route allowlist extended to `sprint-alerts` only; no
  session/writer/stop reach. No new browser-held capability: retry is a
  browser-POST under the existing session+CSRF+Idempotency-Key discipline; the
  UI panel gates on server-computed `retry.applicable`/`needs_outcome` and
  re-verifies nothing client-side. CLI (`sc sprint …`) is a pure API client
  with the shell token; `cmd_retry` mints a fresh uuid key per invocation
  (explicit operator action semantics — correct).

### (5) SKILL THREE-ARTIFACT: PASS (verified hermetically, not via CI-trust)

- Asset edits, migration 0082 (reseed UPSERT, preserves skill_id + grants),
  and `skills_sc/` mirrors are all present; CI render-check green.
- Independent check run in-review: rebuilt a throwaway DB from
  schema.sql + all migrations and compared — `skills.content` body and
  `description` are byte-identical to `assets/skills/{sprint,
  sprint_orchestration}/SKILL.md` (frontmatter excluded by design). Migration
  content carries the asset text exactly.
- Provider-neutrality: the new guidance names claude/codex/kimi only to state
  the steps are identical; no provider-specific instructions anywhere. Reads
  the same on all three harnesses.

## Lows (report-only, do not block)

- L1: `resolve_batch` calls `con.commit()` internally (interface_broker.py:633)
  while its siblings (`release_binding`) declare caller-owns-transaction. Inside
  `_retry_binding`'s `produce()` this splits the retry's side effects into two
  transactions and commits before `_idempotent` records the response.
  Replay-after-crash is benign (re-signal branch), but the convention break
  invites a future mistake.
- L2: retry route parses the id with `int(p.split("/")[4])` — a malformed path
  (`/api/interface/sprint-bindings/retry`) raises an uncaught ValueError. Same
  pattern pre-exists on the DELETE route (seq 8); the new route copies it.
- L3: `_project_binding`'s `park.reason` grabs the newest open alert on the
  binding OR session regardless of reason — an unrelated open alert
  (e.g. unmanaged-writer) would be shown as the park reason. Cosmetic.
- L4: an idempotent REPLAY of a 200 retry re-calls `notify_binding` (stored
  response returned, but the coordinator signal fires again). Harmless — a
  stored-response replay ideally has zero side effects.

## Process notes

- Sprint board (doc #25) seq-10 row still reads `DEV4 / waiting / no PR` — the
  work arrived from DEV3 as PR #513. Planner bookkeeping to catch up, not a PR
  defect.
- Verification performed: CI 6/6 at head; full diff read; code traces through
  interface_broker / interface_wake / interface_routes / server / schema /
  interface_reconcile; hermetic 3-artifact byte-compare; test-gap analysis
  (the two-batch retry case is unconstructed in tests).

## Recommendation

Fix SC-015 (Major) and SC-016 (Medium), push, re-review. Lows ride the sprint
report.

---

# Re-review (r2): PR #513 @66f537a — 2026-07-23

CI: 6/6 green, 801 passed at head 66f537a. Fix commit touches only
`interface_routes.py`, `server.py`, `test_interface_sprint_ops.py` — the
r1-verified surfaces (3-artifact skill chain, migration 0082, dedupe partial
unique, actor scoping) are byte-untouched and carry over.

Verdict: **REVIEW-CLEAN. Both findings verified fixed; flags #57/#58 closed.**

## SC-015 (was Major) — VERIFIED FIXED

- `_retry_binding` now selects **every** `delivery_unknown` batch of the
  binding (`ORDER BY batch_id`, no DESC-LIMIT-1) and resolves each via
  `resolve_batch` — the parked batch closes as audit, its items return to
  `queued`/`batch_id=NULL` for a NEW batch through the coordinator. The
  newest-first pick and the re-signal-over-park branch are gone.
- Alert clears are now **conditional on what the retry actually remedied**:
  parked → `wake_batch_delivery_unknown`, input verdict →
  `crash_window_delivery_unknown`, re-signal → `wake_presend_retries_exhausted`.
  The blanket RETRY_CLEARS UPDATE is deleted. An unrelated open alert
  survives the retry (proven by test).
- `_wake_state` checks `delivery_unknown` FIRST — a park shadows any newer
  live batch; `_project_binding` surfaces `park.batch_id` from its own
  parked-batch query while `current_batch` still shows the newer live one.
  DESC-LIMIT-1 hiding is gone from both surfaces.
- **Red-test proof (run, not assumed):** the new
  `test_retry_resolves_parked_batch_behind_newer_live_batch` and
  `test_closed_close_is_atomic_under_fault` were executed against the OLD
  code (88f055c): both fail — the old projection reads `queued` over the
  hidden park, and the old close path lands CLOSED with the binding still
  armed. Genuine red, not test-the-test.
- Single-batch parking invariant (r1-cleared) still holds — all 16 tests in
  `test_interface_sprint_ops.py` pass at the new head.

## SC-016 (was Medium) — VERIFIED FIXED

- `patch_columns`/`patch_document` take `commit=False`; the
  `PATCH /docs/{id}` route now runs doc patch + `_close_sprint_wake` + ONE
  `con.commit()` — structurally identical to the freeze path (same helper,
  same commit shape). Default `commit=True` preserves all other callers.
- Fault-injection test proves atomicity: `_close_sprint_wake` raising → 500,
  doc still ACTIVE, binding still armed, item still queued, alert still
  open. Neither side lands. Verified red against the old two-transaction
  code.

## Regression one-pass — HOLD

- Input-park verdict gate: 422 `outcome_required` still precedes
  `reconcile_input` and fires only when the session's input is parked. ✓
- Actor scoping: 403 `not_the_planner`, 409 `binding_released`, and the
  status/alerts scoping are untouched by the fix diff. ✓
- Alert re-arm via `idx_planner_alerts_open` partial unique: untouched. ✓
- 3-artifact skill chain: not in the fix diff; r1 byte-compare stands. ✓
- L1–L4: not reopened by the fix.

## New Low (report-only)

- L5: the conditional clears introduce a narrow replay gap on top of L1 —
  `resolve_batch` still commits internally, so a crash between that commit
  and the clears commit leaves the batch resolved with its
  `wake_batch_delivery_unknown` alert open; a retry replay then finds no
  parked batch and the conditional clear never touches the stale alert
  (the old blanket clear would have healed it on replay). Requires a crash
  mid-retry; operator-visible stale alert, no wake loss. Fix with L1
  (caller-owns-transaction) if ever addressed.

## Recommendation

Merge-ready. DEV3 merges under scoped authority and delivers the seq-10
unit report. Flags #57 (SC-015) and #58 (SC-016) closed by REV2.
