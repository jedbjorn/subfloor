# Sprint 25 — Unit 8: PR #511 brokered planner wake — review + re-review

- **PR:** #511 `feat/interface-brokered-wake` (DEV3, task #84, spec #20)
- **Original review:** ec69aa5 → 1 MAJOR / 2 MEDIUM / 4 Low (flags SC-011/012/013; findings → DEV3 msg #503, PLN1 copied #504)
- **Re-review:** c77fcb1 (CI green 6/6) — **review-clean; all three flags verified fixed and closed**

## Original findings (ec69aa5)

- **SC-011 (MAJOR):** `submit_wake_batch` dereferenced `sess[0]` unguarded. The session query filters `occupancy <> 'ended'`, and `_end_session` (End chat) deliberately does NOT release the sprint binding — so armed binding + queued batch + ended chat crashed every coordinator drain and `startup_pass` with `TypeError: 'NoneType' not subscriptable`. No alert, no state change; batch queued forever; the sprint's wake chain stalled silently. Spec Retry Policy requires harness loss to queue AND alert.
- **SC-012 (MEDIUM):** the submit gate revalidated `_sprint_active` but never the doc's `frozen` flag; ingress and the arm route both check frozen, so a frozen-but-ACTIVE doc kept submitting wakes post-revocation.
- **SC-013 (MEDIUM):** the unmanaged-client probe (tmux `list-clients` subprocess) ran INSIDE the gate's `BEGIN IMMEDIATE` write txn, and no wake-path sync tmux call (probe, writer preflight, `_send_keys_sync`) carried a timeout — a wedged-but-alive tmux hung the drain thread while holding the SQLite write lock (engine-wide write stall, restart-only recovery).
- 4 Low (report only, non-blocking).

## Re-review verification (c77fcb1)

### SC-011 — FIXED
`interface_broker.py:762-776`: `sess is None` → rollback, `_alert(severity="critical", reason="wake_session_ended", binding_id=…)`, commit, return `{"submitted": False, "reason": "session ended"}` — batch stays `queued` for a future generation. Dedup holds via `idx_planner_alerts_open` (partial unique on `dedupe_key WHERE resolved_at IS NULL`) + `INSERT OR IGNORE`. Drain treats it as an ordinary gate failure (awaits next event, no crash). `startup_pass` re-drains → same clean gate_fail, no re-alert.

### SC-012 — FIXED
`interface_broker.py:745-754`: `frozen is None or frozen[0] or not _sprint_active(...)` → `_cancel_batch` — the SAME cancel primitive as the binding-released/close path (batch → `complete` with `completed_at`, batched items → `cancelled`; no byte moves). Fail-closed on a missing doc. Frozen-cancel is edge-identical to close-cancel; nothing strands: unbatched queued items are untouched in both paths alike.

### SC-013 — FIXED
- Probe moved BEFORE `_begin_immediate` (`interface_broker.py:717-720`); only its verdict is applied inside the txn (decision-#15 atomicity of transition+alert+commit preserved).
- `TMUX_SYNC_TIMEOUT_S = 10.0` bounds all three wake-path sync tmux calls (`interface_runtime.py`): probe `list-clients`, writer preflight `display-message`, `_send_keys_sync`.
- Timeout semantics verified against the code: probe timeout → `False` (not "unmanaged"; wedged ≠ bypass — dec #32 fail-open for reachability only); preflight timeout → `PreSendError` = DEFINITE pre-send (no byte moved → batch back to `queued`, bounded 1s/5s/30s retry, then alert `wake_presend_retries_exhausted`); send-keys timeout → `TimeoutExpired` propagates to the broker's generic writer catch → batch parked `delivery_unknown` + critical alert, NEVER auto-replayed (ambiguous, exactly like the crash-window path).

### Regression sweep — all hold
- **Decision #15 gate atomicity:** unmanaged verdict (composer→unknown + alert + commit) still applied atomically inside the txn; only the subprocess moved out.
- **Parking invariant:** drain selects only `queued/submitting/running` batches (`interface_wake.py:214-216`); `delivery_unknown` is never reselected; no replay path.
- **Flag #49** (quiet baseline keyed off REAL provider readiness stamp): untouched, verified in place at `interface_broker.py:816-821`.
- **Flag #50** (hook flock held through POST): `interface_hook.py` untouched by the fix commit.
- **Flag #51** (seq-7 hard req): fix blast radius is broker gate + 3 tmux timeouts + tests; adapter/hook paths untouched.

### Red/green proof (run by REV2, not trusted)
Against unfixed ec69aa5 with the new tests grafted on: SC-011 test errors with the exact `TypeError: 'NoneType' object is not subscriptable` at `interface_broker.py:748`; SC-012 test fails (frozen doc submits); SC-013 probe test fails (`in_txn` True); the 3 wedged-tmux tests error (`TMUX_SYNC_TIMEOUT_S` absent). At c77fcb1: all 6 new tests pass; full files green — `test_interface_wake.py` 37/37, `test_interface_runtime.py` 32/32 (4 skipped), `test_interface_crash_window.py` 18/18, `test_interface_wake_submit.py` 11/11.

## Verdict

**Review-clean.** SC-011/012/013 closed. DEV3 clears to merge under scoped authority and delivers the seq-8 unit report. Lows from the original review stand as report-only.
