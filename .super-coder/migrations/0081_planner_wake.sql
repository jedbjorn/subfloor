-- 0081 — Transactional brokered planner wake (sprint 25 seq 8, spec #20
-- task #84, HARD requirement #49 from decisions #28/#31).
--
-- provider_ready_at stamps REAL provider readiness: the provider-native
-- session_start hook (never the entrypoint's pre-exec identity claim). The
-- wake gate's quiet debounce measures from this stamp — a >3s claude/codex
-- boot can no longer let a queued wake submit into an unpainted TUI just
-- because the pre-exec occupied_at baseline already aged past the debounce
-- (flag #49). NULL = the provider has not proven readiness yet; the gate
-- then falls back to occupied_at/created_at exactly as before.

ALTER TABLE interface_sessions ADD COLUMN provider_ready_at TEXT;

-- queued -> done wake-item edge (spec #20 Wake Delivery: a message handled
-- — read — during another batch's turn "completes it" without riding a
-- batch of its own), and running -> quarantined (the third completed wake
-- turn quarantines an unread item at stop-hook reconciliation). The trigger
-- is the DB backstop; DROP + recreate with the widened edge set
-- (interface_state.WAKE_ITEM_EDGES mirrors it;
-- tests/test_interface_transitions.py walks both layers for drift).

DROP TRIGGER trg_pwi_state;
CREATE TRIGGER trg_pwi_state
BEFORE UPDATE OF state ON planner_wake_items
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state = 'queued'      AND NEW.state IN ('batched','done','quarantined','cancelled')) OR
    (OLD.state = 'batched'     AND NEW.state IN ('queued','submitting','cancelled')) OR
    (OLD.state = 'submitting'  AND NEW.state IN ('queued','running','cancelled')) OR
    (OLD.state = 'running'     AND NEW.state IN ('done','reconcile','queued','quarantined','cancelled')) OR
    (OLD.state = 'reconcile'   AND NEW.state IN ('queued','done','cancelled')) OR
    (OLD.state = 'quarantined' AND NEW.state IN ('queued','cancelled'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal wake item transition');
END;
