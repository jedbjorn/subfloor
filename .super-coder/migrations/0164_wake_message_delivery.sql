-- 0164 — general wake messages and delivery-time routing.
--
-- Sprint messages become the engine-wide wake_message literal.  Sprint and
-- participant columns remain as optional workflow context, while delivery and
-- coalescing are keyed directly to the receiving shell.

PRAGMA foreign_keys=OFF;

BEGIN;

DROP TRIGGER IF EXISTS trg_sprint_messages_acceptance_insert;
DROP TRIGGER IF EXISTS trg_sprint_messages_acceptance_update;
DROP TRIGGER IF EXISTS trg_sprint_liveness_acceptance;
DROP TRIGGER IF EXISTS trg_sprint_liveness_work_terminal;
DROP TRIGGER IF EXISTS trg_sprint_liveness_identity_immutable;
DROP TRIGGER IF EXISTS trg_sprint_liveness_no_delete;
DROP TRIGGER IF EXISTS trg_sprint_wake_delivery_state_insert;
DROP TRIGGER IF EXISTS trg_sprint_wake_delivery_state_update;
DROP INDEX IF EXISTS idx_sprint_wake_one_pending_participant;
DROP INDEX IF EXISTS idx_sprint_wake_outbox_claim;
DROP INDEX IF EXISTS idx_sprint_liveness_due;

ALTER TABLE sprint_liveness_expectations
  RENAME TO _wake_message_liveness_legacy;
ALTER TABLE sprint_wake_attempts RENAME TO _wake_attempts_legacy;
ALTER TABLE sprint_wake_messages RENAME TO _wake_messages_join_legacy;
ALTER TABLE sprint_wake_outbox RENAME TO _wake_outbox_legacy;
ALTER TABLE sprint_messages RENAME TO _wake_message_legacy;

CREATE TABLE wake_message (
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
                               'nudge','escalation','system')),
    body                    TEXT NOT NULL CHECK (length(body)>0),
    declared_type           TEXT NOT NULL
                            CHECK (declared_type IN ('new','re-enter')),
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
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
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
CREATE INDEX idx_wake_message_inbox
    ON wake_message(receiver_shell_id, read_at, message_id);
CREATE INDEX idx_wake_message_delivery
    ON wake_message(receiver_shell_id, delivered_at, message_id);

INSERT INTO wake_message (
    message_id,sprint_id,sender_shell_id,receiver_shell_id,
    from_participant_id,to_participant_id,work_unit_id,message_kind,body,
    declared_type,actionable,disposition,read_at,delivered_at,decline_reason,
    idempotency_key,created_at
)
SELECT
    message.message_id,message.sprint_id,sender.shell_id,receiver.shell_id,
    message.from_participant_id,message.to_participant_id,message.work_unit_id,
    message.message_kind,message.body,
    CASE
      WHEN message.message_kind IN ('work_assignment','review_request') THEN 'new'
      ELSE 're-enter'
    END,
    message.actionable,message.disposition,message.read_at,
    COALESCE(
      (
        SELECT wake.delivered_at
        FROM _wake_messages_join_legacy joined
        JOIN _wake_outbox_legacy wake ON wake.wake_id=joined.wake_id
        WHERE joined.message_id=message.message_id
          AND wake.state='delivered'
        LIMIT 1
      ),
      CASE WHEN message.read_at IS NOT NULL THEN message.read_at END
    ),
    message.decline_reason,message.idempotency_key,message.created_at
FROM _wake_message_legacy message
LEFT JOIN sprint_participants sender
  ON sender.participant_id=message.from_participant_id
JOIN sprint_participants receiver
  ON receiver.participant_id=message.to_participant_id;

CREATE TABLE sprint_wake_outbox (
    wake_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id               INTEGER REFERENCES sprints(sprint_id),
    participant_id          INTEGER REFERENCES sprint_participants(participant_id),
    receiver_shell_id       INTEGER NOT NULL REFERENCES shells(shell_id),
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
    claim_owner             TEXT,
    claimed_at              TEXT,
    lease_expires_at        TEXT,
    UNIQUE (sprint_id, wake_id),
    FOREIGN KEY (sprint_id, participant_id)
      REFERENCES sprint_participants(sprint_id, participant_id),
    CHECK (
      (sprint_id IS NULL AND participant_id IS NULL)
      OR
      (sprint_id IS NOT NULL AND participant_id IS NOT NULL)
    )
);
CREATE INDEX idx_sprint_wake_outbox_claim
    ON sprint_wake_outbox(state, available_at, wake_id);
