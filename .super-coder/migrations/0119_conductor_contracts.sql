-- 0119 — Conductor Step 4 contracts.
--
-- New orchestration state is deliberately independent of the retired
-- Interface wake machine. Installed forks can contain live bindings, so this
-- migration drains them into closed historical rows and preserves one audit
-- record per binding instead of dropping evidence.

BEGIN;

CREATE TABLE directive_kinds (
    issuer_flavor TEXT NOT NULL
                  CHECK (issuer_flavor IN
                         ('dev','reviewer','planner','system')),
    kind          TEXT NOT NULL CHECK (trim(kind) <> ''),
    PRIMARY KEY (issuer_flavor, kind)
);

INSERT INTO directive_kinds (issuer_flavor, kind) VALUES
    ('dev',      'ready-for-review'),
    ('dev',      'ask-planner'),
    ('dev',      'merged'),
    ('dev',      'unit-report'),
    ('reviewer', 'review-clean'),
    ('reviewer', 'findings'),
    ('reviewer', 'ask-planner'),
    ('planner',  'kickoff'),
    ('planner',  'hold'),
    ('planner',  're-scope'),
    ('planner',  're-task'),
    ('planner',  'close'),
    ('planner',  'answer'),
    ('system',   'pr-green'),
    ('system',   'pr-red'),
    ('system',   'pr-merged'),
    ('system',   'stall'),
    ('system',   'dead-shell');

CREATE TABLE directives (
    directive_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_shell_id INTEGER REFERENCES shells(shell_id),
    issuer_flavor   TEXT NOT NULL,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}'
                    CHECK (json_valid(payload) AND json_type(payload)='object'),
    target          TEXT NOT NULL CHECK (trim(target) <> ''),
    sprint_doc_id   INTEGER REFERENCES documents(document_id),
    unit_id         INTEGER REFERENCES sprint_units(unit_id),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','executed','refused')),
    refusal_reason  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    executed_at     TEXT,
    FOREIGN KEY (issuer_flavor, kind)
        REFERENCES directive_kinds(issuer_flavor, kind),
    CHECK (
      (status='pending' AND executed_at IS NULL AND refusal_reason IS NULL)
      OR
      (status='executed' AND executed_at IS NOT NULL
       AND refusal_reason IS NULL)
      OR
      (status='refused' AND executed_at IS NOT NULL
       AND trim(COALESCE(refusal_reason,'')) <> '')
    )
);

CREATE TRIGGER trg_directive_issuer_insert
BEFORE INSERT ON directives
WHEN NOT (
    (NEW.issuer_flavor='system' AND NEW.issuer_shell_id IS NULL)
    OR
    (NEW.issuer_flavor<>'system' AND NEW.issuer_shell_id IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM shells s
       WHERE s.shell_id=NEW.issuer_shell_id
         AND s.flavor=NEW.issuer_flavor
         AND COALESCE(s.is_deleted,0)=0
     ))
)
BEGIN
  SELECT RAISE(ABORT, 'directive issuer does not match shell flavor');
END;

CREATE TRIGGER trg_directive_issuer_update
BEFORE UPDATE OF issuer_shell_id, issuer_flavor, kind ON directives
BEGIN
  SELECT RAISE(ABORT, 'directive issuer and kind are immutable');
END;

CREATE TRIGGER trg_directive_unit_insert
BEFORE INSERT ON directives
WHEN NEW.unit_id IS NOT NULL AND (
    NEW.sprint_doc_id IS NULL OR NOT EXISTS (
      SELECT 1 FROM sprint_units u
      WHERE u.unit_id=NEW.unit_id
        AND u.sprint_doc_id=NEW.sprint_doc_id
    )
)
BEGIN
  SELECT RAISE(ABORT, 'directive unit does not belong to sprint');
END;

CREATE TRIGGER trg_directive_link_update
BEFORE UPDATE OF sprint_doc_id, unit_id ON directives
WHEN NEW.unit_id IS NOT NULL AND (
    NEW.sprint_doc_id IS NULL OR NOT EXISTS (
      SELECT 1 FROM sprint_units u
      WHERE u.unit_id=NEW.unit_id
        AND u.sprint_doc_id=NEW.sprint_doc_id
    )
)
BEGIN
  SELECT RAISE(ABORT, 'directive unit does not belong to sprint');
END;

CREATE INDEX idx_directives_pending
    ON directives(status, created_at, directive_id);
CREATE INDEX idx_directives_sprint_unit
    ON directives(sprint_doc_id, unit_id, directive_id);

CREATE TABLE sentinel_events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind     TEXT NOT NULL CHECK (trim(event_kind) <> ''),
    shell_id       INTEGER REFERENCES shells(shell_id),
    sprint_doc_id  INTEGER REFERENCES documents(document_id),
    unit_id        INTEGER REFERENCES sprint_units(unit_id),
    directive_id   INTEGER REFERENCES directives(directive_id),
    evidence       TEXT NOT NULL DEFAULT '{}'
                   CHECK (json_valid(evidence) AND json_type(evidence)='object'),
    observed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TRIGGER trg_sentinel_events_append_only_update
