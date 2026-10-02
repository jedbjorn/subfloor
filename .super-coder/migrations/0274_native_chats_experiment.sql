-- 0274 — explicit native Chats mode and fingerprint-bound evidence cache.
-- Explicit mode remains off for all existing and ordinary new Chats.
BEGIN;
ALTER TABLE conversations ADD COLUMN runtime_mode TEXT NOT NULL DEFAULT 'ephemeral'
 CHECK(runtime_mode IN ('ephemeral','native_experiment'));
CREATE TABLE IF NOT EXISTS conversation_runtime_capability_cache (
 cache_key TEXT PRIMARY KEY,
 evidence_json TEXT NOT NULL,
 updated_at REAL NOT NULL
);
COMMIT;
