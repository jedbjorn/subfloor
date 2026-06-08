-- 0012: add spec_tasks table for per-spec implementation plans
-- Convergent: safe to run on forks that already have the table.

CREATE TABLE IF NOT EXISTS spec_tasks (
    task_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id     INTEGER NOT NULL REFERENCES roadmap(feature_id),
    document_id    INTEGER NOT NULL REFERENCES documents(document_id),
    seq            INTEGER NOT NULL,
    title          TEXT    NOT NULL,
    description    TEXT,
    status         TEXT    NOT NULL DEFAULT 'pending'
                   CHECK(status IN ('pending','in_progress','done')),
    completed_date DATE,
    shell_id       INTEGER REFERENCES shells(shell_id),
    created_date   DATE    NOT NULL DEFAULT (date('now')),
    UNIQUE(document_id, seq)
);
