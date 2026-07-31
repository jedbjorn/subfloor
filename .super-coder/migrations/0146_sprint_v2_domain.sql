-- 0146 — Sprints v2 durable domain foundation.
--
-- Sprint v1 was deliberately removed by migration 0144.  These tables are a
-- new domain: lifecycle is small and explicit, collaboration messages do not
-- share generic shell_messages rows, and external work is represented by
-- outbox/observation records rather than performed inside transactions.

BEGIN;

CREATE TABLE sprint_spec_approvals (
    approval_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id        INTEGER NOT NULL REFERENCES documents(document_id),
    revision_sha256    TEXT NOT NULL
                       CHECK (
                         length(revision_sha256)=64
                         AND revision_sha256 NOT GLOB '*[^0-9a-f]*'
                       ),
    reviewer_shell_id  INTEGER NOT NULL REFERENCES shells(shell_id),
    verdict            TEXT NOT NULL CHECK (verdict IN ('pass','fail')),
    findings_document_id INTEGER REFERENCES documents(document_id),
    reviewed_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (document_id, revision_sha256, reviewer_shell_id)
);

CREATE TABLE sprints (
    sprint_id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id                   INTEGER NOT NULL REFERENCES roadmap(feature_id),
    originating_planner_shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
    lifecycle                    TEXT NOT NULL DEFAULT 'prepared'
                                 CHECK (lifecycle IN
                                   ('prepared','armed','paused',
                                    'completed','aborted')),
    merge_grant_enabled          INTEGER NOT NULL DEFAULT 0
                                 CHECK (merge_grant_enabled IN (0,1)),
    terminal_outcome             TEXT,
    created_at                   TEXT NOT NULL DEFAULT (datetime('now')),
    armed_at                     TEXT,
    paused_at                    TEXT,
    completed_at                 TEXT,
    aborted_at                   TEXT,
    updated_at                   TEXT NOT NULL DEFAULT (datetime('now')),
    version                      INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    CHECK (
      (lifecycle IN ('completed','aborted') AND terminal_outcome IS NOT NULL)
      OR
      (lifecycle NOT IN ('completed','aborted') AND terminal_outcome IS NULL)
    )
);

-- SQLite has one writer but can have many processes deciding concurrently.
-- The invariant therefore belongs in the database, inside the arm transaction.
CREATE UNIQUE INDEX idx_sprints_single_armed
    ON sprints((1)) WHERE lifecycle='armed';
CREATE INDEX idx_sprints_feature ON sprints(feature_id, sprint_id);

