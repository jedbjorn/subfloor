---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# git

Git conventions for a super-coder shell — one repo, one cwd. Branch before committing, open PRs (never merge without the FnB's OK), attribute commits per-shell. Use before any git work.

**Category:** substrate

---

# git — version control, the super-coder way

A super-coder shell works **one repo at its root** — no cross-repo confusion, so
plain `git` (cwd = repo root) is safe. The discipline:

## Branch → commit → push → PR → stop

1. **Never commit straight to the default branch.** Branch first:
   `git checkout -b <type>/<short-desc>` (feat/fix/chore/docs).
2. Commit in logical units. End the message with your shell's attribution so
   parallel shells' work stays legible:
   ```
   Co-Authored-By: <shell display_name> (super-coder) <noreply@…>
   ```
3. Push, open a **PR**, then **stop**. **Do not merge** without an explicit
   directive from the FnB — opening is the default, merging is a separate gate.

## Don't commit rebuilt/derived files

These are gitignored and regenerated — never add them:
`.super-coder/shell_db.db*`, `.super-coder/instance.json`, `CLAUDE.md`,
`AGENTS.md`, `opencode.json`, `.claude/skills/`. **Do** commit the text the `.db`
rebuilds from: `schema.sql`, `migrations/`, `snapshot/content.sql`, and the
tracked `_sc` renders.

## After DB work, before committing

Your DB edits live only in the `.db` until serialized. Run `make snapshot`
(+ `make render` if docs/roadmap/skills changed) so the change is in git-tracked
text, then commit that text. See the `snapshot` skill.

## Notes

- Confirm you're in the intended repo before destructive ops (`git -C` if ever in
  doubt).
- Multi-shell: per-shell branches keep parallel work from colliding (the planned
  worktree model makes this clean — until then, one active shell per cwd).
