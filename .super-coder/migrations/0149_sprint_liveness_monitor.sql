-- 0149 — durable Sprint liveness silence episodes.
--
-- Every accepted actionable Sprint message is a work expectation, including
-- review requests (which deliberately are not editing lanes).  The projection
-- below makes the ten-minute grace, one nudge, and one Planner escalation
-- restart-safe without deriving episode identity from message text.

BEGIN;

CREATE TABLE sprint_liveness_expectations (
    message_id             INTEGER PRIMARY KEY
                           REFERENCES sprint_messages(message_id),
    sprint_id              INTEGER NOT NULL REFERENCES sprints(sprint_id),
    participant_id         INTEGER NOT NULL
                           REFERENCES sprint_participants(participant_id),
    accepted_at            TEXT NOT NULL,
    last_strong_at         TEXT NOT NULL,
    last_strong_key        TEXT NOT NULL CHECK (trim(last_strong_key)<>''),
    silence_episode        INTEGER NOT NULL DEFAULT 1
                           CHECK (silence_episode > 0),
    nudge_at               TEXT,
    escalated_at           TEXT,
    last_evaluated_at      TEXT,
    next_evaluation_at     TEXT,
    last_supporting        TEXT NOT NULL DEFAULT '[]'
                           CHECK (
                             json_valid(last_supporting)
                             AND json_type(last_supporting)='array'
                           ),
    last_unreadable        TEXT NOT NULL DEFAULT '[]'
                           CHECK (
                             json_valid(last_unreadable)
                             AND json_type(last_unreadable)='array'
                           ),
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

CREATE INDEX idx_sprint_liveness_due
    ON sprint_liveness_expectations(
      sprint_id, resolved_at, next_evaluation_at, message_id
    );

-- Downstream updates may already have an armed Sprint.  Start its accepted
-- expectations from their real acceptance timestamp; the insert is naturally
-- idempotent because message_id is the projection identity.
INSERT INTO sprint_liveness_expectations (
    message_id,sprint_id,participant_id,accepted_at,
    last_strong_at,last_strong_key,next_evaluation_at
)
SELECT
    m.message_id,m.sprint_id,m.to_participant_id,m.read_at,
    m.read_at,'message.accepted:' || m.message_id,
    datetime(m.read_at,'+5 minutes')
FROM sprint_messages m
JOIN sprints s ON s.sprint_id=m.sprint_id
LEFT JOIN sprint_work_units unit
  ON unit.sprint_id=m.sprint_id AND unit.work_unit_id=m.work_unit_id
WHERE m.actionable=1
  AND m.disposition='accepted'
  AND m.read_at IS NOT NULL
  AND s.lifecycle='armed'
  AND (m.work_unit_id IS NULL OR unit.disposition NOT IN ('completed','cancelled'));

CREATE TRIGGER trg_sprint_liveness_acceptance
AFTER UPDATE OF disposition,read_at ON sprint_messages
WHEN NEW.actionable=1
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

-- Work assignments have an authoritative terminal projection.  Review-request
-- completion is owned by the Stage-6 review outcome path and calls the monitor's
-- explicit resolve seam; reviews remain monitored even though they own no lane.
CREATE TRIGGER trg_sprint_liveness_work_terminal
AFTER UPDATE OF disposition ON sprint_work_units
WHEN NEW.disposition IN ('completed','cancelled')
  AND OLD.disposition NOT IN ('completed','cancelled')
BEGIN
  UPDATE sprint_liveness_expectations
  SET resolved_at=datetime('now'),
      resolution='work_unit.' || NEW.disposition,
      next_evaluation_at=NULL
  WHERE resolved_at IS NULL
    AND message_id IN (
      SELECT message_id FROM sprint_messages
      WHERE work_unit_id=NEW.work_unit_id
        AND message_kind='work_assignment'
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
