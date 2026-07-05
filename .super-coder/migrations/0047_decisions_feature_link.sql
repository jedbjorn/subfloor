-- 0047 — shell_decisions: add feature_id + document_id (the why-audit link).
--
-- shell_decisions was an unlinked log: it recorded WHAT was decided and (via
-- rationale) the reasoning, but nothing tied a decision back to the feature it
-- shaped or the spec it came out of. The rest of the memory model is already
-- feature-centric — roadmap (the feature), documents (its specs/docs),
-- spec_tasks (the how), flags (its blockers via feature_id). Decisions were the
-- one surface with no edge to a feature, so a feature's page could show its
-- specs and tasks but not the WHY behind them.
--
-- Two nullable FKs close that gap:
--   feature_id  — coarse link: which roadmap feature this decision serves.
--                 Attachable even before any spec exists (an architecture call
--                 made at brainstorm stage). Mirrors flags.feature_id exactly.
--   document_id — fine link: the specific spec/doc revision this decision came
--                 out of or reshaped. A document already rolls up to a feature,
--                 so document_id is a refinement of feature_id, not a rival to
--                 it. Agreement between the two is a convention (documented in
--                 the `memory` skill), not a DB constraint — kept loose on
--                 purpose so a decision can point at a feature without a doc, or
--                 at a doc whose feature is implicit.
--
-- Both NULL by default: a decision unrelated to any feature simply stays
-- unlinked. No backfill — historical rows keep NULL/NULL.
--
-- NOT inlined into schema.sql's shell_decisions CREATE, following the 0018
-- roadmap.project_id precedent: rebuild.py applies schema.sql THEN every
-- migration in order, and SQLite has no `ADD COLUMN IF NOT EXISTS`, so folding
-- the column into the baseline CREATE would make this ALTER fail on a fresh
-- build ("duplicate column"). A migration-only ADD COLUMN converges on both
-- paths — fresh build (baseline lacks it → ALTER adds it) and existing fork
-- (ALTER adds it) — and the ledger keeps it from running twice. schema.sql
-- carries a pointer comment in the shell_decisions block.
--
-- Per-instance content: shell_decisions is a PER_INSTANCE_TABLE in snapshot.py,
-- serialized column-by-column via PRAGMA table_info, so feature_id/document_id
-- flow into .sc-state/content.sql automatically (as flags.feature_id already
-- does). content.sql loads after migrations on rebuild; roadmap/documents rows
-- serialize alongside, so the FK targets are present.
--
-- Plain SQL: migrate.py owns the transaction and the schema_migrations row.

ALTER TABLE shell_decisions ADD COLUMN feature_id  INTEGER REFERENCES roadmap(feature_id);
ALTER TABLE shell_decisions ADD COLUMN document_id INTEGER REFERENCES documents(document_id);

CREATE INDEX IF NOT EXISTS idx_shell_decisions_feature  ON shell_decisions(feature_id);
CREATE INDEX IF NOT EXISTS idx_shell_decisions_document ON shell_decisions(document_id);
