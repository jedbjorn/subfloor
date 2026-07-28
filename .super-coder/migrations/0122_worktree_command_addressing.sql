-- 0122 — Canonical worktree command addressing.
--
-- Every non-admin shell runs from a linked worktree. The tracked launcher can
-- be absent there after a fresh install, while run.py always puts the live
-- checkout's canonical `sc` on PATH. Convert the distributed operational
-- skill bodies to that contract without replacing any fork-local customization
-- around the command examples. dev_kit is a fork-owned starter, so the narrow
-- token substitution is deliberate: preserve its body while making every
-- existing customized copy safe from a worktree.

UPDATE skills
SET description = replace(description, './sc', 'sc'),
    content = replace(content, './sc', 'sc')
WHERE name IN (
    'agents',
    'cartographer',
    'db_map',
    'dev_kit',
    'dev_sprint',
    'issue_reporting',
    'plan_sprint',
    'rev_sprint'
);

UPDATE skills
SET content = replace(
    content,
    '`cat .sc-state/engine.ref`',
    '`sc engine-ref`'
)
WHERE name = 'issue_reporting';