BEFORE UPDATE ON sentinel_events
BEGIN
  SELECT RAISE(ABORT, 'sentinel_events is append-only');
END;

CREATE TRIGGER trg_sentinel_events_append_only_delete
BEFORE DELETE ON sentinel_events
BEGIN
  SELECT RAISE(ABORT, 'sentinel_events is append-only');
END;

CREATE TRIGGER trg_sentinel_events_unit_insert
BEFORE INSERT ON sentinel_events
WHEN NEW.unit_id IS NOT NULL AND (
    NEW.sprint_doc_id IS NULL OR NOT EXISTS (
      SELECT 1 FROM sprint_units u
      WHERE u.unit_id=NEW.unit_id
        AND u.sprint_doc_id=NEW.sprint_doc_id
    )
)
BEGIN
  SELECT RAISE(ABORT, 'sentinel event unit does not belong to sprint');
END;

CREATE INDEX idx_sentinel_events_observed
    ON sentinel_events(observed_at, event_id);
CREATE INDEX idx_sentinel_events_sprint_unit
    ON sentinel_events(sprint_doc_id, unit_id, event_id);

CREATE TABLE unit_expectations (
    unit_state        TEXT PRIMARY KEY
                      CHECK (unit_state IN
                             ('pending','working','in_review','blocked',
                              'merged','cancelled')),
    expected_signals  TEXT NOT NULL
                      CHECK (json_valid(expected_signals)
                             AND json_type(expected_signals)='array'),
    max_dwell_seconds INTEGER CHECK (max_dwell_seconds > 0),
    enabled           INTEGER NOT NULL DEFAULT 1
                      CHECK (enabled IN (0,1)),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (
      (enabled=1 AND max_dwell_seconds IS NOT NULL)
      OR
      (enabled=0 AND max_dwell_seconds IS NULL)
    )
);

INSERT INTO unit_expectations
    (unit_state, expected_signals, max_dwell_seconds, enabled)
VALUES
    ('pending',   '["kickoff"]',                             3600, 1),
    ('working',   '["activity","message","commit","pr"]',    7200, 1),
    ('in_review', '["activity","message","review"]',         3600, 1),
    ('blocked',   '["message","planner-directive"]',        14400, 1),
    ('merged',    '[]',                                      NULL, 0),
    ('cancelled', '[]',                                      NULL, 0);

CREATE TABLE wake_machine_retirements (
    retirement_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_id          INTEGER NOT NULL UNIQUE,
    sprint_doc_id       INTEGER NOT NULL,
    planner_shell_id    INTEGER NOT NULL,
    session_id          INTEGER NOT NULL,
    prior_released_at   TEXT,
    prior_release_reason TEXT,
    wake_batch_count    INTEGER NOT NULL,
    wake_item_count     INTEGER NOT NULL,
    retired_at          TEXT NOT NULL DEFAULT (datetime('now')),
    retirement_reason   TEXT NOT NULL DEFAULT 'conductor-step4-retired'
);

INSERT INTO wake_machine_retirements (
    binding_id, sprint_doc_id, planner_shell_id, session_id,
    prior_released_at, prior_release_reason, wake_batch_count, wake_item_count
)
SELECT b.binding_id, b.sprint_doc_id, b.planner_shell_id, b.session_id,
       b.released_at, b.release_reason,
       (SELECT COUNT(*) FROM planner_wake_batches wb
        WHERE wb.binding_id=b.binding_id),
       (SELECT COUNT(*) FROM planner_wake_items wi
        WHERE wi.binding_id=b.binding_id)
FROM sprint_planner_bindings b;

UPDATE planner_wake_items
SET state='cancelled',
    error=COALESCE(error, 'retired by Conductor Step 4'),
    updated_at=datetime('now')
WHERE state <> 'done' AND state <> 'cancelled';

UPDATE planner_wake_batches
SET state='delivery_unknown'
WHERE state='submitting';

UPDATE planner_wake_batches
SET state='complete',
    completed_at=COALESCE(completed_at, datetime('now'))
WHERE state <> 'complete';

UPDATE planner_action_receipts
SET state='unknown',
    result_detail=COALESCE(
      result_detail, 'retired by Conductor Step 4')
WHERE state='intent';

UPDATE planner_action_receipts
SET state='reconciled',
    result_detail=COALESCE(
      result_detail, 'retired by Conductor Step 4'),
    reconciled_at=COALESCE(reconciled_at, datetime('now'))
WHERE state='unknown';

UPDATE planner_alerts
SET resolved_at=COALESCE(resolved_at, datetime('now'));

UPDATE sprint_planner_bindings
SET released_at=datetime('now'),
    release_reason='conductor-step4-retired'
WHERE released_at IS NULL;

CREATE TRIGGER trg_wake_machine_retirements_append_only_update
BEFORE UPDATE ON wake_machine_retirements
BEGIN
  SELECT RAISE(ABORT, 'wake_machine_retirements is append-only');
END;

CREATE TRIGGER trg_wake_machine_retirements_append_only_delete
BEFORE DELETE ON wake_machine_retirements
BEGIN
  SELECT RAISE(ABORT, 'wake_machine_retirements is append-only');
END;

COMMIT;
