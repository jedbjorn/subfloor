-- 0084 — preserve sprint worker-fault guidance after the wake-ops reseed.
--
-- Concurrent branches introduced the worker-fault guidance as 0081 while the
-- older 0082 wake-ops reseed still followed it in filename order. A fresh
-- rebuild therefore overwrote the newer paragraph. Apply the semantic delta
-- after both whole-skill reseeds without copying either full skill body again.

BEGIN;

UPDATE skills
SET content = replace(
  content,
  '- **Link gone quiet** (no `result` row, no `pr_event` movement): boot it with
  its declared sprint route — `./sc run <shortname> --harness <role-harness>
  -m <role-model> --effort high` drains its inbox and acts; that IS the nudge in
  an event-driven sprint. The liveness guard refusing (session already
  live) + still silent -> escalate to the FnB with the worktree state.',
  '- **Worker faulted mid-task** (rate-limit cutoff, provider error, session
  died): its `task` row is already consumed — a worker marks the row read
  when it starts acting, so a fault leaves a read row and an unfinished
  unit. Re-launching alone drains an empty inbox and the worker idles on
  the default prompt. **Confirm the row''s state at runtime before you
  boot** — `sc mem message sent` carries read receipts; a task row showing
  read means re-send it (same unit, plus where the work stopped and what
  is already on the branch), *then* `./sc run`. A re-boot is not a
  re-task.
- **Link gone quiet** (no `result` row, no `pr_event` movement): boot it with
  its declared sprint route — `./sc run <shortname> --harness <role-harness>
  -m <role-model> --effort high` drains its inbox and acts; that IS the nudge in
  an event-driven sprint. Check `sent` first, though — a read task row
  means the link faulted rather than stalled, and the boot has nothing to
  act on. The liveness guard refusing (session already
  live) + still silent -> escalate to the FnB with the worktree state.'
)
WHERE name = 'sprint_orchestration';

COMMIT;
