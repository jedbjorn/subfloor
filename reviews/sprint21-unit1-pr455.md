# Sprint 21 · Unit 1 review — session-control schema + state machine

**Sprint:** doc #21 (feature #14) · **Spec:** doc #20 · **Task:** #50 ·
**PR:** #455 (`feat/session-control-state`, DEV3) · **By:** REV1, 2026-07-21 ·
**Checks:** all 6 green at review time.

**Verdict: review-clean — 0 Major · 0 Medium · 2 Low (report-only).**

## Scope reviewed

Three added files: `.super-coder/migrations/0077_session_control_state.sql`,
`.super-coder/scripts/session_control.py`, `tests/test_session_control.py`.
Gated per DEV3's request: migration 0077, one-managed-binding constraint +
indexes, exhaustive state transitions / CAS race behavior, unread-message
reconstruction + dedupe, dirty-upgrade preservation.

## Axis 1 — code quality

- **Migration 0077** matches the spec's DDL column-for-column for both
  `shell_session_bindings` and `session_wake_jobs`, including both CHECK
  constraints and both UNIQUE constraints. Outer `BEGIN;`/`COMMIT;` are on
  their own lines, so `migrate.py`'s `_strip_outer_txn` handles them; the
  file is additive and not folded into `schema.sql` — engine convention held.
  Numbering (0077 after 0076) is collision-free.
- **`transition_binding` CAS** is correct: edge validated first, single
  `UPDATE … WHERE binding_id=? AND state=?`, rowcount 1 = success, and the
  0-row path distinguishes `BindingNotFound` from `StaleBindingState` with
  the actual state in the error. Lease-generation checking is correctly left
  to unit 2.
- **`reconstruct_wake_jobs`** counts via `con.total_changes` delta, which is
  exact under `INSERT OR IGNORE` (ignored rows don't count). Join is on
  `shell_messages.to_shell_id` / `read_at IS NULL` — verified against the
  real schema (`schema.sql:268-274`). Caller-owns-commit is documented.
- FK enforcement is real in production: `db_driver.connect` sets
  `PRAGMA foreign_keys=ON` (db_driver.py:17), so the tests' pragma mirrors
  the live path.

## Axis 2 — edge cases

- Unknown states fail closed (raise, never silently False); tested both
  directions.
- `UNIQUE (harness, native_session_id)` with NULL native IDs: SQLite treats
  NULLs as distinct, so multiple `starting` bindings pre-capture coexist —
  correct.
- Partial unique index `idx_session_bindings_managed_shell (shell_id) WHERE
  managed=1` enforces one live conversation target per shell while keeping
  historical/released rows; tested including the historical-row case.
- Idempotent rescan preserves existing job state/attempt history (tested:
  failed job with attempts survives a rescan that adds a new message's job).
- Dirty upgrade: pre-0077 DB with an unread message upgrades, reconstructs
  exactly one job, message body and read_at untouched. `build_pre_session_control_db`
  breaks at `name >= "0077…"`, so it stays correct when 0078+ land.
- CAS stale-owner race tested; invalid transition and missing binding leave
  the row unwritten.

## Axis 3 — spec conformance

- Schema: exact match to spec doc #20's Data model section. Indexes are in
  scope ("Schema + migration — … indexes, constraints").
- Delivery-plan step 1 ("bindings, wake jobs, transition validation, and
  reconstruction tests without launching providers") fully covered; no
  provider/transport code leaked into the unit — no scope creep.
- Read-ack invariant held: nothing in the unit writes `read_at`; a wake row
  reaching `done` is decoupled from message acknowledgement (migration
  header states it, code honors it).
- Ratified judgement (U1/DEV3): conservative edge graph. Verified in code:
  `starting` cannot dispatch (queue only ✓), `released` → only `starting`,
  `error` → `starting`/`released` — recovery through `starting` before any
  delivery ✓, self-refresh valid ✓. The 49-pair exhaustive test pins the
  graph against an independently duplicated table, so any drift turns red.

## Low findings (report-only, non-blocking)

- **L1 — judgement wording vs implemented graph.** The sprint doc records
  "idle/dormant may enter dispatching", but the implemented graph also allows
  `foreground → dispatching` (and `dispatching` self-refresh). The code is
  right — spec's foreground row is "use active transport if supported", and
  Claude's active-watcher delivery needs that edge — but the recorded ruling
  is under-inclusive. If unamended, the conformance pass will read
  `foreground → dispatching` as deviated-silently. Planner should align the
  judgement wording.
- **L2 — test-coverage nit.** Reconstruction tests cover the managed filter
  via a second shell's unmanaged binding, but not a single shell holding both
  a managed and an unmanaged historical binding. The `WHERE b.managed = 1`
  filter plus the partial unique index make the behavior unambiguous;
  coverage nicety only.
