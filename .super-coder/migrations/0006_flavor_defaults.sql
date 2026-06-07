-- 0006 — per-flavor launch defaults (harness + model)
--
-- Adds flavor_defaults and seeds the alpha team's roles. Idempotent and
-- converges with schema.sql on rebuild (CREATE … IF NOT EXISTS; INSERT OR
-- IGNORE), matching the 0002–0005 precedent. ADVISORY ONLY: run.py uses these
-- to set the launch default and annotate the picker, but --harness / -m / the
-- picker override them. model is harness-specific (opencode "provider/model");
-- NULL lets the harness pick its own (the claude harness manages its model).
--
-- Doctrine (see shell_decisions, CC home DB): middle roles (dev, cartographer)
-- = a cheap, caching, coding-tuned model; bookends (planner, reviewer) =
-- premium, with the reviewer on a different lineage (claude) for adversarial
-- diversity against GPT-authored code. Retune with UPDATE as trial data lands.

BEGIN;

CREATE TABLE IF NOT EXISTS flavor_defaults (
    flavor   TEXT PRIMARY KEY,
    harness  TEXT NOT NULL,
    model    TEXT
);

INSERT OR IGNORE INTO flavor_defaults (flavor, harness, model) VALUES
    ('dev',          'opencode', 'openai/gpt-5.1-codex-mini'),
    ('cartographer', 'opencode', 'openai/gpt-5.1-codex-mini'),
    ('planner',      'opencode', 'openai/gpt-5.5'),
    ('reviewer',     'claude',   NULL);

COMMIT;
