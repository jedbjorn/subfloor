---
name: engine_surgery
description: Procedure for changing the engine you are running — pull/reconcile/restart cadence and their costs, three-artifact engine-skill commits with a hermetic mirror render, migrating the live DB safely, and verifying claims about engine code against the remote rather than a possibly-stale checkout. SOURCE-REPO ONLY; a fork consumes the engine as a pinned dependency and never edits it. Load before touching .super-coder/ in the repo that owns it.
category: craft
common: false
---

# engine_surgery — changing the engine you are running

This repo IS the engine. Every shell here runs on the code it edits, reads a DB
it migrates, and is served by a process started from the tree it commits to.
That is surgery on a moving car, and it has one characteristic failure mode:
**a command answers confidently from a target you did not mean.**

**Fork shells never load this.** A fork consumes `.super-coder/` as a gitignored
dependency pinned by `engine.ref` and updates it with `./sc update`; it never
authors engine changes. Granted only in the source repo.

## The four trees, and which one bites

| tree | what it is | who keeps it current |
|---|---|---|
| your worktree | `.sc-worktrees/<shortname>` — your cwd, your branch | you; boot reports it as `sync:` |
| the main checkout | resolves your `./sc`, hosts the live DB, runs the server | admin / the FnB; boot reports it as `floor:` |
| the running process | code already imported — changes only on restart | the FnB |
| `origin/main` | the truth | whoever merged last |

`sc:11-21` derives the engine root from git's **common dir**, so `./sc` from any
worktree reads the MAIN CHECKOUT. Being current in your own tree tells you
nothing about it. Read the `floor:` line in ACTIVE SESSION.

**Verify any claim about engine code against the remote:**

```
git show origin/main:<path>     # correct
./sc help | grep <thing>        # answers from the main checkout — may be stale
```

Three wrong answers in one session came from skipping that: a help query, a
pending-migration check that came back empty because it globbed a stale
migrations dir and nearly made a reconcile a silent no-op, and dormant PR
watches against a stale running floor.

## EDIT IN YOUR WORKTREE — scripted writes bypass the branch guard

`branch-guard.sh` blocks harness file-edit tools from writing to a
default-branch checkout. **It does not see writes made by a script.** A
`cd /home/j3d1/super-coder && python3 -c "...patch..."` lands on `main`,
uncommitted, with no warning — and your worktree stays clean, so `git status`
there reassures you nothing happened.

Recovery, if you find edits on the wrong tree:

```
git -C <main-checkout> diff > /tmp/x.patch
git apply /tmp/x.patch                                  # in your worktree
git -C <main-checkout> checkout -- <files>
```

Prefer the harness edit tools, which are guarded. If you script an edit, `cd` to
your worktree or use absolute worktree paths, and check `git status` in **both**
trees afterwards.

## Cadence — pull often, restart rarely

| action | cost | fixes |
|---|---|---|
| pull the main checkout | cheap, safe, no session impact | stale reads |
| apply pending migrations | low; back up first | stale DB rows |
| `./sc update` + restart | refuses on live Interface state; **restart kills live sessions** | stale running process |

Pull after every merge. Reconcile and restart at **sprint boundaries** — never
mid-sprint, because a restart kills working devs and swapping the floor under an
in-flight unit is its own hazard. The restart is the FnB's call.

## Migrating the live DB

The DB you migrate is the one every shell is using and the server has open.

1. **Fast-forward the main checkout first.** Pending-migration checks glob its
   `migrations/` dir, so a stale tree reports nothing pending and the reconcile
   silently does nothing.
2. **Name the DB path explicitly.** `./sc migrate` from a worktree resolves to
   the main checkout's DB and says so nowhere (issue #569). Prefer
   `python3 .super-coder/scripts/migrate.py <explicit-path>`.
3. **Back up first**, WAL-safe, via SQLite's online backup rather than a file
   copy:

   ```python
   src = sqlite3.connect(LIVE); dst = sqlite3.connect(BACKUP)
   with dst: src.backup(dst)
   ```

4. **Data-only migrations are safe under a running server** (row updates, no
   DDL). Schema changes want the restart window.
5. **Verify by read-back**, not by the migrate command's own output.

## Engine skill edits are a three-artifact commit

All three, or CI goes red even when tests pass:

1. the source asset at `.super-coder/assets/skills/<name>/SKILL.md`;
2. a **trailing reseed migration** so existing installations converge — full-body
   upsert, `INSERT … ON CONFLICT(name) DO UPDATE SET`, patterned on the most
   recent `*_reseed_*.sql`. Generate it FROM the asset rather than hand-writing
   it, and store the body exactly as the guards read it
   (`split("---", 2)[2].strip()`) — an unstripped body fails three freshness
   guards;
3. the re-rendered mirror.

**Render the mirror through the guard's own hermetic path**, never from the live
DB, so it cannot drift from what CI rebuilds:

```python
import render_check as rc, flat
rc._build_tracked_db(db)                  # schema → migrations → content.sql
flat.render_visibility(con, root=rc.ACTIVE_ROOT)
```

In **local artifact mode** the mirror lives under the ignored `.sc-state/local/`
and is not in the diff — the commit is then two artifacts, and `render-check`
still proves the migration.

Run the guard **from your own worktree** — `./sc render-check` resolves to the
main checkout and will judge code you are not committing (flag #47):

```
python3 .super-coder/scripts/render_check.py
```

## Adding a source-repo-only skill

Seeds carry **skills, not grants**: `0001` inserts skill rows and no
`shell_skills`. Grants happen at shell creation — every shell auto-gets
`common=1` skills, plus its flavor template's named opt-ins.

So a skill with **`common: false` and no entry in any flavor template** seeds
into a fork's catalogue but is never granted to a fork shell. Grant it here:

```
./sc skill grant <name> <shell> [<shell>…]
```

## Stance

- A command reporting success against a target you did not intend is the house
  defect. Name the target; verify the effect.
- Never `SC_ADMIN=1` past a gate to save a step — the publish path is gated on
  purpose.
- Never auto-sync the main checkout from a shell. `sync_worktree` may
  `reset --hard` a shell base; the main checkout is the running server's tree and
  a reset discards whatever the operator has in flight.
- Assert before you replace; read back after you write. A non-matching string
  replace does nothing and reports success.
