-- Durable Sprint lifecycle associations, sharing the runtime command journal.
BEGIN;
CREATE TABLE IF NOT EXISTS sprint_native_lifecycle_intents (
 intent_id TEXT PRIMARY KEY,
 sprint_id INTEGER NOT NULL REFERENCES sprints(sprint_id),
 participant_id INTEGER NOT NULL REFERENCES sprint_participants(participant_id),
 conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 generation_id TEXT,
 owner_user_id INTEGER NOT NULL REFERENCES users(user_id),
 shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
 lifecycle TEXT NOT NULL CHECK(lifecycle IN ('paused','aborted','completed')),
 lifecycle_version INTEGER NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('stop_reply','close')),
 primary_json TEXT NOT NULL DEFAULT 'null',
 command_id TEXT,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','accepted','written','processed','terminal','unknown','not_written','unsupported','rejected')),
 detail TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL,
 FOREIGN KEY(generation_id,command_id) REFERENCES conversation_runtime_commands(generation_id,command_id)
);
CREATE INDEX IF NOT EXISTS idx_native_sprint_intent_generation
 ON sprint_native_lifecycle_intents(generation_id,state);
CREATE TRIGGER IF NOT EXISTS native_sprint_intent_identity_immutable
 BEFORE UPDATE OF intent_id,sprint_id,participant_id,conversation_id,generation_id,
 owner_user_id,shell_id,lifecycle,lifecycle_version,action,primary_json,created_at
 ON sprint_native_lifecycle_intents
 BEGIN SELECT RAISE(ABORT,'native Sprint lifecycle identity is immutable'); END;
COMMIT;
