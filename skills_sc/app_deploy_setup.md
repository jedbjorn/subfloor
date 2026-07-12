---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# app_deploy_setup

Admin-run, one-time scaffold — turn the shipped deploy template into this repo's own project-local `deploy` skill (migration dirs, DB backup, ff-only sync, apply + move migrations, restart), then grant it to every shell.

**Category:** substrate

---

# app_deploy_setup — scaffold this app's deploy ritual (once, admin)

The engine deploys itself (`sc update`); the host app's deploy — app process,
app DB, app migrations — is the fork's own. Fill the template below with this
app's specifics and save it as a NEW project-local `deploy` skill.

NEVER save the result by editing this skill: engine skills self-heal on every
`sc update` — a fork edit to any skill named in `assets/skills/` is detected
as stale and reverted to the shipped body. A project-local name (one the
engine doesn't ship) is never touched and persists through rebuilds via
`sc snapshot` -> `.sc-state/content.sql`. Leave this scaffold as shipped.

## 1. Scaffold the migration dirs

```bash
mkdir -p migrations_app/pending migrations_app/completed
touch migrations_app/pending/.gitkeep migrations_app/completed/.gitkeep
```

Commit them. Renaming to fit the repo's layout (`db/migrations/…`,
`deploy/migrations/…`) is fine -> keep `pending/` + `completed/` as siblings
and use the same paths in the template. These hold the APP's schema
migrations — NOT `.super-coder/migrations/` (engine DB, ledger-tracked, owned
by `sc update`).

## 2. Fill the template

Every `⟨ADMIN: …⟩` slot is app-specific — get it from the operator or the
repo. Run each command once by hand before writing it in; an untested command
does not enter a deploy skill.

```markdown
# deploy — ⟨ADMIN: app name⟩ post-merge deploy ritual

Run from the repo root on the host. Every step aborts loudly rather than
guessing; if a step fails, stop — the app is down and the DB is backed up.

1. **Down** — stop the app:
   ⟨ADMIN: stop command — e.g. pm2 stop ecosystem.config.cjs / systemctl stop <app> / docker compose down⟩

2. **Backup** — snapshot the app DB before anything mutates:
   ⟨ADMIN: backup command + destination + how many to retain⟩

3. **Sync main** — `git switch main` (if on a branch), then `git pull --ff-only`.
   `--ff-only` aborts on a diverged or dirty main — resolve by hand; never
   merge inside a deploy.

4. **Migrate** — apply every file in `migrations_app/pending/` in sort order:
   ⟨ADMIN: apply command per file — e.g. psql "$DB_URL" -f <file> / sqlite3 <db> < <file> / alembic upgrade head⟩
   After each success: `git mv migrations_app/pending/<file> migrations_app/completed/`
   On first failure: stop, restore the backup, investigate.

5. **Record** — commit and push the moves — the move IS the applied-ledger,
   and an uncommitted move dirties main and breaks the NEXT deploy's --ff-only:
   `git add migrations_app && git commit -m "deploy: apply <files>" && git push`

6. **Up** — restart the app:
   ⟨ADMIN: start command⟩

7. **Verify** — prove the new code is serving:
   ⟨ADMIN: health check — e.g. curl -fsS http://127.0.0.1:<port>/health⟩
```

## 3. Save as a project-local skill

Run both SQL blocks via `./sc sql-rw "<SQL>"` — `sc sql` is read-only and
refuses writes.

```sql
INSERT INTO skills (name, description, category, content, common)
VALUES ('deploy',
        'Post-merge deploy ritual for this app — down, backup, ff-only sync, migrate pending→completed, restart, verify.',
        'substrate',
        '<the filled template>',
        1);
```

`common=1` = grant-to-every-shell: new shells receive it at creation, and
`sc update` re-grants every common skill to every live shell. Grant existing
shells now, without waiting for an update:

```sql
INSERT OR IGNORE INTO shell_skills (shell_id, skill_id)
SELECT s.shell_id, k.skill_id FROM shells s, skills k
WHERE COALESCE(s.is_deleted,0)=0 AND k.name='deploy' AND k.is_deleted=0;
```

Then `sc snapshot` -> the skill + grants persist in `.sc-state/content.sql`.

## 4. Optional make surface

Operator wants make muscle-memory -> add a bare `deploy` target to the repo's
own root Makefile (the fork's convention space). NEVER add it to
`.super-coder/aliases.mk` — engine-owned; every target there must delegate to
`./sc`, and the engine knows nothing about the app.

## 5. Done

Dry-run the ritual end-to-end once in a quiet window -> all 7 steps pass
before any shell relies on it. This scaffold stays granted to admin only; the
finished `deploy` skill is the one every shell carries.
