-- 0273 — engine-owned runs and non-Sprint wake delivery receipts.
-- migrate: foreign-keys-off
-- Rebuild the wake kind constraint, retaining message ids, references and guards.
PRAGMA foreign_keys=OFF;
PRAGMA legacy_alter_table=ON;
BEGIN;
CREATE TABLE _run_wake_message (
    message_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id               INTEGER REFERENCES sprints(sprint_id),
    sender_shell_id         INTEGER REFERENCES shells(shell_id),
    receiver_shell_id       INTEGER NOT NULL REFERENCES shells(shell_id),
    from_participant_id     INTEGER REFERENCES sprint_participants(participant_id),
    to_participant_id       INTEGER REFERENCES sprint_participants(participant_id),
    work_unit_id            INTEGER REFERENCES sprint_work_units(work_unit_id),
    message_kind            TEXT NOT NULL
                            CHECK (message_kind IN
                              ('work_assignment','review_request','notification',
                               'nudge','escalation','system','result')),
    body                    TEXT NOT NULL CHECK (length(body)>0),
    declared_type           TEXT NOT NULL
                            CHECK (declared_type IN
                              ('force-new','new','re-enter')),
    actionable              INTEGER NOT NULL DEFAULT 0
                            CHECK (actionable IN (0,1)),
    disposition             TEXT
                            CHECK (disposition IN
                              ('pending','accepted','declined')),
    read_at                 TEXT,
    delivered_at            TEXT,
    decline_reason          TEXT,
    idempotency_key         TEXT NOT NULL UNIQUE
                            CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')), intent TEXT NOT NULL DEFAULT 'information'
  CHECK (intent IN ('information','handoff','question','blocker','decision')), requires_reply INTEGER NOT NULL DEFAULT 0
  CHECK (
    requires_reply IN (0,1)
    AND (
      requires_reply=0
      OR intent IN ('question','blocker','decision')
    )
  ), reply_to_message_id INTEGER REFERENCES wake_message(message_id),
    CHECK (
      (actionable=1 AND disposition IS NOT NULL)
      OR
      (actionable=0 AND disposition IS NULL)
    ),
    CHECK (
      disposition<>'declined' OR trim(COALESCE(decline_reason,''))<>''
    ),
    CHECK (
      (sprint_id IS NULL AND from_participant_id IS NULL
       AND to_participant_id IS NULL AND work_unit_id IS NULL)
      OR
      (sprint_id IS NOT NULL AND to_participant_id IS NOT NULL)
    ),
    UNIQUE (sprint_id, message_id),
    FOREIGN KEY (sprint_id, from_participant_id)
      REFERENCES sprint_participants(sprint_id, participant_id),
    FOREIGN KEY (sprint_id, to_participant_id)
      REFERENCES sprint_participants(sprint_id, participant_id),
    FOREIGN KEY (sprint_id, work_unit_id)
      REFERENCES sprint_work_units(sprint_id, work_unit_id)
);
INSERT INTO _run_wake_message ("message_id","sprint_id","sender_shell_id","receiver_shell_id","from_participant_id","to_participant_id","work_unit_id","message_kind","body","declared_type","actionable","disposition","read_at","delivered_at","decline_reason","idempotency_key","created_at","intent","requires_reply","reply_to_message_id") SELECT "message_id","sprint_id","sender_shell_id","receiver_shell_id","from_participant_id","to_participant_id","work_unit_id","message_kind","body","declared_type","actionable","disposition","read_at","delivered_at","decline_reason","idempotency_key","created_at","intent","requires_reply","reply_to_message_id" FROM wake_message;
DROP TABLE wake_message;
ALTER TABLE _run_wake_message RENAME TO wake_message;
CREATE INDEX idx_wake_message_inbox
    ON wake_message(receiver_shell_id, read_at, message_id);
CREATE INDEX idx_wake_message_delivery
    ON wake_message(receiver_shell_id, delivered_at, message_id);
CREATE TRIGGER trg_wake_message_acceptance_insert
BEFORE INSERT ON wake_message
WHEN NOT (
    (NEW.actionable=0 AND NEW.disposition IS NULL
     AND NEW.decline_reason IS NULL)
    OR
    (NEW.actionable=1 AND NEW.disposition='pending'
     AND NEW.read_at IS NULL AND NEW.decline_reason IS NULL)
    OR
    (NEW.actionable=1 AND NEW.disposition='accepted'
     AND NEW.read_at IS NOT NULL AND NEW.decline_reason IS NULL)
    OR
    (NEW.actionable=1 AND NEW.disposition='declined'
     AND NEW.read_at IS NOT NULL
     AND trim(COALESCE(NEW.decline_reason,''))<>'')
)
BEGIN
  SELECT RAISE(ABORT, 'invalid wake message acceptance state');
END;
CREATE TRIGGER trg_wake_message_acceptance_update
BEFORE UPDATE OF actionable, disposition, read_at, decline_reason
ON wake_message
WHEN NOT (
    (NEW.actionable=0 AND NEW.disposition IS NULL
     AND NEW.decline_reason IS NULL)
    OR
    (NEW.actionable=1 AND NEW.disposition='pending'
     AND NEW.read_at IS NULL AND NEW.decline_reason IS NULL)
    OR
    (NEW.actionable=1 AND NEW.disposition='accepted'
     AND NEW.read_at IS NOT NULL AND NEW.decline_reason IS NULL)
    OR
    (NEW.actionable=1 AND NEW.disposition='declined'
     AND NEW.read_at IS NOT NULL
     AND trim(COALESCE(NEW.decline_reason,''))<>'')
)
BEGIN
  SELECT RAISE(ABORT, 'invalid wake message acceptance state');
END;
CREATE INDEX idx_wake_message_replies
  ON wake_message(sprint_id, reply_to_message_id, message_id);
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
UPDATE shells SET system_prompt=replace(system_prompt, 'Use the engine''s `./sc job` tools to watch checks that may outlive the current tool call or session. Do not set up independent watchers.', 'Use `./sc job start --label gate -- <command and args>` for checks that may outlive the current tool call or session. After confirmed registration/start, end the turn and continue on the owner wake; inspect `./sc job status <id>` and `./sc job tail <id>`. A lost outcome is unknown, never a pass. Do not set up independent watchers.') WHERE flavor='dev';
COMMIT;
PRAGMA legacy_alter_table=OFF;
PRAGMA foreign_keys=ON;
