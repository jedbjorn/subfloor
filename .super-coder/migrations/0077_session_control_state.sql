-- 0077 — provider-neutral planner session-control state.
--
-- A managed sprint planner keeps one engine archive bound to one native
-- harness conversation. shell_session_bindings records that address plus the
-- locally validated owner/control state; session_wake_jobs is the durable
-- claim/audit ledger reconstructed from unread shell_messages.
--
-- Message read_at remains the delivery acknowledgement. A wake row reaching
-- done never marks its trigger message read, and a missing wake row can always
-- be recreated with INSERT OR IGNORE from the unread-message source of truth.

BEGIN;

CREATE TABLE IF NOT EXISTS shell_session_bindings (
  binding_id          INTEGER PRIMARY KEY,
  archive_id          INTEGER NOT NULL UNIQUE
                      REFERENCES shell_memory_archives(archive_id),
  shell_id            INTEGER NOT NULL REFERENCES shells(shell_id),
  harness             TEXT NOT NULL,
  native_session_id   TEXT,
  control_endpoint    TEXT,
  control_capabilities TEXT NOT NULL DEFAULT '{}',
  cli_version         TEXT,
  state               TEXT NOT NULL CHECK (state IN
                      ('starting','foreground','idle','dispatching',
                       'dormant','released','error')),
  managed             INTEGER NOT NULL DEFAULT 0 CHECK (managed IN (0,1)),
  lease_pid           INTEGER,
  lease_start_ticks   INTEGER,
  active_channel_pid  INTEGER,
  active_channel_start_ticks INTEGER,
  active_channel_heartbeat_at TEXT,
  lease_generation    INTEGER NOT NULL DEFAULT 0,
  last_error          TEXT,
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (harness, native_session_id)
);

CREATE TABLE IF NOT EXISTS session_wake_jobs (
  wake_id             INTEGER PRIMARY KEY,
  binding_id          INTEGER NOT NULL
                      REFERENCES shell_session_bindings(binding_id),
  trigger_message_id  INTEGER NOT NULL REFERENCES shell_messages(message_id),
  state               TEXT NOT NULL DEFAULT 'queued'
                      CHECK (state IN ('queued','running','done','failed','cancelled')),
  attempt_count       INTEGER NOT NULL DEFAULT 0,
  available_at        TEXT NOT NULL DEFAULT (datetime('now')),
  started_at          TEXT,
  finished_at         TEXT,
  last_error          TEXT,
  UNIQUE (binding_id, trigger_message_id)
);

-- One shell may retain historical/released bindings, but autonomous delivery
-- has exactly one current conversation target.
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_bindings_managed_shell
  ON shell_session_bindings(shell_id) WHERE managed = 1;

CREATE INDEX IF NOT EXISTS idx_session_bindings_managed_state
  ON shell_session_bindings(state) WHERE managed = 1;

CREATE INDEX IF NOT EXISTS idx_session_wake_jobs_ready
  ON session_wake_jobs(binding_id, state, available_at);

CREATE INDEX IF NOT EXISTS idx_session_wake_jobs_message
  ON session_wake_jobs(trigger_message_id);

COMMIT;
