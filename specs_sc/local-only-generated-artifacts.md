---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Local-only generated artifacts
roadmap_status: shipped
frozen: true
title: Local-only generated artifacts
tags: [artifacts, git, update, worktrees]
date: 2026-07-29
project: super-coder
purpose: Keep generated state out of Git
---

# Local-only generated artifacts

## Objective

Generated instance state must never dirty a repository branch or require a
shell to write into another worktree. Snapshot and render commands remain
available for local durability and inspection, but Git contains only authored
project source, the engine pin, and upstream engine migrations.

Done means a fresh install, an upgraded tracked-mode fork, a document write,
an update, and a rebuild all use ignored `.sc-state/local/` artifacts, and no
GUI or API action can create a content branch, commit, push, or pull request.

## Storage contract

- `.sc-state/local/content.sql` is the rebuild snapshot for this local
  instance.
- `.sc-state/local/map/content.sql`, map configuration, and the map DB remain
  local.
- `.sc-state/local/renders/` contains `docs_sc/`, `specs_sc/`, `skills_sc/`,
  and `roadmap_sc.md` as disposable views.
- `.sc-state/local/skills_retired.json` preserves the fork-local retire list.
- `.sc-state/engine.ref` remains tracked because it is authored dependency
  metadata shared by the repository.
- The live DB and WAL-safe backups remain the primary local runtime state and
  rollback path.

Cloning the repository creates a new instance. It does not clone another
machine's shell identities, memory, roadmap, inbox, or runtime orchestration
state.

## Compatibility

On update or first local artifact access, copy each legacy tracked artifact to
its local replacement only when that replacement does not already exist.
After the copy is safe, untrack the legacy snapshot, map-authorship files,
retire list, and flat render paths. Keep the files ignored so an interrupted
upgrade cannot lose the old reconstruction source.

Legacy `artifact_mode: tracked` configuration is accepted only as upgrade
input and resolves to local behavior. Environment or CLI attempts to enable
tracked mode are rejected with the local-only contract.

## Surfaces

- `sc artifact-mode` becomes a read-only local path inspector; mode switching
  is retired.
- `sc snapshot`, `sc render`, automatic document serialization, update,
  rebuild, rollback, and render-check use the local paths.
- The GUI exposes one “save locally” action. Publish is removed.
- `/api/publish` is retired and cannot perform Git operations.
- Update no longer recommends staging snapshots or renders; it names only the
  engine pin and any genuinely authored install files.
- Skills and documentation stop teaching snapshot/render commits.

Deliberately authored human documentation is ordinary project source. An
operator may export or rewrite a local render into `docs/`, review it, and
commit it explicitly; generated render paths themselves never become source.

## Verification

1. Artifact-policy tests prove every instance resolves to local paths and
   tracked mode cannot be enabled.
2. Upgrade tests prove legacy tracked files are copied before Git untracking
   and existing local files are never overwritten.
3. API/UI tests prove Publish is absent or retired and local save performs no
   Git mutation.
4. Install/update tests prove the full generated-path ignore block is applied
   and update guidance stages no generated artifact.
5. Rebuild/render-check tests prove local snapshot durability and local render
   consistency.
6. A downstream dos-app update leaves product main clean except for the
   deliberate engine-pin/untracking migration changes.

## Boundaries

This change does not make the SQLite DB portable through Git, commit generated
documentation, remove local backups, or change engine migration propagation.
It removes the dual tracked/local artifact policy and the Git publication
workflow only.
