-- 0084 — terminal Interface cleanup + orphan alert repair (#529, #533).
--
-- Older closure paths could leave generation-volatile input state attached to
-- a fully ended session. Drop ordinary state, but preserve metadata-only
-- pending/delivery-unknown evidence as a parked observation (decision #16);
-- terminal audit must not block update, but it must not be silently erased.
-- Revoke any lingering writer lease. Older snapshots could also load
-- planner_alerts with FK enforcement disabled while omitting one of their
-- parents; delete those alerts before a reused integer ID can reattach them to
-- a different session/binding/message/watch generation.

BEGIN;

DELETE FROM interface_input_state
WHERE session_id IN (
    SELECT session_id
    FROM interface_sessions
    WHERE occupancy='ended'
      AND lifecycle='ended'
      AND ended_at IS NOT NULL
)
  AND pending_seq IS NULL
  AND delivery <> 'delivery_unknown';

UPDATE interface_input_state
SET composer='unknown',
    delivery='delivery_unknown',
    updated_at=datetime('now')
WHERE session_id IN (
    SELECT session_id
    FROM interface_sessions
    WHERE occupancy='ended'
      AND lifecycle='ended'
      AND ended_at IS NOT NULL
)
  AND (pending_seq IS NOT NULL OR delivery='delivery_unknown');

INSERT OR IGNORE INTO planner_alerts
    (session_id, severity, reason, dedupe_key)
SELECT session_id,
       'critical',
       'crash_window_delivery_unknown',
       CAST(session_id AS TEXT) || '|-|-|crash_window_delivery_unknown'
FROM interface_input_state
WHERE delivery='delivery_unknown'
  AND EXISTS (
      SELECT 1
      FROM interface_sessions s
      WHERE s.session_id=interface_input_state.session_id
        AND NOT (
            s.occupancy='ended'
            AND s.lifecycle='ended'
            AND s.ended_at IS NOT NULL
        )
  );

-- Legacy closure could leave an already-raised session alert open.  Keep the
-- original audit row, but a fully ended session has no live actor and therefore
-- no session-scoped alert may remain actionable.
UPDATE planner_alerts
SET resolved_at=(
    SELECT s.ended_at
    FROM interface_sessions s
    WHERE s.session_id=planner_alerts.session_id
)
WHERE resolved_at IS NULL
  AND EXISTS (
      SELECT 1
      FROM interface_sessions s
      WHERE s.session_id=planner_alerts.session_id
        AND s.occupancy='ended'
        AND s.lifecycle='ended'
        AND s.ended_at IS NOT NULL
  );

UPDATE interface_writer_leases
SET revoked_at=COALESCE(revoked_at, datetime('now')),
    revoke_reason=COALESCE(revoke_reason, 'session_end')
WHERE revoked_at IS NULL
  AND session_id IN (
      SELECT session_id
      FROM interface_sessions
      WHERE occupancy='ended'
        AND lifecycle='ended'
        AND ended_at IS NOT NULL
  );

DELETE FROM planner_alerts
WHERE (session_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM interface_sessions s
           WHERE s.session_id=planner_alerts.session_id
       ))
   OR (binding_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM sprint_planner_bindings b
           WHERE b.binding_id=planner_alerts.binding_id
       ))
   OR (message_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM shell_messages m
           WHERE m.message_id=planner_alerts.message_id
       ))
   OR (watch_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM watched_prs w
           WHERE w.watch_id=planner_alerts.watch_id
       ));

COMMIT;
