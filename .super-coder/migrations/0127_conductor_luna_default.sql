-- 0127 — Route the ephemeral Conductor through GPT-5.6 Luna on OpenCode.
--
-- Luna is the routine cost/capability default for mechanical sprint
-- orchestration.  Operators may still select a larger Ollama model such as
-- ollama-cloud/gpt-oss:120b explicitly.  Only the exact Conductor value
-- previously shipped by 0121 is migrated; an operator-tuned route survives.

BEGIN;

UPDATE flavor_defaults
SET model = 'openai/gpt-5.6-luna'
WHERE flavor = 'conductor'
  AND harness = 'opencode'
  AND model = 'ollama-cloud/gpt-oss:20b';

COMMIT;
