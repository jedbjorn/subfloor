CREATE TABLE chat_schema_migrations (
    migration_id TEXT PRIMARY KEY
        CHECK (migration_id GLOB '[0-9][0-9][0-9][0-9]_*'),
    checksum_sha256 TEXT NOT NULL
        CHECK (length(checksum_sha256) = 64),
    applied_at TEXT NOT NULL
);

CREATE TABLE chat_sessions (
    session_id TEXT PRIMARY KEY,
    shell_id INTEGER NOT NULL CHECK (shell_id > 0),
    harness TEXT NOT NULL CHECK (harness IN ('claude', 'codex', 'kimi')),
    cwd TEXT NOT NULL,
    host_mode TEXT NOT NULL DEFAULT 'idle_chat'
        CHECK (host_mode IN ('idle_chat', 'running_headless', 'hosted_terminal')),
    provider_session_id TEXT,
    transcript_locator TEXT,
    wake_pending INTEGER NOT NULL DEFAULT 0 CHECK (wake_pending IN (0, 1)),
    toggle_pending INTEGER NOT NULL DEFAULT 0 CHECK (toggle_pending IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX chat_sessions_provider_session
ON chat_sessions(harness, provider_session_id)
WHERE provider_session_id IS NOT NULL;

CREATE TABLE chat_turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES chat_sessions(session_id),
    source TEXT NOT NULL CHECK (source IN ('composer', 'wake', 'retry')),
    state TEXT NOT NULL
        CHECK (state IN ('running', 'completed', 'failed', 'aborted')),
    attempt_of TEXT REFERENCES chat_turns(turn_id),
    pre_turn_anchor_json TEXT NOT NULL CHECK (json_valid(pre_turn_anchor_json)),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    exit_code INTEGER,
    failure_code TEXT,
    failure_diagnostic TEXT,
    retry_safe INTEGER CHECK (retry_safe IS NULL OR retry_safe IN (0, 1)),
    CHECK (
        (state = 'running' AND ended_at IS NULL)
        OR (state <> 'running' AND ended_at IS NOT NULL)
    )
);

CREATE INDEX chat_turns_session_started
ON chat_turns(session_id, started_at, turn_id);

CREATE TABLE chat_events (
    event_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES chat_sessions(session_id),
    turn_id TEXT NOT NULL REFERENCES chat_turns(turn_id),
    event_seq INTEGER NOT NULL CHECK (event_seq > 0),
    month_key TEXT NOT NULL
        CHECK (
            length(month_key) = 7
            AND substr(month_key, 5, 1) = '-'
            AND substr(month_key, 1, 4) GLOB '[0-9][0-9][0-9][0-9]'
            AND substr(month_key, 6, 2) BETWEEN '01' AND '12'
        ),
    kind TEXT NOT NULL CHECK (
        kind IN (
            'user_message', 'turn_started', 'message_completed',
            'tool_call', 'tool_result', 'usage',
            'turn_completed', 'turn_failed', 'host_changed'
        )
    ),
    role TEXT CHECK (role IS NULL OR role IN ('user', 'assistant', 'tool', 'system')),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    transcript_anchor_json TEXT
        CHECK (transcript_anchor_json IS NULL OR json_valid(transcript_anchor_json)),
    created_at TEXT NOT NULL,
    UNIQUE (session_id, event_seq),
    CHECK (month_key = substr(created_at, 1, 7))
);

CREATE INDEX chat_events_month_session_seq
ON chat_events(month_key, session_id, event_seq);

CREATE INDEX chat_events_replay
ON chat_events(session_id, event_seq DESC)
WHERE kind IN ('user_message', 'turn_started', 'message_completed',
               'turn_completed', 'turn_failed', 'host_changed');

CREATE TRIGGER chat_events_append_only_update
BEFORE UPDATE ON chat_events
BEGIN
    SELECT RAISE(ABORT, 'chat_events is append-only');
END;

CREATE TRIGGER chat_events_append_only_delete
BEFORE DELETE ON chat_events
BEGIN
    SELECT RAISE(ABORT, 'chat_events is append-only');
END;

CREATE TABLE chat_health (
    harness TEXT NOT NULL,
    counter_key TEXT NOT NULL CHECK (
        length(counter_key) BETWEEN 1 AND 96
    ),
    month_key TEXT NOT NULL
        CHECK (
            length(month_key) = 7
            AND substr(month_key, 5, 1) = '-'
            AND substr(month_key, 1, 4) GLOB '[0-9][0-9][0-9][0-9]'
            AND substr(month_key, 6, 2) BETWEEN '01' AND '12'
        ),
    count INTEGER NOT NULL CHECK (count BETWEEN 1 AND 2147483647),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (harness, counter_key, month_key)
);

CREATE TABLE chat_transcript_cursors (
    cursor_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(session_id),
    turn_id TEXT NOT NULL UNIQUE REFERENCES chat_turns(turn_id),
    transcript_path TEXT,
    source_offset INTEGER CHECK (source_offset IS NULL OR source_offset >= 0),
    next_offset INTEGER CHECK (next_offset IS NULL OR next_offset >= 0),
    line_sha256 TEXT CHECK (line_sha256 IS NULL OR length(line_sha256) = 64),
    file_size INTEGER CHECK (file_size IS NULL OR file_size >= 0),
    resolution_status TEXT NOT NULL
        CHECK (resolution_status IN ('ready', 'missing', 'exact', 'relocated', 'gap')),
    resolved_offset INTEGER CHECK (resolved_offset IS NULL OR resolved_offset >= 0),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX chat_transcript_cursors_session
ON chat_transcript_cursors(session_id, cursor_id);
