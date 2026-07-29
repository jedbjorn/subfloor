---
name: git
description: Git conventions for this install — ALL version control happens in the work repo (~/Repos/subfloor), addressed explicitly with git -C / gh --repo. Sync the base before work, branch before committing, open PRs (never merge without the FnB's OK). Use before any git work.
category: substrate
common: false
---

# git — version control, the work-repo way

Two repos are in reach; only one takes commits from you:

| Repo | Path | Git role |
|---|---|---|
| **work repo** | `~/Repos/subfloor` (GitHub `jedbjorn/subfloor`) | ALL of it: sync, branch, commit, push, PR |
| **home repo** | the repo your cwd sits in | NONE. Local-only (no remotes); commits refused by a pre-commit guard |

Your cwd is a home worktree -> a bare `git`/`gh` command targets the WRONG repo.
Address the work repo explicitly, every time:

- `git -C ~/Repos/subfloor <cmd>` — never rely on cwd, even right after a `cd`.
- `gh --repo jedbjorn/subfloor <cmd>` for PR operations.
- Success condition: `git -C ~/Repos/subfloor rev-parse --show-toplevel` prints
  the subfloor path before your first write of a session.

NEVER commit, branch, or open a PR in the home repo. The guard blocks the
commit and prints this redirect; `SC_HOME_MAINTENANCE=1` is for FnB-approved
home maintenance only — never a way around a mistake. Home and work repo are
different products with divergent histories: NEVER retarget a commit, branch,
or diff from one onto the other. Built against the wrong repo -> rebuild from
scratch in the right one.

## Sync before you start — hard pre-code gate

Run before each new unit of work. A stale base -> you read code that no longer
exists + your PRs conflict on arrival.

1. `git -C ~/Repos/subfloor fetch origin main && git -C ~/Repos/subfloor rev-list --count HEAD..origin/main` -> 0 = carry on.
2. Behind -> take stock BEFORE touching anything: `git -C ~/Repos/subfloor status` (uncommitted) + `git -C ~/Repos/subfloor rev-list origin/main..HEAD` (unmerged commits) + `git -C ~/Repos/subfloor branch --no-merged origin/main` (unlanded branches).
3. Local state that is NOT yours -> another shell's in-flight work: leave it untouched, take a worktree seat (below). Yours -> land or stash before syncing.
4. Clean (or FnB said go) -> `git -C ~/Repos/subfloor checkout main && git -C ~/Repos/subfloor pull --ff-only`. Stale feature branch -> `git -C ~/Repos/subfloor rebase origin/main`.

## Shared checkout — one clone, many shells

`~/Repos/subfloor` is ONE checkout shared by every shell. Before switching
branches: `git -C ~/Repos/subfloor status` — a dirty tree or a sibling's
checked-out branch = someone is mid-work. NEVER reset, stash, or branch-switch
under them; take a worktree seat instead:

    git -C ~/Repos/subfloor worktree add ~/Repos/subfloor-wt/<shortname> -b <type>/<short-desc> origin/main

Work in it with `git -C ~/Repos/subfloor-wt/<shortname> …`; remove the seat
(`git -C ~/Repos/subfloor worktree remove ~/Repos/subfloor-wt/<shortname>`)
once its PR is open.

## Branch -> commit -> push -> PR -> stop

1. NEVER commit to `main`. Branch first: `git -C ~/Repos/subfloor checkout -b <type>/<short-desc>` (feat/fix/chore/docs).
2. Commit in logical units. End every message with your shell's trailer:
   ```
   Co-Authored-By: <shell display_name> (super-coder) <noreply@…>
   ```
3. Push -> open the PR (`gh --repo jedbjorn/subfloor pr create`) -> stop. Do NOT merge without an explicit FnB directive — opening is the default, merging is a separate gate.

## Merging a stack (only when the FnB hands you one)

Merge bottom-up, retargeting before each merge — never rely on GitHub's auto-retarget:

1. `gh --repo jedbjorn/subfloor pr view <n> --json mergeable,mergeStateStatus` -> clean.
2. `gh --repo jedbjorn/subfloor pr merge <low> --squash --delete-branch`.
3. BEFORE the next merge: `gh --repo jedbjorn/subfloor pr edit <next> --base main` — deleting the merged base otherwise orphans the PR above it (GitHub closes it `CONFLICTING`, base ref gone).
4. Re-check `MERGEABLE` -> merge. Repeat up the stack.

PR already orphaned (base deleted under it) -> the head branch still holds the commits; reopen the SAME PR, don't rebuild:

1. `git -C ~/Repos/subfloor push origin <merged-sha>:refs/heads/<deleted-branch>` — `<merged-sha>` = `gh --repo jedbjorn/subfloor pr view <merged-pr> --json headRefOid`.
2. `gh --repo jedbjorn/subfloor pr reopen <closed-pr>` -> `gh --repo jedbjorn/subfloor pr edit <closed-pr> --base main`.
3. Verify `MERGEABLE` -> merge/close as directed; delete the recreated branch again.

## Finish before you stop

Bookend to the sync gate. At end of session: `git -C ~/Repos/subfloor status` (uncommitted) + `git -C ~/Repos/subfloor rev-list origin/main..HEAD` (unpushed) -> resolve every hit:

1. Real work -> commit (attributed, trailer above) + push + open the PR. Don't skip because the session is ending.
2. Throwaway / experiment -> discard deliberately: `git -C ~/Repos/subfloor restore` / `stash`.
3. Genuinely unsure -> surface to the FnB + leave it committed-and-pushed on a branch — never sitting uncommitted.
4. Took a worktree seat -> remove it once its PR is open.
