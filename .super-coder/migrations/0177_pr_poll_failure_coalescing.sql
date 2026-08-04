-- 0177 — coalesce consecutive identical PR subscription poll failures.
--
-- Failure identity remains immutable.  Only the consecutive-failure counters,
-- last-seen timestamp, and current backoff may advance; deletes stay forbidden.

BEGIN;

ALTER TABLE pr_subscription_poll_failures
ADD COLUMN repeat_count INTEGER NOT NULL DEFAULT 1 CHECK (repeat_count>0);

ALTER TABLE pr_subscription_poll_failures
ADD COLUMN last_seen_at TEXT;

DROP TRIGGER IF EXISTS trg_pr_subscription_poll_failures_append_only_update;

UPDATE pr_subscription_poll_failures
SET last_seen_at=failed_at
WHERE last_seen_at IS NULL;

CREATE TRIGGER trg_pr_subscription_poll_failures_coalesce_guard
BEFORE UPDATE ON pr_subscription_poll_failures
WHEN NOT (
    NEW.failure_id IS OLD.failure_id
    AND NEW.subscription_id IS OLD.subscription_id
    AND NEW.trigger IS OLD.trigger
    AND NEW.error_detail IS OLD.error_detail
    AND NEW.failed_at IS OLD.failed_at
    AND NEW.failure_count=OLD.failure_count+1
    AND NEW.backoff_seconds>=OLD.backoff_seconds
    AND NEW.repeat_count=OLD.repeat_count+1
    AND NEW.last_seen_at IS NOT NULL
    AND OLD.last_seen_at IS NOT NULL
    AND NEW.last_seen_at>=OLD.last_seen_at
)
BEGIN
  SELECT RAISE(ABORT, 'PR subscription poll failure update is not monotonic coalescing');
END;

COMMIT;
