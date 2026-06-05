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
visibility surface for browsers without localhost; `./sc render` (and `make
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
(`./sc seed-skills`) into `migrations/0001_seed_skills.sql`. The catalogue rides
in a migration (propagates); the per-shell *grant* (`shell_skills`) rides in the
snapshot (fork-local). `./sc rebuild` seeds the catalogue, then loads grants.

## Adapters

`adapters/<harness>/` is the **only** harness-specific seam — everything above it
is harness-blind. Each holds an `adapter.json`:

| field | meaning |
|---|---|
| `launch` | argv exec'd to start the harness |
| `boot_artifact` | the context file this harness reads (informational) |
| `emit` | files in the adapter dir copied to the repo root at launch (gitignored, regenerated each launch from the tracked template) |
| `env` | extra env merged into the launch environment |

- **`claude/`** — reads `CLAUDE.md` + `.claude/skills/*/SKILL.md` natively; nothing
  extra to emit.
- **`opencode/`** — reads `AGENTS.md` + `.claude/skills/*/SKILL.md` natively, and
  emits **`opencode.json`** (instructions → `AGENTS.md`, tool permissions, `mcp`
  slot). `env.OPENCODE_DISABLE_CLAUDE_CODE=1` avoids double-loading `CLAUDE.md`
  (we dual-write both). Edit the tracked `opencode.json` template to change a
  fork's config.

The boot render dual-writes `CLAUDE.md` + `AGENTS.md` and the skills, so both
harnesses consume the same substrate unchanged — that's the harness-agnostic bet.
`run.py` picks the harness (`HARNESS` env → `instance.json` → claude), loads its
adapter, emits its files, and exec's its launch command. An unknown harness falls
back to running its own name + reading `AGENTS.md`.

## Review layer (`api/` + `ui/`)

A **zero-dependency** localhost GUI over the live DB — no FastAPI, no venv, no
npm/build. `api/server.py` is a stdlib HTTP server that serves both the JSON API
and the static `ui/` (one page, vanilla JS) on a single per-fork port.

- **Read** shells, roadmap, flags. **Edit** a shell's operational fields
  (`current_state`, `connections`) + skill grants, the roadmap, and
  **non-frozen** documents. **Create / resolve** flags.
- **Law enforcement is structural:** seed and L&S have *no write route* (Laws
  2–4, 7) — not a disabled control, an absent endpoint. Frozen documents reject
  edits server-side.
- `POST /api/snapshot` runs `snapshot.py` + `render.py flat` — the manual
  precursor to the B6 commit→PR automation.

`scripts/ports.py` derives this fork's port from its repo path (`8800 + sha1 %
100`), bumping past anything occupied, and persists it to the gitignored
`instance.json`. The server runs inside the docker sandbox (`Dockerfile` +
`./sc launch`/`down`); the container is named `sc-<repo>` so forks never clash,
and the port publishes to `127.0.0.1` only. `./sc serve` runs it on the host
without docker (the escape hatch). `ecosystem.config.cjs` (pm2) is legacy from
the pre-docker host model and no longer on the default path.
