-- 0273 — opt-in native runtime projections. Production dispatch is unchanged.
-- The controller journal bridges transport/API downtime; these tables remain
-- canonical generation/command/work projections and consumer fencing authority.
BEGIN;
CREATE TABLE IF NOT EXISTS conversation_runtime_generations (
 generation_id TEXT PRIMARY KEY,
 conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
 owner_user_id INTEGER NOT NULL REFERENCES users(user_id),
 harness TEXT NOT NULL CHECK(harness IN ('codex','claude')),
 binding_json TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'reserved',
 cleanup_json TEXT NOT NULL DEFAULT '{}',
 last_sequence INTEGER NOT NULL DEFAULT 0,
 next_command_sequence INTEGER NOT NULL DEFAULT 1,
 consumer_id TEXT,
 consumer_fence INTEGER NOT NULL DEFAULT 0,
 consumer_expires REAL NOT NULL DEFAULT 0,
 close_intent INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_one_open_generation
 ON conversation_runtime_generations(conversation_id)
 WHERE state NOT IN ('closed','lost');
CREATE TABLE IF NOT EXISTS conversation_runtime_commands (
 generation_id TEXT NOT NULL REFERENCES conversation_runtime_generations(generation_id),
 command_id TEXT NOT NULL,
 command_sequence INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('submit','control')),
 payload_digest TEXT NOT NULL,
 intent_json TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'accepted',
 receipt_json TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(generation_id,command_id),
 UNIQUE(generation_id,command_sequence)
);
CREATE TABLE IF NOT EXISTS conversation_runtime_work (
 generation_id TEXT NOT NULL REFERENCES conversation_runtime_generations(generation_id),
 work_key TEXT NOT NULL,
 projection_json TEXT NOT NULL,
 last_sequence INTEGER NOT NULL,
 PRIMARY KEY(generation_id,work_key)
);
CREATE TABLE IF NOT EXISTS conversation_runtime_events (
 generation_id TEXT NOT NULL REFERENCES conversation_runtime_generations(generation_id),
 sequence INTEGER NOT NULL,
 event_json TEXT NOT NULL,
 PRIMARY KEY(generation_id,sequence)
);
COMMIT;
