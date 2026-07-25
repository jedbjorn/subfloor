-- 0098 — the sprint board becomes a record (spec doc 58 "The board becomes a
-- record", feature 27, sprint doc 59 U1).
--
-- The worker expectation reconciler compares a declared belief about who
-- should be doing what against machine-observable activity. Today it cannot,
-- because the belief is a markdown table inside a `documents` body. Two
-- consequences, both load-bearing:
--
--   1. There is no structured assignment surface that carries a REVIEWER.
--      `spec_tasks` has exactly one `shell_id` and no reviewer column, so a
--      reviewer assignment is not representable at all — which is why flag
--      #185's instance 4 (a reviewer dead while the dev believed it was
--      re-reviewing) is invisible to any comparator built on today's data.
--      This is structural, not a data accident: no amount of task rows fixes
--      a missing column.
--   2. Unit state lives in a prose cell a planner edits by hand, so it can
--      silently no-op the way a drifted string replace did.
--
-- So the unit table moves into the DB and the document keeps its prose. The
-- record is the source; the rendered table is a view (`sc sprint board`),
-- printed on demand and deliberately NOT written back into the body — a
-- materialised copy would state the board twice, which is the drift this
-- table exists to delete.
--
-- ── The planner is the only writer ──────────────────────────────────────────
--
-- FnB directive, and it is the whole point rather than a permission detail:
-- devs and reviewers work and message, and the planner moves the board as
-- their messages arrive. If a worker could mark its own unit done, the board
-- would agree with reality BY CONSTRUCTION and the comparison this service is
-- would have nothing to compare. Enforcement is at the API (the sprint's
-- binding planner_shell_id, else the sprint document's author); this table
-- records WHO moved belief in updated_by_shell_id so the write is attributable
-- either way.
--
-- ── CURRENT STATE ONLY ──────────────────────────────────────────────────────
--
-- FnB ruling 2026-07-25: no state-change log. The board tells the story; a
-- per-move audit trail is a table nobody would read. The cost is accepted
-- knowingly and is recorded here so a later reader does not "fix" it: "when
-- did belief and reality first diverge" is answerable only back to
-- state_changed_at, and the alert rows carry the rest.

BEGIN;

CREATE TABLE IF NOT EXISTS sprint_units (
    unit_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_doc_id     INTEGER NOT NULL REFERENCES documents(document_id),
    -- TEXT, not INTEGER: every message, board and task row in a sprint names
    -- its unit "U1". An integer column would force a lossy strip at each of
    -- them, and the reconciler keys on what the planner typed.
    seq               TEXT    NOT NULL,
    unit_title        TEXT    NOT NULL,
    -- BOTH roles, as records. dev_shell_id alone is what today's surface
    -- already has; reviewer_shell_id is the gap that blocked everything.
    -- Both are nullable because an unassigned slot is a real board state
    -- (the planner declares the units, then fills the roles).
    dev_shell_id      INTEGER REFERENCES shells(shell_id),
    reviewer_shell_id INTEGER REFERENCES shells(shell_id),
    state             TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (state IN ('pending','working','in_review',
                                       'blocked','merged','cancelled')),
    -- The units this one waits on, as the board writes them ("U1,U3"). A join
    -- table would buy graph traversal that nothing in this service performs.
    depends_on        TEXT,
    -- The merge-surface annotation the markdown board carries in the SAME cell
    -- as the dependency — "owns migration 0098; schema.sql + scripts/sprint.py",
    -- "shares SKILL.md with U8 — MUST rebase onto merged U2". It is load-bearing
    -- prose, not decoration: the merge protocol turns on whether a unit's head
    -- can stand or must rebase, and that is the only place the answer is
    -- written. Storing the comma list alone would make this table hold LESS than
    -- the markdown it replaces — a lossy migration rather than a fix. Free text
    -- deliberately: nothing parses it, a planner writes it for a human to read,
    -- and `sc sprint board` renders it beside depends_on in one cell.
    overlap           TEXT,
    branch            TEXT,
    pr_number         INTEGER,
    assigned_at       TEXT,
    state_changed_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_by_shell_id INTEGER REFERENCES shells(shell_id),
    UNIQUE (sprint_doc_id, seq)
);

-- The reconciler's per-tick read is "the non-terminal units of an ACTIVE
-- sprint" — its trigger condition, and the one query it runs every 60s.
CREATE INDEX IF NOT EXISTS idx_sprint_units_live
    ON sprint_units(sprint_doc_id, state);

COMMIT;
