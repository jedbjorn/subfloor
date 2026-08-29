-- Permit one audited governing-spec rebind while a Sprint is paused.

BEGIN;

CREATE TABLE sprint_spec_revision_history (
    revision_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id              INTEGER NOT NULL,
    document_id            INTEGER NOT NULL,
    generation             INTEGER NOT NULL CHECK (generation > 0),
    bound_revision_sha256  TEXT NOT NULL
                           CHECK (
                             length(bound_revision_sha256)=64
                             AND bound_revision_sha256 NOT GLOB '*[^0-9a-f]*'
                           ),
    bound_revision_body    TEXT,
    bound_revision_legacy  INTEGER NOT NULL CHECK (bound_revision_legacy IN (0,1)),
    approval_id            INTEGER REFERENCES sprint_spec_approvals(approval_id),
    actor_kind             TEXT NOT NULL
                           CHECK (actor_kind IN ('planner','fnb','system')),
    actor_shell_id         INTEGER REFERENCES shells(shell_id),
    reason                 TEXT NOT NULL CHECK (trim(reason)<>''),
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (bound_revision_legacy=1 OR bound_revision_body IS NOT NULL),
    CHECK (
      (actor_kind='system' AND actor_shell_id IS NULL)
      OR (actor_kind IN ('planner','fnb') AND actor_shell_id IS NOT NULL)
    ),
    UNIQUE (sprint_id, document_id, generation),
    FOREIGN KEY (sprint_id) REFERENCES sprints(sprint_id),
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);
CREATE INDEX idx_sprint_spec_revision_history_binding
    ON sprint_spec_revision_history(sprint_id, document_id, revision_id);

INSERT INTO sprint_spec_revision_history (
    sprint_id,
    document_id,
    generation,
    bound_revision_sha256,
    bound_revision_body,
    bound_revision_legacy,
    approval_id,
    actor_kind,
    reason,
    created_at
)
SELECT sprint_id,
       document_id,
       1,
       bound_revision_sha256,
       bound_revision_body,
       bound_revision_legacy,
       approval_id,
       'system',
       'binding history initialized',
       included_at
FROM sprint_specs;

CREATE TABLE governing_revision_rebind_permits (
    sprint_id              INTEGER NOT NULL,
    document_id            INTEGER NOT NULL,
    old_revision_sha256    TEXT NOT NULL,
    new_revision_id        INTEGER NOT NULL
                           REFERENCES sprint_spec_revision_history(revision_id)
                           ON DELETE CASCADE,
    PRIMARY KEY (sprint_id, document_id),
    FOREIGN KEY (sprint_id, document_id)
        REFERENCES sprint_specs(sprint_id, document_id) ON DELETE CASCADE
);

DROP TRIGGER trg_sprint_specs_bound_revision_immutable;
CREATE TRIGGER trg_sprint_specs_bound_revision_immutable
BEFORE UPDATE OF
  bound_revision_sha256, bound_revision_body, bound_revision_legacy
ON sprint_specs
WHEN NOT (
   OLD.bound_revision_legacy=1
   AND OLD.bound_revision_body IS NULL
   AND NEW.bound_revision_legacy=1
   AND NEW.bound_revision_sha256 IS OLD.bound_revision_sha256
   AND NEW.bound_revision_body IS NOT NULL
   AND EXISTS (
     SELECT 1 FROM governing_revision_backfill_permits p
     WHERE p.sprint_id=OLD.sprint_id AND p.document_id=OLD.document_id
   )
 )
 AND NOT EXISTS (
   SELECT 1
   FROM governing_revision_rebind_permits permit
   JOIN sprint_spec_revision_history revision
     ON revision.revision_id=permit.new_revision_id
   WHERE permit.sprint_id=OLD.sprint_id
     AND permit.document_id=OLD.document_id
     AND permit.old_revision_sha256=OLD.bound_revision_sha256
     AND revision.sprint_id=OLD.sprint_id
     AND revision.document_id=OLD.document_id
     AND revision.bound_revision_sha256=NEW.bound_revision_sha256
     AND revision.bound_revision_body IS NEW.bound_revision_body
     AND revision.bound_revision_legacy=NEW.bound_revision_legacy
     AND revision.bound_revision_legacy=0
 )
 AND (
   NEW.bound_revision_sha256 IS NOT OLD.bound_revision_sha256
   OR NEW.bound_revision_body IS NOT OLD.bound_revision_body
   OR NEW.bound_revision_legacy IS NOT OLD.bound_revision_legacy
 )
BEGIN
  SELECT RAISE(ABORT, 'Sprint governing revisions are immutable');
END;

CREATE TRIGGER trg_sprint_spec_revision_history_append_only_update
BEFORE UPDATE ON sprint_spec_revision_history
BEGIN
  SELECT RAISE(ABORT, 'Sprint governing revision history is append-only');
END;

CREATE TRIGGER trg_sprint_spec_revision_history_append_only_delete
BEFORE DELETE ON sprint_spec_revision_history
BEGIN
  SELECT RAISE(ABORT, 'Sprint governing revision history is append-only');
END;

COMMIT;
