-- 0023 — dev (coder) flavor: default to the premium model on codex + claude
--
-- Coder shells should lead with the strongest model their harness offers, not the
-- fast/cheap tier:
--   codex   gpt-5.4-mini  -> gpt-5.5   (full, not the mini)
--   claude  sonnet        -> opus
-- opencode (open-weight) is left as-is.
--
-- flavor_defaults is pure launch config (no FKs, no per-instance memory, not
-- snapshotted into content.sql), so plain UPDATEs converge fresh rebuilds and
-- already-installed forks alike. Idempotent / re-runnable: each targets one
-- (flavor, harness) row by primary key.

BEGIN;

UPDATE flavor_defaults SET model = 'gpt-5.5'
    WHERE flavor = 'dev' AND harness = 'codex';

UPDATE flavor_defaults SET model = 'opus'
    WHERE flavor = 'dev' AND harness = 'claude';

COMMIT;
