-- 0108 — the sprint board gets a real transition machine, and the review
-- verdict gets a column (spec doc 76, H-11 + H-14; sprint doc 84 U3).
--
-- 0098 gave `sprint_units.state` a CHECK vocabulary and nothing else. A
-- vocabulary answers "is this a word" — it never answers "may this row go
-- there from where it is". So `merged -> working` and `cancelled -> pending`
-- are accepted today: the reconciler re-arms on a unit that shipped, and the
-- boot renderer re-issues a worker directive for work that is over. Every
-- other lifecycle in this engine (0078) carries a DB trigger PLUS a mirrored
-- app edge map; the board is the one that did not.
--
-- ── merged and cancelled are TERMINAL — no exits, ever ──────────────────────
--
-- Deliberately no override flag, no admin escape, no `force` verb. The
-- immutability IS the feature: a terminal row is the record of what was
-- declared, and a record that can be walked back is not a record. A
-- MIS-DECLARED terminal state is corrected the way a mis-frozen document is
-- (decision #82's shape): by declaring a SUCCESSOR UNIT at a new seq that
-- redoes or disposes of the work. The predecessor stands untouched, and the
-- pair reads as what actually happened rather than as a state that quietly
-- became something else.
--
-- The trigger is the backstop; scripts/interface_state.py:SPRINT_UNIT_EDGES
-- mirrors it so the API answers 409 with a sentence instead of an
-- IntegrityError. KEEP THE TWO IN SYNC — tests/test_interface_transitions.py
-- walks every (old, new) pair against BOTH layers and fails on any drift.
--
-- A same-state re-assert stays legal (`NEW.state <> OLD.state` guard), exactly
-- as the 0078 machines do: the PATCH route already declines to restamp
-- state_changed_at on a no-op move, and making the no-op itself an error would
-- turn an idempotent retry into a failure.
--
-- ── H-14: review_head ──────────────────────────────────────────────────────
--
-- Sprint close requires "every assigned review ended review-clean at a known
-- head". That fact lives only in message prose, so close cannot verify it —
-- it can only re-read a sentence someone wrote. One column carries it: the
-- planner sets `review_head` from the reviewer's `review-clean head=<sha>`
-- verdict when it moves a unit out of `in_review`.
--
-- PRESENCE, not correctness. Nothing here checks that the SHA is the head that
-- was reviewed, or that it is a SHA at all — judgment about a verdict stays
-- with the planner (decision #76). Nullable, because a unit that has not
-- reached review has no head to name, and `cancelled` units never had a clean
-- review at all and are exempt from the close check by construction.

BEGIN;

ALTER TABLE sprint_units ADD COLUMN review_head TEXT;

CREATE TRIGGER IF NOT EXISTS trg_sprint_units_state
BEFORE UPDATE OF state ON sprint_units
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state = 'pending'   AND NEW.state IN ('working','cancelled')) OR
    (OLD.state = 'working'   AND NEW.state IN ('in_review','blocked','merged','cancelled')) OR
    (OLD.state = 'in_review' AND NEW.state IN ('working','blocked','merged','cancelled')) OR
    (OLD.state = 'blocked'   AND NEW.state IN ('working','cancelled'))
    -- 'merged' and 'cancelled' appear on no left-hand side: terminal.
)
BEGIN
  SELECT RAISE(ABORT, 'illegal sprint unit transition');
END;

COMMIT;
