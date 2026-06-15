---
name: git_cleanup
description: Admin-only — triage and clean the repo's git state across main + every worktree. The acting sibling of git_hygiene.py's read pass: delete what's provably merged, preserve (never discard) outstanding work, sync to remote. Use when the FnB asks to tidy/clean git, prune branches, or reconcile worktrees.
category: substrate
common: false
---

# git_cleanup — the act pass over git state

`git_hygiene.py` reads; this acts. It is the mutating sibling of that
reporting-only script, and it is the **admin shell's job alone** — the admin is
the one vantage that sits in the repo root on `main`, sees every worktree on
disk, and is exempted from the branch-guard. No working shell runs this; a
working shell tidies only its own worktree via the `git` skill.

**What the launcher already does for you.** Since #119, `git_prune.py` deletes
the *provably-merged* branch subset automatically on every boot (the same
`stale` set described in Tier A.1), repo-global across the fork's worktrees — so
in practice you will often find that tier already clear. This pass remains the
backstop for everything the automation deliberately won't touch: merges it could
not prove (a `gh`-down boot keeps the branch), dirty worktrees, outstanding
unpushed work, `main` fast-forward, and remote-ref pruning. Don't be surprised by
an empty Tier A; do still run the report.

The governing asymmetry — internalize it before you touch anything:

> You can **prove** something is safe to **delete** (its PR shows MERGED).
> You almost never have proof that uncommitted work is **disposable**.
> So: delete only on evidence; **preserve by default; discard only on the
> FnB's explicit per-item OK.** When unsure, surface — never guess destructively.

## How to investigate — scripts first, always

The two read-only scripts compute the whole state in one efficient pass. Lean on
them in this order, cheapest-and-most-authoritative first:

1. **The scripts** — `git_hygiene.py` (git state) + `shell_liveness.py` (who's
   live). Run them first and *read their output*; don't re-derive by hand what a
   single pass already gives you.
2. **Git history** — when a script's verdict is ambiguous (a branch's merge state
   is `null`, an unexpected dirty file): `git -C <path> log`, `reflog`,
   `show` to settle it.
3. **The code / files** — read the working-tree contents last, only when history
   doesn't explain what you're looking at.

## Step 1 — Read the state (never skip)

```bash
python3 .super-coder/scripts/git_hygiene.py --text       # git: dirty/stale/clean
python3 .super-coder/scripts/shell_liveness.py --text     # who has a live session
```
(Both have a JSON mode — no `--text` — to drive decisions programmatically.)

`git_hygiene` gives you, in one pass: every worktree (path, branch, dirty count,
sample files, ahead/behind) and every local branch's staleness (`merged` = true /
false / null-unknown, with PR number). Do not re-derive any of this by hand —
consume it. If `gh` was unavailable the JSON says so (`gh_available: false`);
treat every `merged: null` as **unknown**, not safe.

`shell_liveness` tells you which shells have a live harness session *right now*
(read from `/proc` cwd — instant, self-cleaning, no staleness). You are running
this, so your OWN session shows up as the repo-root session (`is_self`) — that is
**expected, not a blocker**. The gate is about *other* shells (see Tier C).

## Step 2 — Triage into three tiers

Walk the report and sort every item into exactly one tier. Act top-down.

### Tier A — Auto-safe (evidence-backed; act without asking)

Only these three. Each has hard proof of safety:

1. **Merged-PR branches.** A branch with `merged: true`, `is_base: false`,
   `checked_out: false`. Squash-merge is the project default, so its commits are
   *not* ancestors of main and `git branch -d` will refuse — that refusal is
   expected, not a signal to stop. Confirm the report says MERGED, then:
   ```bash
   git branch -D <branch>
   ```
   `git_prune.py` already does exactly this at boot, so most of these are gone
   before you look. What survives to here is the residue: branches merged since
   the last boot, or merged during a `gh`-down boot that couldn't prove it.
2. **Dead remote-tracking refs.**
   ```bash
   git fetch --prune
   ```
3. **`main` behind origin, clean tree.** The admin's root tree only:
   ```bash
   git pull --ff-only          # never a plain pull/merge on main — no merge bubbles
   ```
   If `--ff-only` refuses, main has diverged → that is Tier B/C, not auto.

