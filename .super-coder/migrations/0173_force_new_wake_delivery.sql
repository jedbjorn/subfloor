-- 0173 — widen wake delivery for deterministic force-new bundles.
--
-- Preserve the generalized 0164 wake schema while accepting the third declared
-- type.  Quiet observation is receiver-wake state, separate from available_at,
-- which remains the retry/recovery scheduling boundary.
--
-- migrate: foreign-keys-off
-- (The bare PRAGMA below is a no-op inside the runner's transaction; the
-- marker is what actually disables enforcement. Without it the DROP TABLE
-- wake_message fails on any install whose sprint_wake_messages /
-- sprint_liveness_expectations tables are non-empty.)

PRAGMA foreign_keys=OFF;

BEGIN;

DROP TRIGGER IF EXISTS trg_wake_message_acceptance_insert;
DROP TRIGGER IF EXISTS trg_wake_message_acceptance_update;
DROP TRIGGER IF EXISTS trg_sprint_liveness_acceptance;
DROP TRIGGER IF EXISTS trg_sprint_liveness_work_terminal;

CREATE TABLE _wake_message_force_new (
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

INSERT INTO _wake_message_force_new (
    message_id,sprint_id,sender_shell_id,receiver_shell_id,
    from_participant_id,to_participant_id,work_unit_id,message_kind,body,
    declared_type,actionable,disposition,read_at,delivered_at,decline_reason,
    idempotency_key,created_at
)
SELECT
    message_id,sprint_id,sender_shell_id,receiver_shell_id,
    from_participant_id,to_participant_id,work_unit_id,message_kind,body,
    declared_type,actionable,disposition,read_at,delivered_at,decline_reason,
    idempotency_key,created_at
FROM wake_message;

DROP TABLE wake_message;
ALTER TABLE _wake_message_force_new RENAME TO wake_message;

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

ALTER TABLE sprint_wake_outbox ADD COLUMN quiet_since TEXT;

DROP TRIGGER IF EXISTS trg_sprint_wake_delivery_state_insert;
DROP TRIGGER IF EXISTS trg_sprint_wake_delivery_state_update;

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
     AND NEW.failed_at IS NULL AND NEW.quiet_since IS NULL)
    OR
    (NEW.state='failed' AND NEW.failed_at IS NOT NULL
     AND NEW.quiet_since IS NULL)
    OR
    (NEW.state='cancelled' AND NEW.delivered_at IS NULL
     AND NEW.quiet_since IS NULL)
)
BEGIN
  SELECT RAISE(ABORT, 'invalid Sprint wake delivery state');
END;

CREATE TRIGGER trg_sprint_wake_delivery_state_update
BEFORE UPDATE OF
    state, claim_owner, claimed_at, lease_expires_at, delivered_at, failed_at,
    quiet_since
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
     AND NEW.failed_at IS NULL AND NEW.quiet_since IS NULL)
    OR
    (NEW.state='failed' AND NEW.failed_at IS NOT NULL
     AND NEW.quiet_since IS NULL)
    OR
    (NEW.state='cancelled' AND NEW.delivered_at IS NULL
     AND NEW.quiet_since IS NULL)
)
BEGIN
  SELECT RAISE(ABORT, 'invalid Sprint wake delivery state');
END;

COMMIT;

PRAGMA foreign_key_check;
PRAGMA foreign_keys=ON;
