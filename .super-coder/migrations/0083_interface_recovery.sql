-- 0083 — Unified stranded-shell recovery (sprint 31 unit 8, spec #30 req 24
-- / task #95; absorbs roadmap #22 and flag #38).
--
-- Recovery is preview-then-execute: the preview stores one observation row
-- holding the server-derived classification, the full evidence payload, and
-- a fingerprint of the durable state that justified them. Execution must
-- name the observation; any change to the fingerprinted state (pane exit,
-- PID reuse, a concurrent recovery, a new generation) mismatches the
-- fingerprint and the request refuses with 409 recovery_observation_stale —
-- the client previews again. Rows are short-lived (TTL enforced in app
-- code); the table is an audit trail of what was observed and acted on.

CREATE TABLE IF NOT EXISTS interface_recovery_observations (
    observation_id  TEXT PRIMARY KEY,        -- opaque (uuid4 hex)
    shell_id        INTEGER NOT NULL REFERENCES shells(shell_id),
    classification  TEXT NOT NULL
                    CHECK (classification IN
                        ('available','stale_durable_lock','exact_idle_orphan',
                         'verified_live','indeterminate')),
    legal_actions   TEXT NOT NULL,           -- JSON array of action names
    evidence        TEXT NOT NULL,           -- JSON payload shown to clients
    fingerprint     TEXT NOT NULL,           -- sha256 of the durable state
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,
    acted_at        TEXT                     -- set by a successful execution
);
CREATE INDEX IF NOT EXISTS idx_recovery_obs_shell
    ON interface_recovery_observations(shell_id, created_at);
