-- Give every Sprint incarnation a durable identity for generic-conversation
-- idempotency.  Runtime Sprint rows are intentionally not snapshot content,
-- while their closed browser conversations may be preserved.  Numeric
-- sprint/participant ids can therefore be reused after rebuild and must not
-- be the global conversation-creation identity.
BEGIN;

ALTER TABLE sprints
ADD COLUMN conversation_generation TEXT NOT NULL DEFAULT ''
    CHECK (
      conversation_generation='' OR (
        length(conversation_generation)=32
        AND conversation_generation NOT GLOB '*[^0-9a-f]*'
      )
    );

UPDATE sprints
SET conversation_generation=lower(hex(randomblob(16)))
WHERE conversation_generation='';

CREATE UNIQUE INDEX idx_sprints_conversation_generation
    ON sprints(conversation_generation)
    WHERE conversation_generation<>'';

-- ALTER TABLE cannot add a non-constant random default.  Populate the
-- generation inside the inserting statement's transaction instead.
CREATE TRIGGER trg_sprints_conversation_generation_fill
AFTER INSERT ON sprints
WHEN NEW.conversation_generation=''
BEGIN
  UPDATE sprints
  SET conversation_generation=lower(hex(randomblob(16)))
  WHERE sprint_id=NEW.sprint_id;
END;

CREATE TRIGGER trg_sprints_conversation_generation_immutable
BEFORE UPDATE OF conversation_generation ON sprints
WHEN OLD.conversation_generation<>''
  AND NEW.conversation_generation<>OLD.conversation_generation
BEGIN
  SELECT RAISE(ABORT, 'Sprint conversation generation is immutable');
END;

COMMIT;
