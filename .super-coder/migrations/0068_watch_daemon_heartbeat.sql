-- 0068 — generic supervised-daemon heartbeats.
--
-- One row per supervised daemon. A daemon UPSERTs beat_at and its interval once
-- per cycle so callers can distinguish live, stale, and never-started state.
-- IF NOT EXISTS carries existing forks and converges with schema.sql.

BEGIN;

CREATE TABLE IF NOT EXISTS daemon_heartbeats (
    name        TEXT PRIMARY KEY,
    beat_at     TEXT    NOT NULL,              -- datetime('now') at last poll cycle
    interval_s  INTEGER NOT NULL               -- the daemon's configured poll interval
);

COMMIT;
