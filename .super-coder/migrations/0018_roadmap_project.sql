-- 0018 — Roadmap project grouping: roadmap.project_id (work-stream the feature
-- belongs to).
--
-- One additive change: a nullable FK from roadmap → projects. NULL = unassigned
-- (the feature shows under the "Unassigned" board in the GUI). Lets the Board
-- view split into one board per work-stream (e.g. "mi-capture" = F52+F53), each
-- keeping its internal status sections.
--
-- NOT inlined into schema.sql, unlike the 0017 table precedent: roadmap is
-- FK-referenced by feature_blockers / documents / spec_tasks / flags, and SQLite
-- has no `ADD COLUMN IF NOT EXISTS`. Folding the column into schema.sql's roadmap
-- CREATE would make this migration's ALTER fail on a fresh rebuild ("duplicate
-- column"), and the table-rebuild dance (0003) is unsafe for a referenced table.
-- rebuild.py applies schema.sql THEN every migration in order, so a
-- migration-only ADD COLUMN converges on both paths: fresh build (baseline lacks
-- the column → ALTER adds it) and existing fork (ALTER adds it). The ledger keeps
-- it from running twice. schema.sql carries a pointer comment in the roadmap block.
--
-- Per-instance content: roadmap rows are serialized to .sc-state/content.sql by
-- snapshot.py, which reads columns via PRAGMA table_info — project_id flows into
-- the dumped INSERTs automatically, no snapshot change needed. A pre-migration
-- content.sql (INSERT without project_id) still loads: the omitted column
-- defaults NULL, then the next snapshot regenerates the INSERT with project_id.

BEGIN;

ALTER TABLE roadmap ADD COLUMN project_id INTEGER REFERENCES projects(project_id);

CREATE INDEX IF NOT EXISTS idx_roadmap_project ON roadmap(project_id);

COMMIT;
