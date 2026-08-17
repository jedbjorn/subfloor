-- Feature #54 / spec #149 — route support evidence changed payload shape.
--
-- Existing route rows and v6 cache generations have no raw harness/support
-- metadata.  Do not present them as selectable evidence after upgrade: retain
-- their diagnostic rows, mark them stale, and make the next catalogue read
-- require one atomic v7 refresh before publication.

BEGIN;

UPDATE model_routes
SET stale=1,
    last_error='Catalogue refresh required after harness support evidence migration'
WHERE harness_support_state IS NULL;

DELETE FROM model_catalog_generations;

COMMIT;
