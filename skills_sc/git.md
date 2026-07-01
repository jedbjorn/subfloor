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
dependency (refreshed by `sc update`); don't commit it or edit it as if it
were your code. Engine changes are authored upstream in super-coder.

## Sync before you start — a hard pre-code gate

**Before you touch code, reconcile your own tree.** Every session, and again
before each new unit of work — not "when convenient." This is *your* job, not the
admin's; a shell that self-syncs is a shell the admin never has to clean up after.

Your `shell/<shortname>` branch is a **moving base pinned to `origin/main`**,
not a content branch — work happens on feature branches cut from it. A worktree
is born at first-boot HEAD and drifts as other shells' PRs merge; a stale base
means you read code that no longer exists and your PRs conflict on arrival.

The launcher checks drift at every boot and **auto-syncs when provably nothing
can be lost** (on the base branch, clean tree, no local-only commits). Read the
`sync:` line in ACTIVE SESSION above. If it auto-synced and you've done nothing
since, you're current — carry on. Otherwise — it says **NOT auto-synced**, or
you're mid-session about to start new work — run the gate yourself:

1. `git fetch origin main && git rev-list --count HEAD..origin/main` — behind
   count. Zero → carry on.
2. Behind → take stock of local state BEFORE touching anything:
   `git status` (uncommitted), `git rev-list origin/main..HEAD` (unmerged
   commits), `git branch --no-merged origin/main` (unlanded branches).
3. Anything local → **surface it to the FnB first**: list the commits/files and
   ask — land it, stash it, or discard? No sync without their call (soft gate).
4. Clean (or FnB said go) → `git checkout shell/<shortname> && git reset --hard
   origin/main`. **Never `git pull`/merge on the base branch** — merge bubbles
   accumulate forever, and your own squash-merged work replays as conflicts.
5. Never reset a *feature* branch to `origin/main` — only the base. A stale
   feature branch gets `git rebase origin/main` if it must catch up.

## Branch → commit → push → PR → stop

1. **Never commit straight to the default branch.** Branch first:
   `git checkout -b <type>/<short-desc>` (feat/fix/chore/docs).
   *Admin-flavor exception:* the admin shell boots in the repo root on `main`
   and maintains it directly (engine updates, migrations, applying approved
   patches) — the branch-guard exempts it. If that's you, commit to main is
   your mandate; start each session with `git pull --ff-only` so you maintain
   the real main. Every other shell branches first, always.
2. Commit in logical units. End the message with your shell's attribution so
   parallel shells' work stays legible:
   ```
   Co-Authored-By: <shell display_name> (super-coder) <noreply@…>
   ```
3. Push, open a **PR**, then **stop**. **Do not merge** without an explicit
   directive from the FnB — opening is the default, merging is a separate gate.

## Merging a stack (when the FnB has you land one)

Merging stays the FnB's call (above). When they *do* hand you a stack to land,
merge **bottom-up, retargeting before each merge — never rely on GitHub's
auto-retarget**:

1. Check the stack is clean: `gh pr view <n> --json mergeable,mergeStateStatus`.
2. Merge the lowest PR: `gh pr merge <low> --squash --delete-branch`.
3. **Before** the next merge, re-root it onto `main`: `gh pr edit <next> --base
   main`. Deleting the merged base otherwise orphans the PR above it (GitHub
   closes it `CONFLICTING`, base ref gone).
4. Re-check that PR is `MERGEABLE`, then merge. Repeat up the stack.

If a PR was already orphaned (its base was deleted under it), nothing is lost —
the head branch still holds the commits. Recreate the base ref so the *same* PR
can reopen, rather than rebuilding it:

1. `git push origin <merged-sha>:refs/heads/<deleted-branch>`
   (`<merged-sha>` = `gh pr view <merged-pr> --json headRefOid`).
2. `gh pr reopen <closed-pr>` → `gh pr edit <closed-pr> --base main`.
3. Verify `MERGEABLE`, then delete the recreated branch again.

