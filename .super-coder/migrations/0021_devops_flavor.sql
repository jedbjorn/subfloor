-- 0021 — devops flavor defaults
--
-- Introduces the devops flavor: the fork's RUNTIME-infrastructure owner — the
-- hosts the app runs on, the network/tailnet binding them, access, deploys,
-- backups, patch hygiene. Distinct from admin (which owns the super-coder
-- SUBSTRATE — engine, skills, schema) and dev (app code). The lane between the
-- app and the metal had no owner; this is it.
--
-- Opt-in only: NOT added to init_fork's default roster. Operators create a
-- devops shell via the GUI/API when the fork grows infrastructure to run.
--
-- Decisions carry outage risk (a wrong deploy / firewall / access change takes
-- the app down), so codex default is gpt-5.5 (premium), mirroring admin. The
-- flavor's signature skill is `tailscale`; it reaches hosts through the
-- host-side ts-broker, never holding tailnet credentials itself.

BEGIN;

INSERT OR IGNORE INTO flavor_defaults (flavor, harness, model, is_default) VALUES
    ('devops', 'codex',    'gpt-5.5',                       1),
    ('devops', 'claude',   'sonnet',                        0),
    ('devops', 'opencode', 'ollama/qwen3-coder:480b-cloud', 0);

COMMIT;
