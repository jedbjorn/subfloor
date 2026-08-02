---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Skill catalogue convergence and governance
roadmap_status: shipped
frozen: true
title: Skill Catalogue Convergence
tags: [skills, migrations, render, governance]
date: 2026-08-01
project: super-coder
purpose: Exact downstream skill state
---

# Skill Catalogue Convergence

## Overview

Make the upstream skill catalogue convergent across installed forks. Removing or
unassigning a skill must remove its authority from the live database and every
engine-managed flat projection, including dormant shell worktrees. A rebuild from
an older downstream snapshot must not resurrect a removed upstream skill as if it
were fork-local.

This feature also narrows catalogue authorship. Shells may curate their own L&S,
but they may not promote it directly into a skill. A reusable process becomes a
deduplicated upstream recommendation issue; deliberate fork-specific skill
authoring remains an administrator-owned asset-to-seed-to-snapshot workflow.

> [!class1]
> Done means a dirty downstream fixture containing retired rows, grants, snapshots, and flat files converges to the current catalogue after update or rebuild, while genuine fork-owned skills and unrelated native files survive unchanged.

## Settled Decisions

- Decision 14: generated snapshots and renders are local-only. Git carries
  authored engine source and explicit migrations, never repaired downstream
  snapshots or derived Markdown.
- Decision 16: retired Sprint v1 state has no compatibility guarantee and may be
  deleted instead of translated.
- Decision 55: remove the DB-only `sc skill add` surface. Curation recommends a
  skill upstream and retains the L&S until a replacement ships.
- Decision 56: `engine_surgery` is obsolete and is hard-deleted from source and
  downstream installations.
- Decision 57: `test_authoring_pg` and `test_authoring_sqlite` are hard-deleted;
  generic `test_authoring` is the sole engine testing doctrine. Forks own
  stack-specific testing procedures.

## Scope

The implementation changes these source areas:

| Area | Authoritative paths |
|---|---|
| Skill ownership and sync | `.super-coder/scripts/seed_skills.py`, `.super-coder/migrations/0001_seed_skills.sql` |
| Snapshot and rebuild | `.super-coder/scripts/snapshot.py`, `.super-coder/scripts/rebuild.py` |
| Update and assignment mutations | `.super-coder/scripts/update.py`, `skill.py`, `feature.py`, `.super-coder/api/server.py` |
| Flat projections | `.super-coder/render/flat.py`, `.super-coder/scripts/run.py`, adapter `skill_dirs` |
| Skill sources | `.super-coder/assets/skills/` |
| Boot and curation policy | `.super-coder/templates/boot.md`, `curate`, `issue_reporting`, source-mode compose guidance |
| Public contract | `docs/README.md` and focused regression tests |

Historical migrations remain append-only. A new trailing migration removes their
current live effects and reseeds surviving skill text that changed in this feature.

## Ownership Model

Introduce one tracked, validated upstream tombstone registry under
`.super-coder/assets/`. The active upstream namespace is:

```text
current names in 0001_seed_skills.sql + upstream tombstone names
```

The current tombstone set is exactly:

```text
dev_sprint
plan_sprint
rev_sprint
sprint
sprint_cond
sprint_onboarding
sprint_orchestration
sprint_orchestration_close
sprint_orchestration_recover
sprint_review
engine_surgery
test_authoring_pg
test_authoring_sqlite
```

`sprint_dev`, `sprint_pln`, and `sprint_rev` are not tombstones because Sprints
v2 owns those active names. `dev_kit` is not a tombstone because migration 0035
deliberately made it a fork-owned starter.

Registry invariants:

- A name cannot be both active and tombstoned.
- Duplicate, blank, non-string, or malformed entries fail loudly before writes.
- A fork-local asset or DB creation path may not claim a tombstoned name.
- Tombstones are permanent upstream namespace reservations unless a later
  reviewed migration explicitly reactivates a name and removes its tombstone.
- Fork-local skills absent from both sets remain untouched.

## Database Convergence

Provide one idempotent reconciliation function used by every lifecycle path. For
each tombstone it deletes `shell_skills` and `flavor_skills` children before
hard-deleting the `skills` row. It returns the changed names and grant count for
operator-visible reporting.

The new trailing migration performs the same cleanup for ordinary in-place
updates. Runtime reconciliation is still required because rebuild order is:

```linear
Schema and migrations :::class1 -> Downstream snapshot :::class2 -> Reconcile tombstones :::class3 -> Validate and publish DB :::class3
```