Never auto-delete a `merged: null` (unknown) branch, an `is_base` branch
(`main` or any `shell/<shortname>` — those are long-lived moving bases), or a
branch currently checked out in a worktree.

### Tier B — Preserve outstanding work (propose, then act)

Real work that is not yet safely landed. **Default is preserve, never discard.**

- **Unpushed commits** on a branch (`ahead > 0`): real, captured work missing
  from the remote. Propose push + PR. Show the FnB the commits
  (`git -C <path> log origin/<base>..HEAD --oneline`) first.
- **The admin's OWN root tree is dirty** (uncommitted): cut a feature branch off
  main, commit, push, PR — the ordinary `git` skill flow. Show the diff and the
  proposed message; get the OK before committing. Never discard the admin tree's
  dirt without explicit instruction.

### Tier C — Other shells' dirty worktrees (gated; preserve-only)

Any *other* shell's worktree (`is_main: false`) with `dirty > 0`. Two hazards
make this the careful tier:

1. **Attribution.** A commit made in `shell/<shortname>`'s worktree must carry
   *that shell's* trailer, not the admin's. Map the worktree branch
   (`shell/<shortname>`) → shell, and look up its `display_name`:
   ```bash
   sqlite3 .super-coder/shell_db.db \
     "SELECT display_name FROM shells WHERE shortname='<shortname>' AND is_deleted=0;"
   ```
   Use it in the trailer: `Co-Authored-By: <display_name> (super-coder) <noreply@…>`.
2. **Liveness.** Committing files is itself non-destructive, but moving a
   *mid-session* shell's worktree onto a new feature branch underneath it stomps
   that live session. `shell_liveness.py` settles this authoritatively — read its
   verdict for the worktree's shell:
   - **`safe_to_clean_all: true`** — you are the only live shell; every worktree
     is dormant → clear to act on all of them.
   - **shortname in `active_other_shells`** — that shell is LIVE → **surface
     only, do not touch its tree.** The others remain safe.
   - **`indeterminate > 0`** — a harness process whose cwd you couldn't read
     (another OS user, say). Do **not** assume all-clear → surface.

When the verdict clears a worktree (its shell is not live), preserve it:
```bash
WT=.sc-worktrees/<shortname>
git -C $WT checkout -b <type>/<short-desc>           # feature branch off its HEAD
git -C $WT add -A && git -C $WT commit -m "<msg>

Co-Authored-By: <display_name> (super-coder) <noreply@…>"
git -C $WT push -u origin <type>/<short-desc>
gh pr create --repo <owner/repo> --head <type>/<short-desc> --fill   # open, never merge
```

3. **Tell the shell what you did in its tree.** A shell must never boot to find
   its worktree silently rearranged. After acting, message the owning shell (see
   the `messaging` skill) so it discovers the change on its next boot:
   ```bash
   ./sc mem message send <shortname> 'git_cleanup: your worktree had uncommitted work. I preserved it on branch `<type>/<short-desc>` and opened PR #<n>. Your tree now sits on that branch — `git checkout shell/<shortname>` to return to your base.'
   ```
   Also report the same to the FnB. If the worktree could not be acted on (shell
   was live, or indeterminate), no message — it was left untouched.

## Hard nevers

- **Never `git checkout -- `, `git reset --hard`, `git clean`, or `git stash
  drop` on uncommitted work without the FnB's explicit per-item OK.** Preserve
  is reversible; discard is not.
- **Never commit another shell's work under the admin's attribution.**
- **Never act on a worktree whose shell may be live.** Surface instead.
- **Never merge a PR.** Opening is the default; merging is the FnB's gate
  (see the `git` skill).
- **Never delete a branch carrying unmerged, un-PR'd work** — a deleted branch
  with no PR is lost work.
- **Never touch the engine.** `.super-coder/` is gitignored materialized
  dependency, not your code.

## Step 3 — Report

Close with a tight summary: what was deleted (with evidence — PR #), what was
pushed/PR'd (with links), what was surfaced and is waiting on the FnB's call,
and the final `git_hygiene.py --text` so the FnB sees the after-state. If
nothing was outstanding: say so and stop.
