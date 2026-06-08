-- 0010 — admin flavor defaults + self_update to admin-only
--
-- Introduces the admin flavor with its model defaults. Admin is the fork's
-- infrastructure owner (engine updates, rollbacks, migrations, skill lifecycle).
-- Decisions carry real risk (wrong rollback = data loss), so codex default
-- is gpt-5.5 (premium). Claude: sonnet. Opencode: qwen3-coder.
--
-- self_update moves from common: true to common: false in this release.
-- Revoke existing grants on non-admin shells so the live DB converges with
-- the updated skill catalogue on ./sc update.

BEGIN;

INSERT OR IGNORE INTO flavor_defaults (flavor, harness, model, is_default) VALUES
    ('admin', 'codex',    'gpt-5.5',                       1),
    ('admin', 'claude',   'sonnet',                        0),
    ('admin', 'opencode', 'ollama/qwen3-coder:480b-cloud', 0);

-- Revoke self_update from non-admin shells.
-- Safe if self_update doesn't exist yet (skill_id subquery returns NULL →
-- DELETE matches nothing). Idempotent.
DELETE FROM shell_skills
WHERE skill_id = (SELECT skill_id FROM skills WHERE name = 'self_update' AND is_deleted = 0)
  AND shell_id IN (SELECT shell_id FROM shells WHERE flavor != 'admin' AND is_deleted = 0);

COMMIT;