-- The historical index name is intentionally stable for update compatibility;
-- the generalized uniqueness key is now the receiver shell.
CREATE UNIQUE INDEX idx_sprint_wake_one_pending_participant
    ON sprint_wake_outbox(receiver_shell_id)
    WHERE state='pending';

INSERT INTO sprint_wake_outbox (
    wake_id,sprint_id,participant_id,receiver_shell_id,state,attempt_count,
    idempotency_key,last_error,created_at,available_at,delivered_at,failed_at,
    claim_owner,claimed_at,lease_expires_at
)
SELECT
    wake.wake_id,wake.sprint_id,wake.participant_id,participant.shell_id,
    wake.state,wake.attempt_count,wake.idempotency_key,wake.last_error,
    wake.created_at,wake.available_at,wake.delivered_at,wake.failed_at,
    wake.claim_owner,wake.claimed_at,wake.lease_expires_at
FROM _wake_outbox_legacy wake
JOIN sprint_participants participant
  ON participant.participant_id=wake.participant_id;

CREATE TABLE sprint_wake_messages (
    sprint_id   INTEGER REFERENCES sprints(sprint_id),
    wake_id     INTEGER NOT NULL REFERENCES sprint_wake_outbox(wake_id),
    message_id  INTEGER NOT NULL UNIQUE REFERENCES wake_message(message_id),
    PRIMARY KEY (wake_id, message_id)
);
INSERT INTO sprint_wake_messages (sprint_id,wake_id,message_id)
SELECT sprint_id,wake_id,message_id FROM _wake_messages_join_legacy;

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
INSERT INTO sprint_wake_attempts (
    attempt_id,wake_id,attempt_number,target_conversation_id,native_run_ref,
    outcome,error_detail,attempted_at
)
SELECT
    attempt_id,wake_id,attempt_number,target_conversation_id,native_run_ref,
    outcome,error_detail,attempted_at
FROM _wake_attempts_legacy;

CREATE TABLE sprint_liveness_expectations (
    message_id             INTEGER PRIMARY KEY REFERENCES wake_message(message_id),
    sprint_id              INTEGER NOT NULL REFERENCES sprints(sprint_id),
    participant_id         INTEGER NOT NULL REFERENCES sprint_participants(participant_id),
    accepted_at            TEXT NOT NULL,
    last_strong_at         TEXT NOT NULL,
    last_strong_key        TEXT NOT NULL CHECK (trim(last_strong_key)<>''),
    silence_episode        INTEGER NOT NULL DEFAULT 1 CHECK (silence_episode > 0),
    nudge_at               TEXT,
    escalated_at           TEXT,
    last_evaluated_at      TEXT,
    next_evaluation_at     TEXT,
    last_supporting        TEXT NOT NULL DEFAULT '[]'
                           CHECK (json_valid(last_supporting)
                                  AND json_type(last_supporting)='array'),
    last_unreadable        TEXT NOT NULL DEFAULT '[]'
                           CHECK (json_valid(last_unreadable)
                                  AND json_type(last_unreadable)='array'),
    last_failure_key       TEXT,
    resolved_at            TEXT,
    resolution             TEXT,
    UNIQUE (sprint_id, message_id),
    FOREIGN KEY (sprint_id, participant_id)
      REFERENCES sprint_participants(sprint_id, participant_id),
    CHECK (
      (resolved_at IS NULL AND resolution IS NULL)
      OR
      (resolved_at IS NOT NULL AND trim(COALESCE(resolution,''))<>'')
    )
);
INSERT INTO sprint_liveness_expectations
SELECT * FROM _wake_message_liveness_legacy;
CREATE INDEX idx_sprint_liveness_due
    ON sprint_liveness_expectations(
      sprint_id, resolved_at, next_evaluation_at, message_id
    );

DROP TABLE _wake_message_liveness_legacy;
DROP TABLE _wake_attempts_legacy;
DROP TABLE _wake_messages_join_legacy;
DROP TABLE _wake_outbox_legacy;
DROP TABLE _wake_message_legacy;

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

