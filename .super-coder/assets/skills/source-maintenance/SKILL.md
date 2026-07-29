---
name: source-maintenance
description: Maintain the subfloor engine source at ~/Repos/subfloor — you are its upstream. Use for changes to its sc, .super-coder code, migrations, adapters, prompts, shell templates, engine skills, update/rollback behavior, or its tracked dogfood state. NEVER for the home repo's engine.
metadata:
  category: substrate
  common: false
---

# Source maintenance — the subfloor engine

The engine source you maintain lives at **`~/Repos/subfloor`** (GitHub
`jedbjorn/subfloor`). THERE, `.super-coder/` and `sc` are the product and you
are upstream: fix engine defects in that repo directly; the fork
report-upstream / never-edit-engine procedures do not apply to it.

The engine under your OWN cwd (the home repo's `.super-coder/`) is a different
install: your memory substrate, NOT your work surface. NEVER apply this skill
to it — home-engine changes are FnB-gated maintenance (see the boot doc's
PROJECT vs ENGINE).

Every command below runs against the work repo: `git -C ~/Repos/subfloor …`,
or scripts from that root — never from your cwd (the `git` skill has the full
addressing contract).

## Orient

1. Confirm the target: `git -C ~/Repos/subfloor remote get-url origin` ->
   `…jedbjorn/subfloor…`. Anything else = wrong repo; stop.
2. Read subfloor's active decisions/specs before choosing an architecture.
3. Work from a branch (or a worktree seat — `git` skill). Preserve subfloor's
   tracked `.sc-state/content.sql`; it is that repo's dogfood memory, not a
   disposable fork seed.

## Change the right source (paths within ~/Repos/subfloor)

| Concern | Authoritative source |
|---|---|
| Runtime and CLI lifecycle | `sc` plus `.super-coder/scripts/` |
| Harness behavior | `.super-coder/adapters/<harness>/adapter.json` |
| Boot-wide instructions | `.super-coder/templates/boot.md` and `render/compose.py` |
| Shell flavor defaults | `.super-coder/templates/shells/*.json` |
| Engine skill | `.super-coder/assets/skills/<name>/SKILL.md`, then `./sc seed-skills` |
| Schema/system content | a new ordered migration; never rewrite an applied migration except the generated skill seed |
| Subfloor's own team state | its live DB, then `SC_ADMIN=1 ./sc snapshot` (run in subfloor) |

Flat `_sc` markdown and `AGENTS.md`/`CLAUDE.md` are renders. Never author a
behavioral change in them.

## Downstream contract

A subfloor change reaches installed forks (dos-arch, md-converter, ami, rst-c)
only after it merges and they `./sc update` — keep migrations ordered and
non-destructive, and never assume a running shell inherits a changed prompt or
skill before its next boot.

## Finish

Run focused tests, then from `~/Repos/subfloor`:

```bash
./sc map
./sc render-check
./sc verify
git -C ~/Repos/subfloor diff --check
```

If skill assets changed, run `./sc seed-skills` first (in subfloor). Then the
`git` skill's finish gate: branch -> commit -> push -> PR -> stop.
