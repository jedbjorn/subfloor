-- 0148 — Sprint v2 message acceptance and wake-delivery backstops.
--
-- Migration 0146 created the durable domain.  This delta adds the claim lease
-- needed for at-least-once external delivery and database guards for the
-- read-equals-acceptance contract.  Remote harness work remains outside the
-- transaction that creates a Sprint message and its wake intent.

BEGIN;

ALTER TABLE sprint_wake_outbox ADD COLUMN claim_owner TEXT;
ALTER TABLE sprint_wake_outbox ADD COLUMN claimed_at TEXT;
ALTER TABLE sprint_wake_outbox ADD COLUMN lease_expires_at TEXT;

-- One pending follow-up wake absorbs every active message that arrives while a
-- participant already has queued or running work.  A currently delivering wake
-- is distinct: messages arriving behind it coalesce into the next pending row.
CREATE UNIQUE INDEX idx_sprint_wake_one_pending_participant
    ON sprint_wake_outbox(sprint_id, participant_id)
    WHERE state='pending';

CREATE TRIGGER trg_sprint_messages_acceptance_insert
BEFORE INSERT ON sprint_messages
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
  SELECT RAISE(ABORT, 'invalid Sprint message acceptance state');
END;

CREATE TRIGGER trg_sprint_messages_acceptance_update
BEFORE UPDATE OF actionable, disposition, read_at, decline_reason
ON sprint_messages
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
  SELECT RAISE(ABORT, 'invalid Sprint message acceptance state');
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

COMMIT;
