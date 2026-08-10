-- 0196 — retain exact message provenance across Sprint wake recovery.

CREATE TABLE sprint_wake_recovery_messages (
    recovery_event_id   INTEGER NOT NULL
                        REFERENCES sprint_events(event_id) ON DELETE CASCADE,
    sprint_id           INTEGER NOT NULL REFERENCES sprints(sprint_id),
    prior_wake_id       INTEGER REFERENCES sprint_wake_outbox(wake_id),
    replacement_wake_id INTEGER NOT NULL REFERENCES sprint_wake_outbox(wake_id),
    message_id          INTEGER NOT NULL REFERENCES wake_message(message_id),
    PRIMARY KEY (recovery_event_id, message_id)
);

CREATE INDEX idx_sprint_wake_recovery_lookup
    ON sprint_wake_recovery_messages(
        sprint_id, replacement_wake_id, message_id, recovery_event_id
    );
