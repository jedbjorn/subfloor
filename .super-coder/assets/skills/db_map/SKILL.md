---
name: db_map
description: Supported Subfloor control-plane surfaces, ownership, scope, and `sc mem` commands. Check before reading or writing identity, decisions, roadmap, documents, tasks, or flags.
category: substrate
common: true
---

# db_map — use the supported control-plane surfaces

Subfloor control-plane state is already wired to the launched shell. Read and
write it through `sc mem`; the service resolves shell identity and enforces
ownership, caps, immutability, and durable transactions. `sc mem which`
confirms the active identity and API reachability.

The repository catalogue is separate: inspect its `dr_*` objects with
`sc map-schema` and query them with `sc map-sql`. Product runtime data is also
separate and follows the fork application's code, migrations, dev-kit commands,
and app database connection.

## Read surfaces

```text
sc mem get state
sc mem get seed
sc mem get lns
sc mem get decisions [<id>]
sc mem get flags [<id>] [--feature <id>] [--resolved]
sc mem get narrative
sc mem get messages
sc mem get roadmap
sc mem get projects
sc mem get documents [--feature <id> | --doc <id>]
sc mem get tasks [--feature <id> | --doc <id>]
sc mem get shells
```

Identity surfaces (`state`, `seed`, `lns`, narrative, and messages) resolve as
the calling shell. Decisions are fleet-visible and tagged by author. Planning,
document, task, project, and shell reads are shared coordination surfaces.
Use `--json` only when a supported command consumer needs structured output.

## Write surfaces

Each successful command confirms a durable API write:

```text
sc mem state "…"
sc mem seed "…"
sc mem lns "…" --new
sc mem lns "…" --supersedes <ids>
sc mem retire <entry_id>
sc mem decision "…" --rationale "…" [--parent <id>] [--feature <id> | --doc <id>]

sc mem roadmap add "…" --status <status> --summary "…" [--project <id|shortname>]
sc mem roadmap status <feature_id> <status>
sc mem roadmap project <feature_id> <id|shortname|none>
sc mem roadmap depends <feature_id> [--on <feature_id>]…

sc mem doc add "…" --kind <spec|doc> --feature <id> --body-file <path> --render-path <path>
sc mem doc freeze <document_id>
sc mem doc move <document_id> --feature <target_feature_id>
sc mem task add "…" --feature <id> --doc <id> --seq <n> [--desc "…"]
sc mem task start <task_id>
sc mem task done <task_id>
sc mem task cancel <task_id> --notes "…"

sc mem flag open "…" --name <name> [--priority <priority>] [--feature <id>]
sc mem flag edit <flag_id> [--description "…"] [--priority <priority>] [--feature <id>]
sc mem flag close <flag_id> --notes "…"

sc mem project add <shortname> "<title>" --purpose "…" --standing "…"
sc mem project standing <id|shortname> "…"
sc mem message send <shortname> "…"
sc mem oriented
```

Load the owning skill before a governed write: `memory` for state/identity,
`spec` for roadmap tasks, `docs` for documents, `flags` for blockers, and
`messaging` for shell coordination. Frozen documents require a new document
revision; decisions are superseded with `--parent`; seed entries are retired
rather than rewritten. `doc move` preserves one unfrozen spec's identity,
tasks, and document-linked decisions while reassigning them atomically to an
active feature; it refuses frozen, ordinary-doc, terminal-target, and
Sprint-bound moves.

## Failure behavior

An unavailable API stops control-plane work; ordinary shells do not fall back
to files or arbitrary queries. A missing supported read/write projection is an
engine gap: report the exact data and use needed, then stop at that boundary.
Admin storage, backup, rebuild, and repair internals live in the Admin-only
`engine_database` skill.
