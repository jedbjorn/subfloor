-- 0239 — converge the global skill namespace and standard flavor packs.
--
-- Fresh builds receive full global bodies from regenerated 0001. In-place
-- update syncs that same seed after migrations. This delta removes retired
-- authority immediately, upgrades only the untouched fork-local dev_kit
-- starter body while preserving downstream customization, and establishes the
-- exact engine-managed flavor matrix without deleting grants
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

CREATE TEMP TABLE _sc_desired_dev_kit AS
SELECT name, description, category, command, common, content, is_deleted
FROM skills WHERE 0;

INSERT INTO _sc_desired_dev_kit (
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
;

-- dev_kit became fork-owned in 0035. Upgrade only the exact untouched
-- pre-0239 engine starter; any downstream change remains byte-for-byte.
UPDATE skills SET
  description=(SELECT description FROM _sc_desired_dev_kit),
  category=(SELECT category FROM _sc_desired_dev_kit),
  command=(SELECT command FROM _sc_desired_dev_kit),
  common=(SELECT common FROM _sc_desired_dev_kit),
  content=(SELECT content FROM _sc_desired_dev_kit),
  is_deleted=(SELECT is_deleted FROM _sc_desired_dev_kit)
WHERE name='dev_kit'
  AND description='Run fork-owned dev-kit hooks and diagnose host or Docker provisioning states without inferring project policy.'
  AND category='substrate'
  AND command IS NULL
  AND common=0
  AND is_deleted=0
  AND content='# dev_kit — target-aware project tooling

`deps`, `test`, `lint`, and `typecheck` are invariant exact-execution hooks on
both host and Docker seats. The fork owns their argv in the tracked
`.subfloor/dev-kit.json`; the engine validates the declaration, selects the
invoking Git checkout, runs that argv without a shell, preserves child output
and status, and reports the selected checkout, cwd, seat, and executable.

The engine never infers manifests, languages, package managers, tools, file
sets, or acceptance policy. It never installs privileged host packages. A
missing hook is intentionally non-successful, not a request for a fallback.

From a checkout, bare `sc` uses the managed cwd-resolving wrapper on the host
and the equivalent baked wrapper in Docker.
<!-- sc-root-only: the tracked launcher is the fallback when the managed wrapper is unavailable -->
`./sc` remains valid and behaviorally identical for root-checkout commands.

## Read the active seat

Read the boot document''s execution-context section before acting. It is the
authority for this shell''s active seat.

- **Host:** commands and project processes run directly on the host. Respect an
  existing supervisor (`pm2`, `systemd`, or `make`) and bind ad-hoc dev servers
  to `127.0.0.1:$SC_DEV_PORT` unless the task requires another interface.
- **Docker:** the checkout is bind-mounted at its host path. Run a dev server on
  `0.0.0.0:$SC_DEV_PORT`; the published host URL is
  `http://127.0.0.1:$SC_DEV_PORT`. The FnB''s host-supervised app is a separate
  instance.

Host lifecycle remedies such as `sc launch` and `sc enter --devkit-repair`
must be run from a host terminal. If this shell is in Docker, exit the container
before using them. Never restart the FnB''s host stack from a sandbox shell.

## State and remedy contract

User-facing dev-kit output uses these states consistently:

| State | Meaning | Remedy |
|---|---|---|
| **absent** | The message `no fork dev kit declared` means the declaration is absent; the named hook may instead be unconfigured. The engine baseline remains usable, and an absent hook uses exit `78`. | Add or correct the fork-owned declaration only if the fork needs that capability. |
| **invalid** | The declaration, path, mount, image identity, or invocation failed validation before trusted execution. Hook configuration errors exit `64`. | Correct the reported fork-owned file or invocation, then retry the same command. |
| **failed** | A declared hook or provisioning attempt started but did not succeed. Docker retains the container and local attempt evidence and writes no ready receipt. | On the host, inspect `.sc-state/local/dev-kit/`, retry with `sc launch --no-build`, or enter `sc enter --devkit-repair`. |
| **stale** | A declared Docker provision step has no current receipt, or its fingerprint no longer matches the declaration, inputs, checkout, image, or labels. Normal entry is blocked. | On the host, run `sc launch`; if provisioning fails, use the failed/repair path. |
| **advisory** | A declared native apt package or package-dependent candidate failed while the engine baseline remained runnable. Core shell entry stays available; `native_packages=advisory` and `fork_readiness=degraded` are not blocker states. | From the fork root, run `make dos-admin`, inspect the named status/proof evidence and selected baseline, then submit a reviewed tracked remediation. Never infer, rename, unpin, or substitute a package. |
| **ready** | The selected hook can run, or Docker has a current receipt for the exact provision fingerprint and pinned image labels. | Continue with the declared hook or normal `sc enter`. |
| **repair** | An explicit retained-container session is open without a readiness claim. Normal shell entry remains blocked. | Diagnose the declaration/hook, exit to the host, rerun `sc launch`, and require a ready result. |

An unavailable executable exits `126`; a started child keeps its
shell-observable status. `SC_DEVKIT_ROOT`, `SC_DEVKIT_SEAT`, and
`SC_DEVKIT_HOOK` tell fork-owned code which checkout, seat, and hook the engine
selected.

## Ownership layers

- **Engine baseline:** the shipped sandbox image and generic runner. Its baked
  tools are mechanisms, not a promise that a fork uses them.
- **Native packages:** an optional bounded `sandbox.packages.apt` array of exact
  `NAME` / `NAME=VERSION` atoms. The engine installs the canonical array over
  the immutable baseline and proves every package in the final image. Pass =
  the format-version-2 receipt matches the current labels, proof, and checkout.
- **Fork extension:** an optional fork-owned Dockerfile and mounts declared in
  `.subfloor/dev-kit.json`. The Dockerfile must extend `SC_BASE_IMAGE`; the
  engine passes the exact package-layer ID when native packages are declared.
- **Checkout setup:** an optional fork-owned provision hook plus explicit input
  files. A successful receipt is keyed to the declaration, executable, inputs,
  checkout identity, extension image identity, labels, and seat.
- **Host prerequisites:** Git, Docker, language runtimes, credentials, and
  privileged packages installed by the operator. The engine reports missing
  prerequisites; it does not elevate or install them.

Read `.subfloor/dev-kit.json` and its executable before invoking a hook. Run
`sc deps` first only when the declaration makes `deps` the fork''s dependency
policy. A fork may choose a virtualenv, npm, another tool, or no dependency step
at all. In Docker, fork code must treat an out-of-checkout interpreter as
host-managed and shared: verify it, but never install into it.

Treat package advisories as capability evidence, not authorization to edit a
live declaration or restart the sandbox. Inspect `.sc-state/local/dev-kit/` and
the System-managed Flags record. Pass remediation back to the FnB as a reviewed
tracked change; only the FnB authorizes downstream materialization and cutover.

## Verification-seat fallback

A local gate is unavailable only when the selected interpreter, runner, or
declared dependency cannot execute it. A test assertion, source-caused
collection error, red CI result, or incomplete implementation is a failure,
not unavailable infrastructure.

After implementation is complete, a Developer records the selected seat,
executable, and failure; runs every available affected check; opens/registers
the PR; and uses its observed required checks for the unavailable proof.
Pending -> wait for the native fact. Red -> diagnose, fix, and push. Green ->
the proof is complete and review may start. No configured checks or an
untrustworthy watcher after one bounded read -> no trustworthy seat; block the
lane. An optional browser-capability skip is informational and non-failing.

When this registered-PR fallback exists, a Planner NEVER runs `sc deps`,
installs packages/runtimes, edits `.venv` or the dev-kit declaration, or starts
a repair/restart to manufacture a local seat. Keep ownership with the
Developer through the CI route. Only the FnB may authorize a separate tracked
toolchain or environment change.

## Engine-baseline tools

The standard sandbox image includes `rg`, the `sqlite3` CLI, `curl`, Node 22,
npm, pinned `uv` + `pytest`, and Playwright with Chromium at
`PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`. These are available mechanisms,
not inferred lifecycle hooks. On shell boot, an existing assigned-checkout
`.venv/bin` precedes these baseline tools while the checkout root remains first
for bare `sc`; pass = project tools use that checkout''s interpreter without the
engine creating or repairing its environment. A missing `.venv` remains fork
policy through declared hooks. Frontend tools such as `svelte-check`, `tsc`,
and vitest still come from fork-owned dependencies and run only through
declared policy.

After `sc update` changes image-owned tools, run a normal `sc restart` from the
host to build + activate them. `restart --no-build` deliberately retains the
selected image. NEVER install pytest on the host to repair a Docker shell.

## Postgres sidecar (app-only)

When a fork sets `"pg": {}` in `.super-coder/instance.json` (`sc pg-init` adds
it), `sc launch` starts a `postgres:17` sidecar and forwards `DATABASE_URL` into
Docker. This is only the fork application''s database. The engine memory DB is
always SQLite and never reads `DATABASE_URL`.

Inside Docker the app connects by the container hostname in `DATABASE_URL`, not
`127.0.0.1`. The fork owns its Postgres driver and its declared setup/test
hooks. Data persists in the install-owned Docker volume.

An unset `DATABASE_URL` means no sidecar is configured. A set URL with an empty
schema means provision the real app DB through the fork''s migrations and
bootstrap; it is not a blocker and is not permission to create a second
throwaway database.

## Stance

The declaration and active boot seat are the truth. Diagnose the exact state,
use the remedy for that seat, and require observable execution evidence rather
than command narration. Do not convert an absent capability into inferred
policy or a repair session into a readiness claim.';

INSERT OR IGNORE INTO skills (
  name, description, category, command, common, content, is_deleted
)
SELECT name, description, category, command, common, content, is_deleted
FROM _sc_desired_dev_kit;

DROP TABLE _sc_desired_dev_kit;

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

Allocate migrations through the collision-safe source command:

```bash
./sc migration new <lowercase_snake_case_slug>
```

Pass = it reports the created next-numbered path and its source-removal
allowlist entry. Keep historical migrations append-only and change `schema.sql`
only when the current baseline itself must describe a new schema object. Never
fold an already shipped delta into the baseline in a way that makes rebuild
apply it twice.

`0001_seed_skills.sql` is the generated exception: update authoritative global
skill assets, run `./sc seed-skills`, and commit the regenerated 0001 body with
the trailing reconciliation migration. Do not hand-edit 0001 or regenerate it
for fork-local skills.

For seeded system content, update the authoritative asset or generator and add
a trailing reconciliation migration. Preserve per-instance rows carried by the
snapshot. Pass = fresh build, in-place migration, and rebuild from an older
snapshot converge to the same state.

## Protect the live cache

The live engine DB is `.super-coder/shell_db.db` in the main checkout, not a
Developer worktree. Before an authorized live migration, resolve that exact
path independently from the ACTIVE SESSION `floor: live_engine_checkout`, then
use the supported backup-and-apply surface:

```bash
./sc migrate
```

Require its first line, `migrate: db         <absolute-path>`, to match the
independently resolved live DB exactly. The command then reports the migration
source, creates a WAL-safe backup with a `premigrate` restore point for an
existing DB, and reports each applied filename plus the final count (or
`nothing pending`). Pass = the backup receipt names its restore path before the
first migration applies. A DB-path mismatch stops the operation. The FnB owns
the restart and cutover boundary. Never point engine work at `$DATABASE_URL`;
that variable is for the fork application''s database.

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

-- Existing Reviewer rows carry their own rendered system prompt. Remove the
-- retired skill name there as well as in the source template.
UPDATE shells SET system_prompt=REPLACE(
  system_prompt,
  'Read the new tests as a diff (test_authoring lens):',
  'Read the new tests as a diff:'
) WHERE flavor='reviewer';

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
