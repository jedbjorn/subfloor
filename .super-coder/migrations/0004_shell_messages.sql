-- 0004 — Shell Inbox: inter-shell messaging (shell_messages).
--
-- One additive, convergent change — safe to apply both on an existing fork (the
-- table is absent → it is created) AND on a fresh rebuild (schema.sql already
-- has it → CREATE … IF NOT EXISTS converges to the same shape). Matches the
-- 0002/0003 precedent.
--
-- A shell writes a markdown message to another shell; the recipient discovers it
-- on its next boot via the `## STATUS` `Inbox:` count + the `messaging` skill's
-- `check` verb, and marks it read by UPDATE-ing `read_at`. No API layer in v1 —
-- the `messaging` skill runs this SQL directly against shell_db.db (single-user,
-- localhost; every shell owned by the same operator).
--
--   message_id   — AUTOINCREMENT: a message is never reaped or merged, unlike a
--                  shell, so a monotonic id is the right shape here.
--   from/to      — REFERENCES shells(shell_id); the only enforcement v1 has is at
--                  the DB layer (FK + the NOT NULL + the body CHECK). The skill
--                  always sets from_shell_id=<self>; there is no impersonation
--                  threat in a single-operator fork.
--   read_at      — NULL = unread. The soft-delete of the inbox row; no hard
--                  delete in v1 (rows kept for audit).
--
-- The composite index (to_shell_id, read_at) is the hot path: "this shell's
-- unread inbox" — both columns appear in the check query's WHERE.

BEGIN;

CREATE TABLE IF NOT EXISTS shell_messages (
    message_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    from_shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
    to_shell_id   INTEGER NOT NULL REFERENCES shells(shell_id),
    body          TEXT    NOT NULL CHECK (length(body) > 0),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    read_at       TEXT                          -- NULL = unread
);

CREATE INDEX IF NOT EXISTS idx_shell_messages_to_unread
    ON shell_messages(to_shell_id, read_at);

COMMIT;
