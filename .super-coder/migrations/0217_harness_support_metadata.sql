-- Feature #54 / Decision #219 — tested support is advisory metadata.
--
-- A route remains runnable when its executable, exact local source evidence,
-- and generic transport contract are present.  The support state records
-- whether that observed runtime is the maintained canary baseline without
-- turning a version range into an admission gate.

BEGIN;

ALTER TABLE model_routes ADD COLUMN harness_support_state TEXT CHECK (
  harness_support_state IS NULL OR
  harness_support_state IN ('tested','best-effort')
);

COMMIT;
