---
name: app_deploy_setup
description: Admin-run, one-time scaffold — turn the shipped deploy template into this repo's own project-local `deploy` skill (migration dirs, DB backup, ff-only sync, apply + move migrations, restart), then grant it to every shell.
category: substrate
common: false
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

Persist the filled template through the `local_skill_management` path — the
ONE authoring lane for fork-local skills (#321: hand-rolled `sc sql-rw`
INSERTs leave no asset file to re-seed from and contradict that skill's
contract in the same catalogue):

1. Write the asset file at `.super-coder/assets/skills/deploy/SKILL.md` —
   frontmatter carries the identity; body = the filled template:

   ```markdown
   ---
   name: deploy
   description: Post-merge deploy ritual for this app — down, backup, ff-only sync, migrate pending→completed, restart, verify.
   category: substrate
   common: true
   ---
   <the filled template>
   ```

   `common: true` = grant-to-every-shell: new shells receive it at creation,
   and `sc update` re-grants every common skill to every live shell.

2. Seed it into the catalogue + grant it live: `sc seed-skills` (upserts the
   asset into the DB, grants common skills to every live shell).

3. Persist: `SC_ADMIN=1 sc snapshot` → the skill + grants survive in
   `.sc-state/content.sql`; commit per that skill's steps.

Details, updates, and removal: the `local_skill_management` skill.

## 4. Optional make surface

Operator wants make muscle-memory -> add a bare `deploy` target to the repo's
own root Makefile (the fork's convention space). NEVER add it to
`.super-coder/aliases.mk` — engine-owned; every target there must delegate to
`./sc`, and the engine knows nothing about the app.

## 5. Done

Dry-run the ritual end-to-end once in a quiet window -> all 7 steps pass
before any shell relies on it. This scaffold stays granted to admin only; the
finished `deploy` skill is the one every shell carries.