CREATE TRIGGER trg_sprints_lifecycle
BEFORE UPDATE OF lifecycle ON sprints
WHEN NEW.lifecycle <> OLD.lifecycle AND NOT (
    (OLD.lifecycle='prepared' AND NEW.lifecycle IN ('armed','aborted')) OR
    (OLD.lifecycle='armed' AND NEW.lifecycle IN
        ('paused','completed','aborted')) OR
    (OLD.lifecycle='paused' AND NEW.lifecycle IN ('armed','aborted'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal Sprint lifecycle transition');
END;

CREATE TRIGGER trg_sprints_arming_requires_grant
BEFORE UPDATE OF lifecycle ON sprints
WHEN NEW.lifecycle='armed' AND NEW.merge_grant_enabled<>1
BEGIN
  SELECT RAISE(ABORT, 'arming requires a committed Sprint merge grant');
END;

CREATE TABLE sprint_specs (
    sprint_id              INTEGER NOT NULL REFERENCES sprints(sprint_id),
    document_id            INTEGER NOT NULL REFERENCES documents(document_id),
    bound_revision_sha256  TEXT NOT NULL
                           CHECK (
                             length(bound_revision_sha256)=64
                             AND bound_revision_sha256 NOT GLOB '*[^0-9a-f]*'
                           ),
    approval_id            INTEGER NOT NULL
                           REFERENCES sprint_spec_approvals(approval_id),
    included_at            TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (sprint_id, document_id)
);

CREATE TABLE sprint_participants (
    participant_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id               INTEGER NOT NULL REFERENCES sprints(sprint_id),
    shell_id                INTEGER NOT NULL REFERENCES shells(shell_id),
    role                    TEXT NOT NULL
                            CHECK (role IN ('planner','developer','reviewer')),
    harness                 TEXT NOT NULL CHECK (trim(harness)<>''),
    model                   TEXT,
    effort                  TEXT,
    route                   TEXT,
    persistent_conversation_id TEXT REFERENCES conversations(conversation_id),
    current_conversation_id TEXT REFERENCES conversations(conversation_id),
    disposition             TEXT NOT NULL DEFAULT 'assigned'
                            CHECK (disposition IN
                              ('assigned','active','idle','declined','complete')),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (sprint_id, shell_id),
    UNIQUE (sprint_id, participant_id)
);
CREATE INDEX idx_sprint_participants_shell
    ON sprint_participants(shell_id, sprint_id);

CREATE TABLE sprint_work_units (
    work_unit_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id                INTEGER NOT NULL REFERENCES sprints(sprint_id),
    assigned_shell_id        INTEGER NOT NULL REFERENCES shells(shell_id),
    reviewer_shell_id        INTEGER NOT NULL REFERENCES shells(shell_id),
    title                    TEXT NOT NULL CHECK (trim(title)<>''),
    expected_output          TEXT NOT NULL CHECK (trim(expected_output)<>''),
    planned_wave             INTEGER NOT NULL DEFAULT 0
                             CHECK (planned_wave >= 0),
    disposition             TEXT NOT NULL DEFAULT 'planned'
                            CHECK (disposition IN
                              ('planned','ready','active','blocked','in_review',
                               'fixing','merge_ready','completed','cancelled')),
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at             TEXT,
    UNIQUE (sprint_id, work_unit_id)
);
CREATE INDEX idx_sprint_work_units_state
    ON sprint_work_units(sprint_id, disposition, planned_wave, work_unit_id);
CREATE UNIQUE INDEX idx_sprint_one_editing_lane
    ON sprint_work_units(sprint_id, assigned_shell_id)
    WHERE disposition IN ('active','in_review','fixing','merge_ready');

CREATE TABLE sprint_work_unit_tasks (
    sprint_id    INTEGER NOT NULL REFERENCES sprints(sprint_id),
    work_unit_id INTEGER NOT NULL,
    task_id      INTEGER NOT NULL REFERENCES spec_tasks(task_id),
    PRIMARY KEY (sprint_id, work_unit_id, task_id),
    UNIQUE (task_id),
    FOREIGN KEY (sprint_id, work_unit_id)
      REFERENCES sprint_work_units(sprint_id, work_unit_id)
);

CREATE TABLE sprint_work_unit_dependencies (
    sprint_id               INTEGER NOT NULL REFERENCES sprints(sprint_id),
    work_unit_id            INTEGER NOT NULL,
    depends_on_work_unit_id INTEGER NOT NULL,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (sprint_id, work_unit_id, depends_on_work_unit_id),
    CHECK (work_unit_id <> depends_on_work_unit_id),
    FOREIGN KEY (sprint_id, work_unit_id)
      REFERENCES sprint_work_units(sprint_id, work_unit_id),
    FOREIGN KEY (sprint_id, depends_on_work_unit_id)
      REFERENCES sprint_work_units(sprint_id, work_unit_id)
);
CREATE INDEX idx_sprint_work_unit_dependencies_upstream
    ON sprint_work_unit_dependencies(depends_on_work_unit_id, work_unit_id);

CREATE TABLE sprint_messages (
    message_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id               INTEGER NOT NULL REFERENCES sprints(sprint_id),
    from_participant_id     INTEGER REFERENCES sprint_participants(participant_id),
    to_participant_id       INTEGER NOT NULL
                            REFERENCES sprint_participants(participant_id),
    work_unit_id            INTEGER REFERENCES sprint_work_units(work_unit_id),
    message_kind            TEXT NOT NULL
                            CHECK (message_kind IN
                              ('work_assignment','review_request','notification',
                               'nudge','escalation','system')),
    body                    TEXT NOT NULL CHECK (length(body)>0),
    actionable              INTEGER NOT NULL DEFAULT 0
                            CHECK (actionable IN (0,1)),
    disposition             TEXT
                            CHECK (disposition IN
                              ('pending','accepted','declined')),
    read_at                 TEXT,
    decline_reason          TEXT,
    idempotency_key         TEXT NOT NULL UNIQUE
                            CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (
      (actionable=1 AND disposition IS NOT NULL)
      OR
      (actionable=0 AND disposition IS NULL)
    ),
    CHECK (
      disposition<>'declined' OR trim(COALESCE(decline_reason,''))<>''
    ),
    UNIQUE (sprint_id, message_id),
    FOREIGN KEY (sprint_id, from_participant_id)
      REFERENCES sprint_participants(sprint_id, participant_id),
    FOREIGN KEY (sprint_id, to_participant_id)
      REFERENCES sprint_participants(sprint_id, participant_id),
    FOREIGN KEY (sprint_id, work_unit_id)
      REFERENCES sprint_work_units(sprint_id, work_unit_id)
);
CREATE INDEX idx_sprint_messages_inbox
    ON sprint_messages(to_participant_id, read_at, message_id);

CREATE TABLE sprint_wake_outbox (
    wake_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id               INTEGER NOT NULL REFERENCES sprints(sprint_id),
    participant_id          INTEGER NOT NULL
                            REFERENCES sprint_participants(participant_id),
    state                   TEXT NOT NULL DEFAULT 'pending'
                            CHECK (state IN
                              ('pending','delivering','delivered','failed','cancelled')),
    attempt_count           INTEGER NOT NULL DEFAULT 0
                            CHECK (attempt_count BETWEEN 0 AND 3),
    idempotency_key         TEXT NOT NULL UNIQUE
                            CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    last_error              TEXT
                            CHECK (last_error IS NULL OR length(last_error)<=16384),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    available_at            TEXT NOT NULL DEFAULT (datetime('now')),
    delivered_at            TEXT,
    failed_at               TEXT,
    UNIQUE (sprint_id, wake_id),
    FOREIGN KEY (sprint_id, participant_id)
      REFERENCES sprint_participants(sprint_id, participant_id)
);
CREATE INDEX idx_sprint_wake_outbox_claim
    ON sprint_wake_outbox(state, available_at, wake_id);

CREATE TABLE sprint_wake_messages (
    sprint_id   INTEGER NOT NULL REFERENCES sprints(sprint_id),
    wake_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL UNIQUE,
    PRIMARY KEY (sprint_id, wake_id, message_id),
    FOREIGN KEY (sprint_id, wake_id)
      REFERENCES sprint_wake_outbox(sprint_id, wake_id),
    FOREIGN KEY (sprint_id, message_id)
      REFERENCES sprint_messages(sprint_id, message_id)
);

CREATE TABLE sprint_wake_attempts (
    attempt_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    wake_id           INTEGER NOT NULL REFERENCES sprint_wake_outbox(wake_id),
    attempt_number    INTEGER NOT NULL CHECK (attempt_number BETWEEN 1 AND 3),
    target_conversation_id TEXT REFERENCES conversations(conversation_id),
    native_run_ref    TEXT,
    outcome           TEXT NOT NULL
                      CHECK (outcome IN ('delivered','failed','coalesced')),
    error_detail      TEXT
                      CHECK (error_detail IS NULL OR length(error_detail)<=16384),
    attempted_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (wake_id, attempt_number)
);

CREATE TABLE sprint_registered_prs (
    registered_pr_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id        INTEGER NOT NULL REFERENCES sprints(sprint_id),
    owner_participant_id INTEGER NOT NULL
                        REFERENCES sprint_participants(participant_id),
    repository       TEXT NOT NULL CHECK (trim(repository)<>''),
    pr_number        INTEGER NOT NULL CHECK (pr_number>0),
    registered_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (repository, pr_number),
    UNIQUE (sprint_id, registered_pr_id),
    FOREIGN KEY (sprint_id, owner_participant_id)
      REFERENCES sprint_participants(sprint_id, participant_id)
);

CREATE TABLE sprint_pr_work_units (
    sprint_id        INTEGER NOT NULL REFERENCES sprints(sprint_id),
    registered_pr_id INTEGER NOT NULL,
    work_unit_id     INTEGER NOT NULL,
    PRIMARY KEY (sprint_id, registered_pr_id, work_unit_id),
    FOREIGN KEY (sprint_id, registered_pr_id)
      REFERENCES sprint_registered_prs(sprint_id, registered_pr_id),
    FOREIGN KEY (sprint_id, work_unit_id)
      REFERENCES sprint_work_units(sprint_id, work_unit_id)
);

CREATE TABLE sprint_pr_transitions (
    transition_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    registered_pr_id INTEGER NOT NULL
                     REFERENCES sprint_registered_prs(registered_pr_id),
    normalized_state TEXT NOT NULL
                     CHECK (normalized_state IN
                       ('created','pending','red','green','merged','closed')),
    transition_key   TEXT NOT NULL UNIQUE,
    observed_head_sha TEXT,
    evidence         TEXT NOT NULL DEFAULT '{}'
                     CHECK (json_valid(evidence) AND json_type(evidence)='object'),
    observed_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_sprint_pr_transitions_latest
    ON sprint_pr_transitions(registered_pr_id, transition_id DESC);

CREATE TABLE sprint_judgments (
    judgment_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id        INTEGER NOT NULL REFERENCES sprints(sprint_id),
    participant_id   INTEGER NOT NULL
                     REFERENCES sprint_participants(participant_id),
    work_unit_id     INTEGER REFERENCES sprint_work_units(work_unit_id),
    kind             TEXT NOT NULL
                     CHECK (kind IN ('ambiguity','decision','deviation','issue')),
    body             TEXT NOT NULL CHECK (trim(body)<>''),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (sprint_id, participant_id)
      REFERENCES sprint_participants(sprint_id, participant_id),
    FOREIGN KEY (sprint_id, work_unit_id)
      REFERENCES sprint_work_units(sprint_id, work_unit_id)
);

CREATE TABLE sprint_reports (
    report_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id        INTEGER NOT NULL REFERENCES sprints(sprint_id),
    report_kind      TEXT NOT NULL
                     CHECK (report_kind IN
                       ('pause','conformance','final','abort')),
    author_shell_id  INTEGER REFERENCES shells(shell_id),
    body             TEXT NOT NULL CHECK (trim(body)<>''),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE sprint_events (
    event_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id        INTEGER NOT NULL REFERENCES sprints(sprint_id),
    event_type       TEXT NOT NULL CHECK (trim(event_type)<>''),
    actor_kind       TEXT NOT NULL
                     CHECK (actor_kind IN
                       ('planner','fnb','participant','system')),
    actor_shell_id   INTEGER REFERENCES shells(shell_id),
    payload          TEXT NOT NULL DEFAULT '{}'
                     CHECK (json_valid(payload) AND json_type(payload)='object'),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_sprint_events_timeline
    ON sprint_events(sprint_id, event_id);

-- Evidence is append-only.  Current projections have their own authoritative
-- transition paths; history is never rewritten to make recovery look cleaner.
CREATE TRIGGER trg_sprint_wake_attempts_append_only_update
BEFORE UPDATE ON sprint_wake_attempts BEGIN
  SELECT RAISE(ABORT, 'Sprint wake attempts are append-only');
END;
CREATE TRIGGER trg_sprint_wake_attempts_append_only_delete
BEFORE DELETE ON sprint_wake_attempts BEGIN
  SELECT RAISE(ABORT, 'Sprint wake attempts are append-only');
END;
CREATE TRIGGER trg_sprint_pr_transitions_append_only_update
BEFORE UPDATE ON sprint_pr_transitions BEGIN
  SELECT RAISE(ABORT, 'Sprint PR transitions are append-only');
END;
CREATE TRIGGER trg_sprint_pr_transitions_append_only_delete
BEFORE DELETE ON sprint_pr_transitions BEGIN
  SELECT RAISE(ABORT, 'Sprint PR transitions are append-only');
END;
CREATE TRIGGER trg_sprint_events_append_only_update
BEFORE UPDATE ON sprint_events BEGIN
  SELECT RAISE(ABORT, 'Sprint events are append-only');
END;
CREATE TRIGGER trg_sprint_events_append_only_delete
BEFORE DELETE ON sprint_events BEGIN
  SELECT RAISE(ABORT, 'Sprint events are append-only');
END;
CREATE TRIGGER trg_sprint_judgments_append_only_update
BEFORE UPDATE ON sprint_judgments BEGIN
  SELECT RAISE(ABORT, 'Sprint judgments are append-only');
END;
CREATE TRIGGER trg_sprint_judgments_append_only_delete
BEFORE DELETE ON sprint_judgments BEGIN
  SELECT RAISE(ABORT, 'Sprint judgments are append-only');
END;
CREATE TRIGGER trg_sprint_reports_append_only_update
BEFORE UPDATE ON sprint_reports BEGIN
  SELECT RAISE(ABORT, 'Sprint reports are append-only');
END;
CREATE TRIGGER trg_sprint_reports_append_only_delete
BEFORE DELETE ON sprint_reports BEGIN
  SELECT RAISE(ABORT, 'Sprint reports are append-only');
END;
CREATE TRIGGER trg_sprint_messages_content_immutable
BEFORE UPDATE OF
    message_id, sprint_id, from_participant_id, to_participant_id,
    work_unit_id, message_kind, body, actionable, idempotency_key, created_at
ON sprint_messages
BEGIN
  SELECT RAISE(ABORT, 'Sprint message identity and content are immutable');
END;
CREATE TRIGGER trg_sprint_messages_no_delete
BEFORE DELETE ON sprint_messages BEGIN
  SELECT RAISE(ABORT, 'Sprint messages are durable facts');
END;

COMMIT;
