---
title: super-coder — Update a fork
tags: [substrate, shells, agentic-coding, harness-agnostic, sqlite]
date: 2026-07-20
project: super-coder
purpose: In-place engine updates, rollback, customize vs upstream vs eject
---

# Update a fork

## Update a fork

> [!class2]
> **UI** Scripts (migrate · rebuild) · **Shells** admin

Ship an improvement to super-coder, pull it into each fork — **in place**, with
no loss of memory. The shell updates its own substrate: it pulls the new engine,
applies new migrations under its own feet, and the next boot stands on the new
floor with every row intact. (The shell-facing version of this is the
`self_update` skill — same procedure, framed as the handoff it is.)

```bash
./sc update                     # fetch + materialize the engine, reconcile in place
git add -A && git commit -m "chore: update super-coder"   # commits only .sc-state/ + _sc
```

`./sc update` fetches the engine from the `super-coder` remote and
**materializes** it into the gitignored `.super-coder/` dir (the engine is a
dependency — code, schema, migrations, skills; your `.sc-state/`, DB, and
`instance.json` are never touched), **pins** the new upstream SHA in
`.sc-state/engine.ref` (keeping the prior one as `engine.ref.prev`), backs up the
live DB, **applies pending migrations in place** (never a rebuild-from-snapshot —
your unsnapshotted in-session writes survive), syncs the skills catalogue
(id-stable, so grants stay valid), re-grants any new common skills, refreshes the
repo map, and re-snapshots the live state. Nothing under `.super-coder/` is
committed — you commit only `.sc-state/` (refreshed `content.sql` + bumped
`engine.ref`) and any `_sc` renders. Then restart the session to boot onto the
new floor.

- `./sc update --no-fetch` reconciles against the current working tree (offline /
  dev) — engine + `engine.ref` unchanged. `--branch <name>` to track a non-`main`
  engine branch. `--ref <tag|sha>` pins the materialize to a specific upstream
  version instead of the branch head — hold a fork at a known-good engine and
  move deliberately.
- Missing remote? `git remote add super-coder https://github.com/jedbjorn/subfloor.git`

> [!class4]
> **Local engine edits block the update — never silently overwritten.** The
> materialize is a wholesale overwrite, so the engine keeps a hash manifest
> (written at install and after every materialize) and `./sc update` refuses
> when an engine file was locally modified since — listing the files and the
> real options: revert the edit, **upstream it** (PR super-coder — the strong
> default), `--force` to knowingly discard it, or `./sc eject` to own the
> engine outright (see *Customize a fork vs diverge from it*, next).

### Roll back a bad update

```bash
./sc rollback                   # restore the DB + engine together, then reboot
```

`./sc rollback` is a **sound pair-restore**: because engine code is read live and
a migration exists *because new code expects the new schema*, it restores both —
it backs up the current DB first (rollback is itself reversible), restores the DB
from the most recent pre-update backup, and re-materializes the engine at
`.sc-state/engine.ref.prev`. Whole-restore, not a per-step schema reversal; the
only data lost is anything written between the update and the rollback.

> [!class4]
> **The contract:** every schema change *after* a fork exists ships as a `migrations/NNNN_*.sql` file, never an edit to `schema.sql` — the migration ledger is what carries a delta across to an existing fork. Additive where you can make it.

## Customize a fork vs diverge from it

> [!class2]
> **UI** — a policy, not a tab · **Shells** admin (owns the engine boundary)

The engine/fork boundary draws a clean decision rule for the question every
fork operator eventually asks: *"the engine doesn't do what I need — now what?"*

**Customize (the default — track upstream forever).** As long as what you need
fits the **fork-owned extension points**, you never touch engine files, and
`./sc update` keeps delivering fixes, migrations, and new skills indefinitely:

| Extension point | What it carries |
|---|---|
| **Local skills** | Fork-authored procedures (GUI → Skills) — serialized in `content.sql`, survive every update |
| **Flavor overlays** | `.sc-state/flavors/<flavor>.json` — what a NEW shell of a flavor gets: `skills_add` / `skills_remove` against the engine template's list, plus role/mandate/focus overrides (e.g. swap the engine's `test_authoring` for a fork's own testing skill) |
| **Skill retire list** | `.sc-state/skills_retired.json` (written by `./sc skill retire <name>`) — engine skills this fork has taken out of service, e.g. ones superseded by a fork-local skill. Retired skills leave every surface (boot doc, renders, grants) on ALL shells and stay retired across updates; `unretire` restores them, grants intact |
| **`instance.json`** | Per-fork config: ports, harness default, the `pg` / `vm` / `ts` opt-in blocks |
| **`.sc-state/`** | Your memory (content.sql), map tuning, engine pin — the fork's one tracked artifact |
| **Per-shell identity** | `current_state`, connections, decisions, seed — all DB rows, all yours |
| **Your project** | Everything outside `.super-coder/` — the engine never touches it |

**Upstream (when the extension points don't reach).** Need an actual engine
change? **PR it to super-coder first.** If one fork needs it, the next fork
probably does too — that's how the engine grows (dos-arch is exactly this
proving-ground loop). Your fork then picks the change up through a normal
`./sc update`, still on the lifeline.

**Diverge (`./sc eject` — the one-way door).** Only when the change is
genuinely yours and upstream would rightly not take it. Eject flips the model:
`.super-coder/` becomes **fork source** — un-gitignored, committed, edited like
any other code — and the upstream lifeline is cut for good:

```linear
Extension points fit :::class3 -> Upstream the change :::class1 -> Eject :::class4
```

```bash
./sc eject          # interactive warning + typed confirmation, then stages the flip
```

What it does: drops the `/.super-coder/` gitignore rule (engine runtime files —
DB, `instance.json`, `run/`, `logs/` — stay ignored), deletes the engine pin
(`engine.ref`), writes a `.sc-state/ejected` marker recording the SHA you
diverged at, removes the `super-coder` remote (`--keep-remote` to keep it for
reference), and stages everything. **Committing stays yours** — review the diff
first. After eject, `./sc update` and `./sc rollback` refuse (the marker);
launch, enter, snapshot, render, and the GUI work unchanged.

> [!class4]
> **What you give up, permanently:** upstream fixes, schema migrations, and new
> catalogue skills stop flowing — every engine change from here on is yours to
> author and maintain. Re-adopting upstream later is a manual re-fork, not a
> command. Exhaust the first two lanes before taking the third.
