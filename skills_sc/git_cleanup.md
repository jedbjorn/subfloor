---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# git_cleanup

Admin-only — triage and clean the repo's git state across main + every worktree. The acting sibling of git_hygiene.py's read pass: delete what's provably merged, preserve (never discard) outstanding work, sync to remote. Use when the FnB asks to tidy/clean git, prune branches, or reconcile worktrees.

**Category:** substrate

---

# git_cleanup — the act pass over git state

`git_hygiene.py` reads; this acts. It is the mutating sibling of that
reporting-only script, and it is the **admin shell's job alone** — the admin is
the one vantage that sits in the repo root on `main`, sees every worktree on
disk, and is exempted from the branch-guard. No working shell runs this; a
working shell tidies only its own worktree via the `git` skill.

The governing asymmetry — internalize it before you touch anything:

> You can **prove** something is safe to **delete** (its PR shows MERGED).
> You almost never have proof that uncommitted work is **disposable**.
> So: delete only on evidence; **preserve by default; discard only on the
> FnB's explicit per-item OK.** When unsure, surface — never guess destructively.

## Step 1 — Read the state (never skip)

```bash
python3 .super-coder/scripts/git_hygiene.py --text     # human table
python3 .super-coder/scripts/git_hygiene.py            # JSON, to drive decisions
```

This gives you, in one pass: every worktree (path, branch, dirty count, sample
files, ahead/behind) and every local branch's staleness (`merged` = true /
false / null-unknown, with PR number). Do not re-derive any of this by hand —
consume it. If `gh` was unavailable the JSON says so (`gh_available: false`);
treat every `merged: null` as **unknown**, not safe.

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
   that live session. There is **no reliable liveness timestamp in the DB**
   (`shell_memory_archives` carries only a date). So:
   - Best-effort process probe (hint, not proof):
     ```bash
     ls -la /proc/*/cwd 2>/dev/null | grep '.sc-worktrees/<shortname>'
     ```
   - The real gate is the **FnB's confirmation that the shell is idle.** Ask,
     per worktree. If unconfirmed or the probe shows a live process → **surface
     only, do not act.**

When cleared to preserve another shell's worktree (idle-confirmed):
```bash
WT=.sc-worktrees/<shortname>
git -C $WT checkout -b <type>/<short-desc>           # feature branch off its HEAD
git -C $WT add -A && git -C $WT commit -m "<msg>

Co-Authored-By: <display_name> (super-coder) <noreply@…>"
git -C $WT push -u origin <type>/<short-desc>
gh pr create --repo <owner/repo> --head <type>/<short-desc> --fill   # open, never merge
```
Then tell the FnB what branch the worktree now sits on, so the owning shell
isn't surprised on its next boot.

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
