---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# source-maintenance

Maintain the super-coder/subfloor engine source itself on bare metal. Use for changes to sc, .super-coder code, migrations, adapters, prompts, shell templates, engine skills, update/rollback behavior, or the source repository's tracked dogfood state.

**Category:** substrate

---

# Source maintenance

Treat this repository as upstream: `.super-coder/` and `sc` are the product,
not an installed dependency. Fix engine defects here; do not run the fork
report-upstream or never-edit-engine procedures.

## Orient

1. Confirm source mode:
   `python3 -c "import sys; sys.path.insert(0,'.super-coder/scripts'); import install; print(install.is_source_repo())"`
   must print `True`. If not, fix `SOURCE_REPO_NAMES` before other work.
2. Query the repo map, but verify it includes `.super-coder/`. A source map that
   hides the engine is invalid.
3. Read active decisions before choosing an architecture.
4. Work from a branch/worktree. Preserve tracked `.sc-state/content.sql`; it is
   this source repo's dogfood memory, not a disposable fork seed.

## Change the right source

| Concern | Authoritative source |
|---|---|
| Runtime and CLI lifecycle | `sc` plus `.super-coder/scripts/` |
| Harness behavior | `.super-coder/adapters/<harness>/adapter.json` |
| Boot-wide instructions | `.super-coder/templates/boot.md` and `render/compose.py` |
| Shell flavor defaults | `.super-coder/templates/shells/*.json` |
| Engine skill | `.super-coder/assets/skills/<name>/SKILL.md`, then `./sc seed-skills` |
| Schema/system content | a new ordered migration; never rewrite an applied migration except the generated skill seed |
| This team's mandate, grants, memory, roadmap | live DB, then `SC_ADMIN=1 ./sc snapshot` |

Flat `_sc` markdown and `AGENTS.md`/`CLAUDE.md` are renders. Never author a
behavioral change in them.

## Bare-metal contract

- `./sc launch`, `enter`, `run`, `down`, `restart`, and `logs` are the primary
  host-native lifecycle.
- `enter` and `run` intentionally set trusted harness mode. Do not silently
  restore a harness sandbox or approval loop.
- Docker compatibility is explicit under `sandbox-*`; do not let its
  assumptions leak into host prompts or primary help.
- Bind the engine API and ordinary dev servers to loopback by default.
- Direct host authority is not task authority: keep actions scoped to the
  operator's request and preserve unrelated host state.

## Finish

Run focused tests, then:

```bash
./sc map
./sc render-check
./sc verify
git diff --check
```

If skill assets changed, run `./sc seed-skills` first. If dogfood DB content or
grants changed, snapshot it. A source change reaches downstream forks only
after it is merged and they update; never expect the currently running shell to
inherit a changed prompt or skill until its next boot.
