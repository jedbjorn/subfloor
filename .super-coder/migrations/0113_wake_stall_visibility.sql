-- 0113 — a stalled wake batch becomes visible, with its reason (spec #76 H-26)
--
-- Six gates stand between a formed batch and submission and only two of them
-- alert. The rest return gate_fail silently and retry on the next event, so a
-- PERSISTENT gate failure is indistinguishable from a transient deferral: the
-- batch sits queued accumulating items, `sc sprint status` shows depth but
-- never cause, and `sc sprint alerts` shows nothing at all. Downstream issue
-- #638 is the live instance — armed binding, batch queued 33+ minutes through
-- 11 accumulated items including arrived reviewer results, and the planner
-- resumed only on unrelated user input plus a manual inbox drain.
--
-- WHY COLUMNS AND NOT A NEW TABLE. Decision #76 forbids a new state-change
-- log, and none is needed: the alert is the visible artifact and it already
-- has a table. What was missing is somewhere to put the MEASUREMENT the alert
-- must carry. `reason` cannot carry it — `reason` is the dedupe vocabulary
-- (the open-alert unique index keys on dedupe_key, which is built from
-- reason), so folding a varying gate string into it would mint a fresh alert
-- row per distinct gate reason and defeat the dedupe the requirement asks
-- for. Hence `detail`, free text, deliberately outside the dedupe key: one
-- open row per stalled batch, whose detail refreshes to the most recent
-- failing gate. Per decision #76 it states what was measured, never a verdict.
--
-- batch_id makes the alert keyable ON THE BATCH, which the requirement names.
-- It also gives the resolve path an exact target: submit or cancel resolves
-- that batch's rows and nothing else's.
--
-- last_gate_reason / last_gate_at live on the batch rather than the alert
-- because they are recorded on EVERY failed attempt, including the ones long
-- before the stall threshold — the alert reads the latest value when it
-- finally opens. Recording them only at alert time would name the gate that
-- happened to fail last after the threshold, not the gate that has been
-- failing all along, and those differ exactly when a seat is flapping.
--
-- All five are nullable additive columns: nothing reads planner_alerts or
-- planner_wake_batches positionally (every consumer selects by name), so this
-- is safe to replay under the filename ledger.

BEGIN;

ALTER TABLE planner_alerts ADD COLUMN batch_id INTEGER
    REFERENCES planner_wake_batches(batch_id);
ALTER TABLE planner_alerts ADD COLUMN detail TEXT;

ALTER TABLE planner_wake_batches ADD COLUMN last_gate_reason TEXT;
ALTER TABLE planner_wake_batches ADD COLUMN last_gate_at TEXT;

-- H-28: suppression must itself be observable (this spec's monitor rule) —
-- a batch records how many of its candidate rows were skipped as already
-- read, so "the queue went quiet" is never mistaken for "nothing arrived".
ALTER TABLE planner_wake_batches ADD COLUMN skipped_read INTEGER NOT NULL
    DEFAULT 0;

COMMIT;
