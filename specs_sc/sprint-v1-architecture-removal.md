---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprint v1 architecture removal
roadmap_status: shipped
frozen: true
title: Sprint v1 Architecture Removal
tags: [sprint, removal, architecture]
date: 2026-07-31
project: super-coder
purpose: Delete Sprint v1 before redesign
---

# Sprint v1 Architecture Removal

## Overview

Sprint v1 is a broken, disposable subsystem. This feature removes it completely
before any replacement is designed. Existing Sprint data, active work,
compatibility behavior, migration fidelity, and installation-specific Sprint
state have no preservation requirement.

Done means a fresh engine and a cleaned existing engine contain no executable
Sprint v1 architecture: no Sprint lifecycle or board, Conductor, directives,
sentinel, Sprint conversations, Sprint PR watcher, browser Sprint surface,
Sprint CLI, Sprint role skill, retired Interface/TMUX wake state, or hidden
runtime hook. Independently useful normal-chat and engine infrastructure still
works and contains no Sprint-specific branch.

Source baseline: `origin/main` at `36fd867`. Decision #33 governs this spec and
supersedes decision #29's Planner–Conductor ownership model.

> [!class4]
> This is a removal specification, not the first stage of Sprint v2. A component
> may survive only because a current non-Sprint feature owns it—not because a
> future Sprint design might reuse it.

## Ruling

The removal follows five non-negotiable rules:

1. No existing Sprint runtime data is migrated, exported, translated, or
   preserved.
2. No Sprint v1 endpoint, command, schema, state machine, shell role, skill, or
   compatibility stub survives the cutover.
3. Generic infrastructure is retained only after its non-Sprint owner and
   regression gate are named in this spec.
4. Historical migration files and generated seed inputs are cleaned so a fresh
   database never constructs Sprint v1 merely to drop it later.
5. One destructive cleanup migration removes Sprint v1 from already-created
   databases while preserving unrelated identity, memory, roadmap, normal-chat,
   model-route, and job data.

The Git history is the archive. The current source tree does not retain dead
runtime code as an archaeological record.

## Current Surface

Sprint v1 currently spans four generations that coexist in the source.

| generation | principal state | current residue |
|---|---|---|
| Markdown board | `documents.body` | Sprint document title conventions, reports, close/freeze hooks |
| DB board and eventing | `sprint_units`, `shell_messages`, `watched_prs` | board API, PR poller, inbox wake, reconciliation readers |
| Interface/TMUX wake | `interface_*`, `planner_wake_*`, bindings and alerts | schema, migration, snapshot, rebuild, test, and compatibility residue |
| Conductor/browser | `sprints`, directives, Sprint conversations and assignments | Conductor shell, sentinel, broker hooks, Sprint UI/API/CLI and role skills |

The removal manifest covers these source regions:

- `.super-coder/schema.sql` and Sprint/Interface migration history;
- `.super-coder/api/server.py`, `sprint_routes.py`, and
  `conductor_routes.py`;
- `scripts/sprint*.py`, `conductor*.py`, `sentinel.py`, `pr_poller.py`,
  `watch.py`, and `activity_readers.py`;
- Sprint branches in conversation launch/broker, run, memory messaging,
  analytics, snapshot, rebuild, update, install, shell factory, and rendering;
- the browser Sprints tab, board, assignment view, Conductor transcript,
  cancellation controls, styling, API proxy routes, and analytics grouping;
- Conductor shell configuration/template and Sprint role/onboarding skills;
- current docs, screenshots, review artifacts, seeded skill bodies, tests, and
  CLI help.

Every implementation unit updates this manifest when it discovers another
reference. An unlisted reference is not implicitly allowed to survive.

## End State

The engine after removal has no Sprint concept. It continues to provide:

- roadmap features, documents, specs, `spec_tasks`, flags, and projects;
- normal browser conversations with durable messages, runs, replay events,
  outbox delivery, interruption, close, stars, Git targets, and Diff review;
- generic headless shell launching and harness adapters;
- generic shell inbox messages and job completion results;
- model discovery, route resolution, flavor defaults, and quota probes;
- conversation-broker supervision and its generic daemon heartbeat;
- shell identity, archives, decisions, skills, mapping, snapshots, rebuild,
  update, and rendering.

No retained component exposes a field, enum value, environment variable,
branch, projection, or help string reserved for Sprint v1.

```linear
Withdraw entry points :::class1 -> Decouple shared systems :::class2 -> Delete runtime :::class2 -> Excise schema :::class3 -> Prove absence :::class3
```

