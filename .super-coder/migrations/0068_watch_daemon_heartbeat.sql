-- 0068 — daemon_heartbeats: watcher-daemon liveness the watch surface can see (#359).
--
-- The dos-arch incident: a host reboot killed the nohup'd watcher daemon while
-- the docker sandbox auto-restarted — the fork looked healthy, `./sc watch
-- list` kept reporting registered watches "live" (it reads only watched_prs),
-- and two PRs went green with nobody polling. Nothing distinguished "no
-- transition yet" from "nobody watching".
--
-- One row per daemon (name-keyed; only 'watch' today): the daemon UPSERTs
-- beat_at + its poll interval once per cycle — even idle ones — and the
-- /_sc/watches API turns the row into a live/stale/never verdict that
-- `./sc watch list` and `./sc watch pr` print. Stale = age > 3× interval.
--
-- Convergent with the baseline schema.sql CREATE (0059/watched_prs precedent):
-- IF NOT EXISTS carries an existing fork; a fresh build already has the table.

BEGIN;

CREATE TABLE IF NOT EXISTS daemon_heartbeats (
    name        TEXT PRIMARY KEY,              -- 'watch' — one row per daemon
    beat_at     TEXT    NOT NULL,              -- datetime('now') at last poll cycle
    interval_s  INTEGER NOT NULL               -- the daemon's configured poll interval
);

COMMIT;
