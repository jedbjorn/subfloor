-- super-coder — SQLite schema (full current baseline).
--
-- Forkable shell substrate for a single repo. Derived from superCC's substrate
-- schema, inverted to the one-repo model and extended with the roadmap index +
-- content store (spec §Data Model).
--
-- The live shell_db.db is GITIGNORED and rebuilt from this file + migrations/ +
-- snapshot/. A fresh build applies this whole file; existing forks catch up via
-- ordered migrations/*.sql (recorded in schema_migrations).
--
-- Auth note (v1): the launcher is username-only — no password challenge. The
-- password_hash/password_salt columns are kept nullable for forward-compat but
-- are unused at v1.

-- ── Migration ledger ────────────────────────────────────────────────────────
-- Records which migrations/*.sql files have been applied. A fresh build stamps
-- every existing migration as the baseline (squash); updates apply only the
-- unstamped ones.

CREATE TABLE schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Identity ────────────────────────────────────────────────────────────────

CREATE TABLE users (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    email         TEXT,
    initials      TEXT,
    password_hash TEXT,                 -- unused at v1 (no-password launcher)
    password_salt TEXT,                 -- unused at v1
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE shells (
    shell_id          INTEGER PRIMARY KEY,
    display_name      TEXT    NOT NULL,
    shortname         TEXT,
    partner           TEXT,
    role              TEXT,
    mandate           TEXT,
    system_prompt     TEXT    NOT NULL,
    current_state     TEXT,
    connections       TEXT,                          -- RETIRED (B5): authored "where things live" layer; nothing prompted shells to fill it so it sat empty — ## CONNECTIONS is now wholly derived from the dr_* map. Unrendered, unauthored, kept to avoid a table rebuild
    workspace         TEXT,                          -- RETIRED (B5): superseded by connections (itself since retired); unrendered, unauthored, kept to avoid a table rebuild

    lineage_seed      TEXT,
    flavor            TEXT,                          -- dev / planner / reviewer / cartographer (NULL = bespoke, e.g. maintainer); launch defaults in flavor_defaults
    has_identity      INTEGER NOT NULL DEFAULT 0,
    bootstrapped      INTEGER NOT NULL DEFAULT 0,   -- 1 once the shell has run first-run orientation

    active_archive_id INTEGER,
    user_id           INTEGER REFERENCES users(user_id),
    is_shared         INTEGER NOT NULL DEFAULT 0,
    is_deleted        INTEGER NOT NULL DEFAULT 0
);

-- Singleton guard: a fork has exactly one cartographer — it owns the repo map
-- and no other shell maps, so a second one is incoherent. Mirrors the seed/L&S
-- cap triggers (RAISE(ABORT) below the line). is_deleted=0 so a deleted
-- cartographer frees the slot. shell_factory pre-checks for a friendly error;
-- this is the DB backstop that also catches direct writes / the API path.
CREATE TRIGGER trg_singleton_cartographer
BEFORE INSERT ON shells
WHEN NEW.flavor = 'cartographer' AND (
  SELECT COUNT(*) FROM shells
  WHERE flavor = 'cartographer' AND is_deleted = 0
) >= 1
BEGIN
  SELECT RAISE(ABORT, 'cartographer is a singleton — this fork already has one');
END;

-- Per-flavor launch defaults: the harness + model a shell of this flavor boots
-- with. ADVISORY ONLY — overridable per launch (--harness / -m / the picker);
-- A (flavor, harness) matrix: each flavor offers a model per harness, so the
-- operator picks the harness at launch and gets that harness's model. run.py
-- reads these to resolve the launch model + annotate the picker; is_default marks
-- the picker's pre-selected harness for a flavor. model is harness-specific (codex
-- bare id "gpt-5.6-sol" / claude alias "fable" / opencode "provider/model"); NULL =
-- let the harness pick its own. Reshaped + reseeded in migrations/0007.
CREATE TABLE flavor_defaults (
    flavor     TEXT    NOT NULL,
    harness    TEXT    NOT NULL,
    model      TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (flavor, harness)
);

CREATE TABLE shell_memory_archives (
    archive_id     INTEGER PRIMARY KEY,
    shell_id       INTEGER NOT NULL REFERENCES shells(shell_id),
    session_id     TEXT,
    date           DATE    NOT NULL,
    full_narrative TEXT
);

-- ── Seed + L&S (table-backed, cap-enforced) ─────────────────────────────────

CREATE TABLE shell_identity_entries (
    entry_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    shell_id    INTEGER NOT NULL REFERENCES shells(shell_id),
    kind        TEXT    NOT NULL CHECK (kind IN ('seed', 'lns')),
    entry_date  TEXT,
    source_tag  TEXT,
    body        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    retired_at  TEXT,
    is_deleted  INTEGER NOT NULL DEFAULT 0
);

CREATE TRIGGER trg_sie_cap_seed
BEFORE INSERT ON shell_identity_entries
WHEN NEW.kind = 'seed' AND (
  SELECT COUNT(*) FROM shell_identity_entries
  WHERE shell_id = NEW.shell_id AND kind='seed'
    AND is_deleted=0 AND retired_at IS NULL
) >= 10
BEGIN
  SELECT RAISE(ABORT, 'seed cap (10) reached for this shell — retire an entry first');
END;

CREATE TRIGGER trg_sie_cap_lns
BEFORE INSERT ON shell_identity_entries
WHEN NEW.kind = 'lns' AND (
  SELECT COUNT(*) FROM shell_identity_entries
  WHERE shell_id = NEW.shell_id AND kind='lns'
    AND is_deleted=0 AND retired_at IS NULL
) >= 20
BEGIN
  SELECT RAISE(ABORT, 'L&S cap (20) reached for this shell — retire an entry first');
END;

-- ── Decisions ───────────────────────────────────────────────────────────────

CREATE TABLE shell_decisions (
    decision_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    shell_id           INTEGER NOT NULL REFERENCES shells(shell_id),
    decision_date      DATE    NOT NULL,
    priority           TEXT    NOT NULL DEFAULT 'M' CHECK(priority IN ('M','m')),
    decision           TEXT    NOT NULL,
    rationale          TEXT,
    parent_decision_id INTEGER REFERENCES shell_decisions(decision_id),
    is_deleted         INTEGER NOT NULL DEFAULT 0,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    -- feature_id  INTEGER REFERENCES roadmap(feature_id)   — the feature this
    -- document_id INTEGER REFERENCES documents(document_id) — decision shaped
    -- (the why-audit link), both added by migration 0047. Kept out of this
    -- baseline CREATE on purpose: ADD COLUMN can't be IF NOT EXISTS and rebuild
    -- applies migrations after schema.sql, so inlining would double-define.
    -- See migrations/0047_decisions_feature_link.sql.
);

-- ── Roadmap (NEW — the feature index) ───────────────────────────────────────
-- One row per planned feature. The DB *is* the index that kills "where does the
-- spec for X live." Status is a planning horizon (a column, not a folder).

CREATE TABLE roadmap (
    feature_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    roadmap_status TEXT    NOT NULL DEFAULT 'brainstorm'
                   -- funnel order: idea inlet → most-active committed work →
                   -- done (shipped) → taken-off-the-board (retired). shipped
                   -- means we delivered; retired means we chose not to.
                   CHECK (roadmap_status IN
                       ('brainstorm','in_progress','next','near_term',
                        'long_term','shipped','retired')),
    sort_order     INTEGER NOT NULL DEFAULT 0,   -- ordering within a bucket
    owning_shell   INTEGER REFERENCES shells(shell_id),
    summary        TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
    -- project_id INTEGER REFERENCES projects(project_id) — work-stream this
    -- feature belongs to (NULL = unassigned), added by migration 0018. Kept out
    -- of this baseline CREATE on purpose: ADD COLUMN can't be IF NOT EXISTS and
    -- rebuild applies migrations after schema.sql, so inlining it would
    -- double-define the column. See migrations/0018_roadmap_project.sql.
);

-- ── Feature blockers (the roadmap's sequencing edges) ───────────────────────
-- A directed many-to-many self-relation on roadmap. One row = one dependency:
-- `feature_id` is blocked by `blocked_by` (blocked_by must land first). A feature
-- may be blocked by many. The flowchart view renders these as arrows. Cycle
-- prevention is app-level (server.py) so the graph stays a DAG; the table guards
-- only against self-blocks and duplicates. Edges among brainstorm/retired
-- features are simply not drawn (those stages don't sequence yet).

CREATE TABLE feature_blockers (
    feature_id  INTEGER NOT NULL REFERENCES roadmap(feature_id),
    blocked_by  INTEGER NOT NULL REFERENCES roadmap(feature_id),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (feature_id, blocked_by),
    CHECK (feature_id <> blocked_by)
);
CREATE INDEX idx_feature_blockers_blocked_by ON feature_blockers(blocked_by);

-- ── Documents (NEW — the content store) ─────────────────────────────────────
-- DB owns the body, always. A feature accumulates MULTIPLE specs over its life:
-- each stage's spec freezes on ship (frozen=1, immutable), the feature lives on,
-- the next stage opens a new spec. One feature : many docs, each freezable.

CREATE TABLE documents (
    document_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id   INTEGER REFERENCES roadmap(feature_id),  -- NULL = general doc (not tied to a feature)
    kind         TEXT    NOT NULL DEFAULT 'spec' CHECK (kind IN ('spec','doc')),
    seq          INTEGER NOT NULL DEFAULT 1,     -- lineage within (feature, kind)
    title        TEXT,
    frozen       INTEGER NOT NULL DEFAULT 0,     -- 1 = frozen on ship, immutable
    frozen_date  TEXT,
    body         TEXT,                           -- canonical markdown, lives here
    render_path  TEXT,                           -- repo-relative flat-file target
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(feature_id, kind, seq)
);

-- ── Flags (substrate task tracking; link to a feature) ──────────────────────

CREATE TABLE flags (
    flag_id          INTEGER PRIMARY KEY,
    display_name     TEXT,
    priority         TEXT    NOT NULL DEFAULT 'Medium'
                     CHECK(priority IN ('High','Medium','Low')),
    description      TEXT,
    created_date     DATE    NOT NULL DEFAULT (date('now')),
    resolved_date    DATE,
    resolved         INTEGER NOT NULL DEFAULT 0,
    shell_id         INTEGER REFERENCES shells(shell_id),
    feature_id       INTEGER REFERENCES roadmap(feature_id),  -- a feature's blockers
    resolution_notes TEXT,
    parent_flag_id   INTEGER REFERENCES flags(flag_id),
    is_deleted       INTEGER NOT NULL DEFAULT 0
);

-- ── Spec tasks (per-instance — implementation plan for a spec) ──────────────
-- One row per task. Seq 0 = Preparation, last seq = Verification, middle = impl
-- steps. Status drives current_state updates (last done + next pending).
-- 'cancelled' + resolution_notes (migration 0064 rebuild, #342): the honest
-- terminal state for a task overtaken by a feature split/re-scope — the work
-- was never built; the notes say why and where it went.

CREATE TABLE spec_tasks (
    task_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id     INTEGER NOT NULL REFERENCES roadmap(feature_id),
    document_id    INTEGER NOT NULL REFERENCES documents(document_id),
    seq            INTEGER NOT NULL,
    title          TEXT    NOT NULL,
    description    TEXT,
    status         TEXT    NOT NULL DEFAULT 'pending'
                   CHECK(status IN ('pending','in_progress','done','cancelled')),
    completed_date DATE,
    resolution_notes TEXT,
    shell_id       INTEGER REFERENCES shells(shell_id),
    created_date   DATE    NOT NULL DEFAULT (date('now')),
    UNIQUE(document_id, seq)
);

-- ── Shell Inbox (inter-shell messaging) ─────────────────────────────────────
-- A shell writes a markdown message to another shell; the recipient discovers it
-- on its next boot (the `## STATUS` Inbox count + the `messaging` skill's `check`
-- verb) and marks it read by UPDATE-ing `read_at`. No API layer in v1 — the
-- `messaging` skill runs parameterized SQL directly (single-user, localhost). The
-- only enforcement v1 has is at the DB layer: FK on from/to, NOT NULL, body CHECK.
-- See migrations/0004_shell_messages.sql (convergent — carries an existing fork).

CREATE TABLE shell_messages (
    message_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    from_shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
    to_shell_id   INTEGER NOT NULL REFERENCES shells(shell_id),
    body          TEXT    NOT NULL CHECK (length(body) > 0),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    read_at       TEXT                          -- NULL = unread
    -- kind TEXT NOT NULL DEFAULT 'shell' CHECK (kind IN
    -- ('shell','task','result','pr_event')) — typed sprint-eventing traffic,
    -- added by migration 0059. Kept out of this baseline CREATE on purpose:
    -- ADD COLUMN can't be IF NOT EXISTS and rebuild applies migrations after
    -- schema.sql, so inlining would double-define (the 0047 precedent). See
    -- migrations/0059_sprint_eventing.sql.
    -- dedupe_key TEXT — idempotent send (#333), added by migration 0062
    -- (same migration-only precedent; unique partial index
    -- idx_shell_messages_dedupe rides in the migration).
    -- sprint_doc_id INTEGER REFERENCES documents(document_id) — sprint scoping
    -- for wake-eligible traffic, added by migration 0078 (same precedent).
);

-- ── Watched PRs (sprint eventing — subscription registry + daemon state) ────
-- One row per (repo, PR, subscriber shell). `./sc watch pr` registers; the
-- GitHub watcher daemon (`./sc watch daemon`, supervised by launch/down) polls
-- every live watch on one batched query, diffs against last_seen, and writes a
-- `pr_event` message row to the owning shell on each transition. On merge or
-- close it emits the final event and sets closed_at — the watch retires
-- itself. See migrations/0059_sprint_eventing.sql (convergent — carries an
-- existing fork).

CREATE TABLE watched_prs (
    watch_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    repo           TEXT    NOT NULL,          -- owner/name
    pr_number      INTEGER NOT NULL,
    shell_id       INTEGER NOT NULL REFERENCES shells(shell_id),
    last_seen      TEXT,                      -- JSON: checks/review/state fingerprint
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at      TEXT,                      -- set on merge/close; NULL = live
    UNIQUE (repo, pr_number, shell_id)
    -- sprint_doc_id INTEGER REFERENCES documents(document_id) — sprint scoping,
    -- added by migration 0078 (same migration-only ADD COLUMN precedent as
    -- shell_messages.kind above). The uniqueness rebuild into one active watch
    -- per binding/repo/PR is the polling cutover's migration (spec #20 task 36).
);

-- Daemon liveness (#359): the watcher daemon UPSERTs its row once per poll
-- cycle; the /_sc/watches API turns beat age into a live/stale/never verdict
-- so `./sc watch list` can't report watches "live" with nobody polling.
-- See migrations/0068_watch_daemon_heartbeat.sql (convergent — carries an
-- existing fork).

CREATE TABLE daemon_heartbeats (
    name        TEXT PRIMARY KEY,              -- 'watch' — one row per daemon
    beat_at     TEXT    NOT NULL,              -- datetime('now') at last poll cycle
    interval_s  INTEGER NOT NULL               -- the daemon's configured poll interval
);

-- ── Interface (spec #20: Interface-backed planner wake) ─────────────────────
-- Durable state for the API-brokered interactive chat surface: one generation
-- per shell, exact-identity sessions, writer leases, metadata-only input
-- state, idempotency keys, sprint planner bindings, wake items/batches,
-- action receipts, PR poll audit, and alerts. Volatile runtime tables
-- (interface_writer_leases, interface_input_state, pr_poll_runs) are
-- deliberately NOT in snapshot.py's PER_INSTANCE_TABLES; volatile columns
-- (tmux socket, PIDs/start ticks, hook token hash) ride SENSITIVE_COLUMNS.
-- See migrations/0078_interface_sessions.sql (convergent — carries an
-- existing fork) and scripts/interface_state.py (app-level edge maps — keep
-- in sync with the transition triggers below).

CREATE TABLE interface_generations (
    shell_id        INTEGER NOT NULL REFERENCES shells(shell_id),
    generation      INTEGER NOT NULL CHECK (generation > 0),
    hook_token_hash TEXT,                      -- volatile hash; snapshot-excluded
    last_hook_seq   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at        TEXT,
    PRIMARY KEY (shell_id, generation)
);

CREATE TABLE interface_sessions (
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

CREATE TABLE interface_writer_leases (
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

CREATE TABLE interface_input_state (
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

CREATE TABLE interface_idempotency_keys (
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

CREATE TABLE sprint_planner_bindings (
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

CREATE TABLE planner_wake_batches (
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

CREATE TABLE planner_wake_items (
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

CREATE TABLE planner_action_receipts (
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

-- pr_poll_observations.run_id is deliberately NOT a REFERENCES: runs are
-- volatile (excluded from snapshot) while transition/blind-window observations
-- are durable audit — the linkage outlives its target.
CREATE TABLE pr_poll_runs (
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

CREATE TABLE pr_poll_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id    INTEGER NOT NULL REFERENCES watched_prs(watch_id),
    run_id      INTEGER,                 -- audit linkage only (runs are volatile)
    head_sha    TEXT,
    fingerprint TEXT,                    -- normalized JSON; never raw payloads
    transition  TEXT,                    -- semantic transition key; NULL = none
    blind_window INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE planner_alerts (
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

-- Transition validators — the DB backstop (app pre-checks for friendly
-- errors via scripts/interface_state.py; keep the edge sets in sync).

CREATE TRIGGER trg_interface_sessions_occupancy
BEFORE UPDATE OF occupancy ON interface_sessions
WHEN NEW.occupancy <> OLD.occupancy AND NOT (
    (OLD.occupancy = 'reserved'     AND NEW.occupancy IN ('occupied','unreconciled','ended')) OR
    (OLD.occupancy = 'occupied'     AND NEW.occupancy IN ('unreconciled','ended')) OR
    (OLD.occupancy = 'unreconciled' AND NEW.occupancy IN ('occupied','ended'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal interface occupancy transition');
END;

CREATE TRIGGER trg_interface_sessions_lifecycle
BEFORE UPDATE OF lifecycle ON interface_sessions
WHEN NEW.lifecycle <> OLD.lifecycle AND NOT (
    (OLD.lifecycle = 'starting'   AND NEW.lifecycle IN ('idle','stopping','lost','error','ended')) OR
    (OLD.lifecycle = 'idle'       AND NEW.lifecycle IN ('busy','stopping','lost')) OR
    (OLD.lifecycle = 'busy'       AND NEW.lifecycle IN ('idle','approval','user_input','error','stopping','lost')) OR
    (OLD.lifecycle = 'approval'   AND NEW.lifecycle IN ('busy','error','stopping','lost')) OR
    (OLD.lifecycle = 'user_input' AND NEW.lifecycle IN ('busy','error','stopping','lost')) OR
    (OLD.lifecycle = 'stopping'   AND NEW.lifecycle IN ('ended','lost','error')) OR
    (OLD.lifecycle = 'lost'       AND NEW.lifecycle IN ('ended','stopping')) OR
    (OLD.lifecycle = 'error'      AND NEW.lifecycle IN ('ended','stopping'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal interface lifecycle transition');
END;

CREATE TRIGGER trg_interface_input_composer
BEFORE UPDATE OF composer ON interface_input_state
WHEN NEW.composer <> OLD.composer AND NOT (
    (OLD.composer = 'unknown' AND NEW.composer IN ('clean','dirty')) OR
    (OLD.composer = 'clean'   AND NEW.composer IN ('dirty','unknown')) OR
    (OLD.composer = 'dirty'   AND NEW.composer IN ('clean','unknown'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal composer transition');
END;

CREATE TRIGGER trg_interface_input_delivery
BEFORE UPDATE OF delivery ON interface_input_state
WHEN NEW.delivery <> OLD.delivery AND NOT (
    (OLD.delivery = 'normal'           AND NEW.delivery = 'delivery_unknown') OR
    (OLD.delivery = 'delivery_unknown' AND NEW.delivery = 'normal')
)
BEGIN
  SELECT RAISE(ABORT, 'illegal delivery transition');
END;

CREATE TRIGGER trg_pwi_state
BEFORE UPDATE OF state ON planner_wake_items
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state = 'queued'      AND NEW.state IN ('batched','quarantined','cancelled')) OR
    (OLD.state = 'batched'     AND NEW.state IN ('queued','submitting','cancelled')) OR
    (OLD.state = 'submitting'  AND NEW.state IN ('queued','running','cancelled')) OR
    (OLD.state = 'running'     AND NEW.state IN ('done','reconcile','queued','cancelled')) OR
    (OLD.state = 'reconcile'   AND NEW.state IN ('queued','done','cancelled')) OR
    (OLD.state = 'quarantined' AND NEW.state IN ('queued','cancelled'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal wake item transition');
END;

CREATE TRIGGER trg_pwb_state
BEFORE UPDATE OF state ON planner_wake_batches
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state = 'queued'           AND NEW.state IN ('submitting','complete')) OR
    (OLD.state = 'submitting'       AND NEW.state IN ('queued','running','delivery_unknown')) OR
    (OLD.state = 'running'          AND NEW.state IN ('complete','delivery_unknown')) OR
    (OLD.state = 'delivery_unknown' AND NEW.state = 'complete')
)
BEGIN
  SELECT RAISE(ABORT, 'illegal wake batch transition');
END;

CREATE TRIGGER trg_par_state
BEFORE UPDATE OF state ON planner_action_receipts
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state = 'intent'  AND NEW.state IN ('complete','unknown')) OR
    (OLD.state = 'unknown' AND NEW.state = 'reconciled')
)
BEGIN
  SELECT RAISE(ABORT, 'illegal action receipt transition');
END;

-- ── Skills (system content — seeded from assets/, propagates) ────────────────

CREATE TABLE skills (
    skill_id    INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    description TEXT,
    category    TEXT,
    content     TEXT,
    command     TEXT,
    common      INTEGER NOT NULL DEFAULT 1,
    is_deleted  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE shell_skills (
    shell_skill_id  INTEGER PRIMARY KEY,
    shell_id        INTEGER NOT NULL REFERENCES shells(shell_id),
    skill_id        INTEGER NOT NULL REFERENCES skills(skill_id),
    UNIQUE(shell_id, skill_id)
);

-- ── Projects (per-shell project standing) ───────────────────────────────────

CREATE TABLE projects (
    project_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    shortname    TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    purpose      TEXT,
    standing     TEXT,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK(status IN ('active','inactive','paused')),
    is_deleted   INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE project_shells (
    project_shell_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       INTEGER NOT NULL REFERENCES projects(project_id),
    shell_id         INTEGER NOT NULL REFERENCES shells(shell_id),
    role             TEXT,
    added_date       DATE NOT NULL DEFAULT (date('now')),
    is_deleted       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (project_id, shell_id)
);

-- ── Repo catalogue (dr_*) — VESTIGIAL, transition-only ──────────────────────
-- The map moved to its OWN db (`.sc-state/map.db`, schema in `map_schema.sql`)
-- so engine memory-schema changes never touch it. These definitions remain in
-- shell_db.db for ONE release purely so a pre-split `.sc-state/content.sql`
-- (which still carries `INSERT INTO dr_section …`) can load on a rebuild without
-- erroring. map_repo no longer writes here; map_db.seed_authored() lifts any
-- rows that land here into map.db on the first post-split map. Remove in a later
-- release once all forks have re-snapshotted (dr_section → map_content.sql).

CREATE TABLE dr_repo (
    repo_id        INTEGER PRIMARY KEY,
    name           TEXT,
    root           TEXT,
    remote         TEXT,
    vcs            TEXT,
    default_branch TEXT,
    file_count     INTEGER,
    mapped_at      TEXT
);

CREATE TABLE dr_filepath (
    file_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    path     TEXT NOT NULL UNIQUE,    -- repo-relative; UNIQUE → map_repo UPSERTs by path
    ext      TEXT,
    lang     TEXT,                    -- inferred from extension
    role     TEXT,                    -- code / doc / config / test / asset / env
    bytes    INTEGER,
    lines    INTEGER,
    desc     TEXT                     -- ≤100 chars, cartographer-authored; NULL until described.
);                                    -- PRESERVED across the auto-remap (map_repo UPSERT keeps it).

-- Sectioned navigation over the file map (B5). Authored, stable, small (~10-20
-- rows) — NOT wiped by the remap. Files join to a section by path-prefix at
-- query/render time (no file ids stored), so a wiped+repopulated dr_filepath
-- never needs re-stitching and a new file auto-falls into its section. Seeded
-- from top-level dirs on first map; the cartographer renames / merges / curates.
CREATE TABLE dr_section (
    section_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,          -- "API", "UI", "Docs", "Schema", …
    path_prefix  TEXT NOT NULL,          -- repo-relative prefix the section covers
    description  TEXT,                    -- one line, what this area is
    sort_order   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name)
);

CREATE TABLE dr_dependency (
    dep_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    manager     TEXT,                 -- npm / pip / poetry / go / cargo
    name        TEXT NOT NULL,
    version     TEXT,
    kind        TEXT,                 -- runtime / dev
    source_file TEXT
);

CREATE TABLE dr_env (
    env_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    source_file TEXT
);

-- ── Indexes ─────────────────────────────────────────────────────────────────

CREATE INDEX idx_flags_parent   ON flags(parent_flag_id);
CREATE INDEX idx_flags_feature  ON flags(feature_id);
CREATE INDEX idx_decisions_shell ON shell_decisions(shell_id, decision_date);
CREATE INDEX idx_roadmap_status ON roadmap(roadmap_status, sort_order);
CREATE INDEX idx_documents_feature ON documents(feature_id, kind, seq);
CREATE INDEX idx_sie_shell_kind_active
    ON shell_identity_entries(shell_id, kind)
    WHERE is_deleted = 0 AND retired_at IS NULL;
CREATE INDEX idx_shell_messages_to_unread ON shell_messages(to_shell_id, read_at);
CREATE INDEX idx_watched_prs_live ON watched_prs(closed_at) WHERE closed_at IS NULL;
CREATE INDEX idx_dr_filepath_role ON dr_filepath(role);
CREATE INDEX idx_dr_filepath_lang ON dr_filepath(lang);
CREATE INDEX idx_dr_dependency_mgr ON dr_dependency(manager);

-- Interface (0078; the migration-only idx_shell_messages_sprint rides 0078
-- because its column does)
CREATE UNIQUE INDEX idx_interface_generations_live
    ON interface_generations(shell_id) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX idx_interface_sessions_live
    ON interface_sessions(shell_id) WHERE occupancy <> 'ended';
CREATE UNIQUE INDEX idx_interface_writer_leases_current
    ON interface_writer_leases(session_id) WHERE revoked_at IS NULL;
CREATE INDEX idx_interface_idem_expiry
    ON interface_idempotency_keys(expires_at);
CREATE UNIQUE INDEX idx_spb_live_planner
    ON sprint_planner_bindings(planner_shell_id) WHERE released_at IS NULL;
CREATE UNIQUE INDEX idx_spb_live_sprint
    ON sprint_planner_bindings(sprint_doc_id) WHERE released_at IS NULL;
CREATE UNIQUE INDEX idx_pwb_live
    ON planner_wake_batches(binding_id)
    WHERE state IN ('queued','submitting','running');
CREATE INDEX idx_pwi_binding_state
    ON planner_wake_items(binding_id, state);
CREATE INDEX idx_pwi_batch ON planner_wake_items(batch_id);
CREATE INDEX idx_ppo_watch
    ON pr_poll_observations(watch_id, observed_at);
CREATE UNIQUE INDEX idx_planner_alerts_open
    ON planner_alerts(dedupe_key) WHERE resolved_at IS NULL;
