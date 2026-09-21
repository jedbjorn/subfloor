-- 0269 — name the document retirement verb in the Planner prompt (spec #251).
--
-- The source of the text is templates/shells/planner.md, which a fresh build
-- and `refresh_standard_prompts` render from directly. Live Planner rows carry
-- an already-rendered copy, so this reconciles them in place — same shape as
-- 0261 for the Developer body. Idempotent: guarded on the anchor being present
-- and the new sentence being absent.

BEGIN;

UPDATE shells
SET system_prompt = replace(
  system_prompt,
  'Until that close, shipped + open flag is the truthful interim state.',
  'Until that close, shipped + open flag is the truthful interim state.

When a successor replaces a document, retire the old one with
`sc mem doc retire <id> [--superseded-by <id>]` — metadata about the document,
never an edit to it, so a frozen body stays untouched while readers are pointed
at what is current (`--undo` reverses a wrong id).'
)
WHERE flavor = 'planner'
  AND instr(system_prompt, '## REVISE, FREEZE, DOCUMENT') > 0
  AND instr(system_prompt, 'Until that close, shipped + open flag is the truthful interim state.') > 0
  AND instr(system_prompt, 'sc mem doc retire <id>') = 0;

COMMIT;
