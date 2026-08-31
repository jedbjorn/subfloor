-- 0244 — keep Admin maintenance governed across an in-session floor update.

BEGIN;

UPDATE skills SET content=REPLACE(
  content,
  'git commit -m "chore: update super-coder engine pin"
```

Add the root `sc` dispatcher',
  'SC_SHELL_FLAVOR=admin git commit -m "chore: update super-coder engine pin"
```

Set the marker on this commit command even inside an Admin shell. The update
may have replaced the pre-commit hook during a session launched by the old
floor, whose inherited environment cannot contain the new Admin exemption.
The marker makes that one post-update handoff explicit without bypassing hooks.

Add the root `sc` dispatcher'
)
WHERE name='admin_git';

INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT 'admin', skill_id
FROM skills
WHERE name='flags' AND is_deleted=0;

COMMIT;
