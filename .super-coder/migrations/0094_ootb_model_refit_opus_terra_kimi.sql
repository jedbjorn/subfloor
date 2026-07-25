-- 0094 — OOTB launch defaults: Opus 5 primary, Terra + Kimi K3 secondary,
-- every flavor (FnB directive, 2026-07-25; supersedes the per-flavor tuning
-- of 0019/0024/0045/0070, including fable-primary for planner/reviewer).
--
-- Trigger: a cartographer session found ~2/3 of the repo map's descriptions
-- were auto-authored filler — work attributed to the weaker-tier primaries
-- 0024/0070 shipped (sonnet, then gpt-5.6-terra). The OOTB matrix now leads
-- every flavor with claude/opus and keeps codex/gpt-5.6-terra plus
-- kimi/kimi-code/k3 as the named alternates (Kimi is the standing
-- filter-refusal fallback — decisions #60/#90).
--
-- flavor_defaults is OPERATOR-TUNED launch config and IS snapshotted into
-- content.sql (the Default Models GUI writes it), unlike at 0024's writing.
-- Every statement therefore guards on the SET OF VALUES THIS REPO EVER
-- SHIPPED for that slot (union of 0006..0070 seeds + refits): a row holding
-- any shipped value transitions; a row the operator re-tuned to a value we
-- never shipped matches no clause and survives. A tuned value that happens to
-- equal a shipped one is indistinguishable and transitions — accepted. A
-- fresh rebuild (schema -> 0007 seed -> refits -> here) lands on the new
-- OOTB; re-runs are no-ops. One caveat: a custom NON-claude row carrying
-- is_default=1 keeps it and the flavor shows two defaults — operator-owned
-- state, operator resolves via the GUI.

BEGIN;

-- claude slot -> opus, becomes every flavor's primary.
UPDATE flavor_defaults SET model='opus', is_default=1
    WHERE harness='claude'
      AND (model IN ('sonnet', 'haiku', 'opus', 'fable') OR model IS NULL);

-- codex slot -> gpt-5.6-terra, named alternate, never the default harness.
UPDATE flavor_defaults SET model='gpt-5.6-terra', is_default=0
    WHERE harness='codex'
      AND model IN ('gpt-5.4-mini', 'gpt-5.4', 'gpt-5.5',
                    'gpt-5.6-sol', 'gpt-5.6-terra');

-- kimi/kimi-code/k3 joins as the second alternate (new rows; an existing
-- operator row wins via OR IGNORE).
INSERT OR IGNORE INTO flavor_defaults (flavor, harness, model, is_default) VALUES
    ('admin',        'kimi', 'kimi-code/k3', 0),
    ('cartographer', 'kimi', 'kimi-code/k3', 0),
    ('dev',          'kimi', 'kimi-code/k3', 0),
    ('devops',       'kimi', 'kimi-code/k3', 0),
    ('planner',      'kimi', 'kimi-code/k3', 0),
    ('reviewer',     'kimi', 'kimi-code/k3', 0);

-- opencode leaves the OOTB set — deleted only at shipped values, so an
-- operator-tuned opencode row survives as an extra alternate.
DELETE FROM flavor_defaults
    WHERE harness='opencode'
      AND model IN ('openai/gpt-5.1-codex-mini', 'openai/gpt-5.5',
                    'ollama/qwen3-coder:480b-cloud', 'ollama/gpt-oss:20b-cloud',
                    'ollama/deepseek-v4-pro:cloud', 'ollama/kimi-k2.6:cloud',
                    'ollama-cloud/glm-5.2', 'ollama-cloud/qwen3-coder-next',
                    'ollama-cloud/deepseek-v4-pro');

COMMIT;
