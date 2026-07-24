-- 0085 — document the required idempotency key when arming sprint wake.
--
-- The sprint_orchestration skill named the Interface mutation and payload but
-- omitted its mandatory Idempotency-Key header, so following the workflow
-- failed before it could arm a binding. Source asset and this idempotent delta
-- converge fresh builds and existing installs.

BEGIN;

UPDATE skills SET content = replace(content,
'- **Arm before the sprint''s first wake.** Once your Interface chat is
  live, arm the binding: `POST /api/interface/sprint-bindings` with
  `sprint_doc_id` + `planner_shell_id` (a shell may arm only itself; the
  operator may arm any planner). Arming is fail-closed: a frozen or
  non-ACTIVE doc, a mandatory-hook gap, or a second ACTIVE binding is
  refused. PR watches registered with `--sprint <doc-id>` ride the
  binding — an unarmed binding means `pr_event` rows arrive but nothing
  wakes you.',
'- **Arm before the sprint''s first wake.** Once your Interface chat is
  live, arm the binding with the required idempotency header:

  ```http
  POST /api/interface/sprint-bindings
  Idempotency-Key: sprint-bind-<sprint-doc-id>-<planner-shell-id>

  {"sprint_doc_id": <sprint-doc-id>, "planner_shell_id": <planner-shell-id>}
  ```

  Reuse that caller-stable key when retrying the same arm intent. Derive it
  from stable intent inputs (the sprint document + planner), not a timestamp
  or a new random value, so a retry replays the first result instead of
  arming twice. A shell may arm only itself; the operator may arm any planner.
  Arming is fail-closed: a frozen or non-ACTIVE doc, a mandatory-hook gap, or
  a second ACTIVE binding is refused. PR watches registered with
  `--sprint <doc-id>` ride the binding — an unarmed binding means `pr_event`
  rows arrive but nothing wakes you.')
WHERE name='sprint_orchestration';

COMMIT;
