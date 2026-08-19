-- Feature #24 / spec #163 — one immutable boot snapshot per conversation.
--
-- Every durable browser conversation binds exactly one boot-document snapshot
-- before its first native harness session starts. The row stores the canonical
-- UTF-8 content plus its SHA-256 digest and byte length so later turns and
-- closed-chat reopens restore exact bytes without recomposing live shell
-- state. Conversations created before this migration stay unbound; the broker
-- binds them once as 'legacy_first_resume' on their first post-upgrade
-- dispatch.
--
-- The snapshot is immutable: no UPDATE and no direct DELETE. The row leaves
-- only through the owning conversation's retention/delete cascade.

BEGIN;

CREATE TABLE conversation_boot_snapshots (
    conversation_id     TEXT PRIMARY KEY
                        REFERENCES conversations(conversation_id)
                        ON DELETE CASCADE,
    content             TEXT NOT NULL
                        CHECK (length(CAST(content AS BLOB))
                               BETWEEN 1 AND 1048576),
    content_sha256      TEXT NOT NULL
                        CHECK (length(content_sha256)=64
                          AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    content_bytes       INTEGER NOT NULL
                        CHECK (content_bytes > 0
                          AND content_bytes <= 1048576
                          AND content_bytes = length(CAST(content AS BLOB))),
    format_version      INTEGER NOT NULL CHECK (format_version > 0),
    binding_origin      TEXT NOT NULL
                        CHECK (binding_origin IN
                            ('new_conversation','legacy_first_resume')),
    bound_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A bound snapshot is immutable: the committed bytes, digest, origin, and
-- binding time never change. SQLite fires BEFORE DELETE triggers even for
-- cascade deletes, so deletion is guarded only by conversation ownership and
-- rides the ON DELETE CASCADE lifecycle.
CREATE TRIGGER trg_conversation_boot_snapshots_immutable
BEFORE UPDATE ON conversation_boot_snapshots
BEGIN
  SELECT RAISE(ABORT, 'conversation boot snapshot is immutable');
END;

COMMIT;