## Keep And Remove Boundary

| surface | disposition | required final shape |
|---|---|---|
| Conversation broker and adapters | Keep | Normal browser conversations only; no Sprint terminalization, assignment result, cancellation, or worker-failure branch |
| `conversations` and child tables | Keep/rebuild | Remove Sprint mode and `sprint_doc_id`; preserve normal conversations and their children |
| Conversation Git targets and Diff review | Keep | PR review remains an on-demand normal-chat feature, independent of Sprint polling |
| `shell_messages` | Keep/rebuild | Preserve generic `shell`, `task`, and `result` traffic; remove Sprint scope/correlation and `pr_event` |
| Jobs | Keep | Job result delivery continues through generic messages |
| Model route catalogue | Keep/reframe | Preserve generic discovery and launch resolution; remove Sprint naming, effort assumptions, skills, and docs |
| `daemon_heartbeats` | Keep | Preserve generic broker heartbeat support; remove watch/reconcile projections and rows |
| Generic headless `sc run` | Keep | Remove slots, Sprint context lookup, Sprint environment injection, and Sprint archive annotation |
| Roadmap/docs/spec tasks | Keep | Remove Sprint runtime title semantics and freeze/close behavior |
| Sprint PR watcher | Remove | Delete registry, poller, observations, service thread, API, CLI, heartbeat UI, and watcher docs |
| Interface/TMUX wake generation | Remove | Delete all tables, triggers, migration inputs, compatibility code, docs, screenshots, and tests |
| Conductor | Remove | Delete shell flavor, singleton, config, policy, template, install/update reconciliation, runtime, API, boot render, and skill |
| Directives and sentinel | Remove | Delete command/event/expectation tables, APIs, runtime, scheduler hooks, and tests |
| Sprint QAQC | Remove | Delete Sprint-specific review table and declaration gate; future review semantics are undecided |
| Sprint analytics grouping | Remove | Remove `sprint_ref`, Sprint title lookup, grouping projection, and browser presentation |

`shell_launch_records`, generic liveness utilities, quota probes, and model
route records are not removed merely because Sprint code once read them.

## Data And Schema Reset

### Tables removed

The fresh schema and post-cutover database must not contain:

- `interface_generations`;
- `interface_sessions`;
- `interface_writer_leases`;
- `interface_input_state`;
- `interface_idempotency_keys`;
- `sprint_planner_bindings`;
- `planner_wake_batches`;
- `planner_wake_items`;
- `planner_action_receipts`;
- `planner_alerts`;
- `wake_machine_retirements`;
- `watched_prs`;
- `pr_poll_runs`;
- `pr_poll_observations`;
- `sprint_units`;
- `spec_qaqc_reviews`;
- `sprints`;
- `directive_kinds`;
- `directives`;
- `sentinel_events`;
- `unit_expectations`;
- `sprint_conversation_bindings`;
- `sprint_assignment_results`;
- `sprint_cancellations`.

All associated indexes and triggers disappear with their tables. No archive or
replacement table is created.

### Shared tables rebuilt

The destructive cleanup migration rebuilds shared tables where SQLite cannot
drop the retired shape directly:

- `conversations`: delete Sprint-mode rows and their message/run/event/outbox
  children, then remove `mode` and `sprint_doc_id`; retain normal rows and make
  their user ownership invariant direct;
- `shell_messages`: delete Sprint-scoped and `pr_event` rows, remove
  `sprint_doc_id`, retain generic message identity/dedupe/read state, and narrow
  the kind vocabulary to current generic owners;
- `shell_memory_archives`: remove `sprint_ref` while preserving all other
  archives and analytics fields.

The migration soft-deletes any live `conductor` shell and removes its flavor
defaults and grants. It deletes Sprint runtime documents referenced by
`sprints.sprint_doc_id`. Unlinked historical specs, decisions, reports, and
roadmap rows may remain as planning history, but they have no runtime
interpretation.

### Migration-source policy

Fresh rebuilds must never create Sprint v1. Implementation therefore:

1. removes Sprint and retired Interface/TMUX definitions from `schema.sql`;
2. deletes migrations whose only effect is to create, mutate, seed, or reseed
   removed Sprint architecture;
3. edits mixed migrations to retain only independently owned generic behavior;
4. removes Sprint skill bodies from the current seed migration and assets;
5. edits the conversation-foundation migration to create only the retained
   normal-chat shape;
6. adds one next-numbered `remove_sprint_v1` migration using `IF EXISTS` and
   table rebuilds so an already-created database is cleaned destructively.

