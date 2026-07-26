-- 0102 — structured worker-reconciliation alert identity.
--
-- planner_alerts predates the worker reconciler and is already written by
-- Interface wake, recovery, snapshot, and PR polling paths.  Keep those rows
-- valid: every new column is nullable and has no default.  Reconciler alerts
-- retain dedupe_key for the existing open-row uniqueness guard, but their
-- identity is queryable without parsing that string.

BEGIN;

ALTER TABLE planner_alerts
    ADD COLUMN sprint_doc_id INTEGER REFERENCES documents(document_id);
ALTER TABLE planner_alerts
    ADD COLUMN seq TEXT;
ALTER TABLE planner_alerts
    ADD COLUMN role TEXT CHECK (role IN ('dev', 'reviewer', 'planner'));
ALTER TABLE planner_alerts
    ADD COLUMN signal TEXT;
ALTER TABLE planner_alerts
    ADD COLUMN shell_id INTEGER REFERENCES shells(shell_id);

CREATE INDEX IF NOT EXISTS idx_planner_alerts_reconciliation
    ON planner_alerts(sprint_doc_id, seq, role, signal, resolved_at);

COMMIT;
