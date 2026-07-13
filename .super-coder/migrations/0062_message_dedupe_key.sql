-- 0062 — idempotent message send: dedupe_key on shell_messages (#333).
--
-- Under multi-shell sprint load (#331 contention), a client timeout on
-- POST /_sc/mem/messages sometimes fired AFTER the server-side write —
-- the sender couldn't tell delivered from lost, and blind resends
-- duplicated messages fleet-wide. The client now stamps every send
-- invocation with a dedupe_key (uuid, generated per command run, reused
-- across its retries); the server returns the original row for a repeat
-- key instead of inserting a twin.
--
-- Migration-only ADD COLUMN (0047/0059 precedent: SQLite has no ADD COLUMN
-- IF NOT EXISTS and rebuild applies migrations after schema.sql, so
-- inlining the column into the baseline CREATE would double-define it).
-- schema.sql carries a pointer comment in the shell_messages block.
--
-- The unique index backstops the server's check-then-insert against a
-- concurrent retry; partial (NOT NULL only) so keyless writers — the
-- watcher daemon's pr_event INSERTs, older clients — are untouched.

BEGIN;

ALTER TABLE shell_messages ADD COLUMN dedupe_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_shell_messages_dedupe
    ON shell_messages(from_shell_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL;

COMMIT;
