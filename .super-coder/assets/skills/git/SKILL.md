---
name: git
description: Git conventions for a super-coder shell — one repo, one cwd. Sync the base before work, branch before committing, open PRs (never merge without the FnB's OK), attribute commits per-shell. Use before any git work.
category: substrate
common: false
---

# git — version control, the super-coder way

One repo at its root -> plain `git` (cwd = repo root) is safe.

Project = this repo minus `.super-coder/`. Engine = `.super-coder/` — gitignored, materialized by `sc update`, authored upstream in super-coder. NEVER commit or edit anything under `.super-coder/`.

## Sync before you start — hard pre-code gate

Run the gate every session + before each new unit of work. `shell/<shortname>` = a moving base pinned to `origin/main`, not a content branch — cut feature branches from it. A stale base -> you read code that no longer exists + your PRs conflict on arrival.

The launcher auto-syncs at boot when provably nothing can be lost (on base branch + clean tree + no local-only commits). Read the `sync:` line in ACTIVE SESSION: auto-synced + nothing done since -> current, carry on. Says **NOT auto-synced** / you're mid-session about to start new work -> run:

1. `git fetch origin main && git rev-list --count HEAD..origin/main` -> 0 = carry on.
2. Behind -> take stock BEFORE touching anything: `git status` (uncommitted) + `git rev-list origin/main..HEAD` (unmerged commits) + `git branch --no-merged origin/main` (unlanded branches).
3. Anything local -> surface to the FnB first: list the commits/files, ask land / stash / discard. No sync without their call (soft gate).
4. Clean (or FnB said go) -> `git checkout shell/<shortname> && git reset --hard origin/main`. NEVER `git pull`/merge on the base — merge bubbles accumulate + your squash-merged work replays as conflicts.
5. Reset only the base, never a feature branch. Stale feature branch -> `git rebase origin/main`.

## Branch -> commit -> push -> PR -> stop

1. NEVER commit to the default branch. Branch first: `git checkout -b <type>/<short-desc>` (feat/fix/chore/docs). *Admin-shell exception:* it boots at the repo root on `main`, exempt from the branch-guard; committing to main is its mandate (engine updates, migrations, approved patches) and it starts each session with `git pull --ff-only`. Every other shell branches, always.
2. Commit in logical units. End every message with your shell's trailer:
   ```
   Co-Authored-By: <shell display_name> (super-coder) <noreply@…>
   ```
3. Push -> open a PR -> stop. Do NOT merge without an explicit FnB directive — opening is the default, merging is a separate gate.

## Merging a stack (only when the FnB hands you one)

Merge bottom-up, retargeting before each merge — never rely on GitHub's auto-retarget:

1. `gh pr view <n> --json mergeable,mergeStateStatus` -> clean.
2. `gh pr merge <low> --squash --delete-branch`.
3. BEFORE the next merge: `gh pr edit <next> --base main` — deleting the merged base otherwise orphans the PR above it (GitHub closes it `CONFLICTING`, base ref gone).
4. Re-check `MERGEABLE` -> merge. Repeat up the stack.

PR already orphaned (base deleted under it) -> the head branch still holds the commits; reopen the SAME PR, don't rebuild:

1. `git push origin <merged-sha>:refs/heads/<deleted-branch>` — `<merged-sha>` = `gh pr view <merged-pr> --json headRefOid`.
2. `gh pr reopen <closed-pr>` -> `gh pr edit <closed-pr> --base main`.
3. Verify `MERGEABLE` -> delete the recreated branch again.

## Finish before you stop

Bookend to the sync gate. At end of session: `git status` (uncommitted) + `git rev-list origin/<base>..HEAD` (unpushed) -> resolve every hit:

1. Real work -> commit (attributed, trailer above) + push + open the PR. Don't skip because the session is ending.
2. Throwaway / experiment -> discard deliberately: `git restore` / `git stash`.
3. Genuinely unsure -> surface to the FnB + leave it committed-and-pushed on a branch — never sitting uncommitted.

Pass = tree clean, or on a pushed branch with a PR. A dirty/unpushed tree forces the admin's `git_cleanup` to map attribution, check liveness, and commit on your behalf.

## After a merge — clean up local

Only after the PR is merged:

1. Re-pin the base. In a worktree `git checkout main` fails (main is checked out at the repo root; git refuses a branch checked out elsewhere) -> `git checkout shell/<shortname> && git fetch origin && git reset --hard origin/main`. Admin at repo root: `git pull --ff-only` on main.
2. `git branch -d <branch>`. Squash-merged -> `-d` refuses (commits aren't ancestors of main); confirm the PR shows *merged* on the remote -> `git branch -D <branch>`.
3. `git fetch --prune`.

NEVER delete a branch carrying unmerged, un-PR'd work — no PR = lost work.

## Never commit the engine or derived files

- `/.super-coder/` is gitignored — never force-add anything under it.
- Gitignored + regenerated, never commit: `CLAUDE.md`, `AGENTS.md`, `opencode.json`, `.claude/skills/`, `.sc-state/engine.ref.prev` (ephemeral rollback pointer).
- From a worktree, commit only your project's own files. Do NOT hand-commit `.sc-state/content.sql` (serialized DB memory), `.sc-state/engine.ref` (engine pin), or the tracked `_sc` renders — `sc` writes them to the main checkout root, so they aren't in your worktree to stage. They enter the repo via Publish (below).
- Exception: in the super-coder SOURCE repo, `schema.sql` + `migrations/` are tracked — there the engine *is* the project.

## After DB work — `sc mem` is already saved; Publish is separate

An `sc mem` write lands in the shared engine DB immediately (visible to every shell) and `sc rebuild` restores it from the serialized snapshot — there is no per-shell save step. NEVER run `sc snapshot` from a worktree — it refuses by design (`snapshot: refused — serializing to the shared main tree is an admin/GUI step`).

Getting DB text into the repo = the Publish flow (snapshot -> render -> commit -> push -> PR on `sc_gui_content`): the GUI **Publish** button, or the admin shell on `main` running `SC_ADMIN=1 sc snapshot` (+ `SC_ADMIN=1 sc render` if docs/roadmap/skills changed). Output lands at the main checkout root, NOT your worktree — don't try to commit `content.sql` or `_sc` renders onto your branch. Feature-branch PRs carry project files; DB content publishes separately. See the `snapshot` skill.

## Notes

- Before destructive ops, confirm the repo — `git -C <abs-path>` if ever in doubt.
- Multi-shell: each shell boots into its own worktree at `.sc-worktrees/<shortname>/` on branch `shell/<shortname>`; the launcher keeps the base pinned to `origin/main` (see the sync gate). Worktree isolation is automatic — no shared cwd. Admin shell = the one exception: repo root on `main`.
- UI preview: worktree edits do NOT show on the fork's main dev server. `sc preview` (start once from the main checkout if not running) serves every shell's worktree UI live (HMR) on the fork's `dev_port`, one subdomain each: `http://<shortname>.localhost:<dev_port>/`. The `post-commit` hook prints your URL after each commit — surface that line to the FnB.
