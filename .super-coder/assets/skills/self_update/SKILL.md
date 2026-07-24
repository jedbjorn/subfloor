---
name: self_update
description: Update this fork's super-coder engine in place — fetch + materialize new code + migrations, all memory intact; sound rollback. The shell hands off to its own next boot. Use when a super-coder update is available.
category: substrate
command: sc update
common: false
---

# self_update — laying a new floor under your own feet

The local shell updates its own substrate — no external rebuild. All state lives
in the DB and engine code is read live each session, so a code-only update
touches no data; a schema change applies as an in-place migration, never a
destructive rebuild. `current_state`, narrative, decisions, flags, seed, and
L&S all carry across. This is succession for the substrate: you handing off to
you.

## When

- An engine update is available and you choose the moment — no external race.
- The running prompt + schema were read at the old boot -> reboot after the
  update; they refresh only on the far side.

## Procedure

1. **Clean tree first.** `git -C <repo> status` -> clean. Commit, PR, or
   discard any prior update's output BEFORE running again — a fresh `sc update`
   on top of a stranded one stacks two engine bumps into one diff. Glance at
   `current_state` + make it true for now (the snapshot captures it).

2. **Run.** `sc update` — fetches the engine from the `super-coder` remote,
   materializes it into the gitignored `.super-coder/` dir (engine = dependency,
   not fork source), pins the new upstream SHA in `.sc-state/engine.ref`
   (prior saved as `engine.ref.prev`), backs up the live DB, applies pending
   migrations in place, syncs the skills catalogue, re-grants common skills,
   maps the repo, re-snapshots the live state.
   - `sc update --no-fetch` = reconcile against the current working tree
     (offline / dev); engine + `engine.ref` unchanged.
   - Missing-remote error -> `git remote add super-coder <url>`.

3. **Verify.** `sc verify` — headless boot proof: shells, memory, granted
   skills intact + schema current. Wrong count -> `sc rollback` (below).
   - Then `sc render && sc render-check` before step 5. `sc update` re-renders
     from the live DB, which can skip a change the new engine shipped (e.g. a
     skill body) — only `render-check`'s hermetic rebuild surfaces it. A red
     render-check here = a mirror to re-render + commit, NOT a stale diff to
     wave through. Pipeline + guard details: `snapshot` skill.

4. **Record the crossing.** Append a narrative entry — identity event for a
   shell that updates its own floor. Note what changed + write the handoff.

5. **Commit the full public set.**
   Stage every tracked file the update regenerated: `.sc-state/content.sql`
   (refreshed memory) + `.sc-state/engine.ref` (the pin) + the root `sc`
   dispatcher if it changed + any `_sc` renders. `sc` is the live tracked
   entrypoint — a pin-only commit leaves it and the renders stale against the
   engine just pinned, silently dropping commands the new engine ships.
   `.super-coder/` and `engine.ref.prev` are gitignored — nothing to commit
   there.
   With `artifact_mode=local`, `content.sql` and `_sc` renders stay under
   ignored `.sc-state/local/`; commit the engine pin/dispatcher and other
   genuinely public files only.
   - **Render conflict** (committing via PR while main advances):
     `content.sql` + `_sc` renders are serialized DB state and collide with a
     concurrent publisher. NEVER hand-merge serialized SQL — live DB canonical,
     renders derived. Rebase onto main, then either take main's renders
     (re-applying just the pin + `sc`) or re-run `sc update` against the live
     DB so they regenerate clean.

6. **Reboot** the session -> boot onto the new floor.

## Rolling back a bad update

`sc rollback` = sound pair-restore. Engine code is read live and a migration
exists because new code expects the new schema — restoring only the DB strands
new code on the old schema, so rollback restores both:

1. backs up the current (post-bad-update) DB first — rollback is itself
   reversible;
2. restores the DB from the most recent pre-update backup in
   `~/db_backups/<repo-name>/` (keyed by this fork's repo dir name — distinct
   from any `db_backups/` dir the fork's app keeps at its repo root);
3. re-materializes the engine at `.sc-state/engine.ref.prev` + restores
   `engine.ref`.

Whole-restore, not per-step schema reversal. Only data written between update
and rollback is lost (seconds, in practice). Reboot afterwards; commit the
restored `.sc-state/` if the rolled-back floor should persist.

## The contract you rely on

Every schema change AFTER a fork exists ships as a migration file
(`migrations/NNNN_*.sql`), never an edit to `schema.sql` — a baseline edit
reaches fresh clones but never an existing fork; the migration ledger carries
the delta. Authoring engine changes: structural change -> new migration file,
additive where possible.
