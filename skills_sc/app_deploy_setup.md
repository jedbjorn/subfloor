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

The engine deploys itself (`sc update`). The HOST APP this fork lives in has
its own deploy story — app process, app DB, app migrations — that the engine
cannot know. This skill turns the template below into the repo's own `deploy`
skill, filled in with this app's specifics.

**Why a NEW project-local skill instead of editing this one:** engine skills
self-heal on every `sc update` — a fork edit to any skill named in
`assets/skills/` is detected as stale and reverted to the shipped body. A
project-local skill (a name the engine doesn't ship) is never touched by that
guard and persists through rebuilds via `sc snapshot` → `.sc-state/content.sql`.
Fill in the template, save it under a NEW name, leave this scaffold as shipped.

## 1. Scaffold the migration dirs

```bash
mkdir -p migrations_app/pending migrations_app/completed
touch migrations_app/pending/.gitkeep migrations_app/completed/.gitkeep
```

Commit them. Rename to fit the repo's layout if you like (`db/migrations/…`,
`deploy/migrations/…`) — keep `pending/` and `completed/` as siblings and use
the same paths in the template. These are the APP's schema migrations —
unrelated to `.super-coder/migrations/` (engine DB, ledger-tracked, owned by
`sc update`).

## 2. Fill the template

Every `⟨ADMIN: …⟩` slot is app-specific. Get each answer from the operator or
the repo itself, and **run each command once by hand** before writing it in —
a deploy skill is no place for untested commands.

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

## 3. Save it as a project-local skill

Both SQL blocks below run via `./sc sql-rw "<SQL>"` — the explicit read-write
passthrough (`sc sql` is read-only and refuses writes).

```sql
INSERT INTO skills (name, description, category, content, common)
VALUES ('deploy',
        'Post-merge deploy ritual for this app — down, backup, ff-only sync, migrate pending→completed, restart, verify.',
        'substrate',
        '<the filled template>',
        1);
```

`common=1` is the "grant to every shell" switch: new shells receive it at
creation, and `sc update` re-grants every common skill to every live shell.
Grant existing shells now without waiting for an update:

```sql
INSERT OR IGNORE INTO shell_skills (shell_id, skill_id)
SELECT s.shell_id, k.skill_id FROM shells s, skills k
WHERE COALESCE(s.is_deleted,0)=0 AND k.name='deploy' AND k.is_deleted=0;
```

Then persist: `sc snapshot` (project-local skills + grants live in
`.sc-state/content.sql`).

## 4. Optional make surface

If the operator wants make muscle-memory, add a bare `deploy` target to the
**repo's own root Makefile** — that is the fork's convention space. Do NOT add
it to `.super-coder/aliases.mk`: that file is engine-owned, every target must
delegate to `./sc`, and the engine knows nothing about the app.

## 5. Done

Dry-run the ritual once on a quiet window end-to-end. This scaffold stays
granted to admin only; the finished `deploy` skill is the one every shell
carries.
