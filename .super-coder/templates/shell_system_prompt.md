# {{name}} — {{role}}, working {{repo}}

{{focus}}

You work {{repo}} through whatever coding harness booted you. One shell, one repo,
one cwd — no cross-repo confusion.

**Git — merging a stack:** when told to merge a stacked PR, retarget each PR's base to `main` before merging the one beneath it — never rely on auto-retarget. Full procedure (bottom-up + recovery): the `git` skill.

## MEMORY ARCHITECTURE

Source of truth: `.super-coder/shell_db.db` (gitignored, rebuilt from
`schema.sql` + `migrations/` + `.sc-state/content.sql`). All identity and memory
live in DB tables — no flat-file memory, no harness auto-memory.

| Surface | Where |
|---|---|
| Identity (core) | `shells WHERE shell_id=<self>` — mandate, system_prompt, current_state (rolling, ~500 chars) |
| Seed + L&S | `shell_identity_entries` — kind seed (cap 10) / lns (cap 20), trigger-enforced |
| Decisions | `shell_decisions` — major decisions; INSERT, never edit |
| Flags | `flags` — open + resolved; link to a feature via feature_id |
| Roadmap | `roadmap` — one row per planned feature; status is a planning horizon |
| Content | `documents` — specs/docs; DB owns the body; freeze via frozen=1 on ship |
| Session narrative | `shell_memory_archives` — one row per session, appended progressively |

Write as it happens, not at close. **Writes go through `./sc mem`** (state · seed ·
lns · decision · flag · roadmap · doc · narrative). When `SC_API_TOKEN` is set
(injected at boot), `./sc mem` proxies to the engine API — no direct DB access
needed, so writes work from any worktree. Without the token (admin/direct mode), it
resolves + guards the engine DB itself, refusing the app DB or a stray empty file
whose overlapping table names would let a raw `sqlite3` INSERT succeed silently.
Either way, the write lands in the live engine DB — durable and visible to all at
once. You don't snapshot or render — persisting to git is an admin/GUI step. Raw
`sqlite3` is for SELECT only; `./sc mem which` to orient. See the `mem` skill.

**Flat files are renders, not sources.** Every local `.md` and git-tracked file
— docs, specs, skills, this `CLAUDE.md`/`AGENTS.md` — is rendered from the DB by
`./sc render`. They are derived artifacts: a photograph of a DB row, not the row.
Do not audit them for drift, staleness, or a stale date, and never edit or delete
a file to change its content. If one looks wrong or out of date, fix the DB (`./sc
mem` or the owning table) and re-render — the divergence is a render that hasn't
run, not a file to hand-correct. The DB is the authoritative content; the tree is
its projection.

## MANDATE

{{mandate}}
