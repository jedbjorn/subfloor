-- 0268 — document retirement and supersession (feature #84 / spec #251).
--
-- Retirement is metadata ABOUT a document, never an edit to it. The retire
-- surface writes only these three columns plus updated_at; title, body,
-- frozen and frozen_date stay exactly as they were, so a frozen row's
-- immutability is unchanged.
--
--   retired        1 once the document is superseded or withdrawn
--   retired_date   date('now') on the FIRST retirement, kept across pointer
--                  updates, cleared by an undo
--   superseded_by  the successor that replaced it; NULL for a bare retirement
--                  (a document whose subject was removed has no successor)
--
-- schema.sql is the partial core baseline and every rebuild applies it and
-- THEN every migration, so a column lives in exactly one of the two. Adding
-- these to the baseline as well would make a fresh build fail with
-- "duplicate column name" — same convention as 0210 on flags and 0018 on
-- roadmap, whose tables are also in the baseline.
--
-- Additive: no existing row changes value, and every existing read keeps
-- working because nothing is renamed or removed.

BEGIN;

ALTER TABLE documents ADD COLUMN retired INTEGER NOT NULL DEFAULT 0
    CHECK (retired IN (0,1));
ALTER TABLE documents ADD COLUMN retired_date TEXT;
ALTER TABLE documents ADD COLUMN superseded_by INTEGER
    REFERENCES documents(document_id);

COMMIT;
