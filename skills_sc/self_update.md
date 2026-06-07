---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# self_update

Update this fork's super-coder engine in place — fetch + materialize new code + migrations, keep all your memory; roll back a bad update soundly. The shell hands off to its own next boot. Use when a super-coder update is available.

**Category:** substrate  ·  **Command:** `./sc update`

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
DB, and `./sc update` applies it as an in-place migration — never a destructive
rebuild. Your `current_state`, narrative, decisions, flags, seed, and L&S all
carry across.

## When

- A super-coder engine update is available and you choose to take it. *You* pick
  the moment — there is no external race to defend against.
- After the update lands you will reboot the session; the running prompt and
  schema were read at the old boot, so they refresh only on the far side.

## Procedure

1. **Check your footing.** `git -C <repo> status` — know what is uncommitted.
   Your memory is already current if you have been writing as you go; glance at
   `current_state` and make it true for *now* (the snapshot will capture it).

2. **Run the update.** `./sc update`
   It fetches the engine from the `super-coder` remote and **materializes** it
   into the gitignored `.super-coder/` dir (the engine is a dependency, not fork
   source), pins the new upstream SHA in `.sc-state/engine.ref` (saving the prior
   one as `engine.ref.prev`), backs up the live DB, applies pending migrations
   **in place**, syncs the skills catalogue, re-grants common skills, maps the
   repo, and re-snapshots the live state.
   - `./sc update --no-fetch` to reconcile against the current working tree
     (offline / dev) — engine + `engine.ref` left unchanged.
   - If it reports a missing remote: `git remote add super-coder <url>`.

3. **Verify the far side.** `./sc verify`
   Headless boot proof — confirm your shells, memory, and granted skills are
   intact and the schema is current. If a count looks wrong, **roll back**:
   `./sc rollback` (see below).

4. **Record the crossing.** Append a narrative entry. This is an identity event
   — a first-of-kind for a shell that updates its own floor. Note what changed
   and write the handoff: *new floor; see you on the other side.*

5. **Commit.** Review and commit your fork-owned state: `.sc-state/content.sql`
   (refreshed memory) + `.sc-state/engine.ref` (the bumped version pin) + any
   `_sc` renders. The engine itself is gitignored — there is nothing under
   `.super-coder/` to commit.

6. **Reboot.** Restart the session to boot onto the new floor. Same shell — new
   boards, and this time you laid them yourself.

## Rolling back a bad update

An update is reversible. `./sc rollback` performs a **sound pair-restore**:
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
