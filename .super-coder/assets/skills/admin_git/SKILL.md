---
name: admin_git
description: Admin-only Git procedure for the repository root — identify main, fast-forward safely, commit fork engine pins, merge only approved PRs, and preserve every foreign worktree. Use before Admin performs Git maintenance or an authorized merge.
category: substrate
common: false
---

# admin_git — maintain the repository root

Admin owns the root checkout and its `main` branch. Use this procedure for a
specific update, reconciliation, or approved merge; it is not a standing
cleanup pass. The FnB merge gate and the preservation rule remain in force.

## Orient before writing

```bash
git rev-parse --show-toplevel
git branch --show-current
git status --short --branch
git worktree list
```

Proceed only when the top level matches the repository named by the boot
document and the root checkout is on `main`. A dirty root, detached head, or
diverged main is a decision boundary: show the exact state to the FnB before
changing it.

Every other worktree belongs to its shell. Never switch its branch, stash,
reset, clean, move, or remove it. When the FnB explicitly asks for repository-
wide cleanup, load `git_cleanup`; otherwise leave foreign worktrees untouched.

## Fast-forward main

```bash
git fetch origin main
git pull --ff-only origin main
```

Success leaves `main` clean and at the fetched remote head. If `--ff-only`
refuses, stop and report the local/remote commits; never create a merge bubble
or reset main to make the command pass.

## Commit a fork engine pin

In a tracking fork, `.super-coder/` is a materialized dependency and remains
gitignored. After `self_update` succeeds, stage only the durable public update:

```bash
git add .sc-state/engine.ref
git status --short
git commit -m "chore: update super-coder engine pin"
```

Add the root `sc` dispatcher or another public file only when the update
deliberately changed it. Never force-add `.super-coder/`, local snapshots,
rendered `_sc` state, or `.sc-state/engine.ref.prev`. Push the resulting main
commit only within the operator's requested update workflow.

## Merge an approved PR

Merge only after the FnB names or explicitly authorizes the PR. Re-read live
state immediately before acting:

```bash
gh pr view <number> --json url,headRefOid,baseRefName,mergeable,mergeStateStatus,statusCheckRollup
```

Require the expected repository, `baseRefName=main`, the reviewed head, a
mergeable state, and successful required checks. Use the repository's approved
merge method, then `git pull --ff-only origin main`. A changed head, red/pending
check, or merge refusal invalidates the authorization; stop and return the live
evidence instead of overriding it.

For a stack, retarget each remaining PR to `main` before merging the PR above
the one that landed. Never rely on automatic retargeting after a base branch is
deleted.

## Source-repository exception

```bash
git ls-files --error-unmatch .super-coder/schema.sql
```

Exit 0 means this repository authors super-coder itself: `.super-coder/` is
tracked source, not a dependency, and `.sc-state/engine.ref` is not the delivery
unit. Engine implementation still arrives through a Developer branch and PR;
Admin fast-forwards main and merges only the exact approved PR. Apply live
migrations or restart the engine only through their dedicated procedures and
operator-owned recovery window.

## Stop conditions

- No approval -> do not merge.
- Foreign worktree activity -> preserve it and surface it.
- Main cannot fast-forward -> report divergence; do not reset.
- Target repository, PR head, or checks differ from the authorization -> stop.
