-- 0083 — durable planner-alert acknowledgement audit.
--
-- Acknowledgement dismisses an open alert from the current warning surface
-- without resolving or deleting it. The row remains durable audit and can
-- still be returned by the explicit history projection.

ALTER TABLE planner_alerts ADD COLUMN acknowledged_at TEXT;
ALTER TABLE planner_alerts ADD COLUMN acknowledged_by TEXT;
