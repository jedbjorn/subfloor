-- 0002 — add 'retired' to the roadmap_status CHECK constraint.
--
-- Motivation (per the upstream proposal): a feature may be decided-against,
-- split into successors, absorbed, or replaced — taken off the roadmap
-- without being shipped. Today the only options are a status lie
-- (in_progress / shipped) or deleting the row (loses history). 'retired' is
-- the honest terminal status. Last in the funnel order because it sits
-- beside shipped: shipped means we delivered; retired means we chose not to.
--
-- SQLite cannot ALTER a CHECK constraint, so the table is rebuilt. The roadmap
-- is small (typically <100 rows); the copy is cheap. No data migration is
-- needed — the new value is additive, so every existing row stays valid.
--
-- FK note: documents.feature_id and flags.feature_id BOTH declare
-- `REFERENCES roadmap(feature_id)`. The drop/rename is safe because (a)
-- migrate.py opens the connection with foreign_keys at its default (OFF), so
-- the drop is not blocked, and (b) we restore a table of the *same* name, so
-- those references resolve again afterward. PRAGMA foreign_keys=OFF is set
-- defensively in case a future caller has it enabled; it must toggle outside
-- a transaction, hence the placement around BEGIN/COMMIT.

PRAGMA foreign_keys=OFF;

BEGIN;

CREATE TABLE roadmap_new (
    feature_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    roadmap_status TEXT    NOT NULL DEFAULT 'brainstorm'
                   -- funnel order: idea inlet → most-active committed work →
                   -- done (shipped) → taken-off-the-board (retired).
                   CHECK (roadmap_status IN
                       ('brainstorm','in_progress','next','near_term',
                        'long_term','shipped','retired')),
    sort_order     INTEGER NOT NULL DEFAULT 0,
    owning_shell   INTEGER REFERENCES shells(shell_id),
    summary        TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO roadmap_new
  (feature_id, title, roadmap_status, sort_order, owning_shell, summary,
   created_at, updated_at)
SELECT
  feature_id, title, roadmap_status, sort_order, owning_shell, summary,
  created_at, updated_at
FROM roadmap;

DROP TABLE roadmap;
ALTER TABLE roadmap_new RENAME TO roadmap;

CREATE INDEX idx_roadmap_status ON roadmap(roadmap_status, sort_order);

COMMIT;

PRAGMA foreign_keys=ON;
