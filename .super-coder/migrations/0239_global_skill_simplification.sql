-- 0238 — converge the global skill namespace and standard flavor packs.
--
-- Fresh builds receive full global bodies from regenerated 0001. In-place
-- update syncs that same seed after migrations. This delta removes retired
-- authority immediately, publishes the fork-local dev_kit starter body, and
-- establishes the exact engine-managed flavor matrix without deleting grants
-- for differently named genuine fork-local skills.

BEGIN;

DELETE FROM shell_skills
WHERE skill_id IN (
  SELECT skill_id FROM skills WHERE name IN (
    'agents', 'api-design', 'app_deploy_setup', 'authoring_syntax',
    'blueprint', 'configure_winbox', 'database-migrations',
    'local_skill_management', 'migration_management', 'pm2',
    'query_authoring_pg', 'tailscale', 'test_authoring', 'windows_devkit',
    'windows_vm_gui'
  )
);

DELETE FROM flavor_skills
WHERE skill_id IN (
  SELECT skill_id FROM skills WHERE name IN (
    'agents', 'api-design', 'app_deploy_setup', 'authoring_syntax',
    'blueprint', 'configure_winbox', 'database-migrations',
    'local_skill_management', 'migration_management', 'pm2',
    'query_authoring_pg', 'tailscale', 'test_authoring', 'windows_devkit',
    'windows_vm_gui'
  )
);

DELETE FROM skills WHERE name IN (
  'agents', 'api-design', 'app_deploy_setup', 'authoring_syntax',
  'blueprint', 'configure_winbox', 'database-migrations',
  'local_skill_management', 'migration_management', 'pm2',
  'query_authoring_pg', 'tailscale', 'test_authoring', 'windows_devkit',
  'windows_vm_gui'
);

UPDATE skills SET is_deleted=0
WHERE name IN ('self_update', 'snapshot');