An old `content.sql` can therefore reinsert a removed name after the migration
ledger has stamped the cleanup. `rebuild.py` must reconcile after snapshot load
and before retire-list application, key backfill, foreign-key validation, and
candidate publication. A failure aborts the candidate and preserves the outgoing
DB and backup.

`update.py` reconciles after catalogue sync and before regrant, projection, and
snapshot. Boot/render self-heal also reconciles so a missed legacy path cannot
remain live indefinitely.

Snapshot generation must never serialize tombstoned skill rows or their shell or
flavor grants as fork-local content. Active seed membership and upstream
tombstones are separate inputs: current engine skills come from migrations;
genuine local rows are reinserted from the snapshot; tombstoned rows are emitted
nowhere. Snapshot generation additionally reconciles the live DB before
serializing — the same convergent tombstone reconciliation every lifecycle path
performs (admin-gated, reported on stdout when it fires, a no-op once
converged). [Ratified post-conformance: sprint 70, Finding 1 of doc #71.]

## Projection Convergence

The DB is authoritative, but assignment and catalogue mutations must converge
the filesystem too. Add one shared projection reconciler used by boot, update,
CLI mutations, feature enable/disable, and GUI grant endpoints.

Engine-managed harness roots are the default `.claude/skills` plus every relative
path declared by adapter `skill_dirs`, currently `.agents/skills` and
`.opencode/skills`. Installer ignore/teardown inventories must agree with this
set. Absolute paths, parent traversal, symlink roots, and resolved paths outside
the selected checkout are refused; cleanup never follows a symlink.

For one shell, an exact render writes every currently resolved grant and removes
every engine-managed skill directory not in that set. Deletions appear in the
render summary. A Bespoke grant change reconciles that shell. A flavor grant
change reconciles every live shell of that flavor.

Update additionally sweeps the repository root and each existing direct
`.sc-worktrees/<shortname>` checkout for every existing managed harness root. It
does not create missing worktrees or unused native roots. Dormant worktrees are
therefore cleaned without launching their shells.

Legacy root-level `skills_sc` directories in the repository and shell worktrees
are no longer active render targets under the local-only artifact policy. Delete
their super-coder-banner-owned files and remove the directory when empty. Never
delete an unmarked foreign file from a mixed directory. The active catalogue
render under the artifact-policy root remains exact and prunes Markdown for every
non-live skill.

Already-running harness sessions may retain text loaded into their context until
reboot; the update report states this explicitly. The DB and disk converge
immediately, and the next boot is authoritative.

## Catalogue Cleanup

Delete the authoritative asset directories for `engine_surgery`,
`test_authoring_pg`, and `test_authoring_sqlite`, then regenerate the active seed.
The ten retired Sprint v1 assets are already absent and remain tombstoned.

Remove current references and grants:

- Delete the source-mode compose pointer to `engine_surgery`; retain only the
  still-valid inline source-repository guidance.
- Rewrite generic `test_authoring` to provide stack-neutral test-quality rules
  and defer fixtures/database setup to the downstream repository without naming
  deleted companion skills.
- Remove `test_authoring_pg` from the `pg` feature. The Postgres sidecar and
  `query_authoring_pg` remain.
- Rewrite `query_authoring_pg` and `docs/README.md` so the PG feature promises
  infrastructure and diagnostic SQL, not upstream test fixtures.
- Replace deleted skill names used merely as test fixtures with a surviving
  neutral opt-in skill. Add direct negative assertions for every tombstone.
- Update Sprint-removal manifests and other retained-source inventories so they
  no longer require deleted assets while preserving historical migrations.

Existing downstream skills with different names, including specialized testing
skills, survive unchanged even if their prose mentions a retired upstream skill.
The engine does not rewrite fork-authored bodies.

## Curation Governance

Remove `sc skill add`, its parser/help surface, author-resolution code, DB-only
write path, and promotion tests. Do not leave a hidden or deprecated alias.
`grant`, `revoke`, `rm`, `retire`, and `unretire` retain their existing scope in
this feature.

Rewrite the `curate` skill and boot curation summary:

1. Identify a recurring process cluster.
2. Search all upstream issues for an existing recommendation.
3. Add evidence to the existing issue or open one titled
   `skills: recommend <topic>`.
4. Include the trigger, repeated incidents, proposed ownership boundary,
   expected users, why existing skills do not cover it, and a compact candidate
   procedure.
5. Keep one compressed L&S entry until a reviewed upstream skill ships and is
   granted. Filing an issue is not grounds to retire the knowledge.

`issue_reporting` explicitly permits this curation recommendation without the
normal “enhancement ideas go to FnB first” gate because the FnB has authorized
this one route. If issue search/create is unavailable, the shell surfaces the
failure to the FnB, keeps the L&S, and creates no local skill or asset.

Deliberate fork-specific skill authoring remains in `local_skill_management` and
is administrator-owned: authored asset, explicit seed, grant, snapshot, render.

## Mutation Semantics

All assignment entry points share the projection reconciler:

- `sc skill grant|revoke|rm|retire|unretire`
- `sc feature enable|disable`
- GUI flavor and Bespoke skill toggles
- update, rebuild, boot, and explicit render

The DB mutation commits first because it is authoritative. Projection runs
afterward. If projection fails, the command or API response reports a partial
failure naming the committed DB change and the exact cleanup remedy; it never
rolls back a committed grant by pretending the filesystem write was atomic.
The next boot/update retries reconciliation. No path returns silent success with
stale disk state.

Concurrent assignment writes retain existing DB transaction behavior. Projection
writes are deterministic and atomic per file; deletion is idempotent. Two
reconcilers may repeat work but must converge to the same resolved-grant set.

## Construction Plan

1. Add and validate the tombstone registry and shared DB reconciliation API.
   Prove active/tombstone disjointness and exact preservation of local skills.
2. Add the trailing cleanup/reseed migration; integrate post-snapshot rebuild,
   update sync, boot heal, and snapshot exclusion.
3. Remove the three skill assets, regenerate the seed, and repair generic test,
   PG feature, source-mode prompt, public docs, grants, and retained inventories.
4. Remove `sc skill add`; rewrite `curate`, boot curation guidance, and the
   authorized issue-recommendation exception.
5. Build the bounded projection reconciler and integrate CLI, feature, API,
   boot, render, update, and legacy `skills_sc` cleanup paths.
6. Run the dirty-fork compatibility matrix and all focused/full project gates.

Steps 3 and 4 are parallelizable after the ownership contract in step 1. The
projection implementation may proceed alongside them, but update/rebuild
integration waits for the DB reconciliation API.

## Acceptance

- Fresh build contains none of the 13 tombstones and contains the surviving
  generic `test_authoring` with no deleted companion references.
- In-place migration of a dirty legacy DB removes all tombstone rows and grants,
  twice without error.
- Rebuild from a stale downstream snapshot that reinserts tombstone rows and
  grants still publishes a clean, foreign-key-valid candidate.
- A subsequent snapshot contains no tombstone row or grant statement, while
  preserving fork-local skills, bodies, and grants byte-for-byte.
- A same-named fork asset or creation attempt for a tombstone fails loudly.
- Revoking a Bespoke grant removes its managed files from all existing harness
  roots for that shell. Revoking a flavor grant does the same for every existing
  worktree of that flavor.
- Update removes retired v1 projections from a dos-arch-shaped fixture under
  `.claude/skills`, `.agents/skills`, `.opencode/skills`, active catalogue
  renders, and banner-owned legacy `skills_sc` files.
- Projection cleanup never follows symlinks, escapes a checkout, creates a
  dormant worktree, or deletes an unmarked foreign file.
- `sc skill add` is absent from help and execution. Curation produces only an
  issue recommendation and does not retire the source L&S before delivery.
- PG feature enablement grants `query_authoring_pg` only; generic testing
  doctrine continues through ordinary flavor packs.
- Repeated migrate, update reconciliation, render, and rebuild are no-ops after
  convergence.

## Verification Gate

Focused tests must cover tombstone validation, dirty migration, snapshot
resurrection, grant serialization, CLI removal, curation wording, feature
grants, GUI/CLI projection triggers, native harness mirrors, legacy flat cleanup,
symlink confinement, and local-skill preservation.

Then run:

```bash
./sc seed-skills
./sc render-check
./sc verify
git diff --check
```

The adversarial gate starts from a fixture containing all 13 retired rows, both
grant types, a stale local snapshot, dormant worktrees, native harness mirrors,
legacy `skills_sc`, a legitimate fork-local testing skill, and an unmarked
foreign file. Update and rebuild both pass only if the retired authority is gone
from DB and disk and both local controls survive.

## Out of Scope

- Rewriting or deleting downstream-authored skill bodies with non-tombstoned
  names.
- Replacing the deleted PG/SQLite skills with new upstream stack guidance.
- Revoking text already loaded into an active harness context without reboot.
- Changing authorization for the remaining skill grant/retire commands.
- Editing or deleting historical applied migrations.

