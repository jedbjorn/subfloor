-- 0285 — run agents and time limit.
-- Intent: the runs ledger gains the `agent` kind and a recorded time limit for
-- every kind, and agent-only facts get their own table so the `runs` row keeps
-- the shape existing readers expect (Feature #96, spec #273 "Job kinds").
-- The kind CHECK cannot be altered in place, so `runs` is rebuilt in the manner
-- of 0275: rows, run ids and the id high-water mark are carried unchanged, with
-- `timeout_seconds` empty on every existing row.
-- migrate: foreign-keys-off
PRAGMA foreign_keys=OFF;
PRAGMA legacy_alter_table=ON;
BEGIN;
ALTER TABLE runs ADD COLUMN timeout_seconds INTEGER
    CHECK (timeout_seconds IS NULL OR timeout_seconds > 0);
CREATE TABLE _runs_agent_kind (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
    registration_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('job','devkit','probe','native','agent')),
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
    evidence_pruned INTEGER NOT NULL DEFAULT 0
      CHECK (evidence_pruned IN (0,1)),
    foreground INTEGER NOT NULL DEFAULT 0
      CHECK (foreground IN (0,1) AND (foreground = 0 OR kind = 'devkit')),
    timeout_seconds INTEGER
      CHECK (timeout_seconds IS NULL OR timeout_seconds > 0),
    UNIQUE(owner_shell_id,registration_key)
);
INSERT INTO _runs_agent_kind (
    run_id,owner_shell_id,registration_key,kind,label,argv,cwd,"commit",state,
    exit_code,created_at,started_at,finished_at,pid,start_ticks,supervisor_pid,
    supervisor_start_ticks,boot_id,evidence_path,receipt_json,terminal_json,
    wake_state,wake_id,message_id,inbox_message_id,turn_run_id,last_error,
    attached_pr,attached_work_unit,evidence_pruned,foreground,timeout_seconds)
SELECT
    run_id,owner_shell_id,registration_key,kind,label,argv,cwd,"commit",state,
    exit_code,created_at,started_at,finished_at,pid,start_ticks,supervisor_pid,
    supervisor_start_ticks,boot_id,evidence_path,receipt_json,terminal_json,
    wake_state,wake_id,message_id,inbox_message_id,turn_run_id,last_error,
    attached_pr,attached_work_unit,evidence_pruned,foreground,timeout_seconds
FROM runs;
-- Registration allocates above the ledger high-water mark; carry it exactly,
-- including when the newest run ids were never inserted through this table.
DELETE FROM sqlite_sequence WHERE name='_runs_agent_kind';
INSERT INTO sqlite_sequence(name,seq)
    SELECT '_runs_agent_kind',seq FROM sqlite_sequence WHERE name='runs';
DROP TABLE runs;
ALTER TABLE _runs_agent_kind RENAME TO runs;
CREATE INDEX idx_runs_owner ON runs(owner_shell_id,run_id);
CREATE INDEX idx_runs_pending ON runs(state,wake_state);
CREATE INDEX idx_runs_message ON runs(message_id) WHERE message_id IS NOT NULL;
CREATE INDEX idx_runs_attached_pr ON runs(attached_pr) WHERE attached_pr IS NOT NULL;
CREATE INDEX idx_runs_attached_unit ON runs(attached_work_unit) WHERE attached_work_unit IS NOT NULL;
CREATE INDEX idx_runs_live_agents ON runs(kind,state) WHERE kind='agent';
-- A time limit is fixed at registration, like the rest of the run identity.
CREATE TRIGGER runs_identity_immutable
BEFORE UPDATE OF owner_shell_id,registration_key,kind,argv,cwd,timeout_seconds ON runs
BEGIN SELECT RAISE(ABORT,'run identity is immutable'); END;
CREATE TRIGGER runs_path_immutable
BEFORE UPDATE OF evidence_path ON runs
BEGIN SELECT RAISE(ABORT,'run evidence path is immutable'); END;
CREATE TRIGGER runs_terminal_immutable
BEFORE UPDATE OF state,exit_code,finished_at,terminal_json ON runs
WHEN OLD.terminal_json IS NOT NULL
BEGIN SELECT RAISE(ABORT,'run terminal result is immutable'); END;
CREATE TABLE IF NOT EXISTS run_agents (
    run_id INTEGER PRIMARY KEY REFERENCES runs(run_id),
    harness TEXT NOT NULL CHECK (trim(harness)<>''),
    requested_model TEXT NOT NULL CHECK (trim(requested_model)<>''),
    provider_model TEXT,
    requested_effort TEXT,
    effective_effort TEXT,
    session_ref TEXT,
    worktree TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    branch TEXT,
    continues_run_id INTEGER REFERENCES runs(run_id),
    result_bytes INTEGER CHECK (result_bytes IS NULL OR result_bytes >= 0),
    usage_json TEXT CHECK (usage_json IS NULL OR json_valid(usage_json)),
    adapter_version TEXT,
    worktree_removed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_agents_continues
    ON run_agents(continues_run_id) WHERE continues_run_id IS NOT NULL;
-- What a subagent was asked for is fixed at registration; what its run
-- reports (session, served model, result size, usage, removal) arrives later.
CREATE TRIGGER IF NOT EXISTS run_agents_request_immutable
BEFORE UPDATE OF run_id,harness,requested_model,requested_effort,worktree,
                 base_commit,branch,continues_run_id ON run_agents
BEGIN SELECT RAISE(ABORT,'run agent request is immutable'); END;
COMMIT;
PRAGMA legacy_alter_table=OFF;
PRAGMA foreign_keys=ON;
