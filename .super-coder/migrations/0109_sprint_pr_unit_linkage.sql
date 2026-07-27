-- 0109 — structured PR↔unit linkage (spec doc 76, H-13; sprint doc 84 U3).
--
-- `sprint_units.pr_number` and `watched_prs.pr_number` are two free integers
-- that nothing joins. So when a pr_event arrives, "which unit is this about"
-- is answered by REGEX OVER MESSAGE PROSE (activity_readers._names_unit) —
-- the reconciler greps a body for "U3". That works until a body says "U3" for
-- another reason, and it cannot work at all for a row nobody wrote prose into.
--
-- Two columns' worth of structure removes the guess from the path that HAS the
-- answer, and changes nothing about the planner-advances-the-board design:
--
--   * `watched_prs.unit_id` — nullable FK, set at registration
--     (`sc watch pr … --unit U3`). Nullable because unscoped and legacy
--     watches are real rows: this link is an improvement to traffic that
--     carries it, never a new requirement on traffic that does not.
--
--   * a partial UNIQUE index on `sprint_units(sprint_doc_id, pr_number)` —
--     two units cannot claim one PR. On the BOARD, not on the registry: the
--     registry legitimately holds several rows for one PR (a dev's watch and
--     the planner's watch are different subscriptions to the same PR), so
--     uniqueness there would refuse a correct registration. The board is where
--     "this PR is unit U3's" is declared, and where a second claim is a
--     planner typo that silently re-points the reconciler.
--
-- WHERE pr_number IS NOT NULL, because most of a board's life is units with no
-- PR yet, and SQLite treats every NULL as distinct in a plain UNIQUE anyway —
-- the partial index states the intent instead of relying on that.
--
-- The regex is NOT deleted. It stays as the fallback for unscoped traffic;
-- readers PREFER the structured ref where one exists (see
-- pr_poller._alert callers: a watch-scoped alert now names the unit
-- structurally instead of leaving `planner_alerts.unit_id` NULL for a later
-- reader to re-derive from a string).

BEGIN;

ALTER TABLE watched_prs
    ADD COLUMN unit_id INTEGER REFERENCES sprint_units(unit_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sprint_units_pr_claim
    ON sprint_units(sprint_doc_id, pr_number)
    WHERE pr_number IS NOT NULL;

-- The poller resolves watch → unit on every event; the registry is small but
-- this is the only join it performs.
CREATE INDEX IF NOT EXISTS idx_watched_prs_unit
    ON watched_prs(unit_id);

COMMIT;
