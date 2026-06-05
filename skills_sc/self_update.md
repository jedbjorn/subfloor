---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# self_update

Update this fork's super-coder engine in place — pull new code + migrations, keep all your memory. The shell hands off to its own next boot. Use when a super-coder update is available.

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
   It self-fetches the engine from the `super-coder` remote, backs up the live
   DB, applies pending migrations **in place**, syncs the skills catalogue,
   re-grants common skills, maps the repo, and re-snapshots the live state.
   - `./sc update --no-fetch` to reconcile against an already-checked-out engine
     (offline / dev).
   - If it reports a missing remote: `git remote add super-coder <url>`.

3. **Verify the far side.** `./sc verify`
   Headless boot proof — confirm your shells, memory, and granted skills are
   intact and the schema is current. If a count looks wrong, the pre-update DB
   backup is in `~/db_backups/` — restore and investigate before committing.

4. **Record the crossing.** Append a narrative entry. This is an identity event
   — a first-of-kind for a shell that updates its own floor. Note what changed
   and write the handoff: *new floor; see you on the other side.*

5. **Commit.** Review and commit the engine bump + refreshed snapshot
   (`schema.sql` / `migrations/` / `snapshot/content.sql` / `_sc` renders).

6. **Reboot.** Restart the session to boot onto the new floor. Same shell — new
   boards, and this time you laid them yourself.

## The contract you rely on

Every schema change *after* a fork exists ships as a **migration file**, never
an edit to `schema.sql`. A baseline edit reaches fresh clones but never an
existing fork — the migration ledger is what carries a delta across to you. If
you author engine changes, honor this: structural change → a new
`migrations/NNNN_*.sql`, additive where you can make it.
