-- 0083 — terminal Interface cleanup + orphan alert repair (#529, #533).
--
-- Older closure paths could leave generation-volatile input state attached to
-- a fully ended session. It can no longer be delivered or reconciled, so drop
-- it and revoke any lingering writer lease. Older snapshots could also load
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
