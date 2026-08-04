-- 0175 — bounded history for supervised-daemon heartbeat continuity.

BEGIN;

CREATE TABLE daemon_heartbeat_history (
    heartbeat_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT    NOT NULL,
    beat_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    subscriptions_scanned INTEGER NOT NULL CHECK (subscriptions_scanned >= 0)
);

CREATE INDEX idx_daemon_heartbeat_history_name_newest
    ON daemon_heartbeat_history(name, heartbeat_id DESC);

CREATE TRIGGER trg_daemon_heartbeat_history_cap
AFTER INSERT ON daemon_heartbeat_history
BEGIN
    DELETE FROM daemon_heartbeat_history
    WHERE name = NEW.name
      AND heartbeat_id IN (
          SELECT heartbeat_id
          FROM daemon_heartbeat_history
          WHERE name = NEW.name
          ORDER BY heartbeat_id DESC
          LIMIT -1 OFFSET 50
      );
END;

COMMIT;
