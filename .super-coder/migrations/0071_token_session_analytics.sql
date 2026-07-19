-- 0071 — token & session analytics: session lifecycle + per-session token usage (#407, feature 17, doc 11).
--
-- super-coder launches external harness CLIs and captured zero telemetry:
-- no timestamps, no model, no token counts. Providers are opaque about
-- allotments, but every harness already writes usage data to disk — this
-- migration gives it somewhere to land.
--
-- Two parts:
--   1. Session lifecycle columns on shell_memory_archives — written by
--      run.py open_session (started_at/harness/provider/model/sprint_ref);
--      ended_at is backfilled by the sweep (run.py execs the harness, so no
--      code runs at exit). Historical rows stay NULL.
--   2. session_token_usage — one row per (harness session × model), swept
--      from each harness's on-disk data by scripts/token_parsers/*.
--      UNIQUE(harness, harness_session_ref, model) is the idempotency key:
--      parsers upsert, re-sweeps never double-count. archive_id NULL means
--      "unattributed" (real spend on this repo, launched outside run.py or
--      ambiguous) — deliberately NOT a status value.
--
-- Token classes normalize to dos-arch's four (fresh input / cache read /
-- cache write / output) + nullable reasoning. NULL = "not exposed by this
-- harness", 0 = "measured zero" — parsers never write zeros as if measured.

BEGIN;

ALTER TABLE shell_memory_archives ADD COLUMN started_at TEXT;   -- ISO UTC
ALTER TABLE shell_memory_archives ADD COLUMN ended_at   TEXT;   -- ISO UTC, sweep-backfilled
ALTER TABLE shell_memory_archives ADD COLUMN harness    TEXT;   -- claude/opencode/codex/vibe/kimi
ALTER TABLE shell_memory_archives ADD COLUMN provider   TEXT;   -- anthropic/openai/mistral/…
ALTER TABLE shell_memory_archives ADD COLUMN model      TEXT;   -- resolved model id (NULL = harness default)
ALTER TABLE shell_memory_archives ADD COLUMN sprint_ref TEXT;   -- SC_SPRINT_REF (tracker document_id)

CREATE TABLE IF NOT EXISTS session_token_usage (
    usage_id            INTEGER PRIMARY KEY,
    archive_id          INTEGER REFERENCES shell_memory_archives(archive_id),  -- NULL = unattributed
    shell_id            INTEGER,                -- denormalized for filtering
    harness             TEXT    NOT NULL,       -- claude/opencode/codex/vibe/kimi
    harness_session_ref TEXT    NOT NULL,       -- transcript path / session id / rollout file / session dir
    provider            TEXT,
    model               TEXT,
    title               TEXT,                   -- native session title, or first-prompt derived
    started_at          TEXT,                   -- ISO UTC, from harness data
    ended_at            TEXT,                   -- ISO UTC, from harness data
    input_tokens        INTEGER,                -- fresh (uncached) input
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_write_tokens  INTEGER,
    reasoning_tokens    INTEGER,                -- opencode + codex expose it
    status              TEXT    NOT NULL DEFAULT 'ok'
                        CHECK (status IN ('ok', 'partial', 'no_usage')),
    parser_version      TEXT,                   -- per-parser format pin
    captured_at         TEXT,                   -- sweep time, ISO UTC
    UNIQUE (harness, harness_session_ref, model)
);

CREATE INDEX IF NOT EXISTS idx_stu_archive ON session_token_usage(archive_id);
CREATE INDEX IF NOT EXISTS idx_stu_started ON session_token_usage(started_at);

COMMIT;
