-- 0123 — reviewed-spec QAQC and authoritative sprint declarations.
--
-- Legacy boards are preserved exactly as they are and receive only a
-- conservative lifecycle marker.  No owner, governing spec, review, or route
-- is inferred from prose or ambient fleet state; the operator adoption route
-- is the explicit repair seam.

BEGIN;

CREATE TABLE spec_qaqc_reviews (
    review_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_doc_id        INTEGER NOT NULL REFERENCES documents(document_id),
    reviewer_shell_id  INTEGER NOT NULL REFERENCES shells(shell_id),
    body_sha256        TEXT NOT NULL
                       CHECK (
                         length(body_sha256)=64
                         AND body_sha256 NOT GLOB '*[^0-9a-f]*'
                       ),
    verdict            TEXT NOT NULL
                       CHECK (verdict IN ('approved','changes_requested')),
    findings_doc_id    INTEGER REFERENCES documents(document_id),
    completed_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_spec_qaqc_reviews_spec
    ON spec_qaqc_reviews(spec_doc_id, review_id);
CREATE INDEX idx_spec_qaqc_reviews_eligibility
    ON spec_qaqc_reviews(spec_doc_id, body_sha256, verdict, review_id);

CREATE TRIGGER trg_spec_qaqc_reviews_append_only_update
BEFORE UPDATE ON spec_qaqc_reviews
BEGIN
  SELECT RAISE(ABORT, 'spec_qaqc_reviews is append-only');
END;

CREATE TRIGGER trg_spec_qaqc_reviews_append_only_delete
BEFORE DELETE ON spec_qaqc_reviews
BEGIN
  SELECT RAISE(ABORT, 'spec_qaqc_reviews is append-only');
END;

CREATE TRIGGER trg_spec_qaqc_reviews_spec_insert
BEFORE INSERT ON spec_qaqc_reviews
WHEN NOT EXISTS (
  SELECT 1 FROM documents d
  WHERE d.document_id=NEW.spec_doc_id AND d.kind='spec'
)
BEGIN
  SELECT RAISE(ABORT, 'QAQC target must be a spec document');
END;

CREATE TRIGGER trg_spec_qaqc_reviews_reviewer_insert
BEFORE INSERT ON spec_qaqc_reviews
WHEN NOT EXISTS (
  SELECT 1 FROM shells s
  WHERE s.shell_id=NEW.reviewer_shell_id
    AND s.flavor='reviewer'
    AND COALESCE(s.is_deleted,0)=0
)
BEGIN
  SELECT RAISE(ABORT, 'QAQC actor must be an active reviewer shell');
END;

CREATE TABLE sprints (
    sprint_doc_id       INTEGER PRIMARY KEY REFERENCES documents(document_id),
    spec_doc_id         INTEGER REFERENCES documents(document_id),
    planner_shell_id    INTEGER REFERENCES shells(shell_id),
    qaqc_review_id      INTEGER REFERENCES spec_qaqc_reviews(review_id),
    planner_route       TEXT,
    dev_route           TEXT,
    reviewer_route      TEXT,
    state               TEXT NOT NULL
                        CHECK (state IN (
                          'needs_owner','declared','active','closing',
                          'closed','aborted'
                        )),
    legacy              INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0,1)),
    declared_at         TEXT NOT NULL DEFAULT (datetime('now')),
    handed_off_at       TEXT,
    closed_at           TEXT,
    CHECK (
      legacy=1
      OR (
        spec_doc_id IS NOT NULL
        AND planner_shell_id IS NOT NULL
        AND qaqc_review_id IS NOT NULL
        AND trim(COALESCE(planner_route,'')) <> ''
        AND trim(COALESCE(dev_route,'')) <> ''
        AND trim(COALESCE(reviewer_route,'')) <> ''
      )
    )
);

CREATE INDEX idx_sprints_state ON sprints(state, sprint_doc_id);
CREATE INDEX idx_sprints_planner ON sprints(planner_shell_id, state);
CREATE INDEX idx_sprints_spec ON sprints(spec_doc_id, sprint_doc_id);

CREATE TRIGGER trg_sprints_document_insert
BEFORE INSERT ON sprints
WHEN NOT EXISTS (
  SELECT 1 FROM documents d
  WHERE d.document_id=NEW.sprint_doc_id
    AND d.kind='doc'
    AND d.title LIKE 'SPRINT:%'
)
BEGIN
  SELECT RAISE(ABORT, 'sprint row requires a SPRINT: document');
END;

CREATE TRIGGER trg_sprints_review_insert
BEFORE INSERT ON sprints
WHEN NEW.qaqc_review_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM spec_qaqc_reviews q
  WHERE q.review_id=NEW.qaqc_review_id
    AND q.spec_doc_id=NEW.spec_doc_id
    AND q.verdict='approved'
)
BEGIN
  SELECT RAISE(ABORT, 'sprint QAQC review must approve its governing spec');
END;

CREATE TRIGGER trg_sprints_identity_update
BEFORE UPDATE OF sprint_doc_id, spec_doc_id, planner_shell_id, qaqc_review_id,
                 planner_route, dev_route, reviewer_route, legacy
ON sprints
WHEN NOT (
  OLD.state='needs_owner'
  AND OLD.legacy=1
  AND NEW.sprint_doc_id=OLD.sprint_doc_id
  AND NEW.legacy=1
)
BEGIN
  SELECT RAISE(ABORT, 'declared sprint identity and routes are immutable');
END;

CREATE TRIGGER trg_sprints_state_transition
BEFORE UPDATE OF state ON sprints
WHEN NEW.state<>OLD.state AND NOT (
  (OLD.state='needs_owner' AND NEW.state IN ('declared','closed','aborted'))
  OR (OLD.state='declared' AND NEW.state IN ('active','aborted'))
  OR (OLD.state='active' AND NEW.state IN ('closing','aborted'))
  OR (OLD.state='closing' AND NEW.state IN ('closed','aborted'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal sprint state transition');
END;

-- Every legacy document with a real board gets an authoritative lifecycle
-- marker.  The old board remains readable, but Conductor refuses it until the
-- operator explicitly adopts it.
INSERT INTO sprints (
    sprint_doc_id, state, legacy, declared_at, closed_at
)
SELECT
    d.document_id,
    'needs_owner',
    1,
    d.created_at,
    CASE WHEN d.frozen=1
         THEN COALESCE(d.frozen_date, d.updated_at)
         ELSE NULL END
FROM documents d
WHERE d.kind='doc'
  AND d.title LIKE 'SPRINT:%'
  AND EXISTS (
    SELECT 1 FROM sprint_units u
    WHERE u.sprint_doc_id=d.document_id
  );

COMMIT;
