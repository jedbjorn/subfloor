---
name: git_cleanup
description: Admin-only — triage and clean the repo's git state across main + every worktree. The acting sibling of git_hygiene.py's read pass: delete what's provably merged, preserve (never discard) outstanding work, sync to remote. Use when the FnB asks to tidy/clean git, prune branches, or reconcile worktrees.
category: substrate
common: false
---

# git_cleanup — the act pass over git state

`git_hygiene.py` reads; this acts on its report. **Admin shell only** — the one vantage at the repo root on `main`, seeing every worktree, exempt from the branch-guard. A working shell NEVER runs this; it tidies only its own worktree via the `git` skill.

Governing asymmetry: a MERGED PR = proof a branch is safe to delete; uncommitted work has NO proof it is disposable. Delete only on evidence; preserve by default; discard only on the FnB's explicit per-item OK. Unsure -> surface, never guess destructively.

Expect the report to be quiet — still run it:

- Since #119, `git_prune.py` deletes the provably-merged branch set (Tier A.1's `stale` set, repo-global) at every boot -> Tier A often already clear. This pass = backstop for what automation won't touch: `gh`-down unprovable merges, dirty worktrees, unpushed work, `main` fast-forward, remote-ref pruning.
- Working shells self-finish (sync before build, land/surface before stop) -> Tier B/C should be rare. A full Tier B/C = a shell skipped its finish gate -> fix it AND send that shell a note, not a silent fix.

## Investigation order — scripts first, always

1. Scripts: `git_hygiene.py` (git state) + `shell_liveness.py` (who's live). Read their output; never re-derive by hand what one pass gives you.
2. Git history — only when a verdict is ambiguous (`merged: null`, unexpected dirty file): `git -C <path> log` / `reflog` / `show`.
3. Working-tree contents — last, only when history doesn't explain it.

## Step 1 — Read the state (never skip)

```bash
python3 .super-coder/scripts/git_hygiene.py --text       # git: dirty/stale/clean
python3 .super-coder/scripts/shell_liveness.py --text     # who has a live session
```
Drop `--text` for JSON when driving decisions programmatically.

- `git_hygiene` -> every worktree (path, branch, dirty count, sample files, ahead/behind) + every local branch's staleness (`merged` = true / false / null-unknown, with PR number). `gh_available: false` in the JSON -> treat every `merged: null` as unknown, never safe.
- `shell_liveness` -> which shells have a live harness session right now (read from `/proc` cwd — instant, self-cleaning). Your OWN session shows as the repo-root `is_self` entry — expected, not a blocker; the gate is about OTHER shells (Tier C).

## Step 2 — Triage into three tiers, act top-down

Sort every report item into exactly one tier.

### Tier A — auto-safe (act without asking). Only these three:

1. Merged-PR branches — `merged: true` + `is_base: false` + `checked_out: false`:
   ```bash
   git branch -D <branch>
   ```
   Squash-merge is the project default -> `git branch -d` refuses; that refusal is expected, not a stop signal. What survives boot-time `git_prune.py` is residue: merged since the last boot, or merged during a `gh`-down boot.
2. Dead remote-tracking refs:
   ```bash
   git fetch --prune
   ```
3. `main` behind origin + clean tree (admin's root tree only):
   ```bash
   git pull --ff-only          # never a plain pull/merge on main — no merge bubbles
   ```
   `--ff-only` refuses -> main diverged -> Tier B/C, not auto.

NEVER auto-delete: a `merged: null` branch, an `is_base` branch (`main` or any `shell/<shortname>` — long-lived moving bases), or a branch checked out in a worktree.

### Tier B — outstanding work (propose -> FnB OK -> act). Preserve, never discard.

- Unpushed commits (`ahead > 0`): show the FnB `git -C <path> log origin/<base>..HEAD --oneline` first -> propose push + PR.
- Admin's OWN root tree dirty: show the diff + proposed message -> on OK, cut a feature branch off main, commit, push, PR (the ordinary `git` skill flow). NEVER discard the admin tree's dirt without explicit instruction.

### Tier C — other shells' dirty worktrees (gated; preserve-only)

Any other shell's worktree: `is_main: false` + `dirty > 0`.

1. **Liveness gate.** Committing files is non-destructive, but re-branching a worktree under a mid-session shell stomps that live session. Read the `shell_liveness` verdict:
   - `safe_to_clean_all: true` -> every worktree dormant -> act on all.
   - shortname in `active_other_shells` -> that shell is LIVE -> surface only, do NOT touch its tree. The others remain safe.
   - `indeterminate > 0` -> a harness process whose cwd was unreadable (another OS user, say) -> do NOT assume all-clear -> surface.
2. **Attribution.** The commit carries THAT shell's trailer, never the admin's. Map the branch `shell/<shortname>` -> shell:
   ```bash
   sc sql \
     "SELECT display_name FROM shells WHERE shortname='<shortname>' AND is_deleted=0;"
   ```
   Trailer: `Co-Authored-By: <display_name> (super-coder) <noreply@…>`.
3. **Preserve** (shell cleared as not live):
   ```bash
   WT=.sc-worktrees/<shortname>
   git -C $WT checkout -b <type>/<short-desc>           # feature branch off its HEAD
   git -C $WT add -A && git -C $WT commit -m "<msg>

   Co-Authored-By: <display_name> (super-coder) <noreply@…>"
   git -C $WT push -u origin <type>/<short-desc>
   gh pr create --repo <owner/repo> --head <type>/<short-desc> --fill   # open, never merge
   ```
4. **Message the owning shell** — it must never boot to a silently rearranged tree (see the `messaging` skill):
   ```bash
   sc mem message send <shortname> 'git_cleanup: your worktree had uncommitted work. I preserved it on branch `<type>/<short-desc>` and opened PR #<n>. Your tree now sits on that branch — `git checkout shell/<shortname>` to return to your base.'
   ```
   Report the same to the FnB. Worktree left untouched (live / indeterminate) -> no message.

## Hard nevers

- NEVER `git checkout -- `, `git reset --hard`, `git clean`, or `git stash drop` on uncommitted work without the FnB's explicit per-item OK — preserve is reversible, discard is not.
- NEVER commit another shell's work under the admin's attribution.
- NEVER act on a worktree whose shell may be live — surface instead.
- NEVER merge a PR — opening is the default; merging is the FnB's gate (see the `git` skill).
- NEVER delete a branch carrying unmerged, un-PR'd work — no PR = lost work.
- NEVER touch the engine — `.super-coder/` is a gitignored materialized dependency, not your code.

## Step 3 — Report

Close with: deleted (evidence — PR #), pushed/PR'd (links), surfaced and awaiting the FnB's call, + a final `git_hygiene.py --text` as the after-state. Nothing outstanding -> say so and stop.
