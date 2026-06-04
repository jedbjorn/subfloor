# .super-coder/ — the engine

Everything super-coder owns. The host project's own code is untouched; this dir
is the substrate that runs it.

## The DB is rebuilt, never committed

`shell_db.db` is **gitignored**. It reconstructs from two git-tracked text
serializations:

| Category | File(s) | Git? | Role |
|---|---|---|---|
| **System migrations** | `migrations/*.sql` | tracked | ordered; **propagate** to forks; schema + system content |
| **Per-instance snapshot** | `snapshot/content.sql` | tracked | idempotent dump of *this* repo's content + memory; **stays local** |
| **Baseline schema** | `schema.sql` | tracked | full current schema; applied on fresh build |
| **`.db`, boot artifact** | — | ignored | rebuilt at launch |

The split that matters: **system content propagates, per-instance content does
not.** Migrations are pulled downstream by every fork; the snapshot belongs to
one repo only.

## Scripts

| Script | Does |
|---|---|
| `scripts/rebuild.py` | apply `schema.sql` → stamp/apply `migrations/` → load `snapshot/content.sql`. Builds a fresh `.db`. |
| `scripts/migrate.py` | apply pending `migrations/*.sql` to an existing `.db`; record in `schema_migrations`. |
| `scripts/snapshot.py` | dump per-instance tables → `snapshot/content.sql` (deterministic). |
| `scripts/run.py` | launcher: username-only auth → pick shell → render boot (`CLAUDE.md` + `AGENTS.md`) → exec harness. |

## Render

`render/` turns live DB state into three kinds of artifact, all one-way
(DB → file, never read back) and incremental (an artifact whose content already
matches disk is skipped, so an unchanged DB renders to nothing):

| Artifact | Module | Git? | Consumer |
|---|---|---|---|
| Boot doc → `CLAUDE.md` + `AGENTS.md` | `compose.py` | ignored | the harness at launch |
| Per-shell skills → `.claude/skills/<name>/SKILL.md` | `flat.render_skill_md` | ignored | the harness (Agent Skills) |
| Flat `_sc` files → `specs_sc/` `docs_sc/` `skills_sc/` `roadmap_sc.md` | `flat.render_visibility` | **tracked** | the outsider FnB browsing the repo |

The boot doc + SKILL.md are rebuilt every launch by `run.py` for the chosen
shell — gitignored caches, like `.db`. The flat `_sc` files are the tracked
visibility surface for browsers without localhost; `make render` (and `make
verify`) regenerate them, and the B6 commit→PR automation will refresh them on
every content edit. Each rendered file carries the do-not-edit banner (spec
§Content & Render); for bodies that already open with YAML frontmatter the
banner keys are spliced into it rather than prepended, so the YAML stays valid.

`scripts/render.py` is the standalone CLI: `flat` (tracked `_sc`),
`skills <shortname>` (one shell's `.claude/skills/`), or `all <shortname>`.

## Skills

System content: a skill's body propagates to every fork. Authored at
`assets/skills/<name>/SKILL.md` (frontmatter `name`/`description`/`category`/
`command`/`common` + markdown body), compiled by `scripts/seed_skills.py`
(`make seed-skills`) into `migrations/0001_seed_skills.sql`. The catalogue rides
in a migration (propagates); the per-shell *grant* (`shell_skills`) rides in the
snapshot (fork-local). `make rebuild` seeds the catalogue, then loads grants.

## Adapters

`adapters/<harness>/` holds the thin, harness-specific seam: provider/model
config, tool/permission config, MCP config, launch command. `claude/` is v1;
`opencode/` is next. Everything above the seam is harness-blind.
