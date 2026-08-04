-- 0165 — engine-wide PR subscriptions.
--
-- PR observation is owned by a shell, not by a Sprint lifecycle.  The legacy
-- Sprint registration and transition tables remain as compatibility
-- projections for merge authorization, close reports, and snapshot rebuilds.

BEGIN;

CREATE TABLE IF NOT EXISTS pr_subscriptions (
    subscription_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_shell_id        INTEGER NOT NULL REFERENCES shells(shell_id),
    repository            TEXT NOT NULL CHECK (trim(repository)<>''),
    pr_number             INTEGER NOT NULL CHECK (pr_number>0),
    sprint_registered_pr_id INTEGER UNIQUE
                          REFERENCES sprint_registered_prs(registered_pr_id),
    registered_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (repository, pr_number)
);

INSERT OR IGNORE INTO pr_subscriptions (
    owner_shell_id,
    repository,
    pr_number,
    sprint_registered_pr_id
)
SELECT participant.shell_id,
       registered.repository,
       registered.pr_number,
       registered.registered_pr_id
FROM sprint_registered_prs registered
JOIN sprint_participants participant
  ON participant.participant_id=registered.owner_participant_id;

CREATE TABLE IF NOT EXISTS pr_subscription_transitions (
    transition_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id    INTEGER NOT NULL
                       REFERENCES pr_subscriptions(subscription_id),
    normalized_state   TEXT NOT NULL
                       CHECK (normalized_state IN
                         ('created','pending','red','green','merged','closed')),
    transition_key     TEXT NOT NULL UNIQUE,
    observed_head_sha  TEXT,
    evidence           TEXT NOT NULL DEFAULT '{}'
                       CHECK (json_valid(evidence)
                              AND json_type(evidence)='object'),
    observed_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pr_subscription_transitions_latest
    ON pr_subscription_transitions(subscription_id, transition_id DESC);

INSERT OR IGNORE INTO pr_subscription_transitions (
    subscription_id,
    normalized_state,
    transition_key,
    observed_head_sha,
    evidence,
    observed_at
)
SELECT subscription.subscription_id,
       transition.normalized_state,
       transition.transition_key,
       transition.observed_head_sha,
       transition.evidence,
       transition.observed_at
FROM sprint_pr_transitions transition
JOIN pr_subscriptions subscription
  ON subscription.sprint_registered_pr_id=transition.registered_pr_id;

CREATE TABLE IF NOT EXISTS pr_subscription_poll_failures (
    failure_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL
                    REFERENCES pr_subscriptions(subscription_id),
    failure_count    INTEGER NOT NULL CHECK (failure_count>0),
    backoff_seconds  REAL NOT NULL CHECK (backoff_seconds>0),
    trigger          TEXT NOT NULL CHECK (trim(trigger)<>''),
    error_detail     TEXT NOT NULL CHECK (trim(error_detail)<>''),
    failed_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TRIGGER IF NOT EXISTS trg_pr_subscription_transitions_append_only_update
BEFORE UPDATE ON pr_subscription_transitions BEGIN
  SELECT RAISE(ABORT, 'PR subscription transitions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_pr_subscription_transitions_append_only_delete
BEFORE DELETE ON pr_subscription_transitions BEGIN
  SELECT RAISE(ABORT, 'PR subscription transitions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_pr_subscription_poll_failures_append_only_update
BEFORE UPDATE ON pr_subscription_poll_failures BEGIN
  SELECT RAISE(ABORT, 'PR subscription poll failures are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_pr_subscription_poll_failures_append_only_delete
BEFORE DELETE ON pr_subscription_poll_failures BEGIN
  SELECT RAISE(ABORT, 'PR subscription poll failures are append-only');
END;

COMMIT;
