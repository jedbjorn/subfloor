-- 0080 — watched-PR polling cutover (spec #20 task #85): rebuild the legacy
-- UNIQUE(repo, pr_number, shell_id) table constraint into one ACTIVE watch per
-- (repo, PR, subscriber, sprint scope). Closed rows are retained as history —
-- re-arming a retired watch now inserts a NEW row instead of reopening the old
-- one, which the old constraint made impossible.
--
-- The rebuild dance (create v2 / copy / drop / rename) is the only way to drop
-- a table-level UNIQUE in SQLite. FK stays ON (migrate.py wraps every file in
-- one transaction, where PRAGMA foreign_keys is a no-op): the implicit
-- DELETE FROM that DROP TABLE performs is safe because pr_poll_observations —
-- the only child table — has no production writers before this unit lands.
-- watch_id values are preserved; the old UNIQUE guarantees at most one row per
-- (repo, pr, shell), so the new partial index cannot be violated by the copy.

BEGIN;

CREATE TABLE watched_prs_v2 (
    watch_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    repo           TEXT    NOT NULL,          -- owner/name
    pr_number      INTEGER NOT NULL,
    shell_id       INTEGER NOT NULL REFERENCES shells(shell_id),
    last_seen      TEXT,                      -- JSON: normalized fingerprint
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at      TEXT,                      -- set on merge/close; NULL = live
    sprint_doc_id  INTEGER REFERENCES documents(document_id)
);

INSERT INTO watched_prs_v2
    (watch_id, repo, pr_number, shell_id, last_seen, created_at, closed_at,
     sprint_doc_id)
SELECT watch_id, repo, pr_number, shell_id, last_seen, created_at, closed_at,
       sprint_doc_id
FROM watched_prs;

DROP TABLE watched_prs;
ALTER TABLE watched_prs_v2 RENAME TO watched_prs;

-- The 0059 live-filter index rode the dropped table; recreate it.
CREATE INDEX IF NOT EXISTS idx_watched_prs_live
    ON watched_prs(closed_at) WHERE closed_at IS NULL;

-- One ACTIVE watch per (repo, PR, subscriber, sprint scope) — the cutover's
-- uniqueness contract. COALESCE folds unscoped (NULL sprint_doc_id) watches
-- into the same key space: an unscoped live watch and a sprint-scoped live
-- watch for the same PR can coexist (the unscoped one is dormant until
-- rebound); two live watches in the SAME scope cannot.
CREATE UNIQUE INDEX IF NOT EXISTS idx_watched_prs_active
    ON watched_prs(repo, pr_number, shell_id, COALESCE(sprint_doc_id, 0))
    WHERE closed_at IS NULL;

COMMIT;
