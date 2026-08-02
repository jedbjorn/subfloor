-- 0150 — Sprints v2 conformance follow-ups and idempotent close reports.
--
-- Conformance findings are durable work for FnB disposition after the Sprint;
-- they never become editing lanes in the Sprint being reviewed.  Report keys
-- make reviewer retries safe without weakening the append-only report record.

BEGIN;

ALTER TABLE sprint_reports ADD COLUMN idempotency_key TEXT
    CHECK (
      idempotency_key IS NULL
      OR length(idempotency_key) BETWEEN 1 AND 255
    );

CREATE UNIQUE INDEX idx_sprint_reports_idempotency
    ON sprint_reports(sprint_id, report_kind, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE sprint_followups (
    followup_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id         INTEGER NOT NULL REFERENCES sprints(sprint_id),
    source_report_id  INTEGER NOT NULL REFERENCES sprint_reports(report_id),
    severity          TEXT NOT NULL CHECK (length(trim(severity)) BETWEEN 1 AND 32),
    title             TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 255),
    body              TEXT NOT NULL CHECK (length(trim(body)) BETWEEN 1 AND 8000),
    spec_document_id  INTEGER REFERENCES documents(document_id),
    work_unit_id      INTEGER REFERENCES sprint_work_units(work_unit_id),
    disposition       TEXT NOT NULL DEFAULT 'pending'
                      CHECK (disposition IN
                        ('pending','accepted','resolved','dismissed')),
    resolution        TEXT,
    idempotency_key   TEXT NOT NULL UNIQUE
                      CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at       TEXT,
    FOREIGN KEY (sprint_id, work_unit_id)
      REFERENCES sprint_work_units(sprint_id, work_unit_id),
    CHECK (
      (disposition IN ('pending','accepted')
       AND resolved_at IS NULL AND resolution IS NULL)
      OR
      (disposition IN ('resolved','dismissed')
       AND resolved_at IS NOT NULL
       AND length(trim(COALESCE(resolution,''))) > 0)
    )
);

CREATE INDEX idx_sprint_followups_pending
    ON sprint_followups(sprint_id, disposition, followup_id);

CREATE TRIGGER trg_sprint_followups_report_scope
BEFORE INSERT ON sprint_followups
WHEN NOT EXISTS (
  SELECT 1 FROM sprint_reports
  WHERE report_id=NEW.source_report_id AND sprint_id=NEW.sprint_id
)
BEGIN
  SELECT RAISE(ABORT, 'Sprint follow-up report belongs to another Sprint');
END;

CREATE TRIGGER trg_sprint_followups_spec_scope
BEFORE INSERT ON sprint_followups
WHEN NEW.spec_document_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM sprint_specs
  WHERE sprint_id=NEW.sprint_id AND document_id=NEW.spec_document_id
)
BEGIN
  SELECT RAISE(ABORT, 'Sprint follow-up spec is not bound to this Sprint');
END;

CREATE TRIGGER trg_sprint_followups_content_immutable
BEFORE UPDATE OF
    sprint_id,source_report_id,severity,title,body,spec_document_id,
    work_unit_id,idempotency_key
ON sprint_followups
BEGIN
  SELECT RAISE(ABORT, 'Sprint follow-up content is immutable');
END;

CREATE TRIGGER trg_sprint_followups_no_delete
BEFORE DELETE ON sprint_followups
BEGIN
  SELECT RAISE(ABORT, 'Sprint follow-up history is durable');
END;

COMMIT;