Deleted migration filenames may remain stamped in an existing
`schema_migrations` ledger. The runner already keys only on present unstamped
files; no compatibility translation is added.

## Runtime Removal

The engine service stops importing or starting the PR poller, sentinel, or
Conductor machinery. No background thread, timer, heartbeat, inbox wake, or
broker callback remains for Sprint.

Delete the standalone Sprint runtime modules and all calls into them. Remove:

- board/lifecycle/unit helpers;
- declaration, arming, adoption, close, and cancellation;
- directive validation and execution;
- dependency release and role assignment;
- Sprint conversation creation and binding;
- assignment-result correlation and required-result enforcement;
- PR-to-unit transitions and dwell-time reconciliation;
- Conductor configuration, doctor, boot rendering, and action loop;
- Sprint slot parsing and worker context environment variables.

The normal conversation broker must terminalize normal runs without querying a
Sprint table. Failure handling remains conversation-local and does not emit a
directive, sentinel event, Planner assignment, or Sprint result.

## Public Surface Removal

### API

Remove Sprint route registration and every endpoint for Sprint units, QAQC,
declarations, adoption, arming, cancellation, directives, sentinel events,
and watched PRs. Requests reach the ordinary unknown-route response; there are
no `410 Gone` compatibility handlers.

The generic message API no longer accepts Sprint scope, Sprint assignment IDs,
or Sprint result-kind correlation. The conversation API no longer accepts,
projects, or filters by a Sprint mode.

### CLI

Remove `sc sprint`, `sc directives`, `sc events`, and the entire `sc watch`
surface, including retired watch-daemon compatibility verbs. Remove Sprint
slot flags and examples from `sc run`, `sc enter`, help, completion, and error
text.

Generic inbox reads remain available through `sc mem message`; no replacement
blocking watcher is introduced by this feature.

### Browser

Remove the Sprints navigation item, active-count badge, board flow, unit cards,
assignments, Conductor transcript/composer, event streams, cancel workflow, and
Sprint-specific styling. Remove Sprint clustering from Analytics.

Chats and Diff must render and behave exactly as before for normal
conversations. No empty placeholder Sprint tab remains.

## Shells Skills And Guidance

Fresh install and update no longer create or reconcile `CON1`, emit a
`conductor` configuration block, recognize a Conductor flavor, or grant a
Conductor skill pack.

Delete these authored skills and every seeded/reseeded mirror or grant:

- `sprint_pln`;
- `sprint_dev`;
- `sprint_rev`;
- `sprint_cond`;
- `sprint_onboarding`;
- all retired predecessor Sprint skill names still carried by migrations.

Remove Sprint grants from planner, dev, and reviewer shell templates. Remove
Sprint slot/assignment context from boot documents. Current README, full docs,
quick-start guidance, screenshots, and review records must not teach the
removed workflow.

Historical DB decisions and frozen planning specs need not be rewritten. Active
planning is cleaned at delivery: retire the unshipped Sprint-reporting feature,
retain the generic model-route implementation under non-Sprint wording, and
remove Sprint-follow-on language from the browser-conversations feature.

## Failure And Cutover Behavior

- An active or declared Sprint does not block removal, update, rebuild, or
  restart. Its data is discarded.
- A queued/running Sprint conversation is cancelled or deleted during cleanup;
  it is never resumed after cutover.
- A normal conversation sharing the broker remains intact and resumable.
- A missing Sprint table during cleanup is a successful no-op, allowing the
  same migration to run on a fresh baseline.
- A retained-table rebuild failure rolls back the whole migration; partial
  cleanup is not stamped.
- Engine startup after code materialization must not race an old Sprint poller
  against table removal. Update/restart ordering stops the old service before
  migration and starts only the new service afterward.
- Old CLI commands and endpoints fail as unknown surfaces, not with tracebacks,
  missing-table errors, or stale instructions.
- A deleted Conductor shell never blocks normal shell creation or leaves a
  singleton/grant trigger behind.

## Sequenced Construction Plan

