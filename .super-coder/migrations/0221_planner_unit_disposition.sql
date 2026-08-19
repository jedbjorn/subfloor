-- 0221 — Planner unit disposition verbs (spec #161, issue #1220).
--
-- resolve-unit supersedes a unit's sprint_pr_work_units links instead of
-- deleting them: the row stays as link history and every driver of the lane
-- (PR watcher routing, merge completion, resume PR reconciliation) reads only
-- active (unsuperseded) links.  A Planner-attributed completion records its
-- provenance on the unit row; merge/dev completions leave it NULL.

BEGIN;

ALTER TABLE sprint_pr_work_units ADD COLUMN superseded_at TEXT;

ALTER TABLE sprint_work_units ADD COLUMN completion_source TEXT
    CHECK (completion_source IS NULL OR completion_source='planner_override');

COMMIT;
