-- 0152 — durable non-code work-unit completion evidence.
--
-- Existing work units remain code lanes.  Planner-marked report-only/no-code
-- lanes may finish through the shell-facing completion surface with their
-- result retained on the authoritative work-unit row.

BEGIN;

ALTER TABLE sprint_work_units ADD COLUMN output_kind TEXT NOT NULL DEFAULT 'code'
    CHECK (output_kind IN ('code','report_only','no_code'));

ALTER TABLE sprint_work_units ADD COLUMN completion_result TEXT
    CHECK (
      completion_result IS NULL
      OR length(trim(completion_result)) BETWEEN 1 AND 8000
    );

COMMIT;
