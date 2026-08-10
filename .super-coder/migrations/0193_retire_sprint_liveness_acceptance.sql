-- 0193 — Retire model-facing Sprint liveness writers.
--
-- Historical expectations and their resolution paths remain durable and
-- readable. New actionable acceptance no longer creates an expectation; the
-- runtime and monitor no longer evaluate the historical queue.

BEGIN;

DROP TRIGGER IF EXISTS trg_sprint_liveness_acceptance;

COMMIT;
