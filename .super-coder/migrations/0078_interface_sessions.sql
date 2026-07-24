-- 0078 — Interface session schema + state machines (sprint 25 seq 4, spec #20
-- task #80). The Interface tab's durable state: one API brokered interactive
-- chat generation per shell, writer leases, metadata-only input state, HTTP
-- idempotency keys, sprint planner bindings, wake items/batches, planner
-- action receipts, PR poll audit, and alerts — plus transition triggers for
-- every state machine (the DB backstop; interface_state.py mirrors the edge
-- maps for friendly app-level errors) and the uniqueness constraints the
-- spec's occupancy model requires (one non-ended session per shell, one live
-- generation, one current writer, one live batch, one unreleased binding per
-- planner and per sprint, unique (binding, message) wake work).
--
-- Crash-window contract (decision #22): interface_input_state.pending_seq is
-- the durable reservation for a human frame that was accepted but not yet
-- acknowledged; a broker restart with pending_seq set cannot distinguish
-- pre-write from post-write, so startup reconciliation parks composer AND
-- delivery as unknown, revokes the writer, and never replays (see
-- interface_reconcile.startup_reconcile + tests/test_interface_crash_window.py).
--
-- Snapshot stance: volatile runtime tables (interface_writer_leases,
-- pr_poll_runs) are deliberately absent from snapshot.py's
-- PER_INSTANCE_TABLES (the 0075 precedent). interface_input_state preserves
-- only terminal delivery-unknown metadata; durable audit tables are
-- snapshotted with row filters (live rows excluded — rebuild/update refuse
-- while any of them exist anyway) and volatile columns (tmux socket,
-- PIDs/start ticks, hook token hash) ride SENSITIVE_COLUMNS.

BEGIN;

-- ── Generations: the per-shell monotonic fence ──────────────────────────────
-- Every interactive chat is one generation; callbacks, leases, leases' input
-- sequences, and wake batches are all fenced by (shell_id, generation) so a
-- stale generation can never act. hook_token_hash + last_hook_seq are the
-- generation-scoped hook credential and replay fence (spec puts them on the
-- binding; they live here because ordinary chats authenticate hooks too, with
-- no binding). last_hook_seq is DURABLE — it is the crash-window evidence.

CREATE TABLE IF NOT EXISTS interface_generations (
    shell_id        INTEGER NOT NULL REFERENCES shells(shell_id),
    generation      INTEGER NOT NULL CHECK (generation > 0),
    hook_token_hash TEXT,                      -- volatile hash; snapshot-excluded
    last_hook_seq   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at        TEXT,
    PRIMARY KEY (shell_id, generation)
);
-- One live (non-ended) generation per shell.
CREATE UNIQUE INDEX IF NOT EXISTS idx_interface_generations_live
    ON interface_generations(shell_id) WHERE ended_at IS NULL;

-- ── Sessions: one row per interactive chat ──────────────────────────────────
-- occupancy walks reserved → occupied → ended (unreconciled = uncertain);
-- lifecycle is the harness TUI's own state. PID/start-ticks/tmux-socket are
-- exact-identity fencing, never authority by presence alone — and volatile,
-- so snapshot-excluded (ended rows keep only the audit trail).

CREATE TABLE IF NOT EXISTS interface_sessions (
    session_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shell_id             INTEGER NOT NULL REFERENCES shells(shell_id),
    generation           INTEGER NOT NULL CHECK (generation > 0),
    archive_id           INTEGER REFERENCES shell_memory_archives(archive_id),
    harness              TEXT,               -- claude/codex/kimi
    model_route          TEXT,
    cli_version          TEXT,
    worktree             TEXT,
    tmux_socket          TEXT,               -- volatile; snapshot-excluded
    tmux_session         TEXT,
    tmux_window          TEXT,
    tmux_pane_id         TEXT,               -- immutable tmux pane id
    pane_pid             INTEGER,            -- volatile; snapshot-excluded
    pane_start_ticks    INTEGER,            -- volatile; snapshot-excluded
    harness_pid          INTEGER,            -- volatile; snapshot-excluded
    harness_start_ticks INTEGER,            -- volatile; snapshot-excluded
    occupancy            TEXT NOT NULL DEFAULT 'reserved'
                         CHECK (occupancy IN
                             ('reserved','occupied','unreconciled','ended')),
    lifecycle            TEXT NOT NULL DEFAULT 'starting'
                         CHECK (lifecycle IN
                             ('starting','idle','busy','approval','user_input',
                              'stopping','lost','error','ended')),
    reservation_expires_at TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    occupied_at          TEXT,
    ended_at             TEXT,
    end_reason           TEXT,
    error_detail         TEXT,
    UNIQUE (shell_id, generation),
    FOREIGN KEY (shell_id, generation)
        REFERENCES interface_generations(shell_id, generation)
);
-- One partial uniqueness constraint: one non-ended Interface session per shell.
CREATE UNIQUE INDEX IF NOT EXISTS idx_interface_sessions_live
    ON interface_sessions(shell_id) WHERE occupancy <> 'ended';

-- ── Writer leases: one current writer per session ───────────────────────────
-- VOLATILE — excluded from snapshot (live leases, token hashes, heartbeats).

CREATE TABLE IF NOT EXISTS interface_writer_leases (
    lease_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER NOT NULL REFERENCES interface_sessions(session_id),
    shell_id       INTEGER NOT NULL,
    generation     INTEGER NOT NULL,
    client_id      TEXT NOT NULL,            -- browser tab / CLI instance id
    token_hash     TEXT NOT NULL,            -- volatile credential hash
    next_input_seq INTEGER NOT NULL DEFAULT 1, -- expected monotonic client seq
    heartbeat_at   TEXT,                     -- volatile
    acquired_at    TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at     TEXT,
    revoke_reason  TEXT,
    FOREIGN KEY (shell_id, generation)
        REFERENCES interface_generations(shell_id, generation)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_interface_writer_leases_current
    ON interface_writer_leases(session_id) WHERE revoked_at IS NULL;

-- ── Input state: metadata only, never bytes ─────────────────────────────────
-- LIVE rows are volatile and snapshot-excluded. A terminal
-- delivery_unknown row is durable recovery metadata (never input bytes) and
-- survives rebuild. One row per session. pending_seq is the crash-window
-- reservation: set before the tmux write, cleared only by the forward commit
-- AFTER the write. last_submit_seq is the fenced submit-callback proof that
-- lets dirty → clean.

CREATE TABLE IF NOT EXISTS interface_input_state (
    session_id          INTEGER PRIMARY KEY
                        REFERENCES interface_sessions(session_id),
    shell_id            INTEGER NOT NULL,
    generation          INTEGER NOT NULL,
    composer            TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (composer IN ('clean','dirty','unknown')),
    delivery            TEXT NOT NULL DEFAULT 'normal'
                        CHECK (delivery IN ('normal','delivery_unknown')),
    pending_seq         INTEGER,          -- reserved, not yet acked (no bytes)
    pending_reserved_at TEXT,
    forwarded_seq       INTEGER NOT NULL DEFAULT 0, -- highest forwarded seq
    last_human_input_at TEXT,
    last_submit_seq     INTEGER,          -- seq proven by fenced submit callback
    certified_by        TEXT,             -- client_id of certifying writer
    certified_seq       INTEGER,
    certified_at        TEXT,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (shell_id, generation)
        REFERENCES interface_generations(shell_id, generation)
);

-- ── HTTP idempotency keys (all Interface mutations) ─────────────────────────
-- Durable across rebuild — an exact retry must still find its original result.

CREATE TABLE IF NOT EXISTS interface_idempotency_keys (
    actor_scope       TEXT NOT NULL,     -- operator / browser:<session> / cli
    operation         TEXT NOT NULL,
    idem_key          TEXT NOT NULL,
    request_hash      TEXT NOT NULL,     -- reuse with a different body → 409
    response_status   INTEGER,
    response_resource TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at        TEXT NOT NULL,
    PRIMARY KEY (actor_scope, operation, idem_key)
);
CREATE INDEX IF NOT EXISTS idx_interface_idem_expiry
    ON interface_idempotency_keys(expires_at);

-- ── Sprint planner bindings ─────────────────────────────────────────────────
-- Arms one ACTIVE sprint document to one planner generation. Snapshot keeps
-- released rows only (closed-binding audit).

CREATE TABLE IF NOT EXISTS sprint_planner_bindings (
    binding_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_doc_id    INTEGER NOT NULL REFERENCES documents(document_id),
    planner_shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
    session_id       INTEGER NOT NULL REFERENCES interface_sessions(session_id),
    shell_id         INTEGER NOT NULL,   -- = planner_shell_id (generation FK)
    generation       INTEGER NOT NULL,
    armed_at         TEXT NOT NULL DEFAULT (datetime('now')),
    released_at      TEXT,
    release_reason   TEXT,
    FOREIGN KEY (shell_id, generation)
        REFERENCES interface_generations(shell_id, generation)
);
-- A planner owns at most one ACTIVE binding; a sprint names only one planner.
CREATE UNIQUE INDEX IF NOT EXISTS idx_spb_live_planner
    ON sprint_planner_bindings(planner_shell_id) WHERE released_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_spb_live_sprint
    ON sprint_planner_bindings(sprint_doc_id) WHERE released_at IS NULL;

-- ── Wake batches: one coalesced fixed-prompt submission ─────────────────────
-- input_seq_fence is the broker sequence the wake submitted at;
-- submit/stop_hook_seq are the durable hook-sequence evidence that decides
-- restart recovery: submitting/running → delivery_unknown UNLESS the hook
-- evidence proves the transition (decision #22).

CREATE TABLE IF NOT EXISTS planner_wake_batches (
    batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_id      INTEGER NOT NULL REFERENCES sprint_planner_bindings(binding_id),
    shell_id        INTEGER NOT NULL,
    generation      INTEGER NOT NULL,
    state           TEXT NOT NULL DEFAULT 'queued'
                    CHECK (state IN
                        ('queued','submitting','running','complete',
                         'delivery_unknown')),
    input_seq_fence INTEGER,
    submit_hook_seq INTEGER,
    stop_hook_seq   INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    submitted_at    TEXT,
    completed_at    TEXT,
    FOREIGN KEY (shell_id, generation)
        REFERENCES interface_generations(shell_id, generation)
);
-- Only one live batch per binding.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pwb_live
    ON planner_wake_batches(binding_id)
    WHERE state IN ('queued','submitting','running');

-- ── Wake items: one per unread eligible message ─────────────────────────────

CREATE TABLE IF NOT EXISTS planner_wake_items (
    item_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_id      INTEGER NOT NULL REFERENCES sprint_planner_bindings(binding_id),
    message_id      INTEGER NOT NULL REFERENCES shell_messages(message_id),
    batch_id        INTEGER REFERENCES planner_wake_batches(batch_id),
    state           TEXT NOT NULL DEFAULT 'queued'
                    CHECK (state IN
                        ('queued','batched','submitting','running','done',
                         'reconcile','quarantined','cancelled')),
    completed_wakes INTEGER NOT NULL DEFAULT 0,
    ambiguity       TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    done_at         TEXT,
    UNIQUE (binding_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_pwi_binding_state
    ON planner_wake_items(binding_id, state);
CREATE INDEX IF NOT EXISTS idx_pwi_batch ON planner_wake_items(batch_id);

-- ── Planner action receipts: idempotent side-effect guard ───────────────────

CREATE TABLE IF NOT EXISTS planner_action_receipts (
    receipt_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id     INTEGER REFERENCES shell_messages(message_id),
    operation      TEXT NOT NULL,
    target         TEXT NOT NULL,
    idem_key       TEXT NOT NULL UNIQUE, -- derived from message+operation+target
    state          TEXT NOT NULL DEFAULT 'intent'
                   CHECK (state IN ('intent','complete','unknown','reconciled')),
    result_detail  TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at   TEXT,
    reconciled_at  TEXT
);

-- ── PR poll audit ───────────────────────────────────────────────────────────
-- pr_poll_runs is VOLATILE (successful no-transition runs are noise) and
-- excluded from snapshot. observations are durable only when they carry a
-- semantic transition or a blind-window marker (snapshot row filter), so its
-- run_id linkage is audit-only (no REFERENCES — the run row may not survive).

CREATE TABLE IF NOT EXISTS pr_poll_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT,
    source      TEXT NOT NULL,           -- scheduler / startup / reconcile
    watch_count INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running','ok','error','rate_limited')),
    error       TEXT                     -- sanitized
);

CREATE TABLE IF NOT EXISTS pr_poll_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id    INTEGER NOT NULL REFERENCES watched_prs(watch_id),
    run_id      INTEGER,                 -- audit linkage only (runs are volatile)
    head_sha    TEXT,
    fingerprint TEXT,                    -- normalized JSON; never raw payloads
    transition  TEXT,                    -- semantic transition key; NULL = none
    blind_window INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ppo_watch
    ON pr_poll_observations(watch_id, observed_at);

-- ── Alerts: deduplicated while open ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS planner_alerts (
    alert_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES interface_sessions(session_id),
    binding_id  INTEGER REFERENCES sprint_planner_bindings(binding_id),
    message_id  INTEGER REFERENCES shell_messages(message_id),
    watch_id    INTEGER REFERENCES watched_prs(watch_id),
    severity    TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    reason      TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,           -- app-computed: refs + reason
    opened_at   TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_planner_alerts_open
    ON planner_alerts(dedupe_key) WHERE resolved_at IS NULL;

-- ── Sprint scoping columns on existing tables ───────────────────────────────
-- Nullable; existing rows stay valid and unwoken (migration-only ADD COLUMN —
-- the 0047/0059/0062 precedent; schema.sql carries pointer comments). The
-- watched_prs uniqueness rebuild into one active watch per binding/repo/PR is
-- the polling cutover's job (task #85), not this unit's.

ALTER TABLE shell_messages ADD COLUMN sprint_doc_id INTEGER
    REFERENCES documents(document_id);
ALTER TABLE watched_prs ADD COLUMN sprint_doc_id INTEGER
    REFERENCES documents(document_id);
CREATE INDEX IF NOT EXISTS idx_shell_messages_sprint
    ON shell_messages(to_shell_id, sprint_doc_id)
    WHERE sprint_doc_id IS NOT NULL;

-- ── Transition validators (DB backstop — RAISE(ABORT) style) ────────────────
-- The app-level mirror lives in scripts/interface_state.py; keep the edge
-- sets in sync (tests/test_interface_transitions.py walks every pair against
-- BOTH, so drift fails the suite).

CREATE TRIGGER IF NOT EXISTS trg_interface_sessions_occupancy
BEFORE UPDATE OF occupancy ON interface_sessions
WHEN NEW.occupancy <> OLD.occupancy AND NOT (
    (OLD.occupancy = 'reserved'     AND NEW.occupancy IN ('occupied','unreconciled','ended')) OR
    (OLD.occupancy = 'occupied'     AND NEW.occupancy IN ('unreconciled','ended')) OR
    (OLD.occupancy = 'unreconciled' AND NEW.occupancy IN ('occupied','ended'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal interface occupancy transition');
END;

CREATE TRIGGER IF NOT EXISTS trg_interface_sessions_lifecycle
BEFORE UPDATE OF lifecycle ON interface_sessions
WHEN NEW.lifecycle <> OLD.lifecycle AND NOT (
    (OLD.lifecycle = 'starting'   AND NEW.lifecycle IN ('idle','stopping','lost','error','ended')) OR
    (OLD.lifecycle = 'idle'       AND NEW.lifecycle IN ('busy','stopping','lost')) OR
    (OLD.lifecycle = 'busy'       AND NEW.lifecycle IN ('idle','approval','user_input','error','stopping','lost')) OR
    (OLD.lifecycle = 'approval'   AND NEW.lifecycle IN ('busy','error','stopping','lost')) OR
    (OLD.lifecycle = 'user_input' AND NEW.lifecycle IN ('busy','error','stopping','lost')) OR
    (OLD.lifecycle = 'stopping'   AND NEW.lifecycle IN ('ended','lost','error')) OR
    -- unexpected verified exit/close after proof: lost/error → ended
    (OLD.lifecycle = 'lost'       AND NEW.lifecycle IN ('ended','stopping')) OR
    (OLD.lifecycle = 'error'      AND NEW.lifecycle IN ('ended','stopping'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal interface lifecycle transition');
END;

CREATE TRIGGER IF NOT EXISTS trg_interface_input_composer
BEFORE UPDATE OF composer ON interface_input_state
WHEN NEW.composer <> OLD.composer AND NOT (
    (OLD.composer = 'unknown' AND NEW.composer IN ('clean','dirty')) OR
    (OLD.composer = 'clean'   AND NEW.composer IN ('dirty','unknown')) OR
    (OLD.composer = 'dirty'   AND NEW.composer IN ('clean','unknown'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal composer transition');
END;

CREATE TRIGGER IF NOT EXISTS trg_interface_input_delivery
BEFORE UPDATE OF delivery ON interface_input_state
WHEN NEW.delivery <> OLD.delivery AND NOT (
    (OLD.delivery = 'normal'           AND NEW.delivery = 'delivery_unknown') OR
    (OLD.delivery = 'delivery_unknown' AND NEW.delivery = 'normal')
)
BEGIN
  SELECT RAISE(ABORT, 'illegal delivery transition');
END;

CREATE TRIGGER IF NOT EXISTS trg_pwi_state
BEFORE UPDATE OF state ON planner_wake_items
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state = 'queued'      AND NEW.state IN ('batched','quarantined','cancelled')) OR
    (OLD.state = 'batched'     AND NEW.state IN ('queued','submitting','cancelled')) OR
    (OLD.state = 'submitting'  AND NEW.state IN ('queued','running','cancelled')) OR
    (OLD.state = 'running'     AND NEW.state IN ('done','reconcile','queued','cancelled')) OR
    (OLD.state = 'reconcile'   AND NEW.state IN ('queued','done','cancelled')) OR
    (OLD.state = 'quarantined' AND NEW.state IN ('queued','cancelled'))
    -- done and cancelled are terminal
)
BEGIN
  SELECT RAISE(ABORT, 'illegal wake item transition');
END;

CREATE TRIGGER IF NOT EXISTS trg_pwb_state
BEFORE UPDATE OF state ON planner_wake_batches
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state = 'queued'           AND NEW.state IN ('submitting','complete')) OR
    (OLD.state = 'submitting'       AND NEW.state IN ('queued','running','delivery_unknown')) OR
    (OLD.state = 'running'          AND NEW.state IN ('complete','delivery_unknown')) OR
    -- operator reconciliation closes a parked batch; items are re-queued anew
    (OLD.state = 'delivery_unknown' AND NEW.state = 'complete')
    -- complete is terminal
)
BEGIN
  SELECT RAISE(ABORT, 'illegal wake batch transition');
END;

CREATE TRIGGER IF NOT EXISTS trg_par_state
BEFORE UPDATE OF state ON planner_action_receipts
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state = 'intent'  AND NEW.state IN ('complete','unknown')) OR
    (OLD.state = 'unknown' AND NEW.state = 'reconciled')
    -- complete and reconciled are terminal
)
BEGIN
  SELECT RAISE(ABORT, 'illegal action receipt transition');
END;

COMMIT;
