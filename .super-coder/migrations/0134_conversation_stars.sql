-- Conversation stars are durable operator metadata. Existing conversations
-- remain unstarred, and the boolean check keeps direct SQL writers honest.

ALTER TABLE conversations
    ADD COLUMN starred INTEGER NOT NULL DEFAULT 0
    CHECK (starred IN (0, 1));