| seq | unit | dependency | change | verification |
|---|---|---|---|---|
| 0 | Lock manifest and cutover fixtures | — | Convert this inventory into a checked source/schema denylist; prepare a pre-removal DB fixture containing normal chat plus every Sprint generation | Fixture rebuilds; denylist identifies all current references before deletion |
| 1 | Withdraw entry points and schedulers | U0 | Remove Sprint API/CLI/browser entry points and stop service startup of poller/sentinel/Conductor while leaving old tables inert | Engine starts; removed routes/commands are absent; Chats, Diff, messages, jobs, and model routes pass |
| 2 | Decouple shared systems | U1 | Remove Sprint branches from conversations, broker, launch/run, messaging, analytics, snapshot, rebuild, update, render, and document freeze | Normal-chat lifecycle, broker recovery, generic headless launch, messaging/job, analytics, snapshot/rebuild/update tests pass without Sprint tables mocked |
| 3 | Delete standalone runtime and shell role | U2 | Delete Sprint/Conductor/sentinel/watcher/activity modules, config, shell reconciliation, templates, skills, and direct unit tests | Import graph and fresh roster contain no removed modules, CON1, flavor, grants, or supervisor |
| 4 | Excise schema and migrations | U3 | Clean baseline and historical migration inputs; add destructive cleanup migration; rebuild retained tables and drop every Sprint/Interface table | Fresh rebuild and pre-removal fixture migration converge to the same retained schema; normal data survives; Sprint data is absent |
| 5 | Remove guidance and planning residue | U4 | Remove current docs, screenshots, help, UI styles, seeded mirrors, and obsolete Sprint test suites; retire/reframe active roadmap items | Render/seed checks pass; current user-facing guidance contains no executable Sprint v1 instruction |
| 6 | Adversarial absence gate | U5 | Add final source/schema/runtime negative tests and run the complete verification matrix | All acceptance checks below pass on fresh build and cleaned fixture |

U1 is the first green checkpoint: the broken feature becomes unreachable before
its internals are removed. U2 then makes generic infrastructure independent.
Only after no runtime consumer remains does U4 delete the database structures.

## Verification Gate

The feature fails its gate unless all of the following pass:

1. Fresh rebuild creates none of the removed tables, columns, triggers,
   indexes, skills, grants, shell flavors, or config.
2. Applying the cleanup migration to the pre-removal fixture removes all four
   Sprint generations and preserves fixture identity, memory, roadmap, normal
   conversations, normal events/runs/outbox, generic messages/jobs, model
   routes, and Git review targets.
3. `sqlite_master` and table-info assertions prove the removed schema is
   absent.
4. Source scans reject removed module names, table names, route prefixes,
   `SC_SPRINT_*`, Sprint slot flags, Conductor configuration, Sprint skills,
   and current instructional prose. The destructive cleanup migration and
   explicit removal tests are the only allowlisted references.
5. CLI help has no Sprint/directive/event/watch commands or Sprint launch
   flags.
6. HTTP probes return the standard unknown-route response for every removed
   endpoint.
7. Browser navigation has no Sprints tab or active-count request, and Analytics
   has no Sprint grouping.
8. Normal browser conversation create/send/queue/resume/interrupt/close,
   transcript replay, stars, Git targets, and Diff review pass.
9. Conversation broker lease recovery and heartbeat pass without Sprint
   queries.
10. Generic shell messages, task/result kinds, job completion, model refresh,
    route resolution, and generic headless shell launches pass.
11. Install, update, restart, snapshot, rebuild, seed-skills, render-check,
    verification, and the full automated suite pass.
12. Process inspection after launch shows no PR poller, sentinel, Conductor,
    legacy watcher daemon, or Sprint-specific supervisor.

The reviewer must trace the denylist from source to runtime rather than accept
deleted top-level files as proof. Any surviving executable reference is a
blocking finding.

## Acceptance Criteria

- Sprint v1 cannot be created, viewed, armed, run, resumed, cancelled, closed,
  watched, or reported through any engine surface.
- The source tree contains no Sprint v1 implementation or current operating
  guidance.
- The current database schema contains no Sprint v1 or retired Interface/TMUX
  state.
- Fresh install creates no Conductor shell and grants no Sprint skill.
- Normal chat, Diff review, generic headless shells, messaging/jobs, model
  routes, memory, roadmap, and rebuild/update remain operational.
- No Sprint v2 entity, name, schema, workflow, or compatibility abstraction is
  introduced.
- Feature #29 is ready to ship only after an adversarial reviewer confirms both
  absence and retained-system integrity.

## Out Of Scope

- Designing Sprint v2.
- Preserving, exporting, or displaying historical Sprint runtime data.
- Mapping old units, reports, directives, assignments, or conversations into a
  successor model.
- Retaining Conductor as a generic orchestrator.
- Generalizing directives, sentinel events, watched PRs, or QAQC for possible
  future use.
- Changing normal conversation semantics, harness adapter behavior, model
  discovery, generic shell roles, roadmap planning, or job delivery except to
  remove their Sprint-specific branches.
