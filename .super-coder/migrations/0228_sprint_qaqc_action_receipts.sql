-- 0228 — bind pre-arm QA/QC approval writes to the authenticated Reviewer run.
--
-- The existing append-only Sprint event ledger owns the receipt.  Its event_id
-- is the one monotonic receipt identity and created_at is the server timestamp;
-- the payload contains only bounded identifiers derived by the server.

BEGIN;

CREATE UNIQUE INDEX idx_sprint_qaqc_action_receipt_approval
    ON sprint_events(CAST(json_extract(payload,'$.approval_id') AS INTEGER))
    WHERE event_type='qaqc.action_recorded';

CREATE TRIGGER trg_sprint_qaqc_action_receipt_shape
BEFORE INSERT ON sprint_events
WHEN NEW.event_type='qaqc.action_recorded' AND NOT (
  NEW.actor_kind='participant'
  AND NEW.actor_shell_id IS NOT NULL
  AND (SELECT COUNT(*) FROM json_each(NEW.payload))=15
  AND NOT EXISTS (
    SELECT 1 FROM json_each(NEW.payload)
    WHERE key NOT IN (
      'action_kind','sprint_id','participant_id','reviewer_shell_id','role',
      'assignment_generation','conversation_id','session_id','run_id',
      'candidate_sha','document_id','revision_sha256','review_phase',
      'approval_id','approval_created'
    )
  )
  AND json_extract(NEW.payload,'$.action_kind')='record-qaqc'
  AND json_type(NEW.payload,'$.sprint_id')='integer'
  AND json_extract(NEW.payload,'$.sprint_id')=NEW.sprint_id
  AND json_type(NEW.payload,'$.participant_id')='integer'
  AND json_extract(NEW.payload,'$.participant_id')>0
  AND json_type(NEW.payload,'$.reviewer_shell_id')='integer'
  AND json_extract(NEW.payload,'$.reviewer_shell_id')=NEW.actor_shell_id
  AND json_extract(NEW.payload,'$.role')='reviewer'
  AND json_type(NEW.payload,'$.assignment_generation')='text'
  AND length(json_extract(NEW.payload,'$.assignment_generation'))=32
  AND json_extract(NEW.payload,'$.assignment_generation')
      NOT GLOB '*[^0-9a-f]*'
  AND json_type(NEW.payload,'$.conversation_id')='text'
  AND trim(json_extract(NEW.payload,'$.conversation_id'))<>''
  AND json_type(NEW.payload,'$.session_id')='text'
  AND trim(json_extract(NEW.payload,'$.session_id'))<>''
  AND json_type(NEW.payload,'$.run_id')='integer'
  AND json_extract(NEW.payload,'$.run_id')>0
  AND json_type(NEW.payload,'$.candidate_sha')='text'
  AND length(json_extract(NEW.payload,'$.candidate_sha')) IN (40,64)
  AND json_extract(NEW.payload,'$.candidate_sha') NOT GLOB '*[^0-9a-f]*'
  AND json_type(NEW.payload,'$.document_id')='integer'
  AND json_extract(NEW.payload,'$.document_id')>0
  AND json_type(NEW.payload,'$.revision_sha256')='text'
  AND length(json_extract(NEW.payload,'$.revision_sha256'))=64
  AND json_extract(NEW.payload,'$.revision_sha256') NOT GLOB '*[^0-9a-f]*'
  AND json_extract(NEW.payload,'$.review_phase')='pre-arm-qaqc'
  AND json_type(NEW.payload,'$.approval_id')='integer'
  AND json_extract(NEW.payload,'$.approval_id')>0
  AND json_type(NEW.payload,'$.approval_created') IN ('true','false')
)
BEGIN
  SELECT RAISE(ABORT, 'invalid Sprint QAQC action receipt');
END;

COMMIT;
