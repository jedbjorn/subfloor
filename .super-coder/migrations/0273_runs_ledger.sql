-- 0273 — engine-owned runs and non-Sprint wake delivery receipts.
BEGIN;
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
    registration_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('job','devkit','probe','native')),
    label TEXT NOT NULL,
    argv TEXT NOT NULL CHECK(json_valid(argv)),
    cwd TEXT NOT NULL,
    "commit" TEXT,
    state TEXT NOT NULL DEFAULT 'registered'
      CHECK(state IN ('registered','running','done','failed','timeout','killed','lost')),
    exit_code INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    pid INTEGER,
    start_ticks INTEGER,
    supervisor_pid INTEGER,
    supervisor_start_ticks INTEGER,
    boot_id TEXT,
    evidence_path TEXT NOT NULL,
    receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
    terminal_json TEXT CHECK(terminal_json IS NULL OR json_valid(terminal_json)),
    wake_state TEXT NOT NULL DEFAULT 'none'
      CHECK(wake_state IN ('none','pending','enqueued','consumed','blocked')),
    wake_id INTEGER REFERENCES sprint_wake_outbox(wake_id),
    message_id INTEGER REFERENCES wake_message(message_id),
    inbox_message_id INTEGER REFERENCES shell_messages(message_id),
    turn_run_id INTEGER REFERENCES conversation_runs(run_id),
    last_error TEXT,
    attached_pr INTEGER,
    attached_work_unit INTEGER,
    UNIQUE(owner_shell_id,registration_key)
);
CREATE INDEX IF NOT EXISTS idx_runs_owner ON runs(owner_shell_id,run_id);
CREATE INDEX IF NOT EXISTS idx_runs_pending ON runs(state,wake_state);
CREATE TRIGGER IF NOT EXISTS runs_identity_immutable
BEFORE UPDATE OF owner_shell_id,registration_key,kind,argv,cwd ON runs
BEGIN SELECT RAISE(ABORT,'run identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS runs_path_immutable
BEFORE UPDATE OF evidence_path ON runs WHEN OLD.evidence_path <> ''
BEGIN SELECT RAISE(ABORT,'run evidence path is immutable'); END;
CREATE TRIGGER IF NOT EXISTS runs_terminal_immutable
BEFORE UPDATE OF state,exit_code,finished_at,terminal_json ON runs
WHEN OLD.terminal_json IS NOT NULL
BEGIN SELECT RAISE(ABORT,'run terminal result is immutable'); END;
CREATE TABLE IF NOT EXISTS engine_wake_receipts (
    message_id INTEGER PRIMARY KEY REFERENCES wake_message(message_id),
    conversation_message_id INTEGER REFERENCES conversation_messages(message_id),
    busy_attempts INTEGER NOT NULL DEFAULT 0,
    last_run_id INTEGER REFERENCES conversation_runs(run_id),
    retry_at TEXT,
    blocked_reason TEXT
);
CREATE TABLE IF NOT EXISTS engine_wake_failures (
    wake_id INTEGER PRIMARY KEY REFERENCES sprint_wake_outbox(wake_id),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    last_error TEXT
);
COMMIT;
