-- 0289 — chat compaction settings (Feature #95, spec #275).
-- Intent: one operator-saved row per harness (claude, codex, kimi, opencode)
-- holding that harness's Auto compaction (on/off and trigger in tokens) and
-- Idle compaction (wait in minutes, NULL = Off, and idle size). A row is
-- written whole on the first save, so a saved row always wins over the
-- SC_CHAT_COMPACT_WINDOW_* environment and the engine defaults. No row means
-- "not saved": the engine resolves environment, then default. Install-wide,
-- operator-tuned, kept across rebuilds through PER_INSTANCE_TABLES.
-- Additive; idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS chat_compaction_settings (
    harness             TEXT PRIMARY KEY
                        CHECK (harness IN ('claude','codex','kimi','opencode')),
    auto_enabled        INTEGER NOT NULL CHECK (auto_enabled IN (0,1)),
    trigger_tokens      INTEGER NOT NULL CHECK (trigger_tokens > 0),
    idle_after_minutes  INTEGER
                        CHECK (
                          idle_after_minutes IS NULL
                          OR idle_after_minutes BETWEEN 1 AND 1440
                        ),
    idle_min_tokens     INTEGER NOT NULL CHECK (idle_min_tokens > 0),
    updated_by_user_id  INTEGER REFERENCES users(user_id),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

COMMIT;
