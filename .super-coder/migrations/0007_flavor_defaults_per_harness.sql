-- 0007 — per-(flavor × harness) launch model defaults
--
-- Reshapes flavor_defaults from one-row-per-flavor (flavor PRIMARY KEY) to a
-- (flavor, harness) matrix, so a flavor can carry a DIFFERENT model per harness
-- — the operator picks the harness at launch and gets that harness's model. The
-- old shape could only name one harness+model per flavor.
--
-- is_default marks which harness the picker pre-selects for a flavor (advisory;
-- --harness / the picker / -m still override).
--
-- flavor_defaults is pure launch config — no FKs reference it, no per-instance
-- memory in it — so DROP + recreate + reseed is safe, idempotent, and converges
-- with schema.sql on rebuild (schema makes the new shape; 0006 inserts the 4 old
-- rows; this drops & reseeds the 8 new ones — both fresh-rebuild and existing
-- forks land identical). Matches the 0002–0006 convergence precedent.
--
-- Doctrine (see shell_decisions, CC home DB): middle roles (dev, cartographer) =
-- a fast coding-tuned model; bookends (planner, reviewer) = premium. Each flavor
-- offers THREE provider lineages, one per harness:
--   codex    — OpenAI via ChatGPT subscription (flat billing, no per-token API
--              metering — the reason this exists). Bare model ids (gpt-5.5,
--              gpt-5.4-mini — the two codex+ChatGPT exposes; gpt-5.4 was an
--              API-only id ChatGPT-account codex rejects, see 0009).
--   claude   — Anthropic via subscription. Aliases (sonnet/haiku/opus).
--   opencode — open-weights via Ollama Cloud, a deliberately DIFFERENT lineage
--              (not a second OpenAI path). provider/model ids (ollama/…-cloud);
--              opencode's ollama-cloud provider config + signin is handled
--              per-fork, not here — these rows only set the runtime model.
-- The reviewer DEFAULTS to claude (is_default) — a different model lineage from
-- the GPT-authored code it reviews, for adversarial diversity. Codex/claude are
-- the picker defaults; opencode is always the third option (is_default 0).
-- Retune with UPDATE / a later migration as trial data lands.

BEGIN;

DROP TABLE IF EXISTS flavor_defaults;

CREATE TABLE flavor_defaults (
    flavor     TEXT    NOT NULL,
    harness    TEXT    NOT NULL,
    model      TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,   -- 1 = picker default harness for this flavor
    PRIMARY KEY (flavor, harness)
);

INSERT INTO flavor_defaults (flavor, harness, model, is_default) VALUES
    ('dev',          'codex',    'gpt-5.4-mini',                  1),
    ('dev',          'claude',   'sonnet',                        0),
    ('dev',          'opencode', 'ollama/qwen3-coder:480b-cloud', 0),
    ('cartographer', 'codex',    'gpt-5.4-mini',                  1),
    ('cartographer', 'claude',   'haiku',                         0),
    ('cartographer', 'opencode', 'ollama/gpt-oss:20b-cloud',      0),
    ('planner',      'codex',    'gpt-5.5',                       1),
    ('planner',      'claude',   'opus',                          0),
    ('planner',      'opencode', 'ollama/deepseek-v4-pro:cloud',  0),
    ('reviewer',     'codex',    'gpt-5.5',                       0),
    ('reviewer',     'claude',   'opus',                          1),
    ('reviewer',     'opencode', 'ollama/kimi-k2.6:cloud',        0);

COMMIT;
