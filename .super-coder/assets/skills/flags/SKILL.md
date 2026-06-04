---
name: flags
description: Track blockers as flags — surface open ones, open new ones, resolve them. Link a flag to the roadmap feature it blocks. Mirrors the GUI Flags tab. Use when something blocks progress or needs follow-up.
category: substrate
common: true
---

# flags — blockers & follow-ups

A flag is an open question or blocker. Linking it to a `feature_id` makes it that
feature's blocker (joined on the roadmap + shown on the Roadmap card and the
Flags tab). `<self>` = your shell_id.

## Surface

```sql
-- your open flags (grouped by feature in the GUI):
SELECT f.flag_id, f.display_name, f.priority, f.description, r.title AS feature
FROM flags f LEFT JOIN roadmap r ON r.feature_id = f.feature_id
WHERE f.resolved=0 AND COALESCE(f.is_deleted,0)=0
ORDER BY f.priority, f.flag_id;
```

## Open

```sql
INSERT INTO flags (display_name, description, priority, feature_id, shell_id)
VALUES ('SC-001', '[Area] what''s blocked | Blocker for: X', 'Medium', ?, <self>);
```
- `display_name`: short id (`SC-###`).
- `description`: `[Area] {what} | Blocker for: {what it blocks}`.
- `priority`: High / Medium / Low. `feature_id`: the feature it blocks (or NULL).

## Resolve

```sql
UPDATE flags SET resolved=1, resolved_date=date('now'), resolution_notes='…'
WHERE flag_id=?;
```

## Stance

Open a flag the moment something is blocked or needs follow-up — don't hold it in
your head. Resolve with a note saying *how*, so the trail is legible. Open flags
on a feature are its blockers; clear them before calling the feature done.
