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

**Operate on the project, never the engine.** Your project is this repo —
everything except `.super-coder/`. The engine is a gitignored, materialized
dependency (refreshed by `./sc update`); don't commit it or edit it as if it
were your code. Engine changes are authored upstream in super-coder.

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

## After a merge — clean up local

Once the FnB merges your PR, tidy local so stale branches don't accumulate:

1. `git checkout main && git pull` — fast-forward onto the merged commit.
2. Delete the merged branch: `git branch -d <branch>`. If it was
   **squash-merged**, git won't recognize it as merged and `-d` refuses — confirm
   the PR shows *merged* on the remote, then `git branch -D <branch>`.
3. `git fetch --prune` — drop remote-tracking refs for branches deleted upstream.

Only after the PR is merged. Never delete a branch carrying unmerged, un-PR'd
work — a deleted branch with no PR is lost work.

## Don't commit the engine or rebuilt/derived files

The whole engine dir is gitignored (`/.super-coder/`) — never force-add anything
under it. Also gitignored + regenerated: `CLAUDE.md`, `AGENTS.md`,
`opencode.json`, `.claude/skills/`, and `.sc-state/engine.ref.prev` (the
ephemeral rollback pointer). **Do** commit your fork-owned state in `.sc-state/`:
`content.sql` (the memory serialization the `.db` rebuilds from) and `engine.ref`
(the engine version pin) — plus your project's own files and any tracked `_sc`
renders. (In the super-coder SOURCE repo only, `schema.sql` + `migrations/` are
tracked too — there the engine *is* the project.)

## After DB work, before committing

Your DB edits live only in the `.db` until serialized. Run `./sc snapshot`
(+ `./sc render` if docs/roadmap/skills changed) so the change is in git-tracked
text, then commit that text. See the `snapshot` skill.

## Notes

- Confirm you're in the intended repo before destructive ops (`git -C` if ever in
  doubt).
- Multi-shell: dev shells each boot into their own git worktree at
  `.sc-worktrees/<shortname>/` on branch `shell/<shortname>`. Parallel dev shells
  never share a cwd — worktree isolation is automatic.
