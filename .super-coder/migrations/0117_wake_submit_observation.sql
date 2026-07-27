-- 0117 — when a batch entered `submitting`, so silence there is observable
-- (spec #76 H-27, as reshaped by decisions #98/#99)
--
-- H-27 says the declared hook chain must be verified by OBSERVATION rather
-- than trusted from the static CAPABILITIES table. On the floor U7 just laid,
-- the place that trust is now load-bearing is the submit path: a
-- `first_turn_gated` seat is promoted to idle on the weak process_ready_at
-- proof and the wake goes out, so if the chain is in fact dead the batch
-- enters `submitting` and stops there. Nothing in flight looks at it —
-- `_drain_sync` returns early on ('submitting','running') with "the hook
-- evidence drives it from here", and the only writer that ever moves it again
-- is restart recovery (interface_reconcile.py, submitting-without-a-submit-
-- hook -> delivery_unknown). That is U7's F13 measurement exactly: a live,
-- trusted, hooks-installed seat across which not one provider hook arrived.
--
-- WHY A NEW COLUMN AND NOT AN EXISTING ONE. To say "this batch has been
-- submitting for N seconds" something must record WHEN it entered the state,
-- and no such stamp exists. `submitted_at` is the obvious candidate and is
-- exactly wrong: it is written at the submitting -> running transition, BY THE
-- prompt_submit HOOK — the very event whose absence is being measured — so on
-- a silent seat it is NULL forever. `created_at` measures the queued wait H-26
-- already alerts on, which is a different failure with a different threshold.
-- Widening either would make one column mean two things and silently falsify
-- its existing readers (decision #61's defect, and decision #98's first
-- condition one table over).
--
-- Additive and nullable: every consumer of planner_wake_batches selects by
-- name, so this is safe to replay under the filename ledger. Rows that
-- predate it read NULL, which the alert treats as "not measured" and never as
-- "zero seconds" — an unmeasured batch must not be reported as a silent one.

BEGIN;

ALTER TABLE planner_wake_batches ADD COLUMN submitting_at TEXT;

COMMIT;
