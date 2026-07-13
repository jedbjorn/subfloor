-- 0064 — spec_tasks: terminal 'cancelled' status + resolution_notes (#342).
--
-- The roadmap doctrine (db_map) names **split** a first-class operation, but
-- spec_tasks had no terminal state other than 'done' — a task whose work
-- moved to the split-off feature was stranded 'pending' under a shipped
-- feature forever, and the only ways out were both wrong: mark it done (a
-- lie) or leave the drift. `sc mem task cancel <id> --notes "…"` now closes
-- it honestly, mirroring `flag close --notes`: the row is preserved, the
-- status says the work was never built, the notes say why (e.g. "moved to
-- F117 as task #NNN").
--
-- SQLite can't ALTER a CHECK constraint, so this is a table rebuild. The
-- baseline schema.sql CREATE is updated to the new shape in the same commit;
-- on a fresh build the rebuild copies an empty table (content restores after
-- migrations), on an existing fork it carries every row. The copy lists the
-- pre-0064 columns explicitly so it works against either shape.

BEGIN;

CREATE TABLE spec_tasks_new (
    task_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id     INTEGER NOT NULL REFERENCES roadmap(feature_id),
    document_id    INTEGER NOT NULL REFERENCES documents(document_id),
    seq            INTEGER NOT NULL,
    title          TEXT    NOT NULL,
    description    TEXT,
    status         TEXT    NOT NULL DEFAULT 'pending'
                   CHECK(status IN ('pending','in_progress','done','cancelled')),
    completed_date DATE,
    resolution_notes TEXT,               -- why a cancelled task ended (mirrors flags)
    shell_id       INTEGER REFERENCES shells(shell_id),
    created_date   DATE    NOT NULL DEFAULT (date('now')),
    UNIQUE(document_id, seq)
);

INSERT INTO spec_tasks_new (task_id, feature_id, document_id, seq, title,
                            description, status, completed_date, shell_id,
                            created_date)
    SELECT task_id, feature_id, document_id, seq, title,
           description, status, completed_date, shell_id,
           created_date
    FROM spec_tasks;

DROP TABLE spec_tasks;

ALTER TABLE spec_tasks_new RENAME TO spec_tasks;

COMMIT;
