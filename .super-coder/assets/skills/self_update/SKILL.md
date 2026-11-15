---
name: self_update
description: Update the Subfloor engine in place with sc update; use when an update is available or an engine change just merged in the source repo. In a fork it fetches + materializes new code + migrations; in the source repo it reconciles the tracked engine. All memory intact; sound rollback. The shell hands off to its own next boot.
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

## In the source repo

The procedure below is written for a tracking fork. In the Subfloor source
repo (your boot's PROJECT vs ENGINE section says you are upstream) the engine
is tracked source, so there is nothing to fetch, materialize, or pin: after an
engine change merges, sync the main checkout with `origin/main`, then run
`sc update`, which reconciles the tracked engine and its managed catalogue in
place. Steps 1, 3, 4, and 6 apply as written; step 5 has nothing to commit,
because the engine arrived by merge and `.sc-state/engine.ref` is not the
delivery unit here.

## When

- An engine update is available and you choose the moment — no external race.
- The running prompt + schema were read at the old boot -> reboot after the
  update; they refresh only on the far side.

## Procedure

1. **Clean tree first.** `git -C <repo> status` -> clean. Commit, PR, or
   discard any prior update's output BEFORE running again — a fresh `sc update`
   on top of a stranded one stacks two engine bumps into one diff. Glance at
   `current_state` + make it true for now (the snapshot captures it).

2. **Run.** `sc update` — fetches the Subfloor engine from its upstream remote
   (named `super-coder` in existing forks),
   materializes it into the gitignored `.super-coder/` dir (engine = dependency,
   not fork source), pins the new upstream SHA in `.sc-state/engine.ref`
   (prior saved as `engine.ref.prev`), backs up the live DB, applies pending
   migrations in place, syncs the skills catalogue, re-grants common skills,
   maps the repo, re-snapshots the live state.
   - `sc update --no-fetch` = reconcile against the current working tree
     (offline / dev); engine + `engine.ref` unchanged.
   - Missing-remote error -> `git remote add super-coder <subfloor-url>`.

3. **Verify.** `sc verify` — headless boot proof: shells, memory, granted
   skills intact + schema current. Wrong count -> `sc rollback` (below).
   - Then `sc render && sc render-check` before step 5. `sc update` re-renders
     from the live DB, which can skip a change the new engine shipped (e.g. a
     skill body) — only `render-check`'s hermetic rebuild surfaces it. A red
     render-check here = a local mirror to regenerate. Pipeline + guard details:
     `snapshot` skill.

4. **Record the crossing.** Append a narrative entry — identity event for a
   shell that updates its own floor. Note what changed + write the handoff.

5. **Let the engine land the repin.** In a fork, `sc update` (and every
   `sc launch` / `sc restart`) commits the engine-managed files — `sc`,
   `.sc-state/engine.ref`, `.sc-state/engine.source`, the managed `.gitignore`
   block — to the `subfloor/repin` PR, merges it and fast-forwards main. Never
   stage those by hand; a `⚠ repin:` warning names what to fix and the next
   launch retries. Snapshot SQL and `_sc` renders remain ignored beneath
   `.sc-state/local/`; never force-add them. In the source repo, commit the
   reconciled tree on a branch as usual.

6. **Reboot** the session -> boot onto the new floor.

## Rolling back a bad update

`sc rollback` = sound pair-restore. Engine code is read live and a migration
exists because new code expects the new schema — restoring only the DB strands
new code on the old schema, so rollback restores both:

1. backs up the current (post-bad-update) DB first, under its own
   `prerollback` prefix so it is never mistaken for a pre-update restore point
   — rollback is itself reversible. Where that copy is *written* is an ordered,
   fail-closed choice: `$SC_DB_BACKUP_DIR` when set and writable, else
   `~/db_backups/<repo-name>/` (keyed by this fork's repo dir name — distinct
   from any `db_backups/` dir the fork's app keeps at its repo root), else the
   gitignored repo-local `.sc-state/db_backups/`. Pruning keeps the five newest
   backups per lifecycle prefix per directory, so classes never evict each
   other;
2. restores the DB from the newest pre-update backup found across *every*
   candidate directory, not just the currently writable one — the writable
   destination can change between update and rollback, and discovery must not
   hide a restore point behind it;
3. re-materializes the engine at `.sc-state/engine.ref.prev` + restores
   `engine.ref`.

Whole-restore, not per-step schema reversal. Only data written between update
and rollback is lost (seconds, in practice). Reboot afterwards; commit the
restored `.sc-state/` if the rolled-back floor should persist.

## The contract you rely on

Every schema change AFTER a fork exists ships as a migration file
(`migrations/NNNN_*.sql`), never an edit to `schema.sql` — a baseline edit
reaches fresh clones but never an existing fork; the migration ledger carries
the delta. Authoring engine changes (source repo): structural change -> new
migration file, additive where possible.