CREATE TRIGGER trg_sprint_wake_delivery_state_insert
BEFORE INSERT ON sprint_wake_outbox
WHEN NOT (
    (NEW.state='pending' AND NEW.claim_owner IS NULL
     AND NEW.claimed_at IS NULL AND NEW.lease_expires_at IS NULL
     AND NEW.delivered_at IS NULL AND NEW.failed_at IS NULL)
    OR
    (NEW.state='delivering' AND trim(COALESCE(NEW.claim_owner,''))<>''
     AND NEW.claimed_at IS NOT NULL AND NEW.lease_expires_at IS NOT NULL
     AND NEW.delivered_at IS NULL AND NEW.failed_at IS NULL)
    OR
    (NEW.state='delivered' AND NEW.delivered_at IS NOT NULL
     AND NEW.failed_at IS NULL)
    OR
    (NEW.state='failed' AND NEW.failed_at IS NOT NULL)
    OR
    (NEW.state='cancelled' AND NEW.delivered_at IS NULL)
)
BEGIN
  SELECT RAISE(ABORT, 'invalid Sprint wake delivery state');
END;

CREATE TRIGGER trg_sprint_wake_delivery_state_update
BEFORE UPDATE OF
    state, claim_owner, claimed_at, lease_expires_at, delivered_at, failed_at
ON sprint_wake_outbox
WHEN NOT (
    (NEW.state='pending' AND NEW.claim_owner IS NULL
     AND NEW.claimed_at IS NULL AND NEW.lease_expires_at IS NULL
     AND NEW.delivered_at IS NULL AND NEW.failed_at IS NULL)
    OR
    (NEW.state='delivering' AND trim(COALESCE(NEW.claim_owner,''))<>''
     AND NEW.claimed_at IS NOT NULL AND NEW.lease_expires_at IS NOT NULL
     AND NEW.delivered_at IS NULL AND NEW.failed_at IS NULL)
    OR
    (NEW.state='delivered' AND NEW.delivered_at IS NOT NULL
     AND NEW.failed_at IS NULL)
    OR
    (NEW.state='failed' AND NEW.failed_at IS NOT NULL)
    OR
    (NEW.state='cancelled' AND NEW.delivered_at IS NULL)
)
BEGIN
  SELECT RAISE(ABORT, 'invalid Sprint wake delivery state');
END;

CREATE TRIGGER trg_sprint_liveness_acceptance
AFTER UPDATE OF disposition,read_at ON wake_message
WHEN NEW.sprint_id IS NOT NULL
  AND NEW.actionable=1
  AND NEW.disposition='accepted'
  AND NEW.read_at IS NOT NULL
  AND (OLD.disposition<>'accepted' OR OLD.read_at IS NULL)
BEGIN
  INSERT INTO sprint_liveness_expectations (
      message_id,sprint_id,participant_id,accepted_at,
      last_strong_at,last_strong_key,next_evaluation_at
  ) VALUES (
      NEW.message_id,NEW.sprint_id,NEW.to_participant_id,NEW.read_at,
      NEW.read_at,'message.accepted:' || NEW.message_id,
      datetime(NEW.read_at,'+5 minutes')
  );
END;

CREATE TRIGGER trg_sprint_liveness_work_terminal
AFTER UPDATE OF disposition ON sprint_work_units
WHEN NEW.disposition IN ('in_review','completed','cancelled')
  AND OLD.disposition <> NEW.disposition
BEGIN
  UPDATE sprint_liveness_expectations
  SET resolved_at=datetime('now'),
      resolution='work_unit.' || NEW.disposition,
      next_evaluation_at=NULL
  WHERE resolved_at IS NULL
    AND message_id IN (
      SELECT message.message_id
      FROM wake_message message
      JOIN sprint_participants participant
        ON participant.participant_id=message.to_participant_id
      WHERE message.work_unit_id=NEW.work_unit_id
        AND (
          message.message_kind='work_assignment'
          OR (
            message.message_kind='notification'
            AND participant.role='developer'
            AND participant.shell_id=NEW.assigned_shell_id
          )
        )
    );
END;

CREATE TRIGGER trg_sprint_liveness_identity_immutable
BEFORE UPDATE OF message_id,sprint_id,participant_id,accepted_at
ON sprint_liveness_expectations
BEGIN
  SELECT RAISE(ABORT, 'Sprint liveness expectation identity is immutable');
END;

CREATE TRIGGER trg_sprint_liveness_no_delete
BEFORE DELETE ON sprint_liveness_expectations
BEGIN
  SELECT RAISE(ABORT, 'Sprint liveness history is durable');
END;

COMMIT;

PRAGMA foreign_keys=ON;
