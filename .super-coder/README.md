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

`render/` composes the boot artifact from live DB state and dual-writes it to
`CLAUDE.md` + `AGENTS.md` at the repo root. Skills→`SKILL.md` and flat `_sc`
doc/spec render land in a later phase.

## Adapters

`adapters/<harness>/` holds the thin, harness-specific seam: provider/model
config, tool/permission config, MCP config, launch command. `claude/` is v1;
`opencode/` is next. Everything above the seam is harness-blind.
