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
  live, start one arm attempt by generating an attempt nonce once:

  ```sh
  arm_attempt_id="$(python3 -c ''import secrets; print(secrets.token_hex(16))'')"
  ```

  Retain that value until the attempt ends, then arm the binding with the
  required idempotency header:

  ```http
  POST /api/interface/sprint-bindings
  Idempotency-Key: sprint-bind-<sprint-doc-id>-<planner-shell-id>-<arm-attempt-id>

  {"sprint_doc_id": <sprint-doc-id>, "planner_shell_id": <planner-shell-id>}
  ```

  Reuse that exact caller-stable key only for retries of this arm attempt,
  including after an ambiguous transport failure. A successful release or a
  conclusive refusal ends the attempt. Generate a new `arm_attempt_id` for
  every later arm or re-arm; reusing a released attempt''s key would replay its
  released binding and leave the sprint unarmed. Never generate a timestamp or
  random value separately for each transport retry. A shell may arm only
  itself; the operator may arm any planner. Arming is fail-closed: a frozen or
  non-ACTIVE doc, a mandatory-hook gap, or a second ACTIVE binding is refused.
  PR watches registered with `--sprint <doc-id>` ride the binding — an unarmed
  binding means `pr_event` rows arrive but nothing wakes you.')
WHERE name='sprint_orchestration';

COMMIT;
