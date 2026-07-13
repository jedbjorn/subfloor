-- 0066 — reseed: skills audit batch (#319/#320/#321/#322 + db_map decisions note).
--
-- Eleven skills updated from the FnB-commissioned dos-arch catalogue audit:
--
--   flag_sweep            — frozen-doc signal counts ANY frozen document
--                           (kind='doc' froze docs got false "undocumented"
--                           positives under the spec-only count, #319)
--   test_authoring_pg     — de-forked (#320): pattern + defer to the fork's
--   test_authoring_sqlite   conftest; example rosters marked as examples;
--                           fork names and fork-infra assertions stripped
--   app_deploy_setup      — §3 persists the deploy skill via the
--                           local_skill_management path, not raw sc sql-rw
--                           INSERTs (#321)
--   local_skill_management, migration_management — SC_ADMIN=1 on snapshot/
--                           render commands that refuse without it (#322.1)
--   review                — dangling "README model note" ref inlined (#322.2)
--   messaging             — launch token in the catalogue command column (#322.3)
--   db_map                — doctrine blesses the read-only `sc sql` lane for
--                           admin/reporting reads (#322.4); notes decisions
--                           read fleet-wide (#318/#340 companion)
--   database-migrations   — Postgres lock-safety section: CONCURRENTLY,
--                           volatile-default rewrites, enum alters,
--                           lock_timeout (#322.5)
--   cartographer          — `sc mem oriented` does NOT snapshot (#322.6;
--                           ground truth: the API route sets bootstrapped=1
--                           and nothing else)
--
-- Source assets updated in the same commit; this trailing forward reseed
-- (UPSERT by name; skill_id + grants preserved) carries them to installed
-- forks and fresh builds alike.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'flag_sweep',
  'Admin''s every-session flag reconciliation — auto-close flags whose gating work is provably done, open ship flags for implemented-but-unshipped specs and docs-pending flags for shipped features that lack a doc (message the planner), surface judgment calls to the FnB. Step 1 of the admin standing pass; run before git_cleanup.',
  'substrate',
  NULL,
  0,
  '# flag_sweep — reconcile flags against state

Admin-only. Leg 1 of the standing every-session pass -> then `git_cleanup` ->
then optional `local_skill_management`. Working shells close the flags their
own work clears (boot doc, "Finish before you stop"); this sweep is the
backstop for the stragglers they dropped + the docs nobody opened a flag for.
Two directions: close what''s provably resolved, open what''s provably missing.

`<self>` = your shell_id. Resolve the planner once up front:

```sql
SELECT shortname FROM shells WHERE flavor=''planner'' AND COALESCE(is_deleted,0)=0;
-- no planner in this fork → surface to the FnB instead of messaging.
```

---

## Step 1: Load the open flags with their state

```sql
SELECT f.flag_id, f.display_name, f.priority, f.description,
       f.feature_id, r.title AS feature, r.roadmap_status,
       (SELECT COUNT(*) FROM documents d
        WHERE d.feature_id = f.feature_id AND d.frozen=1) AS frozen_docs
FROM flags f
LEFT JOIN roadmap r ON r.feature_id = f.feature_id
WHERE f.resolved=0 AND COALESCE(f.is_deleted,0)=0
ORDER BY f.priority, f.flag_id;
```

`frozen_docs` counts ANY frozen document on the feature — kind=''spec'' AND
kind=''doc'' both qualify (#319: forks that freeze kind=''doc'' rows for shipped
docs got false "undocumented" positives every sweep under a spec-only count).

Sort every open flag into exactly one bucket (Step 2 / Step 4). Auto-close
only on unambiguous evidence — any doubt -> Step 4, not a close.

---

## Step 2: Auto-close the deterministic ones

Close with `sc mem flag close <flag_id> --notes "…"`. The note MUST cite the
evidence.

**A. Docs-pending flag, doc now exists** = `[Docs] … docs pending` flag on a
feature with `frozen_docs > 0`:
```
sc mem flag close <flag_id> --notes "Auto: frozen spec doc now exists for feature #<id> (flag_sweep)."
```

**B. Ship-blocker, feature now shipped** = flag of the form
`… | Blocker for: <X>` + linked feature''s `roadmap_status` is `shipped` (or
later) + the flag text is about that feature shipping / becoming available. A
separate concern that merely hangs off the same feature does NOT qualify:
```
sc mem flag close <flag_id> --notes "Auto: blocking feature #<id> (<title>) now shipped (flag_sweep)."
```

**C. Ship-drift flag, now shipped AND documented** = `[Ship] … not marked
shipped` flag (opened by Step 3A) covers two halves — mark shipped + reconcile
the doc — so close only when BOTH hold: `roadmap_status` is `shipped` (or
later) + `frozen_docs > 0`. Shipped-but-undocumented -> leave open:
```
sc mem flag close <flag_id> --notes "Auto: feature #<id> (<title>) now shipped with a frozen doc (flag_sweep)."
```

NEVER message on close (per the `flags` skill — messages pair with `open`).
NEVER reopen a flag. A close whose evidence you had to infer -> Step 4.

---

## Step 3: Open the flags nobody opened

Two gaps drop silently, in sequence: 3A (done but never marked shipped)
precedes 3B (shipped but undocumented) — a feature exits 3A before 3B can
apply. Pick `SC-###` for any open below = next free id
(`SELECT display_name FROM flags ORDER BY flag_id DESC LIMIT 5;`).

### 3A — Implemented but not marked shipped (ship-drift)

The dev flips the horizon to `shipped` when Verification passes (`spec` skill,
hand-off step) — the flip sometimes gets missed. Deterministic signal = spec''s
**Verification task `done`** + feature **not** `shipped`. Open a durable
`[Ship]` flag — it governs both halves of the dropped hand-off (mark shipped +
reconcile the doc to the spec) and stays open until a planner does both.

```sql
-- specs finished (Verification done) on features still short of shipped, with no open ship/docs flag:
SELECT DISTINCT r.feature_id, r.title, r.roadmap_status
FROM roadmap r
JOIN documents d   ON d.feature_id = r.feature_id AND d.kind=''spec''
JOIN spec_tasks t  ON t.document_id = d.document_id AND t.title=''Verification'' AND t.status=''done''
WHERE r.roadmap_status NOT IN (''shipped'',''retired'')
  AND NOT EXISTS (
    SELECT 1 FROM flags f
    WHERE f.feature_id = r.feature_id AND f.resolved=0 AND COALESCE(f.is_deleted,0)=0
      AND (f.description LIKE ''%not marked shipped%'' OR f.description LIKE ''%docs pending%''));
```

Per row: open + message the planner (no planner -> surface to the FnB) — same
contract as the `flags` skill:

```
sc mem flag open "[Ship] <title> implemented, not marked shipped | Blocker for: <title> ship + doc" --name SC-### --priority Medium --feature <feature_id>
sc mem message send <planner-shortname> "flag_sweep: <title> (#<feature_id>) — Verification done but still <status>; SC-### opened to mark shipped + reconcile docs to spec."
```

### 3B — Shipped but undocumented (docs-pending)

Devs open a docs-pending flag when they ship — sometimes skipped. Find
`shipped` features with no frozen doc + no open docs-pending flag; open one
per row. (Finished-but-not-shipped is 3A''s job, not this one.)

```sql
-- shipped features with no frozen doc and no open docs-pending flag:
SELECT r.feature_id, r.title, r.roadmap_status
FROM roadmap r
WHERE r.roadmap_status = ''shipped''
  AND NOT EXISTS (
    SELECT 1 FROM documents d
    WHERE d.feature_id = r.feature_id AND d.frozen=1)
  AND NOT EXISTS (
    SELECT 1 FROM flags f
    WHERE f.feature_id = r.feature_id AND f.resolved=0 AND COALESCE(f.is_deleted,0)=0
      AND f.description LIKE ''%docs pending%'');
```

Per row: open + message the planner (no planner -> surface to the FnB) — same
contract as the `flags` skill:

```
sc mem flag open "[Docs] <title> shipped, doc pending | Blocker for: <title> doc" --name SC-### --priority Medium --feature <feature_id>
sc mem message send <planner-shortname> "flag_sweep: <title> (#<feature_id>) is shipped with no doc — SC-### opened, ready to freeze + document."
```

---

## Step 4: Surface the rest — don''t guess

Everything that isn''t a clean Step-2 close / Step-3 open -> short list to the
FnB (no `send` unless a specific shell owns it): review-failure flags (author
dev closes those when the fix lands), FnB-decision flags, blockers whose
resolution you can''t verify from state, anything ambiguous. One line each:

> `SC-042` [High] — <description> · feature #N at <status> · *why I didn''t auto-act*

The FnB or the owning shell closes these with a real note. Auto-act ONLY on
unambiguous evidence.

---

## Stance

- **Deterministic-only auto-close.** Evidence in the DB + cited in the note,
  or it surfaces. A wrongly-closed live blocker is worse than a straggler.
- **Backstop, not owner.** The shell that did the work closes its own flag
  with the richer "how" note; don''t race to close a flag whose owner is still
  active on that feature.
- **Both directions, every session.** An implemented-but-unshipped spec and an
  undocumented shipped feature are dropped handoffs; the signal is already in
  the DB (a `done` Verification task, a missing frozen doc) — surfacing them
  is deterministic.
- **Then `git_cleanup`.** flag_sweep is leg 1 of the pass, not the whole pass.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'test_authoring_pg',
  'Postgres test infrastructure for postgres-backed forks — throwaway DB, Alice/Bob tenants, psycopg 3 direct assertions. Read alongside test_authoring for the rules.',
  'craft',
  NULL,
  0,
  '# test_authoring_pg — Postgres test infra

Rules live in `test_authoring` — read it alongside. This skill = the test
infrastructure PATTERN for Postgres-backed forks.

**Your fork''s `tests/conftest.py` is the source of truth** for the throwaway
DB''s naming, what schema artifact seeds it (a live `schema.sql`, a squash, a
migration replay), and the fixture roster — read it before writing a test.
Everything below is the typical shape, not a contract; where your conftest
differs, the conftest wins. A fork may also ship its own superseding
test-authoring skill — if one is granted, prefer it.

## Foundation (typical shape)

`tests/conftest.py` creates a throwaway Postgres DB at session start, applies
the fork''s schema artifact, seeds two tenants (Alice / Bob) + a shared system
shell, and drives the real app through `TestClient` with real auth.

**Key identities (an example roster — confirm against your conftest):**

| Name | Kind | ID |
|---|---|---|
| `USER_ADMIN` | admin user | 1 |
| `USER_A` / Alice | tenant user | 10 |
| `USER_B` / Bob | tenant user | 20 |
| `SHELL_SHARED` | shared system shell | 100 |
| `SHELL_A` / `SHELL_B` | per-tenant shells | 101 / 102 |
| `PROJ_A` / `PROJ_B` | per-tenant projects | 500 / 501 |
| `KEY_A` / `KEY_B` | shell bearer keys | `"ALICEKEY"` / `"BOBKEY"` |

**Throwaway DB setup:**
- An admin connection (`psycopg.connect(DATABASE_URL_ADMIN, autocommit=True)`)
  creates a uniquely-named `<fork>_test_<unique>` database at session start
  and drops it at session teardown — the naming scheme is the conftest''s.
- `DATABASE_URL` injected via `os.environ["DATABASE_URL"]` BEFORE importing
  the app — the app''s DB layer reads it at import time.
- The fork''s schema artifact applied on the throwaway connection — which
  artifact (postgres `schema.sql`, a schema squash, a migration replay) is a
  per-fork choice; read the conftest, don''t assume.
- Some forks isolate egress/spend rows in a second throwaway DB/schema —
  only if your conftest declares one.

**Callers** — same `Caller` pattern as the SQLite variant; identity carried
via cookie or `Authorization: Bearer` header:
```python
alice   # session-cookie caller, USER_A identity
bob     # session-cookie caller, USER_B identity
admin   # session-cookie caller, USER_ADMIN identity
anon    # no auth
shell_a # bearer-key caller, KEY_A
shell_b # bearer-key caller, KEY_B
```

**TestClient:**
- Create WITHOUT a `with` block -> skips startup hooks (catalogue / model
  sync) that would hit the network.
- `scope="session"` -> one DB shared across the whole run. A test needing
  isolation seeds its own fixture rows + cleans up explicitly.

**Direct DB assertions:**
```python
import os, psycopg
from psycopg.rows import dict_row
con = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row)
cur = con.cursor()
cur.execute("SELECT * FROM table WHERE ...")
rows = cur.fetchall()
```
Assert against real rows, not the response payload.

**Mocking boundary:** mock only true external egress — outbound HTTP, broker
calls, third-party APIs. NEVER mock the router, the DB layer, or the
function under test.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'test_authoring_sqlite',
  'SQLite test infrastructure for super-coder-style forks — throwaway DB, Alice/Bob tenants, Caller/TestClient. Read alongside test_authoring for the rules.',
  'craft',
  NULL,
  0,
  '# test_authoring_sqlite — SQLite test infra

Rules live in `test_authoring` — read it alongside. This skill = the test
infrastructure PATTERN for SQLite-backed forks.

**Your fork''s `tests/conftest.py` is the source of truth** for how the
throwaway DB is built and what fixtures exist — read it before writing a
test. Everything below is the typical shape, not a contract; where your
conftest differs, the conftest wins. A fork may also ship its own superseding
test-authoring skill — if one is granted, prefer it.

## Foundation (typical shape)

`tests/conftest.py` builds a throwaway SQLite DB from the fork''s schema
artifact (schema.sql + a migration replay, or a squash), seeds two tenants
(Alice / Bob) + a shared system shell, and drives the real app through
`TestClient` with real auth.

**Key identities (an example roster — confirm against your conftest):**

| Name | Kind | ID |
|---|---|---|
| `USER_ADMIN` | admin user | 1 |
| `USER_A` / Alice | tenant user | 10 |
| `USER_B` / Bob | tenant user | 20 |
| `SHELL_SHARED` | shared system shell | 100 |
| `SHELL_A` / `SHELL_B` | per-tenant shells | 101 / 102 |
| `PROJ_A` / `PROJ_B` | per-tenant projects | 500 / 501 |
| `KEY_A` / `KEY_B` | shell bearer keys | `"ALICEKEY"` / `"BOBKEY"` |

**Throwaway DB setup:**
- `tempfile.NamedTemporaryFile(suffix=".db")` -> path injected via
  `os.environ["SHELL_DB_PATH"]` BEFORE importing the app — the auth
  middleware calls `db()` directly; a `Depends` override alone misses it.
- The conftest''s schema builder (e.g. `apply_schema_and_migrations(con)`)
  builds the throwaway DB — single source shared by all test harnesses;
  NEVER copy-paste it.
- Some forks isolate egress/spend rows in a second throwaway DB — only if
  your conftest declares one.
- `os.environ.setdefault("AUTH_COOKIE_SECURE", "")` -> plain `dsess` cookie,
  no `__Host-` prefix in tests.

**Callers** — all pytest fixtures:
```python
alice   # session-cookie caller, USER_A identity
bob     # session-cookie caller, USER_B identity
admin   # session-cookie caller, USER_ADMIN identity
anon    # no auth
shell_a # bearer-key caller, KEY_A
shell_b # bearer-key caller, KEY_B
```
`shell_a` / `shell_b` send `Authorization: Bearer`.

**TestClient:**
- Create WITHOUT a `with` block -> skips startup hooks (catalogue / model
  sync) that would hit the network.
- `scope="session"` -> one DB shared across the whole run. Never depend on a
  clean DB; a test needing isolation seeds its own via
  `build_substrate_db()` (in-memory, returns a `sqlite3.Connection`).

**Direct DB assertions:**
```python
import sqlite3, os
con = sqlite3.connect(os.environ["SHELL_DB_PATH"])
con.row_factory = sqlite3.Row
rows = con.execute("SELECT * FROM table WHERE ...").fetchall()
```
Assert against real rows, not the response payload. The throwaway path is
stable for the lifetime of the test session.

**Mocking boundary:** mock only true external egress — outbound IMAP, HTTP,
broker calls. NEVER mock the router, the DB layer, or the function under
test.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'app_deploy_setup',
  'Admin-run, one-time scaffold — turn the shipped deploy template into this repo''s own project-local `deploy` skill (migration dirs, DB backup, ff-only sync, apply + move migrations, restart), then grant it to every shell.',
  'substrate',
  NULL,
  0,
  '# app_deploy_setup — scaffold this app''s deploy ritual (once, admin)

The engine deploys itself (`sc update`); the host app''s deploy — app process,
app DB, app migrations — is the fork''s own. Fill the template below with this
app''s specifics and save it as a NEW project-local `deploy` skill.

NEVER save the result by editing this skill: engine skills self-heal on every
`sc update` — a fork edit to any skill named in `assets/skills/` is detected
as stale and reverted to the shipped body. A project-local name (one the
engine doesn''t ship) is never touched and persists through rebuilds via
`sc snapshot` -> `.sc-state/content.sql`. Leave this scaffold as shipped.

## 1. Scaffold the migration dirs

```bash
mkdir -p migrations_app/pending migrations_app/completed
touch migrations_app/pending/.gitkeep migrations_app/completed/.gitkeep
```

Commit them. Renaming to fit the repo''s layout (`db/migrations/…`,
`deploy/migrations/…`) is fine -> keep `pending/` + `completed/` as siblings
and use the same paths in the template. These hold the APP''s schema
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
   and an uncommitted move dirties main and breaks the NEXT deploy''s --ff-only:
   `git add migrations_app && git commit -m "deploy: apply <files>" && git push`

6. **Up** — restart the app:
   ⟨ADMIN: start command⟩

7. **Verify** — prove the new code is serving:
   ⟨ADMIN: health check — e.g. curl -fsS http://127.0.0.1:<port>/health⟩
```

## 3. Save as a project-local skill

Persist the filled template through the `local_skill_management` path — the
ONE authoring lane for fork-local skills (#321: hand-rolled `sc sql-rw`
INSERTs leave no asset file to re-seed from and contradict that skill''s
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
   `.sc-state/content.sql`; commit per that skill''s steps.

Details, updates, and removal: the `local_skill_management` skill.

## 4. Optional make surface

Operator wants make muscle-memory -> add a bare `deploy` target to the repo''s
own root Makefile (the fork''s convention space). NEVER add it to
`.super-coder/aliases.mk` — engine-owned; every target there must delegate to
`./sc`, and the engine knows nothing about the app.

## 5. Done

Dry-run the ritual end-to-end once in a quiet window -> all 7 steps pass
before any shell relies on it. This scaffold stays granted to admin only; the
finished `deploy` skill is the one every shell carries.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'local_skill_management',
  'Create, persist, assign, and remove fork-specific skills — the correct authoring path so skills survive snapshot/rebuild cycles.',
  'substrate',
  NULL,
  0,
  '# local_skill_management — fork-specific skills that survive

Fork-specific skills live in the DB and persist via `.sc-state/content.sql`
(the snapshot). The asset file under `.super-coder/assets/skills/<name>/` is
the **authoring source only** — it sits in gitignored engine territory, and
that is safe: the engine/local boundary is the seed migration (0001,
upstream-owned in a fork), not asset-file presence. The snapshot serializes
your skill to content.sql whether or not the asset file is kept, and
`sc update` neither manifests it nor heals over its DB row. **content.sql =
the durable form; the asset file = your editor.**

The path: **file -> seed -> grant -> snapshot -> commit**.

## Creating a fork-specific skill

1. **Write the skill file** at `.super-coder/assets/skills/<name>/SKILL.md`.

   Required frontmatter:
   ```yaml
   ---
   name: skill_name
   description: One-line summary — shown in boot, catalogue, and the GUI Skills tab
   category: substrate   # or craft; omit for default
   ---
   ```
   Body: markdown procedure the shell will follow. Imperative, compressed —
   this boots into context.

2. **Seed into the live DB:**
   ```bash
   sc seed-skills
   ```
   UPSERTs every asset skill by name (id-stable) and reports what landed. In a
   fork it deliberately does NOT regenerate the seed migration — that file is
   upstream-owned engine territory. DB skills with no asset file = other local
   skills, left intact.

3. **Grant to target shell(s)** — by shell id or shortname:
   ```bash
   sc skill grant <skill_name> <shell>...
   ```
   Unknown skill/shell names = hard error (no silent no-op grants).
   `sc skill list` = catalogue with origins + current grants;
   `sc skill revoke <name> <shell>...` reverses a grant.

4. **Snapshot — the persistence step:**
   ```bash
   SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render
   ```
   `snapshot.py` serializes local skills (any skill the engine seed doesn''t
   own) into `.sc-state/content.sql` — what survives `sc update` and
   `sc rebuild`; the row + grants reconstruct from content.sql. Skip this ->
   the skill is lost on next update.

5. **Commit.** Run `sc render-check` first — hermetic rebuild, fails if the
   `skills_sc/` mirror drifts from the DB render (the CI guard; see the
   `snapshot` skill). Then stage `.sc-state/content.sql` + `skills_sc/`
   together — snapshot without re-rendered mirror = the drift.

## Updating a skill

Edit the asset file -> repeat seed -> snapshot -> commit (steps 2, 4, 5).
Asset file gone (removed / authored elsewhere) -> recreate it from the DB body
first: `sc sql "SELECT content FROM skills WHERE name=''<name>''"`.

## Assigning an existing skill to additional shells

```bash
sc skill grant <skill_name> <shell>...
```
Then `SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render` + commit.

## Removing a skill

1. **Soft-delete the row + revoke its grants:**
   ```bash
   sc skill rm <skill_name>
   ```
   Refuses engine skills — the seed resurrects those on next update/rebuild.
   Engine skill this fork has superseded -> retire fork-wide:
   `sc skill retire <name>` (writes the tracked
   `.sc-state/skills_retired.json`, which rides updates; `sc skill unretire`
   reverses). Per-shell removal -> `sc skill revoke`.

2. **Remove the asset file** (`.super-coder/assets/skills/<name>/`) —
   otherwise the next `sc seed-skills` re-inserts the skill.

3. **Snapshot, render, commit:**
   ```bash
   SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render
   ```

## How the GUI organizes skills

The review GUI Skills tab shows the full catalogue in sections with per-shell
grant toggles; the Shells tab groups its grant list by the same sections.

- **Repo skills** — lead section: skills authored in this fork. Membership is
  *derived* — a skill the engine seed doesn''t own is repo-local. Same rule
  snapshot.py uses to decide what serializes into `.sc-state/content.sql`, so
  the section shows exactly what the snapshot keeps durable. No frontmatter
  flag exists or is needed.
- **Substrate / Craft / …** — engine skills, sectioned by `category`
  frontmatter. A repo skill''s `category` displays as a row label but never
  moves it out of the Repo section.

GUI grant toggles hit the same DB table as `sc skill grant` — they still need
a snapshot (header button or `SC_ADMIN=1 sc snapshot`) to survive a rebuild.

## What NOT to do

- **NEVER skip the snapshot after creating a skill.** Seeding writes the live
  DB only; content.sql is what survives `sc update` and `sc rebuild`.
- **NEVER edit `0001_seed_skills.sql` by hand.** Generated, and in a fork
  upstream-owned engine territory — a local edit blocks the next update.
- **NEVER create skills via the GUI.** Toggling grants there is fine (snapshot
  after); creating is not — the GUI writes only the DB and cannot write the
  asset file or seed it. Use this procedure.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'migration_management',
  'Author and apply fork-specific DB schema migrations — naming, format, how to apply locally and verify.',
  'substrate',
  NULL,
  0,
  '# migration_management — fork-specific schema changes

Migrations live in `.super-coder/migrations/`, apply in numeric order, tracked
by the `schema_migrations` ledger table. Engine updates apply pending
migrations automatically; apply locally without a fetch via
`sc update --no-fetch`.

**Scope:** fork-specific changes — tables, columns, constraints, or
system-content seeds (skills, flavor defaults) this fork needs that will not
ship upstream. Upstream engine migrations arrive via `sc update`; no action
from you.

## Authoring a migration

1. **Find the next number:**
   ```bash
   ls .super-coder/migrations/ | sort | tail -5
   ```
   Name the file `NNNN_<slug>.sql`, NNNN = next integer zero-padded to 4
   digits (e.g. `0012`).

2. **Write the file** at `.super-coder/migrations/NNNN_<slug>.sql`:
   - Wrap in `BEGIN; ... COMMIT;`
   - Idempotent: `CREATE TABLE IF NOT EXISTS`, `INSERT OR IGNORE`,
     `CREATE INDEX IF NOT EXISTS`, `DROP TABLE IF EXISTS` before recreate
   - Comment header: migration number + intent (+ doctrine notes if relevant)
   - Structure + system content only — per-instance data (shell memory,
     grants, roadmap, flags) lives in `.sc-state/content.sql` via snapshot,
     never in migrations

3. **Apply locally:**
   ```bash
   sc update --no-fetch
   ```
   Skips the upstream fetch; applies all pending local migrations in order.
   Confirm it landed:
   ```sql
   SELECT * FROM schema_migrations ORDER BY applied_at DESC LIMIT 5;
   ```

4. **Verify:**
   ```bash
   sc verify
   ```
   Headless boot proof — shells, memory, and schema intact.

5. **Snapshot + commit:**
   ```bash
   SC_ADMIN=1 sc snapshot
   ```
   Commit `.sc-state/content.sql` + `migrations/NNNN_<slug>.sql`.
   - **Content-seed migration** (seeds system content that renders — skills,
     flavor defaults) also changes the flat `_sc` mirrors, but only once the
     new rows are in the DB: after `sc update --no-fetch`, run
     `SC_ADMIN=1 sc render && sc render-check` and commit the re-rendered `_sc` files
     alongside the migration. A render against a DB predating the seed passes
     locally while CI''s hermetic rebuild goes red — the stale-mirror trap; see
     the `snapshot` skill.

## What makes a good migration

- **Additive by default.** Add columns/tables/indexes. No DROP or RENAME
  unless correcting a prior mistake; prefer a new column over renaming one
  code may reference.
- **No per-instance content.** Shell memory, skill grants, roadmap items,
  flags -> snapshot. Migrations carry structure + system content that
  propagates to all forks.
- **Comment the why** — future readers need the intent, not just the SQL.

## Rollback

No per-migration rollback. `sc rollback` restores the full DB + engine to the
prior update point (`engine.ref.prev`). Use only when a migration is so broken
the DB is corrupt or the app won''t start; for logical errors, write a
corrective migration instead.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'review',
  'Reviewer procedure — read a diff against its spec along three axes (code quality, edge cases & gaps, spec conformance), open flags for failures, then propose the handoff (fixes to dev / new spec to planner) to the FnB and send it only on approval. The reviewer''s top-level loop; the lenses live in the skills it points to. Load when reviewing a dev''s work.',
  'craft',
  NULL,
  0,
  '# review — gate a diff against its spec

The reviewer''s job end to end. You are a **different lineage than the code**
— reviewer shells are deliberately booted on a different model family than
the authoring dev, so the review doesn''t share the author''s blind spots ->
read adversarially: disprove the claim that the work is correct, don''t
confirm it. `<self>` = your shell_id.

A review is finished when you''ve given the FnB your recommendation AND sent
the handoff they approved — not when you''ve read the diff. Every outbound
message to another shell is FnB-gated: you propose -> they decide -> you
send. Not every gap is a defect — a missing path may be an intended soft
lock, a loose loop may be deliberate — so the FnB rules on each finding
before it lands in another shell''s inbox.

---

## Step 1: Load the diff and its spec

Review a diff *against intent*, never in a vacuum. Get both:

- The change: the PR diff, or `git -C <author-worktree> diff origin/main...<branch>`.
- The spec it was built to: the feature''s spec doc (`spec` skill, Step 1 —
  `documents` where `kind=''spec''`). Its done-condition = your yardstick.

Note the **author** — Step 4 proposes a handoff to them. Resolve their
shortname from the branch (`shell/<shortname>`) or the commit trailer
(`Co-Authored-By: <display_name> (super-coder)`); the roster maps
display_name -> shortname:
```
sc mem get shells
```

## Step 2: Review along the three axes

**Agents overlay:** this shell granted `agents` + FnB invoked `--agents` ->
that skill''s overlay fans this step out to an adversarial finding-panel.
Load it and apply it on top of this step. Steps 1, 3, and 4 stay yours,
unchanged.

Apply every axis on every review, plus the granted *lenses* matching what
the diff touches:

1. **Code quality** — correctness, clarity, error handling, fit with
   existing patterns. Trace the actual code path; NEVER trust the
   description of it.
2. **Edge cases & gaps** — inputs and states the author didn''t handle:
   empty, null, boundary, concurrent, partial-failure, the unhappy path.
   Name what''s missing, not only what''s wrong.
3. **Spec conformance** — diff vs the spec''s done-condition. Flag where the
   implementation diverges from intent AND where the spec itself was silent
   or wrong.

| Diff touches | Lens |
|---|---|
| an API / endpoint / route | `api-design` → *Review lens* |
| `tests/` | `test_authoring` → *Review lens* |
| schema / migration | `database-migrations` |
| a redline / UI change | `redline_review` |

A granted skill that declares it supersedes a lens (says so in its
description — e.g. a fork-local testing skill superseding `test_authoring`)
-> use the superseding skill: it carries the fork''s actual standard.

## Step 3: Open a flag per failure — record, don''t send yet

One flag per real failure, against the feature:
```
sc mem flag open "[Review] <what''s wrong> | Blocker for: <feature>" --name SC-### --priority <High|Medium|Low> --feature <feature_id>
```
Unlike the `flags` skill''s default: do NOT pair an outbound message here —
the message is the handoff, and handoffs wait for the FnB (Step 4). Nits go
in the summary, not flags; flag only what blocks merge.

## Step 4: Propose the handoff to the FnB — send on approval

Recommendation -> the handoff it implies:

- fixes on the diff -> message to the **author dev**
- a missing or wrong spec -> message to the **planner**
- clean -> nothing to send

Present the findings (flags + summary) and the drafted message(s) to the
FnB. The FnB rules each finding — defect or intended — and approves what
sends. Then, and only then, send:
```
# fixes (FnB-approved):
sc mem message send <author-shortname> "Review of <feature> done — <N> flags: SC-###, SC-###. Patch + re-push; thread closes when clean."

# new/updated spec (FnB-approved):
sc mem message send <planner-shortname> "Review of <feature> surfaced a spec gap — <one line>. Proposing a spec update; see SC-###."

# clean: report to the FnB; no handoff to send.
```

---

## Stance

- **Adversarial by default.** You are the gate — assume there''s a bug and
  find it; "looks fine" is not a review.
- **Verify, don''t trust.** Re-read the claim against the code; trace the
  path. On tests, review the test diff — does any realistic bug survive the
  new assertions? — do NOT re-run the green suite the dev and CI already
  ran. A README-level "it filters X" is not proof the filter runs.
- **Review against the spec, not your taste.** The done-condition is the
  bar. Scope creep in the diff = a flag, not a silent pass.
- **Handoffs are gated.** You flag and recommend; the FnB decides defect vs
  intended before anything reaches another shell. A surfaced gap is not
  automatically a fix request — propose it, don''t push it.
- **Critique and confirm — never build.** Do NOT patch the author''s code;
  flag it and propose it back.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'messaging',
  'Shell-to-shell inbox — send a markdown message to another shell (typed: shell/task/result; pr_event is daemon-emitted), check your unread inbox, verify delivery via the sent view, mark messages read. Driven by `sc mem message`. Use to coordinate with another shell; the recipient sees it on its next boot via the STATUS Inbox count.',
  'substrate',
  'sc mem message',
  1,
  '# messaging — the shell inbox

Shell-to-shell markdown messages, driven by `sc mem message`. Sender = you;
recipient addressed by `shortname`. Body = markdown, preserved verbatim.
Recipient discovers it on its next boot via the `## STATUS` `Inbox:` count.

Trigger: `--message`
Args: `check [N] | send <to-shortname> <body> [--kind k] | sent | mark-read <id>`

## Message kinds

Every message carries a `kind` — the trail stays filterable
(`SELECT * FROM shell_messages WHERE kind != ''shell''` replays a sprint''s
whole coordination history):

- `shell` — ordinary shell-to-shell mail (the default; what `send` does
  unless told otherwise).
- `task` — planner → worker instruction (a sprint kickoff / re-task).
- `result` — worker → planner completion or transition report.
- `pr_event` — GitHub watcher daemon → shell PR transition (checks
  green/red, review submitted, merged, closed). Daemon-emitted only:
  `send` refuses it — a forged PR event would poison the wake loop''s
  ground truth. Detail lives in `gh`; the row is the wake-up, not the
  payload.

## check — your unread inbox

```
sc mem message check [N]      # N optional; default 50, max 200
```

Read-only — it does NOT auto-mark-read. Non-`shell` rows show their kind
inline. Surface the body to the operator (reply if warranted — a reply is
itself a `send`), then `mark-read` the inbound in the same turn.

## send — message another shell

```
sc mem message send <to-shortname> "<body>" [--kind shell|task|result]
```

- Multi-word body = one quoted argument; markdown preserved verbatim.
- Examples: `sc mem message send cartographer "map is stale — re-run sc map"`
  · `sc mem message send plan1 "sprint 12: unit 3 merged (PR #41)" --kind result`
- Unknown / deleted recipient -> `mem: recipient shortname ''<x>'' unknown`;
  empty body -> `mem: body is empty`. Surface either to the operator plainly.
- Sends are idempotent under load: each invocation carries a dedupe key, so
  a timed-out send retries itself and can never write a duplicate. Do NOT
  re-run a timed-out send by hand — the retry already happened; if it still
  died, check `sent` first.

## sent — your outbound view

```
sc mem message sent           # latest 50 you sent, newest first, read receipts
```

Verify delivery after an ambiguous failure (a send that died after its
retries) before ever resending. A row present = delivered; absent = safe
to resend.

## mark-read — clear an inbox item (idempotent)

```
sc mem message mark-read <message_id>
```

Pass the `message_id` that `check` surfaced. Only messages addressed to you
clear — another shell''s message = no-op; re-marking a read message = no-op.

## Stance

- On boot, `Inbox:` non-zero -> run `--message check` and surface the first
  item before continuing.
- No threading: a reply = a new `send`; include `Re: <topic>` in the body if
  it matters.
- `mark-read` only after you have actually acted on the message.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'db_map',
  'Data model behind the engine memory surfaces + the `sc mem` command for each. Check before reading or writing memory — identity, decisions, roadmap, documents, flags. Reads/writes go through the API (`sc mem`), never raw sqlite.',
  'substrate',
  NULL,
  1,
  '# db_map — super-coder''s DB at a glance

All identity, memory, and content live in the engine DB
(`.super-coder/shell_db.db`). NEVER touch that file — read and write it only
through the engine API, via `sc mem`:

- **Read** = `sc mem get <surface>`: your own `state`, `seed`, `lns`,
  `decisions`, `flags`, `narrative`, `messages`; shared planning state
  `roadmap`, `projects`, `documents`, `tasks`, `shells` (`--json` for raw).
  `documents`/`tasks` take `--feature <id>` / `--doc <id>`; `--doc` on
  `documents` returns the one doc *with* its body.
- **Write** = `sc mem <cmd> …` (see `## Common writes`).

There is NO raw `sqlite3` path — not as a fallback, not for "ad-hoc" reads.
If the API isn''t wired, `sc mem` fails loud instead of writing the DB behind
its back. Your identity rides in your bearer token — the server resolves
token -> shell; never name a shell in a write. Decisions read FLEET-WIDE
(every row, tagged `@shortname`) so cross-shell citations resolve; every
other identity surface reads as you.

**The `sc sql` lane** (read-only; `sc sql-rw` gated) is real and blessed for
what `sc mem` doesn''t cover: admin/reporting reads and sweep queries — the
flag_sweep / git_cleanup skills run it by design. The doctrine is one level
down: memory-surface reads and writes go through `sc mem`; `sc sql` is for
reporting ACROSS surfaces, never a write path for identity/memory (that is
what `sc mem` scopes and validates).

The table below = the data model behind those surfaces (what each `sc mem`
write touches), not a query cheatsheet. Lazy-load: `get` the one surface you
need, don''t bulk-read.

**Need a read/write `sc mem` doesn''t expose?** Report the gap, don''t reach for
the DB — the direct path is closed by design, and a fork can''t patch the
engine (`sc update` would overwrite it). Open a flag naming the data + the
use, surface it to the FnB (who carries it upstream); message a
planner-flavor shell too if the fork has one. Until it lands: do what you can
through the API, flag the rest — NEVER query the DB directly.

```
sc mem flag open "[Engine] need to <read|write> <what> — no sc mem surface for it | Blocker for: <your work>"
```

The repo map (`dr_*`) lives in its own db, `.sc-state/map.db` (see the
`surface_catalogue` skill). The `dr_*` tables also exist in `shell_db.db` but
are ALWAYS empty there — a `dr_*` query against `shell_db.db` silently returns
0 rows instead of erroring. Never query `dr_*` here; this map covers only
`shell_db.db` (memory/identity/content).

## Tables

| Table | Holds | Write rule |
|---|---|---|
| `shells` | identity core: `mandate`, `system_prompt`, `current_state` (rolling, ~500 chars), `lineage_seed`, `active_archive_id`. (`connections`/`workspace` retired — boot `## CONNECTIONS` is derived from the `dr_*` map, not authored here) | UPDATE in place |
| `shell_identity_entries` | seed (cap 10) + L&S (`kind=''lns''`, cap 20); triggers enforce caps | INSERT to add; UPDATE `retired_at` to curate out — NEVER edit a seed body (Law 3) |
| `shell_decisions` | major decisions | INSERT only; supersede via `parent_decision_id` |
| `shell_memory_archives` | one row per session; `full_narrative` appended progressively | INSERT at session open; UPDATE narrative |
| `roadmap` | one row per planned feature; `roadmap_status` = planning horizon (`brainstorm`→`in_progress`→`next`→`near_term`→`long_term`→`shipped`→`retired`), `sort_order` within a bucket. `shipped` = delivered; `retired` = off the board without shipping (decided-against / split / absorbed / replaced) — keep the row. `project_id` (nullable) = the work-stream the feature belongs to; the GUI Flow view groups on it (NULL = Ungrouped) | INSERT/UPDATE |
| `feature_blockers` | roadmap dependency edges: one row = `feature_id` depends on `blocked_by` (prerequisite lands first). Directed, kept acyclic (GUI Flow view wires them; the card''s "depends on" picker sets them) | INSERT/DELETE the edge; set the whole set via `sc mem roadmap depends` |
| `documents` | content store — spec/doc bodies; `frozen=1` on ship (immutable); `render_path` = flat-file target | INSERT a new `seq` per stage; NEVER edit a frozen body |
| `flags` | open + resolved tasks; `feature_id` links a flag to the feature it blocks | INSERT to open; UPDATE `resolved=1` + `resolved_date` to close |
| `skills` / `shell_skills` | skill catalogue (system, seeded from `assets/skills/` via migration) + per-shell grants | managed by engine; grants via `./sc skill grant/revoke` |
| `projects` / `project_shells` | project standing + shell linkage; a `projects` row also doubles as a work-stream that roadmap features attach to via `roadmap.project_id` (the Flow-view grouping) | UPDATE `standing`; INSERT to add |

`<self>` = your `shell_id` (in the boot doc''s ACTIVE SESSION block).

## Common writes

Each routes through the engine API to the live shared DB. `sc mem which`
orients; `sc mem <cmd> -h` shows flags. Writes always target your own shell —
the server resolves it from your token.

```
# current_state (rolling status, not a log — replaces in place):
sc mem state "…"

# plant a seed / L&S entry (date stamped for you):
sc mem seed "…"            # sc mem lns "…" for a lesson
sc mem retire <entry_id>   # curate one out (frees a cap slot)

# record a Major decision (supersede with --parent <id>):
sc mem decision "…" --rationale "…"

# roadmap: add a feature / move its horizon:
sc mem roadmap add "…" --status brainstorm --summary "…" [--project <shortname|id>]
sc mem roadmap status <feature_id> shipped

# roadmap grouping + sequencing (drive the GUI Flow view):
sc mem roadmap project <feature_id> <shortname|id>   # assign a work-stream (or ''none'' to clear)
sc mem roadmap depends <feature_id> --on <id> [--on <id>]   # set dependencies (replaces; omit --on to clear; refuses cycles)

# author a spec/doc body (--body-file reads the markdown), then freeze on ship:
sc mem doc add "…" --kind spec --feature <id> --body-file ./draft.md --render-path specs_sc/….md
sc mem doc freeze <document_id>

# spec_tasks (the plan): add a task / advance it / close it honestly:
sc mem task add "…" --feature <id> --doc <doc_id> --seq <n> [--desc "…"]
sc mem task start <task_id>     # sc mem task done <task_id>
sc mem task cancel <task_id> --notes "moved to F<id> as task #<n>"   # split/re-scope — never mark unbuilt work done

# open / edit / close a flag:
sc mem flag open "[Area] … | Blocker for: …" --name CC-001 [--feature <id>]
sc mem flag edit <flag_id> [--description "…"] [--priority High] [--feature <id>]
sc mem flag close <flag_id> --notes "…"

# projects (standing + linkage):
sc mem project add <shortname> "<title>" --purpose "…" --standing "…"
sc mem project standing <shortname|id> "…"     # sc mem project status <…> paused

# inbox + first-run:
sc mem message send <shortname> "…"     # check / mark-read too (see `messaging`)
sc mem oriented                          # mark first-run done (bootstrapped=1)
```

## After writing

Nothing more to run — the write is live in the shared engine DB on commit,
visible to every shell. Persisting to git is an admin/GUI step, not yours.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'database-migrations',
  'Database migration safety + how super-coder''s own migrations work (schema.sql baseline + ordered migrations/ deltas + ledger). Use when altering tables, adding columns, or running backfills — in the host repo''s DB or super-coder''s.',
  'craft',
  NULL,
  0,
  '# database-migrations — change schemas safely

Catalogue skill (opt-in). Two halves: super-coder''s own migration model, and
general safety for the host repo''s database.

## super-coder''s model

- `schema.sql` = current baseline (full schema). `migrations/*.sql` = ordered,
  additive deltas applied on top; the `schema_migrations` ledger dedups so
  each runs once. `rebuild` = schema -> migrations -> snapshot-load.
- NEVER fold a migration back into `schema.sql` — it double-applies. Add a
  new numbered migration instead. Exception: pre-fork (no downstream forks
  yet), editing the baseline directly is acceptable; once forks exist, only
  additive migrations propagate.
- System content (e.g. the skill catalogue) = seeded by migration + re-seed;
  per-instance content rides in the snapshot. See `db_map` / `snapshot`.

## General safety (host repo DBs)

- **Expand -> migrate -> contract**: add columns/tables before reading them;
  deploy code that tolerates both shapes; remove the old shape only after
  nothing uses it.
- **Backfills**: batch large updates (no table-long lock); make them
  resumable + idempotent; separate the schema change from the data change.
- **New columns on a populated table**: nullable or defaulted — `NOT NULL`
  with no default fails on existing rows.
- **Reversibility**: know each migration''s rollback before applying it; a
  destructive change (drop/rename) needs a deploy plan, not just a script.
- **SQLite**: limited `ALTER` — changing a constraint = recreate-and-copy
  (new table -> copy -> drop -> rename) with `foreign_keys` off during the
  swap. Renames break FK references — check them.
- **Postgres**: locks are the hazard, not `ALTER` limits. `CREATE INDEX
  CONCURRENTLY` on populated tables (a plain CREATE INDEX takes a write
  lock for the whole build; note CONCURRENTLY can''t run inside a
  transaction). `ALTER TABLE … ADD COLUMN` with a volatile default rewrites
  the table — add nullable, backfill in batches, then set the default.
  `ALTER TYPE … ADD VALUE` (enums) is append-only and (pre-PG12) refuses to
  run in a transaction with other work; removing/reordering values = new
  type + column swap. Set `lock_timeout` before DDL so a blocked ALTER
  fails fast instead of queueing behind (and ahead of) live traffic.
- Dialect specifics beyond these belong to your fork''s own skills (e.g. a
  `query_authoring_pg`-style companion) — this skill stays stack-neutral.

## Stance

Migrate forward in small, reversible steps. A schema change is a deploy
event: migrated ≠ deployed — restart the consumer, then verify the running
process, not just the DB.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'cartographer',
  'Own the repo map — configure mapping to THIS repo, wire the auto-remap git hooks, heal both on drift. Cartographer-only; no working shell maps. Run on first boot + whenever the map looks wrong.',
  'substrate',
  'sc map-setup',
  0,
  '# cartographer — own the repo map so no other shell has to

Working shells consume the `dr_*` catalogue (`surface_catalogue`) and never
map. You alone do three things: **configure** how this repo is mapped, **wire**
the automation that keeps it fresh, **heal** both on drift.

Map db = `.sc-state/map.db`, separate from the engine memory db
(`shell_db.db`) so an engine schema change never touches the map. Reads: `sc
map-sql "…"`. Authoring writes (UPDATE/INSERT/DELETE on `dr_*`): `sc
map-sql-rw "…"` — `sc map-sql` refuses writes. Authored sections serialize to
`.sc-state/map_content.sql` on snapshot (admin/GUI step — see Standing jobs)
and reload on a fresh map db.

`<self>` = your `shell_id` (ACTIVE SESSION block).

## Freshness machinery — what you own

- **Git hooks** `post-merge` / `post-checkout` / `post-rewrite` re-run `sc map`
  on every pull / branch-switch / rebase. Tracked in `.super-coder/hooks/`,
  fired via `core.hooksPath` — per-clone, unset until `sc map-setup` wires it.
- **`sc rebuild`** re-maps (map = derived cache) -> a fresh rebuild never
  leaves an empty map.
- **Hourly cron** — pm2 runs `sc-map-<repo>` on `cron_restart`
  (`.super-coder/ecosystem.config.cjs`) while the stack is up (`sc up`);
  catches uncommitted local restructuring the git hooks can''t see. Verify:
  `pm2 list | grep sc-map` — state cycling stopped→online per tick = the
  one-shot pattern, not a crash. A fork without pm2 has no cron; the hooks
  still cover it, and manual `sc map` always works.
- **You** — per-repo config + hook wiring + extractors + repair of all three.

## First boot — configure mapping for THIS repo

1. **Inspect.** Read the current map + tree:
   ```sql
   SELECT name, default_branch, file_count, mapped_at FROM dr_repo;
   SELECT lang, COUNT(*) n FROM dr_filepath WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;
   SELECT role, COUNT(*) n FROM dr_filepath GROUP BY role ORDER BY n DESC;
   ```
   Eyeball the top-level dirs -> anything mis-classified, or a
   generated/vendored dir being indexed?

2. **Author `.sc-state/map.config.json`** — authored content (tracked,
   per-fork, survives `sc update`; lives in `.sc-state/`, outside the
   gitignored engine dir). All keys optional; each merges over `map_repo.py`
   defaults:
   ```json
   {
     "skip_dirs":  ["generated", "fixtures"],
     "skip_files": ["LICENSE"],
     "role_overrides": [
       { "prefix": "cmd/",      "role": "code" },
       { "glob":   "*.proto",   "role": "code" },
       { "prefix": "docs/adr/", "role": "doc"  }
     ]
   }
   ```
   - `skip_dirs` / `skip_files` — ADDED to the defaults; never shrink them.
   - `role_overrides` — applied after default role inference, first match
     wins. `prefix` matches the repo-relative path; `glob` matches the filename.
   Add only what the defaults get wrong — empty/absent config is fine for a
   plain repo.

3. **Wire + map:** `sc map-setup` -> `core.hooksPath` points at
   `.super-coder/hooks/`, hooks executable, initial map run.

4. **Verify the wiring, not just the files:**
   ```sh
   git config --get core.hooksPath      # → .super-coder/hooks
   ls -l .super-coder/hooks             # all three, executable
   ```
   ```sql
   SELECT file_count, mapped_at FROM dr_repo;   -- non-zero, just now
   ```
   Spot-check overrides took:
   `SELECT path, role FROM dr_filepath WHERE path LIKE ''cmd/%'';`

5. **Describe all NULLs** — run the description worklist (Standing jobs § 2);
   leave only when it returns zero rows.

6. **Commit** the config + hooks (`git` skill) -> `sc mem state "…"` ->
   `sc mem oriented` (sets `bootstrapped=1` — the write is live in the
   shared DB; it does NOT snapshot).

## Heal — run whenever the map looks wrong

Triggers: repo restructured / new language or dir / files mis-roled / map
stale or empty on a clone whose hooks never got wired.

1. Re-inspect (step 1) — what changed?
2. Edit `.sc-state/map.config.json` to match (step 2).
3. `sc map-setup` (idempotent) — re-wires hooks + re-maps.
4. Verify (step 4). Vanished paths are auto-pruned from `dr_filepath` by the
   remap.
5. **Stale sections** — `dr_section` is authored, never auto-pruned. After any
   migration/restructure run the stale-section worklist (Standing jobs § 1);
   DELETE or repath every row it returns.
6. **Describe all NULLs** (Standing jobs § 2) -> worklist empty before you
   leave.
7. Commit.

## Standing jobs — sections, descriptions, product DB

Both authored layers survive the remap (`dr_section` is never touched by the
mapper; `dr_filepath.desc` is preserved by its UPSERT); neither blocks the
auto-remap hook. Boot `## CONNECTIONS` renders the section index;
descriptions are the leaves a shell queries once narrowed to a section.

**1. Sections (`dr_section`)** — curate the navigational index. Seeded one
section per top-level dir on first map; make it *good*: rename to what shells
call the area, split coarse dirs into real areas, merge noise, write the
one-line `description`.

```sql
-- the current index + live file counts:
SELECT s.name, s.path_prefix, s.description,
       (SELECT COUNT(*) FROM dr_filepath f WHERE f.path LIKE s.path_prefix || ''%'') n
FROM dr_section s ORDER BY s.sort_order, s.name;

-- split / rename / describe (authored — survives the remap, snapshotted):
UPDATE dr_section SET name=''API'', path_prefix=''shell_core/api/'', description=''FastAPI routers'' WHERE name=''shell_core'';
INSERT INTO dr_section (name, path_prefix, description, sort_order)
VALUES (''UI'', ''shell_core/ui/'', ''SvelteKit substrate UI'', 5);

-- WORKLIST — keep the catch-all empty. Files under no section = a new area to
-- section (they render under "other / unsectioned" in CONNECTIONS until you do):
SELECT path FROM dr_filepath f WHERE NOT EXISTS
  (SELECT 1 FROM dr_section s WHERE f.path LIKE s.path_prefix || ''%'')
ORDER BY path;

-- STALE SECTIONS (run after any migration or restructure — dr_filepath pruning
-- is automatic; dr_section is authored and never auto-pruned):
SELECT s.name, s.path_prefix, s.description
FROM dr_section s
WHERE (SELECT COUNT(*) FROM dr_filepath f WHERE f.path LIKE s.path_prefix || ''%'') = 0
ORDER BY s.name;
-- For each row: DELETE (area gone) or UPDATE path_prefix (area renamed).
```

**2. Descriptions (`dr_filepath.desc`)** — per-file one-liners, ≤100 chars.
Run the worklist every session; every run ends with zero NULLs — not optional.
Queried by working shells within a chosen section (`surface_catalogue`), never
bulk-loaded at boot.

```sql
-- WORKLIST — undescribed files, most-load-bearing first:
SELECT path, role FROM dr_filepath WHERE desc IS NULL ORDER BY role, path;

-- describe (≤100 chars; preserved across the next auto-remap):
UPDATE dr_filepath SET desc=''Boot composer — assembles CLAUDE.md from DB state'' WHERE path=''.super-coder/render/compose.py'';
```

**3. Product DB** — the app''s own database, separate from engine memory
(`.super-coder/shell_db.db`); working shells change them in completely
different ways (boot `## DATABASES`), and the map you author is the only
per-fork signal of where the app DB lives. The live `.db` is usually
gitignored (absent from the map); schema + migrations are tracked = the
durable anchor. Tag them plainly as the product/app DB so no shell mistakes
them for engine memory; give them a section if they form an area.

```sql
-- tag the product DB''s definition (the engine-vs-app split made visible):
UPDATE dr_filepath SET desc=''Product DB schema — the APP database (NOT engine memory)'' WHERE path=''<app schema file>'';
UPDATE dr_filepath SET desc=''Product DB migration — change the app schema here'' WHERE path LIKE ''<app migrations dir>/%'';
-- optional: a section if the product DB is its own area
INSERT INTO dr_section (name, path_prefix, description, sort_order)
VALUES (''App DB'', ''<db dir>/'', ''Product runtime database — schema + migrations (NOT the engine memory DB)'', 7);
```

Fork ships no database of its own -> skip.

After a curation pass your writes are already live in the shared map db —
done. NEVER run a plain `sc snapshot` from a shell — it is refused by design;
persistence = the GUI Snapshot button or an admin''s `SC_ADMIN=1 ./sc
snapshot`. Don''t chase it. (Sections are snapshotted; descriptions ride the
live DB + survive remap — refill from the worklist if a rebuild drops them.)

## Extending the map — semantic extractors

The engine maps the generic 80% (files, languages, roles, deps, env).
Semantic dimensions — HTTP endpoints (`dr_endpoint`), app DB schema
(`dr_db_table`/`dr_db_column`), UI routes/components
(`dr_route`/`dr_component`) — vary by stack: you extract them via drop-in
Python modules in `.sc-state/map_extractors/*.py`, discovered + run by
`sc map` after the core pass. Fork-owned (outside the gitignored engine dir ->
`sc update` never clobbers them); table *columns* are standardized in the
engine (`map_schema.sql`) so working-shell queries have a stable shape
everywhere.

Adopt one per stack:

1. **Detect the stack:** `SELECT manager, name FROM dr_dependency;`
   (fastapi? flask? svelte? next?) + the file mix
   (`SELECT lang, COUNT(*) FROM dr_filepath GROUP BY lang`).
2. **Copy the matching reference** from the engine''s
   `.super-coder/templates/map_extractors/` into `.sc-state/map_extractors/`:
   - `fastapi_endpoints.py` — decorator routes (`@app.get(...)`, Flask `@app.route`) → `dr_endpoint`
   - `sqlite_schema.py` — SQL `CREATE TABLE/VIEW` → `dr_db_table`/`dr_db_column`
   - `sveltekit_routes.py` — filesystem routes + `*.svelte` → `dr_route`/`dr_component`
   Adapt the `framework` label + file filter to this repo. Uncovered stack
   (Django URLs, Express, Spring, Rails) -> copy the closest as a skeleton,
   rewrite the match — target the dominant pattern, not 100%.
3. **Run + verify:** `sc map` -> table populated, rows look right
   (`SELECT method, path FROM dr_endpoint LIMIT 10;`).
4. **Commit** `.sc-state/map_extractors/`. (Snapshotting the authored layer =
   the admin/GUI step above — not yours to run.)

**Contract** (full version: `templates/map_extractors/README.md`): each module
defines `extract(con, repo_root, cfg) -> str`. `con` = the live map db with
`dr_filepath` already populated — query it for inputs. DELETE + repopulate
only your own `dr_*` table(s); return a one-line summary for the map log.
NEVER assume a file parses — guard yourself even though `map_repo` guards each
extractor. Static extraction is best-effort: log what you skip (dynamic
routes, computed paths); never claim full coverage.

## Shape-change notices — the curation trigger

The hooks keep the mechanical catalogue fresh, but a newly-landed file arrives
`desc IS NULL` and unsectioned. Working shells message you on shape change so
curation is a timely push, not a next-boot pull — the only inbox traffic you
act on as cartographer.

**Notice contract** (one source of truth — the relay skills point here).
Sender = the **dev/coder** shell on merge (feature landed, doc written); NOT
the planner — specs render into a known area and need no curation. Sent via
the `messaging` skill to shortname `cartographer`:

```
--message send cartographer "shape: <what landed> — paths: <region/>; <ref>. curate."
```

Body names **what** changed + **where** (the path region) so your pass is
scoped, not a full re-survey. A `documents`/feature ref is optional.

**On a notice** — check inbox -> run the worklists scoped to the named
region -> mark read:

```sql
-- 1. the new files this notice is about (scope by the region it named):
SELECT path, role FROM dr_filepath
WHERE desc IS NULL AND path LIKE ''region/%'' ORDER BY role, path;
-- 2. describe them (≤100 chars) — UPDATE dr_filepath SET desc=… per the worklist above.
-- 3. do they form / join a section? curate dr_section if the region is a new area.
```

Then `--message mark-read <id>` (`messaging` skill). The mechanical remap
already ran via the hook; your job on the notice = the authored layer only —
describe the new leaves, section a new area. `desc IS NULL` already narrows to
exactly the uncurated tail.

## Stance

- The map is infrastructure, not a chore for every shell. A working shell
  hunting the tree for something the map should know = heal the map; do not
  teach that shell to map.
- Config is the lever: tune `map.config.json`; touch `map_repo.py` only when
  the mechanism itself (a parser, a role kind) is wrong.
- Verify the automation, not just the file: a written hook that
  `core.hooksPath` doesn''t point at does nothing -> check the wiring after
  every setup.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
