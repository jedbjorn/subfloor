-- 0017 — Feature blockers: the roadmap's sequencing edges (feature_blockers).
--
-- One additive, convergent change — safe both on an existing fork (the table is
-- absent → created) and on a fresh rebuild (schema.sql already has it →
-- CREATE … IF NOT EXISTS converges to the same shape). Matches the 0002/0003/0004
-- precedent.
--
-- A directed many-to-many self-relation on roadmap. One row = one dependency:
-- `feature_id` is blocked by `blocked_by` (blocked_by must land first). A feature
-- may be blocked by many others. The roadmap Flow view renders each edge as an
-- arrow between feature nodes, grouped into per-stage subgraphs.
--
--   PK (feature_id, blocked_by) — dedups edges; idempotent re-inserts.
--   CHECK (feature_id <> blocked_by) — no self-block.
--   Cycle prevention is app-level (api/server.py), not a DB constraint — SQLite
--   can't express "reject if this edge closes a cycle." Keeping the graph a DAG
--   is what lets the flowchart render cleanly.
--   Edges among brainstorm/retired features are stored but simply not drawn;
--   those stages don't sequence yet.
--
-- Per-instance content: edge rows are serialized to .sc-state/content.sql by
-- snapshot.py (PER_INSTANCE_TABLES), so they survive ./sc update / rebuild.
--
-- The (blocked_by) index serves the reverse lookup "what does this feature block"
-- and the cycle-detection walk.

BEGIN;

CREATE TABLE IF NOT EXISTS feature_blockers (
    feature_id  INTEGER NOT NULL REFERENCES roadmap(feature_id),
    blocked_by  INTEGER NOT NULL REFERENCES roadmap(feature_id),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (feature_id, blocked_by),
    CHECK (feature_id <> blocked_by)
);

CREATE INDEX IF NOT EXISTS idx_feature_blockers_blocked_by
    ON feature_blockers(blocked_by);

COMMIT;
