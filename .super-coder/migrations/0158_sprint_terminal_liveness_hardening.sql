-- 0158 — Hand Developer liveness to Review when work enters in_review.
--
-- Migration 0149 resolved accepted assignments only at terminal work-unit
-- dispositions.  A successful review handoff now ends the Developer lane, so
-- resolve its accepted assignment (and any accepted changes-requested wake)
-- while leaving the Reviewer's independent request expectation active.

BEGIN;

DROP TRIGGER IF EXISTS trg_sprint_liveness_work_terminal;

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
      FROM sprint_messages message
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

-- Converge assignments that reached Review before this migration landed.
UPDATE sprint_liveness_expectations
SET resolved_at=datetime('now'),
    resolution='work_unit.in_review',
    next_evaluation_at=NULL
WHERE resolved_at IS NULL
  AND message_id IN (
    SELECT message.message_id
    FROM sprint_messages message
    JOIN sprint_work_units unit
      ON unit.work_unit_id=message.work_unit_id
     AND unit.sprint_id=message.sprint_id
    WHERE message.message_kind='work_assignment'
      AND unit.disposition='in_review'
  );

COMMIT;
