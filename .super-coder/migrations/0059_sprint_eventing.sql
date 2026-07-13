-- 0059 — Sprint eventing: message kinds + the PR watch registry.
--
-- The event-driven sprint loop (spec: specs_sc/sprint-eventing.md) rides the
-- shell_messages bus: every instruction and result is a durable, queryable
-- row. Two additive changes carry it:
--
--   shell_messages.kind — typed traffic so the trail is filterable:
--     'shell'    — ordinary shell-to-shell mail (default; every existing row
--                  and writer stays valid)
--     'task'     — planner → worker instruction
--     'result'   — worker → planner completion report
--     'pr_event' — GitHub watcher daemon → owning shell transition
--   Migration-only ADD COLUMN (0047 precedent: SQLite has no ADD COLUMN IF
--   NOT EXISTS and rebuild applies migrations after schema.sql, so inlining
--   the column into the baseline CREATE would double-define it). schema.sql
--   carries a pointer comment in the shell_messages block.
--
--   watched_prs — the subscription registry and the daemon's diff state in
--   one. `./sc watch pr` (or the sprint skill, at PR open) registers a watch;
--   the daemon polls GitHub on one batched query, diffs each PR against
--   last_seen, and INSERTs a pr_event row to the watch's shell on every
--   transition. On merge/close it emits the final event and sets closed_at —
--   the watch retires itself; no leaked watchers to tear down at sprint
--   close. Convergent CREATE IF NOT EXISTS (0004 precedent — the table also
--   lives in schema.sql, so fresh builds and existing forks converge).
--
--   last_seen is a JSON fingerprint (head SHA, check-rollup state, review
--   count, PR state) owned exclusively by the daemon. NULL = never polled;
--   the first poll baselines it (emitting only already-terminal states).
--
-- The partial index is the daemon's hot path: "every live watch", each poll.

BEGIN;

ALTER TABLE shell_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'shell'
  CHECK (kind IN ('shell','task','result','pr_event'));

CREATE TABLE IF NOT EXISTS watched_prs (
    watch_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    repo           TEXT    NOT NULL,          -- owner/name
    pr_number      INTEGER NOT NULL,
    shell_id       INTEGER NOT NULL REFERENCES shells(shell_id),
    last_seen      TEXT,                      -- JSON: checks/review/state fingerprint
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at      TEXT,                      -- set on merge/close; NULL = live
    UNIQUE (repo, pr_number, shell_id)
);

CREATE INDEX IF NOT EXISTS idx_watched_prs_live
    ON watched_prs(closed_at) WHERE closed_at IS NULL;

COMMIT;