INSERT INTO skills (
  name, description, category, command, common, content, is_deleted
) VALUES (
  'harness_readiness',
  'Read Subfloor harness/model support states, refresh the supplied local evidence, run bounded compatibility checks, and prepare an exact upstream handoff when the installed runtime is unqualified. Developer-only.',
  'substrate',
  NULL,
  0,
  '# harness_readiness — qualify the installed route

Subfloor reports maintained harness support as `tested`, `best-effort`, or
`newer-unverified`. These states describe source evidence; they do not hide a
locally discovered model or silently substitute another route.

## Read the supplied evidence

```bash
sc harness-status
sc models refresh
sc models list <harness>
sc models resolve <harness> <selector> [--effort <level>] --json
```

Record the complete version line, active host/container seat, exact selector,
effort, evidence source, digest/fingerprint, and resolve result. Pass = list and
resolve agree on the same fresh local route. A public model absent from local
evidence remains unavailable for that account; an unsupported effort fails
before dispatch.

## Use the smallest available compatibility check

When the FnB authorizes a provider call or harness refresh, exercise the exact
installed model/effort through the fork''s declared hook or the adapter''s native
one-shot surface. Pass = one request uses the requested route, returns parseable
events and session identity, and performs no fallback or changed-effort retry.

`sc update-harnesses`, sandbox rebuild, provider-token use, and session restart
remain operator-authorized boundaries. A host result does not prove the
container seat, and a passing newer build does not promote the maintained
source baseline.

## Hand source maintenance upstream

Use `issue_reporting` when the installed version or adapter contract remains
unqualified. Include the complete version line, seat and engine commit,
selector/effort, status/list/resolve outputs, sanitized native-check result,
expected versus actual behavior, and the narrow failing boundary.

Tracking forks do not edit materialized `.super-coder/` metadata or adapters.
Pass after a published fix = the exact build reports `tested`, simulated newer
builds remain `best-effort`, and the local route still resolves from fresh
evidence after the authorized update/restart.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description,
  category=excluded.category,
  command=excluded.command,
  common=excluded.common,
  content=excluded.content,
  is_deleted=0;

INSERT INTO skills (
  name, description, category, command, common, content, is_deleted
) VALUES (
  'dev_kit',
  'Read the fork''s declared development hooks, execution seat, readiness state, evidence locations, and supported recovery surfaces. Planner-only on demand; Developer and Reviewer receive the same inventory in boot.',
  'substrate',
  NULL,
  0,
  '# dev_kit — read the fork development contract

The fork owns `.subfloor/dev-kit.json`. Subfloor validates the declaration,
selects the invoking checkout and host/container seat, runs exact hook argv,
and retains readiness evidence under `.sc-state/local/dev-kit/`. The engine
does not infer project policy from manifests or install privileged host tools.

## Hook inventory

The supported hook names are `deps`, `test`, `lint`, and `typecheck`:

```bash
sc deps [args...]
sc test [args...]
sc lint [args...]
sc typecheck [args...]
```

Read the declaration and its executable before running a hook. A configured
hook reports the selected checkout, cwd, seat, executable, and child status.
An absent hook is `unavailable`; do not reconstruct one from package metadata.

## Canonical states

| State | Meaning | Supported recovery |
|---|---|---|
| `absent` | No declaration exists; engine-baseline tools remain mechanisms, not project policy. | Add a tracked declaration only when the fork needs one. |
| `declared` | The declaration is valid; hook configuration is known, but execution/receipt evidence decides readiness. | Run the exact configured hook. |
| `invalid` | Declaration, path, mount, image, or invocation validation failed. | Correct the named tracked input and retry. |
| `ready` | The hook can execute on the active seat or the exact Docker receipt is current. | Continue. |
| `failed` | A declared hook or provisioning attempt ran and failed. | Inspect retained logs/evidence; retry the same supported surface. |
| `stale` | Docker provisioning or package evidence no longer matches the declaration, checkout, image, or labels. | From the host, run `sc launch`; use repair only after a failed attempt. |
| `advisory` | Engine baseline is runnable while a declared native-package candidate is degraded. | Inspect the named advisory evidence and submit a reviewed tracked remediation. |
| `repair` | A retained-container repair session is open without readiness. | Exit to the host, rerun `sc launch`, and require `ready`. |

Unavailable executable = exit 126; missing hook = exit 78; invalid
configuration = exit 64. A started child preserves its own status.

## Seats and evidence

Host hooks use the host checkout and toolchain. Container hooks use the
bind-mounted checkout, engine-baseline tools, declared sandbox extension, and
current provisioning receipt. `$SC_DEV_PORT` is loopback-bound on the host and
published from `0.0.0.0` in the container. A configured `$DATABASE_URL` reaches
the fork application sidecar; it never points at the engine memory DB.

Full hook output is available with `SC_DEVKIT_OUTPUT=full`; retained
provisioning/readiness evidence lives under `.sc-state/local/dev-kit/`. Planner
uses this skill for pinch-hit development and capability design. It describes
the available surface and boundaries, not the fork''s test assertions,
deployment ritual, database technique, or VM lifecycle.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description,
  category=excluded.category,
  command=excluded.command,
  common=excluded.common,
  content=excluded.content,
  is_deleted=0;

INSERT INTO skills (
  name, description, category, command, common, content, is_deleted
) VALUES (
  'engine_migrations',
  'Maintain Subfloor''s schema baseline, ordered migration ledger, live-DB backup boundary, rebuild/update compatibility, and source-repository migration files. Admin-only by default.',
  'substrate',
  NULL,
  0,
  '# engine_migrations — maintain Subfloor''s database floor

Subfloor owns `.super-coder/schema.sql` as the current baseline and
`.super-coder/migrations/*.sql` as ordered additive deltas. The
`schema_migrations` ledger applies each delta once. `sc rebuild` creates the
baseline, applies every migration, then restores instance content; `sc update`
materializes source and reconciles migrations before the next boot.

## Author in the source repository

Add one next-numbered migration. Keep historical migrations append-only and
change `schema.sql` only when the current baseline itself must describe a new
schema object. Never fold an already shipped delta into the baseline in a way
that makes rebuild apply it twice.

For seeded system content, update the authoritative asset or generator and add
a trailing reconciliation migration. Preserve per-instance rows carried by the
snapshot. Pass = fresh build, in-place migration, and rebuild from an older
snapshot converge to the same state.

## Protect the live cache

The live engine DB is `.super-coder/shell_db.db` in the main checkout, not a
Developer worktree. Before an authorized live migration, resolve that exact
path and create the workflow''s WAL-safe backup. The FnB owns the restart and
cutover boundary. Never point engine work at `$DATABASE_URL`; that variable is
for the fork application''s database.

## Verify compatibility

Run the migration on a dirty fixture containing the stale rows it must
reconcile, then run it again. Require:

- one application recorded in `schema_migrations`;
- identical desired state after repeated migration and rebuild;
- preserved shell memory and genuine fork-local content;
- no stale grant, projection, or system row restored by an older snapshot; and
- the running engine healthy after the authorized restart.

Stop before live application when the backup, exact DB path, compatibility
fixture, or FnB maintenance authority is absent.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description,
  category=excluded.category,
  command=excluded.command,
  common=excluded.common,
  content=excluded.content,
  is_deleted=0;

INSERT INTO skills (
  name, description, category, command, common, content, is_deleted
) VALUES (
  'fork_skill_design',
  'Design and maintain DB-canonical fork-local skills that describe the fork''s real systems, tools, testing seats, and core processes. Planner-only; use when a capability needs durable shell guidance without becoming global doctrine.',
  'substrate',
  NULL,
  0,
  '# fork_skill_design — describe fork capabilities

Use a fork-local skill when shells need durable knowledge specific to this
repository, stack, host, VM, deployment surface, database, or core fork
process. Keep global skills limited to Subfloor itself, supplied tools and
testing environments, and core Subfloor processes.

## Discover the real capability

Read the repo map, tracked configuration, declared dev-kit hooks, and current
readiness evidence before drafting. Identify:

- the capability and the shells that need it;
- its tracked declaration or owning source;
- the seat, host, VM, service, or database it reaches;
- readiness states and evidence locations;
- authority, recovery, and data-tenancy boundaries; and
- one observable success receipt.

Pass = every operational claim names evidence available in this fork. Do not
infer package managers, test policy, credentials, hosts, or deployment steps.

## Apply the purpose test

Keep a line only when it explains this fork, a supplied tool or testing
environment, or a core fork process. Use an imperative only when variation
would break shared state, authority, compatibility, or recovery. Remove generic
planning, coding, API, test, database, deployment, VM, and troubleshooting
method.

## Draft and persist

Write a Planner-owned draft with a lowercase underscore name and
`common: false`:

```yaml
---
name: repo_capability
description: State the capability and when it fires.
category: substrate
common: false
---
```

Describe locations, commands, states, boundaries, and receipts. A testing-seat
skill identifies the runner, fixtures, reach, readiness, and evidence; it does
not choose assertions. A VM or host skill identifies the supplied control
surface and reset boundary; it does not invent a lifecycle. A deployment or
database skill records the fork''s tracked procedure and authority; it does not
teach generic deployment or SQL technique.

Persist and grant through the supported DB-canonical surface:

```bash
sc skill put --file <path/to/SKILL.md>
sc skill grant <skill_name> <shell>...
sc skill list
```

`put` succeeds only after DB, local snapshot, flat catalogue, and managed skill
projections reconcile. Naming a standard shell changes its shared flavor pack;
naming a Bespoke shell changes only that shell. Creation grants nothing.

## Update, retire, and recover

```bash
sc skill put --file <path/to/SKILL.md>
sc skill revoke <skill_name> <shell>...
sc skill rm <skill_name>
```

Retry the exact command after fixing a reported snapshot, render, or projection
path. Pass = the full persistence receipt returns and the projected body
matches `sc skill list` plus the intended grant. `rm` is only for fork-local
names; retire an upstream skill with `sc skill retire <name>` and restore it
with `sc skill unretire <name>`.

Never place a fork-local body under `.super-coder/assets/skills/`, regenerate
the engine seed for it, set it common, or write the engine DB directly.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description,
  category=excluded.category,
  command=excluded.command,
  common=excluded.common,
  content=excluded.content,
  is_deleted=0;

-- Remove active-process routes to retired global doctrine. These exact
-- transforms keep migration replay identical to the authoritative assets
-- without restating five otherwise unchanged process bodies in this delta.
UPDATE skills SET content=REPLACE(
  content,
  'When FnB explicitly invoked `--agents`, load `agents`; its adjudicated waves
overlay this loop. Otherwise:

',
  ''
) WHERE name='spec';

UPDATE skills SET content=REPLACE(
  content,
  '**Agents overlay:** this shell granted `agents` + FnB invoked `--agents` ->
that skill''s overlay fans this step out to an adversarial finding-panel.
Load it and apply it on top of this step. Steps 1, 3, and 4 stay yours,
unchanged.

Apply every axis on every review, plus the granted *lenses* matching what
the diff touches:',
  'Apply every axis on every review, plus any granted fork-local capability skill
matching what the diff touches:'
) WHERE name='review';

UPDATE skills SET content=REPLACE(
  content,
  '| an API / endpoint / route | `api-design` → *Review lens* |
| `tests/` | `test_authoring` → *Review lens* |
| schema / migration | `database-migrations` |
',
  ''
) WHERE name='review';

UPDATE skills SET content=REPLACE(
  content,
  'A granted skill that declares it supersedes a lens (says so in its
description — e.g. a fork-local testing skill superseding `test_authoring`)
-> use the superseding skill: it carries the fork''s actual standard.',
  'A matching fork-local skill carries the fork''s actual environment, tools, and
process boundary; the three axes above remain the review contract.'
) WHERE name='review';

UPDATE skills SET content=REPLACE(
  REPLACE(
    content,
    '- `local_skill_management` — fork-local skills persist via the local snapshot.',
    '- `fork_skill_design` — DB-canonical fork-local skills persist via the local
  snapshot.'
  ),
  '- `migration_management` — a **content-seed** migration (skills, flavor
  defaults) changes what renders; rebuild + render + `render-check` after.',
  '- `engine_migrations` — a **content-seed** migration (skills, flavor defaults)
  changes what renders; rebuild + render + `render-check` after.'
) WHERE name='snapshot';

UPDATE skills SET content=REPLACE(
  REPLACE(
    REPLACE(
      content,
      '| **Fork — don''t** | the repo''s app code, fork-local skills (see `local_skill_management`), operator-owned host config |',
      '| **Fork — don''t** | the repo''s app code, DB-canonical fork-local skills, operator-owned host config |'
    ),
    '| A skill instructs tools/paths your seat doesn''t have | `configure_winbox` drove raw `ssh`/`virsh` — neither exists in the broker-only sandbox (#248) |',
    '| A skill instructs tools/paths your seat doesn''t have | a sandbox skill drove raw host-only `ssh`/`virsh` paths (#248) |'
  ),
  'create no local skill or asset. Deliberate fork-specific authoring remains the
administrator-owned workflow in `local_skill_management`.',
  'create no local skill or asset. Deliberate fork-specific authoring remains the
Planner-owned workflow in `fork_skill_design`.'
) WHERE name='issue_reporting';

UPDATE skills SET content=REPLACE(
  content,
  'Deliberate fork-specific skill authoring is separate from curation and remains
administrator-owned. The admin follows `local_skill_management`: authored
asset → explicit seed → grant → snapshot → render.',
  'Deliberate fork-specific skill authoring is separate from curation and remains
Planner-owned. The Planner follows `fork_skill_design`: draft → DB persist →
grant → projection and snapshot receipts.'
) WHERE name='curate';

-- Remove only upstream-managed/starter grants. Differently named local skills
-- keep their standard-pack assignments.
DELETE FROM flavor_skills
WHERE skill_id IN (
  SELECT skill_id FROM skills WHERE name IN (
    'admin_git', 'bootstrap', 'cartographer', 'curate', 'db_map', 'dev_kit',
    'docs', 'engine_migrations', 'flag_sweep', 'flags', 'fork_skill_design',
    'git', 'git_cleanup', 'harness_readiness', 'issue_reporting', 'memory',
    'messaging', 'onboard', 'redline_review', 'review', 'self_update',
    'snapshot', 'spec', 'sprint_close', 'sprint_dev', 'sprint_pln',
    'sprint_prep', 'sprint_rev', 'surface_catalogue'
  )
);

WITH standard_flavors(flavor) AS (
  VALUES ('admin'), ('planner'), ('dev'), ('reviewer'), ('devops'), ('cartographer')
), common_skills(skill_name) AS (
  VALUES
    ('bootstrap'), ('curate'), ('db_map'), ('issue_reporting'), ('memory'),
    ('messaging'), ('surface_catalogue')
)
INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT standard_flavors.flavor, skills.skill_id
FROM standard_flavors
CROSS JOIN common_skills
JOIN skills ON skills.name=common_skills.skill_name
WHERE skills.is_deleted=0;

WITH desired_grants(flavor, skill_name) AS (
  VALUES
    ('admin','admin_git'),
    ('admin','git_cleanup'),
    ('admin','engine_migrations'),
    ('admin','self_update'),
    ('admin','snapshot'),
    ('planner','docs'),
    ('planner','flags'),
    ('planner','git'),
    ('planner','onboard'),
    ('planner','flag_sweep'),
    ('planner','fork_skill_design'),
    ('planner','dev_kit'),
    ('planner','sprint_prep'),
    ('planner','sprint_pln'),
    ('planner','sprint_close'),
    ('dev','docs'),
    ('dev','spec'),
    ('dev','flags'),
    ('dev','git'),
    ('dev','redline_review'),
    ('dev','sprint_dev'),
    ('dev','harness_readiness'),
    ('reviewer','review'),
    ('reviewer','flags'),
    ('reviewer','git'),
    ('reviewer','redline_review'),
    ('reviewer','sprint_rev'),
    ('devops','git'),
    ('devops','flags'),
    ('devops','docs'),
    ('cartographer','cartographer'),
    ('cartographer','git')
)
INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT desired_grants.flavor, skills.skill_id
FROM desired_grants
JOIN skills ON skills.name=desired_grants.skill_name
WHERE skills.is_deleted=0;

COMMIT;
