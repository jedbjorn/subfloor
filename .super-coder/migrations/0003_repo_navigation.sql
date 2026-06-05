-- 0003 — B5 repo-navigation layer: dr_section, dr_filepath.desc, workspace→connections.
--
-- Three additive changes, all convergent so the migration is safe to apply both
-- on an existing fork (the column/table are absent → it adds them) AND on a fresh
-- rebuild (schema.sql already has them → the rebuild converges to the same shape;
-- dr_filepath is empty at this point anyway). Matches the 0002 precedent.
--
--   1. dr_section            — new navigational table (CREATE … IF NOT EXISTS).
--   2. dr_filepath.desc      — per-file descriptions + UNIQUE(path). SQLite has no
--                              ADD COLUMN IF NOT EXISTS, so the table is rebuilt to
--                              the new shape. dr_filepath is a DERIVED cache (no FK
--                              references it; map_repo repopulates it), so losing a
--                              file_id / an unsnapshotted desc here is harmless — on
--                              an existing fork no desc exists yet to lose, and the
--                              very next map UPSERT preserves them thereafter.
--   3. workspace → connections — move any authored workspace text into connections
--                              (the single surviving "where things live" surface),
--                              only where connections is still empty. workspace is
--                              left in place (retired, unrendered) — see schema.sql.

PRAGMA foreign_keys=OFF;

BEGIN;

-- 1. Sectioned navigation -----------------------------------------------------
CREATE TABLE IF NOT EXISTS dr_section (
    section_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    path_prefix  TEXT NOT NULL,
    description  TEXT,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name)
);

-- 2. dr_filepath: add desc + UNIQUE(path) via table rebuild -------------------
CREATE TABLE dr_filepath_new (
    file_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    path     TEXT NOT NULL UNIQUE,
    ext      TEXT,
    lang     TEXT,
    role     TEXT,
    bytes    INTEGER,
    lines    INTEGER,
    desc     TEXT
);

-- Select only the base columns (present in every prior shape) so this works
-- whether or not the source table already has `desc`. desc lands NULL.
INSERT INTO dr_filepath_new (path, ext, lang, role, bytes, lines)
SELECT path, ext, lang, role, bytes, lines FROM dr_filepath;

DROP TABLE dr_filepath;
ALTER TABLE dr_filepath_new RENAME TO dr_filepath;

CREATE INDEX IF NOT EXISTS idx_dr_filepath_role ON dr_filepath(role);
CREATE INDEX IF NOT EXISTS idx_dr_filepath_lang ON dr_filepath(lang);

-- 3. Migrate workspace text into the connections notes layer ------------------
UPDATE shells
   SET connections = workspace
 WHERE (connections IS NULL OR TRIM(connections) = '')
   AND workspace IS NOT NULL AND TRIM(workspace) <> '';

COMMIT;

PRAGMA foreign_keys=ON;
