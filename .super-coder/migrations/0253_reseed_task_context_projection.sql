-- 0253 — reseed cartographer, spec, sprint_dev, and surface_catalogue for
-- task context projection (doc #187, decision #306). Role guidance loads the
-- exact `sc context --task|--work-unit` projection first; the repo catalogue
-- is abbreviated documentation rather than a map-first/anti-grep mandate; and
-- Cartographer file descriptions follow a compact behavioral standard applied
-- incrementally. No schema change: full-body UPSERTs converge upgraded
-- installations on the same text a fresh seed produces.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'cartographer',
  'Own the repo map — configure mapping to THIS repo, wire auto-remap, install semantic extractors, curate authored navigation, and finish through one truthful finalization gate. Cartographer-only.',
  'substrate',
  'sc map-setup',
  0,
  '# cartographer — own the repo map

Working shells consume the `dr_*` catalogue and NEVER map. Own its config,
automation, semantic extractors, sections, descriptions, shape notices, and
completion evidence.

Map data = `.sc-state/local/map/map.db`, separate from engine memory. Use:

- `sc map-schema [dr_table]` for structure. Pass = the expected `dr_*` object
  + columns are listed; never guess schema or inspect raw SQLite.
- `sc map-sql "…"` for read-only data queries.
- `sc map-sql-rw "…"` only for the authored `dr_section` / `dr_filepath.desc`
  writes named below.
- `sc map` to refresh derived rows.
- `sc map finalize` to prove completion. Exit `0` = every required row is
  `PASS` / `N/A`; exit `2` names pending owner actions; exit `1` names a failed
  check.

## First boot / heal

Run this sequence on first boot, after a shape notice, or when the map drifts:

1. `sc map-schema` then `sc map-schema dr_repo`. Pass = map structure is
   inspectable through the supported surface.
2. Inspect live data:

   ```sql
   SELECT name, root, default_branch, file_count, mapped_at FROM dr_repo;
   SELECT lang, COUNT(*) n FROM dr_filepath
   WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;
   SELECT role, COUNT(*) n FROM dr_filepath GROUP BY role ORDER BY n DESC;
   ```

3. Tune `.sc-state/local/map/config.json` in the assigned worktree only where defaults are
   wrong. Config is per-clone runtime state and never a commit. All keys are
   optional; skip sets extend defaults and cannot re-include engine-owned
   paths:

   ```json
   {
     "skip_dirs": ["generated", "fixtures"],
     "skip_files": ["LICENSE"],
     "role_overrides": [
       {"prefix": "cmd/", "role": "code"},
       {"glob": "*.proto", "role": "code"},
       {"prefix": "docs/adr/", "role": "doc"}
     ]
   }
   ```

4. Run `sc map-setup`. Pass = `git config --get core.hooksPath` prints
   `.super-coder/hooks`, the declared hooks are executable, and `dr_repo`
   carries a current `mapped_at` + correct file count.
5. Curate sections + descriptions + semantic rows with the worklists below.
6. Resolve every notice-linked flag, then mark the notice read last.
7. Run `sc map finalize`. Complete Cartographer-owned actions; hand each
   Admin-owned snapshot/review action to Admin. Pass = a rerun exits `0`.
8. On first boot only, run `sc mem state "…"` then `sc mem oriented` after the
   finalizer is green.

Automation remains healthy when:

- `post-merge` / `post-checkout` / `post-rewrite` run `sc map` through
  `core.hooksPath`.
- Admin control-plane rebuilds remap through their supported lifecycle.
- pm2''s `sc-map-<repo>` one-shot cycles stopped -> online hourly while the
  stack is up. A repo without pm2 relies on hooks + manual `sc map`.

## Authored navigation

### Sections

`dr_section` is authored + snapshot-backed. Curate useful path prefixes; never
insert an empty prefix. Root files belong to the synthetic `Repository Root`
group and never enter `dr_section`.

```sql
-- Repository Root leaves; a non-empty result renders the synthetic group:
SELECT path, desc, lines FROM dr_filepath
WHERE instr(path, ''/'') = 0 ORDER BY path;

-- Authored sections + live counts:
SELECT s.name, s.path_prefix, s.description,
       (SELECT COUNT(*) FROM dr_filepath f
        WHERE f.path LIKE s.path_prefix || ''%'') n
FROM dr_section s ORDER BY s.sort_order, s.name;

-- WORKLIST: only nested unmatched files are real section gaps:
SELECT f.path FROM dr_filepath f
WHERE instr(f.path, ''/'') > 0
  AND NOT EXISTS (
    SELECT 1 FROM dr_section s
    WHERE f.path LIKE s.path_prefix || ''%''
  )
ORDER BY f.path;

-- STALE authored sections after a rename/removal:
SELECT s.name, s.path_prefix, s.description
FROM dr_section s
WHERE NOT EXISTS (
  SELECT 1 FROM dr_filepath f
  WHERE f.path LIKE s.path_prefix || ''%''
)
ORDER BY s.name;
```

Use `sc map-sql-rw` to `INSERT` / `UPDATE` / `DELETE` the exact rows identified
by these queries. Pass = nested unmatched + stale-section worklists return no
rows; root files remain queryable through `instr(path, ''/'') = 0`.

### Descriptions

`dr_filepath.desc` is abbreviated behavioral documentation: one line, soft
200-character bound, that tells a shell why it would open the file — the
responsibility the file owns, the mechanism it uses, its principal input, and
its observable output, state change, or exposed surface. Omit a component that
genuinely does not apply; never invent behavior to fill the template, and never
merely repeat the filename, role, language, directory, or a symbol list.

| File role | Emphasis |
|---|---|
| Code | owned behavior, mechanism, principal input, output or side effect |
| Test | the contract, boundary, or failure mode it proves |
| Configuration | controlled behavior, consumed keys, runtime consumer |
| Migration | the durable state transition and affected surface |
| Documentation | intended audience and the system or workflow explained |
| Entrypoint | accepted invocation and where control is dispatched |

Examples: `Boot renderer — composes each shell''s boot document from DB
identity, memory, map header, and dev-kit inventory into CLAUDE.md/AGENTS.md`;
`Proves the branch guard refuses commits on the default branch and admits
shell/* bases`.

Apply the standard incrementally: a new or changed file, a NULL description, a
shape notice naming the region, or a working shell reporting an inadequate
description. Existing adequate descriptions need no bulk rewrite. Descriptions
survive remap in the live DB but are not snapshot durability; refill after a
fresh rebuild.

```sql
WITH f AS (
  SELECT path, role, desc,
         replace(path, rtrim(path, replace(path,''/'','''')), '''') AS base
  FROM dr_filepath
), g AS (
  SELECT *, CASE WHEN instr(base,''.'') > 0
    THEN substr(base, 1, instr(base,''.'')-1) ELSE base END AS stem
  FROM f
)
SELECT path, role, desc FROM g
WHERE desc IS NULL
   OR (length(stem) >= 5 AND (
       lower(substr(desc, -length(base))) = lower(base)
       OR lower(substr(desc, -length(stem))) = lower(stem)
   ))
ORDER BY (desc IS NULL) DESC, role, path;
```

Update only rows verified against the file. Pass = the worklist is empty +
spot checks per section state behavior the path alone cannot reveal.

### Product DB

Tag the host application''s schema/migrations as product DB, never engine
memory. The live app `.db` is often ignored; tracked schema + migrations are
the durable map anchors.

```sql
UPDATE dr_filepath
SET desc=''Product DB schema — the APP database (NOT engine memory)''
WHERE path=''<app schema file>'';

UPDATE dr_filepath
SET desc=''Product DB migration — change the app schema here''
WHERE path LIKE ''<app migrations dir>/%'';
```

Create an authored section when those files form a real area. Pass = working
shells can identify the app DB definition without confusing it with Subfloor
control-plane state. No product DB -> `N/A`.

## Semantic extractors

Extractors implement `extract(con, repo_root, cfg) -> str` and own only their
semantic `dr_*` rows. They DELETE + repopulate their own derived tables, guard
unparseable files, report best-effort omissions, and never claim exhaustive
coverage.

Adopt an extractor:

1. Inspect stack dependencies/file mix with `sc map-sql`.
2. Author `.sc-state/map_extractors/<name>.py` in the assigned worktree against
   the extractor contract above.
3. Run `sc map-extractor install
   ".sc-state/map_extractors/<name>.py"`. Pass = output
   prints the installed canonical path + SHA-256 matching the authored bytes.
4. NEVER `cp`, `mv`, redirect, or use a file-edit tool into
   another checkout''s `.sc-state/map_extractors/`. The guarded installer is the only
   supported cross-worktree write.
5. Run `sc map`, inspect structure with `sc map-schema <dr_table>`, then query
   rows with `sc map-sql`. Pass = expected semantic rows exist + the map log
   has no extractor failure.
6. Commit + push the authored worktree source. Hand Admin the source path for
   review/merge when finalization names that action. Generated map DB, status,
   receipts, and snapshots stay local-only.

An extractor failure rolls its plug-in writes back while preserving the core
map. Pass = `sc map finalize` reports no failed module and every installed
extractor has matching receipt/source/Admin evidence.

## Shape notices

Sender = the dev/coder shell on merge, not Planner. Open blocking map-quality flags before sending
one notice to the `cartographer` role alias:

```text
shape: <what landed> — paths: <region/>; ref: <feature/doc/PR>
flags: <numeric_id>=<SC-name>[, <numeric_id>=<SC-name>] | none
curate; verify and close each flag; mark this notice read last.
```

Name the durable ref + exact path region. Pair every flag''s numeric DB ID with
its display name. Write `flags: none` when no flag exists. Pass = one notice
carries every map-quality flag opened for that shape change.

On receipt:

1. Parse all three lines. Missing/malformed `flags`, missing flag, or ID/name mismatch -> surface
   the exact defect + leave the notice unread.
2. Run the nested-section + stale-section + description + semantic worklists
   scoped to the named region. Pass = every scoped result is clean.
3. For each pair, run `sc mem get flags <numeric_id>` and confirm the display
   name. An already-resolved row passes only when its notes name the verified
   map result. Otherwise run `sc mem flag close <numeric_id> --notes "<what
   was verified>"`; pass = the exact row is resolved with adequate notes.
4. Run `--message mark-read <message_id>` last. Pass = scoped worklists + every
   named flag passed before the notice became read. Send no closure reply.

## Persistence boundary

Map config, live descriptions, derived rows, install receipts, and generated
status are local-only. Sections persist only after the GUI Snapshot action or
Admin runs `sc snapshot`. NEVER run plain `sc snapshot` from Cartographer; it
is refused. Pass = `sc map finalize` reports Authored sections `PASS` after
Admin acts, without Cartographer mutating snapshot/Git/message/flag state on
their behalf.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'spec',
  'Load before implementing any feature, spec, or roadmap item. Analyze viability, surface blockers, plan Preparation → implementation → Verification, and track spec_tasks/current_state across sessions.',
  'craft',
  NULL,
  0,
  '# spec — analyze and execute a spec

Load before any feature/spec/roadmap build. Missing spec -> author via `docs`.
Analyze before code; ask FnB on ambiguity, flag hard blockers. `<self>` = your
shell id.

## 1. Select the spec

Never auto-pick the latest document. A named task -> load its projection
first as the default planning context:

```text
sc context --task <task_id>
```

It carries task, feature, governing document id + hash, active linked
decisions, feature-level flags, boundaries, and resources. Load the full body
or broader indexes only for an unresolved need:

```text
sc mem get documents --feature <id>
sc mem get documents --doc <doc_id>
sc mem get tasks --doc <doc_id>
```

The list includes `kind`, `seq`, `frozen`, and `task_count`. Resume the unfrozen
spec with tasks; zero tasks = backlog and needs the plan below. Multiple
plausible specs -> ask FnB. Existing tasks -> track the first unfinished one.

## 2. Analyze before planning

Return these findings before any code or task writes:

| Check | Pass condition / action |
|---|---|
| Viability | Bounded, clear entry points, session-sized verification. Otherwise propose a verifiable slice. Missing done-condition = unclear. |
| Current Posture | Preparation code/DB state matches the documented baseline. A mismatch stops for Planner/FnB; never redefine it silently. |
| Scope | Every In Scope promise is planned; every Out of Scope item stays out. New/substantively revised specs contain both `## Current Posture` and `## Scope`; ambiguity stops. |
| Anticipated User Activity | Plan stated roles, audience/reach, access, validation, recovery, authority, safety, process curation, and tenancy invariants. Older absence is allowed; ambiguity is not. |
| Unclear item | Two plausible readings, missing target/interface, or unstated required knowledge -> ask FnB; do not flag a question they can answer. |
| Blocker | Missing prerequisite/environment/external dependency -> open one High feature flag and stop at its boundary. |

```text
sc mem flag open "[Spec] <blocked fact> | Blocker for: <feature>" \
  --name SC-### --priority High --feature <feature_id>
```

## 3. Engage and plan

Building now moves `brainstorm|long_term|near_term` to `in_progress`; planning
ahead -> `next`; matching/later stages and reference reads do not move.

```text
sc mem roadmap status <feature_id> in_progress
```

Confirm `roadmap.project_id`. Assign the obvious stream; ask FnB if ambiguous;
leave an existing assignment unchanged:

```text
sc mem roadmap project <feature_id> <shortname>
```

Create exactly: Preparation, independently verifiable implementation steps,
then Verification. Each write is immediately durable:

```text
sc mem task add "Preparation" --feature <id> --doc <doc_id> --seq 0 \
  --desc "Read code paths, verify DB state, confirm entry points"
sc mem task add "<step>" --feature <id> --doc <doc_id> --seq <n> \
  --desc "<independently verifiable outcome>"
sc mem task add "Verification" --feature <id> --doc <doc_id> --seq <last> \
  --desc "Test every scope promise, done-condition, audience assurance, snapshot, and render"
sc mem state "[<feature>] — last: —. next: Preparation."
```

No task plan = no implementation.

## 4. Execute one task at a time

```text
sc context --task <task_id>
sc mem task start <task_id>
# work and verify only this task
sc mem task done <task_id>
```

After completion, re-read the ledger. Set `current_state` to the highest done
task + lowest pending task. Start the next only after the current task is
verified and durably done. Work moved to another feature/spec is cancelled,
never marked done or left pending under a shipped feature:

```text
sc mem task cancel <task_id> --notes "moved to F<id> as task #<n>"
sc mem state "[<feature>] — last: #<task_id> <last_done>. next: #<task_id> <next_up>."
```

Intact spec move: `sc mem doc move <document_id> --feature <target_feature_id>`
(see `docs`).

Final Verification follows the boot `TESTING POSTURE`. Complete code; run every
available focused proof; use observed registered-PR checks only for an
unavailable local gate: pending -> wait, red -> fix, green -> review. No checks
or untrustworthy watcher after one bounded read -> block; an optional browser
skip is non-failing. Require every In Scope done-condition + Anticipated User
Activity contract; unexpected reach, weakened hardening, or crossed tenancy
fails. A large spec may stop after a verified task slice; leave later tasks
pending and state the next one.

## 5. Ship and hand docs to Planner

All tasks done + Verification green -> deliver:

1. `sc mem roadmap status <feature_id> shipped`
2. Open one Medium docs-pending feature flag.
3. Message the planner: read shipped code, freeze the spec, write a `kind=doc`
   document under the feature, then close the flag. Confirm the durable message.
4. Tell FnB that code shipped and Planner owns freeze + docs. No planner shell ->
   message nobody; tell FnB and leave the flag open.

```text
sc mem flag open "[Docs] <feature> shipped, doc pending | Blocker for: <feature> doc" \
  --name SC-### --priority Medium --feature <feature_id>
```

Do not freeze or author the shipped doc as Developer.

## Scope change and stop rules

Small growth within the same intent -> revise the unfrozen spec with `docs`, add
tasks, continue. A separate mental model -> stop and recommend a new feature +
spec; never absorb it silently.

Stop for unresolved ambiguity/blocker, at the end of a verified session slice,
or after the shipped/docs handoff. `current_state` must always point to the
durable plan rather than reproduce its rationale.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_dev',
  'Execute a Sprints v2 Developer lane — accept one assignment, implement and verify it, own the PR through green and review, merge only after live authorization, and record judgment without overlapping edits.',
  'workflow',
  NULL,
  0,
  '# sprint_dev — own one editing lane

Use for an actionable armed-Sprint assignment. Use the simplest path supported
by current durable state. Treat ownership, lifecycle, writes, and handoffs as
hard boundaries. Repeat a read only when later activity could have changed it
or a command requires live revalidation.

## Route the entry

Load `sprint_dev` on every entry, then classify it:

| Trigger | First read / action |
|---|---|
| Assignment, verdict, question, blocker, relay | Inspect `sc sprint inbox --sprint <id>` once; accept or handle the relevant message. Accepted assignment -> `sc context --work-unit <id>` next. |
| Self-describing engine-wide PR fact | Inspect the fact + registered PR directly. Do not manufacture a Sprint inbox item; check the inbox once immediately before the next typed handoff. |
| Live FnB instruction | Preserve its authority; read only durable state needed for safe action. |

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Accepting starts ownership; decline concretely. After an informational message,
`accept` marks the message read and does not change Sprint or work-unit state.

For an unusable bookkeeping receipt, retry the exact command once, then use its
normal read surface once to prove the postcondition. For informational `accept`,
prior inbox presence + absence of that exact message id proves the read landed;
name the defect next handoff. NEVER infer assignment ownership, review outcome,
merge authorization, lifecycle/work-unit transition, governing revision, PR
head/green state, or cleanup authority. An unproved postcondition stops.

Assignments and review requests use Force-new delivery; verdicts and PR-event
wakes use Re-enter. Delivery waits for a natural boundary; the runtime owns
bundling, rotation, and recovery. Stop after a successful typed handoff.

## Bound the lane

`sc context --work-unit <id>` is the default planning context: assignment,
expected output, linked tasks, bound revision id, active decisions,
dependencies, unit blockers, roles, worktree, lifecycle walls, resources. Read
the full bound revision or broader indexes only for an unresolved need. Own
one active unit; never start another lane or edit another shell''s worktree. Resolve ambiguity to shippable in-scope work + rationale. Ask
Planner before changing boundary, interface, deliverable, priority, or scope.

Put one question, blocker, decision, answer, or useful context item in a short
body file. Unit questions/blockers require a reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent question --requires-reply --work-unit <work-unit-id> \
  --key <stable-key>
```

Use `--intent blocker` for a blocked lane. Cross-unit, closeout, or external
authority rulings are Sprint-level decisions:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level --key <stable-key>
```

Reply through the original message; the server inherits its scope, so never add
`--work-unit` or `--sprint-level` to a reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent information --reply-to <message-id> --key <stable-key>
```

Ask the Reviewer about review evidence and the Planner about scope or
cross-unit authority. Confirm the durable reply, then `accept` the incoming
message. At a decision boundary, stop until the required answer arrives;
unread recovery re-wakes, so send no duplicate reminder.

Stable key = recipient + exact body + intent + reply + scope. Reuse it only for
the same failed/ambiguous write; when any of those fields changes, use a new
key. Keep bodies near 6,000 characters and below
8,000; run `wc -m < <path>`. Handoff completes only when the command exits
successfully and confirms durable state + wake. If a command is rejected or
transport fails, correct/retry. If relay itself fails, give FnB command +
evidence + impact + recommendation; invent no alternate protocol.

A Developer does not pause the Sprint. Report blocker or integrity evidence to
the Planner, continue safe independent work, and stop at the unsafe boundary.
The Reviewer decides continue/replan/pause; the Planner executes the decision.

Scratch proof/diffs/reports -> gitignored `shared/sprints/sprint-<n>/`; never
commit/PR them. Durable judgment -> `record-review`; reports -> `sprint_reports`;
decisions -> relay.

## Build and verify

Sync + branch; implement the smallest complete change. Per boot `TESTING
POSTURE`, finish code + run every available smallest affected gate. If the
selected interpreter, runner, or declared dependency cannot execute one,
record exact seat evidence; the registered PR supplies only that proof. Test
assertion/source collection red or incomplete code = failure. Optional browser
skip = non-failing. Keep external calls outside DB transactions; preserve
durable identities and append-only evidence. Record failures, anomalies,
retries, review friction, and departures for closeout.

Immediately before `complete-unit`, `register-pr`, or `request-review`, re-run
`sc sprint inbox --sprint <id>` once and act on new messages. After the typed
handoff confirms its durable write, stop without another inbox pass. The
reopened-PR route below is the sole exception.

## Report-only or no-code completion

Only an explicitly planned report/no-code lane may finish without a PR. Keep
the result near 6,000 characters and below 8,000; run `wc -m < <path>`, perform
the pre-handoff inbox check, then require a durable completion receipt:

```text
sc sprint complete-unit --sprint <id> --work-unit <id> \
  --result-file <path>
```

Stop after success. A code lane continues through merge observation.

## Register and observe the PR

```text
sc sprint register-pr --sprint <id> --repository <owner/name> \
  --pr <number> --work-unit <id>
```

Register complete code even when a local gate is unavailable; registration
obtains evidence, not review. After `register-pr` succeeds, retain ownership;
Red/green/closed/merged Re-enter wakes continue (the engine may already have
discovered the PR from your worktree branch; `register-pr` attaches it). Required checks: pending -> native
wake; red -> fix/push; green -> judge/request review; none or untrustworthy
watcher after one bounded read -> report + block. Follow context: armed -> fix
red + judge/pass green + merged -> post-merge handoff; paused -> fix red now +
judge green, review after resume; no active Sprint -> fix red if needed, green
arrives only as red recovery, merged -> git skill after-merge cleanup.
Planner/Reviewer get none.

If the same registered PR was externally closed, then reopened, rebased, and
pushed, replay the exact `register-pr` command. Require `created: false`, which
keeps identity/ownership and takes a fresh snapshot. Its one pre-handoff inbox
check covers registration replay + the immediately following review request.
Do not wait for a second PR-fact wake: immediately request review. Green
proceeds; any other snapshot returns the watcher diagnostic without partial
handoff. Never register a replacement PR or ask the Planner to bypass observed
green.

Otherwise, when no local action remains, stop for the native PR fact. Start no
recurring loop, scheduled job, daemon, or external watcher. A stalled gate
permits one bounded read, then stop or report its evidence:

```text
sc sprint watcher-state --sprint <id>
```

Do not repeat this read as a polling loop.

## Review handoff and correction

Complete each round in order:

1. Finish readiness judgment + available local proof; require observed green.
2. Perform the once-only inbox check; handle and `accept` new messages.
3. Use `submit` first or `resubmit` after changes requested. The engine injects
   the PR URL, registered id, exact green head, and work-unit id into the
   Reviewer''s canonical bare one-line locator. Create no readiness file. Send
   no scope narrative, verification evidence, rationale, or review-focus
   steering. Put only the work-unit id and spec reference in the PR body; write
   no PR comments or annotations.
4. As the literal final action, run:

```text
sc sprint request-review \
  --sprint <id> --registered-pr <registered-id> \
  --intent <submit|resubmit> --key <stable-key>
```

5. Require confirmation of the durable write + Reviewer wake; run no trailing
   command and stop and await the native verdict wake.

Changes requested returns by Re-enter. Apply every blocking finding,
re-establish green, and resubmit with a new review-round key. Do not narrate
cleared findings; the Reviewer verifies the full diff at the engine-injected
head. Record disagreements as judgment. Reviewer owns scope/severity; Planner
executes resulting action.

## Merge boundary

Approval is stale evidence. Immediately before merge, re-read live GitHub,
grant, ownership, unit state, approved head, and checks through:

```text
sc sprint authorize-merge \
  --sprint <id> --registered-pr <registered-id>
```

Merge only the returned repository, PR, and head SHA. A refusal means wait for
the watcher or re-enter the appropriate loop; never bypass it.

## Post-merge handoff

After the authorized merge:

1. Clean the worktree; put merged PR + SHA, unit result, verification,
   judgments, and departures in the handoff file.
2. Re-run `sc sprint inbox --sprint <id>` once; handle and `accept` new items.
3. Run `wc -m < <path>`; keep the body near 6,000 characters and below 8,000.
4. As the literal final action, send:

```text
sc sprint send --sprint <id> --to <planner-shortname> --body-file <path> \
  --intent handoff --key <stable-merged-handoff-key>
```

5. Require the durable message + Planner wake, then stop immediately. Run no trailing Git,
   Sprint, inbox, cleanup, or status command. Automatic merge
   observation records the PR transition; this handoff releases the next wave.

## Report and stop

Report broken bases, destructive ambiguity, unavailable GitHub, untrustworthy
runners, provider exhaustion, or unrecoverable environment with evidence,
impact, and recommendation. Stop when merged + reported, declined, returned to
review, paused for a native wake, or awaiting Planner/FnB recovery. Ask for
later work only after this editing lane is terminal.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'surface_catalogue',
  'Read the host repo''s dr_* catalogue (sections, file behavior, deps, env, semantic layer) as abbreviated source documentation — one navigation resource beside grep, direct reads, and docs. Use to orient in an unfamiliar repo fast.',
  'substrate',
  NULL,
  1,
  '# surface_catalogue — the repo map as abbreviated documentation

The `dr_*` catalogue is a scan of the host repo: a resource for orienting, not
a required first step. Use it, grep, read files directly, read repository docs,
or use harness-native search as the work warrants. It is separate from Subfloor
control-plane memory and from the product''s runtime database. Inspect structure
with `sc map-schema`; query data with `sc map-sql "…"`.

NEVER map the repo yourself. The map stays fresh automatically (git hooks
re-map on pull / branch-switch / rebase) and is owned by the **cartographer**
shell. Empty / stale / wrong map -> flag the cartographer, don''t re-map.

| Table | Holds |
|---|---|
| `dr_repo` | the repo: name, root, remote, vcs, default_branch, file_count, mapped_at |
| `dr_section` | the navigational index: `name`, `path_prefix`, `description` — "UI here / API here / docs here". Rendered in the boot `## CONNECTIONS` block; start here. |
| `dr_filepath` | one row per file: `path`, `ext`, `lang`, `role` (code/doc/config/test/asset/env), `bytes`, `lines`, `desc` (cartographer one-line behavior: responsibility, mechanism, input, output; NULL until curated) |
| `dr_dependency` | deps from the manifests: `manager` (npm/pip/poetry/go/cargo), `name`, `version`, `kind`, `source_file` |
| `dr_env` | env-var names found in `.env.*` example files: `name`, `source_file` |
| `dr_endpoint` | HTTP routes: `method`, `path`, `handler` (file:line), `framework`, `source_file` |
| `dr_db_table` / `dr_db_column` | the app DB schema: tables/views + their columns (`type`, `pk`, `not_null`) |
| `dr_route` / `dr_component` | UI routes (`path`, `kind`) + components (`name`, `path`) |

First five = mapped on EVERY repo. Last three = the semantic layer, populated
only when the cartographer wired an extractor for this repo''s stack (see the
`cartographer` skill). Empty `dr_endpoint` = no extractor wired, NOT "no
endpoints" — check before relying on it; flag the cartographer if a dimension
you need is missing.

## Orient fast

Boot `## CONNECTIONS` already shows the section index. Cheap flow: pick a
section there -> query that section''s leaves (file names + descriptions) ->
read the one or two files you need. One query deep beats a full preload.

Run `sc map-schema` before the first structural query; pass = it lists the
expected `dr_*` object. Run `sc map-schema <dr_table>` before using unfamiliar
columns; pass = ordinal/name/type/nullability/default/PK + indexes are explicit.
Use `sc map-sql` only for data queries.

```sql
-- all of these run against the map db:  sc map-sql "<query>"
-- the section index (same as boot CONNECTIONS) — where to start:
SELECT name, path_prefix, description FROM dr_section ORDER BY sort_order, name;

-- a chosen section''s leaves — the descriptions tell you which file to open:
SELECT path, desc, lines FROM dr_filepath
WHERE path LIKE ''shell_core/api/%'' ORDER BY path;

-- the synthetic Repository Root group (not an authored dr_section row):
SELECT path, desc, lines FROM dr_filepath
WHERE instr(path, ''/'') = 0 ORDER BY path;

-- what is this repo + how big:
SELECT name, default_branch, file_count, mapped_at FROM dr_repo;

-- language mix:
SELECT lang, COUNT(*) n, SUM(lines) lines FROM dr_filepath
WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;

-- where the code lives (skip docs/config/assets):
SELECT path, lang, lines FROM dr_filepath WHERE role=''code'' ORDER BY lines DESC;

-- find files by area (grep or open them directly afterwards):
SELECT path FROM dr_filepath WHERE path LIKE ''%auth%'';

-- stack + config surface:
SELECT manager, name, version FROM dr_dependency ORDER BY manager, name;
SELECT name, source_file FROM dr_env ORDER BY name;

-- semantic layer (only if an extractor is wired for this repo — see cartographer):
SELECT method, path, handler FROM dr_endpoint ORDER BY path;            -- the API surface
SELECT name, kind, source_file FROM dr_db_table ORDER BY name;          -- the app DB schema
-- table_name is a string ref (cache; no FK): schema + migration files each
-- contribute their own copy of a table''s columns — select source_file and
-- read one source''s rows, or expect duplicates:
SELECT source_file, name, type, pk, not_null FROM dr_db_column
WHERE table_name=''users'' ORDER BY source_file;
SELECT path, kind, file FROM dr_route ORDER BY path;                    -- UI routes
```

## Stance

- **Any method.** The catalogue is a resource, not a mandate. Its value is the
  per-file behavioral `desc` and the semantic layer when wired; grep, direct
  reads, docs, and harness-native search remain equally valid.
- **Lazy-load.** Pull a file''s contents once you know you need it. Carry the
  map, not the territory.
- **Map looks wrong?** Empty, stale (repo changed since `mapped_at`),
  mis-classified, a nested file under "other / unsectioned", or a `desc IS
  NULL` where you needed one -> Cartographer worklist item. Root files belong
  to `Repository Root`, not the unsectioned worklist. Flag the gap and keep
  working with another method; don''t author the map yourself.
- **Semantic layer when wired.** Endpoints / DB schema / UI routes let you
  jump straight to the API surface or schema; a dimension is empty -> fall
  back to section + descriptions. Symbol-level semantics (functions/classes)
  are a later pass.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
