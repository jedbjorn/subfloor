-- 0205 — singular, recoverable whole-Sprint conformance ownership.

BEGIN;

ALTER TABLE sprints
  ADD COLUMN conformance_reviewer_shell_id INTEGER REFERENCES shells(shell_id);

ALTER TABLE sprints
  ADD COLUMN conformance_owner_generation INTEGER NOT NULL DEFAULT 0;

-- Historical rows are safe to backfill only when the choice is unambiguous.
UPDATE sprints
SET conformance_reviewer_shell_id=(
      SELECT MIN(participant.shell_id)
      FROM sprint_participants participant
      JOIN shells shell ON shell.shell_id=participant.shell_id
      WHERE participant.sprint_id=sprints.sprint_id
        AND participant.role='reviewer'
        AND participant.disposition<>'declined'
        AND COALESCE(shell.is_deleted,0)=0
    ),
    conformance_owner_generation=1
WHERE (
  SELECT COUNT(*)
  FROM sprint_participants participant
  JOIN shells shell ON shell.shell_id=participant.shell_id
  WHERE participant.sprint_id=sprints.sprint_id
    AND participant.role='reviewer'
    AND participant.disposition<>'declined'
    AND COALESCE(shell.is_deleted,0)=0
)=1;

CREATE TRIGGER trg_sprints_conformance_owner_valid
BEFORE UPDATE OF conformance_reviewer_shell_id ON sprints
WHEN NEW.conformance_reviewer_shell_id IS NOT NULL
 AND NOT EXISTS (
   SELECT 1
   FROM sprint_participants participant
   JOIN shells shell ON shell.shell_id=participant.shell_id
   WHERE participant.sprint_id=NEW.sprint_id
     AND participant.shell_id=NEW.conformance_reviewer_shell_id
     AND participant.role='reviewer'
     AND participant.disposition<>'declined'
     AND COALESCE(shell.is_deleted,0)=0
 )
BEGIN
  SELECT RAISE(ABORT, 'Sprint conformance owner must be an active Reviewer participant');
END;

CREATE TRIGGER trg_sprints_conformance_owner_generation
BEFORE UPDATE OF conformance_reviewer_shell_id,conformance_owner_generation
ON sprints
WHEN NEW.conformance_owner_generation<0
  OR (
    NEW.conformance_reviewer_shell_id IS OLD.conformance_reviewer_shell_id
    AND NEW.conformance_owner_generation<>OLD.conformance_owner_generation
  )
  OR (
    NEW.conformance_reviewer_shell_id IS NOT OLD.conformance_reviewer_shell_id
    AND NEW.conformance_owner_generation<>OLD.conformance_owner_generation+1
  )
BEGIN
  SELECT RAISE(ABORT, 'Sprint conformance owner generation is invalid');
END;

CREATE TRIGGER trg_sprints_conformance_owner_reassignment_paused
BEFORE UPDATE OF conformance_reviewer_shell_id ON sprints
WHEN NEW.conformance_reviewer_shell_id IS NOT OLD.conformance_reviewer_shell_id
 AND OLD.lifecycle NOT IN ('prepared','paused')
BEGIN
  SELECT RAISE(ABORT, 'Sprint conformance owner may be reassigned only while paused');
END;

CREATE TRIGGER trg_sprints_arming_requires_conformance_owner
BEFORE UPDATE OF lifecycle ON sprints
WHEN NEW.lifecycle='armed' AND NEW.conformance_reviewer_shell_id IS NULL
BEGIN
  SELECT RAISE(ABORT, 'arming requires a Sprint conformance owner');
END;

COMMIT;
