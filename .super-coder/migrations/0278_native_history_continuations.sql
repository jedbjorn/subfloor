-- 0278 — native history continuations.
-- Links preserve predecessor records. New boot, native generation and current
-- canonical workspace belong to the continuation, not the old Sprint/chat.

BEGIN;

CREATE TABLE IF NOT EXISTS conversation_native_history (
 conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),
 generation_id TEXT NOT NULL UNIQUE,
 source_conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 source_generation_id TEXT NOT NULL REFERENCES conversation_runtime_generations(generation_id),
 owner_user_id INTEGER NOT NULL REFERENCES users(user_id),
 shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
 request_key TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 source_digest TEXT NOT NULL,
 fingerprint_key TEXT NOT NULL,
 proof_digest TEXT NOT NULL,
 workspace_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL DEFAULT (datetime('now')),
 UNIQUE(owner_user_id,request_key),
 CHECK(conversation_id<>source_conversation_id),
 CHECK(generation_id<>source_generation_id)
);
CREATE TRIGGER IF NOT EXISTS native_history_identity_immutable
BEFORE UPDATE ON conversation_native_history
WHEN NEW.conversation_id<>OLD.conversation_id OR NEW.generation_id<>OLD.generation_id
 OR NEW.source_conversation_id<>OLD.source_conversation_id OR NEW.source_generation_id<>OLD.source_generation_id
 OR NEW.owner_user_id<>OLD.owner_user_id OR NEW.shell_id<>OLD.shell_id
 OR NEW.request_key<>OLD.request_key OR NEW.request_hash<>OLD.request_hash
 OR NEW.source_digest<>OLD.source_digest OR NEW.fingerprint_key<>OLD.fingerprint_key
 OR NEW.proof_digest<>OLD.proof_digest OR NEW.created_at<>OLD.created_at
BEGIN
 SELECT RAISE(ABORT,'native history association is immutable');
END;

COMMIT;
