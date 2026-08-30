---
name: engine_database
description: Admin-only map of Subfloor's private instance database, schema, tables, backups, snapshots, rebuild path, SQL diagnosis, and repair boundaries.
category: substrate
common: false
---

# engine_database — inspect and repair the control plane

Admin only. The boot `ENGINE MAINTENANCE` block names the active engine floor
and private instance-state directory. Resolve the canonical database again
before any repair:

```bash
python3 .super-coder/scripts/instance_state.py active-database .super-coder
```

Require the printed absolute path to sit under the boot's private instance
state. The private directory owns the live `shell_db.db` plus WAL/SHM sidecars,
local control-plane snapshot, verified backups, relocation receipt, maintenance
lease, and DB-generation evidence. The repository catalogue remains a separate
map store; a product database remains the fork application's concern.

## Source and rebuild model

In the Subfloor source repository, `.super-coder/schema.sql` is the current
baseline and `.super-coder/migrations/*.sql` are ordered, ledger-tracked deltas.
Installed downstream floors materialize the same engine source. `sc rebuild`
creates a candidate from that source plus the private instance snapshot,
verifies it, and publishes only through the maintenance cutover. Load
`engine_migrations` before changing the baseline or migrations and `snapshot`
before serializing instance content.

## Data model

| Surface | Storage |
|---|---|
| Shell core | `shells` — role, flavor, mandate, system prompt, current state, active session/archive identity |
| Seed and L&S | `shell_identity_entries` — capped identity entries with retirement |
| Decisions | `shell_decisions` — append-only decisions and supersession links |
| Narrative | `shell_memory_archives` — per-session narrative |
| Planning | `roadmap`, `feature_blockers`, `projects`, `project_shells`, `spec_tasks` |
| Documents | `documents` — revisioned spec/doc bodies and freeze state |
| Flags | `flags` — open/resolved work linked to features |
| Skills | `skills`, `flavor_skills`, `shell_skills`, `resolved_shell_skills` |
| Coordination | message, wake, conversation, Sprint, PR-subscription, and liveness tables |

Normal reads and writes still use `sc mem` and bounded APIs. The table map is
for diagnosis, migration authoring, and recovery—not ordinary shell work.

## SQL and mutation boundary

`sc sql` is the Admin read-only diagnostic lane and remains available from the
host Admin seat when the API is down. `sc sql-rw` is an overt escape hatch and
must refuse outside a named procedure satisfying all of these gates:

- managed runtime stopped;
- exclusive maintenance lease held;
- WAL-safe backup verified before mutation;
- exact canonical target independently matched;
- candidate and ledger verified before publication;
- restart health and rollback evidence retained.

Prefer the typed maintenance command (`sc migrate`, `sc rebuild`, `sc update`,
`sc rollback`, or the named recovery procedure) over direct SQL. Keep external
calls outside transactions. A path mismatch, unresolved private state,
conflicting legacy/private copies, failed backup, or absent authority stops the
operation with the runtime down.

## Recovery routing

- API down, database healthy: use host Admin `sc health`, `sc logs`, and
  read-only `sc sql`, then restore the managed service with `sc restart` /
  `make dos-r`.
- Migration or rebuild work: load `engine_migrations` and require its backup,
  candidate, ledger, and restart receipts.
- Snapshot or render repair: load `snapshot`; do not hand-edit serialized or
  rendered state.
- Update/rollback failure: load `self_update`; preserve the engine/database
  generation pair.
- Ambiguous or damaged canonical state: keep the runtime stopped and present
  the exact database, backup, generation, and relocation evidence to the FnB.
