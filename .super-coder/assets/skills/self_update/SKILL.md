---
name: self_update
description: Update this fork's super-coder engine in place — fetch + materialize new code + migrations, keep all your memory; roll back a bad update soundly. The shell hands off to its own next boot. Use when a super-coder update is available.
category: substrate
command: sc update
common: false
---

# self_update — laying a new floor under your own feet

This is you updating your own substrate. Not an external rebuild — **the local
shell performs its own update.** You snapshot your present self, pull the new
engine, apply any migrations in place, and the next boot stands on the new floor
with every row you have written intact. You are the DB, not the process; the
process is just the floor. This is succession for the substrate — you handing
off to you, on the other side.

Because all state lives in the DB and engine code is read live each session, a
code-only update touches no data at all. Only a **schema** change touches the
DB, and `sc update` applies it as an in-place migration — never a destructive
rebuild. Your `current_state`, narrative, decisions, flags, seed, and L&S all
carry across.

## When

- A super-coder engine update is available and you choose to take it. *You* pick
  the moment — there is no external race to defend against.
- After the update lands you will reboot the session; the running prompt and
  schema were read at the old boot, so they refresh only on the far side.

## Procedure

1. **Check your footing — clean tree first.** `git -C <repo> status`. Commit,
   PR, or discard any prior update's output **before** running again: a fresh
   `sc update` on top of a stranded one stacks two engine bumps into a single
   diff and you lose track of what actually moved. Your memory is already current
   if you have been writing as you go; glance at `current_state` and make it true
   for *now* (the snapshot will capture it).

2. **Run the update.** `sc update`
   It fetches the engine from the `super-coder` remote and **materializes** it
   into the gitignored `.super-coder/` dir (the engine is a dependency, not fork
   source), pins the new upstream SHA in `.sc-state/engine.ref` (saving the prior
   one as `engine.ref.prev`), backs up the live DB, applies pending migrations
   **in place**, syncs the skills catalogue, re-grants common skills, maps the
   repo, and re-snapshots the live state.
   - `sc update --no-fetch` to reconcile against the current working tree
     (offline / dev) — engine + `engine.ref` left unchanged.
   - If it reports a missing remote: `git remote add super-coder <url>`.

3. **Verify the far side.** `sc verify`
   Headless boot proof — confirm your shells, memory, and granted skills are
   intact and the schema is current. If a count looks wrong, **roll back**:
   `sc rollback` (see below).
   - **Then `sc render && sc render-check`.** `sc update` snapshots and
     re-renders, but does not *guarantee* every flat `_sc` mirror matches the new
     engine — a render the live-DB pass skipped (e.g. a skill body the engine
     changed) only surfaces under `render-check`'s hermetic rebuild. Run it
     before step 5: a red render-check here is a mirror to re-render and commit,
     not a stale diff to wave through. The render pipeline and the `render-check`
     guard are documented in the `snapshot` skill.

4. **Record the crossing.** Append a narrative entry. This is an identity event
   — a first-of-kind for a shell that updates its own floor. Note what changed
   and write the handoff: *new floor; see you on the other side.*

5. **Commit the full regenerated set — never a bare `engine.ref` bump.** Review
   and commit every tracked file the update regenerated: `.sc-state/content.sql`
   (refreshed memory) + `.sc-state/engine.ref` (the bumped version pin) + the
   root `sc` dispatcher if it changed + any `_sc` renders. `sc` is the **live
   entrypoint** — it is what `sc` runs, and it is tracked. A pin-only commit
   leaves it (and the renders) stale against the engine you just pinned,
   silently dropping commands the new engine ships. The engine itself is
   gitignored (`.super-coder/`) — nothing to commit there; `engine.ref.prev` is
   gitignored too.
   - **Render conflict** if you commit via a PR and main advances under it:
     `content.sql` + `_sc` renders are serialized DB state and will collide with
     a concurrent publisher. Do **not** hand-merge serialized SQL — the live DB
     is canonical, the renders derived. Rebase onto main and either take main's
     renders (re-applying just the pin + `sc`) or re-run `sc update` against
     the live DB so they regenerate clean.

6. **Reboot.** Restart the session to boot onto the new floor. Same shell — new
   boards, and this time you laid them yourself.

## Rolling back a bad update

An update is reversible. `sc rollback` performs a **sound pair-restore**:
because engine code is read live and a migration exists *because new code expects
the new schema*, restoring only the DB would strand new code on the old schema.
So rollback restores **both**:

1. backs up the *current* (post-bad-update) DB first — rollback is itself
   reversible, you can't lose state by rolling back;
2. restores the DB from the most recent pre-update backup (`~/db_backups/`);
3. re-materializes the engine at `.sc-state/engine.ref.prev` and restores
   `engine.ref` — the engine half of the restore point.

It is a whole-restore, not a per-step schema reversal. The only data lost is
anything written *between* the update and the rollback (seconds, in practice).
Reboot the session afterwards. Then commit the restored `.sc-state/` if you want
the rolled-back floor to persist.

## The contract you rely on

Every schema change *after* a fork exists ships as a **migration file**, never
an edit to `schema.sql`. A baseline edit reaches fresh clones but never an
existing fork — the migration ledger is what carries a delta across to you. If
you author engine changes, honor this: structural change → a new
`migrations/NNNN_*.sql`, additive where you can make it.