## Finish before you stop

The bookend to "sync before you start." **Before you go dormant, land or surface
your own work — don't leave a dirty or unpushed tree for the admin to adopt.** A
worktree left with uncommitted or unpushed work is exactly what forces the admin's
`git_cleanup` to map attribution, check your liveness, and commit on your behalf.
Self-finish and that whole tier disappears.

At end of session, take stock — `git status` (uncommitted), `git rev-list
origin/<base>..HEAD` (unpushed) — and resolve it:

1. **Real work** → commit it (attributed, see above), push, open the PR. That's
   the normal flow; just don't skip it because the session is ending.
2. **Throwaway / experiment** → discard it deliberately (`git restore` /
   `git stash`), so the tree is clean.
3. **Genuinely unsure** → surface to the FnB and leave it committed-and-pushed on
   a branch (never just sitting uncommitted) — captured work is recoverable; an
   abandoned dirty worktree is the admin's problem.

Leave your tree either **clean** or **on a pushed branch with a PR**. Nothing
half-done waiting for someone else to finish.

## After a merge — clean up local

Once the FnB merges your PR, tidy local so stale branches don't accumulate:

1. Re-pin your base onto the merged commit. In a worktree you **cannot**
   `git checkout main` — main is checked out at the repo root, and git refuses
   a branch already checked out in another worktree. Instead:
   `git checkout shell/<shortname> && git fetch origin && git reset --hard
   origin/main`. (Admin shell, repo root: `git pull --ff-only` on main.)
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
ephemeral rollback pointer). From a shell **worktree you commit your project's
own files** — the code/config you edited there. You do **not** hand-commit the
serialized DB state: `.sc-state/content.sql` (the memory the `.db` rebuilds
from), `.sc-state/engine.ref` (the engine pin), and the tracked `_sc` renders are
written by `sc` to the **main checkout root** (where the shared engine + DB
live), not your worktree — so they aren't even present to stage from your branch.
Getting that text into the repo is the GUI **Publish** button (or the admin shell
on `main`) — see 'After DB work' below. (In the super-coder SOURCE repo only,
`schema.sql` + `migrations/` are tracked too — there the engine *is* the project.)

## After DB work — snapshot persists it; Publish puts it in the repo

Your DB edits live only in the live `.db` until serialized, so run `sc
snapshot` (+ `sc render` if docs/roadmap/skills changed) — that's the "save my
work" step, so a `sc rebuild` can't lose it. But the serialization lands at the
**main checkout root**, NOT your worktree (the shared engine + DB live there), so
don't try to commit `content.sql` or the `_sc` renders onto your branch — from a
worktree they aren't there. Committing that text to the repo is the GUI
**Publish** button (snapshot → render → commit → push → PR on `sc_gui_content`),
or the admin shell working in the repo root on `main`. Your feature-branch PRs
carry your project files; the serialized DB content is published separately. See
the `snapshot` skill.

## Notes

- Confirm you're in the intended repo before destructive ops (`git -C` if ever in
  doubt).
- Multi-shell: shells each boot into their own git worktree at
  `.sc-worktrees/<shortname>/` on branch `shell/<shortname>` — a moving base
  the launcher keeps pinned to `origin/main` (see 'Sync before you start').
  Parallel shells never share a cwd — worktree isolation is automatic. The
  admin shell is the one exception: it boots in the repo root on `main`.
- Preview UI work: because you edit in your worktree, your changes do NOT show on
  the fork's main dev server. `sc preview` runs a router that serves every
  shell's worktree UI live (HMR) on the fork's `dev_port`, one subdomain each:
  `http://<shortname>.localhost:<dev_port>/`. The `post-commit` hook prints your
  URL after each commit — surface that line to the FnB so they can eyeball the
  change. If preview isn't running, start it once from the main checkout:
  `sc preview`.
