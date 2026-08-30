---
name: fork_skill_design
description: Design and maintain DB-canonical fork-local skills that describe the fork's real systems, tools, testing seats, and core processes. Planner-only; use when a capability needs durable shell guidance without becoming global doctrine.
category: substrate
common: false
---

# fork_skill_design — describe fork capabilities

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
database skill records the fork's tracked procedure and authority; it does not
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

Keep fork-local skill bodies on the supported `sc skill` surface; do not place
them under engine assets, regenerate the engine seed for them, or set them
common.
