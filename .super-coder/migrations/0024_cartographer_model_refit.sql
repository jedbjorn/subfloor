-- 0024 — Cartographer flavor: Sonnet as primary, GPT-5.4 + GLM-5.2 as alternates
--
-- Cartographer's workload (scripting, semantic extraction, section curation) is
-- heavier than haiku-tier, and the current codex default (gpt-5.4-mini) is the
-- cheap/fast variant. Refit:
--
--   claude   haiku       -> sonnet    (becomes default harness — Claude over codex)
--   codex    gpt-5.4-mini -> gpt-5.4  (non-mini; stays as named alternate)
--   opencode qwen3-coder-next -> glm-5.2  (aligns with reviewer; strong SWE-Bench)
--
-- flavor_defaults is pure launch config (no FKs, no per-instance memory, not
-- snapshotted into content.sql), so plain UPDATEs converge fresh rebuilds and
-- already-installed forks alike. Idempotent / re-runnable: each targets one
-- (flavor, harness) row by primary key.

BEGIN;

-- Make claude the default harness for cartographer (was codex)
UPDATE flavor_defaults SET model = 'sonnet', is_default = 1
    WHERE flavor = 'cartographer' AND harness = 'claude';

UPDATE flavor_defaults SET model = 'gpt-5.4', is_default = 0
    WHERE flavor = 'cartographer' AND harness = 'codex';

UPDATE flavor_defaults SET model = 'ollama-cloud/glm-5.2'
    WHERE flavor = 'cartographer' AND harness = 'opencode';

COMMIT;
